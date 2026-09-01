#!/bin/bash
# ============================================================================
# setup-node.sh <ip> <expected-hostname>
#
# Full lab bring-up of a freshly-flashed node, in order:
#   00-fix-usb-host.sh        enable CM4 USB host (HaLow + OpenVLM enumerate); reboots
#   10-deploy-halow-1mhz.sh   868 HaLow mesh, 1 MHz, max power, duty/MPSW off
#   20-open-24-5ghz.sh        open 2.4/5 GHz (reg 00), 5 GHz AP @ max power
#   30-fix-wlan-naming.sh     boot-time deterministic wlan naming (swap-proof)
#   40-enforce-radio-policy.sh  AP on MT7916 5 GHz; blacklist onboard CM4 Wi-Fi
#
# Each sub-script is idempotent and safe to re-run. After this, run
# ./status.sh <ip> to confirm, and give HaLow ~1-2 min + possibly a
# coordinated (NOT one-off) reboot to peer — see README "HaLow peering".
# ============================================================================
set -uo pipefail
SD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; source "$SD/lib.sh"
IP="${1:?usage: $0 <ip> <hostname>}"; HOST="${2:?usage: $0 <ip> <hostname>}"

echo "### [1/5] USB host mode"          ; "$SD/00-fix-usb-host.sh"          "$IP" "$HOST" || exit 1
echo "### [2/5] HaLow 868 / 1 MHz"      ; "$SD/10-deploy-halow-1mhz.sh"     "$IP" "$HOST" || exit 1
echo "### [3/5] open 2.4/5 GHz"         ; "$SD/20-open-24-5ghz.sh"          "$IP" "$HOST" || exit 1
echo "### [4/5] boot-time wlan naming" ; "$SD/30-fix-wlan-naming.sh"       "$IP" "$HOST" || exit 1
echo "### [5/5] radio-role policy"     ; "$SD/40-enforce-radio-policy.sh" "$IP" "$HOST" || exit 1
echo "### done — run: ./status.sh $IP"
