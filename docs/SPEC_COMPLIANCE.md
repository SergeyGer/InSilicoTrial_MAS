# Specification compliance map

Requirement-by-requirement mapping from
`InSilicoTrial_MAS_Specification_v2.md` to the implementation, with the test that
proves it and the defects that were fixed along the way.

Legend: **Implemented** = required behaviour exists; **Extended** = requirement
exists and was strengthened beyond the draft; **Fixed** = the draft was incorrect
or unsafe and the implementation differs deliberately.

---

## 1. System overview & agent types

| # | Requirement | Status | Implementation | Proof |
| --- | --- | --- | --- | --- |
| 1.1 | Protocol Agent distributes drug protocol and dosage steps | Implemented | `agents/protocol_agent.py` (`validate`, `screen`, `randomize`, `directive`) | `tests/test_cohort.py::test_randomisation_is_balanced_and_deterministic`, `test_directive_reflects_arm_and_titration` |
| 1.2 | Patient Persona Agent: physiology via tabular ML | Extended | `agents/patient_agent.py` + `ml/physiology.py` (mechanistic PK/PD backbone plus a learned ridge residual head) | `tests/test_pk_pd_and_physiology.py`, `tests/test_calibration.py` |
| 1.3 | Patient Persona Agent: qualitative reactions via LLM | Implemented | `llm/` (prompts, parsing, offline + LangChain/Bedrock providers), `PatientPersonaAgent._narrate` | `tests/test_llm.py`, `tests/test_agents.py::test_llm_symptoms_are_merged_with_modelled_events` |
| 1.4 | Biostatistician Agent aggregates logs, monitors AEs, generates reports | Extended | `agents/biostatistician_agent.py`, `reporting/report.py`, `reporting/cdisc.py` | `tests/test_agents.py::test_biostatistician_produces_coherent_analytics`, `tests/test_pipeline.py` |
| 1.5 | Distributed scale instead of single-threaded MAS | Implemented | `engine/spark_runner.py`, `engine/local_runner.py`, `engine/sequential_runner.py` | `tests/test_engines.py` (all three backends) |

### Defects fixed in the section 3 blueprint

| Draft code | Problem | Fix |
| --- | --- | --- |
| `simulated_bp = 120 + (row['dose'] * 0.1)` | Toy formula: no pharmacokinetics, no placebo arm, no baseline covariates, no time dimension. It cannot support a dose-response or safety claim. | One-compartment oral PK with first-order absorption and closed-form repeated-dose superposition; sigmoid Emax PD; placebo effect, disease progression, comorbidity burden, pharmacogenomic effect modification, inter-individual sensitivity and measurement noise (`ml/pk_pd.py`, `ml/physiology.py`). |
| `chain = prompt \| llm \| JsonOutputParser()` | No format instructions: the model has no contract to satisfy, so parsing fails intermittently. | Explicit JSON schema plus few-shot rules embedded in the prompt (`llm/prompts.py::PATIENT_PERSONA_TEMPLATE`). |
| `except Exception as e: symptoms = f"Error capturing response: {e}"` | A failure becomes free text in a clinical column, silently corrupting analytics and hiding outages. | Typed `llm_error` column, retry with backoff, deterministic offline fallback, and a safety filter that rejects medical advice (`llm/resilient.py`, `llm/prompts.py::parse_symptom_response`). |
| `asyncio.gather(*tasks)` over every row | Unbounded concurrency: 10,000 sockets and instant provider throttling. | `async_utils.gather_bounded` with a hard ceiling plus a token-bucket limiter (`llm/rate_limit.py`). |
| `asyncio.run(main())` inside a Spark partition | Raises `RuntimeError: asyncio.run() cannot be called from a running event loop` in Databricks notebooks/Spark Connect. | `async_utils.run_sync` detects a live loop and bridges to a private loop in a helper thread. |
| `BedrockChat(model_id="anthropic.claude-3-sonnet-20240229-v1:0", beta_use_converse_api=True)` | `langchain_community` chat models are deprecated, `beta_use_converse_api` no longer exists, and that model id has reached end of life. | `langchain_aws.ChatBedrock` with a configurable, current inference-profile id default (`us.anthropic.claude-3-5-sonnet-20241022-v2:0`); provider import is lazy so the package has no hard dependency. |
| Client constructed per row | Thousands of HTTP clients per partition. | One client per worker process, cached in `engine/runtime.py::get_runtime`. |
| `row['epoch']` read but never produced | The blueprint referenced a column no code created. | Explicit timeline: epoch 0 is the untreated baseline, epochs 1..N are dosing epochs with titration support. |
| No seeding | Runs were not reproducible; Delta time travel would be meaningless. | Every stochastic draw is derived from `sha256(seed, run, patient, epoch, purpose)` (`reproducibility.py`). |

