#!/usr/bin/env bash
# Runs once, inside the dev container, after VS Code has created it.
#
# It must stay fast and must not require network access: the image already has the
# package and the dev tooling, so this is a health check with helpful output rather
# than an installer. Anything that genuinely needs installing is printed as a command
# the developer can choose to run.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "==> InSilicoTrial MAS development container"
echo "    python     : $(python --version 2>&1) at $(command -v python)"
echo "    user       : $(id -un) (uid $(id -u))"
echo "    workspace  : $(pwd)"

if python -c "import insilico_trial_mas" 2>/dev/null; then
  echo "    package    : insilico_trial_mas $(python -c 'import insilico_trial_mas as m; print(getattr(m, "__version__", "unknown"))' 2>/dev/null || echo installed)"
else
  echo "    package    : NOT importable - run:  pip install -e '.[dev]'"
fi

if python -c "import pytest, ruff" 2>/dev/null; then
  echo "    dev tools  : pytest, ruff available"
else
  echo "    dev tools  : missing - run:  pip install -e '.[dev]'"
fi

echo
python -m insilico_trial_mas.cli env-check 2>/dev/null | sed 's/^/    /' || true

cat <<'EOF'

    Useful commands inside this container
      make check                  # ruff + mypy + the fast test suite
      make test                   # pytest only
      insilico-trial demo --patients 200 --epochs 3
      insilico-trial studio --host 0.0.0.0 --port 8765   # then open the forwarded port
      make docker-studio          # only if the docker-outside-of-docker feature is enabled

    The containers of this project are started and stopped from the *host*:
      bash scripts/project.sh start | stop | status      (Windows: .\scripts\project.ps1 ...)
EOF
