"""Command line interface for InSilicoTrial MAS.

Every capability of the platform is reachable from the CLI so that Databricks
jobs, CI pipelines and a developer laptop all drive the same code path::

    insilico-trial env-check
    insilico-trial validate-protocol --config conf/simulation_local.yaml
    insilico-trial generate-cohort --patients 2000 --out artifacts/cohort.parquet
    insilico-trial simulate --config conf/simulation_local.yaml --patients 5000
    insilico-trial report --run artifacts/<RUN-ID>
    insilico-trial time-travel --table silver/patient_states --history
    insilico-trial time-travel --table silver/patient_states --version 1 --where "sbp > 140"
    insilico-trial lineage --run artifacts/<RUN-ID> --format mermaid
    insilico-trial traces --tree
    insilico-trial train-physiology --rows 20000 --register
    insilico-trial dashboard --run artifacts/<RUN-ID> --open
    insilico-trial studio --config conf/simulation_local.yaml
    insilico-trial demo
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .config import SimulationConfig, load_config
from .errors import InSilicoTrialError
from .logging_utils import configure_logging, get_logger

logger = get_logger("cli")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="insilico-trial",
        description="InSilicoTrial MAS - distributed multi-agent in-silico clinical trial simulation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Documentation: docs/ARCHITECTURE.md, docs/RUNBOOK_DATABRICKS_AWS.md",
    )
    parser.add_argument("--version", action="store_true", help="print the package version and exit")
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING, ERROR (default: INFO)")
    parser.add_argument("--log-json", action="store_true", help="emit JSON logs (default in workers)")

    subparsers = parser.add_subparsers(dest="command")

    # -- env-check ---------------------------------------------------------
    env = subparsers.add_parser("env-check", help="inspect the runtime and recommend an execution engine")
    env.add_argument("--config", help="optional simulation configuration YAML")
    env.add_argument("--json", action="store_true", help="print machine-readable JSON")

    # -- validate-protocol -------------------------------------------------
    validate = subparsers.add_parser("validate-protocol", help="validate a trial protocol definition")
    validate.add_argument("--protocol", help="protocol YAML (defaults to the one in --config)")
    validate.add_argument("--config", default="conf/simulation_local.yaml")
    validate.add_argument("--json", action="store_true")

    # -- generate-cohort ---------------------------------------------------
    cohort = subparsers.add_parser("generate-cohort", help="generate a synthetic screening cohort")
    cohort.add_argument("--config", default="conf/simulation_local.yaml")
    cohort.add_argument("--protocol")
    cohort.add_argument("--patients", type=int, help="override n_patients")
    cohort.add_argument("--cohorts", type=int, help="override n_cohorts")
    cohort.add_argument("--seed", type=int)
    cohort.add_argument("--out", default="artifacts/cohort.parquet")
    cohort.add_argument("--json", action="store_true")

    # -- simulate ----------------------------------------------------------
    simulate = subparsers.add_parser("simulate", help="run a full trial simulation and produce a report")
    _add_simulation_options(simulate)
    simulate.add_argument("--run-id", help="reuse an explicit run id (enables run-scoped re-runs)")
    simulate.add_argument("--offline", action="store_true", help="force the deterministic offline LLM provider")
    simulate.add_argument("--json", action="store_true", help="print the run summary as JSON")

    # -- report ------------------------------------------------------------
    report = subparsers.add_parser("report", help="rebuild the report from a finished run directory")
    report.add_argument("--run", required=True, help="run directory (contains silver_observations.parquet)")
    report.add_argument("--config", default="conf/simulation_local.yaml")
    report.add_argument("--format", action="append", choices=["markdown", "html", "json"], dest="formats")
    report.add_argument("--json", action="store_true")

    # -- time-travel -------------------------------------------------------
    travel = subparsers.add_parser("time-travel", help="query the Silver table as of an earlier version")
    travel.add_argument("--config", default="conf/simulation_local.yaml")
    travel.add_argument("--table", default="silver/patient_states")
    travel.add_argument("--version", type=int, help="read this version (VERSION AS OF)")
    travel.add_argument("--run-id", help="restrict to the versions written by one run")
    travel.add_argument("--where", help="pandas query filter, e.g. \"sbp > 140\"")
    travel.add_argument("--columns", help="comma-separated projection")
    travel.add_argument("--limit", type=int, default=10)
    travel.add_argument("--history", action="store_true", help="show the version history instead of data")
    travel.add_argument("--json", action="store_true")

    # -- lineage -----------------------------------------------------------
    lineage = subparsers.add_parser("lineage", help="inspect the lineage graph of a run")
    lineage.add_argument("--run", required=True, help="run directory containing run_manifest.json")
    lineage.add_argument("--format", choices=["summary", "mermaid", "json", "edges"], default="summary")
    lineage.add_argument("--upstream", help="show the ancestors of this node")
    lineage.add_argument("--json", action="store_true")

    # -- traces ------------------------------------------------------------
    traces = subparsers.add_parser("traces", help="analyse LLM traces (tokens, latency, hierarchies)")
    traces.add_argument("--path", default="artifacts/traces/llm_traces.jsonl")
    traces.add_argument("--run-id")
    traces.add_argument("--tree", help="render the span hierarchy of this trace id (or 'first')")
    traces.add_argument("--json", action="store_true")

    # -- train-physiology --------------------------------------------------
    train = subparsers.add_parser("train-physiology", help="train and persist the physiology residual model")
    train.add_argument("--config", default="conf/simulation_local.yaml")
    train.add_argument("--protocol")
    train.add_argument("--rows", type=int, help="number of synthetic historical rows")
    train.add_argument("--output", help="artifact path (defaults to ml.model_path)")
    train.add_argument("--register", action="store_true", help="register the model in the MLflow registry")
    train.add_argument("--json", action="store_true")

    # -- dashboard ---------------------------------------------------------
    dashboard = subparsers.add_parser(
        "dashboard", help="build the interactive HTML dashboard for a finished run"
    )
    dashboard.add_argument("--run", required=True, help="run directory (contains run_manifest.json)")
    dashboard.add_argument("--out", help="output file (defaults to <run>/report/dashboard.html)")
    dashboard.add_argument("--open", action="store_true", dest="open_browser", help="open it in the browser")
    dashboard.add_argument("--json", action="store_true")

    # -- studio ------------------------------------------------------------
    studio = subparsers.add_parser(
        "studio", help="serve the live Studio UI that launches and monitors simulations"
    )
    studio.add_argument("--config", default="conf/simulation_local.yaml", help="base simulation configuration")
    studio.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback only)")
    studio.add_argument("--port", type=int, default=8765)
    studio.add_argument("--no-browser", action="store_true", help="do not open a browser window")

    # -- demo --------------------------------------------------------------
    demo = subparsers.add_parser("demo", help="run a small end-to-end simulation with the offline LLM")
    demo.add_argument("--patients", type=int, default=300)
    demo.add_argument("--epochs", type=int, default=6)
    demo.add_argument("--output-dir", default="artifacts/demo")
    demo.add_argument("--engine", default="auto", choices=["auto", "sequential", "local", "spark"])
    demo.add_argument("--json", action="store_true")

    return parser


def _add_simulation_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default="conf/simulation_local.yaml", help="simulation configuration YAML")
    parser.add_argument("--protocol", help="trial protocol YAML (overrides the config value)")
    parser.add_argument("--patients", type=int, help="override n_patients")
    parser.add_argument("--cohorts", type=int, help="override n_cohorts")
    parser.add_argument("--epochs", type=int, help="override the number of dosing epochs")
    parser.add_argument("--seed", type=int, help="override the master seed")
    parser.add_argument("--engine", choices=["auto", "sequential", "local", "spark"], help="execution backend")
    parser.add_argument("--llm-mode", choices=["off", "sample", "triggered", "all"], help="when personas narrate")
    parser.add_argument("--llm-provider", choices=["offline", "mock", "bedrock", "openai", "langchain"], help="LLM provider")
    parser.add_argument("--llm-sample-rate", type=float, help="share of epochs narrated in sample mode")
    parser.add_argument("--storage", choices=["local", "delta", "memory"], help="storage backend")
    parser.add_argument("--output-dir", help="artefact directory (default from the config)")


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def _load_config(args: argparse.Namespace, *, require_file: bool = True) -> SimulationConfig:
    """Load configuration with CLI overrides applied on top."""
    overrides: dict[str, Any] = {}
    mapping = {
        "patients": ("n_patients", None),
        "cohorts": ("n_cohorts", None),
        "epochs": ("epochs", None),
        "seed": ("seed", None),
        "llm_mode": ("llm_mode", None),
        "llm_sample_rate": ("llm_sample_rate", None),
        "output_dir": ("output_dir", None),
    }
    for attribute, (field_name, _) in mapping.items():
        value = getattr(args, attribute, None)
        if value is not None:
            overrides[field_name] = value
    if getattr(args, "engine", None):
        overrides.setdefault("engine", {})["backend"] = args.engine
    if getattr(args, "llm_provider", None):
        overrides.setdefault("llm", {})["provider"] = args.llm_provider
    if getattr(args, "storage", None):
        overrides.setdefault("storage", {})["backend"] = args.storage
    if getattr(args, "protocol", None):
        overrides["protocol_path"] = args.protocol
    config_path = getattr(args, "config", None)
    if config_path and Path(config_path).exists():
        return load_config(config_path, overrides)
    if config_path and require_file:
        # A missing default config file is tolerated: fall back to built-in defaults.
        logger.warning(f"configuration file {config_path} not found; using built-in defaults")
    return load_config(None, overrides)


def cmd_env_check(args: argparse.Namespace) -> int:
    from .engine.checklist import inspect_environment

    config = _load_config(args, require_file=False)
    report = inspect_environment(config)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(report.render())
    return EXIT_OK


def cmd_validate_protocol(args: argparse.Namespace) -> int:
    from .pipeline import load_protocol

    config = _load_config(args, require_file=False)
    protocol_path = args.protocol or config.protocol_path
    protocol = load_protocol(protocol_path)
    from .agents.base import AgentContext
    from .agents.protocol_agent import ProtocolAgent

    agent = ProtocolAgent(
        AgentContext(run_id="validation", protocol=protocol, config=config, seed=config.seed, total_epochs=protocol.epochs)
    )
    warnings = agent.validate()
    arm_ids: list[str] = [arm.arm_id for arm in protocol.arms]
    endpoint_names: list[str] = [endpoint.name for endpoint in protocol.endpoints]
    rule_ids: list[str] = [rule.rule_id for rule in protocol.stopping_rules]
    payload: dict[str, Any] = {
        "protocol_id": protocol.protocol_id,
        "path": protocol_path,
        "digest": protocol.digest(),
        "arms": arm_ids,
        "epochs": protocol.epochs,
        "endpoints": endpoint_names,
        "stopping_rules": rule_ids,
        "warnings": warnings,
        "valid": True,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"protocol {protocol.protocol_id} is valid (digest {payload['digest']})")
        print(f"  arms        : {', '.join(arm_ids)}")
        print(f"  epochs      : {protocol.epochs} x {protocol.epoch_duration_hours} h")
        print(f"  endpoints   : {', '.join(endpoint_names)}")
        print(f"  stopping    : {', '.join(rule_ids) or 'none'}")
        for warning in warnings:
            print(f"  warning     : {warning}")
    return EXIT_OK


def cmd_generate_cohort(args: argparse.Namespace) -> int:
    from .agents.base import AgentContext
    from .agents.protocol_agent import ProtocolAgent
    from .cohort.generator import CohortGenerator, cohort_summary
    from .cohort.serialization import profiles_to_frame
    from .pipeline import load_protocol

    config = _load_config(args, require_file=False)
    protocol = load_protocol(config.protocol_path)
    cohort = CohortGenerator(protocol, config).generate()
    agent = ProtocolAgent(
        AgentContext(run_id="cohort-generation", protocol=protocol, config=config, seed=config.seed, total_epochs=protocol.epochs)
    )
    screening, allocation = agent.plan(cohort)
    frame = profiles_to_frame(screening.enrolled, allocation.arm_by_patient)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix == ".csv":
        frame.to_csv(out, index=False)
    else:
        frame.to_parquet(out, index=False)
    summary: dict[str, Any] = {
        "output": str(out),
        "screening": screening.summary(),
        "allocation": allocation.arm_counts(),
        "cohort": cohort_summary(screening.enrolled),
    }
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        screening_summary = summary["screening"]
        cohort_stats = summary["cohort"]
        print(f"wrote {len(frame)} enrolled patients to {out}")
        print(f"  screened        : {screening_summary['screened']}")
        print(f"  screen failures : {screening_summary['screen_failures']} {screening_summary['failure_reasons']}")
        print(f"  allocation      : {summary['allocation']}")
        print(f"  cohort digest   : {cohort_stats['digest']}")
    return EXIT_OK


def cmd_simulate(args: argparse.Namespace) -> int:
    from .pipeline import TrialSimulationPipeline, load_protocol

    config = _load_config(args)
    protocol = load_protocol(args.protocol or config.protocol_path)
    pipeline = TrialSimulationPipeline(config, protocol)
    result = pipeline.run(run_id=args.run_id, force_offline=args.offline)
    if args.json:
        print(json.dumps(result.summary(), indent=2, default=str))
    else:
        from .reporting.report import ReportBuilder

        builder = ReportBuilder(
            result.protocol,
            result.provenance,
            result.analysis,
            lineage=result.lineage,
            lineage_mermaid=result.lineage_mermaid,
        )
        print(builder.render_text_summary())
        print("")
        print(f"run id      : {result.run_id}")
        print(f"artefacts   : {result.output_dir}")
        print(f"silver      : {result.silver_location} (version {result.silver_version})")
        if result.report.markdown:
            print(f"report      : {result.report.markdown}")
        if result.report.html:
            print(f"html report : {result.report.html}")
        if result.dashboard_path:
            print(f"dashboard   : {result.dashboard_path}")
        print(f"manifest    : {result.manifest_path}")
        if result.cdisc:
            print(f"cdisc       : {result.cdisc.get('directory', '')}")
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    """Rebuild a report from a stored Silver frame and the run manifest."""
    import pandas as pd

    from .agents.base import AgentContext
    from .agents.biostatistician_agent import BiostatisticianAgent
    from .pipeline import load_protocol
    from .reporting.report import ReportBuilder
    from .schemas import SimulationProvenance

    run_dir = Path(args.run)
    silver_path = run_dir / "silver_observations.parquet"
    manifest_path = run_dir / "run_manifest.json"
    if not silver_path.exists():
        raise InSilicoTrialError(f"{silver_path} not found; point --run at a finished run directory")
    config = _load_config(args, require_file=False)
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    protocol = load_protocol(config.protocol_path) if not manifest else type(
        load_protocol(config.protocol_path)
    ).model_validate(manifest["protocol"])
    frame = pd.read_parquet(silver_path)
    provenance = SimulationProvenance(**manifest["provenance"]) if manifest.get("provenance") else SimulationProvenance(
        sim_run_id=run_dir.name,
        protocol_id=protocol.protocol_id,
        protocol_digest=protocol.digest(),
        protocol_version=protocol.version,
        package_version="unknown",
        git_revision="unknown",
        engine_backend="unknown",
    )
    context = AgentContext(
        run_id=provenance.sim_run_id,
        protocol=protocol,
        config=config,
        seed=provenance.seed or config.seed,
        total_epochs=protocol.epochs,
    )
    analysis = BiostatisticianAgent(context).analyze(frame)
    lineage_summary = manifest.get("lineage") or {
        "run_id": provenance.sim_run_id,
        "nodes": 0,
        "edges": 0,
        "agents": [],
    }
    builder = ReportBuilder(
        protocol,
        provenance,
        analysis,
        lineage=lineage_summary,
        lineage_mermaid=manifest.get("lineage_mermaid", ""),
        traces=manifest.get("traces", {}),
    )
    formats = args.formats or ["markdown", "html", "json"]
    artifacts = builder.write(run_dir / "report", formats=formats)
    if args.json:
        print(json.dumps({"report": artifacts.as_dict(), "overview": analysis.overview}, indent=2, default=str))
    else:
        print(builder.render_text_summary())
        print("")
        for fmt in formats:
            path = {"markdown": artifacts.markdown, "html": artifacts.html, "json": artifacts.payload}[fmt]
            print(f"{fmt:<9}: {path}")
    return EXIT_OK


def cmd_time_travel(args: argparse.Namespace) -> int:
    import pandas as pd

    config = _load_config(args, require_file=False)
    from .storage.factory import create_store

    store = create_store(config)
    if args.history:
        history = store.history(args.table, limit=args.limit if args.limit else 20)
        if args.json:
            print(json.dumps(history, indent=2, default=str))
        else:
            print(f"version history of {args.table} ({store.table_location(args.table)}):")
            print(f"{'version':>8}  {'timestamp':<26}  {'operation':<9}  {'rows':>8}  run_id")
            for entry in history:
                print(
                    f"{entry.get('version', -1):>8}  {str(entry.get('timestamp', ''))[:26]:<26}  "
                    f"{entry.get('operation', 'WRITE')!s:<9}  {entry.get('rows', 0):>8}  {entry.get('run_id', '')}"
                )
        return EXIT_OK

    frame = store.read(args.table, version=args.version, run_id=args.run_id)
    if frame.empty:
        print(f"no data in {args.table} (version={args.version}, run_id={args.run_id})")
        return EXIT_OK
    if args.where:
        frame = frame.query(args.where)
    if args.columns:
        columns = [c.strip() for c in args.columns.split(",") if c.strip()]
        frame = frame[[c for c in columns if c in frame.columns]]
    total = len(frame)
    preview = frame.head(args.limit)
    if args.json:
        print(preview.to_json(orient="records", indent=2))
    else:
        version_label = f"version {args.version}" if args.version is not None else "latest"
        print(f"{args.table} @ {version_label}: {total} rows match" + (f" ({args.where})" if args.where else ""))
        if total:
            with pd.option_context("display.max_columns", 40, "display.width", 200):
                print(preview.to_string(index=False))
    return EXIT_OK


def cmd_lineage(args: argparse.Namespace) -> int:
    run_dir = Path(args.run)
    manifest_path = run_dir / "run_manifest.json"
    if not manifest_path.exists():
        raise InSilicoTrialError(f"{manifest_path} not found; point --run at a finished run directory")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    lineage = manifest.get("lineage", {})
    mermaid = manifest.get("lineage_mermaid", "")
    if args.format == "mermaid":
        print(mermaid)
        return EXIT_OK
    if args.format == "json":
        print(json.dumps({"lineage": lineage, "mermaid": mermaid}, indent=2, default=str))
        return EXIT_OK
    if args.format == "edges":
        edges = pd_edges(manifest)
        print(edges.to_string(index=False) if not edges.empty else "no lineage edges recorded")
        return EXIT_OK
    graph = manifest.get("lineage_graph", {})
    print(f"lineage for run {manifest.get('run_id', run_dir.name)}")
    print(f"  nodes   : {lineage.get('nodes', 0)}")
    print(f"  edges   : {lineage.get('edges', 0)}")
    print(f"  agents  : {', '.join(lineage.get('agents', []))}")
    print(f"  kinds   : {lineage.get('kinds', {})}")
    if args.upstream:
        incoming: dict[str, list[str]] = {}
        for edge in graph.get("edges", []):
            incoming.setdefault(str(edge.get("target")), []).append(str(edge.get("source")))
        seen: set[str] = set()
        queue = list(incoming.get(args.upstream, []))
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            queue.extend(incoming.get(current, []))
        print(f"  upstream of {args.upstream}: {', '.join(sorted(seen)) or 'none'}")
    print("")
    print(mermaid)
    return EXIT_OK


def pd_edges(manifest: dict[str, Any]):
    """Lineage edges of a run as a DataFrame (handles summary-only manifests)."""
    import pandas as pd

    edges = manifest.get("lineage_graph", {}).get("edges")
    if edges is None:
        candidate = manifest.get("lineage", {}).get("edges", [])
        edges = candidate if isinstance(candidate, list) else []
    return pd.DataFrame(
        [
            {
                "source": edge.get("source"),
                "target": edge.get("target"),
                "relation": edge.get("relation"),
                "agent": edge.get("agent"),
            }
            for edge in edges
        ]
    )


def cmd_traces(args: argparse.Namespace) -> int:
    from .mlflow_tracking.tracing import TraceStore

    store = TraceStore(args.path)
    summary = store.summary(run_id=args.run_id)
    if args.tree:
        trace_ids = store.trace_ids(limit=1) if args.tree == "first" else [args.tree]
        trees = [store.tree(trace_id) for trace_id in trace_ids]
        if args.json:
            print(json.dumps({"summary": summary, "trees": trees}, indent=2, default=str))
        else:
            print(json.dumps(summary, indent=2))
            for tree in trees:
                print("")
                _print_tree(tree["roots"])
        return EXIT_OK
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(f"trace file : {args.path}")
        if not summary.get("llm_calls"):
            print("no LLM spans recorded (llm_mode='off', or no persona calls were triggered)")
            return EXIT_OK
        for key, value in summary.items():
            print(f"  {key:<18}: {value}")
    return EXIT_OK


def _print_tree(roots: list[dict[str, Any]], depth: int = 0) -> None:
    for node in roots:
        detail = f"{node['duration_ms']:.0f} ms" if node.get("duration_ms") else ""
        tokens = f"tokens in/out {node.get('tokens_in', 0)}/{node.get('tokens_out', 0)}" if node.get("tokens_in") else ""
        print(f"{'  ' * depth}- {node['name']} [{node['kind']}] {detail} {tokens}".rstrip())
        _print_tree(node.get("children", []), depth + 1)


def cmd_train_physiology(args: argparse.Namespace) -> int:
    from .ml.training import train_and_register, training_report
    from .pipeline import load_protocol

    config = _load_config(args, require_file=False)
    protocol = load_protocol(args.protocol or config.protocol_path)
    if args.output:
        config.ml.model_path = args.output
    if args.register:
        config.tracking.register_model = True
    result = train_and_register(protocol, config, n_rows=args.rows, register=args.register or None)
    payload = training_report(result)
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(f"physiology model trained: {payload['artifact_path']}")
        print(f"  version      : {payload['model_version']}")
        print(f"  digest       : {payload['model_digest']}")
        print(f"  train/holdout: {payload['n_train']}/{payload['n_holdout']} rows")
        for target, metrics in sorted(payload["metrics"].items()):
            if target.startswith("r2_"):
                print(f"  {target:<28}: {metrics:+.4f}")
    return EXIT_OK


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Build the static dashboard for a finished run."""
    import webbrowser

    from .ui.dashboard import write_dashboard
    from .ui.data import resolve_run_dir

    try:
        run_dir = resolve_run_dir(args.run)
    except FileNotFoundError as exc:
        raise InSilicoTrialError(str(exc)) from exc
    path = write_dashboard(run_dir, args.out)
    if args.open_browser:
        webbrowser.open(path.resolve().as_uri())
    if args.json:
        print(json.dumps({"dashboard": str(path), "bytes": path.stat().st_size}, indent=2))
    else:
        print(f"dashboard : {path}")
        print(f"size      : {path.stat().st_size / 1024:.0f} KB (self-contained, no network required)")
        print("open      : file://" + str(path.resolve()))
    return EXIT_OK


