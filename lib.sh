#!/bin/bash
# ============================================================================
# lib.sh — shared helpers for the LAB-FLEET-OPS scripts
#
# Derived from mesh-voice-fleet-rollout/lib.sh, with two fixes learned the
# hard way on this fleet:
#   * put_file(): writes via a plain /tmp staging file + `sudo install`,
#     instead of `sudo -S tee`. When sudo's credential timestamp is cached,
#     `sudo -S tee` does NOT consume the piped password line and it ends up
#     as line 1 of the file. Staging to /tmp (which the login user owns)
#     avoids that entirely.
#   * ssh_root(): always wraps the whole command in `sudo -S bash -c '...'`
#     so `&&`/`||`/`;` chains all run as root (a bare `sudo -S a && b` only
#     runs `a` as root).
#
# Config: reads .credentials next to this file (SSH_USER, SUDO_PASS, SSH_KEY).
# SSH login is key-based; SUDO_PASS is only for remote sudo.
# ============================================================================

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CRED="$LIB_DIR/.credentials"
[[ -f "$CRED" ]] && source "$CRED"
SSH_USER="${SSH_USER:-radio}"
SUDO_PASS="${SUDO_PASS:-}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/mesh-fleet}"
SSH_OPTS="-i $SSH_KEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=6 -o BatchMode=yes"

log()  { echo "  $*"; }
ok()   { echo "  OK   $*"; }
warn() { echo "  WARN $*"; }
fail() { echo "  FAIL $*"; }

# Plain (non-root) SSH command.
ssh_cmd() { local ip="$1"; shift; ssh $SSH_OPTS "$SSH_USER@$ip" "$@"; }

# Root command — whole thing runs under sudo via bash -c.
ssh_root() {
  local ip="$1"; shift
  printf '%s\n' "$SUDO_PASS" | ssh $SSH_OPTS "$SSH_USER@$ip" "sudo -S bash -c '$*'" 2>&1 | grep -v '^\[sudo'
}

# Root command with an explicit sane PATH for tools in /usr/sbin & /usr/local/bin.
ssh_rootp() {
  local ip="$1"; shift
  ssh_root "$ip" "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; $*"
}

# put_file <ip> <remote_path> <content> [mode]  — safe root file write.
put_file() {
  local ip="$1" path="$2" content="$3" mode="${4:-644}" stamp
  stamp="/tmp/.stage.$$.$RANDOM"
  printf '%s' "$content" | ssh $SSH_OPTS "$SSH_USER@$ip" "cat > $stamp"
  printf '%s\n' "$SUDO_PASS" | ssh $SSH_OPTS "$SSH_USER@$ip" \
    "sudo -S bash -c 'install -m $mode -o root -g root $stamp \"$path\" && rm -f $stamp'" 2>&1 | grep -v '^\[sudo'
}

# put_scp <local> <ip> <remote_path> [mode] — copy a (binary) file as root.
put_scp() {
  local src="$1" ip="$2" path="$3" mode="${4:-644}" stamp
  stamp="/tmp/.stage.$$.$RANDOM"
  scp $SSH_OPTS "$src" "$SSH_USER@$ip:$stamp" >/dev/null 2>&1
  ssh_root "$ip" "install -m $mode -o root -g root $stamp \"$path\" && rm -f $stamp"
}

# Mandatory hostname guard before any mutating/rebooting action.
verify_hostname() {
  local ip="$1" expected="$2" actual
  actual=$(ssh_cmd "$ip" hostname 2>/dev/null)
  if [[ -z "$actual" ]]; then fail "no response from $ip (expected $expected)"; return 1; fi
  if [[ "$actual" != "$expected" ]]; then
    fail "ABORT: expected '$expected' at $ip but got '$actual' — refusing to act on wrong node."; return 1
  fi
  ok "confirmed $ip is $expected"; return 0
}

# Wait for a node to answer ping again after a reboot (max ~5 min).
wait_online() {
  local ip="$1" tries=0
  sleep 20
  until ping -c1 -W2 "$ip" >/dev/null 2>&1; do
    tries=$((tries+1)); [[ $tries -gt 30 ]] && { fail "$ip never pinged back"; return 1; }
    sleep 8
  done
  # ping is back; now wait for SSH to actually answer (boot can lag)
  tries=0
  until [[ -n "$(ssh_cmd "$ip" hostname 2>/dev/null)" ]]; do
    tries=$((tries+1)); [[ $tries -gt 20 ]] && { fail "$ip pings but SSH never came up"; return 1; }
    sleep 6
  done
  return 0
}
