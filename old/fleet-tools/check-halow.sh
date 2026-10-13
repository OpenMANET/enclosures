#!/bin/bash
# ============================================================================
# check-halow.sh — HaLow diagnostics across every discovered node
#
# Usage:
#   ./check-halow.sh                    # check and report
#   ./check-halow.sh --restart          # also restart wpa_supplicant-s1g on
#                                        # any node that isn't COMPLETED
#   ./check-halow.sh --restart --logs   # restart AND pull full service
#                                        # status + journal + dmesg + lsusb
#   ./check-halow.sh --usb              # just print lsusb/lsusb -t per node
#                                        # (combine with other flags freely)
# ============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib.sh"

SUBNET="${SUBNET:-10.30.2}"

DO_RESTART="no"
DO_LOGS="no"
DO_USB="no"
for arg in "$@"; do
  [[ "$arg" == "--restart" ]] && DO_RESTART="yes"
  [[ "$arg" == "--logs" ]] && DO_LOGS="yes"
  [[ "$arg" == "--usb" ]] && DO_USB="yes"
done

echo "=== Discovering nodes on ${SUBNET}.0/24 ==="
discover_fleet "$SUBNET"

if [[ ${#HOSTNAME_TO_IP[@]} -eq 0 ]]; then
  echo "No nodes discovered."
  exit 1
fi
ok "discovered ${#HOSTNAME_TO_IP[@]} node(s)"

generate_ssh_config
ok "regenerated $MESH_SSH_CONFIG"
echo

declare -A RESULTS

for name in "${!HOSTNAME_TO_IP[@]}"; do
  ip="${HOSTNAME_TO_IP[$name]}"
  echo "=== $name ($ip) ==="

  verify_hostname "$ip" "$name" || {
    RESULTS[$name]="SKIPPED (hostname mismatch)"
    echo
    continue
  }

  if [[ "$DO_USB" == "yes" ]]; then
    log "--- lsusb ---"
    ssh_cmd "$ip" "lsusb" 2>/dev/null | sed 's/^/      /'
    log "--- lsusb -t ---"
    ssh_cmd "$ip" "lsusb -t" 2>/dev/null | sed 's/^/      /'
    echo
  fi

  svc_status=$(ssh_cmd "$ip" "systemctl is-active wpa_supplicant-s1g-wlan2.service" 2>/dev/null)
  log "wpa_supplicant-s1g-wlan2.service: ${svc_status:-unknown}"

  wpa_out=$(ssh_sudo "$ip" "wpa_cli_s1g -i wlan2 status" 2>&1)
  wpa_state=$(echo "$wpa_out" | grep '^wpa_state=' | cut -d= -f2)
  ctrl_error=$(echo "$wpa_out" | grep -o 'No such file or directory')

  if [[ -n "$ctrl_error" ]]; then
    fail "wpa_cli_s1g: control socket missing"
    wpa_state="NO_SOCKET"
  elif [[ -n "$wpa_state" ]]; then
    if [[ "$wpa_state" == "COMPLETED" ]]; then
      ok "wpa_state=COMPLETED"
    else
      warn "wpa_state=$wpa_state (not COMPLETED)"
    fi
  else
    fail "wpa_cli_s1g: no response"
    wpa_state="NO_RESPONSE"
  fi

  usb_errors=$(ssh_cmd "$ip" "dmesg | grep -iE 'morse_usb.*-71|USB disconnect' | tail -5")
  if [[ -n "$usb_errors" ]]; then
    warn "recent USB errors in dmesg:"
    echo "$usb_errors" | sed 's/^/      /'
  else
    ok "no recent USB -71/disconnect errors"
  fi

  neighbor_count=$(ssh_sudo "$ip" "batctl n" 2>/dev/null | grep -c '0c:bf:74')
  neighbor_count="${neighbor_count:-0}"
  [[ "$neighbor_count" -gt 0 ]] && ok "batctl n shows $neighbor_count HaLow neighbor(s)" || warn "zero HaLow neighbors"

  RESULTS[$name]="$wpa_state"

  if [[ "$DO_RESTART" == "yes" && "$wpa_state" != "COMPLETED" ]]; then
    warn "restarting wpa_supplicant-s1g-wlan2.service..."
    ssh_sudo "$ip" "systemctl restart wpa_supplicant-s1g-wlan2.service"
    sleep 5
    new_state=$(ssh_sudo "$ip" "wpa_cli_s1g -i wlan2 status" 2>&1 | grep '^wpa_state=' | cut -d= -f2)
    if [[ "$new_state" == "COMPLETED" ]]; then
      ok "after restart: COMPLETED"
      RESULTS[$name]="COMPLETED (after restart)"
    else
      fail "after restart: ${new_state:-still no response}"
      RESULTS[$name]="${new_state:-NO_RESPONSE} (after restart)"
    fi
  fi

  if [[ "$DO_LOGS" == "yes" && "${RESULTS[$name]}" != "COMPLETED" ]]; then
    echo
    log "--- full service status ---"
    ssh_sudo "$ip" "systemctl status wpa_supplicant-s1g-wlan2.service --no-pager -l" 2>/dev/null | sed 's/^/      /'
    log "--- last 40 journal lines ---"
    ssh_sudo "$ip" "journalctl -u wpa_supplicant-s1g-wlan2.service -n 40 --no-pager" 2>/dev/null | sed 's/^/      /'
    log "--- recent morse/wlan2 dmesg ---"
    ssh_cmd "$ip" "dmesg | grep -iE 'morse|wlan2' | tail -40" 2>/dev/null | sed 's/^/      /'
    log "--- lsusb -t ---"
    ssh_cmd "$ip" "lsusb -t" 2>/dev/null | sed 's/^/      /'
  fi

  echo
done

echo "=== HaLow status summary ==="
for name in "${!RESULTS[@]}"; do
  printf "  %-14s %s\n" "$name" "${RESULTS[$name]}"
done
echo
[[ "$DO_RESTART" == "no" ]] && echo "Run with --restart to auto-restart non-COMPLETED nodes (add --logs for full diagnostics)."
