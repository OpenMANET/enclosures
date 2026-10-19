#!/bin/bash
# Enforce the lab radio policy on nodes with an onboard brcmfmac radio:
#
#   MT7916 2.4 GHz (wlan0) --+
#                            +-- bat0 mesh
#   Morse HaLow    (wlan2) --+
#   MT7916 5 GHz   (wlan1) ----- EUD AP (rdt-manet-<id>)
#   brcmfmac (onboard)     ----- blacklisted
#
# Reboots the node if brcmfmac is loaded, then reruns radio-setup.sh.
# Do not run against the node whose AP this laptop is using.
# usage: scripts/apply-radio-policy.sh <ip>...
set -u
source "$(dirname "$0")/lib.sh"

REBOOT_WAIT_SEC=300
SETUP_WAIT_SEC=600
POLL_SEC=10
R="$REPO_ROOT/scripts/remote"

[ $# -gt 0 ] || { echo "usage: $0 <ip>..." >&2; exit 2; }

for ip in "$@"; do
    echo "== $ip"

    # Blacklist; reboot only when the module is loaded now.
    if node_root_script "$ip" "$R/no-brcmfmac.sh" | grep -q reboot; then
        echo "rebooting to unload brcmfmac"
        printf '%s\n' "$SUDO_PASS" | node_ssh "$ip" "sudo -S -p '' systemctl reboot" || true
        wait_for_node "$ip" "$REBOOT_WAIT_SEC" || { echo "STOP: $ip did not come back"; exit 1; }
    fi

    # Reassign roles and wait for the run to finish.
    node_root_script "$ip" "$R/radio-setup-start.sh"
    waited=0
    state=running
    while [ "$waited" -lt "$SETUP_WAIT_SEC" ]; do
        sleep "$POLL_SEC"
        waited=$((waited + POLL_SEC))
        state=$(node_root_script "$ip" "$R/radio-setup-done.sh" 2>/dev/null) || continue
        [ "$state" = running ] || break
    done
    echo "$state"
done
