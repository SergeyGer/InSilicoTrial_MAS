"""Live Studio server: launch and watch a simulation from a browser.

Why this module exists
----------------------
``cli.py`` runs a simulation and exits, and the static dashboard
(:mod:`insilico_trial_mas.ui.dashboard`) renders a run that has already finished.
Neither lets a clinical team *try* a cohort size, an execution engine or an LLM
policy and watch the run as it happens. The Studio is that missing loop: one
process, ``http.server``, standard library only, no build step - so it starts on
a laptop, inside a Databricks notebook, or in an air-gapped environment where a
Node toolchain and a CDN are simply not available.

The Studio runs the real pipeline
(:class:`insilico_trial_mas.pipeline.TrialSimulationPipeline`); it is a thin
window onto it, never a second simulation implementation. Work is executed on
daemon threads, tracked by :class:`StudioState`, and exposed through a small JSON
API plus one self-contained HTML page:

===============================  ==================================================
``GET  /``                       the Studio page (no external assets)
``GET  /api/config``             configuration path, protocol catalog, defaults
``POST /api/runs``               start a run (201) or reject bad input (400)
``GET  /api/runs``               every run, newest first
``GET  /api/runs/{id}``          one run
``GET  /api/runs/{id}/dashboard``  the generated static dashboard (202 before ready)
``GET  /api/runs/{id}/artifacts/{name}``  report files (md/html/json only)
===============================  ==================================================

Studio runs are demonstrations, so they never touch shared infrastructure: the
CDISC export is disabled and MLflow tracking is off unless the caller explicitly
passes ``"tracking": true``. The configured storage backend is left untouched.
"""

from __future__ import annotations

import hashlib
import html
import inspect
import json
import threading
import urllib.parse
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import SimulationConfig, load_config
from ..logging_utils import get_logger

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    from ..pipeline import RunResult, TrialSimulationPipeline

logger = get_logger("ui.studio")

#: Accepted values for the request fields that drive engine / LLM / storage selection.
ENGINES: tuple[str, ...] = ("auto", "sequential", "local", "spark")
LLM_MODES: tuple[str, ...] = ("off", "sample", "triggered", "all")
STORAGE_BACKENDS: tuple[str, ...] = ("local", "delta", "memory")

#: Input guard rails. They protect the demo box, not the science: a Studio user
#: should not be able to start a 2M-patient Spark job by typing a number.
MIN_PATIENTS, MAX_PATIENTS = 10, 20_000
MIN_COHORTS, MAX_COHORTS = 1, 256
MIN_EPOCHS, MAX_EPOCHS = 1, 32

