# InSilicoTrial MAS — Data Model and Contracts

Scope: the tables, columns and payload shapes that the code actually reads and writes. Column lists are
taken from the source of truth in the package — `schemas.SILVER_COLUMNS`, `cohort.serialization.COHORT_COLUMNS`,
`governance.namespace.LAYER_TABLES`, `governance.lineage.LINEAGE_COLUMNS`,
`agents.biostatistician_agent.AE_COLUMNS` and `reporting.cdisc.VARIABLE_LABELS` — not from an external
specification. All data is synthetic: no real patient records exist anywhere in this model.

## 1. Medallion layers

| Layer | Logical key | Purpose | Written by |
| --- | --- | --- | --- |
| Bronze | `bronze/<table>` | Raw, immutable inputs: the screened/randomised synthetic cohort and the protocol definition used. | `pipeline.TrialSimulationPipeline._write_bronze()` |
| Silver | `silver/<table>` | Cleaned per-epoch patient states plus derived event tables. One row per patient per epoch. | `_write_silver()`; derived tables in `_write_derived_silver()` (Delta backend only) |
| Gold | `gold/<table>` | Biostatistician aggregates, safety analytics and the run manifest / lineage graph. | `_write_gold()` |

Storage location is resolved by `UnityCatalogNamespace.logical(layer, name)` → `"<schema>/<name>"`, then by
the store: `LocalVersionedStore` writes `<local_root>/<schema>/<name>/`, `DeltaStore` writes
`<catalog>.<schema>.<name>` (or `<root_uri>/<schema>/<name>` when `storage.root_uri` is set).

## 2. Table inventory

Every table below was checked against `governance/namespace.py::LAYER_TABLES` and against the pipeline's
`self.namespace.logical(...)` call sites.

| Table | Declared in `LAYER_TABLES` | Written by the current pipeline | Notes |
| --- | --- | --- | --- |
| `bronze/synthetic_cohort` | yes | yes | Cohort frame + `sim_run_id`; also copied to `<run_dir>/cohort.parquet`. |
| `bronze/protocol_definitions` | yes | yes | One row: the protocol JSON (`TrialProtocol.model_dump(mode="json")`) + `sim_run_id`. Nested fields are JSON-encoded on write. |
| `bronze/llm_raw_traces` | yes | yes, when tracing is enabled | The persona spans of the run (`TraceStore.as_rows(run_id=...)`) copied from `tracking.trace_path`; written by `TrialSimulationPipeline._write_llm_raw_traces`. Empty when `tracking.enabled: false`, `tracking.trace_path` is unset, or no persona was narrated. |
| `silver/patient_states` | yes | yes | The 50-column contract in section 3. |
| `silver/adverse_events` | yes | yes | Exploded `adverse_events_json` via `extract_adverse_events` + `sim_run_id`; written on every backend by `_write_derived_silver`. |
| `silver/protocol_deviations` | yes | yes, when somebody discontinued | One row per discontinued patient: `patient_id`, `cohort_id`, `arm_id`, `epoch`, `discontinuation_reason`, `reason`, `sim_run_id`. |
| `silver/screen_failures` | yes | yes, when the protocol excludes anybody | One row per screened-out patient: `patient_id`, `cohort_id`, `reason`, `age`, `egfr`, `alt_u_l`, `comorbidities`, `sim_run_id`. Also summarised in the run manifest (`screening.failure_reasons`). |
| `gold/arm_summaries` | yes | yes | Section 6.1. |
| `gold/endpoint_comparisons` | yes | yes | Section 6.2. |
| `gold/safety_summary` | yes | yes | Section 6.3. |
| `gold/stopping_rule_evaluations` | yes | yes | Section 6.4. |
| `gold/run_manifest` | yes | yes | Section 7. |
| `gold/lineage_edges` | yes | yes | Section 8. |

Every Gold write adds `sim_run_id` to the frame before writing (`pipeline._write_gold`).

## 3. `silver/patient_states` — the `SILVER_COLUMNS` contract

