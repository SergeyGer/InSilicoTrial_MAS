# Configuration, credentials and secrets

This document answers the two questions that come up first when someone adopts
the platform:

1. *Where do I put my settings?* — YAML profiles plus `INSILICO_*` overrides.
2. *Where do I put my keys?* — never in the repository. The platform reads the
   standard credential chain of every SDK it uses, and runs fully without any
   credentials when you want it to.

---

## 1. Why there is no `.env` in the repository

There is a **`.env.example`** template and a git-ignored `.env`, but no committed
`.env`, on purpose:

| Reason | Detail |
| --- | --- |
| **Secrets must not be versioned** | A committed `.env` leaks keys to everyone with repository access and to every clone, fork and CI log. `.gitignore` excludes `.env`, `.env.*` (except `.env.example`) and `*.tfvars`. |
| **The platform must run without credentials** | CI, a laptop, a Databricks Community Edition notebook and the `insilico-trial demo` path all use the deterministic **offline** persona provider. That is what keeps runs reproducible and the test suite hermetic. |
| **Cloud-native identity is better than keys** | On Databricks the cluster's instance profile / IAM role and the Unity Catalog storage credential grant access to S3 and Bedrock without any long-lived key existing in code or config. |
| **Every SDK already defines its own variable names** | boto3, the OpenAI client, the Databricks SDK and MLflow each resolve credentials from their documented environment variables. A `.env` file would add a second, drifting convention — so the project simply documents the first one. |

If you *want* a `.env` for local development, create one — it is supported out of
the box:

```bash
cp .env.example .env      # then fill in only what you need
```

* **VS Code** loads it automatically: `.vscode/settings.json` sets
  `"python.envFile": "${workspaceFolder}/.env"`, which applies to the debugger,
  the test runner and integrated terminals.
* **A shell** needs it sourced explicitly:
  `set -a; source .env; set +a`.
* **Databricks jobs** should not use a `.env` file at all — use cluster
  environment variables, a secret scope, or `%pip`/init-script free workspace
  configuration (see section 4).

---

## 2. Configuration precedence

Lowest to highest:

1. dataclass defaults (`SimulationConfig` in `src/insilico_trial_mas/config.py`);
2. the YAML profile passed with `--config` (for example `conf/simulation_cluster.yaml`);
3. environment variables of the form `INSILICO_<SECTION>__<FIELD>`, or
   `INSILICO_<FIELD>` for top-level fields;
4. explicit CLI flags (`--patients`, `--engine`, `--llm-provider`, …).

```bash
# Equivalent ways to run 10,000 patients on Spark with Bedrock personas
insilico-trial simulate --config conf/simulation_cluster.yaml --patients 10000 --engine spark
INSILICO_N_PATIENTS=10000 INSILICO_ENGINE__BACKEND=spark insilico-trial simulate --config conf/simulation_cluster.yaml
```

Unknown keys are rejected rather than ignored, so a typo cannot silently change a
trial. **YAML 1.1 pitfall:** unquoted `off`/`on`/`yes`/`no` parse as booleans; the
loader maps them back for enumerated fields (`llm_mode: off` works).

---

## 3. What each provider needs

| Provider | Configuration | Credentials |
| --- | --- | --- |
| `offline` (default) | `llm.provider: offline` | none — deterministic local personas |
| `mock` | `llm.provider: mock` | none — alias kept for notebooks |
| `bedrock` | `llm.provider: bedrock`, `llm.model` (a *cross-region inference profile* id, e.g. `us.anthropic.claude-3-5-sonnet-20241022-v2:0`), `llm.region` | `AWS_ACCESS_KEY_ID`+`AWS_SECRET_ACCESS_KEY`, or `AWS_PROFILE`, or `AWS_ROLE_ARN`, or the instance role |
| `openai` | `llm.provider: openai`, `llm.model`, optional `llm.endpoint_url` | `OPENAI_API_KEY` |
| `langchain` | `llm.options.chat_model` — a pre-built LangChain chat model | whatever that model needs |
| MLflow tracking | `tracking.tracking_uri` (empty → `file://<output_dir>/mlruns`, or `databricks` in a workspace) | `DATABRICKS_HOST` + `DATABRICKS_TOKEN`, or OAuth M2M |
| Model registry | `ml.use_mlflow_registry`, `ml.mlflow_model_name`, `ml.mlflow_stage` | same as MLflow |
| Delta / Unity Catalog | `storage.backend: delta`, `storage.catalog`, `storage.root_uri` | the workspace identity; no keys in the configuration |
| Genomic reference data (S3) | `storage.root_uri`, or a Unity Catalog external volume | storage credential provisioned by Terraform |

Verify what the current environment actually sees — this never prints a value, only
whether a chain is configured:

```bash
insilico-trial env-check
#   credentials       : aws-bedrock=no, bedrock_region=yes, databricks=no, mlflow=no, openai=no
#                       (presence only - values are never read or logged)
#   note              : no LLM credentials detected: the deterministic offline persona
#                       provider is used (identical JSON contract, no network, reproducible)
```

`env-check --json` exposes the same map, and every run manifest records the
provider and model that were actually used.

---

## 4. Secrets on Databricks (recommended production path)

```hcl
# terraform/service_principals.tf provisions an AWS Secrets Manager backed scope
resource "databricks_secret_scope" "llm" {
  name = "insilico-llm"
  keyvault_metadata {
    resource_id = aws_secretsmanager_secret.llm_credentials.arn
    dns_name    = "secretsmanager.<region>.amazonaws.com"
  }
}
```

Three supported patterns, in order of preference:

1. **No secret at all** — attach the storage credential / instance profile to the
   cluster and let boto3 resolve the role. This is how the reference deployment
   reaches S3 and Bedrock.
2. **Secret scope** — read at runtime and inject into the environment before the
   pipeline starts:
   ```python
   import os
   os.environ["OPENAI_API_KEY"] = dbutils.secrets.get("insilico-llm", "openai_api_key")  # noqa: F821
   ```
   Never write the value into a notebook cell, a Delta table or a log line.
3. **Cluster environment variables** — set `AWS_REGION`, `INSILICO_LLM__PROVIDER`
   and friends in the cluster's *Advanced options → Spark → Environment variables*.

The Unity Catalog privilege model keeps the data side separate: each agent role
has its own service principal (`patient_agent_service_principal` → Silver,
`protocol_agent_service_principal` → Bronze, `biostatistician_agent_service_principal`
→ Gold), so a leaked LLM key cannot read the clinical tables.

---

## 5. Checking that a credential change did not break determinism

Credentials change *who* narrates, not *what* the simulation computes: the PK/PD
and ML layers are seeded and local. Prompt hashes, cache hits and the provider name
are recorded per observation, so a readout can always be traced back to the model
that produced it.

```bash
insilico-trial simulate --offline …      # force the deterministic provider for any profile
make verify-repro                        # two runs, same seed, identical rows
insilico-trial traces --tree first       # which model answered, with what latency
```

Deployments that must be byte-for-byte reproducible should keep
`llm.provider: offline` in the profile used for regulated artefacts and treat
credentialed providers as a qualitative add-on.
