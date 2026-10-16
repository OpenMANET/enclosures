#!/bin/bash
# Simulate this node moving out of 2.4 GHz range: take its 2.4 GHz mesh
# radio down for <hold> seconds, then restore it. The node stays in the mesh
# over HaLow the whole time. The restore is trapped, so it runs on normal
# exit OR if we are killed. Usage: band-degrade.sh <hold_sec>
set -u
HOLD=${1:?usage: band-degrade.sh <hold_sec>}

mesh24=""
batmembers=$(batctl meshif bat0 interface 2>/dev/null | cut -d: -f1)
for i in /sys/class/net/wlan*; do
    [ -e "$i" ] || continue
    n=$(basename "$i")
    d=$(basename "$(readlink -f "$i/device/driver" 2>/dev/null)" 2>/dev/null)
    if [ "$d" = mt7915e ] && printf '%s\n' "$batmembers" | grep -qx "$n"; then
        mesh24=$n
        break
    fi
done
[ -n "$mesh24" ] || { echo "band-degrade: no 2.4 GHz batman member"; exit 1; }

restore() {
    ip link set "$mesh24" up 2>/dev/null
    systemctl restart "wpa_supplicant@${mesh24}.service" 2>/dev/null
}
trap restore EXIT

ip link set "$mesh24" down || exit 1
echo "down ${mesh24} for ${HOLD}s (node stays meshed via HaLow; auto-restore)"
sleep "$HOLD"
echo "restored ${mesh24} up"
