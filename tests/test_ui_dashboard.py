"""Tests for the dashboard: chart primitives, payload derivation and HTML output.

The dashboard is a deliverable artefact, so it is tested like one: the SVG must be
well formed, the payload must survive a run directory that is missing optional
files, and the rendered HTML must contain the screens a reviewer expects.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from insilico_trial_mas.ui import charts
from insilico_trial_mas.ui.dashboard import build_dashboard, write_dashboard
from insilico_trial_mas.ui.data import load_dashboard_data

SVG_TAG_RE = re.compile(r"<svg\b[^>]*>.*?</svg>", re.DOTALL)


def _run_dir(tmp_path: Path, config, protocol, *, patients: int = 60) -> Path:
    """Produce a small finished run on disk and return its directory."""
    from insilico_trial_mas.pipeline import TrialSimulationPipeline
    from insilico_trial_mas.storage.factory import MemoryStore

    config.output_dir = str(tmp_path / "artifacts")
    config.export_cdisc = False
    config.report_formats = ["json"]
    pipeline = TrialSimulationPipeline(config, protocol, store=MemoryStore(config.storage))
    result = pipeline.run(run_id="RUN-UI")
    return Path(result.output_dir)


# ---------------------------------------------------------------------------
# Chart primitives
# ---------------------------------------------------------------------------


def test_bar_with_ci_produces_valid_svg() -> None:
    chart = charts.bar_with_ci(
        ["placebo", "low", "high"],
        [-1.0, -5.0, -9.0],
        lower=[-2.0, -6.0, -10.0],
        upper=[0.0, -4.0, -8.0],
        unit=" mmHg",
        title="Primary endpoint",
        description="change from baseline",
    )
    match = SVG_TAG_RE.search(chart.svg)
    assert match, "chart must contain a complete svg element"
    assert "<title>Primary endpoint</title>" in chart.svg
    assert chart.svg.count("<rect") == 3, "one bar per label"
    assert 'role="img"' in chart.svg


def test_bar_with_ci_handles_empty_and_nan_inputs() -> None:
    assert "no data" in charts.bar_with_ci([], [], title="empty").svg
    assert "no data" in charts.bar_with_ci(["a"], [float("nan")], title="nan").svg


def test_forest_plot_draws_points_and_confidence_intervals() -> None:
    rows = [
        {"label": "sbp_change", "group": "low_dose", "group_index": 0, "estimate": -4.2, "low": -5.0, "high": -3.4, "p_value": 0.0001},
        {"label": "sbp_change", "group": "high_dose", "group_index": 1, "estimate": -8.4, "low": -9.3, "high": -7.5, "p_value": 0.00001},
    ]
    chart = charts.forest_plot(rows, unit=" mmHg")
    assert chart.svg.count("<circle") == 2
    assert chart.svg.count('class="ci"') == 2
    assert "null-line" in chart.svg, "a no-effect reference line is mandatory in a forest plot"
    assert "&lt;0.0001" in chart.svg, "p-values are HTML-escaped inside SVG text"


def test_line_with_band_includes_polygon_and_polyline() -> None:
    series = [{"name": "arm A", "values": [1, 2, 3], "low": [0.5, 1.5, 2.5], "high": [1.5, 2.5, 3.5]}]
    chart = charts.line_with_band([0, 1, 2], series, y_label="mmHg", x_label="epoch")
    assert "<polygon" in chart.svg
    assert "<polyline" in chart.svg
    assert "arm A" in chart.svg


def test_heatmap_and_stacked_bars_render_cells() -> None:
    heat = charts.heatmap(["headache", "cough"], ["placebo", "high"], [[1.0, 12.0], [2.0, 8.0]], value_suffix="%")
    assert heat.svg.count("heat-cell") >= 4
    # Large values drop the decimal (12 instead of 12.0) to keep cells readable.
    assert ">12%<" in heat.svg and ">1.0%<" in heat.svg

    stacks = charts.stacked_bars(["placebo", "high"], [[3, 1, 0, 0, 0], [5, 4, 2, 1, 0]])
    assert stacks.svg.count("<rect") >= 7
    assert "G1" in stacks.svg and "G5" in stacks.svg


def test_histogram_and_threshold_monitor() -> None:
    histogram = charts.histogram([1, 2, 2, 3, 4, 5, 5, 5], unit=" ms")
    assert histogram.svg.count("<rect") >= 4

    monitor = charts.threshold_monitor([1, 2, 3], [0.05, 0.12, 0.22], 0.20)
    assert "threshold-line" in monitor.svg
    assert "trigger-region" in monitor.svg
    assert "threshold crossed" in monitor.svg


def test_patient_timeline_marks_doses_and_adverse_events() -> None:
    chart = charts.patient_timeline(
        [0, 1, 2, 3],
        dose_mg=[0, 50, 50, 75],
        concentration=[0.0, 0.8, 1.1, 1.4],
        sbp=[132, 128, 126, 124],
        adverse_events={2: ["headache"]},
    )
    assert chart.svg.count("dose-bar") == 3
    assert chart.svg.count("ae-marker") >= 2  # one event marker plus the legend marker
    assert "concentration" in chart.svg


def test_sparkline_and_nice_ticks() -> None:
    spark = charts.sparkline([1, 3, 2, 5])
    assert spark.startswith("<svg") and "polyline" in spark
    assert charts.sparkline([]).count("polyline") == 0
    ticks = charts.nice_ticks(0.0, 10.0, 5)
    assert ticks[0] <= 0.0 and ticks[-1] >= 10.0
    assert len(ticks) >= 3


# ---------------------------------------------------------------------------
# Payload derivation
# ---------------------------------------------------------------------------


def test_load_dashboard_data_from_run_directory(tmp_path, config, protocol) -> None:
    run_dir = _run_dir(tmp_path, config, protocol)
    data = load_dashboard_data(run_dir)

    assert data.run_id == "RUN-UI"
    assert data.protocol["protocol_id"] == protocol.protocol_id
    assert data.provenance["n_patients"] > 0
    assert data.row_count > 0 and data.patient_count > 0
    assert {item["metric"] for item in data.trajectories} >= {"sbp", "dbp", "hr"}
    assert data.ae_heatmap["arms"] and data.ae_heatmap["terms"]
    assert data.grade_stacks["stacks"]
    assert data.comparisons, "the JSON report supplies the comparisons"
    assert data.patients and data.patient_details, "the patient explorer needs both index and details"
    assert data.stopping_rules, "the protocol declares stopping rules"
    assert json.dumps(data.as_dict(), default=str)


def test_payload_degrades_gracefully_without_optional_files(tmp_path, config, protocol) -> None:
    run_dir = _run_dir(tmp_path, config, protocol)
    (run_dir / "report" / "trial_report.json").unlink()
    data = load_dashboard_data(run_dir)
    assert data.notes, "a missing report must be reported, not hidden"
    assert data.safety_summary, "safety is derived from Silver when the report is unavailable"
    assert data.cohort_flow == {}


def test_missing_run_directory_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_dashboard_data(tmp_path / "nope")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        load_dashboard_data(empty)


# ---------------------------------------------------------------------------
# Rendered dashboard
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dashboard_html(tmp_path_factory, request) -> str:
    """Render one dashboard for the whole module (rendering is the slow part)."""
    pytest.importorskip("pandas")
    from insilico_trial_mas.config import load_config
    from insilico_trial_mas.pipeline import TrialSimulationPipeline, load_protocol
    from insilico_trial_mas.storage.factory import MemoryStore

    tmp_path = tmp_path_factory.mktemp("dashboard")
    config = load_config(
        None,
        {
            "n_patients": 80,
            "n_cohorts": 2,
            "epochs": 3,
            "llm_mode": "triggered",
            "export_cdisc": False,
            "report_formats": ["json"],
            "storage": {"backend": "memory"},
            "engine": {"backend": "sequential", "batch_size": 32},
            "llm": {"provider": "offline", "cache_enabled": False},
            "ml": {"backend": "mechanistic", "auto_train_if_missing": False},
            "tracking": {"enabled": True, "trace_path": str(tmp_path / "traces.jsonl")},
            "output_dir": str(tmp_path / "artifacts"),
        },
    )
    protocol = load_protocol("conf/trial_protocol_demo.yaml")
    pipeline = TrialSimulationPipeline(config, protocol, store=MemoryStore(config.storage))
    result = pipeline.run(run_id="RUN-UI-HTML")
    del result, request
    return build_dashboard(load_dashboard_data(Path(config.output_dir) / "RUN-UI-HTML"))


def test_dashboard_contains_every_screen(dashboard_html: str) -> None:
    for panel in ("overview", "efficacy", "safety", "patients", "reproducibility", "lineage"):
        assert f'data-panel="{panel}"' in dashboard_html, f"missing panel: {panel}"


def test_dashboard_is_self_contained(dashboard_html: str) -> None:
    """No CDN, no external stylesheet, no external script - it must work offline."""
    assert "http://" not in dashboard_html.replace("http://www.w3.org/2000/svg", "")
    assert "https://" not in dashboard_html
    assert "<style>" in dashboard_html
    assert dashboard_html.count("<script") >= 3  # theme, patient payload, explorer
    assert "cdn." not in dashboard_html


def test_dashboard_states_the_disclaimer_and_provenance(dashboard_html: str) -> None:
    assert "Synthetic data" in dashboard_html
    assert "not clinical evidence" in dashboard_html
    assert "Protocol digest" in dashboard_html
    assert "Master seed" in dashboard_html
    assert "insilico-trial simulate --config" in dashboard_html


def test_dashboard_has_charts_and_accessible_titles(dashboard_html: str) -> None:
    svgs = SVG_TAG_RE.findall(dashboard_html)
    assert len(svgs) >= 8, f"expected the full chart set, found {len(svgs)}"
    assert all('role="img"' in svg for svg in svgs)
    assert all("<title>" in svg for svg in svgs)


def test_dashboard_embeds_patient_data_safely(dashboard_html: str) -> None:
    assert '<script type="application/json" id="patient-data">' in dashboard_html
    payload = dashboard_html.split('id="patient-data">', 1)[1].split("</script>", 1)[0]
    parsed = json.loads(payload)
    assert parsed["patients"] and parsed["details"]
    # JSON inside a script tag must never contain a raw closing tag sequence.
    assert "</" not in payload


def test_write_dashboard_creates_the_file(tmp_path, config, protocol) -> None:
    run_dir = _run_dir(tmp_path, config, protocol)
    path = write_dashboard(run_dir)
    assert path.exists() and path.name == "dashboard.html"
    assert path.parent.name == "report"
    assert path.stat().st_size > 20_000
    custom = write_dashboard(run_dir, tmp_path / "custom.html")
    assert custom.exists() and custom.parent == tmp_path


def test_resolve_run_dir_accepts_a_parent_directory(tmp_path, config, protocol) -> None:
    from insilico_trial_mas.ui.data import resolve_run_dir

    run_dir = _run_dir(tmp_path, config, protocol)
    assert resolve_run_dir(run_dir) == run_dir
    assert resolve_run_dir(run_dir.parent) == run_dir
    with pytest.raises(FileNotFoundError):
        resolve_run_dir(tmp_path / "nothing-here")
