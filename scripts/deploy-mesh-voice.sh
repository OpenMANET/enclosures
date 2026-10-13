#!/bin/bash
# Install the patched mesh-voice.py (fixes/mesh-voice-audio) on nodes.
# Upstream PR: https://github.com/very-srs/MANET/pull/8
# Any node-update.sh run replaces it with the release's daemon; re-run this
# afterwards until a release contains the PR.
# usage: scripts/deploy-mesh-voice.sh [ip...]
set -u
source "$(dirname "$0")/lib.sh"

DAEMON="$REPO_ROOT/fixes/mesh-voice-audio/mesh-voice.py"
MD5=$(md5sum < "$DAEMON" | cut -d' ' -f1)

for ip in $(fleet_ips "$@"); do
    node_scp "$ip" /tmp/mesh-voice.py "$DAEMON" || { echo "$ip: copy failed"; exit 1; }
    node_root_script "$ip" "$REPO_ROOT/scripts/remote/install-mesh-voice.sh" "$MD5" || { echo "STOP at $ip"; exit 1; }
done
