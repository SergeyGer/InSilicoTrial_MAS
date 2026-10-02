# ---------------------------------------------------------------------------
# Unity Catalog governance for InSilicoTrial MAS.
#
# This file is the infrastructure counterpart of
# `insilico_trial_mas.governance.namespace`: the catalog/schema/table names and
# the privilege model are declared once here and read back by the application, so
# code and infrastructure cannot drift apart.
#
# Fixed relative to the specification's draft:
#   * `databricks_grant` (deprecated plural `privileges` argument) -> `databricks_grants`
#     with explicit `grant { privilege = ... }` blocks, which is what current
#     providers accept and what produces a reviewable diff;
#   * principals are *created* (see service_principals.tf) instead of referenced
#     by name, so `terraform apply` works on a fresh workspace;
#   * catalog isolation and metastore assignment are explicit;
#   * a Unity Catalog volume holds the raw genomic reference data.
# ---------------------------------------------------------------------------

resource "databricks_catalog" "insilico_trial" {
  name           = var.catalog_name
  comment        = "Production catalog for the InSilicoTrial Multi-Agent Simulation platform"
  isolation_mode = "ISOLATED"

  properties = {
    purpose     = "clinical-trials-simulation"
    environment = var.environment
    owner-team  = "clinical-pharmacology"
  }
}

# ---------------------------------------------------------------------------
# Medallion schemas
# ---------------------------------------------------------------------------

resource "databricks_schema" "bronze" {
  catalog_name  = databricks_catalog.insilico_trial.name
  name          = var.bronze_schema
  comment       = "Raw ingestion: synthetic cohorts, protocol definitions and raw LLM traces"
  force_destroy = false

  properties = {
    layer          = "bronze"
    retention_days = "365"
  }
}

resource "databricks_schema" "silver" {
  catalog_name  = databricks_catalog.insilico_trial.name
  name          = var.silver_schema
  comment       = "Cleaned per-epoch patient states, adverse events and protocol deviations (time-travel enabled)"
  force_destroy = false

  properties = {
    layer = "silver"
    # Delta time travel is a regulatory requirement for this platform: keep the
    # history long enough to replay any readout.
    delta_log_retention = "90"
    retention_days      = "1825"
  }
}

resource "databricks_schema" "gold" {
  catalog_name  = databricks_catalog.insilico_trial.name
  name          = var.gold_schema
  comment       = "Biostatistician aggregates: arm summaries, endpoint comparisons, safety analytics and reports"
  force_destroy = false

  properties = {
    layer          = "gold"
    retention_days = "3650"
  }
}

# ---------------------------------------------------------------------------
# Volumes: raw genomic reference data and generated report artefacts
# ---------------------------------------------------------------------------

resource "databricks_volume" "genomics_reference" {
  catalog_name = databricks_catalog.insilico_trial.name
  schema_name  = databricks_schema.bronze.name
  name         = "genomics_reference"
  volume_type  = "EXTERNAL"
  comment      = "Raw (licensed) genomic reference datasets used to sample population priors"

  # An external volume must live inside an external location, not at a raw S3 URL.
  storage_location = databricks_external_location.genomics.url

  depends_on = [databricks_external_location.genomics]
}

resource "databricks_volume" "simulation_artifacts" {
  catalog_name = databricks_catalog.insilico_trial.name
  schema_name  = databricks_schema.gold.name
  name         = "simulation_artifacts"
  volume_type  = "MANAGED"
  comment      = "Generated reports, CDISC-inspired exports, LLM traces and run manifests"
}

# ---------------------------------------------------------------------------
# Privileges
# ---------------------------------------------------------------------------

resource "databricks_grants" "catalog" {
  catalog = databricks_catalog.insilico_trial.name

  grant {
    principal  = "account users"
    privileges = ["USE_CATALOG", "USE_SCHEMA"]
  }

  grant {
    principal  = databricks_group.simulation_readers.display_name
    privileges = ["USE_CATALOG", "USE_SCHEMA", "SELECT"]
  }
}

resource "databricks_grants" "bronze" {
  schema = databricks_schema.bronze.id

  grant {
    principal  = "protocol_agent_service_principal"
    privileges = ["USE_SCHEMA", "SELECT", "MODIFY", "CREATE_TABLE"]
  }

  grant {
    principal  = databricks_group.simulation_readers.display_name
    privileges = ["USE_SCHEMA", "SELECT"]
  }
}

resource "databricks_grants" "silver" {
  schema = databricks_schema.silver.id

  grant {
    principal  = "patient_agent_service_principal"
    privileges = ["USE_SCHEMA", "SELECT", "MODIFY", "CREATE_TABLE"]
  }

  grant {
    principal  = "biostatistician_agent_service_principal"
    privileges = ["USE_SCHEMA", "SELECT"]
  }

  grant {
    principal  = databricks_group.simulation_readers.display_name
    privileges = ["USE_SCHEMA", "SELECT"]
  }
}

resource "databricks_grants" "gold" {
  schema = databricks_schema.gold.id

  grant {
    principal  = "biostatistician_agent_service_principal"
    privileges = ["USE_SCHEMA", "SELECT", "MODIFY", "CREATE_TABLE"]
  }

  # The Data Safety Monitoring Board and human reviewers read Gold only.
  grant {
    principal  = databricks_group.simulation_readers.display_name
    privileges = ["USE_SCHEMA", "SELECT"]
  }

  grant {
    principal  = "patient_agent_service_principal"
    privileges = ["USE_SCHEMA", "SELECT"]
  }
}

resource "databricks_grants" "genomics_volume" {
  volume = databricks_volume.genomics_reference.id

  grant {
    principal  = "protocol_agent_service_principal"
    privileges = ["READ_VOLUME"]
  }

  grant {
    principal  = databricks_group.simulation_readers.display_name
    privileges = ["READ_VOLUME"]
  }
}

resource "databricks_grants" "artifacts_volume" {
  volume = databricks_volume.simulation_artifacts.id

  grant {
    principal  = "biostatistician_agent_service_principal"
    privileges = ["READ_VOLUME", "WRITE_VOLUME"]
  }

  grant {
    principal  = "patient_agent_service_principal"
    privileges = ["READ_VOLUME", "WRITE_VOLUME"]
  }

  grant {
    principal  = databricks_group.simulation_readers.display_name
    privileges = ["READ_VOLUME"]
  }
}
