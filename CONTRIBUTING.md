# Contributing to InSilicoTrial MAS

Thanks for your interest. This repository is a multi-agent platform that simulates a
randomised clinical trial over a **synthetic** cohort: a Protocol Agent screens and
randomises, thousands of Patient Persona Agents react epoch by epoch (mechanistic PK/PD +
a learned physiology residual + an optional LLM narrator), and a Biostatistician Agent
turns the logs into a safety and efficacy readout. It is a trial-design decision-support
tool, not clinical evidence — read
[docs/ETHICS_AND_LIMITATIONS.md](docs/ETHICS_AND_LIMITATIONS.md) before you change
anything that can move a number.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md). Report
vulnerabilities privately as described in [SECURITY.md](SECURITY.md) — never in a public
issue.

## 1. Read this first

| Document | Why you need it |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | The real module map: agents, engines, determinism strategy, storage, tracing, and a test index for every claim. |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md) | The Bronze/Silver/Gold contract and the meaning of every Silver column. |
| [docs/ETHICS_AND_LIMITATIONS.md](docs/ETHICS_AND_LIMITATIONS.md) | Synthetic-data guarantee, illustrative priors, the LLM safety filter, and what the model does not capture. |
| [docs/UI.md](docs/UI.md) | Dashboard and Studio design record and extension points. |
| [docs/SPEC_COMPLIANCE.md](docs/SPEC_COMPLIANCE.md) | Where the implementation deliberately departs from the original specification draft. |
| [docs/RUNBOOK_DATABRICKS_AWS.md](docs/RUNBOOK_DATABRICKS_AWS.md) | Sizing, cost control and the failure playbook for the cloud path. |
| [terraform/README.md](terraform/README.md) | Unity Catalog and AWS storage module usage. |

## 2. Setting up a development environment

Python 3.10 or newer is required (`pyproject.toml` declares `requires-python = ">=3.10"`);
CI runs 3.10, 3.11 and 3.12. No cloud account, credentials or network access are needed for
the core path: the default LLM provider is the deterministic `offline` one.

```bash
git clone https://github.com/SergeyGer/InSilicoTrial_MAS.git
cd InSilicoTrial_MAS
bash scripts/bootstrap.sh        # creates .venv, installs -e '.[dev,llm,tracking]', prints env-check
source .venv/bin/activate
make test                        # fast test suite, no JVM required
```

Useful variants:

```bash
bash scripts/bootstrap.sh --spark   # adds PySpark + Delta and a portable Temurin JRE 17 in .toolchain/
bash scripts/bootstrap.sh --all     # everything, including the scikit-learn/xgboost extras
make help                           # every target with a one-line description
insilico-trial demo --patients 200 --epochs 3   # offline end-to-end run into artifacts/demo/
```

`make jre` downloads the same portable JRE on demand; `make test-spark` and
`make simulate-spark` set `JAVA_HOME` for you.

## 3. Development workflow

