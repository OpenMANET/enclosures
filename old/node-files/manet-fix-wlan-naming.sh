#!/bin/bash
# ============================================================================
# manet-fix-wlan-naming.sh — deterministic wlan naming at boot (lab).
#
# The MAC-pinned /etc/systemd/network/10-wlan*.link files cannot reliably
# resolve a 2-cycle rename (e.g. the USB morse HaLow and the onboard brcmfmac
# swapping wlan2<->wlan3) when the USB radio enumerates late: udev renames in
# arrival order and can't atomically swap two occupied names. The result is a
# random boot where the s1g supplicant/hostapd bind the wrong radio.
#
# This script is the boot-time counterpart to fleet-tools/recover-wlan-swap.sh:
# it reads the same .link MAC->name mapping and forces the actual interfaces to
# match, using temp names so any permutation (including a full swap) resolves.
# It is ordered BEFORE the wireless services by manet-wlan-naming.service.
# ============================================================================
set -u
LINK_DIR=/etc/systemd/network
log() { logger -t manet-wlan-naming "$*"; echo "manet-wlan-naming: $*"; }

declare -A WANT
for f in "$LINK_DIR"/10-wlan*.link; do
    [ -e "$f" ] || continue
    grep -qi '^Type=wlan' "$f" || continue
    mac=$(grep -i '^MACAddress=' "$f" | cut -d= -f2 | tr 'A-Z' 'a-z' | tr -d '[:space:]')
    name=$(grep -i '^Name=' "$f" | cut -d= -f2 | tr -d '[:space:]')
    [ -n "$mac" ] && [ -n "$name" ] && WANT["$mac"]="$name"
done
if [ ${#WANT[@]} -eq 0 ]; then
    log "no wlan .link pins found; nothing to do"; exit 0
fi

mac_of() { tr 'A-Z' 'a-z' < "/sys/class/net/$1/address" 2>/dev/null; }
iface_for_mac() {
    local want="$1" d n
    for d in /sys/class/net/wlan* /sys/class/net/tmp-wlan*; do
        [ -e "$d" ] || continue
        n=$(basename "$d")
        [ "$(mac_of "$n")" = "$want" ] && { echo "$n"; return; }
    done
}

# Wait until every expected radio has enumerated (USB morse is the slow one).
for _ in $(seq 1 40); do
    missing=0
    for mac in "${!WANT[@]}"; do
        [ -z "$(iface_for_mac "$mac")" ] && missing=1
    done
    [ "$missing" -eq 0 ] && break
    sleep 1
done
[ "$missing" -ne 0 ] && log "WARNING: not all pinned radios present after 40s; reconciling what is here"

# Phase 1: park every mismatched interface under a temp name so its target
# name (which may be held by another mismatched interface) becomes free.
for mac in "${!WANT[@]}"; do
    cur=$(iface_for_mac "$mac"); want="${WANT[$mac]}"
    [ -z "$cur" ] && continue
    case "$cur" in tmp-*) continue;; esac
    if [ "$cur" != "$want" ]; then
        ip link set "$cur" down 2>/dev/null
        if ip link set "$cur" name "tmp-$want" 2>/dev/null; then
            log "parked $mac ($cur) -> tmp-$want"
        else
            log "ERROR: could not park $cur -> tmp-$want"
        fi
    fi
done

# Phase 2: move parked interfaces to their final names.
for mac in "${!WANT[@]}"; do
    want="${WANT[$mac]}"
    if [ -e "/sys/class/net/tmp-$want" ]; then
        if ip link set "tmp-$want" name "$want" 2>/dev/null; then
            log "renamed tmp-$want -> $want ($mac)"
        else
            log "ERROR: could not rename tmp-$want -> $want"
        fi
    fi
done

# Report final mapping.
for mac in "${!WANT[@]}"; do
    cur=$(iface_for_mac "$mac")
    [ "$cur" = "${WANT[$mac]}" ] || log "STILL WRONG: $mac on '${cur:-none}', wanted ${WANT[$mac]}"
done
log "done"
exit 0
