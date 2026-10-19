#!/bin/bash
# Action: toggle auto_update=y|n in /etc/mesh.conf on target nodes.
set -u
source "$(dirname "$0")/../lib/common.sh"
source "$(dirname "$0")/../lib/target.sh"

toggle_auto_update() {
    local ip=$1 value=$2

    printf '%s\n' "$SUDO_PASS" | node_ssh "$ip" \
        "sudo -S -p '' sed -i 's/^auto_update=.*/auto_update=$value/' /etc/mesh.conf \
         && echo \"\$(hostname): \$(grep ^auto_update= /etc/mesh.conf)\""
}

# --- main -------------------------------------------------------------------

pick_target

echo
ask "  set auto_update to [y/n]: "
case $ANSWER in
y | n) ;;
*)
    print "$C_BAD" "  need y or n"
    exit 2
    ;;
esac
VALUE=$ANSWER

for ip in $TARGET_IPS; do
    result=$(toggle_auto_update "$ip" "$VALUE" 2>/dev/null)
    if [ -n "$result" ]; then
        print "$C_OK" "  $result"
    else
        print "$C_BAD" "  $ip: unreachable or failed"
    fi
done