1. Look for an existing issue or start a thread in
   [GitHub Discussions](https://github.com/SergeyGer/InSilicoTrial_MAS/discussions) before
   writing code for anything larger than a fix.
2. Branch from `main` using a descriptive name:
   `git switch -c fix/spark-empty-partition-schema`.
3. Make the change in small commits, with tests and docs in the same branch.
4. Run the gates that apply (section 4).
5. Open a pull request and fill in the template. CI must be green, `CODEOWNERS` review is
   required, and `main` only accepts linear history: PRs are squash-merged or rebased, and
   force-pushes to `main` are rejected.

Continuous integration (`.github/workflows/ci.yml`) runs six jobs on every pull request,
which appear as eight checks plus the CodeQL analysis because the test job fans out over the Python matrix:
`Lint and type-check`, `Tests (Python 3.10)`, `Tests (Python 3.11)`, `Tests (Python 3.12)`,
`Simulation plausibility and reproducibility`, `Spark engine (local[*] driver)` and
`Terraform and bundle validation`. CodeQL analysis (`Analyze Python`) runs on top of that.

## 4. Quality gates

Run these before pushing. `make check` is the minimum for every change; the rest depend on
what you touched.

| Command | What it proves | When |
| --- | --- | --- |
| `make check` | `ruff check src tests scripts` + `mypy` + the fast test suite | Every change |
| `make test` | 229 of the 231 tests, excluding the two Spark-marked ones | Every change |
| `make test-spark` | The 2 Spark tests: the Spark engine reproduces the sequential engine, and both partition modes agree | Any change to `engine/`, `ml/`, `agents/` or the Silver contract |
| `make coverage` | `coverage.xml` plus a terminal report (`--cov=insilico_trial_mas`) | Optional, useful for large additions |
| `make verify-repro` | Two runs with the same seed produce identical comparable rows (`scripts/verify_reproducibility.py --patients 400 --epochs 4`) | Any change that can affect a stochastic draw |
| `make calibrate` | Internal plausibility bands: therapeutic exposure, plausible effect size, non-catastrophic AE profile, monotone dose response (`scripts/calibrate_protocol.py --patients 3000`) | Any PK/PD, physiology, AE or priors change |
| `make notebook` | Every verification notebook cell executes headlessly (`scripts/run_notebook.py`) | Changes to `notebooks/` or the public API they use |
| `make verify-ui` | Dashboard and Studio tests (`tests/test_ui_dashboard.py`, `tests/test_ui_studio.py`) | Any UI or report-template change |
| `make iac-validate` | `terraform fmt -check`, `init -backend=false`, `validate` | Any `terraform/` change |
| `make bundle-validate` | Databricks Asset Bundle schema check (needs the Databricks CLI and a profile) | Any `databricks.yml` or `resources/` change |
| `python scripts/render_ui_previews.py` | Regenerates the checked-in `docs/dashboard-preview.html` and `docs/studio-preview.html` | Any UI change |

Calibration and reproducibility are not optional niceties here: they are the evidence behind
the project's claims, and a reduction in either is treated as a regression.

## 5. Coding standards

**Language and style.**

- All code, comments, docstrings, commit messages and documentation are in English.
- Every module starts with `from __future__ import annotations` and a docstring that says
  what the module is for and, where relevant, which specification defect it fixes.
- Type hints on public functions, methods and dataclass fields. The hot path uses
  `@dataclass(slots=True)` (`schemas.py`) rather than Pydantic models — keep it that way.
- Ruff is configured in `pyproject.toml` with `line-length = 110`, `target-version = "py310"`
  and the rule families `E, F, W, I, N, UP, B, C4, SIM, RUF`. `E501` is currently in the
  ignore list, so wrapping at 110 columns is a review expectation rather than a lint error —
  please respect it anyway.
- `mypy` runs over the installed package (`packages = ["insilico_trial_mas"]`,
  `python_version = "3.10"`, `ignore_missing_imports = true`). Do not silence a real type
  error with `# type: ignore`; fix the annotation or narrow the type.

**Logging and output.**

- Library code uses the project logger, never `print`:
  `from insilico_trial_mas.logging_utils import get_logger`.
  `print` is reserved for CLI user-facing output (including the `--json` payloads that CI
  parses).
- Include structured context through `log_extra(...)` instead of f-string prose when the
  value matters operationally (run id, table, engine, patient id).
- Log levels must stay useful: `WARNING` is the default in CI (`INSILICO_LOG_LEVEL`).

**Dependencies.**

- The core runtime footprint is deliberately small: `numpy`, `pandas`, `pyarrow`,
  `pydantic`, `PyYAML`, `Jinja2`. **No new runtime dependency without justification** in the
  pull request: what it replaces, why the standard library or NumPy is insufficient, its
  licence (MIT-compatible), and whether it is available on Databricks runtimes.
- Anything optional belongs in an extra — `spark`, `llm`, `tracking`, `ml` — and must be
  imported lazily so that `pyspark`, `mlflow` and `langchain` stay optional (see
  `engine/__init__.py` and `mlflow_tracking/tracker.py` for the established pattern).
- Test-only tooling goes in the `dev` extra. Dependabot proposes the bumps; you review them.

**Determinism and data contracts.**

- Never use a global or unseeded RNG. Every stochastic draw comes from
  `reproducibility.rng_for(seed, run_id, patient_id, epoch, purpose)` or
  `stable_hash(...)`, so partition layout cannot change a number. New draws must follow that
  pattern or `make verify-repro` and the cross-engine tests will fail.
- `engine/partition.py::NON_DETERMINISTIC_COLUMNS` (`observation_ts`, `llm_latency_ms`) is
  the only tolerated source of run-to-run variation. Do not add to it without a documented
  reason.
- The Silver contract has one source of truth. Adding, renaming or retyping a column means
  updating `SILVER_COLUMNS`, the pandas dtypes and `silver_struct_type()` together **and**
  bumping `SILVER_SCHEMA_VERSION` in `version.py`, because Delta time-travel reads must stay
  interpretable.
- Errors: raise the typed exceptions in `errors.py` (`SimulationError`,
  `ProtocolValidationError`, `ConfigurationError`, `LLMProviderError`); the CLI maps them to
  exit codes.

## 6. Scientific changes

A "scientific change" is anything that can move a published number:

- `src/insilico_trial_mas/ml/pk_pd.py` — clearance, volume, absorption, superposition, the
  Emax link, exposure metrics;
- `src/insilico_trial_mas/ml/physiology.py` — the mechanistic backbone, the AE logit and
  sampling, measurement noise, the learned residual head and its feature contract;
- `src/insilico_trial_mas/resources/genomic_priors.yaml` — ancestry mixture, allele
  frequencies, physiology priors, comorbidity multipliers;
- `src/insilico_trial_mas/agents/` — screening, randomisation, dose directives, the
  aggregation and the statistical readout;
- `conf/*.yaml` drug blocks: PK parameters, `ec50_mg_l`, `hill`, `ae_models`, stopping rules
  or endpoints.

Such a pull request must include:

1. **Calibration evidence.** `make calibrate` (and the assertions in
   `tests/test_calibration.py`) must still pass, and the description must show the
   before/after values for the affected bands — exposure range, effect size, AE rates and
   dose-response monotonicity.
2. **Reproducibility evidence.** `make verify-repro` must pass, and the cross-engine
   equivalence tests must not be weakened. A change that makes two runs with the same seed
   differ, or that makes engines disagree, is rejected regardless of the numbers it produces.
3. **A stated rationale and source.** Why the new parameters are better, and where they come
   from. Shipped priors and drug parameters are illustrative; if you replace them with
   licensed reference data, record the source, version and licence — do not paste restricted
   data into the repository.
4. **An honest limitations note.** If the change removes or narrows a documented limitation,
   update `docs/ETHICS_AND_LIMITATIONS.md` in the same pull request.

Keep the LLM out of the quantitative path. The persona model is a narrator: it may add
`source = "llm"` symptom terms, but it must never compute an endpoint, a dose or a safety
signal. The medical-advice filter in `llm/prompts.py` is a safety control — do not relax it
without a test that documents why.

## 7. Proposing a protocol

Protocols are YAML files under `conf/` validated by the Pydantic models in `schemas.py`
(`TrialProtocol`, `DrugModel`, `EligibilityCriteria`, endpoints, `StoppingRule`s).

1. Copy an existing profile, for example
   [`conf/trial_protocol_demo.yaml`](conf/trial_protocol_demo.yaml), and adapt the arms,
   titration, eligibility criteria, endpoints and DSMB rules.
2. Validate it and read the review warnings:
   `insilico-trial validate-protocol --protocol conf/your_protocol.yaml`.
3. Prove it runs end to end:
   `insilico-trial simulate --config conf/simulation_local.yaml --protocol conf/your_protocol.yaml --patients 500 --epochs 4 --engine local --output-dir artifacts/your-protocol`
   and check the dashboard in `artifacts/your-protocol/<RUN-ID>/report/dashboard.html`.
4. Open a pull request that states which parameters are illustrative and which are taken
   from a citable source.

Do not commit patient-level data, a sponsor's confidential protocol text or anything you do
not have the right to publish. A design (doses, endpoint definitions) is usually fine; the
document it came from often is not.

## 8. Commit messages

Conventional Commits, imperative mood, English, with a scope naming the module:

```
feat(engine): reject empty spark partitions with the canonical silver schema
fix(llm): keep the offline fallback when the provider raises a permanent error
perf(storage): read a single version directory instead of globbing the table
docs(ethics): document the exposure-driven toxicity limitation
test(calibration): tighten the monotone dose-response band
chore(deps): bump pandas to 2.2
ci(deps): bump actions/upload-artifact from 4 to 5
```

Keep the subject under ~72 characters, explain the *why* in the body, and reference the
issue (`Refs #123`, `Closes #123`). Do not put emoji in commit messages.

## 9. Pull request process

- One logical change per pull request; split refactors from behaviour changes.
- Fill in [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md). The last
  checkbox — that all patient data in the change is synthetic — is mandatory.
- Expect a review from the owner of the touched paths (see
  [`.github/CODEOWNERS`](.github/CODEOWNERS)). Science-critical paths (`ml/`, `agents/`),
  infrastructure (`terraform/`), CI (`.github/workflows/`) and the ethics and security
  policies always need the maintainer's review.
- CI must pass. If a job is flaky, say so in the PR thread rather than re-running until it
  is green; flakes are bugs.
- Merging keeps linear history: squash or rebase. The merged commit message should read as
  a conventional commit, because `CHANGELOG.md` is maintained from them.
- Update `CHANGELOG.md` under `## [Unreleased]` for user-visible changes
  ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed,
  Deprecated, Removed, Fixed, Security).

Releases are cut from tags, not from a branch: pushing a `v*` tag runs
`.github/workflows/release.yml`, which builds the sdist and wheel, installs the wheel in a
clean environment, smoke-tests `insilico-trial demo`, verifies the rendered diagrams and
previews, and publishes the GitHub Release using the matching `CHANGELOG.md` section as the
release notes. Maintainers publish the tree with `make publish`
(`scripts/publish_github.sh` refuses to push unless the quality gates pass).

## 10. Reporting bugs, security issues and conduct problems

- Bugs and feature requests: use the issue forms; they ask for the engine backend, the Python
  version and the `insilico-trial env-check` output, which is what makes a report actionable.
- Questions and design debate: GitHub Discussions.
- Vulnerabilities: private GitHub Security Advisory only, per [SECURITY.md](SECURITY.md).
- Conduct: see [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## 11. License

By contributing you agree that your contribution is licensed under the MIT License
([LICENSE](LICENSE)), and you confirm that you have the right to submit it.
