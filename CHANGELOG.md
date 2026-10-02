# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Terraform: raised the AWS provider constraint from `~> 5.50` to `~> 6.0` and
  migrated the module to it (verified against 6.67.0, pinned in
  `terraform/.terraform.lock.hcl`). The only breaking change that affected the
  module is the provider 6.0 rename of `aws_batch_compute_environment`'s
  `compute_environment_name` argument to `name`; the S3, IAM and Secrets Manager
  resources the module uses are unchanged in 6.x, and so is the Databricks
  provider constraint.

## [1.0.0] - 2026-10-02

First public release of InSilicoTrial MAS: a multi-agent platform that simulates a
randomised clinical trial over a fully synthetic cohort, on a laptop or on
Databricks/AWS. It is a trial-design decision-support tool — the outputs are not clinical
evidence (see `docs/ETHICS_AND_LIMITATIONS.md`).

### Added

**Agents and pipeline**

- Three agent roles sharing one immutable `AgentContext`: a Protocol Agent (protocol
  validation, eligibility screening with recorded screen-failure reasons, deterministic
  stratified permuted-block randomisation with an auditable allocation table, titration-aware
  dose directives, deviation records), a Synthetic Patient Persona Agent (per patient and
  epoch: exposure, physiology prediction, adverse-event sampling, optional LLM narration,
  adherence/discontinuation logic) and a Biostatistician Agent (per-arm summaries,
  treatment-vs-control comparisons, CTCAE safety tabulation, DSMB stopping-rule evaluation,
  safety alerts, demographics, dose-response, CONSORT-style cohort flow and a data-quality
  audit).
- `pipeline.py` as the single definition of one simulation run: protocol to Bronze, cohort,
  screening and randomisation, patient agents through the selected engine, Silver, analysis,
  report, Gold tables, CDISC exports, MLflow tracking and the run manifest.

**Models**

- Mechanistic PK/PD: one-compartment oral model with first-order absorption and closed-form
  repeated-dose superposition, individual clearance from weight, age, sex, eGFR and CYP2D6
  phenotype, and sigmoid Emax linked to average steady-state concentration, with the full
  exposure vector (trough, Cmax, per-epoch AUC, cumulative AUC, exposure ratio) recorded per
  epoch.
- Hybrid physiology model: an auditable mechanistic backbone (placebo onset, progression,
  pharmacogenomic modifiers for ADRB1, ACE, HLA-B*57:01 and SLCO1B1, exposure-driven logistic
  adverse-event models with CTCAE grade distributions) plus a learned ridge-regression
  residual head trained on synthetic historical cohorts, versioned, digest-hashed and
  optionally registered in the MLflow Model Registry, with a mechanistic fallback when no
  artifact is available.
- Cohort generation from population priors: synthetic demographics, comorbidities, smoking
  and alcohol distributions, eGFR, and ancestry-conditional pharmacogenomic markers, with
  every draw derived from an explicit seed tuple.

**Execution engines**

- Three interchangeable engines behind one `BaseEngine` interface: `sequential` (reference
  implementation), `local` (process pool with per-worker asyncio and bounded LLM
  concurrency, degrading in-process with a recorded reason when a pool cannot be created)
  and `spark` (`applyInPandas` in cohort mode, `mapInPandas` in balanced mode).
- Bit-identical output across engines: every stochastic draw comes from
  `sha256(seed, run, patient, epoch, purpose)`, and cross-engine comparisons run through
  `comparable_rows`, which drops only the wall-clock columns. `tests/test_engines.py` proves
  sequential == local == Spark and cohort == balanced partitioning.
- Engine selection and an environment checklist (`insilico-trial env-check`): Python,
  platform, CPU, memory, JVM and PySpark detection, Databricks and Community Edition
  detection, recommended backend and notes, with `strict_engines` refusing a requested but
  unavailable backend instead of silently degrading.

**Storage and governance**

- Storage abstraction with three backends: versioned local Parquet (immutable version
  directories plus a manifest, point-in-time reads, overwrite history and `vacuum`), Delta
  Lake (three-part Unity Catalog naming, time travel through `versionAsOf` and
  `DESCRIBE HISTORY`, `OPTIMIZE` / `ZORDER BY`) and an in-memory store for tests.
- Medallion layout (bronze/silver/gold) with the Silver `patient_states` contract defined
  once in `SILVER_COLUMNS`, shared by the pandas writer, the Spark `StructType` and the Delta
  writer, and a schema version for time-travel interpretability.
