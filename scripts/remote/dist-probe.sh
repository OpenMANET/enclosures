#!/bin/bash
# Read-only probe for the distance checker: this node's 2.4 GHz identity,
# TX power, and the RSSI it measures from each 2.4 GHz mesh peer.
# Prints key=value lines: MESH24_IF, MESH24_MAC, TXDBM, then PEER/RSSI pairs.
set -u
batmembers=$(batctl meshif bat0 interface 2>/dev/null | cut -d: -f1)
mesh24=""
for i in /sys/class/net/wlan*; do
    [ -e "$i" ] || continue
    n=$(basename "$i")
    d=$(basename "$(readlink -f "$i/device/driver" 2>/dev/null)" 2>/dev/null)
    if [ "$d" = mt7915e ] && printf '%s\n' "$batmembers" | grep -qx "$n"; then
        mesh24=$n
        break
    fi
done
[ -n "$mesh24" ] || { echo "error=no 2.4 GHz batman member"; exit 1; }

echo "MESH24_IF=$mesh24"
echo "HOST=$(hostname)"
echo "MESH24_MAC=$(cat "/sys/class/net/$mesh24/address" 2>/dev/null)"
echo "TXDBM=$(iw dev "$mesh24" info 2>/dev/null | grep -oE 'txpower [0-9.]+' | awk '{print $2}')"

# Each station block: "Station <mac> ..." then a "signal:" line (skip ack lines).
iw dev "$mesh24" station dump 2>/dev/null | awk '
    /^Station / { mac = $2 }
    /signal:/ && $0 !~ /ack/ {
        if (mac != "") { printf "PEER=%s RSSI=%s\n", mac, $2; mac = "" }
    }'
