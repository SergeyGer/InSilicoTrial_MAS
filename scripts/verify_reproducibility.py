"""Verify that a simulation is bit-reproducible.

Run this after a deployment (or nightly) to prove that the platform still
reproduces a known trial: two independent runs with the same seed must agree on
every simulated column. Wall-clock metadata (``observation_ts``,
``llm_latency_ms``) is excluded by design - see
:data:`insilico_trial_mas.engine.partition.NON_DETERMINISTIC_COLUMNS`.

Usage::

    python scripts/verify_reproducibility.py --config conf/simulation_local.yaml --patients 800

Exit code 0 means the two runs matched; 1 means the platform is no longer
deterministic and the difference is printed for triage.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from insilico_trial_mas.config import load_config
from insilico_trial_mas.engine.partition import comparable_rows
from insilico_trial_mas.pipeline import TrialSimulationPipeline, load_protocol
from insilico_trial_mas.storage.factory import MemoryStore


def run_once(config, protocol, run_id: str) -> list[tuple]:
    pipeline = TrialSimulationPipeline(config, protocol, store=MemoryStore(config.storage))
    pipeline.run(run_id=run_id)
    frame = pipeline.store.read("silver/patient_states", run_id=run_id)
    return comparable_rows(frame.to_dict(orient="records"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify bit-level reproducibility of a simulation")
    parser.add_argument("--config", default="conf/simulation_local.yaml")
    parser.add_argument("--protocol", default=None)
    parser.add_argument("--patients", type=int, default=500)
    parser.add_argument("--epochs", type=int, default=4)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    config = load_config(
        args.config,
        {
            "n_patients": args.patients,
            "epochs": args.epochs,
            "storage": {"backend": "memory"},
            "tracking": {"enabled": False},
            "llm": {"cache_enabled": False},
        },
    )
    protocol = load_protocol(args.protocol or config.protocol_path)

    first = run_once(config, protocol, "REPRO-CHECK")
    second = run_once(config, protocol, "REPRO-CHECK")

    payload = {
        "rows": len(first),
        "identical": first == second,
        "run_id": "REPRO-CHECK",
        "protocol_id": protocol.protocol_id,
        "seed": config.seed,
        "epochs": args.epochs,
    }
    if first != second:
        differences = 0
        for left, right in zip(first, second, strict=False):
            if left != right:
                differences += 1
                if differences <= 3:
                    payload.setdefault("examples", []).append(
                        {
                            "left": dict(left),
                            "right": dict(right),
                        }
                    )
        payload["differing_rows"] = differences

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(f"reproducibility check for {protocol.protocol_id} ({payload['rows']} rows)")
        print(f"  identical: {payload['identical']}")
        if not payload["identical"]:
            print(f"  differing rows: {payload.get('differing_rows')}")
    return 0 if payload["identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
