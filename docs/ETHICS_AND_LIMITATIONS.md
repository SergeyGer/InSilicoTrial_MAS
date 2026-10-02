# InSilicoTrial MAS — Responsible Use, Ethics and Limitations

Status: this document describes the guarantees and the gaps of the shipped code
(`insilico-trial-mas` 1.0.0). It is a responsible-use statement, not a regulatory opinion. The platform
is a decision-support simulator for trial design; it is not a source of clinical evidence, not a medical
device claim, and not a submission package.

## 1. Synthetic-data-only guarantee

| Claim | How the code enforces it |
| --- | --- |
| No protected health information | No module reads an external patient dataset. The only inputs are YAML files: protocol definitions such as `conf/trial_protocol_demo.yaml`, simulation configs under `conf/`, and the population-level frequencies in `src/insilico_trial_mas/resources/genomic_priors.yaml`. |
| Patients are generated, not sampled | `cohort/generator.py::CohortGenerator` synthesises every `PatientProfile` from the priors with `rng_for(seed, protocol_id, "cohort")`. `patient_id` is the uppercased 10-character `stable_hash(seed, protocol_id, index)`. |
| No re-identification path | There is no join key to any real record; the strongest identifiers are a synthetic patient id and a randomisation stratum. |
| Outputs are labelled synthetic | The generated report and JSON payload carry `reporting/report.py::DISCLAIMER`, which states the document is produced from fully synthetic patient data and must not be used as clinical evidence, a regulatory submission or medical advice. CDISC exports are written with `STUDY_ID = "INSILICO-001"` and a `define.json` whose `standard` field says "structural contract only, simulated data". |

Two residual concerns even with synthetic data:

- The genomic markers are fictional *frequencies*, but they carry real population labels
  (`EUR`, `AFR`, `EAS`, `SAS`, `AMR`). Outputs that break results down by ancestry can be read as claims
  about real populations; review any such table before publishing it.
- `PatientProfile.persona_text()` and the LLM prompt include ancestry and genotype notes. They are
  synthetic attributes, but the text is realistic enough that it should be handled as sensitive-looking
  material in demos and screenshots.

## 2. Population priors and drug parameters are illustrative

Nothing in the model is a validated epidemiological or pharmacological source. The priors file states
this in its own header: the values are "order-of-magnitude consistent with published ... literature, but
they are NOT a validated epidemiological source and MUST be replaced with a licensed reference dataset
(e.g. gnomAD, UK Biobank, NHANES) before any use in a regulatory submission."

| Parameter family | Where it lives | What it is today | What must replace it for regulated use |
| --- | --- | --- | --- |
| Ancestry mixture | `resources/genomic_priors.yaml` → `ancestries` (`EUR 0.45`, `AFR 0.20`, `EAS 0.15`, `SAS 0.12`, `AMR 0.08`) | Illustrative global mixture | A recruitment-plan-specific mixture with a documented source. |
| Pharmacogenomic frequencies | `cyp2d6_by_ancestry`, `cyp3a4_by_ancestry`, `marker_frequencies` (HLA-B\*57:01, HLA-DQ2.2, ADRB1, ACE, SLCO1B1) | Illustrative carrier rates | Licensed allele-frequency reference data, plus a plan for unmeasured genotypes. |
| Physiology priors | `physiology` (height, weight, BP, HR, ALT, AST, creatinine, eGFR), `comorbidities` (base rate + age/BMI/smoking multipliers), smoking distribution (0.55/0.28/0.17), site list | Illustrative | Real baseline characteristics from the indication, with covariate correlations modelled, not assumed independent. |
| Drug PK/PD | `TrialProtocol.drug`: `half_life_h`, `clearance_ml_min_per_kg`, `volume_of_distribution_l_per_kg`, `ka_per_h`, `ec50_mg_l`, `hill`, `bioavailability`, `renal_fraction`, `hepatic_fraction`, `emax` profile | Defaults in `schemas.py` (`t_half 12 h`, `CL 1.20 mL/min/kg`, `V 0.60 L/kg`, `ka 1.0/h`, `EC50 1.0 mg/L`, Hill 1.0) unless overridden | Population PK/PD estimates from phase I/II studies, with between-subject variability and covariate effects, not point values. |
| Adverse-event models | `drug.ae_models[]`: `intercept`, `coefficients`, `exposure_slope`, `grade_distribution`, `serious` | Logistic approximations with monotone exposure effects | Incidences from the reference safety database / class labelling, with time-varying and idiosyncratic risk. |
| Measurement noise | `ml/physiology.py::MEASUREMENT_NOISE` (SBP 2.4, DBP 1.5, HR 2.0, QTc 5.0, ALT 8 %) | Illustrative device + biological variation | Device-specific and assay-specific error models. |
| Placebo / progression | `TrialProtocol.placebo_effect`, hard-coded progression terms (`0.20 * epoch` SBP, `0.10 * epoch` DBP) | Illustrative | Historical control data for the indication. |

