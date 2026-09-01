#!/bin/bash
# ============================================================================
# recover-wlan-swap.sh — on-demand fix for a live wlan interface swap
#
# Wraps the full stop -> rename -> restart -> verify sequence we've done
# manually multiple times this session into one command. Works over the
# mesh (hostname alias) OR a direct IP (LAN cable, no mesh/router needed —
# a point-to-point Ethernet cable + a manually-set IP on both ends works
# fine, you don't need a full LAN/DHCP setup for this).
#
# Self-contained: pushes its own copy of the reconciliation logic rather
# than depending on fix-wlan-swap.sh already being installed on the
# target — this is exactly for the case where the boot-time fix is
# missing/wasn't deployed yet, same situation that prompted writing this.
#
# Usage:
#   ./recover-wlan-swap.sh <ip-or-hostname-alias> <expected-hostname>
# ============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib.sh"

TARGET="${1:-}"
EXPECTED_HOSTNAME="${2:-}"

if [[ -z "$TARGET" || -z "$EXPECTED_HOSTNAME" ]]; then
  echo "Usage: $0 <ip-or-hostname-alias> <expected-hostname>"
  echo "Works over the mesh alias (e.g. mesh-5ab8) or a direct IP"
  echo "(e.g. a LAN/Ethernet cable IP if the mesh side is down)."
  exit 1
fi

echo "=== $EXPECTED_HOSTNAME ($TARGET) ==="
verify_hostname "$TARGET" "$EXPECTED_HOSTNAME" || exit 1

log "checking which interface this SSH session is actually using..."
active_iface=$(ssh_cmd "$TARGET" "ss -tn state established '( dport = :22 or sport = :22 )' 2>/dev/null | tail -n +2 | head -1 | awk '{print \$4}' | cut -d: -f1")
active_iface_name=""
if [[ -n "$active_iface" ]]; then
  active_iface_name=$(ssh_cmd "$TARGET" "ip -o route get $active_iface 2>/dev/null | grep -oP 'dev \K\S+'")
fi
if [[ -n "$active_iface_name" ]]; then
  warn "this session appears to be routed via $active_iface_name — will handle it last and reconnect if it drops mid-fix"
else
  log "could not determine active session interface — proceeding, but a connection drop mid-fix is possible if the swap involves your current path"
fi

log "reading this node's expected MAC->name mapping from its own .link files..."
mapping=$(ssh_cmd "$TARGET" "cat /etc/systemd/network/*.link 2>/dev/null")
if [[ -z "$mapping" ]]; then
  fail "could not read .link files — aborting, nothing changed"
  exit 1
fi

log "stopping wireless-dependent services..."
ssh_sudo "$TARGET" "systemctl stop hostapd.service wpa_supplicant@wlan0.service wpa_supplicant-s1g-wlan2.service batman-enslave.service" >/dev/null 2>&1

