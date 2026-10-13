#!/bin/bash
# Set auto_update=y|n in /etc/mesh.conf. Every upstream update trigger
# (uplink dispatcher hook, clock-sync path unit) checks this key first.
# Currently n: a release would overwrite the patched mesh-voice.py.
# usage: scripts/set-auto-update.sh y|n [ip...]
set -u
source "$(dirname "$0")/lib.sh"

VALUE=${1:-}
case "$VALUE" in
    y|n) ;;
    *) echo "usage: $0 y|n [ip...]" >&2; exit 2 ;;
esac
shift

for ip in $(fleet_ips "$@"); do
    printf '%s\n' "$SUDO_PASS" | node_ssh "$ip" \
        "sudo -S -p '' sed -i 's/^auto_update=.*/auto_update=$VALUE/' /etc/mesh.conf && echo \"\$(hostname): \$(grep ^auto_update= /etc/mesh.conf)\""
done
