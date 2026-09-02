#!/bin/bash
# ============================================================================
# 60-set-cpu-governor.sh <ip> <expected-hostname>
#
# CPU policy: schedutil governor + 1000 MHz cap (idles to 600, clocks to 1000
# under load). Installs manet-cpu-perf.sh + manet-cpu-perf.service and DISABLES
# the stock cpu-powersave.service (which pinned 600 MHz and left a failed unit;
# it is a real unit file in /etc/systemd/system, so disable, not mask).
# Idempotent; applies immediately and persists across reboot.
# ============================================================================
set -uo pipefail
SD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; source "$SD/lib.sh"
IP="${1:?usage: $0 <ip> <hostname>}"; HOST="${2:?usage: $0 <ip> <hostname>}"
SCRIPT="$SD/node-files/manet-cpu-perf.sh"
UNIT="$SD/node-files/manet-cpu-perf.service"
[[ -f "$SCRIPT" && -f "$UNIT" ]] || { fail "missing node-files/ payload"; exit 1; }

verify_hostname "$IP" "$HOST" || exit 1

log "installing manet-cpu-perf + disabling stock cpu-powersave ..."
put_scp "$SCRIPT" "$IP" /usr/local/bin/manet-cpu-perf.sh 755
put_scp "$UNIT"   "$IP" /etc/systemd/system/manet-cpu-perf.service 644
ssh_rootp "$IP" "systemctl disable --now cpu-powersave.service >/dev/null 2>&1; systemctl reset-failed cpu-powersave.service >/dev/null 2>&1; systemctl daemon-reload; systemctl enable --now manet-cpu-perf.service >/dev/null 2>&1"

echo "  --- CPU state ---"
ssh_rootp "$IP" '
echo "  governor=$(cat /sys/devices/system/cpu/cpufreq/policy0/scaling_governor) max=$(cat /sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq|awk "{print \$1/1000}")MHz cur=$(cat /sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq|awk "{print \$1/1000}")MHz online=$(cat /sys/devices/system/cpu/online)"
echo "  cpu-powersave: $(systemctl is-enabled cpu-powersave.service 2>&1) | manet-cpu-perf: $(systemctl is-active manet-cpu-perf.service)"'
ok "CPU policy set on $HOST"
