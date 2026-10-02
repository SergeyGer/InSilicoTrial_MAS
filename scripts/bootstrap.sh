#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# One-command developer bootstrap.
#
#   bash scripts/bootstrap.sh            # venv + dev/llm/tracking extras
#   bash scripts/bootstrap.sh --spark    # also install PySpark/Delta + a portable JRE
#   bash scripts/bootstrap.sh --all      # everything, including gradient boosting
#
# The JRE is downloaded into .toolchain/ (git-ignored) so local Spark runs work
# without root privileges or a system JDK.
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VENV_DIR=".venv"
JRE_DIR=".toolchain/jre17"
EXTRAS="dev,llm,tracking"

for arg in "$@"; do
  case "$arg" in
    --spark) EXTRAS="${EXTRAS},spark" ;;
    --ml) EXTRAS="${EXTRAS},ml" ;;
    --all) EXTRAS="dev,llm,tracking,spark,ml" ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -n 12
      exit 0
      ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

PYTHON_BIN="${PYTHON_BIN:-python3}"
echo "==> python: $($PYTHON_BIN --version)"

if [ ! -d "$VENV_DIR" ]; then
  echo "==> creating virtual environment in $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# Keep pip's cache inside the repository: some sandboxed/CI environments have a
# read-only ~/.cache, and wheel builds (PySpark) fail without a writable cache.
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-$ROOT_DIR/.toolchain/pip-cache}"
mkdir -p "$PIP_CACHE_DIR"

echo "==> installing the package with extras: $EXTRAS"
"$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel >/dev/null
"$VENV_DIR/bin/python" -m pip install -e ".[${EXTRAS}]"

if [[ "$EXTRAS" == *spark* ]]; then
  if ! command -v java >/dev/null 2>&1 && [ ! -x "$JRE_DIR/bin/java" ]; then
    echo "==> no JVM found; downloading a portable Temurin JRE 17 into $JRE_DIR"
    mkdir -p "$JRE_DIR"
    curl -sSL -o /tmp/insilico-jre.tar.gz \
      "https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jre/hotspot/normal/eclipse"
    tar -xzf /tmp/insilico-jre.tar.gz -C "$JRE_DIR" --strip-components=1
  fi
  if [ -x "$JRE_DIR/bin/java" ]; then
    echo "==> local JVM: $("$JRE_DIR/bin/java" -version 2>&1 | head -n 1)"
    echo "    export JAVA_HOME=$ROOT_DIR/$JRE_DIR"
  fi
fi

echo
echo "==> environment checklist"
"$VENV_DIR/bin/python" -m insilico_trial_mas.cli env-check || true

cat <<'EOF'

Next steps
----------
  source .venv/bin/activate
  make demo                 # small end-to-end run with the offline LLM
  make test                 # fast test suite
  make simulate PATIENTS=10000 ENGINE=local
  make simulate-spark       # needs JAVA_HOME (see above)
  code .                    # VS Code picks up .vscode/ automatically
EOF
