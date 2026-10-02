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

## AWS provider 6.x

The module requires `hashicorp/aws ~> 6.0` (verified against 6.67.0, which
`terraform/.terraform.lock.hcl` pins). Moving from 5.x needed exactly one code
change:

* **`aws_batch_compute_environment`**: provider 6.0 renamed
  `compute_environment_name` to `name` (and `compute_environment_name_prefix` to
  `name_prefix`). The optional Batch compute environment uses `name` now; the name
  it creates is unchanged.

Nothing else the module uses broke, and this was checked against the provider
schema and the 6.0.0 changelog rather than assumed:

* the S3 resources keep the same arguments — `aws_s3_bucket_lifecycle_configuration`
  still takes `rule.filter { prefix = ... }` (the `rule.prefix` shorthand stays
  deprecated), and `transition_default_minimum_object_size` still defaults to
  `all_storage_classes_128K` in both 5.x and 6.x, so the transition semantics of the
  genomic archive rule do not change;
* `aws_iam_policy_document`, `aws_iam_role`, `aws_iam_role_policy`,
  `aws_iam_role_policy_attachment` and `aws_secretsmanager_secret` have no breaking
  changes, and the `default_tags` provider block is unaffected.

The Databricks provider constraint is deliberately unchanged. Note that
`enable_aws_batch = true` still needs real subnet ids in `compute_resources.subnets`
(the module ships an empty placeholder, which AWS rejects at apply time); that is a
pre-existing limitation, not something provider 6.x introduced.

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
