#!/bin/bash
# Run upstream node-update.sh on nodes, one at a time, stopping on failure.
# Afterwards re-apply local fixes the release lacks (deploy-mesh-voice.sh).
#
# Known failure: "Recovery archive is missing; keep in-progress and repair the
# installation locally" means an earlier update was interrupted (mesh-0825,
# 2026-10-09). Move /var/lib/manet-update/in-progress aside and re-run; see
# FIXES.md.
# usage: scripts/update-node.sh [ip...]
set -u
source "$(dirname "$0")/lib.sh"

UPDATE_WAIT_SEC=900
POLL_SEC=10

for ip in $(fleet_ips "$@"); do
    echo "== $ip before=$(node_ssh "$ip" 'head -1 /etc/manet_version.txt')"
    node_root_script "$ip" "$REPO_ROOT/scripts/remote/node-update-start.sh"

    log=""
    waited=0
    while [ "$waited" -lt "$UPDATE_WAIT_SEC" ]; do
        sleep "$POLL_SEC"
        waited=$((waited + POLL_SEC))
        log=$(node_ssh "$ip" 'cat /tmp/node-update.log 2>/dev/null' 2>/dev/null) || continue
        grep -q UPDATER_EXIT <<<"$log" && break
    done
    echo "$log"

    if ! grep -q UPDATER_EXIT=0 <<<"$log"; then
        echo "STOP: updater failed or did not finish on $ip"
        exit 1
    fi
    echo "after=$(node_ssh "$ip" 'head -1 /etc/manet_version.txt')"
done