def cmd_studio(args: argparse.Namespace) -> int:
    """Serve the live Studio UI (stdlib HTTP server, loopback by default)."""
    from .ui.studio import run_studio

    return run_studio(
        config_path=args.config,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )


def cmd_demo(args: argparse.Namespace) -> int:
    """Small end-to-end run: cohort -> simulation -> report, offline LLM only."""
    from .pipeline import TrialSimulationPipeline, load_protocol

    config = load_config(None, {
        "protocol_path": "conf/trial_protocol_demo.yaml",
        "n_patients": args.patients,
        "n_cohorts": max(4, args.patients // 50),
        "epochs": args.epochs,
        "output_dir": args.output_dir,
        "llm_mode": "triggered",
        "llm_sample_rate": 0.05,
        "export_cdisc": True,
        "engine": {"backend": args.engine, "batch_size": 128},
        "llm": {"provider": "offline", "cache_path": f"{args.output_dir}/llm_cache.jsonl"},
        "ml": {"backend": "mechanistic"},
        "tracking": {"enabled": False, "trace_path": f"{args.output_dir}/traces/llm_traces.jsonl"},
    })
    protocol = load_protocol(config.protocol_path)
    result = TrialSimulationPipeline(config, protocol).run(force_offline=True)
    if args.json:
        print(json.dumps(result.summary(), indent=2, default=str))
    else:
        from .reporting.report import ReportBuilder

        builder = ReportBuilder(result.protocol, result.provenance, result.analysis, lineage_mermaid=result.lineage_mermaid)
        print(builder.render_text_summary())
        print("")
        print(f"run        : {result.run_id}")
        print(f"artefacts  : {result.output_dir}")
        print(f"report     : {result.report.markdown}")
        print(f"html       : {result.report.html}")
    return EXIT_OK


HANDLERS = {
    "env-check": cmd_env_check,
    "validate-protocol": cmd_validate_protocol,
    "generate-cohort": cmd_generate_cohort,
    "simulate": cmd_simulate,
    "report": cmd_report,
    "time-travel": cmd_time_travel,
    "lineage": cmd_lineage,
    "traces": cmd_traces,
    "train-physiology": cmd_train_physiology,
    "dashboard": cmd_dashboard,
    "studio": cmd_studio,
    "demo": cmd_demo,
}


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "version", False) and not args.command:
        from .version import __version__

        print(f"insilico-trial-mas {__version__}")
        return EXIT_OK
    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    configure_logging(args.log_level, json_logs=args.log_json or None)
    handler = HANDLERS[args.command]
    try:
        return handler(args)
    except InSilicoTrialError as exc:
        logger.error(f"{type(exc).__name__}: {exc}")
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover - interactive use
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        logger.exception("unhandled error")
        print(f"unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
