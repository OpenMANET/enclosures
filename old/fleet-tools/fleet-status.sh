#!/bin/bash
# ============================================================================
# fleet-status.sh — full health check: services, mesh, radios, voice
#
# Usage:
#   ./fleet-status.sh                # check and report everything
#   ./fleet-status.sh --start-voice  # also start/enable the voice service
#                                     # on any node where it's inactive
#
# Run from your laptop or from one node (e.g. via the 5GHz AP + SSH) —
# never shifts targets, everything happens over SSH from wherever you run
# this. Reads credentials from .credentials, uses the fleet SSH key.
# ============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib.sh"

SUBNET="${SUBNET:-10.30.2}"

DO_START_VOICE="no"
[[ "${1:-}" == "--start-voice" ]] && DO_START_VOICE="yes"

echo "=== Discovering nodes on ${SUBNET}.0/24 ==="
discover_fleet "$SUBNET"

if [[ ${#HOSTNAME_TO_IP[@]} -eq 0 ]]; then
  echo "No nodes discovered. If this is a freshly-flashed fleet, run"
  echo "./bootstrap-ssh-key.sh first to distribute the SSH key."
  exit 1
fi

ok "discovered ${#HOSTNAME_TO_IP[@]} node(s)"
generate_ssh_config
ok "regenerated $MESH_SSH_CONFIG"
echo

declare -A SUMMARY

for name in "${!HOSTNAME_TO_IP[@]}"; do
  ip="${HOSTNAME_TO_IP[$name]}"
  echo "════════════════════════════════════════"
  echo "  $name ($ip)"
  echo "════════════════════════════════════════"

  verify_hostname "$ip" "$name" || { SUMMARY[$name]="SKIPPED (hostname mismatch)"; echo; continue; }

  # ---- Radios present on the USB/PCIe bus --------------------------------
  echo
  log "--- Radios on bus ---"
  lsusb_out=$(ssh_cmd "$ip" "lsusb" 2>/dev/null)
  lspci_out=$(ssh_cmd "$ip" "lspci 2>/dev/null")

  echo "$lsusb_out" | grep -qi "325b:8100" && ok "HaLow (Morse, USB) present" || warn "HaLow (Morse, USB) NOT found on USB bus"
  echo "$lspci_out" | grep -qi "MT7916\|Filogic" && ok "MT7916 (2.4/5GHz) present on PCIe" || warn "MT7916 NOT found on PCIe bus (check M.2 seating / dwc2 overlay if USB-attached)"
  echo "$lsusb_out" | grep -qi "0d8c:0012" && ok "OpenVLM present" || warn "OpenVLM NOT found on USB bus"

  # ---- Interfaces, identified by driver rather than assumed name --------
  echo
  log "--- Wireless interfaces (by driver) ---"
  for iface in $(ssh_cmd "$ip" "ls /sys/class/net 2>/dev/null | grep -E '^wlan'"); do
    driver=$(ssh_cmd "$ip" "basename \$(readlink -f /sys/class/net/$iface/device/driver 2>/dev/null) 2>/dev/null")
    master=$(ssh_cmd "$ip" "cat /sys/class/net/$iface/master/uevent 2>/dev/null | grep INTERFACE | cut -d= -f2")
    state=$(ssh_cmd "$ip" "cat /sys/class/net/$iface/operstate 2>/dev/null")
    printf "      %-8s driver=%-12s master=%-6s state=%s\n" "$iface" "${driver:-none}" "${master:-none}" "${state:-?}"
  done

  # ---- Services -----------------------------------------------------------
  echo
  log "--- Key services ---"
  for svc in batman-enslave.service batman-enslave-watch.service \
             "wpa_supplicant@wlan0.service" "wpa_supplicant-s1g-wlan2.service" \
             hostapd.service; do
    st=$(ssh_cmd "$ip" "systemctl is-active $svc 2>/dev/null")
    [[ "$st" == "active" ]] && ok "$svc: active" || warn "$svc: ${st:-not found}"
  done

  if ssh_cmd "$ip" "systemctl list-unit-files mesh-voice.service --no-legend 2>/dev/null" | grep -q mesh-voice; then
    voice_svc="mesh-voice.service"
  else
    voice_svc=$(ssh_cmd "$ip" "systemctl list-unit-files --type=service --no-legend 2>/dev/null | grep -i voice | awk '{print \$1}'" | head -1)
  fi
  if [[ -n "$voice_svc" ]]; then
    voice_st=$(ssh_cmd "$ip" "systemctl is-active $voice_svc" 2>/dev/null)
    [[ "$voice_st" == "active" ]] && ok "voice service ($voice_svc): active" || warn "voice service ($voice_svc): $voice_st"
    if [[ "$DO_START_VOICE" == "yes" && "$voice_st" != "active" ]]; then
      warn "starting $voice_svc..."
      ssh_sudo "$ip" "systemctl enable --now $voice_svc" >/dev/null 2>&1
      sleep 3
      voice_st=$(ssh_cmd "$ip" "systemctl is-active $voice_svc" 2>/dev/null)
      [[ "$voice_st" == "active" ]] && ok "now active" || fail "still $voice_st — check logs manually"
    fi
  else
    warn "no voice-related service found on this node"
  fi

  # ---- Mesh health --------------------------------------------------------
  echo
  log "--- Mesh (batman-adv) ---"
  batif=$(ssh_sudo "$ip" "batctl if" 2>/dev/null)
  echo "$batif" | sed 's/^/      /'
  neighbor_count=$(ssh_sudo "$ip" "batctl n" 2>/dev/null | tail -n +2 | grep -c .)
  neighbor_count="${neighbor_count:-0}"
  [[ "$neighbor_count" -gt 0 ]] && ok "$neighbor_count mesh neighbor(s)" || warn "zero mesh neighbors"

  halow_state=$(ssh_sudo "$ip" "wpa_cli_s1g -i wlan2 status" 2>&1 | grep '^wpa_state=' | cut -d= -f2)
  [[ "$halow_state" == "COMPLETED" ]] && ok "HaLow wpa_state=COMPLETED" || warn "HaLow wpa_state=${halow_state:-no response}"

  SUMMARY[$name]="checked"
  echo
done

echo "════════════════════════════════════════"
echo "  Summary"
echo "════════════════════════════════════════"
for name in "${!SUMMARY[@]}"; do
  printf "  %-14s %s\n" "$name" "${SUMMARY[$name]}"
done
echo
echo "Radio/service detail is above per node — scroll up for specifics."
echo "This script can't press physical PTT buttons — once every node"
echo "shows HaLow COMPLETED, MT7916 present, OpenVLM present, and the"
echo "voice service active, do the actual PTT/audio test by hand between"
echo "two nodes to confirm end-to-end."