The run manifest records `physiology_model_version`, `physiology_model_digest`, `physiology_backend`,
`llm_provider`, `llm_model`, `llm_temperature`, `llm_mode`, `seed` and `git_revision`, so a reviewer can
always see *which* illustrative configuration produced a number.

## 3. LLM safety filter and prompt constraints

The persona LLM is a narrator, never an adviser. The constraints are enforced in code, not only in the
prompt:

| Constraint | Implementation |
| --- | --- |
| The persona is explicitly not a clinician | `llm/prompts.py::PATIENT_PERSONA_SYSTEM_PROMPT`: "You are NOT a physician and you never give medical advice ... Answer with a single JSON object and nothing else." |
| Grounding in the simulated physiology | `PATIENT_PERSONA_TEMPLATE` supplies the model-predicted vitals and the highest-risk AE probability, and instructs: "Never invent a symptom that contradicts the physiological data above." |
| Output contract | An explicit JSON schema (`SYMPTOM_JSON_SCHEMA`), 0–3 symptoms, CTCAE grade definitions, "Output JSON only, no markdown, no commentary." |
| Medical-advice filter | `FORBIDDEN_MEDICAL_ADVICE = ("you should take", "i recommend", "diagnosis is", "prescribe", "stop taking your")`. Any response containing one of these markers is rejected wholesale with `parse_error = "response contained medical advice and was rejected by the safety filter"` and zero symptoms extracted. |
| Bounded, non-fatal parsing | `parse_symptom_response` never raises; it repairs common JSON mistakes, clamps grades to 1–5, truncates terms/verbatims and caps the list at 5. Failures become typed `parse_error`/`llm_error` values. |
| Blast radius | The LLM cannot change the mechanistic model output. It can escalate the CTCAE grade of an existing term (up to `source = "hybrid"`) and can add new terms with `source = "llm"` and `soc = "Reported by patient (LLM)"`. Both are retained for audit rather than silently dropped. |
| Cost ceiling per patient | `MAX_LLM_CALLS_PER_PATIENT = 6`; epoch 0 is never narrated. |
| Replayability | Prompts are hashed (`prompt_hash`), responses are cached in append-only JSONL (`llm.cache_path`), and spans are written to `tracking.trace_path`. |

Limits an ethics reviewer should note: the filter is a small marker list on the persona output, not a
general content-safety classifier; it does not inspect the prompt, the provider's hidden reasoning, or the
verbatim text that is stored in `symptoms_json` and re-emitted into reports. Real providers are
non-deterministic even at `temperature = 0`, so a narrative regenerated with a newer model version may
differ from the cached one — which is precisely why the cache and prompt hash exist.

## 4. What the model does not capture

The report template (`templates/report.md.j2`, section 10) already states five limitations; this is the
expanded list with the code evidence.

