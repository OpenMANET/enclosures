#!/bin/bash
# Shared helpers for fleet scripts. Source, do not run.
#
# Access model:
#   laptop --ssh (key)--> radio@node --sudo -S (SUDO_PASS)--> root
#
# Reads .credentials (SSH_USER, SUDO_PASS, SSH_KEY) and the nodes file
# (one IPv4 per line) from the repo root.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NODES_FILE="$REPO_ROOT/nodes"
CREDENTIALS_FILE="$REPO_ROOT/.credentials"
SSH_TIMEOUT_SEC=5
REBOOT_WAIT_SEC=300

[ -f "$CREDENTIALS_FILE" ] || {
    echo "missing $CREDENTIALS_FILE (copy .credentials.example)" >&2
    exit 1
}
# shellcheck source=/dev/null
source "$CREDENTIALS_FILE"

SSH_OPTS=(-i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout="$SSH_TIMEOUT_SEC"
          -o StrictHostKeyChecking=accept-new)

# --- terminal colors --------------------------------------------------------
# Green = ok, yellow = signal, orange = other, red = offline.

if [ -t 1 ]; then
    C_OK=$'\e[32m' C_WARN=$'\e[33m' C_ORG=$'\e[38;5;208m' C_BAD=$'\e[31m'
    C_HEAD=$'\e[1;36m' C_DIM=$'\e[2m' C_RST=$'\e[0m'
else
    C_OK="" C_WARN="" C_ORG="" C_BAD="" C_HEAD="" C_DIM="" C_RST=""
fi

# --- output helpers ---------------------------------------------------------

# print <color> <text>
print() {
    if [ -n "$2" ]; then
        printf '%s%s%s\n' "$1" "$2" "$C_RST"
    else
        printf '\n'
    fi
}

# Seconds -> "1h 23m" | "42m" | "33s".
fmt_uptime() {
    local s=$1
    if [ "$s" -ge 3600 ]; then
        echo "$((s / 3600))h $(((s % 3600) / 60))m"
    elif [ "$s" -ge 60 ]; then
        echo "$((s / 60))m"
    else
        echo "${s}s"
    fi
}

# --- input helpers ----------------------------------------------------------

# Read one line from the user; sets ANSWER.
# Prefers /dev/tty so prompts stay interactive when stdin is redirected.
ask() {  # $1 prompt text
    local line
    printf '%s' "$1"
    { read -r line < /dev/tty; } 2>/dev/null || read -r line || {
        echo
        echo "no input available, aborting"
        exit 1
    }
    ANSWER=$line
}

# --- SSH transport ----------------------------------------------------------

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

# Wait until SSH answers after a reboot: wait_for_node <ip> <max_sec>.
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

# Reboot a node and wait for it to come back.
reboot_node() {
    local ip=$1
    echo "  rebooting $ip ..."
    printf '%s\n' "$SUDO_PASS" | node_ssh "$ip" "sudo -S -p '' systemctl reboot"
    wait_for_node "$ip" "$REBOOT_WAIT_SEC" \
        || print "$C_BAD" "  WARNING: $ip not back within $((REBOOT_WAIT_SEC / 60)) min"
}

# --- fleet helpers ----------------------------------------------------------

# All node IPs, or the arguments when given.
fleet_ips() {
    if [ $# -gt 0 ]; then
        printf '%s\n' "$@"
        return
    fi
    grep -E '^[0-9.]+$' "$NODES_FILE"
}
