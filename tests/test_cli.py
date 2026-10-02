"""CLI tests: every documented command must run and exit with a sane status."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from insilico_trial_mas.cli import EXIT_ERROR, EXIT_OK, EXIT_USAGE, main


def test_version_flag(capsys) -> None:
    assert main(["--version"]) == EXIT_OK
    assert "insilico-trial-mas" in capsys.readouterr().out


def test_no_command_prints_help(capsys) -> None:
    assert main([]) == EXIT_USAGE
    assert "usage" in capsys.readouterr().out.lower()


def test_env_check_json(capsys) -> None:
    assert main(["env-check", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["cpu_count"] >= 1
    assert payload["recommended_backend"] in {"sequential", "local", "spark"}
    assert "pyspark_available" in payload


def test_env_check_human_readable(capsys) -> None:
    assert main(["env-check"]) == EXIT_OK
    output = capsys.readouterr().out
    assert "environment checklist" in output
    assert "engine" in output


def test_validate_protocol(capsys) -> None:
    assert main(["validate-protocol", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["protocol_id"] == "INS-HTN-201"
    assert payload["arms"]


def test_validate_protocol_reports_broken_file(capsys, tmp_path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("protocol_id: X\narms: []\n", encoding="utf-8")
    assert main(["validate-protocol", "--protocol", str(broken)]) == EXIT_ERROR
    assert "error" in capsys.readouterr().err.lower()


def test_generate_cohort(tmp_path, capsys) -> None:
    out = tmp_path / "cohort.parquet"
    code = main(
        [
            "generate-cohort",
            "--patients",
            "80",
            "--cohorts",
            "2",
            "--out",
            str(out),
            "--json",
        ]
    )
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["screening"]["screened"] == 80
    assert out.exists()
    import pandas as pd

    frame = pd.read_parquet(out)
    assert "arm_id" in frame.columns


def test_generate_cohort_csv(tmp_path, capsys) -> None:
    out = tmp_path / "cohort.csv"
    assert main(["generate-cohort", "--patients", "50", "--cohorts", "1", "--out", str(out)]) == EXIT_OK
    capsys.readouterr()
    assert out.read_text(encoding="utf-8").splitlines()[0].startswith("patient_id")


def test_simulate_time_travel_lineage_and_report(tmp_path, capsys) -> None:
    output_dir = tmp_path / "artifacts"
    code = main(
        [
            "simulate",
            "--patients",
            "120",
            "--cohorts",
            "3",
            "--epochs",
            "3",
            "--engine",
            "sequential",
            "--llm-mode",
            "triggered",
            "--output-dir",
            str(output_dir),
            "--storage",
            "local",
            "--json",
        ]
    )
    assert code == EXIT_OK, capsys.readouterr().err
    summary = json.loads(capsys.readouterr().out)
    run_id = summary["run_id"]
    assert summary["rows"] == summary["patients"] * 4

    # time travel: history + a version-scoped read
    assert main(["time-travel", "--table", "silver/patient_states", "--history", "--json"]) == EXIT_OK
    history = json.loads(capsys.readouterr().out)
    assert history and history[0]["version"] >= 1

    assert (
        main(
            [
                "time-travel",
                "--table",
                "silver/patient_states",
                "--version",
                "1",
                "--where",
                "sbp > 0",
                "--columns",
                "patient_id,epoch,sbp,arm_id",
                "--limit",
                "3",
            ]
        )
        == EXIT_OK
    )
    assert "rows match" in capsys.readouterr().out

    run_dir = output_dir / run_id
    assert main(["lineage", "--run", str(run_dir), "--format", "mermaid"]) == EXIT_OK
    assert capsys.readouterr().out.startswith("flowchart")
    assert main(["lineage", "--run", str(run_dir), "--format", "edges"]) == EXIT_OK
    assert "target" in capsys.readouterr().out

    assert main(["report", "--run", str(run_dir), "--format", "markdown"]) == EXIT_OK
    assert (run_dir / "report" / "trial_report.md").exists()
    capsys.readouterr()


def test_time_travel_on_missing_table(tmp_path, capsys) -> None:
    code = main(["time-travel", "--table", "silver/unknown", "--config", "conf/simulation_local.yaml"])
    assert code == EXIT_OK
    assert "no data" in capsys.readouterr().out


def test_lineage_requires_a_run_directory(tmp_path, capsys) -> None:
    code = main(["lineage", "--run", str(tmp_path / "nope")])
    assert code == EXIT_ERROR
    assert "not found" in capsys.readouterr().err


def test_train_physiology_writes_an_artifact(tmp_path, capsys) -> None:
    model_path = tmp_path / "model.json"
    code = main(
        [
            "train-physiology",
            "--rows",
            "600",
            "--output",
            str(model_path),
            "--json",
        ]
    )
    assert code == EXIT_OK, capsys.readouterr().err
    payload = json.loads(capsys.readouterr().out)
    assert model_path.exists()
    assert payload["n_train"] > 0
    assert any(key.startswith("r2_") for key in payload["metrics"])


def test_traces_command_without_data(tmp_path, capsys) -> None:
    assert main(["traces", "--path", str(tmp_path / "none.jsonl")]) == EXIT_OK
    assert "no LLM spans" in capsys.readouterr().out


def test_demo_end_to_end(tmp_path, capsys) -> None:
    output_dir = tmp_path / "demo"
    code = main(["demo", "--patients", "80", "--epochs", "2", "--output-dir", str(output_dir), "--json"])
    assert code == EXIT_OK, capsys.readouterr().err
    payload = json.loads(capsys.readouterr().out)
    assert payload["patients"] > 0
    assert Path(payload["report"]["markdown"]).exists()
    assert Path(payload["report"]["html"]).exists()


def test_unknown_option_exits_with_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["simulate", "--nonsense"])
    assert excinfo.value.code == 2


def test_dashboard_command_builds_a_self_contained_readout(tmp_path, capsys) -> None:
    output_dir = tmp_path / "artifacts"
    assert (
        main(
            [
                "simulate",
                "--patients",
                "80",
                "--cohorts",
                "2",
                "--epochs",
                "2",
                "--engine",
                "sequential",
                "--llm-mode",
                "off",
                "--output-dir",
                str(output_dir),
                "--json",
            ]
        )
        == EXIT_OK
    )
    summary = json.loads(capsys.readouterr().out)
    run_dir = output_dir / summary["run_id"]

    # The pipeline exports the dashboard automatically.
    assert Path(summary["dashboard"]).exists()

    # The command also accepts the parent directory and picks the newest run.
    assert main(["dashboard", "--run", str(output_dir), "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    dashboard = Path(payload["dashboard"])
    assert dashboard.exists() and payload["bytes"] > 20_000
    html = dashboard.read_text(encoding="utf-8")
    assert 'data-panel="overview"' in html
    assert "Synthetic data" in html

    # Explicit output path.
    target = tmp_path / "board.html"
    assert main(["dashboard", "--run", str(run_dir), "--out", str(target)]) == EXIT_OK
    assert target.exists()


def test_dashboard_command_reports_a_missing_run(tmp_path, capsys) -> None:
    assert main(["dashboard", "--run", str(tmp_path / "nope")]) == EXIT_ERROR
    assert "not found" in capsys.readouterr().err