| Not modelled | Consequence | Code evidence |
| --- | --- | --- |
| Unmeasured confounding | The simulated treatment effect is the structural effect; no adjustment problem exists in-silico, so the pipeline cannot tell you whether a real analysis would be biased. | `ml/physiology.py` has no latent-confounder term; `pipeline.run()` writes no randomisation-bias diagnostics. |
| Idiosyncratic / immune-mediated toxicity | Toxicity is exposure-driven via logistic models; the only immune-flavoured modifiers are HLA flags on ALT/AST. Severe rare events can be under- or over-represented. | `MechanisticPhysiology.ae_logit`, `sample_adverse_events`. |
| Adherence behaviour beyond discontinuation | Patients take every scheduled dose until a discontinuation rule fires. Missed doses, partial adherence and re-starts are not simulated. | `PatientPersonaAgent.run()` sets `dose_mg = 0` from the epoch after `discontinue_from`; the persona's `adherence_intent` is persisted to Silver and can end dosing, but partial adherence is not modelled (see `docs/DATA_MODEL.md` section 3). |
| Site / country effects | `site_id` is sampled and recorded, and can be a randomisation stratum, but no site random effect, practice pattern or standard-of-care difference is modelled. | `CohortGenerator._make_profile` picks a site; no site term appears in any prediction. |
| Drug–drug interactions | Concomitant medications are recorded as text tokens only. | `concomitant_meds_json` in `bronze/synthetic_cohort`; no interaction term in `ml/pk_pd.py`. |
| Missing data and dropout mechanism | Every scheduled epoch produces a complete row; there is no MAR/MNAR mechanism, so missing-data methods are never exercised. | `PatientPersonaAgent.run()` appends one observation per epoch. |
| Long-term and cumulative safety | The horizon is `epochs × epoch_duration_hours` (warned about beyond one year by `ProtocolAgent.validate`); no carcinogenicity, teratogenicity, tolerance or rebound. | Protocol guard-rail warning in `agents/protocol_agent.py`. |
| Real protocol execution errors | The only deviations recorded are discontinuations; there are no visit-window, wrong-dose or kit-allocation errors. | `ProtocolAgent.protocol_deviation` is called only from the discontinuation branch. |
| Reporting behaviour and nocebo | AE reporting is a Bernoulli draw from the model probability; the LLM can add terms, but there is no reporting-rate-by-site or nocebo model. | `sample_adverse_events`, `_apply_narration`. |
| Exposure non-linearity / formulation | PK is a one-compartment model with first-order absorption and identical repeated doses; no saturable clearance, food effect or formulation change. | `ml/pk_pd.py`; identical `dose_mg` per epoch unless titration is configured. |
| Competing risks and estimands | Mortality is a CTCAE grade-5 event count; there is no competing-risk or intercurrent-event framework. | `ArmSummary.mortality_rate` definition in `biostatistician_agent.py`. |
| Statistical realism of the readout | The analysis is unadjusted; multiplicity is controlled (Benjamini-Hochberg) only across secondary endpoints; no group-sequential alpha spending, no covariate adjustment, no pre-registered SAP. | `BiostatisticianAgent._comparisons`. |

## 5. Regulatory posture

| Aspect | Position |
| --- | --- |
| Role of outputs | Decision support for trial design (dose selection, sample size, endpoint and stopping-rule trade-offs). Not evidence of efficacy or safety. |
| Inspirations vs claims | The design follows ICH E9-style principles (stratified randomisation, an auditable allocation table, pre-specified endpoints, a DSMB stopping-rule loop) and CTCAE-style grading. These are inspirations; the platform does not claim compliance with ICH E9, ICH E6(R3) GCP, or any health-authority guidance. |
| Submission readiness | None claimed. The CDISC export mimics SDTM/ADaM structure but ships `define.json`, not Define-XML; there is no controlled terminology validation, no SDTM/ADaM conformance run, and no eCTD packaging. |
| Software lifecycle | There is no IQ/OQ/PQ pack, no 21 CFR Part 11 audit trail or electronic signature, no change-control board, and no formal verification of the statistical routines beyond the unit tests in `tests/` (including `tests/test_stats.py`, which checks the estimators against reference values). |
| Model governance | The physiology model is versioned, digest-hashed and optionally registered in the MLflow Model Registry (`ml/registry.py`, `mlflow_tracking/tracker.py`), which is the *mechanism* for model governance — but a governed use still needs an approved model-risk process. |
| LLM position | The LLM is a qualitative narrator. It is not used to compute endpoints, and it must be documented by provider, model, temperature and cache state in any downstream use. |
| Data protection | Because no real patient data enters the system, GDPR/HIPAA data-subject mechanics are not triggered by the simulation itself. Logs, traces and reports may still contain realistic-looking synthetic personas and should be handled accordingly. |

## 6. Bias considerations

