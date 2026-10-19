#!/bin/bash
# Apply fixes/voice-tab/voice_webGUI_fix.sh (blank VOICE tab, MANET
# 0.569-0.572). Idempotent: an already fixed node reports "already fixed".
# Drop once the release with the maintainer's fix is installed.
# usage: scripts/deploy-voice-tab-fix.sh [ip...]
set -u
source "$(dirname "$0")/lib.sh"

FIX="$REPO_ROOT/fixes/voice-tab/voice_webGUI_fix.sh"

for ip in $(fleet_ips "$@"); do
    echo "== $ip"
    node_root_script "$ip" "$FIX" || { echo "STOP at $ip"; exit 1; }
done
