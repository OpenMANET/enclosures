#!/bin/bash
# ============================================================================
# manet-cpu-perf.sh — CPU governor policy for the fleet.
#
# Replaces the stock cpu-powersave.service (which pinned 600 MHz, capped max to
# ~1000 MHz, and tried to offline cores 2&3 — failing, leaving a failed unit).
#
# Policy: governor = schedutil, max freq capped at 1000 MHz, all cores online.
# schedutil idles down to 600 MHz when quiet and clocks up to 1000 MHz under
# load (e.g. voice DSP) — better real-time headroom than a 600 MHz pin, without
# the power/heat of the full 1500 MHz. On the CM4 all 4 cores share one cpufreq
# policy (policy0), so setting each policy covers every core.
# ============================================================================
set -u
log() { logger -t manet-cpu-perf "$*"; echo "manet-cpu-perf: $*"; }

CAP_KHZ=1000000
GOV=schedutil

for pol in /sys/devices/system/cpu/cpufreq/policy*; do
    [ -e "$pol" ] || continue
    # fall back to ondemand if schedutil is somehow unavailable
    if grep -qw "$GOV" "$pol/scaling_available_governors" 2>/dev/null; then
        echo "$GOV" > "$pol/scaling_governor" 2>/dev/null
    else
        echo ondemand > "$pol/scaling_governor" 2>/dev/null
    fi
    echo "$CAP_KHZ" > "$pol/scaling_max_freq" 2>/dev/null
done

# undo any core offlining (belt & braces — the stock service tries cpu2/3)
for c in /sys/devices/system/cpu/cpu[0-9]*/online; do
    [ -e "$c" ] || continue
    echo 1 > "$c" 2>/dev/null
done

g=$(cat /sys/devices/system/cpu/cpufreq/policy0/scaling_governor 2>/dev/null)
m=$(cat /sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq 2>/dev/null | awk '{print $1/1000}')
log "governor=$g max=${m}MHz online=$(cat /sys/devices/system/cpu/online 2>/dev/null)"
exit 0