| Source of bias | Where it enters | Mitigation available today |
| --- | --- | --- |
| Population priors | `resources/genomic_priors.yaml` fixes the ancestry mixture, allele frequencies, comorbidity prevalence and smoking distribution. A wrong mixture biases exposure (CYP2D6/CYP3A4) and toxicity (HLA, SLCO1B1) for every arm. | Replace the priors resource and re-run; `cohort_summary()` reports the realised ancestry distribution and marker fractions so a mismatch is visible in the manifest. |
| Ancestry as a proxy | Ancestry selects genotype distributions; it is a coarse proxy and can create apparent group differences that are artefacts of the prior table. | Use `ancestry` as a stratification variable (`randomization.strata`) or remove it; interpret any by-ancestry output with the prior caveat attached. |
| eGFR equation | `CohortGenerator._egfr` uses a CKD-EPI-2021-style formula with illustrative constants and **no race coefficient**. This is a deliberate choice, but the constants are not validated for the simulated population. | Recalibrate against a licensed reference dataset. |
| Compounding prevalence multipliers | Comorbidity odds are multiplied per year of age and per BMI unit above 25, then clipped at 0.95; small changes to the base rate or multipliers have multiplicative effects in older/heavier subgroups. | Sensitivity analysis across prior variants. |
| Fixed lifestyle distributions | Smoking (0.55/0.28/0.17) and alcohol priors are global, not subgroup-specific, so they cannot create or remove a real health-equity signal. | Supply setting-specific priors. |
| LLM persona stereotyping | The prompt includes age, sex, BMI, ancestry, comorbidity history and genotype notes; a provider may generate stereotyped narratives that then appear in reports. | Run with `--llm-mode off` or `--offline` for primary analyses; review qualitative text before circulating it; keep `source = llm` terms separable from modelled events (the pipeline already labels them). |
| Offline fallback skew | `heuristic_symptom_response` has a fixed vocabulary of verbatim quotes and a probability threshold of 0.15, so offline narratives over-represent the terms it knows. | Treat offline mode as a reproducibility fixture, not as a data source for qualitative conclusions. |
| Small strata / small arms | Stratified block randomisation can leave sparse strata; comparisons are unadjusted. | Inspect `allocation_table` and per-arm `n_treatment`/`n_control` in `gold/endpoint_comparisons`. |
| Endpoint and MCID choice | `meets_mcid` depends entirely on the configured `mcid`, `direction` and `response_threshold`. A favourable result can be manufactured by loosening them. | Pre-specify endpoints and MCID before running; the protocol digest makes later edits detectable. |

## 7. Cost and energy considerations

LLM narration dominates the marginal cost of a run; the simulation itself is CPU-bound and cheap by
comparison.

- Volume: one row per patient per epoch (`include_baseline_epoch` adds epoch 0), and at most
  `MAX_LLM_CALLS_PER_PATIENT = 6` LLM calls per patient regardless of mode.
- Modes: `off` = zero calls (used by `tests/test_calibration.py`); `sample` = `llm_sample_rate` of rows;
  `triggered` = rows with `worst_ctcae_grade >= llm_trigger_grade` plus the seeded sample; `all` = every
  non-baseline epoch.
- Pre-run estimate: `cohort_size_estimate(protocol, config)` returns `rows`, `llm_calls_estimate`
  (`rows` for `all`, `rows × llm_sample_rate` for `sample`, `rows × max(llm_sample_rate, 0.02)` for
  `triggered`), `spark_partitions` and `epochs`. It is stored in the manifest under `estimate`.
- The shipped cluster profile (`conf/simulation_cluster.yaml`) sets `llm_mode: triggered`,
  `llm_sample_rate: 0.03`, `llm_trigger_grade: 3` specifically to avoid narrating every epoch.
- Re-runs are cheap when nothing changed: `LLMResponseCache` keys on provider + model + temperature +
  prompt, so a repeated run with a warm cache issues no provider calls, and cache hits are reported in
  `llm_cache_hit`, the trace summary (`cache_hit_rate`) and the manifest.
- Actual consumption is measured, not guessed: `LLMCallStats` and `TraceStore.summary()` report
  `tokens_in_total` / `tokens_out_total`, and the manifest stores `total_tokens_in` /
  `total_tokens_out`. Multiply those by the provider's current price sheet rather than relying on a
  built-in estimate.
- The `offline` provider is free and network-free; `echo` is a test double. Use `--offline` for CI,
  demos and any run where the qualitative narrative is not part of the question.
