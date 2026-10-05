"""Configuration loading for InSilicoTrial MAS.

Precedence (lowest to highest):

1. dataclass defaults,
2. YAML files referenced by ``--config``,
3. ``INSILICO_<SECTION>__<FIELD>`` environment variables,
4. explicit CLI overrides applied by the caller.

The loader deliberately avoids ``pydantic-settings`` so that the package imports
cleanly on a bare Databricks Runtime (which does not ship it) while still giving
typed, validated configuration objects.
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeVar, get_args, get_origin, get_type_hints

import yaml

from .errors import ConfigurationError

ENV_PREFIX = "INSILICO_"
T = TypeVar("T")

EngineBackend = Literal["auto", "sequential", "local", "spark"]
LLMProviderName = Literal["offline", "mock", "bedrock", "openai", "langchain"]
LLMMode = Literal["off", "sample", "triggered", "all"]
StorageBackend = Literal["local", "delta", "memory"]


@dataclass
class StorageConfig:
    """Medallion-architecture locations, Unity Catalog naming and local paths."""

    backend: StorageBackend = "local"
    catalog: str = "trial_simulations_prod"
    bronze_schema: str = "bronze"
    silver_schema: str = "silver"
    gold_schema: str = "gold"
    local_root: str = "artifacts/lake"
    #: Overrides the ``catalog.schema`` prefix with a physical path/URI (S3, DBFS, UC volume).
    root_uri: str = ""
    delta_partition_by: list[str] = field(default_factory=lambda: ["arm_id"])
    write_mode: Literal["append", "overwrite"] = "append"
    time_travel_versions_kept: int = 32

    def schema_for(self, logical: str) -> str:
        """Map a medallion layer name onto the physical schema.

        ``"silver"`` -> ``storage.silver_schema``; anything unknown is passed
        through unchanged so that an ad-hoc schema name still works.
        """
        mapping = {
            "bronze": self.bronze_schema,
            "silver": self.silver_schema,
            "gold": self.gold_schema,
        }
        return mapping.get(logical, logical)


@dataclass
class EngineConfig:
    """Execution-engine selection and worker tuning."""

    backend: EngineBackend = "auto"
    max_workers: int = 0  # 0 -> os.cpu_count()
    batch_size: int = 256
    partition_mode: Literal["cohort", "balanced"] = "cohort"
    cohorts_per_partition: int = 1
    ray_arrow_batches: int = 16
    fail_fast: bool = False
    #: Databricks Community Edition / single-node driver usage.
    single_node: bool = False
    spark_shuffle_partitions: int = 0  # 0 -> derived from cohort count
    spark_max_records_per_batch: int = 2048
    spark_local_dir: str = ""


@dataclass
class LLMConfig:
    """Synthetic Patient Persona LLM settings."""

    provider: LLMProviderName = "offline"
    model: str = ""
    region: str = "us-east-1"
    temperature: float = 0.2
    max_tokens: int = 512
    timeout_seconds: float = 30.0
    max_retries: int = 3
    requests_per_second: float = 8.0
    burst: int = 16
    max_concurrency: int = 32
    cache_enabled: bool = True
    cache_path: str = "artifacts/llm_cache.jsonl"
    endpoint_url: str = ""
    #: Extra provider options merged into the LangChain client constructor.
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class MLConfig:
    """Physiology model and MLflow model registry settings."""

    backend: Literal["mechanistic", "ridge", "xgboost", "auto"] = "auto"
    model_path: str = "artifacts/models/physiology_model.json"
    registry_uri: str = ""
    auto_train_if_missing: bool = True
    training_rows: int = 20000
    training_seed: int = 7
    ridge_alpha: float = 1.0
    use_mlflow_registry: bool = False
    mlflow_model_name: str = "insilico_physiology"
    mlflow_stage: str = "latest"


@dataclass
class TrackingConfig:
    """MLflow experiment tracking / tracing."""

    enabled: bool = True
    tracking_uri: str = ""
    experiment_name: str = "/Shared/insilico_trial_mas"
    run_name: str = ""
    log_artifacts: bool = True
    trace_path: str = "artifacts/traces/llm_traces.jsonl"
    register_model: bool = False
    strict: bool = False  # if True, tracking failures raise instead of warn


@dataclass
class SimulationConfig:
    """Top-level knobs for one simulation run."""

    protocol_path: str = "conf/trial_protocol_demo.yaml"
    n_patients: int = 1000
    n_cohorts: int = 8
    seed: int = 20240517
    epochs: int = 0  # 0 -> protocol value
    llm_mode: LLMMode = "triggered"
    llm_sample_rate: float = 0.05
    llm_trigger_grade: int = 2
    output_dir: str = "artifacts"
    report_formats: list[str] = field(default_factory=lambda: ["markdown", "html", "json"])
    include_baseline_epoch: bool = True
    export_cdisc: bool = True
    #: Build the interactive HTML dashboard (report/dashboard.html) after the run.
    export_dashboard: bool = True
    arm_overrides: dict[str, float] = field(default_factory=dict)
    storage: StorageConfig = field(default_factory=StorageConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    ml: MLConfig = field(default_factory=MLConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    #: Guard-rail: refuse obviously unrealistic cohort sizes instead of OOM-ing a cluster.
    max_patients: int = 2_000_000
    strict_engines: bool = True

    # -- derived helpers ---------------------------------------------------
    @property
    def resolved_epochs(self) -> int:
        return self.epochs

    def with_epochs(self, epochs: int) -> SimulationConfig:
        clone = dataclasses.replace(self, epochs=epochs)
        return clone

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------


def _coerce(value: Any, target_type: Any, path: str) -> Any:
    """Best-effort coercion of YAML/env scalars to the annotated field type."""
    origin = get_origin(target_type)
    if origin is Literal:
        allowed = get_args(target_type)
        if value in allowed:
            return value
        # YAML 1.1 turns unquoted `off`/`on`/`yes`/`no` into booleans, so
        # `llm_mode: off` arrives here as False. Accept the obvious mapping and
        # case-insensitive spellings instead of failing the whole run.
        if isinstance(value, bool):
            candidate = "off" if value is False else "on"
            if candidate in allowed:
                return candidate
        if isinstance(value, str):
            lowered = {str(option).lower(): option for option in allowed}
            if value.strip().lower() in lowered:
                return lowered[value.strip().lower()]
        raise ConfigurationError(f"{path}: {value!r} is not one of {allowed}")
    if origin in (list, tuple):
        if isinstance(value, str):
            value = [item.strip() for item in value.split(",") if item.strip()]
        if not isinstance(value, (list, tuple)):
            raise ConfigurationError(f"{path}: expected a list, got {type(value).__name__}")
        (arg_type,) = get_args(target_type) or (str,)
        return [_coerce(item, arg_type, f"{path}[]") for item in value]
    if origin is dict:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ConfigurationError(f"{path}: cannot parse JSON dict: {exc}") from exc
        if not isinstance(value, dict):
            raise ConfigurationError(f"{path}: expected a mapping, got {type(value).__name__}")
        return value
    if target_type is bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if target_type is float:
        return float(value)
    if target_type is int:
        return int(value)
    if target_type is str:
        return str(value)
    if dataclasses.is_dataclass(target_type) and isinstance(target_type, type):
        return _build_dataclass(target_type, value, path)
    return value


def _build_dataclass(cls: type[T], payload: dict[str, Any] | None, path: str = "") -> T:
    payload = dict(payload or {})
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):  # type: ignore[arg-type]
        if f.name not in payload:
            continue
        value = payload.pop(f.name)
        if value is None:
            continue
        kwargs[f.name] = _coerce(value, hints[f.name], f"{path}{f.name}")
    if payload:
        raise ConfigurationError(f"{path or cls.__name__}: unknown configuration keys {sorted(payload)}")
    return cls(**kwargs)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


#: Top-level SimulationConfig fields that may be set without a section prefix,
#: e.g. ``INSILICO_N_PATIENTS=10000``. Unknown section-less variables (logging,
#: credentials, provider SDKs) are ignored instead of raising.
TOP_LEVEL_ENV_FIELDS: frozenset[str] = frozenset(
    {
        "protocol_path",
        "n_patients",
        "n_cohorts",
        "seed",
        "epochs",
        "llm_mode",
        "llm_sample_rate",
        "llm_trigger_grade",
        "output_dir",
        "include_baseline_epoch",
        "export_cdisc",
        "export_dashboard",
        "report_formats",
        "strict_engines",
        "max_patients",
    }
)


def _env_overrides(prefix: str = ENV_PREFIX) -> dict[str, Any]:
    """Translate ``INSILICO_ENGINE__BACKEND=spark`` into ``{"engine": {"backend": "spark"}}``.

    A section-less name is honoured only when it matches a top-level field
    (``INSILICO_N_PATIENTS``); anything else - ``INSILICO_LOG_LEVEL``, provider
    credentials - is left alone.
    """
    overrides: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        path = key[len(prefix) :]
        if "__" not in path:
            field_name = path.lower()
            if field_name in TOP_LEVEL_ENV_FIELDS:
                overrides[field_name] = value
            continue
        section, _, field_name = path.partition("__")
        overrides.setdefault(section.lower(), {})[field_name.lower()] = value
    return overrides


#: Container layout used by the published image (`conf/` lands in `/app/conf`).
CONTAINER_ASSET_ROOT = Path("/app")


def asset_roots() -> tuple[Path, ...]:
    """Directories a repository-relative asset (``conf/*.yaml``) may live under.

    The examples and profiles are written as ``conf/simulation_local.yaml``, which
    only resolves when the process happens to run from the checkout root. An
    installed wheel, a Databricks job whose working directory is the workspace
    root, and the container image (working directory ``/data``, profiles in
    ``/app/conf``) all need the paths to resolve anyway - so the candidate roots
    are the current directory, the repository root inferred from this file, and the
    container layout.
    """
    package_root = Path(__file__).resolve().parent  # .../src/insilico_trial_mas
    roots = [Path.cwd(), package_root]
    roots.extend(package_root.parents[:3])  # src/, repository root, parent of it
    roots.append(CONTAINER_ASSET_ROOT)
    seen: list[Path] = []
    for root in roots:
        if root not in seen:
            seen.append(root)
    return tuple(seen)


def resolve_asset(path: str | Path) -> Path:
    """Return the first existing candidate for ``path``, else the path unchanged.

    Callers keep raising their usual "not found" error when nothing matches, so a
    genuinely wrong path still fails loudly - it just also works from any working
    directory.
    """
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    if candidate.exists():
        return candidate
    for root in asset_roots():
        probe = root / candidate
        if probe.exists():
            return probe
    return candidate


def load_yaml(path: str | Path) -> dict[str, Any]:
    """Load a YAML mapping, raising :class:`ConfigurationError` on any problem."""
    file_path = Path(path)
    if not file_path.exists():
        raise ConfigurationError(f"configuration file not found: {file_path}")
    try:
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"invalid YAML in {file_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{file_path}: top-level YAML document must be a mapping")
    return raw


def load_config(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> SimulationConfig:
    """Load the full simulation configuration.

    Parameters
    ----------
    path:
        YAML file. Missing file raises :class:`ConfigurationError`; pass ``None``
        to use defaults (useful for tests).
    overrides:
        Nested dict applied last, e.g. ``{"engine": {"backend": "spark"}}``.
    """
    payload: dict[str, Any] = load_yaml(resolve_asset(path)) if path is not None else {}
    payload = _deep_merge(payload, _env_overrides())
    payload = _deep_merge(payload, overrides or {})
    config = _build_dataclass(SimulationConfig, payload)
    # Profiles carry a repository-relative protocol path (`conf/trial_protocol_demo.yaml`).
    # Resolving it here keeps a profile portable across the checkout, a Databricks job
    # whose working directory is not the repository root, and the container image
    # (where the profiles live in /app/conf but the process runs in /data).
    if config.protocol_path:
        config.protocol_path = str(resolve_asset(config.protocol_path))
    validate_config(config)
    return config


def config_from_dict(payload: dict[str, Any]) -> SimulationConfig:
    """Rebuild a :class:`SimulationConfig` from ``dataclasses.asdict`` output.

    Used on Spark executors, which receive the configuration as JSON inside the
    partition context instead of a pickled object.
    """
    config = _build_dataclass(SimulationConfig, payload)
    validate_config(config)
    return config


def validate_config(config: SimulationConfig) -> None:
    """Reject configurations that would waste cluster time or produce nonsense."""
    if config.n_patients < 1:
        raise ConfigurationError("n_patients must be >= 1")
    if config.n_patients > config.max_patients:
        raise ConfigurationError(
            f"n_patients={config.n_patients} exceeds the safety cap max_patients={config.max_patients}"
        )
    if config.n_cohorts < 1:
        raise ConfigurationError("n_cohorts must be >= 1")
    if config.engine.batch_size < 1:
        raise ConfigurationError("engine.batch_size must be >= 1")
    if config.engine.partition_mode == "cohort" and config.engine.cohorts_per_partition < 1:
        raise ConfigurationError("engine.cohorts_per_partition must be >= 1")
    if not 0.0 <= config.llm_sample_rate <= 1.0:
        raise ConfigurationError("llm_sample_rate must be within [0, 1]")
    if config.llm.max_concurrency < 1:
        raise ConfigurationError("llm.max_concurrency must be >= 1")
    if config.llm.requests_per_second <= 0:
        raise ConfigurationError("llm.requests_per_second must be > 0")
    if config.llm_mode != "off" and config.llm.provider == "offline" and not config.engine.single_node:
        # Offline provider is a legitimate deterministic mode; only warn through logs.
        pass
    if config.storage.backend == "delta" and not (config.storage.root_uri or config.storage.local_root):
        raise ConfigurationError("delta storage backend requires storage.root_uri or storage.local_root")
