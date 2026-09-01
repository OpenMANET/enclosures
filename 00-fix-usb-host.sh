#!/bin/bash
# ============================================================================
# 00-fix-usb-host.sh <ip> <expected-hostname>
#
# Fixes the provisioning bug where the CM4's USB host controller is never
# enabled, so nothing on USB enumerates (Morse HaLow radio + OpenVLM audio
# are both invisible). The image ships the USB-enable directive under the
# [cm5] config.txt section while these are CM4 modules, AND leaves the CM4's
# own `otg_mode=1` commented out. We uncomment otg_mode=1 under [cm4] and
# reboot.
#
# Idempotent. Reboots the node, so hostname is verified first and after.
# NOTE: once host mode is on, that USB-C port can no longer flash an image
# until the line is reverted.
# ============================================================================
set -uo pipefail
SD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; source "$SD/lib.sh"
IP="${1:?usage: $0 <ip> <hostname>}"; HOST="${2:?usage: $0 <ip> <hostname>}"

verify_hostname "$IP" "$HOST" || exit 1

if [[ "$(ssh_cmd "$IP" 'grep -c "^otg_mode=1" /boot/firmware/config.txt')" == "1" ]]; then
  ok "otg_mode=1 already set — USB host already enabled"
  if ssh_rootp "$IP" "lsusb | grep -q 325b:8100"; then ok "HaLow already enumerates"; fi
  exit 0
fi

log "backing up config.txt and enabling otg_mode=1 ..."
ssh_root "$IP" "test -f /boot/firmware/config.txt.pre-usbfix || cp /boot/firmware/config.txt /boot/firmware/config.txt.pre-usbfix; sed -i s/^#otg_mode=1/otg_mode=1/ /boot/firmware/config.txt"
[[ "$(ssh_cmd "$IP" 'grep -c "^otg_mode=1" /boot/firmware/config.txt')" == "1" ]] || { fail "edit did not take"; exit 1; }

log "rebooting $HOST to apply ..."
ssh_root "$IP" "reboot" >/dev/null 2>&1 || true
wait_online "$IP" || exit 1
verify_hostname "$IP" "$HOST" || exit 1

if ssh_rootp "$IP" "lsusb | grep -q 325b:8100"; then ok "Morse HaLow now enumerates"; else warn "HaLow still missing — check hub/cable"; fi
if ssh_rootp "$IP" "lsusb | grep -q 0d8c:0012"; then ok "OpenVLM now enumerates";   else warn "OpenVLM still missing — check hub/cable"; fi
