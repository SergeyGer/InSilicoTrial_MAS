"""The story page must stay self-contained and must not invent numbers."""

from __future__ import annotations

import importlib.util
import pathlib
import re

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "build_story.py"

MANIFEST = {
    "run_id": "RUN-TEST-0001",
    "engine": "sequential",
    "config": {"epochs": 6, "llm_mode": "off"},
    "analysis_overview": {
        "protocol_id": "INS-HTN-201",
        "primary_endpoint": "change_in_systolic_bp",
        "n_patients": 329,
        "n_adverse_events": 85,
        "grade3_plus_patients": 3,
        "stopping_rules_triggered": 0,
        "safety_alerts": 0,
        "control_arm": "placebo",
        "sbp_change_placebo": -3.117,
        "sbp_change_low_dose": -7.404,
        "sbp_change_high_dose": -11.571,
        "responder_rate_placebo": 0.0,
        "responder_rate_low_dose": 0.1636,
        "responder_rate_high_dose": 0.6306,
        "grade3_rate_high_dose": 0.018,
        "effect_low_dose_vs_placebo": -4.2863,
        "effect_high_dose_vs_placebo": -8.4538,
    },
    "screening": {
        "screened": 400,
        "enrolled": 329,
        "screen_failures": 71,
        "screen_failure_rate": 0.1775,
        "failure_reasons": {"excluded_condition": 32},
    },
    "allocation": {"high_dose": 111, "low_dose": 110, "placebo": 108},
    "replay": {"seed": 20240517, "protocol_digest": "355d80abd4a137f3"},
    "provenance": {"llm_mode": "triggered"},
}


@pytest.fixture(scope="module")
def build_story():
    spec = importlib.util.spec_from_file_location("build_story", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_story


def test_story_is_self_contained(build_story) -> None:
    """No remote scripts, styles, fonts or images: the page must work offline."""
    page = build_story(MANIFEST, dashboard_url="https://example.test/", repo_url="https://example.test/repo")

    assert "<script src" not in page
    assert 'rel="stylesheet"' not in page
    assert "@import" not in page
    assert "<iframe" not in page
    # The only absolute URLs allowed are navigational links, never loads.
    for url in re.findall(r'(?:src|href)="(https?://[^"]+)"', page):
        assert url.startswith("https://example.test"), url


def test_story_quotes_the_run_it_describes(build_story) -> None:
    page = build_story(MANIFEST, dashboard_url="https://example.test/", repo_url="https://example.test/repo")

    for expected in ("INS-HTN-201", "329", "400", "85", "20240517", "-11.6", "63.1%", "8.5"):
        assert expected in page, expected
    # The verdict is derived from the numbers, not hard-coded prose.
    assert "high dose separates from placebo" in page


def test_story_has_one_section_per_step(build_story) -> None:
    page = build_story(MANIFEST, dashboard_url="https://example.test/", repo_url="https://example.test/repo")
    steps = re.findall(r'<section class="step" data-step="([a-z]+)"', page)
    assert steps == ["screening", "allocation", "exposure", "safety", "estimate", "verdict"]


def test_story_survives_a_sparse_manifest(build_story) -> None:
    """A run manifest from an older version may miss keys; the page must still render."""
    page = build_story({"run_id": "RUN-EMPTY"}, dashboard_url="https://example.test/", repo_url="https://example.test/repo")
    assert "<!doctype html>" in page
    assert "Would this trial have worked?" in page


def test_story_escapes_run_values(build_story) -> None:
    """Manifest values are data, not markup."""
    hostile = dict(MANIFEST)
    hostile["analysis_overview"] = dict(MANIFEST["analysis_overview"], protocol_id="<script>alert(1)</script>")
    page = build_story(hostile, dashboard_url="https://example.test/", repo_url="https://example.test/repo")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
