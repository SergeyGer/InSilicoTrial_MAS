"""Governance, lineage and tracking tests."""

from __future__ import annotations

import json

import pytest

from insilico_trial_mas.config import TrackingConfig, load_config
from insilico_trial_mas.governance.lineage import LineageTracker
from insilico_trial_mas.governance.namespace import LAYER_TABLES, UnityCatalogNamespace
from insilico_trial_mas.mlflow_tracking.tracing import Span, TraceRecorder, TraceStore
from insilico_trial_mas.mlflow_tracking.tracker import MlflowTracker, NullTracker, resolve_tracking_uri


def test_namespace_builds_three_part_names() -> None:
    config = load_config(None, {"storage": {"catalog": "trial_simulations_prod", "silver_schema": "silver"}})
    namespace = UnityCatalogNamespace.from_storage_config(config.storage)
    assert namespace.fqn("silver", "patient_states") == "trial_simulations_prod.silver.patient_states"
    assert namespace.logical("gold", "arm_summaries") == "gold/arm_summaries"
    assert "silver" in namespace.schema_name("silver")


def test_namespace_rejects_unknown_layers() -> None:
    namespace = UnityCatalogNamespace.from_storage_config(load_config(None).storage)
    with pytest.raises(Exception, match="unknown medallion layer"):
        namespace.schema_name("platinum")


def test_namespace_ddl_and_grants_are_complete() -> None:
    namespace = UnityCatalogNamespace.from_storage_config(load_config(None).storage)
    ddl = namespace.ddl_statements()
    assert any("CREATE CATALOG" in statement for statement in ddl)
    assert sum("CREATE SCHEMA" in statement for statement in ddl) == 3
    grants = namespace.grants_summary()
    principals = {grant["principal"] for grant in grants}
    assert "patient_agent_service_principal" in principals
    assert "biostatistician_agent_service_principal" in principals
    assert all(grant["privileges"] for grant in grants)


def test_declared_tables_match_the_storage_layer() -> None:
    namespace = UnityCatalogNamespace.from_storage_config(load_config(None).storage)
    fqns = {table.fqn for table in namespace.all_tables()}
    assert "trial_simulations_prod.silver.patient_states" in fqns
    assert "trial_simulations_prod.gold.arm_summaries" in fqns
    assert set(LAYER_TABLES) == {"bronze", "silver", "gold"}


def test_external_path_requires_root_uri() -> None:
    namespace = UnityCatalogNamespace.from_storage_config(load_config(None).storage)
    with pytest.raises(Exception, match="root_uri"):
        namespace.external_path("silver", "patient_states")
    configured = UnityCatalogNamespace.from_storage_config(
        load_config(None, {"storage": {"root_uri": "s3://bucket/lake"}}).storage
    )
    assert configured.external_path("silver", "patient_states") == "s3://bucket/lake/silver/patient_states"


def test_lineage_graph_navigation() -> None:
    tracker = LineageTracker("RUN-1", created_at="2024-01-01T00:00:00+00:00")
    tracker.add_node("bronze.synthetic_cohort", "table")
    tracker.add_node("silver.patient_states", "table")
    tracker.add_node("gold.arm_summaries", "table")
    tracker.add_node("report", "report")
    tracker.add_edge("bronze.synthetic_cohort", "silver.patient_states", agent="engine")
    tracker.add_edge("silver.patient_states", "gold.arm_summaries", agent="biostatistician")
    tracker.add_edge("gold.arm_summaries", "report", agent="biostatistician")
    assert tracker.upstream("gold.arm_summaries") == ["bronze.synthetic_cohort", "silver.patient_states"]
    assert tracker.downstream("bronze.synthetic_cohort") == ["gold.arm_summaries", "report", "silver.patient_states"]
    summary = tracker.summary()
    assert summary["nodes"] == 4 and summary["edges"] == 3
    assert summary["kinds"]["table"] == 3
    mermaid = tracker.to_mermaid()
    assert mermaid.startswith("flowchart LR")
    frame = tracker.to_frame()
    assert list(frame.columns) == [
        "source",
        "target",
        "relation",
        "agent",
        "run_id",
        "details_json",
        "created_at",
    ]
    assert json.dumps(tracker.to_dict())


