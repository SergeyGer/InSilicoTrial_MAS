"""End-to-end orchestration: cohort -> screening -> simulation -> Gold -> report.

This is the module the CLI, the notebooks and the Databricks job all call, so
there is exactly one definition of "a simulation run". The pipeline is explicit
about every side effect it performs, in order, because the audit trail depends on
that order:

1. load and validate the protocol (Protocol Agent),
2. generate the synthetic screening population,
3. screen and randomise (Protocol Agent) -> Bronze,
4. resolve the physiology model and build the run context,
5. execute the agents on the selected engine -> Silver (versioned / Delta),
6. analyse (Biostatistician Agent) -> Gold + report artefacts,
7. export CDISC-inspired datasets,
8. log parameters, metrics, artefacts and the trace summary to MLflow,
9. write the run manifest that makes the run replayable.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from .agents.base import AgentContext
from .agents.biostatistician_agent import AnalysisResult, BiostatisticianAgent
from .agents.protocol_agent import (
    AllocationOutcome,
    ProtocolAgent,
    ScreeningOutcome,
)
from .cohort.generator import CohortGenerator, cohort_size_estimate, cohort_summary
from .cohort.serialization import profiles_to_frame
from .config import SimulationConfig, load_config
from .engine.base import EngineResult
from .engine.checklist import inspect_environment
from .engine.context import RunSpec
from .engine.factory import create_engine, engine_plan
from .errors import ConfigurationError, SimulationError
from .governance.lineage import LineageTracker
from .governance.namespace import UnityCatalogNamespace
from .logging_utils import get_logger
from .ml.registry import load_physiology_model
from .mlflow_tracking.tracing import TraceStore
from .mlflow_tracking.tracker import MlflowTracker, resolve_tracking_uri
from .reporting.cdisc import export_cdisc
from .reporting.report import ReportArtifacts, ReportBuilder
from .reproducibility import stable_hash
from .schemas import SimulationProvenance, TrialProtocol
from .storage.base import BaseStore
from .storage.factory import create_store
from .ui.dashboard import write_dashboard
from .version import SILVER_SCHEMA_VERSION, __version__, git_revision

logger = get_logger("pipeline")

RUN_MANIFEST_FILE = "run_manifest.json"

#: Progress callback signature: ``(phase, fraction_complete)``.
ProgressCallback = Callable[[str, float], None]


def _emit(progress: ProgressCallback | None, phase: str, fraction: float) -> None:
    """Report progress to a caller (studio UI, notebook widget) - never fatal."""
    if progress is None:
        return
    try:
        progress(phase, max(0.0, min(1.0, fraction)))
    except Exception as exc:
        logger.debug(f"progress callback failed: {exc}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_protocol(path: str | Path) -> TrialProtocol:
    """Load and validate a trial protocol from YAML."""
    file_path = Path(path)
    if not file_path.exists():
        raise ConfigurationError(f"protocol file not found: {file_path}")
    payload = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ConfigurationError(f"{file_path}: protocol must be a YAML mapping")
    return TrialProtocol.model_validate(payload)


@dataclass(slots=True)
class RunResult:
    """Everything a completed run produced."""

    run_id: str
    protocol: TrialProtocol
    config: SimulationConfig
    provenance: SimulationProvenance
    screening: ScreeningOutcome
    allocation: AllocationOutcome
    engine_result: EngineResult
    analysis: AnalysisResult
    report: ReportArtifacts = field(default_factory=ReportArtifacts)
    dashboard_path: str = ""
    silver_location: str = ""
    silver_version: int = -1
    gold_tables: dict[str, str] = field(default_factory=dict)
    cdisc: dict[str, Any] = field(default_factory=dict)
    lineage: dict[str, Any] = field(default_factory=dict)
    lineage_mermaid: str = ""
    environment: dict[str, Any] = field(default_factory=dict)
    output_dir: str = ""
    manifest_path: str = ""
    duration_seconds: float = 0.0

    def summary(self) -> dict[str, Any]:
        """Compact, JSON-serialisable run summary."""
        return {
            "run_id": self.run_id,
            "protocol_id": self.protocol.protocol_id,
            "patients": self.engine_result.n_patients,
            "rows": self.engine_result.n_rows,
            "engine": self.engine_result.backend,
            "silver": {"location": self.silver_location, "version": self.silver_version},
            "gold_tables": self.gold_tables,
            "overview": self.analysis.overview,
            "safety_alerts": self.analysis.safety_alerts,
            "report": self.report.as_dict(),
            "dashboard": self.dashboard_path,
            "duration_seconds": round(self.duration_seconds, 2),
        }


class TrialSimulationPipeline:
    """Runs one complete in-silico trial simulation."""

    def __init__(
        self,
        config: SimulationConfig,
        protocol: TrialProtocol | None = None,
        *,
        store: BaseStore | None = None,
    ) -> None:
        self.config = config
        self.protocol = protocol
        self.store = store or create_store(config)
        self.namespace = UnityCatalogNamespace.from_storage_config(config.storage)

    # -- construction helpers ---------------------------------------------
    @classmethod
    def from_config_file(cls, path: str | Path, overrides: dict[str, Any] | None = None) -> TrialSimulationPipeline:
        config = load_config(path, overrides)
        protocol = load_protocol(config.protocol_path)
        return cls(config, protocol)

    # -- public API --------------------------------------------------------
    def run(
        self,
        *,
        run_id: str | None = None,
        force_offline: bool = False,
        progress: ProgressCallback | None = None,
    ) -> RunResult:
        """Execute the full pipeline.

        ``progress`` (optional) is called as ``progress(phase, fraction)`` at every
        major step so a UI can show where the run is; it is never allowed to fail
        the run itself.
        """
        started = time.perf_counter()
        _emit(progress, "protocol", 0.02)
        config = self.config
        protocol = self.protocol or load_protocol(config.protocol_path)
        self.protocol = protocol  # keep instance state consistent for later helpers
        if config.epochs:
            protocol = protocol.model_copy(update={"epochs": config.epochs})
        run_id = run_id or self._new_run_id(protocol)

        output_dir = Path(config.output_dir) / run_id
        output_dir.mkdir(parents=True, exist_ok=True)
        lineage = LineageTracker(run_id, created_at=utc_now())
        environment = inspect_environment(config)

        # 1-3. Protocol Agent: validate, screen, randomise.
        protocol_agent = ProtocolAgent(
            AgentContext(run_id=run_id, protocol=protocol, config=config, seed=config.seed, total_epochs=protocol.epochs)
        )
        warnings = protocol_agent.validate()
        for warning in warnings:
            logger.warning(f"protocol check: {warning}")
        _emit(progress, "cohort", 0.05)
        cohort = CohortGenerator(protocol, config).generate()
        _emit(progress, "screening", 0.10)
        screening, allocation = protocol_agent.plan(cohort)
        if not screening.enrolled:
            raise SimulationError("no patient passed eligibility screening; check the protocol criteria")

        cohort_frame = profiles_to_frame(screening.enrolled, allocation.arm_by_patient)
        bronze = self._write_bronze(cohort_frame, protocol, run_id, output_dir)
        lineage.add_node("agents.protocol", "agent", label="Protocol Agent")
        lineage.add_node("bronze.synthetic_cohort", "table", label="Synthetic cohort (Bronze)")
        lineage.add_edge("agents.protocol", "bronze.synthetic_cohort", relation="produces", agent="protocol")
        lineage.add_edge(
            "protocol:" + protocol.protocol_id,
            "bronze.synthetic_cohort",
            relation="parameterises",
            agent="protocol",
            protocol_digest=protocol.digest(),
        )

        # 4. Physiology model + run context.
        loaded = load_physiology_model(protocol, config.ml, output_dir=config.output_dir)
        context = RunSpec(
            run_id=run_id,
            protocol=protocol,
            config=config,
            arm_by_patient=allocation.arm_by_patient,
            seed=config.seed,
            total_epochs=protocol.epochs,
        ).to_context(
            model_version=loaded.version,
            model_path=loaded.path,
            model_digest=loaded.digest,
        )
        plan = engine_plan(context)
        logger.info(
            "engine plan",
            extra={"extra_fields": {"requested": plan["requested"], "resolved": plan["resolved"]}},
        )

        # 5. Simulate.
        _emit(progress, "simulate", 0.15)
        engine = create_engine(context, force_offline=force_offline)
        engine_result = engine.run(cohort_frame, run_id=run_id)
        _emit(progress, "silver-write", 0.75)
        silver_write = self._write_silver(engine_result, run_id)
        lineage.add_node("silver.patient_states", "table", label="Patient states (Silver)")
        lineage.add_edge("bronze.synthetic_cohort", "silver.patient_states", relation="derives_from", agent="engine")
        lineage.add_edge(
            f"model:{loaded.version}",
            "silver.patient_states",
            relation="predicts",
            agent="patient",
            backend=loaded.backend,
            digest=loaded.digest,
        )

        observations = self._materialise(engine_result, silver_write, run_id=run_id)
        if observations.empty:
            raise SimulationError("the simulation produced no observations")
        self._write_derived_silver(observations, screening, run_id, lineage)

        # 6. Analyse + report.
        _emit(progress, "analysis", 0.82)
        biostatistician = BiostatisticianAgent(context)
        analysis = biostatistician.analyze(
            observations,
            screening_summary=screening.summary(),
            allocation={"arm_counts": allocation.arm_counts()},
            extra_quality={
                "storage_backend": self.store.backend,
                "silver_location": silver_write.location,
                "engine_stats": _jsonable(engine_result.stats),
            },
        )
        traces = TraceStore(config.tracking.trace_path).summary(run_id=run_id) if config.tracking.trace_path else {}
        provenance = self._provenance(
            run_id=run_id,
            protocol=protocol,
            engine_result=engine_result,
            loaded=loaded,
            screening=screening,
            silver_location=silver_write.location,
            started_at=context.created_at,
            traces=traces,
        )
        builder = ReportBuilder(
            protocol,
            provenance,
            analysis,
            lineage=lineage.summary(),
            lineage_mermaid=lineage.to_mermaid(),
            traces=traces,
            engine_info={"plan": _jsonable(plan), "environment": environment.as_dict(), "stats": _jsonable(engine_result.stats)},
            protocol_warnings=warnings,
        )
        _emit(progress, "report", 0.88)
        report = builder.write(output_dir / "report", formats=config.report_formats)
        lineage.add_node("report.trial_report", "report", label="Trial report")
        lineage.add_edge("silver.patient_states", "report.trial_report", relation="summarised_by", agent="biostatistician")

        # 7. Gold tables + CDISC-inspired export.
        _emit(progress, "gold", 0.92)
        gold = self._write_gold(analysis, provenance, lineage, run_id)
        cdisc: dict[str, Any] = {}
        if config.export_cdisc:
            export = export_cdisc(
                output_dir / "cdisc",
                profiles=screening.enrolled,
                arm_by_patient=allocation.arm_by_patient,
                observations=observations,
                adverse_events=analysis.adverse_events,
                drug_name=protocol.drug.name,
            )
            cdisc = export.as_dict()

        # 8. MLflow tracking and raw LLM traces.
        _emit(progress, "tracking", 0.95)
        self._track(builder, protocol, provenance, analysis, loaded, report, engine_result, run_id)
        self._write_llm_raw_traces(run_id)

        # 9. Run manifest.
        duration = time.perf_counter() - started
        provenance.duration_seconds = duration
        manifest = {
            "run_id": run_id,
            "silver_schema_version": SILVER_SCHEMA_VERSION,
            "package_version": __version__,
            "git_revision": git_revision(),
            "created_at": utc_now(),
            "provenance": provenance.as_dict(),
            "protocol": protocol.model_dump(mode="json"),
            "config": config.to_dict(),
            "cohort": cohort_summary(screening.enrolled),
            "screening": screening.summary(),
            "allocation": allocation.arm_counts(),
            "estimate": cohort_size_estimate(protocol, config),
            "engine": {"plan": _jsonable(plan), "stats": _jsonable(engine_result.stats), "backend": engine_result.backend},
            "storage": {"backend": self.store.backend, "silver": silver_write.as_dict(), "gold": gold, "bronze": bronze},
            "environment": environment.as_dict(),
            "traces": traces,
            "analysis_overview": analysis.overview,
            "report": report.as_dict(),
            "cdisc": cdisc,
            "lineage": lineage.summary(),
            "lineage_graph": lineage.to_dict(),
            "lineage_mermaid": lineage.to_mermaid(),
            "protocol_warnings": warnings,
            "replay": {
                "command": (
                    f"insilico-trial simulate --config <config.yaml> --seed {config.seed} --run-id {run_id}"
                ),
                "seed": config.seed,
                "protocol_digest": protocol.digest(),
            },
        }
        manifest_path = output_dir / RUN_MANIFEST_FILE
        manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
        (output_dir / "silver_observations.parquet").parent.mkdir(parents=True, exist_ok=True)
        observations.to_parquet(output_dir / "silver_observations.parquet", index=False)

        # 10. Interactive dashboard (self-contained HTML, no server required).
        # Built after the manifest so it can read the complete provenance, then
        # recorded back into the manifest.
        dashboard_path = ""
        if config.export_dashboard:
            _emit(progress, "dashboard", 0.97)
            try:
                dashboard_path = str(write_dashboard(output_dir))
                manifest["dashboard"] = dashboard_path
                manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
            except Exception as exc:
                logger.warning(f"dashboard generation skipped: {type(exc).__name__}: {exc}")
        _emit(progress, "done", 1.0)
        logger.info(f"run {run_id} finished in {duration:.1f}s; manifest at {manifest_path}")

        return RunResult(
            run_id=run_id,
            protocol=protocol,
            config=config,
            provenance=provenance,
            screening=screening,
            allocation=allocation,
            engine_result=engine_result,
            analysis=analysis,
            report=report,
            dashboard_path=dashboard_path,
            silver_location=silver_write.location,
            silver_version=silver_write.version,
            gold_tables=gold,
            cdisc=cdisc,
            lineage=lineage.summary(),
            lineage_mermaid=lineage.to_mermaid(),
            environment=environment.as_dict(),
            output_dir=str(output_dir),
            manifest_path=str(manifest_path),
            duration_seconds=duration,
        )

    # -- internals ---------------------------------------------------------
    def _new_run_id(self, protocol: TrialProtocol) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        suffix = stable_hash(protocol.protocol_id, self.config.seed, stamp, length=6)
        return f"RUN-{stamp}-{suffix.upper()}"

    def _write_bronze(
        self, cohort_frame: pd.DataFrame, protocol: TrialProtocol, run_id: str, output_dir: Path
    ) -> dict[str, Any]:
        frame = cohort_frame.copy()
        frame["sim_run_id"] = run_id
        result = self.store.write(
            self.namespace.logical("bronze", "synthetic_cohort"),
            frame,
            run_id=run_id,
            mode=self.config.storage.write_mode,
        )
        cohort_frame.to_parquet(output_dir / "cohort.parquet", index=False)
        protocol_payload = protocol.model_dump(mode="json")
        protocol_payload["sim_run_id"] = run_id
        self.store.write(
            self.namespace.logical("bronze", "protocol_definitions"),
            [protocol_payload],
            run_id=run_id,
            mode="append",
        )
        return result.as_dict()

    def _write_silver(self, engine_result: EngineResult, run_id: str) -> Any:
        table = self.namespace.logical("silver", "patient_states")
        payload = engine_result.dataframe if engine_result.dataframe is not None else engine_result.rows
        partition_by = list(self.config.storage.delta_partition_by)
        return self.store.write(table, payload, run_id=run_id, mode="append", partition_by=partition_by)

    def _write_derived_silver(
        self,
        observations: pd.DataFrame,
        screening: ScreeningOutcome,
        run_id: str,
        lineage: LineageTracker,
    ) -> None:
        """Write the derived Silver tables: adverse events, deviations, screen failures.

        These are written on *every* storage backend so that a laptop run and a
        cluster run expose the same contract (previously they were Delta-only).
        """
        from .agents.biostatistician_agent import extract_adverse_events

        events = extract_adverse_events(observations)
        if not events.empty:
            events = events.copy()
            events["sim_run_id"] = run_id
            self.store.write(self.namespace.logical("silver", "adverse_events"), events, run_id=run_id)
            lineage.add_node("silver.adverse_events", "table", label="Adverse events (Silver)")
            lineage.add_edge(
                "silver.patient_states", "silver.adverse_events", relation="normalises", agent="biostatistician"
            )

        deviations = observations[observations["discontinued"].fillna(False).astype(bool)][
            ["patient_id", "cohort_id", "arm_id", "epoch", "discontinuation_reason"]
        ].copy()
        if not deviations.empty:
            deviations["sim_run_id"] = run_id
            deviations["reason"] = deviations["discontinuation_reason"]
            self.store.write(self.namespace.logical("silver", "protocol_deviations"), deviations, run_id=run_id)
            lineage.add_node("silver.protocol_deviations", "table", label="Protocol deviations (Silver)")
            lineage.add_edge("agents.protocol", "silver.protocol_deviations", relation="records", agent="protocol")

        if screening.screen_failures:
            failures = pd.DataFrame(screening.screen_failures)
            failures["sim_run_id"] = run_id
            self.store.write(self.namespace.logical("silver", "screen_failures"), failures, run_id=run_id)
            lineage.add_node("silver.screen_failures", "table", label="Screen failures (Silver)")
            lineage.add_edge("agents.protocol", "silver.screen_failures", relation="records", agent="protocol")

    def _write_llm_raw_traces(self, run_id: str) -> None:
        """Copy the raw LLM spans of this run into ``bronze.llm_raw_traces``.

        The JSONL trace file remains the low-level record; this table makes the
        traces queryable alongside the cohort and protocol definitions.
        """
        trace_path = self.config.tracking.trace_path
        if not trace_path:
            return
        from .mlflow_tracking.tracing import TraceStore

        rows = list(TraceStore(trace_path).as_rows(run_id=run_id))
        if not rows:
            return
        self.store.write(self.namespace.logical("bronze", "llm_raw_traces"), rows, run_id=run_id)

    def _materialise(
        self,
        engine_result: EngineResult,
        silver_write: Any | None,
        *,
        run_id: str = "",
        sample: int | None = None,
    ) -> pd.DataFrame:
        """Return the Silver observations as a pandas frame.

        For Spark runs the rows are read back from the Delta table (or collected
        from the cached DataFrame when the table has just been written), which
        keeps a single source of truth for the analysis.
        """
        if engine_result.rows is not None:
            return pd.DataFrame(engine_result.rows)
        if sample is not None and engine_result.dataframe is not None:
            return engine_result.dataframe.limit(sample).toPandas()
        table = self.namespace.logical("silver", "patient_states")
        try:
            # Read back only this run's rows: the Silver table accumulates across
            # runs and the analysis must never mix two simulations.
            return self.store.read(table, run_id=run_id or None)
        except Exception as exc:
            logger.warning(f"could not read Silver back from {table} ({exc}); collecting from the job instead")
            if engine_result.dataframe is None:
                return pd.DataFrame()
            return engine_result.dataframe.toPandas()

    def _write_gold(
        self,
        analysis: AnalysisResult,
        provenance: SimulationProvenance,
        lineage: LineageTracker,
        run_id: str,
    ) -> dict[str, str]:
        tables: dict[str, str] = {}
        writes = {
            "arm_summaries": pd.DataFrame([asdict(summary) for summary in analysis.arm_summaries]),
            "endpoint_comparisons": pd.DataFrame([asdict(comparison) for comparison in analysis.comparisons]),
            "safety_summary": pd.DataFrame(analysis.safety_summary),
            "stopping_rule_evaluations": pd.DataFrame([asdict(e) for e in analysis.stopping_evaluations]),
            "lineage_edges": lineage.to_frame(),
        }
        for name, frame in writes.items():
            frame = frame.copy()
            frame["sim_run_id"] = run_id
            result = self.store.write(self.namespace.logical("gold", name), frame, run_id=run_id, mode="append")
            tables[name] = result.location
        manifest_frame = pd.DataFrame([provenance.as_dict()])
        manifest_frame["sim_run_id"] = run_id
        result = self.store.write(
            self.namespace.logical("gold", "run_manifest"), manifest_frame, run_id=run_id, mode="append"
        )
        tables["run_manifest"] = result.location
        return tables

    def _provenance(
        self,
        *,
        run_id: str,
        protocol: TrialProtocol,
        engine_result: EngineResult,
        loaded: Any,
        screening: ScreeningOutcome,
        silver_location: str,
        started_at: str,
        traces: dict[str, Any],
    ) -> SimulationProvenance:
        stats = engine_result.stats or {}
        return SimulationProvenance(
            sim_run_id=run_id,
            protocol_id=protocol.protocol_id,
            protocol_digest=protocol.digest(),
            protocol_version=protocol.version,
            package_version=__version__,
            git_revision=git_revision(),
            engine_backend=engine_result.backend,
            spark_version=str(engine_result.runtime_info.get("spark_version", "")),
            master_url=str(engine_result.runtime_info.get("master", "")),
            seed=self.config.seed,
            n_patients=engine_result.n_patients,
            n_cohorts=len({p.cohort_id for p in screening.enrolled}),
            n_epochs=protocol.epochs,
            physiology_model_version=loaded.version,
            physiology_model_digest=loaded.digest,
            physiology_backend=loaded.backend,
            llm_provider=self.config.llm.provider,
            llm_model=self.config.llm.model,
            llm_temperature=self.config.llm.temperature,
            llm_mode=self.config.llm_mode,
            started_at=started_at,
            finished_at=utc_now(),
            duration_seconds=engine_result.duration_seconds,
            total_llm_calls=int(stats.get("llm_calls", 0)) + int(traces.get("llm_calls", 0)),
            llm_cache_hits=int(traces.get("cache_hits", 0)),
            total_tokens_in=int(traces.get("tokens_in_total", 0)),
            total_tokens_out=int(traces.get("tokens_out_total", 0)),
            storage_backend=self.store.backend,
            silver_location=silver_location,
            notes=f"model source: {loaded.source}",
        )

    def _track(
        self,
        builder: ReportBuilder,
        protocol: TrialProtocol,
        provenance: SimulationProvenance,
        analysis: AnalysisResult,
        loaded: Any,
        report: ReportArtifacts,
        engine_result: EngineResult,
        run_id: str,
    ) -> None:
        """Log the run to MLflow (best effort unless tracking.strict is set)."""
        if not self.config.tracking.enabled:
            return
        tracker = MlflowTracker(self.config.tracking, resolve_tracking_uri(self.config))
        if not tracker.available:
            logger.info(f"MLflow tracking disabled ({tracker.degraded_reason or 'not configured'})")
            return
        with tracker.start_run(
            run_name=self.config.tracking.run_name or f"trial-simulation-{run_id}",
            tags={
                "protocol_id": protocol.protocol_id,
                "engine": engine_result.backend,
                "git_revision": provenance.git_revision,
                "run_id": run_id,
            },
        ):
            tracker.log_params(
                {
                    "protocol_id": protocol.protocol_id,
                    "protocol_digest": provenance.protocol_digest,
                    "seed": self.config.seed,
                    "n_patients": provenance.n_patients,
                    "n_epochs": provenance.n_epochs,
                    "engine": engine_result.backend,
                    "llm_provider": self.config.llm.provider,
                    "llm_model": self.config.llm.model,
                    "llm_mode": self.config.llm_mode,
                    "physiology_backend": loaded.backend,
                    "physiology_model_version": loaded.version,
                    "physiology_model_source": loaded.source,
                    "storage_backend": self.store.backend,
                }
            )
            tracker.log_metrics(analysis.overview)
            tracker.log_metrics(
                {
                    "n_rows": engine_result.n_rows,
                    "duration_seconds": engine_result.duration_seconds,
                    "n_adverse_events": len(analysis.adverse_events),
                    "n_safety_alerts": len(analysis.safety_alerts),
                    "stopping_rules_triggered": sum(1 for e in analysis.stopping_evaluations if e.triggered),
                }
            )
            tracker.log_dict(builder.payload(), "report_payload.json")
            for path in (report.markdown, report.html, report.payload):
                if path:
                    tracker.log_artifact(path)
            if self.config.tracking.register_model and loaded.path:
                tracker.register_model(loaded.path, self.config.ml.mlflow_model_name)


def _jsonable(payload: Any) -> Any:
    """Make engine stats JSON-serialisable without losing their structure."""
    try:
        json.dumps(payload, default=str)
    except (TypeError, ValueError):
        return {"repr": str(payload)}
    return payload
