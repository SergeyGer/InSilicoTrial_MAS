"""Environment checklist - the logic behind notebook cell 1 of the specification.

Decides whether a run should scale across worker nodes (AWS/Databricks cluster),
fall back to local multiprocessing (Databricks Community Edition, laptop) or run
sequentially (tests, tiny cohorts), and reports every constraint it inspected so
the decision is auditable rather than implicit.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any

from ..config import SimulationConfig
from ..credentials import detect_credentials
from ..logging_utils import get_logger

logger = get_logger("engine.checklist")


def _module_available(name: str) -> bool:
    try:
        import_module(name)
    except ImportError:
        return False
    return True


def java_available() -> tuple[bool, str]:
    """Check for a JVM (required by PySpark)."""
    java_home = os.environ.get("JAVA_HOME", "")
    if java_home:
        candidate = os.path.join(java_home, "bin", "java")
        if os.path.exists(candidate):
            return True, candidate
    found = shutil.which("java")
    if found:
        return True, found
    # A workspace-local portable JRE (see scripts/bootstrap.sh) is also honoured.
    for relative in (".toolchain/jre17/bin/java", ".toolchain/jdk/bin/java"):
        candidate = os.path.abspath(relative)
        if os.path.exists(candidate):
            return True, candidate
    return False, ""


def is_databricks() -> bool:
    return bool(os.environ.get("DATABRICKS_RUNTIME_VERSION")) or os.path.exists("/databricks")


def is_databricks_community() -> bool:
    """Community Edition: single-node driver, no worker pools."""
    if not is_databricks():
        return False
    if os.environ.get("INSILICO_FORCE_SINGLE_NODE", "").lower() in {"1", "true", "yes"}:
        return True
    try:
        from pyspark.sql import SparkSession

        spark = SparkSession.getActiveSession()
        if spark is not None:
            master = spark.sparkContext.master or ""
            return master.startswith("local")
    except Exception:
        return False
    return False


def cluster_shape() -> dict[str, Any]:
    """Read the Spark cluster shape when a session is active."""
    shape: dict[str, Any] = {
        "spark_available": _module_available("pyspark"),
        "spark_version": "",
        "master": "",
        "executors": 0,
        "cores_per_executor": 0,
        "driver_cores": os.cpu_count() or 1,
    }
    if not shape["spark_available"]:
        return shape
    try:
        from pyspark.sql import SparkSession

        spark = SparkSession.getActiveSession()
        if spark is None:
            return shape
        sc = spark.sparkContext
        shape["spark_version"] = spark.version
        shape["master"] = sc.master
        status = sc.statusTracker()
        executor_ids = [e for e in status.getExecutorInfos() if e.id() != "driver"] if hasattr(status, "getExecutorInfos") else []
        shape["executors"] = len(executor_ids)
        if executor_ids:
            shape["cores_per_executor"] = executor_ids[0].totalCores()
    except Exception as exc:
        logger.warning(f"could not inspect the Spark cluster: {exc}")
    return shape


@dataclass(slots=True)
class EnvironmentReport:
    """Everything the platform detected, plus the recommended engine."""

    python_version: str
    platform: str
    cpu_count: int
    memory_gb: float
    pyspark_available: bool
    spark_version: str
    java_available: bool
    java_path: str
    databricks: bool
    databricks_community: bool
    master: str
    executors: int
    recommended_backend: str
    recommended_workers: int
    notes: list[str] = field(default_factory=list)
    packages: dict[str, bool] = field(default_factory=dict)
    credentials: dict[str, bool] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "python_version": self.python_version,
            "platform": self.platform,
            "cpu_count": self.cpu_count,
            "memory_gb": self.memory_gb,
            "pyspark_available": self.pyspark_available,
            "spark_version": self.spark_version,
            "java_available": self.java_available,
            "java_path": self.java_path,
            "databricks": self.databricks,
            "databricks_community": self.databricks_community,
            "master": self.master,
            "executors": self.executors,
            "recommended_backend": self.recommended_backend,
            "recommended_workers": self.recommended_workers,
            "packages": self.packages,
            "credentials": self.credentials,
            "notes": self.notes,
        }

    def render(self) -> str:
        """Human-readable checklist (used by ``insilico-trial env-check``)."""
        credential_summary = (
            ", ".join(f"{name}={'yes' if present else 'no'}" for name, present in sorted(self.credentials.items()))
            or "none detected"
        )
        lines = [
            "InSilicoTrial MAS - environment checklist",
            f"  python            : {self.python_version} ({self.platform})",
            f"  cpu / memory      : {self.cpu_count} cores / {self.memory_gb:.1f} GB",
            f"  java              : {'yes' if self.java_available else 'no'} {self.java_path}",
            f"  pyspark           : {'yes ' + self.spark_version if self.pyspark_available else 'no'}",
            f"  databricks        : {self.databricks} (community edition: {self.databricks_community})",
            f"  spark master      : {self.master or 'not started'} ({self.executors} executors)",
            f"  packages          : {', '.join(f'{k}={int(v)}' for k, v in sorted(self.packages.items()))}",
            f"  credentials       : {credential_summary}",
            "                      (presence only - values are never read or logged)",
            f"  => engine         : {self.recommended_backend} (workers={self.recommended_workers})",
        ]
        lines.extend(f"  note              : {note}" for note in self.notes)
        return "\n".join(lines)


def _memory_gb() -> float:
    try:
        pages = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        return pages / (1024**3)
    except (ValueError, OSError, AttributeError):  # pragma: no cover - platform dependent
        return 0.0


def inspect_environment(config: SimulationConfig | None = None) -> EnvironmentReport:
    """Inspect the runtime and recommend an execution backend."""
    config = config or SimulationConfig()
    cpu_count = os.cpu_count() or 1
    shape = cluster_shape()
    java_ok, java_path = java_available()
    community = is_databricks_community()
    notes: list[str] = []

    requested = config.engine.backend
    if requested == "auto":
        if shape["spark_available"] and java_ok and (is_databricks() or config.engine.single_node or shape["master"]):
            backend = "spark"
        elif cpu_count > 1:
            backend = "local"
        else:
            backend = "sequential"
    else:
        backend = requested

    if backend == "spark" and not (shape["spark_available"] and java_ok):
        notes.append("spark requested but pyspark/JVM is missing: falling back to local execution")
        backend = "local" if cpu_count > 1 else "sequential"
    if community:
        notes.append(
            "Databricks Community Edition detected (single-node driver): using the driver's local "
            "multiprocessing path with vectorised Arrow batches"
        )
    if not java_ok:
        notes.append("no JVM found - Spark is unavailable; set JAVA_HOME or run the pip-installed portable JRE")
    if backend == "local" and config.n_patients > 200_000:
        notes.append("large cohort on the local engine: expect minutes of runtime; prefer a Databricks cluster")

    credentials = detect_credentials()
    if not credentials.get("aws-bedrock") and config.llm.provider == "bedrock":
        notes.append(
            "llm.provider=bedrock but no AWS credentials were detected: configure the standard boto3 chain "
            "(AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, AWS_PROFILE, or the instance role) or switch to "
            "llm.provider=offline for a deterministic credential-free run"
        )
    if not credentials.get("openai") and config.llm.provider == "openai":
        notes.append("llm.provider=openai but OPENAI_API_KEY is not set")
    if not credentials.get("aws-bedrock") and not credentials.get("openai"):
        notes.append(
            "no LLM credentials detected: the deterministic offline persona provider is used "
            "(identical JSON contract, no network, fully reproducible)"
        )

    workers = config.engine.max_workers or (max(1, cpu_count) if backend == "local" else 1)
    packages = {
        name: _module_available(name)
        for name in ("numpy", "pandas", "pyarrow", "pydantic", "yaml", "jinja2", "pyspark", "delta", "mlflow", "langchain_core")
    }
    return EnvironmentReport(
        python_version=sys.version.split()[0],
        platform=f"{platform.system()} {platform.release()}",
        cpu_count=cpu_count,
        memory_gb=_memory_gb(),
        pyspark_available=bool(shape["spark_available"]),
        spark_version=str(shape["spark_version"]),
        java_available=java_ok,
        java_path=java_path,
        databricks=is_databricks(),
        databricks_community=community,
        master=str(shape["master"]),
        executors=int(shape["executors"]),
        recommended_backend=backend,
        recommended_workers=workers,
        notes=notes,
        packages=packages,
        credentials=detect_credentials(),
    )
