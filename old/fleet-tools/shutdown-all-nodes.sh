#!/bin/bash
# ============================================================================
# shutdown-all-nodes.sh — shut down every discovered node
#
# More destructive than shutdown-node.sh (whole fleet at once, no undo
# without physical access to each node), so this uses a stronger
# confirmation than a single hostname: you must type the literal phrase
# "SHUTDOWN ALL" after seeing the exact list of nodes about to go down.
#
# Each node still gets its own live hostname re-verification immediately
# before its shutdown command fires, same defense-in-depth as every other
# script here — a node whose IP drifted between listing and confirming
# gets skipped, not shut down blind.
#
# Usage:
#   ./shutdown-all-nodes.sh
# ============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib.sh"

SUBNET="${SUBNET:-10.30.2}"
LOG_FILE="$SCRIPT_DIR/shutdown-actions.log"

log_action() {
  echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') | $*" >> "$LOG_FILE"
}

echo "=== Discovering nodes on ${SUBNET}.0/24 ==="
discover_fleet "$SUBNET"

if [[ ${#HOSTNAME_TO_IP[@]} -eq 0 ]]; then
  echo "No nodes discovered. Nothing to shut down."
  exit 0
fi

mapfile -t names < <(printf '%s\n' "${!HOSTNAME_TO_IP[@]}" | sort)

echo
echo "The following ${#names[@]} node(s) will be shut down:"
for n in "${names[@]}"; do
  printf "  - %-14s %s\n" "$n" "${HOSTNAME_TO_IP[$n]}"
done
echo

read -r -p "Type SHUTDOWN ALL exactly to confirm, or press Enter to cancel: " confirm

if [[ "$confirm" != "SHUTDOWN ALL" ]]; then
  warn "cancelled — nothing was shut down"
  log_action "CANCELLED shutdown-all typed='$confirm' nodes=${names[*]}"
  exit 0
fi

echo
SUCCESS=()
FAIL=()

for name in "${names[@]}"; do
  ip="${HOSTNAME_TO_IP[$name]}"
  echo "=== $name ($ip) ==="

  verify_hostname "$ip" "$name" || {
    fail "skipping — hostname no longer matches, IP may have drifted"
    log_action "SKIPPED (mismatch) name=$name ip=$ip"
    FAIL+=("$name (hostname mismatch)")
    echo
    continue
  }

  ssh_sudo "$ip" "shutdown -h now" >/dev/null 2>&1
  ok "shutdown command sent"
  log_action "SHUTDOWN name=$name ip=$ip (bulk)"
  SUCCESS+=("$name ($ip)")
  echo
done

echo "=== Summary ==="
echo "Shut down (${#SUCCESS[@]}):"
for n in "${SUCCESS[@]}"; do echo "  ✅ $n"; done
if [[ ${#FAIL[@]} -gt 0 ]]; then
  echo "Skipped (${#FAIL[@]}):"
  for n in "${FAIL[@]}"; do echo "  ❌ $n"; done
fi
