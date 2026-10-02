# Terraform: Unity Catalog governance and AWS storage

Provisions everything the simulation platform assumes exists:

| File | Resources |
| --- | --- |
| `versions.tf` | Provider/version pinning, optional S3 remote state |
| `variables.tf` | All inputs, including the agent privilege map |
| `unity_catalog.tf` | Catalog, three medallion schemas, volumes, grants |
| `service_principals.tf` | One service principal per agent role + reviewer group + secret scope |
| `aws_storage.tf` | S3 lakehouse/genomics buckets, IAM role, storage credential, external locations, optional AWS Batch |
| `warehouses.tf` | SQL warehouse, MLflow experiment, cluster policy |
| `outputs.tf` | Values consumed by CI, the bundle and the CLI |

## What was fixed relative to the draft in the specification

The original snippet could not be applied to a fresh workspace:

1. **`databricks_grant` with a plural `privileges` argument** is deprecated. This
   module uses `databricks_grants` with explicit `grant { privilege = ... }`
   blocks (one block per principal), which is the current provider contract.
2. **Principals were assumed to exist.** `service_principals.tf` creates the three
   agent service principals and the reviewer group, so `terraform apply` converges
   on an empty workspace.
3. **No storage layer.** A catalog without a storage credential, external
   location and bucket cannot hold external tables. `aws_storage.tf` adds them,
   including the IAM trust policy, bucket encryption, public-access blocking and
   a lifecycle rule that archives cold genomic reference data.
4. **No metastore/isolation semantics.** The catalog now declares
   `isolation_mode = "ISOLATED"` so that a shared metastore cannot leak data
   across business units.
5. **No handling of the external-id chicken-and-egg.** The two-phase apply is
   documented below instead of failing halfway through with an opaque error.
6. **Added**: SQL warehouse for Gold consumption, MLflow experiment with an
   S3 artifact location, cluster policy that caps cluster size and DBU/hour, and
   a Unity Catalog volume for the raw genomic datasets mentioned in the platform
   description.

## Usage

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # fill in host, bucket names, account id

terraform init
terraform validate
terraform plan  -out tfplan
terraform apply tfplan

# Two-phase storage credential (see aws_storage.tf):
terraform apply -target=aws_iam_role.storage_credential
terraform apply -target=databricks_storage_credential.lakehouse
terraform output storage_credential_external_id
terraform apply -var databricks_external_id=<value-from-previous-step>
```

In CI, authenticate with OAuth machine-to-machine
(`databricks_auth_type = "oauth-m2m"`, `DATABRICKS_CLIENT_ID`/`DATABRICKS_CLIENT_SECRET`
in the environment) and use a remote backend.

## Contract with the application

The names created here are mirrored in code by
`insilico_trial_mas.governance.namespace.UnityCatalogNamespace`, which reads them
from `storage.catalog`, `storage.bronze_schema`, `storage.silver_schema` and
`storage.gold_schema` in the simulation configuration. Change one, change the
other; `tests/test_governance_and_tracking.py` asserts that the declared table
registry and the storage layer agree.

## Cost notes

* The cluster policy caps worker count and DBU/hour; simulations are
  embarrassingly parallel, so more small workers beat fewer large ones.
* Genomic reference data is transitioned to `STANDARD_IA` after 30 days and
  `GLACIER_IR` after 120.
* The SQL warehouse auto-stops after 10 minutes.
* `enable_aws_batch = false` by default: AWS Batch is only needed for burst
  genomic pre-processing outside Spark.
