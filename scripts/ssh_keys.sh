#!/usr/bin/env bash
# ===========================================================================
#  Manage the SSH keys that may log into this account.
#
#  Written for a machine that several laptops connect to: every key carries a
#  comment that names the device, so `list` answers "which key belongs to which
#  laptop" and `revoke` removes exactly one of them.
#
#      bash scripts/ssh_keys.sh list                  # keys, fingerprints, comments
#      bash scripts/ssh_keys.sh check                 # permissions, sshd policy, attack noise
#      bash scripts/ssh_keys.sh add "$(cat ~/laptop.pub)"
#      bash scripts/ssh_keys.sh add --file ~/laptop.pub
#      bash scripts/ssh_keys.sh revoke SHA256:AbCdEf  # by fingerprint prefix
#      bash scripts/ssh_keys.sh test 188.245.2.148    # publickey-only login attempt
#
#  Every change is validated first, a timestamped backup is written next to the
#  file, and the permissions are normalised afterwards (700 for ~/.ssh, 600 for
#  authorized_keys) because sshd silently ignores keys in a world-writable file.
# ===========================================================================
set -euo pipefail

AUTH_KEYS="${SSH_AUTH_KEYS:-$HOME/.ssh/authorized_keys}"
SSH_DIR="$(dirname "$AUTH_KEYS")"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
step() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
ok()   { printf '    \033[32mok\033[0m   %s\n' "$1"; }
warn() { printf '    \033[33mwarn\033[0m %s\n' "$1"; }
fail() { printf '\n\033[1;31mERROR:\033[0m %s\n' "$1" >&2; exit 1; }

usage() {
  sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
}

ensure_file() {
  mkdir -p "$SSH_DIR"
  chmod 700 "$SSH_DIR"
  [ -f "$AUTH_KEYS" ] || : > "$AUTH_KEYS"
  chmod 600 "$AUTH_KEYS"
}

count_keys() {
  # awk never fails on an empty file, unlike `grep -c` whose non-zero exit status
  # would make a `|| echo 0` fallback print a second "0" and break the numeric test.
  [ -f "$AUTH_KEYS" ] || { echo 0; return; }
  awk 'NF && $1 !~ /^#/' "$AUTH_KEYS" | wc -l | tr -d ' '
}

