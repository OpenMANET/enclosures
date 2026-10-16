#!/bin/bash
# Boot apply: set the HaLow hardif's batman hop_penalty from the persisted
# value. No-op when the file is absent (the fleet default is link quality,
# penalty 0). Installed to /usr/local/bin by band-install.sh.
set -u
conf=/etc/manet-band-halow-hop-penalty
[ -f "$conf" ] || exit 0
pen=$(tr -dc '0-9' <"$conf" | head -c 3)
[ -n "$pen" ] || exit 0

batmembers=$(batctl meshif bat0 interface 2>/dev/null | cut -d: -f1)
halow=""
for i in /sys/class/net/wlan*; do
    [ -e "$i" ] || continue
    n=$(basename "$i")
    d=$(basename "$(readlink -f "$i/device/driver" 2>/dev/null)" 2>/dev/null)
    if [ "$d" = morse_usb ] && printf '%s\n' "$batmembers" | grep -qx "$n"; then
        halow=$n
        break
    fi
done
[ -n "$halow" ] || exit 0
batctl hardif "$halow" hop_penalty "$pen" 2>/dev/null || true
exit 0
