#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Publish this working tree to GitHub.
#
#   bash scripts/publish_github.sh                 # commit + push to main
#   bash scripts/publish_github.sh --dry-run       # show what would happen
#   GITHUB_TOKEN=ghp_... bash scripts/publish_github.sh --setup   # also apply About/topics
#
# Authentication, in order of preference:
#   1. GITHUB_TOKEN / GH_TOKEN in the environment (fine-grained or classic PAT
#      with 'repo' scope) - the token is never printed and never written to disk;
#   2. an existing SSH key (`git@github.com:...` remote);
#   3. the GitHub CLI (`gh auth login`), if installed.
#
# The script refuses to publish anything that fails the quality gates unless
# --skip-checks is passed explicitly.
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

OWNER="SergeyGer"
REPO="InSilicoTrial_MAS"
REMOTE_HTTPS="https://github.com/${OWNER}/${REPO}.git"
REMOTE_SSH="git@github.com:${OWNER}/${REPO}.git"
BRANCH="main"

DRY_RUN="false"
SKIP_CHECKS="false"
DO_SETUP="false"
MESSAGE="feat: InSilicoTrial MAS - multi-agent in-silico clinical trial simulation platform"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN="true"; shift ;;
    --skip-checks) SKIP_CHECKS="true"; shift ;;
    --setup) DO_SETUP="true"; shift ;;
    --message) MESSAGE="$2"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -n 20; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
note() { printf '    %s\n' "$1"; }

# ---------------------------------------------------------------- git basics
if ! command -v git >/dev/null 2>&1; then
  echo "git is not installed" >&2
  exit 1
fi

if [ ! -d .git ]; then
  step "Initialising the repository"
  note "git init -b ${BRANCH}"
  [ "$DRY_RUN" = "true" ] || git init -q -b "$BRANCH"
fi

if [ -z "$(git config user.name || true)" ]; then
  step "Configuring a local commit identity (repository scope only)"
  note "git config user.name  'Sergey Ger'"
  note "git config user.email '<your GitHub e-mail>'"
  [ "$DRY_RUN" = "true" ] || {
    git config user.name "Sergey Ger"
    git config user.email "${GIT_AUTHOR_EMAIL:-sergey@users.noreply.github.com}"
  }
fi

# ---------------------------------------------------------------- quality gates
if [ "$SKIP_CHECKS" = "false" ]; then
  step "Running the quality gates (--skip-checks to bypass)"
  if [ -x .venv/bin/python ]; then
    .venv/bin/python -m ruff check src tests scripts
    .venv/bin/python -m mypy
    .venv/bin/python -m pytest -q -p no:warnings -m "not spark"
    .venv/bin/python scripts/render_architecture.py --check
    .venv/bin/python scripts/render_social_preview.py --check
    .venv/bin/python scripts/render_ui_previews.py --check
  else
    note "no .venv found - run 'bash scripts/bootstrap.sh' first, or pass --skip-checks"
    exit 1
  fi
fi

# ---------------------------------------------------------------- stage + commit
step "Staging the working tree"
[ "$DRY_RUN" = "true" ] || git add -A
if [ "$DRY_RUN" = "true" ]; then
  note "would commit: $(git status --porcelain | wc -l) paths"
else
  if git diff --cached --quiet; then
    note "nothing to commit - the tree is already clean"
  else
    git commit -q -m "$MESSAGE"
    note "committed $(git diff --stat HEAD~1 HEAD | tail -1)"
  fi
fi

# ---------------------------------------------------------------- remote
step "Configuring the remote"
TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
if [ -n "$TOKEN" ]; then
  REMOTE_URL="https://x-access-token:${TOKEN}@github.com/${OWNER}/${REPO}.git"
  note "using GITHUB_TOKEN (remote URL is not persisted)"
elif [ -f "$HOME/.ssh/id_ed25519" ] || [ -f "$HOME/.ssh/id_rsa" ]; then
  REMOTE_URL="$REMOTE_SSH"
  note "using SSH"
else
  REMOTE_URL="$REMOTE_HTTPS"
  note "no token and no SSH key found - the push will prompt for credentials"
fi

if git remote get-url origin >/dev/null 2>&1; then
  [ "$DRY_RUN" = "true" ] || git remote set-url origin "$REMOTE_URL"
  note "origin updated"
else
  [ "$DRY_RUN" = "true" ] || git remote add origin "$REMOTE_URL"
  note "origin added"
fi

# ---------------------------------------------------------------- push
step "Pushing ${BRANCH} to ${OWNER}/${REPO}"
if [ "$DRY_RUN" = "true" ]; then
  note "git push -u origin ${BRANCH} --force-with-lease"
else
  if [ -n "$TOKEN" ]; then
    # Keep the token out of the persisted config: push with an explicit URL.
    git push "$REMOTE_URL" "HEAD:${BRANCH}"
    git remote set-url origin "$REMOTE_HTTPS"
  else
    git push -u origin "$BRANCH"
  fi
fi

# ---------------------------------------------------------------- repo settings
if [ "$DO_SETUP" = "true" ]; then
  step "Applying the About box and topics"
  if [ -n "$TOKEN" ]; then
    [ "$DRY_RUN" = "true" ] && .venv/bin/python scripts/github_repo_setup.py --dry-run \
      || GITHUB_TOKEN="$TOKEN" .venv/bin/python scripts/github_repo_setup.py
  else
    note "skipped: set GITHUB_TOKEN to apply the About box, topics and branch protection"
  fi
fi

step "Done"
cat <<EOF
    Repository : https://github.com/${OWNER}/${REPO}
    Actions    : https://github.com/${OWNER}/${REPO}/actions
    Settings   : https://github.com/${OWNER}/${REPO}/settings

    Still manual (GitHub has no API for it):
      Settings -> Social preview -> upload .github/social-preview.png
EOF
