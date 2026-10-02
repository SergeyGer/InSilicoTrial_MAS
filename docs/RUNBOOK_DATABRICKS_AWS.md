# Runbook: Databricks & AWS operations

Operational guide for running InSilicoTrial MAS on AWS Databricks, on Databricks
Community Edition and on a laptop. For architecture, see
[ARCHITECTURE.md](ARCHITECTURE.md); for the requirement mapping, see
[SPEC_COMPLIANCE.md](SPEC_COMPLIANCE.md).

---

## 1. Choosing a deployment profile

| Environment | Config profile | Engine | LLM | Storage |
| --- | --- | --- | --- | --- |
| Laptop / CI | `conf/simulation_local.yaml` | `auto` → `local` (process pool) | `offline` | local versioned Parquet |
| Databricks Community Edition | `conf/simulation_community_edition.yaml` | `local` (single-node driver) | `offline` | Delta on the Hive metastore |
| AWS Databricks (production) | `conf/simulation_cluster.yaml` | `spark` (`applyInPandas`) | Bedrock Claude | Delta in Unity Catalog |

`insilico-trial env-check --config <profile>` prints what the current runtime can
actually do, including the recommended engine, the JVM path, the Spark master and
whether the worker Python can import pandas.

---

## 2. Provisioning (once per environment)

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars     # host, buckets, AWS account id
terraform init && terraform validate
terraform apply                                   # phase 1: buckets, IAM role
terraform apply -target=databricks_storage_credential.lakehouse
terraform apply -var databricks_external_id="$(terraform output -raw storage_credential_external_id)"
```

Then deploy the bundle (job definition, wheel, cluster policy):

```bash
bash scripts/databricks_deploy.sh --target prod        # validate + deploy
bash scripts/databricks_deploy.sh --target prod --run  # deploy and trigger
```

Permissions the deploying identity needs: `CREATE CATALOG`/`CREATE SCHEMA` on the
metastore (or ownership of the catalog), `CREATE SERVICE PRINCIPAL` on the
account, and `iam:CreateRole`/`iam:PutRolePolicy` on the AWS side.

---

## 3. Running a simulation

### Cluster job

```bash
databricks bundle run insilico_trial_simulation -t prod
```

The job validates the protocol, runs the simulation, trains/registers the
physiology model and writes Gold tables. Artefacts land in
`<output_dir>/<RUN-ID>/` (`run_manifest.json`, `report/`, `cdisc/`).

### Interactive

```python
from insilico_trial_mas.config import load_config
from insilico_trial_mas.pipeline import TrialSimulationPipeline, load_protocol

config = load_config("conf/simulation_cluster.yaml", {"n_patients": 10000})
pipeline = TrialSimulationPipeline(config, load_protocol(config.protocol_path))
result = pipeline.run()
print(result.run_id, result.silver_location, result.report.markdown)
```

### Sizing

| Cohort | Epochs | Rows | Recommended cluster | Wall clock (indicative) |
| --- | --- | --- | --- | --- |
| 1,000 | 8 | 9,000 | 1 driver + 2 workers (m5.xlarge) | < 1 min |
| 10,000 | 8 | 90,000 | 4–8 workers (m5.2xlarge) | 3–8 min |
| 100,000 | 8 | 900,000 | 16–32 workers (m5.2xlarge), autoscale | 20–45 min |
| 1,000,000 | 8 | 9,000,000 | 32+ workers, `partition_mode: balanced` | hours |

Rules of thumb:

* Keep `engine.batch_size` between 256 and 1024: smaller batches add scheduling
  overhead, larger ones raise peak worker memory (each patient holds its own PK
  state and prompt context).
* `engine.cohorts_per_partition: 1..4` amortises worker start-up; higher values
  increase skew when cohorts differ in size.
* With more than ~200 cohorts, switch to `partition_mode: balanced` and set
  `engine.spark_shuffle_partitions` explicitly.
* `llm_mode: triggered` with `llm_trigger_grade: 2..3` and
  `llm_sample_rate: 0.02..0.05` keeps persona cost bounded. `llm_mode: all` on a
  million rows means millions of API calls - estimate it before you launch:

```bash
insilico-trial env-check --json | jq '.recommended_backend'
# the pipeline logs an estimate (rows, LLM calls, partitions) at run start
```

---

## 4. Cost control

| Lever | Where | Effect |
| --- | --- | --- |
| `llm_mode` / `llm_sample_rate` / `llm_trigger_grade` | simulation config | Dominant cost driver: LLM calls scale with narrated epochs |
| `llm.cache_enabled` + `llm.cache_path` | simulation config | Re-runs and sweeps reuse identical prompts; put the cache on shared storage (DBFS/UC volume) so executors hit it |
| `engine.batch_size` | simulation config | Fewer, larger tasks reduce scheduling overhead |
| Spot instances with fallback | `resources/jobs.yml` (`availability: SPOT_WITH_FALLBACK`) | 60–70% cheaper workers; simulation is retry-safe because runs are deterministic |
| Cluster policy DBU/hour cap | `terraform/warehouses.tf` | Blocks runaway autoscaling |
| `tracking.log_artifacts` | simulation config | Set `false` for huge sweeps; keep it for headline runs |
| Delta `OPTIMIZE`/`ZORDER` | `DeltaStore.optimize` | Run weekly, not per simulation |

Track actual spend per run: every run logs `duration_seconds`, `llm tokens`, and
the engine to MLflow and into `gold.run_manifest`.

---

## 5. Monitoring and validation

```sql
-- Latest runs and their headline metrics
SELECT sim_run_id, protocol_id, engine_backend, n_patients, n_epochs, duration_seconds
FROM trial_simulations_prod.gold.run_manifest
ORDER BY started_at DESC LIMIT 20;

