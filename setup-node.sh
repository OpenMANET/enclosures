#!/bin/bash
# ============================================================================
# setup-node.sh [--join] <ip> [expected-hostname]
#
# Full lab bring-up of a freshly-flashed node, in order:
#   00-fix-usb-host.sh        enable CM4 USB host (HaLow + OpenVLM enumerate); reboots
#   10-deploy-halow-1mhz.sh   868 HaLow mesh, 1 MHz, max power, duty/MPSW off
#   20-open-24-5ghz.sh        open 2.4/5 GHz (reg 00), 5 GHz AP @ max power
#   30-fix-wlan-naming.sh     boot-time deterministic wlan naming (swap-proof)
#   40-enforce-radio-policy.sh  AP on MT7916 5 GHz; blacklist onboard CM4 Wi-Fi
#   50-deploy-voice-features.sh PTT keying pop + triple-tap free-speak latch + audio
#
# Credentials come from .credentials (SSH_USER/SUDO_PASS/SSH_KEY) — you do not
# pass them. Each sub-script is idempotent and safe to re-run.
#
#   <ip>                required. The node's current (LAN) IP.
#   [expected-hostname] optional. If omitted it is auto-detected over SSH and
#                       echoed — pass it explicitly to hard-guard against acting
#                       on the wrong box (IPs drift).
#   --join              first write the fleet mesh identity (MESH_SSID / MESH_KEY
#                       from .credentials + halow_regulatory_domain=EU,
#                       regulatory_domain=00) into the node's /etc/mesh.conf, so a
#                       generically-flashed node joins THIS mesh instead of
#                       forming its own. Omit if the image is already provisioned
#                       for this fleet.
#
# Examples:
#   ./setup-node.sh 10.24.17.21                 # auto-detect hostname
#   ./setup-node.sh 10.24.17.21 mesh-1a2b       # guarded
#   ./setup-node.sh --join 10.24.17.21          # provision mesh identity + set up
# ============================================================================
set -uo pipefail
SD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; source "$SD/lib.sh"

JOIN=0
if [[ "${1:-}" == "--join" ]]; then JOIN=1; shift; fi
IP="${1:?usage: $0 [--join] <ip> [expected-hostname]}"
HOST="${2:-}"

# Auto-detect the hostname when not given (fresh node whose name you don't know).
if [[ -z "$HOST" ]]; then
  HOST="$(ssh_cmd "$IP" hostname 2>/dev/null | tr -d '\r')"
  [[ -n "$HOST" ]] || { fail "cannot reach $IP over SSH to auto-detect hostname (key/creds/authorized?)"; exit 1; }
  ok "auto-detected hostname '$HOST' at $IP"
fi

# --join: provision the fleet mesh identity BEFORE 10-deploy (which reads
# mesh_ssid/mesh_key from the node) so the node joins this mesh, not its own.
if [[ "$JOIN" == "1" ]]; then
  : "${MESH_SSID:?--join needs MESH_SSID in .credentials}"
  : "${MESH_KEY:?--join needs MESH_KEY in .credentials}"
  verify_hostname "$IP" "$HOST" || exit 1
  log "provisioning fleet mesh identity (ssid=$MESH_SSID, halow=EU, reg=00) into /etc/mesh.conf ..."
  for kv in "mesh_ssid=$MESH_SSID" "mesh_key=$MESH_KEY" "halow_regulatory_domain=EU" "regulatory_domain=00"; do
    k="${kv%%=*}"
    ssh_root "$IP" "grep -q \"^$k=\" /etc/mesh.conf && sed -i \"s|^$k=.*|$kv|\" /etc/mesh.conf || echo \"$kv\" >> /etc/mesh.conf"
  done
  ok "mesh identity set on $HOST"
fi

echo "### [1/6] USB host mode"          ; "$SD/00-fix-usb-host.sh"          "$IP" "$HOST" || exit 1
echo "### [2/6] HaLow 868 / 1 MHz"      ; "$SD/10-deploy-halow-1mhz.sh"     "$IP" "$HOST" || exit 1
echo "### [3/6] open 2.4/5 GHz"         ; "$SD/20-open-24-5ghz.sh"          "$IP" "$HOST" || exit 1
echo "### [4/6] boot-time wlan naming"  ; "$SD/30-fix-wlan-naming.sh"       "$IP" "$HOST" || exit 1
echo "### [5/6] radio-role policy"      ; "$SD/40-enforce-radio-policy.sh"  "$IP" "$HOST" || exit 1
echo "### [6/6] voice features"         ; "$SD/50-deploy-voice-features.sh" "$IP" "$HOST" || exit 1
echo "### done — run: ./status.sh $IP"