normalise_line() {
  # Trim whitespace, keep the key material and its comment.
  printf '%s' "$1" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

fingerprint_of() {
  # Echo "<fingerprint> <comment>" for a single key line, or nothing if invalid.
  local line="$1" tmp
  tmp="$(mktemp)"
  printf '%s\n' "$line" > "$tmp"
  ssh-keygen -lf "$tmp" 2>/dev/null || true
  rm -f "$tmp"
}

action_list() {
  step "Keys allowed to log in as $(id -un)@$(hostname -s)"
  echo "    file: $AUTH_KEYS"
  local total
  total="$(count_keys)"
  echo "    keys: $total"
  if [ "$total" -eq 0 ]; then
    warn "no keys installed - this account can only be reached with a password"
    return 0
  fi
  echo
  printf '    %-24s %-10s %s\n' "FINGERPRINT" "TYPE" "COMMENT (device)"
  local index=0 line fp
  while IFS= read -r line; do
    case "$line" in ''|\#*) continue ;; esac
    index=$((index + 1))
    fp="$(fingerprint_of "$line")"
    if [ -z "$fp" ]; then
      printf '    %-24s %-10s %s\n' "unparsable" "-" "line $index"
      continue
    fi
    # ssh-keygen -lf prints: "<bits> SHA256:xxx comment (ED25519)"
    local digest type comment
    digest="$(printf '%s' "$fp" | awk '{print $2}')"
    type="$(printf '%s' "$fp" | sed -n 's/.*(\(.*\))$/\1/p')"
    comment="$(printf '%s' "$fp" | sed -e 's/^[0-9]* //' -e 's/^[^ ]* //' -e 's/ ([^)]*)$//')"
    printf '    %-24s %-10s %s\n' "$digest" "${type:-?}" "${comment:-<no comment>}"
  done < "$AUTH_KEYS"
  echo
  echo "    A key without a device comment cannot be traced - regenerate it with"
  echo "    ssh-keygen -t ed25519 -C \"laptop-home-2026-10\"."
}

action_check() {
  step "Permissions"
  stat -c '    %n: %a %U:%G' "$SSH_DIR" 2>/dev/null || warn "$SSH_DIR is missing"
  if [ -f "$AUTH_KEYS" ]; then
    local mode
    mode="$(stat -c '%a' "$AUTH_KEYS")"
    if [ "$mode" = "600" ] || [ "$mode" = "640" ]; then
      ok "authorized_keys is $mode (sshd requires it to be private)"
    else
      warn "authorized_keys is $mode - run: chmod 600 $AUTH_KEYS"
    fi
    ok "keys installed: $(count_keys)"
  else
    warn "$AUTH_KEYS does not exist yet"
  fi

  step "sshd policy that decides whether keys work"
  if command -v sshd >/dev/null 2>&1 && [ "$(id -u)" -eq 0 ]; then
    sshd -T 2>/dev/null | grep -iE "^(pubkeyauthentication|authorizedkeysfile|permitrootlogin|passwordauthentication|maxauthtries|logingracetime)" | sed 's/^/    /'
  else
    warn "run as root to read the effective sshd configuration"
  fi

  step "Recent authentication activity"
  if command -v journalctl >/dev/null 2>&1; then
    local failed accepted
    failed="$(journalctl -u ssh --since '24 hours ago' --no-pager 2>/dev/null | grep -c 'Failed password' || true)"
    accepted="$(journalctl -u ssh --since '24 hours ago' --no-pager 2>/dev/null | grep -c 'Accepted' || true)"
    echo "    failed password attempts (24h): ${failed:-0}"
    echo "    accepted logins (24h)         : ${accepted:-0}"
    journalctl -u ssh --since '24 hours ago' --no-pager 2>/dev/null \
      | grep 'Accepted publickey' | tail -3 | sed 's/^/    /' || true
    if [ "${failed:-0}" -gt 100 ]; then
      echo
      warn "the account is being brute-forced: install a key, verify it, then set"
      warn "PasswordAuthentication no in /etc/ssh/sshd_config and reload sshd"
    fi
  else
    warn "journalctl is unavailable; cannot summarise authentication activity"
  fi
}

action_add() {
  local source="$1"
  local key
  if [ "$source" = "--file" ]; then
    [ -n "${2:-}" ] || fail "add --file needs a path"
    [ -f "$2" ] || fail "no such file: $2"
    key="$(cat "$2")"
  else
    key="$source"
  fi
  [ -n "$key" ] || fail 'add needs a public key: add "ssh-ed25519 AAAA... comment"'

  ensure_file
  step "Validating"
  local added=0 line
  while IFS= read -r line; do
    line="$(normalise_line "$line")"
    case "$line" in ''|\#*) continue ;; esac
    local fp digest
    fp="$(fingerprint_of "$line")"
    [ -n "$fp" ] || fail "this does not look like a public key: ${line:0:40}..."
    digest="$(printf '%s' "$fp" | awk '{print $2}')"
    if ssh-keygen -lf "$AUTH_KEYS" 2>/dev/null | awk '{print $2}' | grep -qx "$digest"; then
      warn "$digest is already installed - nothing to do"
      continue
    fi
    ok "$digest ($(printf '%s' "$fp" | sed -n 's/.*(\(.*\))$/\1/p')) accepted"
    cp -p "$AUTH_KEYS" "${AUTH_KEYS}.bak-$(date +%Y%m%d-%H%M%S)"
    printf '%s\n' "$line" >> "$AUTH_KEYS"
    added=$((added + 1))
  done <<< "$key"

  chmod 600 "$AUTH_KEYS"
  chmod 700 "$SSH_DIR"
  if [ "$added" -eq 0 ]; then
    warn "no new key was added"
  else
    ok "added $added key(s); backup written next to $AUTH_KEYS"
  fi
  echo
  action_list
}

action_revoke() {
  local needle="${1:-}"
  [ -n "$needle" ] || fail "revoke needs a fingerprint (or a unique part of it)"
  [ -f "$AUTH_KEYS" ] || fail "$AUTH_KEYS does not exist"
  step "Looking for $needle"
  local tmp removed=0 line fp digest
  tmp="$(mktemp)"
  while IFS= read -r line; do
    case "$line" in ''|\#*) printf '%s\n' "$line" >> "$tmp"; continue ;; esac
    fp="$(fingerprint_of "$line")"
    digest="$(printf '%s' "$fp" | awk '{print $2}')"
    if [ -n "$digest" ] && [[ "$digest" == *"$needle"* ]]; then
      ok "removing $digest"
      removed=$((removed + 1))
      continue
    fi
    printf '%s\n' "$line" >> "$tmp"
  done < "$AUTH_KEYS"
  if [ "$removed" -eq 0 ]; then
    rm -f "$tmp"
    fail "no key matched $needle"
  fi
  cp -p "$AUTH_KEYS" "${AUTH_KEYS}.bak-$(date +%Y%m%d-%H%M%S)"
  cat "$tmp" > "$AUTH_KEYS"
  rm -f "$tmp"
  chmod 600 "$AUTH_KEYS"
  ok "revoked $removed key(s)"
  echo
  action_list
}

action_test() {
  local host="${1:-}"
  [ -n "$host" ] || fail "test needs a host, for example: test 188.245.2.148"
  step "Publickey-only login to $host"
  echo "    the password prompt is disabled on purpose: if the key is installed and"
  echo "    configured, this succeeds without any input."
  if ssh -o BatchMode=yes -o PreferredAuthentications=publickey -o PasswordAuthentication=no \
        -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "$host" 'echo "    ok   publickey login works: $(hostname -s) as $(id -un)"'; then
    return 0
  fi
  warn "publickey login did not succeed (wrong user, key not offered, or not installed)"
  echo "    check: the IdentityFile in ~/.ssh/config, then 'ssh -v $host'"
  return 1
}

main() {
  local command="${1:-help}"
  shift || true
  case "$command" in
    list)   action_list ;;
    check)  action_check ;;
    add)    action_add "${1:-}" "${2:-}" ;;
    revoke) action_revoke "${1:-}" ;;
    test)   action_test "${1:-}" ;;
    help|-h|--help) usage ;;
    *) fail "unknown command: $command (try --help)" ;;
  esac
}

main "$@"
