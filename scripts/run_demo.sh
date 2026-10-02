#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Run a full simulation and print the readout.
#
#   bash scripts/run_demo.sh                          # 2000 patients, local engine
#   bash scripts/run_demo.sh --patients 10000 --engine local
#   bash scripts/run_demo.sh --engine spark           # requires JAVA_HOME or .toolchain/jre17
#   bash scripts/run_demo.sh --config conf/simulation_cluster.yaml --engine spark
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PY=".venv/bin/python"
CONFIG="conf/simulation_local.yaml"
PATIENTS="2000"
COHORTS="8"
EPOCHS=""
ENGINE="local"
OUT="artifacts/run"
LLM_MODE="triggered"
OFFLINE_FLAG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --patients) PATIENTS="$2"; shift 2 ;;
    --cohorts) COHORTS="$2"; shift 2 ;;
    --epochs) EPOCHS="$2"; shift 2 ;;
    --engine) ENGINE="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --llm-mode) LLM_MODE="$2"; shift 2 ;;
    --offline) OFFLINE_FLAG="--offline"; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -n 10; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [ ! -x "$PY" ]; then
  echo "virtual environment missing - run: bash scripts/bootstrap.sh" >&2
  exit 1
fi

if [ "$ENGINE" = "spark" ] && [ -z "${JAVA_HOME:-}" ] && [ -x ".toolchain/jre17/bin/java" ]; then
  export JAVA_HOME="$ROOT_DIR/.toolchain/jre17"
  export PATH="$JAVA_HOME/bin:$PATH"
  echo "==> using the portable JVM at $JAVA_HOME"
fi

ARGS=(simulate --config "$CONFIG" --patients "$PATIENTS" --cohorts "$COHORTS"
      --engine "$ENGINE" --llm-mode "$LLM_MODE" --output-dir "$OUT")
[ -n "$EPOCHS" ] && ARGS+=(--epochs "$EPOCHS")
[ -n "$OFFLINE_FLAG" ] && ARGS+=("$OFFLINE_FLAG")

echo "==> insilico-trial ${ARGS[*]}"
"$PY" -m insilico_trial_mas.cli "${ARGS[@]}"

echo
echo "==> artefacts under $OUT"
find "$OUT" -maxdepth 2 -name 'run_manifest.json' -o -maxdepth 2 -name 'trial_report.md' | sort | tail -n 4