-- Arm-level readout of the newest run
SELECT * FROM trial_simulations_prod.gold.arm_summaries
WHERE sim_run_id = (SELECT sim_run_id FROM trial_simulations_prod.gold.run_manifest ORDER BY started_at DESC LIMIT 1);

-- Safety signals raised
SELECT arm_id, term, rate, risk_difference, q_value
FROM trial_simulations_prod.gold.safety_summary
WHERE q_value < 0.10 ORDER BY risk_difference DESC;

-- Version history of the Silver table (audit trailing)
DESCRIBE HISTORY trial_simulations_prod.silver.patient_states;

-- Point-in-time read ("rewind" the simulation)
SELECT arm_id, COUNT(*) AS n, AVG(sbp_change) AS mean_change
FROM trial_simulations_prod.silver.patient_states VERSION AS OF 1
WHERE sbp > 140
GROUP BY arm_id;
```

Also monitor: DLQ/retry counts (`llm_retries`, `llm_fallbacks` in the run
manifest), `llm_error_pct` in the report's data-quality section, and the
`numRecords` of each Delta write.

---

## 6. Failure playbook

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `ModuleNotFoundError: No module named 'pandas'` inside a task | Executors use a Python interpreter without the package (`PYSPARK_PYTHON`) | The engine logs the mismatch at start-up (`ensure_worker_python`). Install the wheel on the cluster (bundle does this) or pin `spark.pyspark.python` to an interpreter that has it |
| `JAVA_HOME is not set` / `Unable to locate a Java Runtime` | No JVM on the driver | Install a system JDK, or run `bash scripts/bootstrap.sh --spark` to place a portable JRE in `.toolchain/` |
| `Only remote Spark sessions using Databricks Connect are supported` | The `databricks-connect` package replaced local PySpark with a remote-only client | `pip uninstall databricks-connect && pip install "pyspark==3.5.3"` for local runs, or set `DATABRICKS_HOST`/`DATABRICKS_TOKEN` to use the remote cluster. The engine detects this case and raises an actionable error (`detect_databricks_connect_shadowing`) |
| `ProcessPoolExecutor` fails with `Permission denied` | Restricted container/sandbox forbids process creation | The local engine logs a warning and continues in-process; use `engine.backend: sequential` for full determinism in such environments |
| Job fails with `ThrottlingException` | Provider rate limit exceeded | Lower `llm.requests_per_second`, raise `llm.max_retries`, or reduce `llm_sample_rate`. The fallback provider keeps the run alive and flags affected rows in `llm_error` |
| All rows have `llm_error` and `llm_used = false` | Missing/expired credentials or a bad model id | Run `insilico-trial simulate --offline` to confirm the pipeline itself is healthy, then fix credentials (secret scope `insilico-llm`) |
| Simulation results change between runs with the same seed | A code or dependency change, or an unseeded random source | `python scripts/verify_reproducibility.py`; check `git_revision` and `physiology_model_digest` in `gold.run_manifest` |
| Delta write fails with `ConcurrentAppendException` | Two runs writing the same partition concurrently | Expected with `delta_partition_by: [arm_id]`; the pipeline writes run-scoped data and reads with `run_id`, so either serialise runs or partition by `sim_run_id ` in high-concurrency setups |
| Report shows no adverse events | `drug.ae_models` empty or AE probabilities too low | `insilico-trial validate-protocol` warns about missing AE models; re-fit parameters with `scripts/fit_ae_parameters.py` |
| Run takes far longer than expected | Skewed cohort partitioning, or per-patient LLM calls dominating | Switch to `partition_mode: balanced`, check the Spark UI stage timeline, and inspect `llm_mean_latency_ms` in MLflow |

---

## 7. Model lifecycle

```bash
# Train on synthetic historical cohorts and write the artifact
insilico-trial train-physiology --rows 60000 --output /dbfs/insilico-trial/models/physiology_model.json

# Train and register in the MLflow Model Registry
insilico-trial train-physiology --rows 60000 --register
```

* The artifact is a small JSON file with per-target ridge coefficients, training
  metrics and a content digest; the digest is stored in every run's provenance.
* Set `ml.use_mlflow_registry: true` and `ml.mlflow_stage` to serve a registered
  version; if the registry is unreachable the platform logs a warning and falls
  back to the local artifact, then to the mechanistic model.
* Promote a model only after `make calibrate` shows plausible exposure, efficacy
  and safety bands for the reference protocol.

---

## 8. LLM trace handling

* `tracking.trace_path` receives one JSON line per span. On a cluster this must be
  a shared path (DBFS or a Unity Catalog volume), otherwise each executor writes
  to its own disk and only driver spans reach the report.
* `insilico-trial traces --path <file> --json` prints token distribution and
  latency percentiles; `--tree first` prints a span hierarchy.
* The trace summary is attached to the MLflow run and embedded in the report
  (`data_quality` and the telemetry section).

---

## 9. Upgrade and rollback

1. `git pull && make test && make verify-repro` - a reproducibility failure is a
   release blocker.
2. `databricks bundle deploy -t prod` (the wheel is versioned in
   `pyproject.toml`; bump it in the same commit).
3. Re-run a historical `sim_run_id` and compare `gold.arm_summaries` with the
   stored version. Differences are acceptable only when the model or protocol
   changed, and the manifest will show it.
4. Rollback: redeploy the previous wheel version; Delta tables are append-only, so
   no data migration is required. `RESTORE TABLE ... VERSION AS OF n` reverts a
   corrupted table if a bad write ever lands.
