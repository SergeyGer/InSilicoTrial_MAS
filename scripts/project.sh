#!/usr/bin/env bash
# ===========================================================================
#  InSilicoTrial MAS - start, stop and inspect this project's containers.
#
#  Written for a machine that hosts several projects: every command touches only
#  the containers, network and volumes of this compose project, and nothing here
#  starts on its own after a reboot.
#
#      bash scripts/project.sh start            # Studio + a demo run (http://localhost:8765)
#      bash scripts/project.sh start studio     # only the Studio
#      bash scripts/project.sh start spark      # Spark profile: JRE image, one simulation
#      bash scripts/project.sh start dev        # interactive shell in the dev image
#      bash scripts/project.sh status           # what this project runs + what else runs here
#      bash scripts/project.sh logs studio      # follow a service log
#      bash scripts/project.sh stop             # stop the containers, keep the results volume
#      bash scripts/project.sh down             # stop and remove containers/network
#      bash scripts/project.sh purge            # also delete the results volume (asks first)
#      bash scripts/project.sh url              # where the Studio answers
#      bash scripts/project.sh doctor           # ports, docker, compose, other projects
#
#  Works on Linux, macOS, WSL and Git Bash. On Windows PowerShell use
#  scripts/project.ps1 (same verbs).
# ===========================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# On a shared server each developer works under their own compose project name and
# port, so two people in the same checkout never collide. Both can be pinned in .env.
DEFAULT_PROJECT="insilico-trial-mas-$(id -un 2>/dev/null || echo dev)"
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-$DEFAULT_PROJECT}"
PROJECT_NAME="$COMPOSE_PROJECT_NAME"

# Port: honour an explicit INSILICO_STUDIO_PORT, otherwise take the first free one.
free_port() {
  local candidate
  for candidate in $(seq "${1:-8765}" "$(( ${1:-8765} + 30 ))"); do
    if command -v ss >/dev/null 2>&1; then
      ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$candidate\$" || { echo "$candidate"; return; }
    else
      (exec 3<>"/dev/tcp/127.0.0.1/$candidate") 2>/dev/null || { echo "$candidate"; return; }
    fi
  done
  echo "$1"
}

if [[ -n "${INSILICO_STUDIO_PORT:-}" ]]; then
  STUDIO_PORT="$INSILICO_STUDIO_PORT"
  PORT_IS_PINNED="true"
else
  STUDIO_PORT="$(free_port 8765)"
  PORT_IS_PINNED="false"
fi
export INSILICO_STUDIO_PORT="$STUDIO_PORT"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
step() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
ok()   { printf '    \033[32mok\033[0m   %s\n' "$1"; }
warn() { printf '    \033[33mwarn\033[0m %s\n' "$1"; }
fail() { printf '\n\033[1;31mERROR:\033[0m %s\n' "$1" >&2; exit 1; }

usage() {
  sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Services
  studio   the readout UI (published on INSILICO_STUDIO_PORT, default 8765)
  demo     one-shot simulation; exits when the report is written
  spark    Spark-engine simulation (profile: spark)
  dev      interactive shell in the development image (profile: tools)

The compose project name isolates this stack from everything else on the machine:
its containers are prefixed with it, and its results live in the volume
insilico-trial-mas_artifacts.
EOF
}

require_docker() {
  command -v docker >/dev/null 2>&1 || fail "docker is not installed or not on PATH"
  docker info >/dev/null 2>&1 || fail "the Docker daemon is not reachable (is Docker Desktop or dockerd running?)"
  docker compose version >/dev/null 2>&1 || fail "the docker compose plugin is required (docker compose version failed)"
}

compose() { DOCKER_CLI_HINTS=false docker compose "$@"; }

other_projects() {
  # Everything on this machine that is not this project, so a shared server stays
  # predictable: we only ever report on it, never touch it.
  compose ls --format json 2>/dev/null | python3 -c "
import json, sys
try:
    entries = json.load(sys.stdin)
except Exception:
    raise SystemExit
for entry in entries:
    if entry.get('Name') != '$PROJECT_NAME' and entry.get('Status'):
        print(f\"    {entry['Name']}: {entry['Status']}\")
" 2>/dev/null || true
}

port_busy() {
  # Returns 0 when something already listens on the port (outside this project).
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$STUDIO_PORT\$"
  elif command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"$STUDIO_PORT" -sTCP:LISTEN >/dev/null 2>&1
  else
    return 1
  fi
}

action_start() {
  local target="${1:-all}"
  require_docker
  step "Starting: $target"

  case "$target" in
    all)    compose up -d studio && compose up demo ;;
    studio) compose up -d studio ;;
    demo)   compose run --rm demo ;;
    spark)  compose --profile spark run --rm spark ;;
    dev)    compose --profile tools run --rm dev ;;
    test)   compose --profile ci build test ;;
    *)      fail "unknown service: $target (studio, demo, spark, dev, test, all)" ;;
  esac

  if [[ "$target" == "all" || "$target" == "studio" ]]; then
    ok "compose project: $PROJECT_NAME"
    ok "Studio: http://localhost:$STUDIO_PORT"
    [[ "$PORT_IS_PINNED" == "false" ]] && ok "(port chosen automatically; pin it in .env to keep it)"
    ok "stop it later with: bash scripts/project.sh stop"
  fi
}