---

## 2. Distributed scale architecture

| # | Requirement | Status | Implementation | Proof |
| --- | --- | --- | --- | --- |
| 2.1 | AWS production: horizontal scale via `df.groupBy("cohort_id").applyInPandas()` | Implemented | `engine/spark_runner.py` (`partition_mode: cohort`, `cohorts_per_partition` batching) | `tests/test_engines.py::test_spark_engine_matches_sequential` |
| 2.2 | Community Edition: driver-side vectorised execution with local multiprocessing | Implemented | `engine/local_runner.py` (spawn process pool + asyncio inside each worker), `conf/simulation_community_edition.yaml` | `tests/test_engines.py::test_sequential_matches_local_multiprocessing` |
| 2.3 | Batch splitting before worker execution | Extended | Two strategies: cohort-aligned batches and balanced Arrow batches (`mapInPandas`) that remove the large-cohort straggler | `tests/test_engines.py::test_batch_splitting_modes`, `test_spark_balanced_partitioning_matches_cohort_partitioning` |
| 2.4 | LangChain initialised per worker thread | Fixed | Per *process*, not per row or per thread: one runtime per executor, cached by run digest | `engine/runtime.py`, `tests/test_engines.py` |
| 2.5 | Async LLM calls with rate limiting | Implemented | Token bucket + bounded concurrency + exponential backoff with jitter | `tests/test_llm.py::test_token_bucket_throttles`, `test_resilient_client_retries_then_succeeds` |
| 2.6 | Write dynamic logs straight to Delta Lake Silver | Implemented | `storage/delta_store.py` (Unity Catalog or external location), `LocalVersionedStore` for JVM-free runs | `tests/test_storage.py`, `tests/test_pipeline.py::test_gold_tables_are_written` |

Additional hardening not in the draft: `PYSPARK_PYTHON` is pinned to the driver
interpreter and *verified* on an existing session (`spark_runner.ensure_worker_python`),
because a worker Python without pandas kills the job deep inside a task; shuffle
partitions are derived from the cohort count instead of the 200-partition default.

---

## 3. LangChain worker initialisation

| # | Requirement | Status | Implementation |
| --- | --- | --- | --- |
| 3.1 | LangChain prompt template with patient profile, history, dose | Implemented | `llm/prompts.py::build_patient_prompt` (adds genotype, model-predicted vitals and AE probabilities so the persona cannot contradict the physiology) |
| 3.2 | LCEL chain `prompt \| llm \| parser` | Implemented | `llm/langchain_provider.py::LangChainChatClient.build_chain` (exposed for inspection/tracing) |
| 3.3 | Hybrid ML + LLM evaluation per row | Implemented | `agents/patient_agent.py`: model-predicted AE probabilities → persona narration → merge (grade escalation on matching terms, LLM-only terms retained with `source = llm`) |

Deviation worth noting: the hot path calls the model directly rather than through
the LCEL chain, because `StrOutputParser` discards `usage_metadata` and token
accounting is required for cost control and MLflow tracing. The chain remains
available and is exercised in `llm/langchain_provider.py`.

---

## 4. Unity Catalog governance & Terraform

| # | Requirement | Status | Implementation | Proof |
| --- | --- | --- | --- | --- |
| 4.1 | Catalog + three schemas (bronze/silver/gold) | Implemented | `terraform/unity_catalog.tf` | `terraform validate` (CI job `infrastructure`) |
| 4.2 | Grants for agent service principals (`SELECT`, `MODIFY`, `ALL_PRIVILEGES`) | Fixed | `databricks_grants` with per-principal `grant {}` blocks; the deprecated plural `privileges` argument of `databricks_grant` is gone | `terraform/README.md` documents the change |
| 4.3 | Principals exist before privileges are granted | Fixed | `terraform/service_principals.tf` creates the three agent service principals, the reviewer group and a secret scope; the draft assumed they already existed and could never converge on a fresh workspace | `tests/test_governance_and_tracking.py::test_namespace_ddl_and_grants_are_complete` (privilege model mirrored in code) |
| 4.4 | Data lineage tracing | Extended | Unity Catalog lineage plus an application-level graph written to `gold.lineage_edges` (`governance/lineage.py`) covering agent → table → report provenance, which UC cannot see | `tests/test_governance_and_tracking.py::test_lineage_graph_navigation` |
| 4.5 | AWS integration (S3 genomic data, AWS Batch) | Extended | `terraform/aws_storage.tf`: buckets with KMS encryption, public-access block, lifecycle archival, IAM role, storage credential, external locations, optional AWS Batch compute environment | `terraform validate` |

