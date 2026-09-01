#!/bin/bash
# ============================================================================
# 30-fix-wlan-naming.sh <ip> <expected-hostname>
#
# Installs the boot-time deterministic wlan-naming fix on a node: the
# manet-fix-wlan-naming.sh script + manet-wlan-naming.service unit, ordered
# before the wireless services. Closes the interface-name swap race for good
# (morse<->brcmfmac wlan2<->wlan3) so no reboot needs the manual
# fleet-tools/recover-wlan-swap.sh anymore.
#
# Idempotent. Does NOT reboot. After install it runs the script once (a no-op
# if naming is already correct) so you can see the result immediately.
# ============================================================================
set -uo pipefail
SD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; source "$SD/lib.sh"
IP="${1:?usage: $0 <ip> <hostname>}"; HOST="${2:?usage: $0 <ip> <hostname>}"
SCRIPT="$SD/node-files/manet-fix-wlan-naming.sh"
UNIT="$SD/node-files/manet-wlan-naming.service"
[[ -f "$SCRIPT" && -f "$UNIT" ]] || { fail "missing node-files/ payload"; exit 1; }

verify_hostname "$IP" "$HOST" || exit 1

log "installing manet-fix-wlan-naming.sh + unit ..."
put_scp "$SCRIPT" "$IP" /usr/local/bin/manet-fix-wlan-naming.sh 755
put_scp "$UNIT"   "$IP" /etc/systemd/system/manet-wlan-naming.service 644

log "enabling service (runs before wireless every boot) ..."
ssh_rootp "$IP" "systemctl daemon-reload; systemctl enable manet-wlan-naming.service >/dev/null 2>&1"

log "running once now (no-op if naming already correct) ..."
ssh_rootp "$IP" "/usr/local/bin/manet-fix-wlan-naming.sh"

echo "  --- current wlan mapping ---"
ssh_rootp "$IP" 'for i in /sys/class/net/wlan*; do n=$(basename $i); d=$(basename $(readlink -f $i/device/driver) 2>/dev/null); echo "    $n -> $d ($(cat $i/address))"; done'
ok "installed on $HOST"
