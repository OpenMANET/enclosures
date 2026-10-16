#!/bin/bash
# Set the band-priority mode by the HaLow hardif's batman hop_penalty.
#   penalty 0 = link-quality (switch the moment 2.4 GHz measures worse)
#   penalty N = sticky (hold 2.4 GHz until HaLow is clearly better)
# The 2.4 GHz hardif stays at 0. Runtime only; a reboot resets to 0.
# usage: band-set.sh <halow_penalty>
set -u
PEN=${1:?usage: band-set.sh <halow_penalty>}
case $PEN in (*[!0-9]*|'') echo "band-set: penalty must be a non-negative integer"; exit 2 ;; esac

halow="" mesh24=""
for i in /sys/class/net/wlan*; do
    [ -e "$i" ] || continue
    n=$(basename "$i")
    d=$(basename "$(readlink -f "$i/device/driver" 2>/dev/null)" 2>/dev/null)
    case $d in
    morse_usb) [ -z "$halow" ] && halow=$n ;;
    mt7915e)   [ -z "$mesh24" ] && mesh24=$n ;;
    esac
done
[ -n "$halow" ] || { echo "band-set: no HaLow iface"; exit 1; }

batctl hardif "$halow" hop_penalty "$PEN" || exit 1
[ -n "$mesh24" ] && batctl hardif "$mesh24" hop_penalty 0
echo "set: haLow($halow) hop_penalty=$PEN  2.4G(${mesh24:-?}) hop_penalty=0"