`SILVER_COLUMNS` (`schemas.py`) is a tuple of `(name, type)` pairs and **contains 50 columns**.
`SILVER_COLUMN_NAMES` is derived from it; `PatientStateObservation.as_row()` returns a dict in exactly
that order. The pandas writer (`engine/partition.py::rows_to_frame`), the Spark `StructType`
(`engine/spark_runner.py::silver_struct_type`) and the Delta writer all derive from it.

Type mapping used by each writer:

| Declared type | pandas dtype (`PANDAS_DTYPES`) | Spark type (`DTYPE_MAP`) |
| --- | --- | --- |
| `string` | `object` (NaN → `""`) | `StringType` |
| `double` | `float64` | `DoubleType` |
| `int` | `int64` | `IntegerType` |
| `long` | `int64` | `LongType` |
| `bool` | `bool` | `BooleanType` |

| # | Column | Type | Meaning / source in code |
| --- | --- | --- | --- |
| 1 | `observation_id` | string | `f"{patient_id}:{epoch:03d}"` — stable row key. |
| 2 | `sim_run_id` | string | Run identifier (`RUN-<UTC stamp>-<hash6>` or a caller-supplied `--run-id`). |
| 3 | `protocol_id` | string | `TrialProtocol.protocol_id`. |
| 4 | `protocol_version` | string | `TrialProtocol.version` (defaults to `PROTOCOL_SCHEMA_VERSION = "1.0.0"`). |
| 5 | `patient_id` | string | Synthetic patient id, `PT-<hash10>`. |
| 6 | `cohort_id` | string | `COHORT-001` … assigned round-robin by `CohortGenerator.iter_profiles`. |
| 7 | `site_id` | string | Site sampled from `resources/genomic_priors.yaml` (`site_ids`). |
| 8 | `arm_id` | string | Randomised arm (`context.arm_of(patient_id)`). |
| 9 | `arm_label` | string | Human label from `ArmSpec.label`. |
| 10 | `epoch` | int | 0 = untreated baseline (when `include_baseline_epoch`), 1..N = dosing epochs. |
| 11 | `time_hours` | double | `epoch * protocol.epoch_duration_hours`. |
| 12 | `dose_mg` | double | Dose actually administered this epoch (0 for placebo, or while withheld after discontinuation). |
| 13 | `plasma_conc_mg_l` | double | `ExposureMetrics.c_avg_mg_l`, rounded to 6 dp. |
| 14 | `auc_epoch_mg_h_l` | double | `F * dose / CL` for the epoch, rounded to 6 dp. |
| 15 | `cumulative_exposure` | double | Running sum of `auc_epoch_mg_h_l`. |
| 16 | `sbp` | double | Predicted systolic BP (mmHg) including placebo, progression and measurement noise. |
| 17 | `dbp` | double | Predicted diastolic BP (mmHg). |
| 18 | `hr` | double | Predicted heart rate (bpm). |
| 19 | `qtc_ms` | double | Predicted QTc (ms). |
| 20 | `alt_u_l` | double | Predicted ALT (U/L). |
| 21 | `ast_u_l` | double | Predicted AST (U/L). |
| 22 | `creatinine_mg_dl` | double | Predicted creatinine (mg/dL), scaled by the eGFR ratio. |
| 23 | `egfr` | double | Predicted eGFR (mL/min/1.73 m²), floored at 5.0. |
| 24 | `sbp_change` | double | `sbp - profile.baseline_sbp`, rounded to 2 dp. |
| 25 | `dbp_change` | double | `dbp - profile.baseline_dbp`. |
| 26 | `hr_change` | double | `hr - profile.baseline_hr`. |
| 27 | `biomarker_composite` | double | Weighted composite (BP, HR, QTc, ALT ratio); higher is better. |
| 28 | `responder` | bool | Set only at the responder endpoint's evaluation epoch (`endpoint.epoch` or the final epoch); false elsewhere. |
| 29 | `worst_ctcae_grade` | int | Max CTCAE grade across the row's adverse events (0 if none). |
| 30 | `n_adverse_events` | int | Length of the decoded `adverse_events_json` after LLM merging. |
| 31 | `adverse_events_json` | string | JSON array — shape in section 4.1. |
| 32 | `symptoms_json` | string | JSON array — shape in section 4.2. |
| 33 | `symptom_summary` | string | `"; ".join(f"{term} (G{grade})")` sorted by descending grade, truncated to 480 characters. |
| 34 | `discontinued` | bool | True from the epoch a discontinuation rule fires (and for every later epoch). |
| 35 | `discontinuation_reason` | string | `"CTCAE grade <n> adverse event"` or the recorded withdrawal reason. |
| 36 | `adherence_intent` | string | `continue`, `unsure` or `discontinue`, taken from the persona's parsed answer. Only populated on narrated rows; otherwise `continue`. |
| 37 | `llm_used` | bool | True when a persona LLM response was merged into this row. |
| 38 | `llm_provider` | string | Provider name from `LLMResponse.provider`. |
| 39 | `llm_model` | string | Model id from `LLMResponse.model`. |
| 40 | `llm_latency_ms` | double | Provider latency; one of the two `NON_DETERMINISTIC_COLUMNS`. |
| 41 | `llm_tokens_in` | int | Estimated/actual prompt tokens. |
| 42 | `llm_tokens_out` | int | Estimated/actual completion tokens. |
| 43 | `llm_cache_hit` | bool | True when the response came from `LLMResponseCache`. |
| 44 | `llm_attempts` | int | Attempt count reported by `ResilientLLMClient`. |
| 45 | `llm_error` | string | Typed error string; empty on success. |
| 46 | `prompt_hash` | string | `stable_hash(system, user, length=16)` of the exact prompt used. |
| 47 | `physiology_model_version` | string | e.g. `mechanistic-v1` or `ridge-residual-v1-<digest8>`. |
| 48 | `physiology_backend` | string | `mechanistic` or `ridge`. |
| 49 | `rng_seed` | long | The master `context.seed` (per-draw seeds are derived from it, never stored individually). |
| 50 | `observation_ts` | string | ISO-8601 UTC timestamp captured when the agent was constructed; the second `NON_DETERMINISTIC_COLUMNS` member. |

