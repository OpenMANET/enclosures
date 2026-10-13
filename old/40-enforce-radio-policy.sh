#!/bin/bash
# ============================================================================
# 40-enforce-radio-policy.sh <ip> <expected-hostname>
#
# Enforces the lab radio-role policy on a node (idempotent):
#   MESH = USB MM8108 HaLow + MT7916 2.4 GHz;  5 GHz EUD AP = MT7916 5 GHz;
#   any other Wi-Fi (onboard CM4 brcmfmac) is blacklisted and unused.
#
# Pushes node-files/manet-enforce-radio-policy.sh and runs it once (the config
# it writes is persistent). Safe on already-compliant nodes (no-op). Does NOT
# reboot, but the onboard-Wi-Fi blacklist only fully applies after the next
# reboot (the interface is brought down immediately in the meantime).
# ============================================================================
set -uo pipefail
SD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; source "$SD/lib.sh"
IP="${1:?usage: $0 <ip> <hostname>}"; HOST="${2:?usage: $0 <ip> <hostname>}"
SCRIPT="$SD/node-files/manet-enforce-radio-policy.sh"
[[ -f "$SCRIPT" ]] || { fail "missing $SCRIPT"; exit 1; }

verify_hostname "$IP" "$HOST" || exit 1

log "pushing + running radio-policy enforcement ..."
put_scp "$SCRIPT" "$IP" /usr/local/bin/manet-enforce-radio-policy.sh 755
ssh_rootp "$IP" "/usr/local/bin/manet-enforce-radio-policy.sh"

echo "  --- resulting roles ---"
ssh_rootp "$IP" '
ap=$(cat /var/lib/ap_interface 2>/dev/null)
echo "  AP iface=$ap hostapd=$(systemctl is-active hostapd) $(iw dev $ap info 2>/dev/null|awk "/channel/{print \$2,\$3}")"
echo "  bat0: $(batctl if 2>/dev/null|awk -F: "/active/{print \$1}"|tr "\n" " ")"
echo "  onboard blacklist: $(test -f /etc/modprobe.d/manet-blacklist-onboard-wifi.conf && echo present || echo none)"'
ok "policy enforced on $HOST"
