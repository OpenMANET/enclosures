#!/bin/bash
# Shared helpers for the fleet scripts. Source, do not run.
#
#   laptop --ssh (key)--> radio@node --sudo -S (SUDO_PASS)--> root
#
# Reads .credentials (SSH_USER, SUDO_PASS, SSH_KEY) and the nodes file
# (one IPv4 per line) from the repo root.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NODES_FILE="$REPO_ROOT/nodes"
CREDENTIALS_FILE="$REPO_ROOT/.credentials"
SSH_TIMEOUT_SEC=5

[ -f "$CREDENTIALS_FILE" ] || { echo "missing $CREDENTIALS_FILE (copy .credentials.example)" >&2; exit 1; }
# shellcheck source=/dev/null
source "$CREDENTIALS_FILE"

SSH_OPTS=(-i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout="$SSH_TIMEOUT_SEC"
          -o StrictHostKeyChecking=accept-new)

# All node IPs, or the arguments when given.
fleet_ips() {
    if [ $# -gt 0 ]; then
        printf '%s\n' "$@"
        return
    fi
    grep -E '^[0-9.]+$' "$NODES_FILE"
}

# Run a command as the SSH user.
node_ssh() {
    local ip=$1
    shift
    ssh "${SSH_OPTS[@]}" "$SSH_USER@$ip" "$@"
}

# Copy local files to the node: node_scp <ip> <dest> <file>...
node_scp() {
    local ip=$1 dest=$2
    shift 2
    scp -q "${SSH_OPTS[@]}" "$@" "$SSH_USER@$ip:$dest"
}

# Run a local script on the node as root: node_root_script <ip> <script> [args].
# The script is copied, not piped: the sudo password owns ssh's stdin.
node_root_script() {
    local ip=$1 script=$2
    shift 2
    local remote="/tmp/fleet-$$-$(basename "$script")"
    node_scp "$ip" "$remote" "$script" || return 1
    printf '%s\n' "$SUDO_PASS" | node_ssh "$ip" \
        "sudo -S -p '' bash $remote $*; rc=\$?; rm -f $remote; exit \$rc"
}

# Wait until SSH answers again after a reboot: wait_for_node <ip> <max_sec>.
wait_for_node() {
    local ip=$1 max=$2 waited=0
    sleep 30
    while [ "$waited" -lt "$max" ]; do
        if node_ssh "$ip" true 2>/dev/null; then
            return 0
        fi
        sleep 10
        waited=$((waited + 10))
    done
    return 1
}
