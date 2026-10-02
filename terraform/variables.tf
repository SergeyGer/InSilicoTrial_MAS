# ---------------------------------------------------------------------------
# Input variables.
# ---------------------------------------------------------------------------

variable "databricks_host" {
  description = "Databricks workspace URL, e.g. https://dbc-xxxx.cloud.databricks.com"
  type        = string
}

variable "databricks_auth_type" {
  description = "Authentication type passed to the Databricks provider (pat, oauth-m2m, databricks-cli, ...)"
  type        = string
  default     = "pat"
}

variable "databricks_profile" {
  description = "Optional Databricks CLI profile name (leave empty in CI)"
  type        = string
  default     = ""
}

variable "databricks_account_id" {
  description = "Databricks account id (required only for account-level resources)"
  type        = string
  default     = ""
}

variable "databricks_aws_account_id" {
  description = <<-EOT
    AWS account id that Databricks uses to assume the storage-access role in your
    account. This is the well-known Databricks control-plane account for your
    region (see the Databricks docs: "Create an IAM role for S3 access").
  EOT
  type        = string
  default     = ""
}

variable "databricks_external_id" {
  description = <<-EOT
    External id Databricks generated for this workspace's storage credential.
    Create the storage credential once (Terraform output `storage_credential_external_id`
    or `databricks storage-credentials create`) and pass it here on the next apply.
  EOT
  type        = string
  default     = ""
}

variable "environment" {
  description = "Environment name used for tagging (dev, staging, prod)"
  type        = string
  default     = "prod"
}

variable "aws_region" {
  description = "AWS region for the S3 buckets backing Bronze/Silver/Gold"
  type        = string
  default     = "us-east-1"
}

variable "catalog_name" {
  description = "Unity Catalog that owns the medallion schemas"
  type        = string
  default     = "trial_simulations_prod"
}

variable "bronze_schema" {
  description = "Bronze schema name (raw cohort, protocol definitions, raw LLM traces)"
  type        = string
  default     = "bronze"
}

variable "silver_schema" {
  description = "Silver schema name (per-epoch patient states, adverse events)"
  type        = string
  default     = "silver"
}

variable "gold_schema" {
  description = "Gold schema name (arm summaries, comparisons, reports)"
  type        = string
  default     = "gold"
}

variable "genomics_bucket_name" {
  description = "S3 bucket holding raw genomic reference data consumed by the cohort generator"
  type        = string
}

variable "lakehouse_bucket_name" {
  description = "S3 bucket holding the Delta tables of the medallion architecture"
  type        = string
}

variable "genomics_prefix" {
  description = "Key prefix inside the genomics bucket"
  type        = string
  default     = "reference/genomics"
}

variable "enable_aws_batch" {
  description = "Provision the AWS Batch queue used for burst genomic pre-processing"
  type        = bool
  default     = false
}

variable "simulation_readers_group" {
  description = "Workspace group granted read access to the Gold layer"
  type        = string
  default     = "simulation_readers"
}

variable "agent_service_principals" {
  description = "Service principals that the three agent roles authenticate as"
  type = map(object({
    display_name = string
    schema       = string
    privileges   = list(string)
  }))
  default = {
    patient_agent = {
      display_name = "patient_agent_service_principal"
      schema       = "silver"
      privileges   = ["USE_SCHEMA", "SELECT", "MODIFY"]
    }
    protocol_agent = {
      display_name = "protocol_agent_service_principal"
      schema       = "bronze"
      privileges   = ["USE_SCHEMA", "SELECT", "MODIFY"]
    }
    biostatistician_agent = {
      display_name = "biostatistician_agent_service_principal"
      schema       = "gold"
      privileges   = ["USE_SCHEMA", "SELECT", "MODIFY"]
    }
  }
}

variable "reader_user_names" {
  description = "User names (or service principal application ids) added to the Gold-layer reviewer group"
  type        = set(string)
  default     = []
}

variable "sql_warehouse_size" {
  description = "T-shirt size of the SQL warehouse used for BI/report consumption"
  type        = string
  default     = "2X-Small"
}

variable "tags" {
  description = "Additional tags applied to every taggable resource"
  type        = map(string)
  default     = {}
}
