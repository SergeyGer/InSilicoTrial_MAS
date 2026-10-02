"""Plausibility calibration harness for a trial protocol.

Before a protocol is used for a headline simulation it should pass a *sanity*
check: are the exposure levels, blood-pressure effects and adverse-event rates
in ranges that a clinical team would recognise? This script runs a fast
mechanistic pass (no LLM, one process) and prints the resulting profile so the
drug/AE parameters can be tuned deliberately rather than by accident.

Usage::

    python scripts/calibrate_protocol.py --config conf/simulation_local.yaml --patients 4000

The same numbers are asserted (with generous bands) by
``tests/test_calibration.py`` so a regression in the PK/PD or AE code cannot
silently produce a trial where 90% of patients have a serious adverse event.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from insilico_trial_mas.agents.base import AgentContext
from insilico_trial_mas.agents.biostatistician_agent import (
    BiostatisticianAgent,
    end_of_treatment_frame,
    extract_adverse_events,
)
from insilico_trial_mas.agents.protocol_agent import ProtocolAgent
from insilico_trial_mas.cohort.generator import CohortGenerator
from insilico_trial_mas.cohort.serialization import profiles_to_frame
from insilico_trial_mas.config import load_config
from insilico_trial_mas.engine.context import RunSpec
from insilico_trial_mas.engine.sequential_runner import SequentialEngine
from insilico_trial_mas.pipeline import load_protocol


def calibrate(config_path: str, protocol_path: str | None, patients: int) -> dict[str, pd.DataFrame]:
    config = load_config(
        config_path,
        {
            "n_patients": patients,
            "ml": {"backend": "mechanistic"},
            "llm_mode": "off",
            "tracking": {"enabled": False},
            "storage": {"backend": "memory"},
            "export_cdisc": False,
        },
    )
    protocol = load_protocol(protocol_path or config.protocol_path)
    cohort = CohortGenerator(protocol, config).generate()
    agent = ProtocolAgent(
        AgentContext(
            run_id="calibration", protocol=protocol, config=config, seed=config.seed, total_epochs=protocol.epochs
        )
    )
    screening, allocation = agent.plan(cohort)
    frame = profiles_to_frame(screening.enrolled, allocation.arm_by_patient)
    context = RunSpec(
        run_id="calibration",
        protocol=protocol,
        config=config,
        arm_by_patient=allocation.arm_by_patient,
        seed=config.seed,
        total_epochs=protocol.epochs,
    ).to_context()
    result = SequentialEngine(context).run(frame)
    observations = pd.DataFrame(result.rows)
    analysis = BiostatisticianAgent(context).analyze(observations)

    exposure = (
        observations[observations["epoch"] == protocol.epochs]
        .groupby("arm_id")[["plasma_conc_mg_l", "auc_epoch_mg_h_l"]]
        .mean()
        .round(3)
    )
    efficacy = pd.DataFrame(
        [
            {
                "arm_id": summary.arm_id,
                "n": summary.n_patients,
                "mean_sbp_change": round(summary.mean_sbp_change, 2),
                "sd_sbp_change": round(summary.sd_sbp_change, 2),
                "responder_rate": round(summary.responder_rate, 3),
                "mean_alt_ratio": round(summary.mean_alt_ratio, 3) if summary.mean_alt_ratio == summary.mean_alt_ratio else None,
            }
            for summary in analysis.arm_summaries
        ]
    )
    safety = pd.DataFrame(
        [
            {
                "arm_id": summary.arm_id,
                "any_ae": round(summary.ae_rate, 3),
                "grade3_plus": round(summary.grade3_plus_rate, 3),
                "sae": round(summary.sae_rate, 3),
                "mortality": round(summary.mortality_rate, 4),
                "discontinued": summary.discontinuations,
            }
            for summary in analysis.arm_summaries
        ]
    )
    eot = end_of_treatment_frame(observations)
    worst = (
        extract_adverse_events(observations)
        .groupby(["arm_id", "term"])["ctcae_grade"]
        .agg(["count", "max"])
        .reset_index()
        .sort_values(["arm_id", "count"], ascending=[True, False])
        .round(3)
    )
    return {"exposure": exposure, "efficacy": efficacy, "safety": safety, "ae_by_term": worst, "eot": eot}


def main() -> int:
    parser = argparse.ArgumentParser(description="Calibrate and sanity-check a trial protocol")
    parser.add_argument("--config", default="conf/simulation_local.yaml")
    parser.add_argument("--protocol", default=None)
    parser.add_argument("--patients", type=int, default=4000)
    args = parser.parse_args()

    tables = calibrate(args.config, args.protocol, args.patients)
    for name in ("exposure", "efficacy", "safety"):
        print(f"\n=== {name} ===")
        print(tables[name].to_string(index=False))
    print("\n=== adverse events by arm and term (top 12) ===")
    print(tables["ae_by_term"].head(12).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
