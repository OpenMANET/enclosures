#!/bin/bash
# Set the OpenVLM Mic capture level on the voice card of every node and
# persist it in the ALSA state (restored at boot by alsa-restore.service).
#
# Fleet standard: 90%. The codec steps are 1 dB, so 90% lands on 91%
# (+20 dB, value 32/35) on every node.
#
# A node without its OpenVLM board plugged in reports "no OpenVLM/USB
# audio card" and exits non-zero; the level is remembered only once the
# board is present, since hot-plug does not re-run alsa-restore.
# usage: scripts/set-mic-gain.sh [pct] [ip...]
set -u
source "$(dirname "$0")/lib.sh"

PCT=${1:-90}
case "$PCT" in
    ''|*[!0-9]*) echo "usage: $0 [pct] [ip...]" >&2; exit 2 ;;
esac
[ "$PCT" -le 100 ] || { echo "pct must be 0-100" >&2; exit 2; }
[ $# -gt 0 ] && shift

rc=0
for ip in $(fleet_ips "$@"); do
    node_root_script "$ip" "$REPO_ROOT/scripts/remote/set-mic-gain.sh" "$PCT" \
        || { echo "note: $ip could not be set (board missing or node down)"; rc=1; }
done
exit "$rc"
