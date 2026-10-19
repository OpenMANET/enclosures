#!/bin/bash
# Action: run upstream node-update.sh on target nodes, one at a time.
# Stops on the first failure.
set -u
source "$(dirname "$0")/../lib/common.sh"
source "$(dirname "$0")/../lib/target.sh"

REMOTE="$REPO_ROOT/remote/node-update.sh"
UPDATE_WAIT_SEC=900
POLL_SEC=10

update_node() {
    local ip=$1

    local before
    before=$(node_ssh "$ip" 'head -1 /etc/manet_version.txt' 2>/dev/null)
    print "$C_HEAD" "  $ip  before=$before"

    node_root_script "$ip" "$REMOTE"

    local log="" waited=0
    while [ "$waited" -lt "$UPDATE_WAIT_SEC" ]; do
        sleep "$POLL_SEC"
        waited=$((waited + POLL_SEC))
        log=$(node_ssh "$ip" 'cat /tmp/node-update.log 2>/dev/null' 2>/dev/null) || continue
        grep -q UPDATER_EXIT <<<"$log" && break
    done

    echo "$log"

    if ! grep -q UPDATER_EXIT=0 <<<"$log"; then
        print "$C_BAD" "  STOP: updater failed or did not finish on $ip"
        return 1
    fi

    local after
    after=$(node_ssh "$ip" 'head -1 /etc/manet_version.txt' 2>/dev/null)
    print "$C_OK" "  $ip  after=$after"
}

# --- main -------------------------------------------------------------------

pick_target

for ip in $TARGET_IPS; do
    update_node "$ip" || exit 1
done
print "$C_OK" "  update complete"
