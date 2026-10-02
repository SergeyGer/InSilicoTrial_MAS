# ---------------------------------------------------------------------------
# Agent identities.
#
# The specification's draft granted privileges to principals that were assumed to
# exist already, which means a fresh workspace could never converge. Here the
# service principals and the reviewer group are managed resources.
#
# Each agent runs under its own identity so that Unity Catalog lineage and the
# audit log attribute every write to the agent that made it:
#   * protocol_agent_service_principal       -> bronze
#   * patient_agent_service_principal        -> silver
#   * biostatistician_agent_service_principal-> gold
# ---------------------------------------------------------------------------

resource "databricks_service_principal" "agents" {
  for_each = var.agent_service_principals

  display_name = each.value.display_name
  active       = true

  # Service principals cannot be deleted while they own objects; the pipeline
  # runs as the owning user, not as these principals.
  allow_cluster_create       = false
  allow_instance_pool_create = false
  databricks_sql_access      = true
  workspace_access           = true
}

resource "databricks_group" "simulation_readers" {
  display_name               = var.simulation_readers_group
  allow_cluster_create       = false
  allow_instance_pool_create = false
  databricks_sql_access      = true
  workspace_access           = true
}

resource "databricks_group_member" "readers" {
  for_each = var.reader_user_names

  group_id  = databricks_group.simulation_readers.id
  member_id = each.value
}

# ---------------------------------------------------------------------------
# Secret scope for provider credentials (Bedrock API keys, SMTP, webhooks).
# Backed by AWS Secrets Manager so nothing sensitive lives in Terraform state.
# ---------------------------------------------------------------------------

resource "databricks_secret_scope" "llm" {
  name = "insilico-llm"

  keyvault_metadata {
    resource_id = aws_secretsmanager_secret.llm_credentials.arn
    dns_name    = "secretsmanager.${var.aws_region}.amazonaws.com"
  }
}

resource "aws_secretsmanager_secret" "llm_credentials" {
  name                    = "insilico-trial-mas/${var.environment}/llm-credentials"
  description             = "Bedrock/OpenAI credentials consumed by the Patient Persona agents"
  recovery_window_in_days = 7
}