- Practical guidance: run a small `demo` or `--llm-mode off` sweep to tune dose/endpoints, then spend
  tokens only on the shortlist; keep `max_concurrency`, `requests_per_second` and `burst` below the
  provider quota so the retry path is not exercised at scale.

## 8. Checklist before a real go/no-go decision

Do not present a simulation as support for a go/no-go decision until every applicable box is ticked.

1. **Replace the priors.** Swap `resources/genomic_priors.yaml` for licensed reference data; document the
   source, version and the ancestry mixture you actually intend to recruit.
2. **Qualify the drug parameters.** Replace the default PK/PD values and AE models with estimates from
   real studies, including between-subject variability and covariate effects.
3. **Validate the learned component.** Retrain the residual head (`insilico-trial train-physiology`) on
   defensible data, record its metrics, and confirm that `physiology_model_version`/`digest` in the
   manifest is the model you approved.
4. **Pre-specify the analysis.** Fix the primary endpoint, MCID, responder threshold, stopping rules and
   multiplicity plan before looking at results; treat the protocol digest as the version of record.
5. **Calibrate against reality.** Compare simulated control-arm event rates and endpoint distributions
   with historical trial data for the indication; the calibration tests in `tests/test_calibration.py`
   only check internal plausibility bands.
6. **Quantify uncertainty.** Run multiple seeds (the manifest records the seed), vary priors and model
   parameters, and report the spread rather than a single point result.
7. **Decide the LLM's status.** For a regulated readout, run `--llm-mode off` or pin and document the
   provider, model, temperature and cache; review the qualitative text for medical advice and
   stereotyping before it leaves the team.
8. **Independently review the statistics.** Have a statistician review `stats/estimators.py` usage, the
   missing-data assumptions and the decision rules; the code has unit tests, not a validated statistical
   software qualification.
9. **Confirm reproducibility.** Re-run with the same config and seed and verify identical Silver rows
   (`engine/partition.py::comparable_rows` ignores only `observation_ts` and `llm_latency_ms`); archive
   the run manifest, Silver Parquet and the version history.
10. **Fix the governance path.** Confirm that storage, access control (`governance/namespace.py`
    grants summary and `terraform/unity_catalog.tf`), model registry and retention
    (`storage.time_travel_versions_kept`) match your SOPs.
11. **Get the classification question answered.** Determine with regulatory affairs whether the intended
    use makes the tool a medical device / SaMD or merely an internal design aid; this repository makes no
    such determination.
12. **State the limitations in the output.** Keep the `DISCLAIMER` in the report, and add the
    indication-specific gaps from section 4 to any slide or memo that cites the numbers.

## 9. Code references for the claims above

| Statement | Evidence |
| --- | --- |
| Priors are illustrative and must be replaced | Header comment and value structure of `src/insilico_trial_mas/resources/genomic_priors.yaml`; `cohort/generator.py::CohortGenerator` |
| Reports carry a no-clinical-use disclaimer | `reporting/report.py::DISCLAIMER`; `templates/report.md.j2` line 1 section |
| The LLM is instructed never to give advice and its output is filtered | `llm/prompts.py::PATIENT_PERSONA_SYSTEM_PROMPT`, `::FORBIDDEN_MEDICAL_ADVICE`, `::parse_symptom_response` |
| The advice filter is enforced | `tests/test_llm.py::test_parse_rejects_medical_advice` |
| LLM failures degrade rather than abort | `tests/test_agents.py::test_llm_failure_is_recorded_not_fatal`; `llm/resilient.py` fallback path |
| Toxicity is exposure-driven and grade-distributed | `ml/physiology.py::MechanisticPhysiology.ae_logit`, `::sample_adverse_events` |
| Concomitant medications are not modelled | `bronze/synthetic_cohort` `concomitant_meds_json`; no consumer in `ml/` |
| Plausibility bands are internal, not external validation | `tests/test_calibration.py` module docstring and assertions |
| Consumption is measurable per run | `llm/resilient.py::LLMCallStats`, `mlflow_tracking/tracing.py::TraceStore.summary`, `gold/run_manifest` fields `total_tokens_in` / `total_tokens_out` |
| Retention of historical versions is configurable | `storage/local_store.py::_save_manifest`, `vacuum`; `config.StorageConfig.time_travel_versions_kept` |
