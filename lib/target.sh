#!/bin/bash
# Target selection: every action sources this and calls pick_target.
# Sets TARGET_IPS (space-separated): all nodes or one IP.
# Requires lib/common.sh to be sourced first.

TARGET_IPS=""

pick_target() {
    local all_ips
    all_ips=$(fleet_ips | tr '\n' ' ')
    local count
    count=$(echo "$all_ips" | wc -w)

    echo
    print "$C_DIM" "  nodes ($count): $all_ips"
    ask "  target [Enter=all, or one IP]: "

    case $ANSWER in
    '' | a | all)
        TARGET_IPS=$all_ips
        ;;
    *)
        if grep -qxF "$ANSWER" "$NODES_FILE"; then
            TARGET_IPS=$ANSWER
        else
            print "$C_WARN" "  $ANSWER not in 'nodes'; targeting all"
            TARGET_IPS=$all_ips
        fi
        ;;
    esac
}