Also added: catalog `isolation_mode`, Unity Catalog volumes for genomic reference
data and generated reports, SQL warehouse for Gold consumption, MLflow experiment
with an S3 artifact location, and a cluster policy that caps size and DBU/hour.

---

## 5. Notebook structure & validation

| # | Requirement | Status | Implementation | Proof |
| --- | --- | --- | --- | --- |
| 5.1 | Cell 1: environment checklist (multi-node vs Community Edition fallback) | Implemented | `engine/checklist.py::inspect_environment`, CLI `env-check`, notebook cell 1 | `tests/test_engines.py::test_environment_report_is_serialisable`, `tests/test_cli.py::test_env_check_json` |
| 5.2 | Cell 2: generate 10,000 synthetic patients | Implemented | `cohort/generator.py` (streaming generator, population priors) | `tests/test_cohort.py` |
| 5.3 | Cell 3: execute distributed batches with `applyInPandas` | Implemented | `pipeline.py` + `engine/spark_runner.py`; notebook prints engine, partitions and Silver location | `tests/test_engines.py::test_spark_engine_matches_sequential` |
| 5.4 | Cell 4: Delta time travel audit | Extended | `DeltaStore.history` (`DESCRIBE HISTORY`), `read(version=...)`; the local store implements identical version semantics so the cell also runs without a JVM | `tests/test_storage.py::test_time_travel_reads_a_historical_version`, `tests/test_cli.py::test_simulate_time_travel_lineage_and_report` |
| 5.5 | Cell 5: lineage visualisation and MLflow tracing (token distribution, request intervals, trace hierarchy) | Implemented | `governance/lineage.py::to_mermaid`, `mlflow_tracking/tracing.py` (`TraceRecorder`, `TraceStore.summary`, `TraceStore.tree`) | `tests/test_governance_and_tracking.py::test_trace_recorder_writes_hierarchical_spans`, `scripts/run_notebook.py` (CI executes every notebook cell) |

### Issues in the draft SQL corrected

```sql
-- Draft: the column does not exist on the Silver contract, so the query fails.
SELECT * FROM trial_simulations_prod.silver.patient_states VERSION AS OF 1
WHERE simulated_blood_pressure > 140;
```

The Silver contract stores `sbp` (plus `sbp_change`), and the corrected query is:

```sql
SELECT arm_id, COUNT(*) AS observations, AVG(sbp_change) AS mean_change
FROM trial_simulations_prod.silver.patient_states VERSION AS OF 1
WHERE sbp > 140
GROUP BY arm_id;
```

`VERSION AS OF 1` also requires at least two writes; `DESCRIBE HISTORY` is shown
first in the notebook so the reader picks a version that exists. `RESTORE TABLE`
is documented for "rewinding" a simulation, and the local store exposes the same
operation through `LocalVersionedStore.read(version=n)`.

---

## 6. Requirements introduced by the platform description

| Requirement | Implementation | Proof |
| --- | --- | --- |
| Reproducibility / Delta Time Travel | Seeded, order-independent RNG; version history on every backend; run manifest with protocol digest, model digest, seed, LLM policy | `scripts/verify_reproducibility.py`, `tests/test_pipeline.py::test_repeated_runs_are_bit_identical` |
| MLflow Models/Registry manages the ML models that predict biomarkers | Ridge residual head trained on synthetic historical cohorts, saved as a versioned, digest-hashed artifact, optionally registered | `ml/training.py`, `ml/registry.py`, CLI `train-physiology --register` |
| Scalability that is hard to reach with plain local Python | Three interchangeable engines with proven bit-identical output | `tests/test_engines.py` |
| Synthetic cohort with phenotype, history and genetic markers from historical data | Population priors resource, ancestry-specific allele frequencies, comorbidity hazards, PEG-like eGFR | `cohort/generator.py`, `resources/genomic_priors.yaml` |

---

## 7. Verification summary

```bash
make test          # unit + integration + plausibility bands (no JVM required)
make test-spark    # Spark engine must reproduce the sequential engine exactly
make verify-repro  # two runs, same seed, identical rows
make notebook      # every notebook cell executes
make calibrate     # exposure/efficacy/safety land in clinically plausible bands
make iac-validate  # terraform fmt + validate
```

`tests/test_calibration.py` deserves a special mention: it is the guard that
stops a modelling regression from silently turning a plausible phase II trial
into one where 98% of patients have an adverse event - which is exactly what the
first draft of the PK exposure formula did during development.
