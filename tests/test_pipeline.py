"""End-to-end pipeline tests: artefacts, reproducibility and run manifests."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from insilico_trial_mas.agents.biostatistician_agent import BiostatisticianAgent
from insilico_trial_mas.config import load_config
from insilico_trial_mas.engine.partition import comparable_rows
from insilico_trial_mas.errors import ConfigurationError, SimulationError
from insilico_trial_mas.pipeline import RUN_MANIFEST_FILE, TrialSimulationPipeline
from insilico_trial_mas.storage.factory import MemoryStore


@pytest.fixture
def pipeline(config, protocol) -> TrialSimulationPipeline:
    return TrialSimulationPipeline(config, protocol, store=MemoryStore(config.storage))


def test_pipeline_produces_every_artefact(pipeline, tmp_path) -> None:
    result = pipeline.run(run_id="RUN-TEST-1")
    assert result.run_id == "RUN-TEST-1"
    assert result.engine_result.n_patients > 0
    assert result.engine_result.n_rows == result.engine_result.n_patients * (result.protocol.epochs + 1)
    assert result.analysis.arm_summaries
    assert result.report.payload, "the JSON report is always produced"

    output = Path(result.output_dir)
    assert (output / RUN_MANIFEST_FILE).exists()
    assert (output / "silver_observations.parquet").exists()
    assert (output / "cohort.parquet").exists()
    manifest = json.loads((output / RUN_MANIFEST_FILE).read_text(encoding="utf-8"))
    assert manifest["run_id"] == "RUN-TEST-1"
    assert manifest["provenance"]["protocol_digest"] == result.protocol.digest()
    assert manifest["lineage"]["edges"] > 0
    assert manifest["lineage_mermaid"].startswith("flowchart")
    assert manifest["replay"]["seed"] == result.config.seed
    assert manifest["cohort"]["n"] == result.engine_result.n_patients


def test_gold_tables_are_written(pipeline) -> None:
    result = pipeline.run(run_id="RUN-TEST-GOLD")
    for table in ("arm_summaries", "endpoint_comparisons", "safety_summary", "lineage_edges", "run_manifest"):
        assert table in result.gold_tables
    stored = pipeline.store.read("silver/patient_states", run_id="RUN-TEST-GOLD")
    assert not stored.empty
    assert stored["sim_run_id"].nunique() == 1


def test_repeated_runs_are_bit_identical(config, protocol) -> None:
    """The same seed must reproduce the same trial, in independent stores."""
    frames = []
    for _ in range(2):
        pipeline = TrialSimulationPipeline(config, protocol, store=MemoryStore(config.storage))
        pipeline.run(run_id="RUN-REPRO")
        observations = pipeline.store.read("silver/patient_states", run_id="RUN-REPRO")
        frames.append(comparable_rows(observations.to_dict(orient="records")))
    assert frames[0] == frames[1]
    assert len(frames[0]) > 0


def test_different_seed_changes_the_trial(config, protocol, tmp_path) -> None:
    other = load_config(None, {**config.to_dict(), "seed": config.seed + 1})
    other.store = config.storage
    first = TrialSimulationPipeline(config, protocol, store=MemoryStore(config.storage)).run(run_id="RUN-S1")
    second = TrialSimulationPipeline(other, protocol, store=MemoryStore(other.storage)).run(run_id="RUN-S2")
    assert first.analysis.arm_summaries[0].mean_sbp_change != second.analysis.arm_summaries[0].mean_sbp_change


def test_csv_export_when_enabled(config, protocol) -> None:
    config.export_cdisc = True
    pipeline = TrialSimulationPipeline(config, protocol, store=MemoryStore(config.storage))
    result = pipeline.run(run_id="RUN-CDIS")
    assert set(result.cdisc["datasets"]) == {"DM", "EX", "VS", "AE", "ADSL", "ADVS", "ADAE"}
    for name, path in result.cdisc["datasets"].items():
        assert Path(path).exists(), f"{name} dataset missing"
        frame = pd.read_csv(path)
        assert not frame.empty or name in {"AE", "ADAE"}
    assert Path(result.cdisc["define"]).exists()


def test_run_manifest_records_llm_policy(pipeline) -> None:
    result = pipeline.run(run_id="RUN-LLM")
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert manifest["provenance"]["llm_mode"] == result.config.llm_mode
    assert manifest["provenance"]["llm_provider"] == "offline"
    assert manifest["engine"]["backend"] in {"sequential", "local", "spark"}


def test_pipeline_requires_eligible_patients(config, protocol) -> None:
    config.eligibility = None  # not a real field; ensure override path is explicit
    strict_protocol = protocol.model_copy(deep=True)
    strict_protocol.eligibility.min_age = 120  # nobody qualifies
    pipeline = TrialSimulationPipeline(config, strict_protocol, store=MemoryStore(config.storage))
    with pytest.raises(SimulationError, match="no patient passed eligibility screening"):
        pipeline.run()


def test_pipeline_from_config_file(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "protocol_path: conf/trial_protocol_demo.yaml",
                "n_patients: 40",
                "n_cohorts: 2",
                "epochs: 2",
                "output_dir: " + str(tmp_path / "out"),
                "llm_mode: off",
                "report_formats: [json]",
                "storage: {backend: memory}",
                "engine: {backend: sequential, batch_size: 16}",
                "llm: {provider: offline, cache_enabled: false}",
                "ml: {backend: mechanistic, auto_train_if_missing: false}",
                "tracking: {enabled: false}",
            ]
        ),
        encoding="utf-8",
    )
    pipeline = TrialSimulationPipeline.from_config_file(config_path)
    result = pipeline.run()
    assert result.run_id.startswith("RUN-")
    assert result.engine_result.n_patients > 0


def test_missing_protocol_file_is_reported(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("protocol_path: conf/nope.yaml\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="protocol file not found"):
        TrialSimulationPipeline.from_config_file(config_path)
    with pytest.raises(ConfigurationError, match="configuration file not found"):
        TrialSimulationPipeline.from_config_file(tmp_path / "missing.yaml")


def test_analysis_handles_empty_frame(context) -> None:
    analysis = BiostatisticianAgent(context).analyze(pd.DataFrame())
    assert analysis.arm_summaries == []
    assert analysis.overview == {}


def test_report_builder_renders_all_formats(pipeline) -> None:
    from insilico_trial_mas.reporting.report import ReportBuilder

    result = pipeline.run(run_id="RUN-REPORT")
    builder = ReportBuilder(
        result.protocol,
        result.provenance,
        result.analysis,
        lineage=result.lineage,
        lineage_mermaid=result.lineage_mermaid,
        traces={"llm_calls": 3, "cache_hit_rate": 0.5, "tokens_in_total": 900, "tokens_in_p50": 300,
                "tokens_in_p95": 400, "latency_ms_p50": 12.0, "latency_ms_p95": 30.0},
    )
    markdown = builder.render("markdown")
    html = builder.render("html")
    payload = json.loads(builder.render("json"))
    assert "Executive summary" in markdown
    assert "Data Safety Monitoring Board" in markdown
    assert "<!DOCTYPE html>" in html
    assert payload["provenance"]["sim_run_id"] == "RUN-REPORT"
    assert "DISCLAIMER" not in markdown.upper().replace("**DISCLAIMER.**", "")
    summary = builder.render_text_summary()
    assert "arm" in summary and "dSBP" in summary


def test_derived_silver_tables_are_written_on_every_backend(pipeline) -> None:
    """Adverse events, deviations and screen failures are not Delta-only artefacts."""
    result = pipeline.run(run_id="RUN-DERIVED")
    store = pipeline.store
    assert store.exists("silver/adverse_events")
    events = store.read("silver/adverse_events", run_id="RUN-DERIVED")
    assert not events.empty
    assert set(events["sim_run_id"]) == {"RUN-DERIVED"}
    # The protocol excludes patients, so screen failures must be recorded.
    failures = store.read("silver/screen_failures", run_id="RUN-DERIVED")
    assert not failures.empty
    assert "reason" in failures.columns
    # Deviations only exist when somebody discontinued; the table may be absent.
    if result.analysis.cohort_flow["discontinued"]:
        deviations = store.read("silver/protocol_deviations", run_id="RUN-DERIVED")
        assert not deviations.empty
        assert deviations["reason"].str.len().gt(0).all()


def test_lineage_covers_the_derived_tables(pipeline) -> None:
    result = pipeline.run(run_id="RUN-LIN")
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    graph_nodes = {node["node_id"] for node in manifest["lineage_graph"]["nodes"]}
    assert "silver.patient_states" in graph_nodes
    assert "silver.adverse_events" in graph_nodes
    assert "silver.screen_failures" in graph_nodes


def test_llm_raw_traces_reach_bronze(config, protocol) -> None:
    """Persona traces are queryable next to the cohort, not only in a JSONL file."""
    store = MemoryStore(config.storage)
    config.llm_mode = "all"  # guarantee narration and therefore spans
    config.tracking.enabled = True  # tracing is part of the tracking subsystem
    config.tracking.trace_path = str(Path(config.output_dir) / "traces" / "llm_traces.jsonl")
    pipeline = TrialSimulationPipeline(config, protocol, store=store)
    pipeline.run(run_id="RUN-TRACES")
    traces = store.read("bronze/llm_raw_traces", run_id="RUN-TRACES")
    assert not traces.empty
    assert {"trace_id", "span_id", "name", "kind", "tokens_in"} <= set(traces.columns)
    assert (traces["kind"] == "LLM").any()