- Unity Catalog naming, idempotent DDL, per-agent grants and an agent-to-table-to-report
  lineage graph with Mermaid rendering.

**LLM layer**

- Offline deterministic provider as the default (no credentials, no network), plus
  LangChain-backed Bedrock/OpenAI providers with configurable model ids.
- Resilience stack: response cache keyed on provider, model, temperature and prompt
  (append-only JSONL with prompt hashes), token-bucket rate limiting, timeout, exponential
  backoff retry with transient/permanent classification, and an offline fallback so a
  provider outage degrades rather than aborts a run.
- Persona contract enforced in code: JSON-only output schema, repair heuristics, a
  medical-advice filter, CTCAE grade clamping, `source = "llm"` terms kept separable from
  modelled events, and a per-patient call cap.

**Analysis and reporting**

- SciPy-free statistical estimators checked against reference values: Wilson and Newcombe
  intervals, mean confidence intervals, Welch t-test, two-proportion z-test, Fisher exact
  test, Mann-Whitney U, Benjamini-Hochberg q-values, bootstrap difference intervals and
  Cohen's d.
- DSMB stopping-rule evaluation per rule and arm with threshold monitors, safety alerts and
  risk differences, plus pre-specified endpoints with MCID and responder thresholds.
- Reports in Markdown, HTML and JSON with an embedded synthetic-data disclaimer, and
  CDISC-inspired SDTM/ADaM-subset exports (DM, EX, VS, AE, ADSL, ADVS, ADAE) with
  `define.json`.
- MLflow tracking of params, metrics, artifacts and the report payload, with failure-tolerant
  degradation, plus span-level LLM tracing (tokens, latency percentiles, cache hits) exposed
  through `insilico-trial traces`.

**User interface**

- Readout dashboard: one self-contained HTML file per run with inline SVG charts and no
  external assets — Overview (KPIs, primary endpoint with confidence intervals, forest plot,
  CONSORT flow, safety signals), Efficacy, Safety, Patients (individual trajectories and
  verbatim persona narration), Reproducibility (provenance, digests, replay command,
  data-quality audit) and Lineage & traces.
- Live Studio (`insilico-trial studio`): a stdlib HTTP server on loopback that launches the
  real pipeline, reports phase progress and embeds the finished dashboard, with input
  validation, a protocol allow-list, path-traversal rejection and artifact type restrictions.
- `python scripts/render_ui_previews.py` regenerates the checked-in previews
  (`docs/dashboard-preview.html`, `docs/studio-preview.html`).

**Infrastructure**

- Terraform module for AWS + Databricks: Unity Catalog catalog/schemas, per-agent service
  principals and grants, S3 storage credential and external location, managed locations with
  lifecycle and encryption settings, and SQL warehouses.
- Databricks Asset Bundle with `dev` and `prod` targets and a three-task simulation job
  (`validate_protocol`, `simulate_trial`, `train_physiology_model`), plus a deploy script.

**Developer experience**

- `insilico-trial` CLI with twelve subcommands (including `env-check`, `validate-protocol`,
  `generate-cohort`, `simulate`, `report`, `time-travel`, `lineage`, `traces`,
  `train-physiology`, `dashboard`, `studio` and `demo`); every command supports `--json`.
- Configuration through YAML profiles in `conf/` with environment-variable overrides, three
  shipped profiles (laptop/CI, AWS Databricks, Databricks Community Edition) and an
  illustrative phase II dose-finding protocol.
- 206 tests: 204 run without a JVM (`make test`), and the remaining 2 prove Spark engine
  equivalence (`make test-spark`). `make check` adds ruff and mypy, `make verify-repro`
  proves two same-seed runs are identical, `make calibrate` checks the clinical plausibility
  bands, `make notebook` executes every notebook cell headlessly, `make verify-ui` covers the
  dashboard and Studio endpoints and `make iac-validate` covers Terraform. CI runs the suite
  on Python 3.10, 3.11 and 3.12.
- Documentation set: architecture (with a test index for every claim), data model, UI design
  record, responsible-use and limitations statement, Databricks/AWS runbook and a
  specification-compliance mapping.

[Unreleased]: https://github.com/SergeyGer/InSilicoTrial_MAS/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/SergeyGer/InSilicoTrial_MAS/releases/tag/v1.0.0
