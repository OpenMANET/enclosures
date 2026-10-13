#!/bin/bash
# Read-only health check of every node (or the IPs given).
# usage: scripts/fleet-check.sh [ip...]
set -u
source "$(dirname "$0")/lib.sh"

for ip in $(fleet_ips "$@"); do
    if ! node_root_script "$ip" "$REPO_ROOT/scripts/remote/fleet-check.sh"; then
        echo "==== $ip: UNREACHABLE"
    fi
done
