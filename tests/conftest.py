"""Shared pytest fixtures.

Everything the tests need is built from the repository's own configuration files
so a change in a protocol or config is exercised by the test suite immediately.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from insilico_trial_mas.agents.base import AgentContext
from insilico_trial_mas.agents.protocol_agent import ProtocolAgent
from insilico_trial_mas.cohort.generator import CohortGenerator
from insilico_trial_mas.cohort.serialization import profiles_to_frame
from insilico_trial_mas.config import SimulationConfig, load_config
from insilico_trial_mas.engine.context import RunSpec
from insilico_trial_mas.engine.runtime import reset_runtimes
from insilico_trial_mas.pipeline import load_protocol
from insilico_trial_mas.schemas import TrialProtocol

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = REPO_ROOT / "conf" / "trial_protocol_demo.yaml"


def _fast_config(**overrides) -> dict:
    """A tiny, fully offline configuration for fast tests."""
    payload = {
        "n_patients": 120,
        "n_cohorts": 3,
        "epochs": 3,
        "llm_mode": "triggered",
        "llm_sample_rate": 0.05,
        "export_cdisc": False,
        "report_formats": ["json"],
        "storage": {"backend": "memory"},
        "engine": {"backend": "sequential", "batch_size": 64},
        "llm": {"provider": "offline", "cache_enabled": False, "max_concurrency": 4},
        "ml": {"backend": "mechanistic", "auto_train_if_missing": False},
        "tracking": {"enabled": False},
    }
    payload.update(overrides)
    return payload


@pytest.fixture(scope="session", autouse=True)
def _quiet_logging() -> None:
    os.environ.setdefault("INSILICO_LOG_LEVEL", "WARNING")


@pytest.fixture
def protocol() -> TrialProtocol:
    return load_protocol(PROTOCOL_PATH)


@pytest.fixture
def config(tmp_path: Path) -> SimulationConfig:
    payload = _fast_config()
    payload["output_dir"] = str(tmp_path / "artifacts")
    config = load_config(None, payload)
    config.protocol_path = str(PROTOCOL_PATH)
    return config


@pytest.fixture
def cohort(protocol: TrialProtocol, config: SimulationConfig):
    return CohortGenerator(protocol, config).generate()


@pytest.fixture
def planned(protocol: TrialProtocol, config: SimulationConfig, cohort):
    """Screened + randomised cohort, ready for simulation."""
    agent = ProtocolAgent(
        AgentContext(
            run_id="test-run",
            protocol=protocol,
            config=config,
            seed=config.seed,
            total_epochs=config.epochs or protocol.epochs,
        )
    )
    screening, allocation = agent.plan(cohort)
    frame = profiles_to_frame(screening.enrolled, allocation.arm_by_patient)
    return screening, allocation, frame


@pytest.fixture
def context(protocol: TrialProtocol, config: SimulationConfig, planned) -> AgentContext:
    _, allocation, _ = planned
    return RunSpec(
        run_id="test-run",
        protocol=protocol,
        config=config,
        arm_by_patient=allocation.arm_by_patient,
        seed=config.seed,
        total_epochs=config.epochs or protocol.epochs,
    ).to_context()


@pytest.fixture(autouse=True)
def _reset_runtime_cache():
    """Worker runtimes are cached per process: never leak one test into another."""
    reset_runtimes()
    yield
    reset_runtimes()


@pytest.fixture
def fast_config_factory(tmp_path: Path):
    """Return a factory that builds a fast, fully offline :class:`SimulationConfig`."""

    def build(**overrides) -> SimulationConfig:
        payload = _fast_config(**overrides)
        payload["output_dir"] = str(tmp_path / "artifacts")
        config = load_config(None, payload)
        config.protocol_path = str(PROTOCOL_PATH)
        return config

    return build
