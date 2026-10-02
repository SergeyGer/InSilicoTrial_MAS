"""Tests for the live Studio server (:mod:`insilico_trial_mas.ui.studio`).

The Studio is the only component of the platform whose contract is HTTP, so these
tests exercise it the way a browser does: a real :class:`ThreadingHTTPServer`
bound to an ephemeral port and driven with ``urllib.request`` (no new dependency,
no test client). The end-to-end simulation test is marked ``slow`` because it runs
the genuine pipeline; everything else answers from the state object and finishes
in milliseconds.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from insilico_trial_mas.config import load_config
from insilico_trial_mas.ui.studio import StudioRun, StudioState, create_server

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "conf" / "simulation_local.yaml"
PROTOCOL_PATH = REPO_ROOT / "conf" / "trial_protocol_demo.yaml"

TERMINAL_STATUSES = {"done", "error"}


@dataclass
class StudioHarness:
    """A bound, serving studio plus the state it exposes."""

    base_url: str
    state: StudioState
    server: ThreadingHTTPServer


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    body: bytes | None = None,
    timeout: float = 15.0,
) -> tuple[int, str, dict[str, str]]:
    """Perform one HTTP request and return ``(status, body, headers)`` even on 4xx/5xx."""
    if body is not None:
        data = body
    elif payload is not None:
        data = json.dumps(payload).encode("utf-8")
    else:
        data = None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), dict(exc.headers)


def _get_json(url: str) -> tuple[int, dict[str, Any]]:
    status, body, _ = _request(url)
    return status, json.loads(body)


def _post_json(url: str, payload: dict[str, Any], timeout: float = 30.0) -> tuple[int, dict[str, Any]]:
    status, body, _ = _request(url, method="POST", payload=payload, timeout=timeout)
    return status, json.loads(body)


def _server_url(server: ThreadingHTTPServer) -> str:
    address = server.server_address
    host, port = str(address[0]), int(address[1])
    return f"http://{host}:{port}"


def _wait_for_terminal_status(base_url: str, run_id: str, timeout: float = 120.0) -> dict[str, Any]:
    """Poll ``GET /api/runs/{id}`` until the run finishes or the timeout expires."""
    deadline = time.monotonic() + timeout
    payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        _, payload = _get_json(f"{base_url}/api/runs/{run_id}")
        if payload.get("status") in TERMINAL_STATUSES:
            return payload
        time.sleep(0.5)
    return payload


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def studio() -> Iterator[StudioHarness]:
    """Serve a studio on an ephemeral port and always shut it down."""
    config = load_config(CONFIG_PATH)
    state = StudioState.from_config(config, config_path=str(CONFIG_PATH))
    server = create_server(state, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield StudioHarness(base_url=_server_url(server), state=state, server=server)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _register_run(
    state: StudioState,
    tmp_path: Path,
    *,
    run_id: str = "RUN-TEST-0001",
    status: str = "queued",
) -> StudioRun:
    """Insert a run record without executing anything (keeps route tests instant)."""
    output_dir = tmp_path / "studio" / run_id
    (output_dir / "report").mkdir(parents=True, exist_ok=True)
    run = StudioRun(
        run_id=run_id,
        status=status,
        phase="queued",
        progress=0.0,
        started_at="2024-05-17T10:00:00+00:00",
        params={"protocol": str(PROTOCOL_PATH)},
        output_dir=str(output_dir),
    )
    with state.lock:
        state.runs[run.run_id] = run
    return run


# ---------------------------------------------------------------------------
# Page and configuration
# ---------------------------------------------------------------------------


def test_index_page_has_control_panel_and_disclaimer(studio: StudioHarness) -> None:
    status, body, headers = _request(f"{studio.base_url}/")

    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    # Control panel.
    assert 'id="run-form"' in body
    assert "Run simulation" in body
    assert 'id="protocol"' in body
    assert 'id="n_patients"' in body
    assert 'id="engine"' in body
    assert 'id="llm_mode"' in body
    assert 'data-preset="quick"' in body
    # Disclaimer.
    assert "Synthetic data" in body
    assert "not clinical evidence" in body
    # Accessible progress bar, and no CDN / external asset anywhere.
    assert 'role="progressbar"' in body
    assert 'src="http' not in body
    assert 'href="http' not in body


def test_config_endpoint_documents_protocols_defaults_and_options(studio: StudioHarness) -> None:
    status, payload = _get_json(f"{studio.base_url}/api/config")

    assert status == 200
    assert payload["config_path"].endswith("simulation_local.yaml")
    protocols = payload["protocols"]
    assert protocols, "at least the demo protocol must be discoverable"
    demo = next(item for item in protocols if item["path"].endswith("trial_protocol_demo.yaml"))
    assert set(demo) == {"path", "protocol_id", "title", "phase", "epochs", "arms"}
    assert demo["protocol_id"] == "INS-HTN-201"
    assert demo["phase"] == "PHASE_II"
    assert demo["epochs"] == 8
    assert len(demo["arms"]) >= 2

    defaults = payload["defaults"]
    assert defaults["n_patients"] == 1000
    assert defaults["n_cohorts"] == 8
    assert defaults["epochs"] == demo["epochs"]  # 0 in the profile means "protocol value"
    assert defaults["engine"] == "auto"
    assert defaults["llm_mode"] == "triggered"
    assert defaults["seed"] == 20240517
    assert defaults["protocol"].endswith("trial_protocol_demo.yaml")
    assert payload["options"] == {
        "engines": ["auto", "sequential", "local", "spark"],
        "llm_modes": ["off", "sample", "triggered", "all"],
    }


def test_runs_endpoint_is_newest_first(studio: StudioHarness, tmp_path: Path) -> None:
    _register_run(studio.state, tmp_path, run_id="RUN-TEST-0001")
    _register_run(studio.state, tmp_path, run_id="RUN-TEST-0002")

    status, payload = _get_json(f"{studio.base_url}/api/runs")

    assert status == 200
    assert [run["run_id"] for run in payload["runs"]] == ["RUN-TEST-0002", "RUN-TEST-0001"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ["quantum", "gpu", "sequential-engine"])
def test_post_runs_rejects_unknown_engine(studio: StudioHarness, engine: str) -> None:
    status, body = _post_json(f"{studio.base_url}/api/runs", {"engine": engine})

    assert status == 400
    assert "engine" in body["error"]


def test_post_runs_rejects_unknown_llm_mode(studio: StudioHarness) -> None:
    status, body = _post_json(f"{studio.base_url}/api/runs", {"llm_mode": "sometimes"})

    assert status == 400
    assert "llm_mode" in body["error"]


def test_post_runs_rejects_unknown_protocol(studio: StudioHarness) -> None:
    status, body = _post_json(
        f"{studio.base_url}/api/runs", {"protocol": "conf/does-not-exist.yaml", "engine": "sequential"}
    )

    assert status == 400
    assert "protocol" in body["error"]


def test_post_runs_rejects_a_malformed_body(studio: StudioHarness) -> None:
    status, body, _ = _request(f"{studio.base_url}/api/runs", method="POST", body=b"{not json")

    assert status == 400
    assert "invalid JSON" in json.loads(body)["error"]


def test_post_runs_clamps_out_of_range_inputs(studio: StudioHarness, tmp_path: Path) -> None:
    payload = {
        "protocol": str(PROTOCOL_PATH),
        "n_patients": 1,
        "n_cohorts": 0,
        "epochs": 99,
        "engine": "sequential",
        "llm_mode": "off",
        "seed": 7,
        "llm_sample_rate": 4.0,
        "storage_backend": "memory",
        "output_dir": str(tmp_path / "studio"),
    }

    status, run = _post_json(f"{studio.base_url}/api/runs", payload)

    assert status == 201
    assert run["status"] in {"queued", "running"}
    assert run["params"]["n_patients"] == 10  # clamped up to the floor
    assert run["params"]["n_cohorts"] == 1
    assert run["params"]["epochs"] == 32  # clamped down to the ceiling
    assert run["params"]["llm_sample_rate"] == 1.0


# ---------------------------------------------------------------------------
# Dashboard and artefacts
# ---------------------------------------------------------------------------


def test_dashboard_returns_202_while_a_run_has_no_report(studio: StudioHarness, tmp_path: Path) -> None:
    run = _register_run(studio.state, tmp_path)

    status, body, headers = _request(f"{studio.base_url}/api/runs/{run.run_id}/dashboard")

    assert status == 202
    assert headers["Content-Type"].startswith("text/html")
    assert run.run_id in body
    assert "not ready" in body.lower()


def test_dashboard_falls_back_to_the_trial_report(studio: StudioHarness, tmp_path: Path) -> None:
    run = _register_run(studio.state, tmp_path, status="done")
    report = Path(run.output_dir) / "report" / "trial_report.html"
    report.write_text("<html><body>FALLBACK-REPORT</body></html>", encoding="utf-8")

    status, body, _ = _request(f"{studio.base_url}/api/runs/{run.run_id}/dashboard")

    assert status == 200
    assert "FALLBACK-REPORT" in body


def test_artifacts_serves_whitelisted_files_and_blocks_traversal(
    studio: StudioHarness, tmp_path: Path
) -> None:
    run = _register_run(studio.state, tmp_path, status="done")
    artifact = Path(run.output_dir) / "report" / "trial_report.json"
    artifact.write_text('{"run_id": "RUN-TEST-0001"}', encoding="utf-8")
    base = f"{studio.base_url}/api/runs/{run.run_id}/artifacts"

    status, body, headers = _request(f"{base}/trial_report.json")
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body)["run_id"] == "RUN-TEST-0001"

    encoded = "%2e%2e%2f%2e%2e%2fetc%2fpasswd"
    status, body, _ = _request(f"{base}/{encoded}")
    assert status == 400
    assert "invalid" in json.loads(body)["error"]

    status, _, _ = _request(f"{base}/../../etc/passwd")
    assert status == 400

    status, body, _ = _request(f"{base}/script.sh")
    assert status == 400
    assert "not allowed" in json.loads(body)["error"]

    status, _, _ = _request(f"{base}/missing.json")
    assert status == 404


def test_unknown_run_is_a_json_404(studio: StudioHarness) -> None:
    status, body, _ = _request(f"{studio.base_url}/api/runs/RUN-DOES-NOT-EXIST")

    assert status == 404
    assert "unknown run" in json.loads(body)["error"]


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_unknown_route_returns_404_and_wrong_method_returns_405(studio: StudioHarness) -> None:
    status, body, headers = _request(f"{studio.base_url}/api/nope")
    assert status == 404
    assert headers["Content-Type"].startswith("application/json")
    assert "unknown route" in json.loads(body)["error"]

    status, _, headers = _request(f"{studio.base_url}/api/config", method="POST", body=b"{}")
    assert status == 405
    assert headers["Allow"] == "GET, POST"

    status, _, _ = _request(f"{studio.base_url}/api/runs", method="PUT")
    assert status == 405


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_studio_run_reaches_done_and_serves_its_report(studio: StudioHarness, tmp_path: Path) -> None:
    payload = {
        "protocol": str(PROTOCOL_PATH),
        "n_patients": 40,
        "n_cohorts": 2,
        "epochs": 2,
        "engine": "sequential",
        "llm_mode": "off",
        "seed": 20240517,
        "llm_sample_rate": 0.0,
        # Supported by the studio so the test never writes a lake or an MLflow run.
        "storage_backend": "memory",
        "output_dir": str(tmp_path / "studio"),
    }

    status, run = _post_json(f"{studio.base_url}/api/runs", payload, timeout=30.0)
    assert status == 201, run
    run_id = run["run_id"]
    assert run["params"]["n_patients"] == 40

    # A just-queued run may still answer 202 on the dashboard route.
    dashboard_status, _, _ = _request(f"{studio.base_url}/api/runs/{run_id}/dashboard")
    assert dashboard_status in {200, 202}

    finished = _wait_for_terminal_status(studio.base_url, run_id, timeout=120.0)
    assert finished["status"] == "done", finished.get("error") or finished
    assert finished["progress"] == 1.0
    assert finished["finished_at"]
    assert finished["summary"]["engine"] == "sequential"
    patients = finished["summary"]["patients"]
    assert 0 < patients <= 40  # eligibility screening may remove a few patients
    assert Path(finished["dashboard_path"]).parent == Path(finished["output_dir"]) / "report"

    status, dashboard, headers = _request(f"{studio.base_url}/api/runs/{run_id}/dashboard")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert "<html" in dashboard.lower()

    status, report, headers = _request(f"{studio.base_url}/api/runs/{run_id}/artifacts/trial_report.md")
    assert status == 200
    assert headers["Content-Type"].startswith("text/markdown")
    assert len(report) > 200