#: Artefact extensions the download route is allowed to serve (exactly the report
#: formats the report builder writes - never scripts or binaries).
ARTIFACT_SUFFIXES = (".md", ".html", ".json")
_CONTENT_TYPES = {
    ".md": "text/markdown; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}

MAX_BODY_BYTES = 64 * 1024
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
EXIT_OK = 0
EXIT_ERROR = 1


def _utc_now() -> str:
    """Timestamp format shared with the pipeline's run manifests."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _clamp_float(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _as_int(value: Any, default: int, name: str) -> int:
    if value in (None, ""):
        return int(default)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _as_float(value: Any, default: float, name: str) -> float:
    if value in (None, ""):
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc


@dataclass
class StudioRun:
    """One studio submission and everything known about it.

    The object is mutated in place by the worker thread; readers take a snapshot
    through :meth:`to_dict` so a half-written run never reaches the JSON encoder.
    """

    run_id: str
    status: str = "queued"  # queued | running | done | error
    phase: str = "queued"
    progress: float = 0.0
    started_at: str = ""
    finished_at: str = ""
    summary: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    dashboard_path: str = ""
    output_dir: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable snapshot handed to the browser."""
        return {
            "run_id": self.run_id,
            "status": self.status,
            "phase": self.phase,
            "progress": float(self.progress),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": self.summary,
            "error": self.error,
            "dashboard_path": self.dashboard_path,
            "output_dir": self.output_dir,
            "params": self.params,
        }

    @property
    def report_dir(self) -> Path:
        """Directory holding the generated report artefacts (may not exist yet)."""
        return Path(self.output_dir) / "report" if self.output_dir else Path("report")


class _ProgressCallback:
    """Bridge between the pipeline's ``progress`` hook and a :class:`StudioRun`.

    It is intentionally forgiving: a progress callback must never be able to fail
    a simulation, and the pipeline itself already guards its calls.
    """

    def __init__(self, state: StudioState, run_id: str) -> None:
        self._state = state
        self._run_id = run_id
        self.called = False

    def __call__(self, phase: str, fraction: float) -> None:
        self.called = True
        try:
            value = _clamp_float(float(fraction), 0.0, 1.0)
        except (TypeError, ValueError):  # pragma: no cover - the pipeline clamps already
            value = 0.0
        self._state.update_run(self._run_id, phase=str(phase), progress=value, status="running")


def _run_pipeline(
    pipeline: TrialSimulationPipeline,
    run_id: str,
    callback: _ProgressCallback,
) -> RunResult:
    """Call ``pipeline.run`` with only the keywords this pipeline revision accepts.

    ``run_id`` and ``progress`` are optional keywords in the pipeline API, so the
    studio keeps working while that API evolves (an older pipeline has neither).
    A ``TypeError`` raised before the callback ever fired is treated as "the
    keyword is not supported" and retried without it; anything else propagates so
    a genuine failure is never silently executed twice.
    """
    try:
        parameters = inspect.signature(pipeline.run).parameters
        accepts_progress = "progress" in parameters
        accepts_run_id = "run_id" in parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic/builtin callables
        accepts_progress, accepts_run_id = True, True

    keywords: dict[str, Any] = {"run_id": run_id} if accepts_run_id else {}
    if not accepts_progress:
        return pipeline.run(**keywords)
    try:
        return pipeline.run(progress=callback, **keywords)
    except TypeError:
        if callback.called:
            raise
        logger.debug("pipeline.run() rejected the progress keyword; retrying without it")
        return pipeline.run(**keywords)


class StudioState:
    """Everything the HTTP layer needs: the base config, the runs and a lock."""

    def __init__(
        self,
        config: SimulationConfig,
        config_path: str = "",
        protocol_paths: Sequence[str | Path] | None = None,
    ) -> None:
        self.config = config
        self.config_path = str(config_path or "")
        #: Runs by id. Insertion order is the submission order, which is what
        #: ``GET /api/runs`` reverses into "newest first".
        self.runs: dict[str, StudioRun] = {}
        self.lock = threading.Lock()
        #: Studio artefacts live below the configured output directory so a demo
        #: never mixes with the outputs of ``insilico-trial simulate``.
        self.output_root = str(Path(config.output_dir) / "studio")
        self.protocol_files = _discover_protocols(config, protocol_paths)
        self._counter = 0

    # -- construction ------------------------------------------------------
    @classmethod
    def from_config(
        cls,
        config: SimulationConfig,
        *,
        config_path: str = "",
        protocol_paths: Sequence[str | Path] | None = None,
    ) -> StudioState:
        """Build a state object from an already loaded configuration."""
        return cls(config, config_path=config_path, protocol_paths=protocol_paths)

    # -- catalog -----------------------------------------------------------
    def protocol_catalog(self) -> list[dict[str, Any]]:
        """Descriptors for every protocol the studio can launch (see ``GET /api/config``)."""
        catalog: list[dict[str, Any]] = []
        for path in self.protocol_files:
            descriptor = _describe_protocol(path)
            if descriptor is not None:
                catalog.append(descriptor)
        return catalog

    def primary_protocol(self) -> dict[str, Any] | None:
        """Descriptor of the configuration's own protocol, falling back to the first one."""
        configured = str(Path(self.config.protocol_path).resolve()) if self.config.protocol_path else ""
        catalog = self.protocol_catalog()
        for descriptor in catalog:
            if descriptor["path"] == configured:
                return descriptor
        return catalog[0] if catalog else None

    def default_params(self) -> dict[str, Any]:
        """Form defaults for the browser, taken from the profile and the protocol."""
        protocol = self.primary_protocol()
        return {
            "protocol": str(Path(self.config.protocol_path).resolve()) if self.config.protocol_path else "",
            "n_patients": int(self.config.n_patients),
            "n_cohorts": int(self.config.n_cohorts),
            "epochs": self.default_epochs(),
            "engine": str(self.config.engine.backend),
            "llm_mode": str(self.config.llm_mode),
            "seed": int(self.config.seed),
            "llm_sample_rate": float(self.config.llm_sample_rate),
            "output_dir": self.output_root,
            "protocol_id": protocol["protocol_id"] if protocol else "",
        }

    def default_epochs(self) -> int:
        """Protocol epochs unless the configuration pins them."""
        if self.config.epochs:
            return int(self.config.epochs)
        protocol = self.primary_protocol()
        return int(protocol["epochs"]) if protocol else MIN_EPOCHS

    # -- validation --------------------------------------------------------
    def resolve_protocol(self, requested: str) -> str:
        """Return the absolute path of a launchable protocol or raise :class:`ValueError`."""
        if not requested:
            raise ValueError("a protocol path is required")
        try:
            resolved = Path(requested).expanduser().resolve()
        except OSError as exc:  # pragma: no cover - unreadable path
            raise ValueError(f"unknown protocol: {requested}") from exc
        known = {path.resolve() for path in self.protocol_files}
        if resolved in known and resolved.is_file():
            return str(resolved)
        if resolved.is_file() and _looks_like_protocol(resolved) and _describe_protocol(resolved) is not None:
            return str(resolved)
        raise ValueError(f"unknown protocol: {requested}")

    def normalize_params(self, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        """Validate and clamp a run request.

        Raises :class:`ValueError` with a user-facing message; the HTTP layer turns
        that into a ``400`` and ``submit`` refuses to start a doomed thread.
        """
        data: dict[str, Any] = dict(payload or {})
        engine = str(data.get("engine") or self.config.engine.backend).strip().lower()
        if engine not in ENGINES:
            raise ValueError(f"engine must be one of {', '.join(ENGINES)} (got {engine!r})")
        llm_mode = str(data.get("llm_mode") or self.config.llm_mode).strip().lower()
        if llm_mode not in LLM_MODES:
            raise ValueError(f"llm_mode must be one of {', '.join(LLM_MODES)} (got {llm_mode!r})")

        storage_backend = ""
        storage_requested = data.get("storage_backend", data.get("storage"))
        if storage_requested not in (None, ""):
            storage_backend = str(storage_requested).strip().lower()
            if storage_backend not in STORAGE_BACKENDS:
                raise ValueError(
                    f"storage_backend must be one of {', '.join(STORAGE_BACKENDS)} (got {storage_backend!r})"
                )

        protocol = self.resolve_protocol(str(data.get("protocol") or self.config.protocol_path or ""))
        n_patients = _clamp(
            _as_int(data.get("n_patients"), self.config.n_patients, "n_patients"), MIN_PATIENTS, MAX_PATIENTS
        )
        n_cohorts = _clamp(
            _as_int(data.get("n_cohorts"), self.config.n_cohorts, "n_cohorts"),
            MIN_COHORTS,
            min(MAX_COHORTS, n_patients),
        )
        epochs = _clamp(_as_int(data.get("epochs"), self.default_epochs(), "epochs"), MIN_EPOCHS, MAX_EPOCHS)
        seed = _as_int(data.get("seed"), self.config.seed, "seed")
        sample_rate = _clamp_float(
            _as_float(data.get("llm_sample_rate"), self.config.llm_sample_rate, "llm_sample_rate"), 0.0, 1.0
        )
        output_dir = str(data.get("output_dir") or self.output_root)

        return {
            "protocol": protocol,
            "n_patients": n_patients,
            "n_cohorts": n_cohorts,
            "epochs": epochs,
            "engine": engine,
            "llm_mode": llm_mode,
            "seed": seed,
            "llm_sample_rate": sample_rate,
            "output_dir": output_dir,
            "storage_backend": storage_backend,
            "tracking": bool(data.get("tracking", False)),
        }

    # -- run registry ------------------------------------------------------
    def submit(self, params: Mapping[str, Any]) -> StudioRun:
        """Validate ``params`` and start the simulation on a daemon thread."""
        normalized = self.normalize_params(params)
        run_id = self._new_run_id(normalized)
        run = StudioRun(
            run_id=run_id,
            status="queued",
            phase="queued",
            progress=0.0,
            started_at=_utc_now(),
            summary={},
            params=normalized,
            # Predictable, because the studio hands its own run id to the pipeline.
            output_dir=str(Path(normalized["output_dir"]) / run_id),
        )
        with self.lock:
            self.runs[run_id] = run
        thread = threading.Thread(target=self._execute, args=(run_id,), name=f"studio-{run_id}", daemon=True)
        thread.start()
        logger.info(
            f"studio run {run_id} queued ({normalized['n_patients']} patients, engine={normalized['engine']})"
        )
        return run

    def get(self, run_id: str) -> StudioRun | None:
        """Return the live run record (or ``None``)."""
        with self.lock:
            return self.runs.get(run_id)

    def list_runs(self) -> list[StudioRun]:
        """Every run, newest first."""
        with self.lock:
            return list(reversed(list(self.runs.values())))

    def update_run(self, run_id: str, **fields: Any) -> None:
        """Apply ``fields`` to a run under the state lock."""
        with self.lock:
            run = self.runs.get(run_id)
            if run is None:  # pragma: no cover - defensive
                return
            for name, value in fields.items():
                setattr(run, name, value)

    # -- worker ------------------------------------------------------------
    def _new_run_id(self, params: Mapping[str, Any]) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        with self.lock:
            self._counter += 1
            counter = self._counter
        digest = hashlib.sha256(
            "|".join(
                [stamp, str(counter), str(params.get("protocol", "")), str(params.get("seed", ""))]
            ).encode("utf-8")
        ).hexdigest()
        return f"RUN-{stamp}-{digest[:6].upper()}"

    def _overrides(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Translate validated request parameters into ``load_config`` overrides."""
        overrides: dict[str, Any] = {
            "protocol_path": str(params["protocol"]),
            "n_patients": int(params["n_patients"]),
            "n_cohorts": int(params["n_cohorts"]),
            "epochs": int(params["epochs"]),
            "seed": int(params["seed"]),
            "llm_mode": str(params["llm_mode"]),
            "llm_sample_rate": float(params["llm_sample_rate"]),
            "output_dir": str(params["output_dir"]),
            "engine": {"backend": str(params["engine"])},
            # A demo must not touch shared infrastructure: no CDISC export, and no
            # MLflow run unless the caller explicitly asked for tracking.
            "export_cdisc": False,
            "tracking": {"enabled": bool(params.get("tracking", False))},
        }
        storage_backend = params.get("storage_backend")
        if storage_backend:
            overrides["storage"] = {"backend": str(storage_backend)}
        return overrides

    def _execute(self, run_id: str) -> None:
        """Worker body: build the pipeline, run it, record the outcome."""
        # Imported lazily so that serving the page and listing protocols never
        # depends on the (heavier) pipeline import graph.
        from ..pipeline import TrialSimulationPipeline, load_protocol

        run = self.get(run_id)
        if run is None:  # pragma: no cover - defensive
            return
        params = dict(run.params)
        self.update_run(run_id, status="running", phase="configuring", progress=0.01)
        try:
            overrides = self._overrides(params)
            config = load_config(self.config_path or None, overrides)
            pipeline = TrialSimulationPipeline(config, load_protocol(str(overrides["protocol_path"])))
            callback = _ProgressCallback(self, run_id)
            result = _run_pipeline(pipeline, run_id, callback)

            output_dir = str(getattr(result, "output_dir", "") or params["output_dir"])
            dashboard_path = str(getattr(result, "dashboard_path", "") or "")
            if not dashboard_path:
                dashboard_path = str(Path(output_dir) / "report" / "dashboard.html")
            self.update_run(run_id, phase="dashboard", progress=max(float(run.progress), 0.98))
            dashboard_path = self._ensure_dashboard(output_dir, dashboard_path)
            self.update_run(
                run_id,
                status="done",
                phase="done",
                progress=1.0,
                summary=result.summary(),
                output_dir=output_dir,
                dashboard_path=dashboard_path,
                finished_at=_utc_now(),
                error="",
            )
            logger.info(f"studio run {run_id} finished (artefacts: {output_dir})")
        except Exception as exc:
            logger.error(f"studio run {run_id} failed: {type(exc).__name__}: {exc}", exc_info=True)
            self.update_run(
                run_id,
                status="error",
                phase="failed",
                error=f"{type(exc).__name__}: {exc}",
                finished_at=_utc_now(),
            )

    def _ensure_dashboard(self, output_dir: str, dashboard_path: str) -> str:
        """Return the dashboard file, asking the static generator to build it if needed."""
        candidate = Path(dashboard_path) if dashboard_path else Path(output_dir) / "report" / "dashboard.html"
        if candidate.is_file():
            return str(candidate)
        if not output_dir:
            return str(candidate)
        try:
            from .dashboard import write_dashboard
        except ImportError as exc:  # the static generator is optional for a live demo
            logger.debug(f"static dashboard module unavailable ({exc}); serving the report instead")
            return str(candidate)
        try:
            produced = write_dashboard(output_dir)
        except Exception as exc:
            logger.warning(f"dashboard generation failed for {output_dir}: {type(exc).__name__}: {exc}")
            return str(candidate)
        if isinstance(produced, (str, Path)):
            generated = Path(produced)
            return str(generated if generated.is_file() else candidate)
        return str(candidate)


# ---------------------------------------------------------------------------
# Protocol discovery helpers
# ---------------------------------------------------------------------------


def _looks_like_protocol(path: Path) -> bool:
    """Cheap screen so a config profile is never parsed as a trial protocol."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return "protocol_id:" in text and "arms:" in text


def _discover_protocols(config: SimulationConfig, extra: Sequence[str | Path] | None = None) -> list[Path]:
    """Protocol YAML files the studio may launch, de-duplicated and resolved."""
    candidates: list[Path] = []
    configured = Path(config.protocol_path) if config.protocol_path else None
    if configured is not None:
        candidates.append(configured)
    if extra:
        candidates.extend(Path(item) for item in extra)
    if configured is not None:
        directory = configured.parent if str(configured.parent) else Path(".")
        for pattern in ("*.yaml", "*.yml"):
            candidates.extend(sorted(directory.glob(pattern)))
    found: dict[str, Path] = {}
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:  # pragma: no cover - unreadable path
            continue
        key = str(resolved)
        if key in found or not resolved.is_file() or not _looks_like_protocol(resolved):
            continue
        found[key] = resolved
    return list(found.values())


def _describe_protocol(path: Path) -> dict[str, Any] | None:
    """Summarise a protocol for ``GET /api/config``; ``None`` when it cannot be loaded."""
    try:
        from ..pipeline import load_protocol
    except ImportError as exc:  # pragma: no cover - only while the pipeline is unavailable
        logger.warning(f"protocol catalog unavailable: {exc}")
        return None
    try:
        protocol = load_protocol(path)
    except Exception as exc:
        logger.debug(f"{path} is not a usable protocol: {type(exc).__name__}: {exc}")
        return None
    return {
        "path": str(path),
        "protocol_id": protocol.protocol_id,
        "title": protocol.title or protocol.protocol_id,
        "phase": protocol.phase,
        "epochs": int(protocol.epochs),
        "arms": [arm.arm_id for arm in protocol.arms],
    }


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


class StudioHTTPServer(ThreadingHTTPServer):
    """Threading server that carries the shared :class:`StudioState`."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], handler: type[BaseHTTPRequestHandler]) -> None:
        super().__init__(server_address, handler)
        self.state: StudioState | None = None


class StudioRequestHandler(BaseHTTPRequestHandler):
    """The whole Studio HTTP surface: one page, a JSON API and report downloads."""

    server_version = "InSilicoTrialStudio/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------
    @property
    def studio_state(self) -> StudioState:
        state = getattr(self.server, "state", None)
        if not isinstance(state, StudioState):  # pragma: no cover - defensive
            raise RuntimeError("studio handler is not attached to a StudioState")
        return state

    def log_message(self, format: str, *args: Any) -> None:
        """Route the access log through the platform logger instead of stderr writes."""
        logger.debug(f"{self.address_string()} {format % args}")

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:
        self._method_not_allowed()

    # -- routing -----------------------------------------------------------
    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        segments = [urllib.parse.unquote(segment) for segment in parsed.path.split("/") if segment]
        if any(segment in {".", ".."} or "\x00" in segment for segment in segments) or "\x00" in parsed.path:
            # Path traversal is rejected before routing so both the encoded
            # (``..%2F``) and the plain form receive the same answer.
            self._send_json(400, {"error": "invalid path"})
            return
        route = tuple(segments)
        try:
            if method == "POST":
                if route == ("api", "runs"):
                    self._post_run()
                elif self._get_route(route) is not None:
                    self._method_not_allowed()
                else:
                    self._not_found(parsed.path)
                return
            handler = self._get_route(route)
            if handler is None:
                self._not_found(parsed.path)
            else:
                handler()
        except Exception as exc:
            logger.error(f"studio request failed: {type(exc).__name__}: {exc}", exc_info=True)
            self._send_json(500, {"error": f"internal error: {type(exc).__name__}: {exc}"})

    def _get_route(self, route: tuple[str, ...]) -> Callable[[], None] | None:
        """Return the GET handler for a route, or ``None`` when it is unknown."""
        if not route:
            return self._get_page
        if route == ("api", "config"):
            return self._get_config
        if route == ("api", "runs"):
            return self._get_runs
        if len(route) == 3 and route[:2] == ("api", "runs"):
            run_id = route[2]
            return lambda: self._get_run(run_id)
        if len(route) == 4 and route[:2] == ("api", "runs") and route[3] == "dashboard":
            run_id = route[2]
            return lambda: self._get_dashboard(run_id)
        if len(route) == 5 and route[:2] == ("api", "runs") and route[3] == "artifacts":
            run_id, name = route[2], route[4]
            return lambda: self._get_artifact(run_id, name)
        return None

    # -- GET handlers ------------------------------------------------------
    def _get_page(self) -> None:
        self._send_html(200, STUDIO_HTML)

    def _get_config(self) -> None:
        state = self.studio_state
        self._send_json(
            200,
            {
                "config_path": state.config_path,
                "protocols": state.protocol_catalog(),
                "defaults": state.default_params(),
                "options": {"engines": list(ENGINES), "llm_modes": list(LLM_MODES)},
            },
        )

    def _get_runs(self) -> None:
        self._send_json(200, {"runs": [run.to_dict() for run in self.studio_state.list_runs()]})

    def _get_run(self, run_id: str) -> None:
        run = self.studio_state.get(run_id)
        if run is None:
            self._send_json(404, {"error": f"unknown run: {run_id}"})
            return
        self._send_json(200, run.to_dict())

    def _get_dashboard(self, run_id: str) -> None:
        run = self.studio_state.get(run_id)
        if run is None:
            self._send_json(404, {"error": f"unknown run: {run_id}"})
            return
        if run.output_dir:
            report_dir = run.report_dir
            for candidate in (report_dir / "dashboard.html", report_dir / "trial_report.html"):
                if candidate.is_file():
                    self._send_bytes(200, candidate.read_bytes(), "text/html; charset=utf-8")
                    return
        # 202: the run exists but there is nothing to render yet.
        self._send_html(202, _not_ready_page(run))

    def _get_artifact(self, run_id: str, name: str) -> None:
        run = self.studio_state.get(run_id)
        if run is None:
            self._send_json(404, {"error": f"unknown run: {run_id}"})
            return
        if not name or "/" in name or "\\" in name or ".." in name:
            self._send_json(400, {"error": f"invalid artifact name: {name}"})
            return
        suffix = Path(name).suffix.lower()
        if suffix not in ARTIFACT_SUFFIXES:
            self._send_json(400, {"error": f"artifact type not allowed: {suffix or name}"})
            return
        if not run.output_dir:
            self._send_json(404, {"error": f"run {run_id} has no artefacts yet"})
            return
        report_dir = run.report_dir.resolve()
        candidate = (report_dir / name).resolve()
        if report_dir != candidate.parent or not candidate.is_file():
            self._send_json(404, {"error": f"artifact not found: {name}"})
            return
        self._send_bytes(200, candidate.read_bytes(), _CONTENT_TYPES[suffix])

    # -- POST handlers -----------------------------------------------------
    def _post_run(self) -> None:
        try:
            payload = self._read_json_body()
            params = self.studio_state.normalize_params(payload)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        run = self.studio_state.submit(params)
        self._send_json(201, run.to_dict())

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ValueError("invalid Content-Length header") from exc
        if length <= 0:
            raise ValueError("a JSON request body is required")
        if length > MAX_BODY_BYTES:
            raise ValueError("request body is too large")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    # -- responses ---------------------------------------------------------
    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _send_json(
        self,
        status: int,
        payload: Mapping[str, Any],
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8", extra_headers=extra_headers)

    def _send_html(self, status: int, page: str) -> None:
        self._send_bytes(status, page.encode("utf-8"), "text/html; charset=utf-8")

    def _not_found(self, path: str) -> None:
        self._send_json(404, {"error": f"unknown route: {path}"})

    def _method_not_allowed(self) -> None:
        self._send_json(
            405,
            {"error": f"method {self.command} is not allowed for {self.path}"},
            extra_headers={"Allow": "GET, POST"},
        )


def create_server(
    state: StudioState,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> ThreadingHTTPServer:
    """Bind (but do not serve) the Studio HTTP server.

    The caller owns the serve loop. That is what lets the tests bind ``port=0``
    for an ephemeral port and run ``serve_forever`` on their own thread.
    """
    server = StudioHTTPServer((host, int(port)), StudioRequestHandler)
    server.state = state
    return server


def run_studio(
    config_path: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> int:
    """Serve the Studio until interrupted and return a process exit code."""
    try:
        config = load_config(config_path)
    except Exception as exc:
        logger.error(f"cannot start the studio: {type(exc).__name__}: {exc}")
        return EXIT_ERROR

    state = StudioState.from_config(config, config_path=str(config_path))
    server = create_server(state, host=host, port=port)
    url = _server_url(server)
    logger.info(f"studio listening on {url} (config: {config_path})")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:  # pragma: no cover - interactive use
        logger.info("studio interrupted; shutting down")
    finally:
        server.server_close()
    return EXIT_OK


def _server_url(server: ThreadingHTTPServer) -> str:
    """Human-usable URL for a bound server (``0.0.0.0`` becomes the loopback address)."""
    address = server.server_address
    if isinstance(address, tuple):
        host, port = str(address[0]), int(address[1])
    else:  # pragma: no cover - unix sockets are not used by the studio
        host, port = DEFAULT_HOST, 0
    display = DEFAULT_HOST if host in {"", "0.0.0.0", "::"} else host
    return f"http://{display}:{port}/"


def _not_ready_page(run: StudioRun) -> str:
    """Small styled 202 page shown while a run has no dashboard yet."""
    progress = round(_clamp_float(float(run.progress or 0.0), 0.0, 1.0) * 100)
    return _NOT_READY_PAGE.replace("__RUN_ID__", html.escape(run.run_id)).replace(
        "__STATUS__", html.escape(run.status)
    ).replace("__PHASE__", html.escape(run.phase)).replace("__PROGRESS__", str(progress))


# ---------------------------------------------------------------------------
# Studio page (self-contained: no CDN, no external assets, no build step)
# ---------------------------------------------------------------------------

_NOT_READY_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="3">
<title>Dashboard not ready - InSilicoTrial MAS Studio</title>
<style>
  :root { color-scheme: light dark; --bg:#f8fafc; --panel:#ffffff; --ink:#0f172a; --muted:#64748b;
          --line:#e2e8f0; --accent:#1d4ed8; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0b1220; --panel:#111a2b; --ink:#e2e8f0; --muted:#94a3b8; --line:#1e293b; --accent:#60a5fa; }
  }
  body { margin:0; padding:32px 20px; background:var(--bg); color:var(--ink);
         font:15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
  main { max-width:640px; margin:0 auto; background:var(--panel); border:1px solid var(--line);
         border-radius:12px; padding:24px 28px; }
  h1 { margin:0 0 8px; font-size:18px; letter-spacing:.01em; }
  p { margin:8px 0; color:var(--muted); }
  code { color:var(--ink); background:color-mix(in srgb, var(--accent) 8%, transparent);
         border:1px solid var(--line); border-radius:6px; padding:1px 6px; }
  strong { color:var(--ink); }
</style>
</head>
<body>
<main>
  <h1>Dashboard not ready yet</h1>
  <p>Run <code>__RUN_ID__</code> is <strong>__STATUS__</strong> (phase: __PHASE__, __PROGRESS__% complete).</p>
  <p>The dashboard appears here as soon as the pipeline has written its report. This page refreshes every 3 seconds.</p>
</main>
</body>
</html>
"""

STUDIO_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>InSilicoTrial MAS - Studio</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #f7f8fa;
    --panel: #ffffff;
    --panel-alt: #fbfcfd;
    --ink: #0f172a;
    --ink-soft: #334155;
    --muted: #64748b;
    --line: #e3e8ef;
    --line-strong: #cbd5e1;
    --accent: #1d4ed8;
    --accent-soft: #eef2ff;
    --ok: #047857;
    --ok-soft: #ecfdf5;
    --warn: #b45309;
    --warn-soft: #fffbeb;
    --bad: #b91c1c;
    --bad-soft: #fef2f2;
    --radius: 10px;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0b1220;
      --panel: #111a2b;
      --panel-alt: #0f1828;
      --ink: #e5e9f0;
      --ink-soft: #cbd5e1;
      --muted: #93a3b8;
      --line: #1e293b;
      --line-strong: #334155;
      --accent: #7aa2f7;
      --accent-soft: #16233b;
      --ok: #34d399;
      --ok-soft: #10241d;
      --warn: #fbbf24;
      --warn-soft: #2a2110;
      --bad: #f87171;
      --bad-soft: #2a1414;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  }
  .wrap { max-width: 1200px; margin: 0 auto; padding: 28px 20px 64px; }
  header.site { display: flex; flex-wrap: wrap; align-items: baseline; gap: 12px; margin-bottom: 18px; }
  header.site h1 { margin: 0; font-size: 21px; letter-spacing: .01em; font-weight: 650; }
  header.site p { margin: 0; color: var(--muted); font-size: 14px; }
  .disclaimer {
    border: 1px solid var(--line-strong);
    border-left: 3px solid var(--warn);
    background: var(--warn-soft);
    border-radius: var(--radius);
    padding: 12px 16px;
    margin-bottom: 20px;
    font-size: 13.5px;
    color: var(--ink-soft);
  }
  .disclaimer strong { color: var(--ink); }
  .grid { display: grid; grid-template-columns: minmax(320px, 380px) 1fr; gap: 20px; align-items: start; }
  @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
  section.panel {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 18px 18px 20px;
    margin-bottom: 20px;
  }
  section.panel > h2 {
    margin: 0 0 4px;
    font-size: 15px;
    font-weight: 650;
    letter-spacing: .02em;
    text-transform: uppercase;
    color: var(--ink-soft);
  }
  section.panel > p.hint { margin: 0 0 14px; color: var(--muted); font-size: 13px; }
  label { display: block; font-size: 12.5px; color: var(--muted); margin: 10px 0 4px; }
  input, select {
    width: 100%;
    padding: 7px 9px;
    font: inherit;
    font-size: 14px;
    color: var(--ink);
    background: var(--panel-alt);
    border: 1px solid var(--line-strong);
    border-radius: 7px;
  }
  input:focus, select:focus, button:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  .row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  button {
    font: inherit;
    font-size: 13.5px;
    padding: 7px 12px;
    color: var(--ink);
    background: var(--panel-alt);
    border: 1px solid var(--line-strong);
    border-radius: 7px;
    cursor: pointer;
  }
  button:hover { border-color: var(--accent); color: var(--accent); }
  button.primary {
    width: 100%;
    margin-top: 16px;
    padding: 10px 14px;
    font-weight: 600;
    color: #ffffff;
    background: var(--accent);
    border-color: var(--accent);
  }
  button.primary:hover { filter: brightness(1.08); color: #ffffff; }
  button.primary:disabled { opacity: .55; cursor: not-allowed; filter: none; }
  .presets { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
  .presets span { align-self: center; font-size: 12.5px; color: var(--muted); }
  .muted { color: var(--muted); }
  .error { color: var(--bad); font-size: 13px; margin: 8px 0 0; }
  .ok { color: var(--ok); font-size: 13px; margin: 10px 0 0; }
  .run { border: 1px solid var(--line); border-radius: var(--radius); padding: 12px 14px; margin-bottom: 12px; }
  .run.selected { border-color: var(--accent); background: var(--accent-soft); }
  .run-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 13px; }
  .run-head code { font-size: 12.5px; }
  .spacer { flex: 1 1 auto; }
  .chip {
    display: inline-block;
    padding: 1px 8px;
    border-radius: 999px;
    border: 1px solid var(--line-strong);
    font-size: 11.5px;
    letter-spacing: .04em;
    text-transform: uppercase;
    color: var(--muted);
    background: var(--panel-alt);
  }
  .chip-running { color: var(--accent); border-color: var(--accent); background: var(--accent-soft); }
  .chip-done { color: var(--ok); border-color: var(--ok); background: var(--ok-soft); }
  .chip-error { color: var(--bad); border-color: var(--bad); background: var(--bad-soft); }
  .bar {
    height: 6px;
    margin: 10px 0 4px;
    background: var(--line);
    border-radius: 999px;
    overflow: hidden;
  }
  .bar > span { display: block; height: 100%; background: var(--accent); transition: width .3s ease; }
  .run-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
  .metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin: 12px 0 16px; }
  .metric { border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; background: var(--panel-alt); }
  .metric-label { display: block; font-size: 11.5px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }
  .metric-value { display: block; margin-top: 4px; font-size: 16px; font-weight: 600; }
  .frame { width: 100%; height: 620px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); }
  pre { overflow: auto; max-height: 320px; padding: 12px; background: var(--panel-alt);
        border: 1px solid var(--line); border-radius: 8px; font-size: 12px; }
  details { margin: 6px 0 14px; }
  summary { cursor: pointer; color: var(--muted); font-size: 13px; }
  a { color: var(--accent); }
  footer { margin-top: 8px; color: var(--muted); font-size: 12.5px; }
</style>
</head>
<body>
<div class="wrap">
  <header class="site">
    <h1>InSilicoTrial MAS - Studio</h1>
    <p>Launches the real pipeline (<code>TrialSimulationPipeline</code>): cohort, agents, analysis and report.</p>
  </header>

  <p class="disclaimer">
    <strong>Synthetic data, decision support only.</strong>
    Every patient in this simulation is generated. Results are a design aid for trial planning and are
    not clinical evidence, not a regulatory submission and not medical advice. Validate population priors
    and drug parameters against licensed reference data before any real decision.
  </p>

  <div class="grid">
    <div>
      <section class="panel" id="control-panel">
        <h2>New run</h2>
        <p class="hint">Runs execute on the server; the page polls while they are active.</p>
        <form id="run-form">
          <label for="protocol">Protocol</label>
          <select id="protocol" name="protocol"></select>
          <div class="row">
            <div>
              <label for="n_patients">Patients</label>
              <input id="n_patients" name="n_patients" type="number" min="10" max="20000" step="10" value="100">
            </div>
            <div>
              <label for="n_cohorts">Cohorts</label>
              <input id="n_cohorts" name="n_cohorts" type="number" min="1" max="256" step="1" value="4">
            </div>
          </div>
          <div class="row">
            <div>
              <label for="epochs">Epochs</label>
              <input id="epochs" name="epochs" type="number" min="1" max="32" step="1" value="8">
            </div>
            <div>
              <label for="seed">Seed</label>
              <input id="seed" name="seed" type="number" step="1" value="20240517">
            </div>
          </div>
          <div class="row">
            <div>
              <label for="engine">Engine</label>
              <select id="engine" name="engine"></select>
            </div>
            <div>
              <label for="llm_mode">LLM mode</label>
              <select id="llm_mode" name="llm_mode"></select>
            </div>
          </div>
          <label for="llm_sample_rate">LLM sample rate</label>
          <input id="llm_sample_rate" name="llm_sample_rate" type="number" min="0" max="1" step="0.01" value="0.05">
          <button class="primary" id="submit-run" type="submit">Run simulation</button>
          <p class="presets">
            <span>Presets:</span>
            <button type="button" data-preset="quick">Quick demo</button>
            <button type="button" data-preset="k1">1k</button>
            <button type="button" data-preset="k10">10k</button>
          </p>
          <p class="muted" id="form-status" role="status" aria-live="polite"></p>
        </form>
      </section>
    </div>

    <div>
      <section class="panel" id="runs-panel">
        <h2>Runs</h2>
        <p class="hint" id="config-path">Loading configuration...</p>
        <div id="run-list" aria-live="polite"></div>
      </section>

      <section class="panel" id="results-panel">
        <h2>Results</h2>
        <div id="detail-body">
          <p class="muted">Select <em>Run summary</em> on a finished run to see its headline metrics and dashboard.</p>
        </div>
      </section>
    </div>
  </div>

  <footer>
    Studio server: Python standard library only. Disable MLflow and CDISC side effects are applied to
    demo runs; set tracking explicitly if you need it.
  </footer>
</div>

<script>
(function () {
  "use strict";

  const POLL_MS = 1500;
  const ACTIVE = { queued: true, running: true };
  const PRESETS = {
    quick: { n_patients: 60, n_cohorts: 2, epochs: 2, engine: "sequential", llm_mode: "off" },
    k1: { n_patients: 1000, n_cohorts: 8, epochs: 8, engine: "auto", llm_mode: "triggered" },
    k10: { n_patients: 10000, n_cohorts: 20, epochs: 8, engine: "auto", llm_mode: "sample" }
  };
  const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const state = { runs: [], selected: null, timer: null };
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value === null || value === undefined ? "" : value)
    .replace(/[&<>"']/g, (ch) => ESCAPES[ch]);

  async function api(path, options) {
    const response = await fetch(path, options);
    const text = await response.text();
    let body = null;
    try { body = text ? JSON.parse(text) : null; } catch (err) { body = null; }
    if (!response.ok) { throw new Error((body && body.error) || ("HTTP " + response.status)); }
    return body;
  }

  function elapsed(run) {
    if (!run.started_at) { return null; }
    const start = Date.parse(run.started_at);
    const end = run.finished_at ? Date.parse(run.finished_at) : Date.now();
    if (Number.isNaN(start) || Number.isNaN(end)) { return null; }
    return Math.max(0, (end - start) / 1000);
  }

  function fmtSeconds(value) {
    if (value === null || value === undefined) { return "-"; }
    if (value < 60) { return value.toFixed(0) + " s"; }
    const minutes = Math.floor(value / 60);
    const seconds = Math.round(value % 60);
    return minutes + " min " + (seconds < 10 ? "0" : "") + seconds + " s";
  }

  function chip(status) {
    return '<span class="chip chip-' + esc(status) + '">' + esc(status) + "</span>";
  }

  function progressBar(run) {
    const percent = Math.round(Math.max(0, Math.min(1, run.progress || 0)) * 100);
    return '<div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + percent +
      '" aria-label="Progress of ' + esc(run.run_id) + '"><span style="width:' + percent + '%"></span></div>';
  }

  function renderRuns() {
    const host = $("run-list");
    if (!state.runs.length) {
      host.innerHTML = '<p class="muted">No runs yet. Choose a protocol and select Run simulation.</p>';
      return;
    }
    host.innerHTML = state.runs.map(function (run) {
      const failed = run.error ? '<p class="error" role="alert">' + esc(run.error) + "</p>" : "";
      return '<article class="run' + (run.run_id === state.selected ? " selected" : "") + '">' +
        '<div class="run-head"><code>' + esc(run.run_id) + "</code>" + chip(run.status) +
        '<span class="muted">' + esc(run.phase) + "</span>" +
        '<span class="spacer"></span><span class="muted">' + fmtSeconds(elapsed(run)) + "</span></div>" +
        progressBar(run) + failed +
        '<div class="run-actions">' +
        '<button type="button" data-action="dashboard" data-run="' + esc(run.run_id) + '">Open dashboard</button>' +
        '<button type="button" data-action="summary" data-run="' + esc(run.run_id) + '">Run summary</button>' +
        "</div></article>";
    }).join("");
  }

  function headline(run) {
    const summary = run.summary || {};
    const overview = summary.overview || {};
    const armKeys = Object.keys(overview).filter((key) => key.indexOf("grade3_rate_") === 0);
    const effectKey = Object.keys(overview).find((key) => key.indexOf("effect_") === 0);
    const patients = overview.n_patients !== undefined ? overview.n_patients : summary.patients;
    const grade3 = (overview.grade3_plus_patients !== undefined && patients)
      ? (100 * overview.grade3_plus_patients / patients) : null;
    return [
      ["Patients", patients === undefined || patients === null ? "-" : patients],
      ["Arms", armKeys.length || "-"],
      ["Primary effect", effectKey ? effectKey.replace(/^effect_/, "").replace(/_/g, " ") + ": " + overview[effectKey] : "-"],
      ["Grade 3 or above", grade3 === null ? "-" : grade3.toFixed(2) + " %"],
      ["Duration", summary.duration_seconds === undefined ? "-" : fmtSeconds(summary.duration_seconds)]
    ];
  }

  function renderDetail() {
    const host = $("detail-body");
    const run = state.runs.find((item) => item.run_id === state.selected);
    if (!run) {
      host.innerHTML = '<p class="muted">Select <em>Run summary</em> on a finished run to see its ' +
        "headline metrics and dashboard.</p>";
      return;
    }
    const link = "/api/runs/" + encodeURIComponent(run.run_id) + "/dashboard";
    const cards = headline(run).map(([label, value]) =>
      '<div class="metric"><span class="metric-label">' + esc(label) + '</span>' +
      '<span class="metric-value">' + esc(value) + "</span></div>").join("");
    const summaryBlock = Object.keys(run.summary || {}).length
      ? "<details><summary>Raw run summary (JSON)</summary><pre>" +
        esc(JSON.stringify(run.summary, null, 2)) + "</pre></details>"
      : "";
    const frame = run.status === "done"
      ? '<iframe class="frame" src="' + link + '" title="Dashboard for ' + esc(run.run_id) + '"></iframe>'
      : '<p class="muted">The dashboard appears here when the run completes.</p>';
    host.innerHTML = '<p class="run-head"><code>' + esc(run.run_id) + "</code> " + chip(run.status) +
      ' <span class="muted">' + esc(run.phase) + '</span> <span class="spacer"></span>' +
      '<span class="muted">' + fmtSeconds(elapsed(run)) + "</span></p>" +
      '<div class="metrics">' + cards + "</div>" + summaryBlock + frame +
      '<p><a href="' + link + '" target="_blank" rel="noopener">Open dashboard in a new tab</a></p>';
  }

  function setStatus(message, isError) {
    const host = $("form-status");
    host.className = isError ? "error" : "ok";
    host.textContent = message;
  }

  function schedule() {
    if (state.timer) { return; }
    if (!state.runs.some((run) => ACTIVE[run.status])) { return; }
    state.timer = window.setTimeout(function () { state.timer = null; refresh(); }, POLL_MS);
  }

  async function refresh() {
    try {
      const data = await api("/api/runs");
      state.runs = data.runs || [];
      renderRuns();
      renderDetail();
    } catch (err) {
      setStatus("Could not load runs: " + err.message, true);
    }
    schedule();
  }

  async function loadConfig() {
    const data = await api("/api/config");
    const select = $("protocol");
    select.innerHTML = (data.protocols || []).map((protocol) =>
      '<option value="' + esc(protocol.path) + '">' + esc(protocol.protocol_id) + " - " + esc(protocol.title) +
      " (" + esc(protocol.phase) + ", " + esc(protocol.epochs) + " epochs, " + esc((protocol.arms || []).length) +
      " arms)</option>").join("");
    const fill = (id, values) => {
      $(id).innerHTML = (values || []).map((value) =>
        '<option value="' + esc(value) + '">' + esc(value) + "</option>").join("");
    };
    fill("engine", data.options && data.options.engines);
    fill("llm_mode", data.options && data.options.llm_modes);
    const defaults = data.defaults || {};
    $("n_patients").value = defaults.n_patients !== undefined ? defaults.n_patients : 100;
    $("n_cohorts").value = defaults.n_cohorts !== undefined ? defaults.n_cohorts : 4;
    $("epochs").value = defaults.epochs !== undefined ? defaults.epochs : 8;
    $("seed").value = defaults.seed !== undefined ? defaults.seed : 20240517;
    $("llm_sample_rate").value = defaults.llm_sample_rate !== undefined ? defaults.llm_sample_rate : 0.05;
    if (defaults.engine) { $("engine").value = defaults.engine; }
    if (defaults.llm_mode) { $("llm_mode").value = defaults.llm_mode; }
    if (defaults.protocol) { select.value = defaults.protocol; }
    $("config-path").textContent = data.config_path
      ? "Configuration: " + data.config_path
      : "Using the package defaults.";
    if (!select.options.length) {
      $("submit-run").disabled = true;
      setStatus("No launchable protocol was found next to the configuration file.", true);
    }
  }

  $("run-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    const button = $("submit-run");
    button.disabled = true;
    try {
      const payload = {
        protocol: $("protocol").value,
        n_patients: Number($("n_patients").value),
        n_cohorts: Number($("n_cohorts").value),
        epochs: Number($("epochs").value),
        engine: $("engine").value,
        llm_mode: $("llm_mode").value,
        seed: Number($("seed").value),
        llm_sample_rate: Number($("llm_sample_rate").value)
      };
      const run = await api("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      state.selected = run.run_id;
      setStatus("Run " + run.run_id + " queued.", false);
      await refresh();
    } catch (err) {
      setStatus("Could not start the run: " + err.message, true);
    } finally {
      button.disabled = false;
    }
  });

  $("run-list").addEventListener("click", function (event) {
    const target = event.target.closest("button[data-run]");
    if (!target) { return; }
    state.selected = target.getAttribute("data-run");
    renderRuns();
    renderDetail();
    $("results-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  });

  document.querySelectorAll("button[data-preset]").forEach(function (button) {
    button.addEventListener("click", function () {
      const preset = PRESETS[button.getAttribute("data-preset")];
      if (!preset) { return; }
      Object.keys(preset).forEach(function (key) { $(key).value = preset[key]; });
      setStatus("Preset applied. Select Run simulation to start.", false);
    });
  });

  loadConfig().catch(function (err) { setStatus("Could not load the configuration: " + err.message, true); });
  refresh();
})();
</script>
</body>
</html>
"""