log "phase 1: reconciling everything except this session's own interface..."
phase1_output=$({ printf '%s\n' "$SUDO_PASS"; } | ssh $SSH_OPTS "$SSH_USER@$TARGET" "sudo -S bash -s" -- "$active_iface_name" <<'REMOTE_SCRIPT' 2>&1
ACTIVE_IFACE="$1"
LINK_DIR="/etc/systemd/network"
declare -A EXPECTED=()
for f in "$LINK_DIR"/*.link; do
  [[ -e "$f" ]] || continue
  grep -qi '^Type=wlan' "$f" || continue
  mac=$(grep -i '^MACAddress=' "$f" | cut -d= -f2 | tr 'A-Z' 'a-z')
  name=$(grep -i '^Name=' "$f" | cut -d= -f2)
  [[ -n "$mac" && -n "$name" ]] && EXPECTED["$mac"]="$name"
done
current_iface_for_mac() {
  ip -o link show | awk -v m="$1" 'tolower($0) ~ m {print $2}' | tr -d ':' | head -n1
}

# Phase 1: fix every mismatch EXCEPT one currently sitting on the
# session's own active interface — that one is deliberately left for
# phase 2, since renaming it will disconnect this very SSH session.
ACTIVE_PENDING_MAC=""
for mac in "${!EXPECTED[@]}"; do
  want="${EXPECTED[$mac]}"
  cur=$(current_iface_for_mac "$mac")
  if [[ -n "$cur" && "$cur" != "$want" && "$cur" != tmp-* ]]; then
    if [[ -n "$ACTIVE_IFACE" && "$cur" == "$ACTIVE_IFACE" ]]; then
      ACTIVE_PENDING_MAC="$mac"
      echo "deferring $mac (on $cur, needs $want) — this is the session's own interface"
      continue
    fi
    echo "MAC $mac is on $cur, needs to be $want"
    ip link set "$cur" down
    ip link set "$cur" name "tmp-${cur}"
  fi
done
for mac in "${!EXPECTED[@]}"; do
  [[ "$mac" == "$ACTIVE_PENDING_MAC" ]] && continue
  want="${EXPECTED[$mac]}"
  cur=$(current_iface_for_mac "$mac")
  if [[ "$cur" == tmp-* ]]; then
    ip link set "$cur" name "$want"
    ip link set "$want" up
  fi
done

if [[ -n "$ACTIVE_PENDING_MAC" ]]; then
  echo "ACTIVE_IFACE_PENDING=${ACTIVE_PENDING_MAC}=${EXPECTED[$ACTIVE_PENDING_MAC]}"
else
  echo "ACTIVE_IFACE_OK"
fi
REMOTE_SCRIPT
)
echo "$phase1_output" | sed 's/^/  /'

pending_line=$(echo "$phase1_output" | grep '^ACTIVE_IFACE_PENDING=')

if [[ -n "$pending_line" ]]; then
  pending_mac_want="${pending_line#ACTIVE_IFACE_PENDING=}"
  pending_mac="${pending_mac_want%%=*}"
  pending_want="${pending_mac_want##*=}"

  echo
  warn "the remaining fix is on $active_iface_name itself — THIS SESSION WILL DISCONNECT NOW"
  log "renaming $active_iface_name -> $pending_want (MAC $pending_mac)..."

  # Fire the disruptive rename. We expect this connection to die mid-command
  # — that's normal, not a failure. A short timeout keeps us from hanging
  # on a connection we already know is about to drop.
  timeout 10 ssh $SSH_OPTS -o ConnectTimeout=5 "$SSH_USER@$TARGET" \
    "echo '$SUDO_PASS' | sudo -S bash -c '
      ip link set $active_iface_name down
      ip link set $active_iface_name name $pending_want
      ip link set $pending_want up
    '" >/dev/null 2>&1

  log "waiting for the connection to come back (up to 40s)..."
  reconnected=0
  for i in $(seq 1 8); do
    sleep 5
    if ssh_cmd "$TARGET" "hostname" >/dev/null 2>&1; then
      reconnected=1
      break
    fi
    log "  still waiting... ($((i*5))s)"
  done

  if [[ "$reconnected" -ne 1 ]]; then
    fail "connection did not come back within 40s."
    fail "the node may still be reachable via its LAN/Ethernet IP if the"
    fail "renamed interface isn't your only path — check manually before"
    fail "assuming this recovery failed outright."
    exit 1
  fi
  ok "reconnected"
else
  ok "no fix needed on this session's own interface"
fi

log "restarting services in order..."
ssh_sudo "$TARGET" "systemctl restart wpa_supplicant@wlan0.service" >/dev/null 2>&1
sleep 5
ssh_sudo "$TARGET" "systemctl restart wpa_supplicant-s1g-wlan2.service" >/dev/null 2>&1
sleep 5
ssh_sudo "$TARGET" "systemctl restart hostapd.service" >/dev/null 2>&1
sleep 3
ssh_sudo "$TARGET" "systemctl restart batman-enslave.service" >/dev/null 2>&1
sleep 5

echo
log "--- final state ---"
ssh_cmd "$TARGET" "ip -o link show | grep -E 'wlan'" | sed 's/^/  /'
svc_state=$(ssh_cmd "$TARGET" "systemctl is-active hostapd.service wpa_supplicant@wlan0.service wpa_supplicant-s1g-wlan2.service" 2>/dev/null | tr '\n' ' ')
echo "  services: $svc_state"
batif=$(ssh_sudo "$TARGET" "batctl if" 2>/dev/null)
echo "  batctl if:"
echo "$batif" | sed 's/^/    /'

if echo "$svc_state" | grep -qv "failed\|inactive"; then
  ok "recovery complete — verify manually that this matches your other healthy nodes"
else
  fail "one or more services still not active — may need another pass or deeper investigation"
fi
