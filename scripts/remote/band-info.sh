#!/bin/bash
# Read-only band info for the swapper: which iface is HaLow vs 2.4 GHz,
# their MACs, and each hardif's batman hop_penalty. The 2.4 GHz mesh radio
# is the mt7915e iface that is a member of bat0 (the 5 GHz AP is also
# mt7915e but is not a batman member). Prints key=value lines.
set -u
halow="" mesh24=""
batmembers=$(batctl meshif bat0 interface 2>/dev/null | cut -d: -f1)
inbat() { printf '%s\n' "$batmembers" | grep -qx "$1"; }
for i in /sys/class/net/wlan*; do
    [ -e "$i" ] || continue
    n=$(basename "$i")
    d=$(basename "$(readlink -f "$i/device/driver" 2>/dev/null)" 2>/dev/null)
    case $d in
    morse_usb) [ -z "$halow" ] && inbat "$n" && halow=$n ;;
    mt7915e)   [ -z "$mesh24" ] && inbat "$n" && mesh24=$n ;;
    esac
done
if [ -z "$halow" ] || [ -z "$mesh24" ]; then
    echo "error=no HaLow or 2.4 GHz batman member found"
    exit 1
fi
pen() { batctl hardif "$1" hop_penalty 2>/dev/null | grep -oE '[0-9]+' | head -1; }
echo "halow_if=$halow"
echo "halow_mac=$(cat "/sys/class/net/$halow/address" 2>/dev/null)"
echo "mesh24_if=$mesh24"
echo "pen_halow=$(pen "$halow")"
echo "pen_mesh24=$(pen "$mesh24")"
echo "pen_file=$(tr -dc '0-9' < /etc/manet-band-halow-hop-penalty 2>/dev/null | head -c 3)"
