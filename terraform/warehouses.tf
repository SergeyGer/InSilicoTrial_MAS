# ---------------------------------------------------------------------------
# Consumption layer: SQL warehouse for the Gold tables and observability wiring.
# ---------------------------------------------------------------------------

resource "databricks_sql_endpoint" "analytics" {
  name             = "insilico-trial-analytics-${var.environment}"
  cluster_size     = var.sql_warehouse_size
  warehouse_type   = "PRO"
  max_num_clusters = 3

  auto_stop_mins            = 10
  min_num_clusters          = 1
  enable_serverless_compute = true
  tags {
    custom_tags {
      key   = "Project"
      value = "insilico-trial-mas"
    }
  }

  channel {
    name = "CHANNEL_NAME_CURRENT"
  }
}

# ---------------------------------------------------------------------------
# MLflow experiment location for the simulation runs. The platform also works
# with the workspace default experiment, but an explicit path makes retention and
# permissions reviewable.
# ---------------------------------------------------------------------------

resource "databricks_mlflow_experiment" "simulation" {
  name              = "/Shared/insilico_trial_mas_${var.environment}"
  artifact_location = "${databricks_external_location.lakehouse.url}/mlflow-artifacts"

  depends_on = [databricks_external_location.lakehouse]
}

# ---------------------------------------------------------------------------
# Cluster policy: what a simulation cluster is allowed to be. Keeping the policy
# in Terraform prevents "temporary" 200-node clusters from becoming the norm.
# ---------------------------------------------------------------------------

resource "databricks_cluster_policy" "simulation" {
  name = "insilico-trial-simulation-${var.environment}"

  definition = jsonencode({
    "spark_version" : {
      "type" : "fixed",
      "value" : "15.4.x-scala2.12"
    },
    "node_type_id" : {
      "type" : "allowlist",
      "values" : ["m5.2xlarge", "m5.4xlarge", "m5.8xlarge", "m5a.2xlarge", "m5a.4xlarge"]
    },
    "autoscale" : {
      "type" : "range",
      "minValue" : 1,
      "maxValue" : 16
    },
    "custom_tags.Project" : {
      "type" : "fixed",
      "value" : "insilico-trial-mas"
    },
    "spark_conf.spark.sql.execution.arrow.pyspark.enabled" : {
      "type" : "fixed",
      "value" : "true"
    },
    "spark_conf.spark.databricks.delta.optimizeWrite.enabled" : {
      "type" : "fixed",
      "value" : "true"
    },
    "spark_conf.spark.databricks.delta.autoCompact.enabled" : {
      "type" : "fixed",
      "value" : "true"
    },
    "dbus_per_hour" : {
      "type" : "range",
      "maxValue" : 400
    }
  })
}