def test_trace_recorder_writes_hierarchical_spans(tmp_path) -> None:
    path = tmp_path / "traces.jsonl"
    recorder = TraceRecorder(path, run_id="RUN-1")
    recorder.record_llm_call(
        patient_id="PT-1",
        epoch=2,
        provider="offline",
        model="offline-heuristic-v1",
        tokens_in=320,
        tokens_out=90,
        duration_ms=12.5,
        prompt_hash="abc",
    )
    recorder.record_llm_call(
        patient_id="PT-1",
        epoch=3,
        provider="offline",
        model="offline-heuristic-v1",
        tokens_in=330,
        tokens_out=95,
        duration_ms=15.0,
        cache_hit=True,
        prompt_hash="def",
    )
    summary = recorder.summary()
    assert summary["llm_calls"] == 2
    assert summary["cache_hits"] == 1
    assert summary["tokens_in_total"] == 650
    assert summary["latency_ms_p95"] >= summary["latency_ms_mean"] * 0.5

    store = TraceStore(path)
    assert store.summary()["traces"] == 1
    trace_id = store.trace_ids()[0]
    tree = store.tree(trace_id)
    assert tree["roots"], "a trace must have a root span"
    kinds = json.dumps(tree)
    assert '"AGENT"' in kinds
    assert '"LLM"' in kinds
    assert len(list(store.as_rows())) == 3  # agent span + 2 LLM spans


def test_trace_recorder_is_disabled_cleanly(tmp_path) -> None:
    recorder = TraceRecorder(tmp_path / "x.jsonl", run_id="R", enabled=False)
    recorder.record_span(Span(trace_id="t", span_id="s", parent_id="", name="n"))
    assert recorder.summary()["spans"] == 0
    assert not (tmp_path / "x.jsonl").exists()


def test_trace_store_handles_missing_file(tmp_path) -> None:
    store = TraceStore(tmp_path / "missing.jsonl")
    assert store.spans() == []
    assert store.summary()["llm_calls"] == 0
    assert store.trace_ids() == []


def test_tracking_uri_resolution(tmp_path, monkeypatch) -> None:
    config = load_config(None, {"output_dir": str(tmp_path), "tracking": {"enabled": True}})
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    assert resolve_tracking_uri(config).startswith("file://")
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.cloud.databricks.com")
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-test")
    assert resolve_tracking_uri(config) == "databricks"


def test_null_tracker_records_calls() -> None:
    tracker = NullTracker()
    with tracker.start_run(run_name="x"):
        tracker.log_params({"a": 1})
        tracker.log_metrics({"b": 2.0})
        tracker.register_model("path", "name")
    kinds = [call[0] for call in tracker.calls]
    assert kinds == ["start_run", "log_params", "log_metrics", "register_model"]


def test_mlflow_tracker_degrades_without_mlflow(tmp_path, monkeypatch) -> None:
    """Tracking must never be able to fail a simulation."""
    tracker = MlflowTracker(TrackingConfig(enabled=True, tracking_uri=str(tmp_path / "nope")))
    with tracker.start_run(run_name="degraded"):
        tracker.log_params({"a": 1})
        tracker.log_metrics({"b": 1.5})
        tracker.log_artifact(tmp_path / "missing.json")
    # Either MLflow is present and working, or the tracker degraded gracefully.
    assert isinstance(tracker.degraded_reason, str)


def test_mlflow_tracker_disabled_is_a_no_op() -> None:
    tracker = MlflowTracker(TrackingConfig(enabled=False))
    assert tracker.available is False
    with tracker.start_run():
        tracker.log_params({"a": 1})
    assert tracker.degraded_reason == ""