action_stop() {
  require_docker
  step "Stopping this project's containers (other projects are left alone)"
  compose stop
  ok "containers stopped; the results volume is intact"
  ok "start again with: bash scripts/project.sh start"
}

action_down() {
  require_docker
  step "Stopping and removing this project's containers and network"
  compose --profile spark --profile tools --profile ci down --remove-orphans
  ok "removed; the results volume is kept (use 'purge' to delete it)"
}

action_purge() {
  require_docker
  step "This deletes the results volume of $PROJECT_NAME (all runs and reports)"
  read -r -p "    type 'yes' to continue: " answer
  [[ "$answer" == "yes" ]] || { warn "cancelled"; return 0; }
  compose --profile spark --profile tools --profile ci down -v --remove-orphans
  ok "containers, network and volume removed"
}

action_status() {
  require_docker
  step "This project: $PROJECT_NAME"
  local published
  published="$(compose port studio 8765 2>/dev/null | head -1 | awk -F: '{print $NF}')"
  echo "    studio port : ${published:-$STUDIO_PORT (not running)}"
  echo "    volume      : ${PROJECT_NAME}_artifacts"
  local running
  running="$(compose ps --format '{{.Service}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null || true)"
  if [[ -z "$running" ]]; then
    echo "    nothing is running"
  else
    echo "$running" | sed 's/^/    /'
  fi

  step "Ports published by this project"
  compose ps --format '{{.Service}} -> {{.Ports}}' 2>/dev/null | sed 's/^/    /' || true

  step "Other compose projects on this machine (read-only)"
  local others
  others="$(other_projects)"
  [[ -n "$others" ]] && echo "$others" || echo "    none"

  step "Restart policy of this project's containers"
  local ids
  ids="$(compose ps -q 2>/dev/null || true)"
  if [[ -z "$ids" ]]; then
    echo "    no containers to inspect"
  else
    # shellcheck disable=SC2086
    docker inspect --format '    {{.Name}}: restart={{.HostConfig.RestartPolicy.Name}}' $ids
    echo "    (restart=none means a server reboot never brings them back)"
  fi
}

action_logs() {
  require_docker
  local service="${1:-studio}"
  step "Following the log of $service (Ctrl-C to stop following)"
  compose logs -f --tail 50 "$service"
}

action_url() {
  local port
  port="$(compose port studio 8765 2>/dev/null | head -1 | awk -F: '{print $NF}')"
  [[ -n "$port" ]] || port="$STUDIO_PORT"
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) echo "http://localhost:$port" ;;   # Git Bash on Windows
    *) echo "http://localhost:$port" ;;
  esac
}

action_doctor() {
  require_docker
  step "Docker"
  docker version --format '    client {{.Client.Version}} / server {{.Server.Version}}'
  docker compose version | sed 's/^/    /'

  step "Ports"
  if compose ps --status running --services 2>/dev/null | grep -q '^studio$'; then
    ok "port $STUDIO_PORT is held by this project's Studio"
  elif port_busy; then
    warn "port $STUDIO_PORT is used by something else - set INSILICO_STUDIO_PORT"
  else
    ok "port $STUDIO_PORT is free (this project would use $STUDIO_PORT)"
  fi
  echo "    other developers' stacks keep their own COMPOSE_PROJECT_NAME and port"

  step "Autostart after a reboot"
  ok "every service declares restart: \"no\""
  echo "    containers started earlier by another policy can be fixed with:"
  echo "      docker update --restart=no \$(docker ps -aq)"

  step "Disk footprint"
  docker system df --format '    {{.Type}}: {{.Size}} (reclaimable {{.Reclaimable}})' | head -4

  step "Other compose projects"
  local others
  others="$(other_projects)"
  [[ -n "$others" ]] && echo "$others" || echo "    none"
}

main() {
  local verb="${1:-help}"
  shift || true
  case "$verb" in
    start)   action_start "${1:-all}" ;;
    stop)    action_stop ;;
    down)    action_down ;;
    purge)   action_purge ;;
    restart) action_stop; action_start "${1:-all}" ;;
    status|ps) action_status ;;
    logs)    action_logs "${1:-studio}" ;;
    url)     action_url ;;
    doctor)  action_doctor ;;
    help|-h|--help) usage ;;
    *)       fail "unknown command: $verb (try --help)" ;;
  esac
}

main "$@"
