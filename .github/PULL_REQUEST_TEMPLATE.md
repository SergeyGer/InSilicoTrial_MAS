<!--
Keep this short and factual. Delete sections that genuinely do not apply, but do not
delete the synthetic-data statement: every pull request must carry it.
-->

## What changed and why

<!-- 2-4 sentences. Link the issue or Discussion this closes (for example "Closes #123"). -->

## How it was verified

<!-- Commands you ran and the result. Paste the failing case you fixed if there was one. -->

## Checklist

- [ ] `make check` passes locally (ruff + mypy + the fast test suite).
- [ ] Engine backends exercised: <!-- tick what you actually ran -->
  - [ ] `sequential`
  - [ ] `local` (`make simulate` / `make test`)
  - [ ] `spark` (`make test-spark` or `make simulate-spark`; skip only if no JVM is available, and say so)
- [ ] Determinism preserved: `make verify-repro` reports identical comparable rows.
- [ ] Calibration bands still hold: `make calibrate` passes (required for any PK/PD, physiology or AE change).
- [ ] UI regenerated if a dashboard, chart, Studio or report template changed: `python scripts/render_ui_previews.py`.
- [ ] Documentation updated for user-visible changes (`README.md`, `docs/ARCHITECTURE.md`, `docs/UI.md`, `docs/DATA_MODEL.md`, `CHANGELOG.md` under `## [Unreleased]`).
- [ ] No new runtime dependency without justification in the description; optional extras stay optional (pyspark, mlflow, langchain imports remain lazy).
- [ ] New stochastic behaviour goes through `reproducibility.rng_for` / `stable_hash` — no global or unseeded RNG.

## Synthetic-data statement (required)

- [ ] I confirm that **all patient data added or touched by this change is synthetic**, that
      no real patient data, protected health information, credentials or secrets are
      included, and that the change does not present simulated output as clinical evidence.
