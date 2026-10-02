#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Deploy the platform to Databricks.
#
#   bash scripts/databricks_deploy.sh --target dev
#   bash scripts/databricks_deploy.sh --target prod --run
#
# Requires the Databricks CLI (>= 0.220) authenticated against the workspace, and
# Terraform applied first (see terraform/README.md) so the catalog, schemas,
# service principals and external locations exist.
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

TARGET="dev"
RUN_JOB="false"
VERIFY="true"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) TARGET="$2"; shift 2 ;;
    --run) RUN_JOB="true"; shift ;;
    --skip-verify) VERIFY="false"; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -n 8; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

command -v databricks >/dev/null 2>&1 || {
  echo "the Databricks CLI is not installed: https://docs.databricks.com/dev-tools/cli/" >&2
  exit 1
}

if [ "$VERIFY" = "true" ]; then
  echo "==> validating the bundle against target '$TARGET'"
  databricks bundle validate -t "$TARGET"
fi

echo "==> deploying the bundle to target '$TARGET'"
databricks bundle deploy -t "$TARGET"

if [ "$RUN_JOB" = "true" ]; then
  echo "==> triggering the simulation job"
  databricks bundle run insilico_trial_simulation -t "$TARGET"
else
  cat <<EOF

Deployed. To run:

  databricks bundle run insilico_trial_simulation -t $TARGET
  databricks bundle run insilico_trial_nightly_regression -t $TARGET

Inspect the results:

  SELECT * FROM <catalog>.gold.arm_summaries ORDER BY sim_run_id DESC LIMIT 20;
  DESCRIBE HISTORY <catalog>.silver.patient_states;
  SELECT * FROM <catalog>.silver.patient_states VERSION AS OF 1 WHERE sbp > 140;
EOF
fi