Two properties of these columns worth knowing before relying on them:

- `adherence_intent` is populated from the persona's parsed answer
  (`parse_symptom_response` -> `Narration.adherence_intent` -> `_apply_narration`). A persona that
  answers `"adherence_intent": "discontinue"` therefore ends dosing even with moderate symptoms
  (`_discontinuation_reason`), which is verified by
  `tests/test_agents.py::test_persona_adherence_intent_reaches_the_silver_row`. Rows that were never
  narrated keep the default `"continue"`, so the column must not be read as "patient agreed to
  continue" for non-narrated epochs.
- `observation_ts` is per-agent, not per-epoch: it is captured once in `PatientPersonaAgent.__init__`
  and written to every epoch row of that patient.

## 4. JSON payload shapes

### 4.1 `adverse_events_json`

A JSON array of event objects. Modelled events come from
`ml/physiology.py::sample_adverse_events`; LLM-only terms are appended in
`PatientPersonaAgent._apply_narration`; matched terms are upgraded in place with `source = "hybrid"`.

| Key | Type | Source and meaning |
| --- | --- | --- |
| `ae_id` | string | `stable_hash(run_id, patient_id, epoch, term, length=20)` — identical for model and LLM-only rows. |
| `patient_id` | string | Patient id. |
| `arm_id` | string | Filled in by the Patient agent after sampling. |
| `epoch` | int | Epoch of onset (in this model, onset = the epoch of the draw). |
| `term` | string | MedDRA-style preferred term (drug `ae_models[].term`, or the LLM's term). |
| `soc` | string | System organ class; LLM-only terms use `"Reported by patient (LLM)"`. |
| `ctcae_grade` | int | 1–5. |
| `serious` | bool | `ae.serious and grade >= 3` for modelled events; `grade >= 3` for LLM-only. |
| `relatedness` | string | One of `not_related`, `unlikely`, `possible`, `probable`, `definite`; sampled from the predicted probability band. |
| `predicted_probability` | float | Model probability rounded to 6 dp; `0.0` for LLM-only terms. |
| `reported_verbatim` | string | Patient-voice quote when the LLM reported the term. |
| `source` | string | One of `model`, `llm`, `hybrid`, `offline`. |

### 4.2 `symptoms_json`

A JSON array of `SymptomReport` objects (`schemas.py`), i.e. only the qualitative narration, capped at
5 per row after parsing and at 3 when merged.

| Key | Type | Meaning |
| --- | --- | --- |
| `term` | string | Short clinical term (truncated to 120 chars). |
| `ctcae_grade` | int | 1–5, coerced from numbers or words (`mild`, `moderate`, `severe`, `life-threatening`, `fatal`). |
| `verbatim` | string | Patient-voice quote (truncated to 400 chars). |
| `source` | string | `llm` for provider output; `model`, `hybrid`, `offline` are also valid literals. |

The provider contract that produces these objects is `SYMPTOM_JSON_SCHEMA` in `llm/prompts.py`:
`{"symptoms": [{"term", "ctcae_grade", "verbatim"}], "overall_tolerability": "good|acceptable|poor",
"adherence_intent": "continue|unsure|discontinue"}`.

## 5. `bronze/synthetic_cohort` — the `COHORT_COLUMNS` contract

`COHORT_COLUMNS` (`cohort/serialization.py`) has **30 columns**; the pipeline adds `sim_run_id` on
write. `SparkEngine._to_spark()` additionally appends a transient `partition_key` column (and
`cohort_struct_type()` declares it) so that `applyInPandas` can group cohorts; `partition_key` is not
part of the persisted Bronze contract.

| # | Column | Type | Meaning |
| --- | --- | --- | --- |
| 1 | `patient_id` | string | `PT-<hash10>`. |
| 2 | `cohort_id` | string | Enrolment cohort. |
| 3 | `site_id` | string | Site. |
| 4 | `arm_id` | string | Randomised arm (empty until randomisation). |
| 5 | `enrolled` | bool | Eligibility outcome. |
| 6 | `age` | double | Years. |
| 7 | `sex` | string | `F` / `M`. |
| 8 | `weight_kg` | double | kg. |
| 9 | `height_cm` | double | cm. |
| 10 | `bmi` | double | kg/m². |
| 11 | `baseline_sbp` | double | mmHg. |
| 12 | `baseline_dbp` | double | mmHg. |
| 13 | `baseline_hr` | double | bpm. |
| 14 | `egfr` | double | mL/min/1.73 m² (CKD-EPI-2021-style, illustrative constants). |
| 15 | `alt_u_l` | double | U/L. |
| 16 | `ast_u_l` | double | U/L. |
| 17 | `creatinine_mg_dl` | double | mg/dL. |
| 18 | `comorbidities_json` | string | JSON array of comorbidity tokens. |
| 19 | `concomitant_meds_json` | string | JSON array of medication tokens. |
| 20 | `medical_history` | string | Narrative persona text used as the LLM prompt's history slot. |
| 21 | `smoking` | string | `never` / `former` / `current`. |
| 22 | `alcohol_units_week` | double | Units per week. |
| 23 | `ancestry` | string | Synthetic ancestry group (`EUR`, `AFR`, `EAS`, `SAS`, `AMR`). |
| 24 | `cyp2d6` | string | `PM` / `IM` / `NM` / `UM`. |
| 25 | `cyp3a4` | string | `PM` / `IM` / `NM` / `UM`. |
| 26 | `hla_b_57_01` | bool | Synthetic carrier flag. |
| 27 | `hla_dq2_2` | bool | Synthetic carrier flag. |
| 28 | `adrb1_arg389gly` | bool | Synthetic carrier flag. |
| 29 | `ace_dd` | bool | Synthetic carrier flag. |
| 30 | `slco1b1_decreased` | bool | Synthetic reduced-function flag. |

`frame_to_profiles` / `profiles_to_frame` implement the round trip, with tolerant parsing
(`_as_list`, `_as_bool`, `_as_float`, `_as_str`) and documented defaults for missing values
(age 55, weight 75 kg, eGFR 90, ALT 25 U/L, …). Round-trip fidelity is covered by
`tests/test_cohort.py::test_serialisation_round_trip` and `::test_row_parsing_tolerates_missing_and_nan_values`.

## 6. Gold tables

### 6.1 `gold/arm_summaries`

One row per arm, from the `ArmSummary` dataclass, plus `sim_run_id`. The `extra` mapping is JSON-encoded
by `storage.base.sanitise_for_parquet`.

| Column | Type | Meaning |
| --- | --- | --- |
| `arm_id`, `label` | string | Arm identity. |
| `n_patients`, `n_observations` | int | Unique patients at end of treatment; total Silver rows for the arm. |
| `dose_mg` | double | Nominal arm dose. |
| `mean_sbp_change`, `sd_sbp_change` | double | End-of-treatment SBP change. |
| `mean_dbp_change`, `mean_hr_change` | double | End-of-treatment DBP / HR change. |
| `responder_rate` | double | Responders / patients. |
| `ae_rate` | double | Patients with at least one AE / patients. |
| `grade3_plus_rate` | double | Patients with any grade ≥ 3 event / patients. |
| `sae_rate` | double | Patients with any serious event / patients. |
| `mortality_rate` | double | Patients with any grade 5 event / patients. |
| `mean_alt_ratio` | double | Mean end-of-treatment ALT / baseline ALT. |
| `discontinuations` | int | Discontinued patients at end of treatment. |
| `extra` | string (JSON) | `is_control`, `responder_ci_low/high`, `grade3_plus_ci_low/high` (Wilson intervals). |
| `sim_run_id` | string | Run id. |

### 6.2 `gold/endpoint_comparisons`

One row per treatment arm × endpoint, from `ComparisonResult`, plus `sim_run_id`.

| Column | Meaning |
| --- | --- |
| `endpoint` | Endpoint name. |
| `arm_id`, `control_arm_id` | Compared arms. |
| `metric` | Silver column analysed. |
| `effect_estimate` | Risk difference (binary) or mean difference (continuous). |
| `ci_low`, `ci_high` | Newcombe difference interval (binary) or bootstrap CI (continuous, 2000 resamples seeded from `context.seed`). |
| `p_value` | Two-proportion z-test / Fisher exact (binary) or Welch t-test (continuous). |
| `test` | Test label, e.g. `"Welch t-test (bootstrap CI)"`. |
| `n_treatment`, `n_control` | Sample sizes. |
| `mcid`, `meets_mcid` | Minimal clinically important difference and whether it is met. |
| `q_value` | Benjamini-Hochberg adjusted p across the secondary-endpoint family (`null` for the primary endpoint). |
| `sim_run_id` | Run id. |

### 6.3 `gold/safety_summary`

One row per arm × term, from `BiostatisticianAgent._safety_summary`, plus `sim_run_id`. Rows are sorted
by descending rate then term.

| Column | Meaning |
| --- | --- |
| `arm_id`, `term`, `soc` | Event identity. |
| `n_patients`, `n_events`, `n_patients_with_event` | Denominators and counts. |
| `rate`, `control_rate`, `risk_difference` | Patient-level incidence and difference vs control. |
| `rd_ci_low`, `rd_ci_high` | Newcombe interval for the risk difference. |
| `p_value`, `q_value` | Fisher exact p, Benjamini-Hochberg q across terms. |
| `grade3_plus`, `grade5`, `serious` | Patient counts at each severity level. |
| `llm_reported_patients` | Patients whose event had `source` in `{llm, hybrid}`. |
| `mean_predicted_probability` | Mean model probability for the term. |
| `grade_distribution` | JSON object `{"1": n, ...}` of grade counts. |
| `sim_run_id` | Run id. |

### 6.4 `gold/stopping_rule_evaluations`

One row per stopping rule × arm × epoch from `StoppingRuleEvaluation`, plus `sim_run_id`.

| Column | Meaning |
| --- | --- |
| `rule_id`, `epoch`, `arm_id` | Evaluation key. |
| `metric` | `grade3_plus_rate`, `sae_rate`, `mortality_rate` or `alt_elevation_rate`. |
| `observed` | Cumulative metric up to and including `epoch`. |
| `threshold`, `comparator` | Rule definition. |
| `triggered` | Comparison result. |
| `action` | `review`, `pause` or `stop`. |
| `n_at_risk` | Patients in the arm at that epoch. |
| `sim_run_id` | Run id. |

The `alt_elevation_rate` metric counts patients with `alt_u_l >= 3.0 * baseline_alt` **and**
`alt_u_l > 100.0` (`BiostatisticianAgent._rule_metric`).

## 7. Run manifest

Two artefacts share the name "manifest":

1. `gold/run_manifest` — a single-row DataFrame built from `SimulationProvenance.as_dict()` plus
   `sim_run_id`. This is the queryable provenance record:

   | Group | Fields (`SimulationProvenance`) |
   | --- | --- |
   | Identity | `sim_run_id`, `protocol_id`, `protocol_digest`, `protocol_version`, `package_version`, `git_revision` |
   | Engine | `engine_backend`, `spark_version`, `master_url`, `seed`, `n_patients`, `n_cohorts`, `n_epochs` |
   | Model | `physiology_model_version`, `physiology_model_digest`, `physiology_backend` |
   | LLM | `llm_provider`, `llm_model`, `llm_temperature`, `llm_mode`, `total_llm_calls`, `llm_cache_hits`, `total_tokens_in`, `total_tokens_out` |
   | Storage | `storage_backend`, `silver_location` |
   | Timing / notes | `started_at`, `finished_at`, `duration_seconds`, `notes` |

2. `<output_dir>/<run_id>/run_manifest.json` — the full replay document written at the end of
   `TrialSimulationPipeline.run()`:

   | Top-level key | Content |
   | --- | --- |
   | `run_id`, `silver_schema_version`, `package_version`, `git_revision`, `created_at` | Build identity. |
   | `provenance` | The `SimulationProvenance` dict above. |
   | `protocol` | Full protocol JSON. |
   | `config` | Full `SimulationConfig` dict (every section: engine, llm, ml, storage, tracking). |
   | `cohort` | `cohort_summary(enrolled)` — means, ancestry distribution, PM/HLA fractions, digest. |
   | `screening`, `allocation` | Screen-failure summary and per-arm counts. |
   | `estimate` | `cohort_size_estimate()` — rows, LLM calls, Spark partitions, epochs. |
   | `engine` | Resolved plan, engine stats, backend. |
   | `storage` | Backend, Silver `WriteResult`, Gold table locations, Bronze result. |
   | `environment` | `EnvironmentReport.as_dict()`. |
   | `traces` | `TraceStore.summary(run_id=...)`. |
   | `analysis_overview`, `report` | Headline metrics and report artefact paths. |
   | `cdisc` | Export directory, dataset paths, define path, row counts. |
   | `lineage`, `lineage_graph`, `lineage_mermaid` | Lineage summary, full node/edge graph, Mermaid source. |
   | `protocol_warnings` | Warnings from `ProtocolAgent.validate()`. |
   | `replay` | Suggested command, seed and protocol digest. |

   The run directory also contains `cohort.parquet`, `silver_observations.parquet`,
   `report/trial_report.{md,html,json}` and (when `export_cdisc` is true) `cdisc/*.csv` + `cdisc/define.json`.

## 8. `gold/lineage_edges`

`LineageTracker.to_frame()` produces exactly `LINEAGE_COLUMNS`
(`source`, `target`, `relation`, `agent`, `run_id`, `details_json`, `created_at`); the pipeline adds
`sim_run_id`.

| Column | Meaning |
| --- | --- |
| `source`, `target` | Node ids, e.g. `agents.protocol`, `bronze.synthetic_cohort`, `silver.patient_states`, `model:<version>`, `report.trial_report`. |
| `relation` | `produces`, `parameterises`, `derives_from`, `predicts`, `summarised_by`, … |
| `agent` | Producing agent role. |
| `run_id` | Run id stored on the edge by `LineageTracker`. |
| `details_json` | JSON object of edge metadata (e.g. protocol digest, model digest). |
| `created_at` | Tracker creation timestamp. |
| `sim_run_id` | Run id added by the pipeline. |

## 9. CDISC-inspired exports

`reporting/cdisc.py::export_cdisc(...)` writes seven CSV datasets plus `define.json` into
`<run_dir>/cdisc/`. `STUDY_ID = "INSILICO-001"`; every subject id is `INSILICO-001-<patient_id>`.

| Dataset | File | Variables (exact labels from `VARIABLE_LABELS`) |
| --- | --- | --- |
| DM | `dm.csv` | `STUDYID` Study identifier; `USUBJID` Unique subject identifier; `SITEID` Study site identifier; `ARM` Planned arm; `ARMCD` Planned arm code; `AGE` Age at enrolment (years); `SEX` Sex; `RACE` Ancestry group (synthetic); `BMIBL` Baseline BMI (kg/m2); `COMORBN` Number of baseline comorbidities; `CYP2D6` CYP2D6 metaboliser phenotype (synthetic); `HLAB5701` HLA-B\*57:01 carrier status (synthetic) |
| EX | `ex.csv` | `STUDYID` Study identifier; `USUBJID` Unique subject identifier; `EXTRT` Name of study treatment; `EXDOSE` Dose administered (mg); `EXDOSU` Dose units; `EPOCH` Simulated dosing epoch; `EXSTDTC` Simulated start of dose (hours from first dose) |
| VS | `vs.csv` | `STUDYID` Study identifier; `USUBJID` Unique subject identifier; `EPOCH` Simulated epoch; `VSTESTCD` Vital signs test code; `VSORRES` Result; `VSORRESU` Result units; `VSBLFL` Baseline flag |
| AE | `ae.csv` | `STUDYID` Study identifier; `USUBJID` Unique subject identifier; `AETERM` Reported term for the adverse event; `AESOC` System organ class; `AETOXGR` CTCAE grade; `AESER` Serious event flag; `AEREL` Causality; `AESOURCE` Origin of the report (model, llm, hybrid); `AEPREDP` Model-predicted probability; `AESTDTC` Simulated onset epoch |
| ADSL | `adsl.csv` | `STUDYID` Study identifier; `USUBJID` Unique subject identifier; `TRT01P` Planned treatment; `TRT01A` Actual treatment; `SAFFL` Safety population flag; `ITTFL` Intention-to-treat flag; `DISCONFL` Discontinued from treatment flag; `DCSREAS` Reason for discontinuation |
| ADVS | `advs.csv` | `STUDYID` Study identifier; `USUBJID` Unique subject identifier; `PARAMCD` Parameter code; `AVAL` Analysis value; `CHG` Change from baseline; `EPOCH` Simulated epoch |
| ADAE | `adae.csv` | `STUDYID` Study identifier; `USUBJID` Unique subject identifier; `TRTA` Actual treatment; `AEDECOD` Dictionary-derived term; `AETOXGR` CTCG grade; `TRTEMFL` Treatment-emergent flag; `AESER` Serious flag |

Notes on the derived datasets:

- `VS` emits one row per patient-epoch per test code: `SYSBP` (mmHg), `DIABP` (mmHg), `HR` (beats/min),
  `QTcF` (ms), `ALT` (U/L); `VSBLFL` is `Y` at epoch 0.
- `ADVS` parameter codes are `SYSBP`, `DIABP`, `HR`, `COMPOSITE`; `CHG` comes from the matching
  `*_change` column (null for `COMPOSITE`).
- `ADSL` is one row per patient at the last simulated epoch; `SAFFL` and `ITTFL` are always `Y`.
- `TRTEMFL` in `ADAE` is `Y` for `epoch > 0`.
- `define.json` contains `study`, `standard` (`"CDISC-inspired SDTM/ADaM subset (structural contract
  only, simulated data)"`), `generated_by` and, per dataset, `name`, `rows` and the `variables` list of
  `{name, label}` pairs. It is a JSON data-definition file, not a CDISC Define-XML document.

The module states its own posture: this is the *structure* of a submission package, not a substitute
for one.

## 10. Versioning, time travel and history queries

### 10.1 Local versioned store

Layout written by `LocalVersionedStore`:

```
<local_root>/<schema>/<table>/_manifest.json
<local_root>/<schema>/<table>/_version=000001/part-00000.parquet
<local_root>/<schema>/<table>/_version=000002/arm_id=placebo/part-00000.parquet   # when partitioned
```

Every write creates a new immutable `_version=NNNNNN` directory and appends one entry to
`_manifest.json`:

| Manifest key | Meaning |
| --- | --- |
| `version` | 1-based, monotonically increasing per table. |
| `timestamp` | UTC ISO-8601 write time. |
| `run_id` | Run that produced the version. |
| `mode` | `append` or `overwrite`. |
| `rows`, `columns` | Row count and column list of the version. |
| `partition_by` | Hive partition columns used for this write. |
| `path` | Version directory. |
| `operation` | `APPEND` or `WRITE`. |
| `replaces_versions` | Present on an `overwrite`: the versions it shadows. |

Retention: `_save_manifest(..., keep=storage.time_travel_versions_kept)` trims the manifest to the newest
N versions (0 disables trimming). `vacuum(table, keep=2)` deletes older version directories and rewrites
the manifest.

Read semantics:

| Call | Behaviour |
| --- | --- |
| `store.read(table)` | Latest effective state: all versions since the last `overwrite`. |
| `store.read(table, version=n)` / `read_as_of(table, n)` | Exact point-in-time read; raises `StorageError` if the version is unknown. |
| `store.read(table, run_id=...)` | Concatenates only the versions written by that run; raises if none match. |
| `store.history(table, limit=n)` | Newest-first version history (Delta `DESCRIBE HISTORY` equivalent). |
| `store.list_tables()` | Tables discovered by globbing `*/_manifest.json`. |

### 10.2 Delta store

`DeltaStore.read(version=n)` sets the Delta reader option `versionAsOf`; `history()` runs
`DESCRIBE HISTORY <table> LIMIT n` and normalises the result; `read_spark()` returns a lazy DataFrame
for large tables; `optimize(table, zorder_by=[...])` compacts files best-effort.

### 10.3 Querying history from the CLI

```bash
# Version history of the Silver table
insilico-trial time-travel --table silver/patient_states --history

# Point-in-time read (VERSION AS OF)
insilico-trial time-travel --table silver/patient_states --version 1 --where "sbp > 140" --limit 20

# Only the versions produced by one run
insilico-trial time-travel --table silver/patient_states --run-id RUN-20260101T000000-ABCDEF

# Machine-readable
insilico-trial time-travel --table gold/arm_summaries --history --json

# Rebuild a report from a finished run directory
insilico-trial report --run artifacts/<RUN-ID> --format markdown --format html
```

The `--table` argument takes the logical `layer/name` form, which both stores accept; `LocalVersionedStore`
maps it to a path and `DeltaStore` maps it to a three-part name (or a `root_uri` path).

### 10.4 Schema versioning

`version.py` holds `SILVER_SCHEMA_VERSION = "1.0.0"` and `PROTOCOL_SCHEMA_VERSION = "1.0.0"`. The Silver
schema version is embedded in `run_manifest.json` so a historical read stays interpretable after a
column change; `engine/context.py` declares `CONTEXT_SCHEMA_VERSION = "1.0.0"` and rejects a worker
payload from a different version rather than guessing.
