#!/bin/bash
# Set the band-priority mode: persist the HaLow hop_penalty, install the
# boot-apply unit (idempotent), and apply it live now. Runs as root; the
# two files to install are staged in /tmp by the caller.
# usage: band-install.sh <halow_penalty>
set -u
PEN=${1:?usage: band-install.sh <halow_penalty>}
case $PEN in (*[!0-9]*|'') echo "band-install: penalty must be a non-negative integer"; exit 2 ;; esac

[ -f /tmp/band-apply.sh ] || { echo "band-install: /tmp/band-apply.sh missing (staged by the caller)"; exit 1; }
[ -f /tmp/band-priority.service ] || { echo "band-install: /tmp/band-priority.service missing"; exit 1; }
install -m 0755 /tmp/band-apply.sh /usr/local/bin/manet-band-priority-apply.sh
install -m 0644 /tmp/band-priority.service /etc/systemd/system/manet-band-priority.service
rm -f /tmp/band-apply.sh /tmp/band-priority.service

# Persist the selection; a future boot applies it via the unit.
printf '%s\n' "$PEN" > /etc/manet-band-halow-hop-penalty
systemctl daemon-reload
systemctl enable manet-band-priority.service >/dev/null 2>&1

# Apply live now (detect the HaLow + 2.4 GHz batman members).
batmembers=$(batctl meshif bat0 interface 2>/dev/null | cut -d: -f1)
halow="" mesh24=""
for i in /sys/class/net/wlan*; do
    [ -e "$i" ] || continue
    n=$(basename "$i")
    d=$(basename "$(readlink -f "$i/device/driver" 2>/dev/null)" 2>/dev/null)
    case $d in
    morse_usb) [ -z "$halow" ] && printf '%s\n' "$batmembers" | grep -qx "$n" && halow=$n ;;
    mt7915e)   [ -z "$mesh24" ] && printf '%s\n' "$batmembers" | grep -qx "$n" && mesh24=$n ;;
    esac
done
[ -n "$halow" ] || { echo "band-install: no HaLow batman member"; exit 1; }
batctl hardif "$halow" hop_penalty "$PEN" || exit 1
[ -n "$mesh24" ] && batctl hardif "$mesh24" hop_penalty 0
echo "haLow($halow) hop_penalty=$PEN applied + persisted (survives reboot)"
