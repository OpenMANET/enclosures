#!/bin/bash
# Action: region swap. Accepts any 2-letter country code, maps to the
# EU or US HaLow channel plan. Writes config and optionally reboots.
#
# HaLow plans: US = 907 MHz, everything else = EU 863.5 MHz.
# Nodes on different plans cannot HaLow-mesh each other.
set -u
source "$(dirname "$0")/../lib/common.sh"
source "$(dirname "$0")/../lib/target.sh"

REMOTE_STATE="$REPO_ROOT/remote/region-state.sh"
REMOTE_APPLY="$REPO_ROOT/remote/region-apply.sh"

# The HaLow plan a country code maps to (mirrors manet-region.py).
halow_plan() {  # $1 CC -> "US" or "EU"
    if [ "$1" = "US" ]; then
        echo "US"
    else
        echo "EU"
    fi
}

region_verify() {
    local ip=$1
    local state
    state=$(node_root_script "$ip" "$REMOTE_STATE" 2>/dev/null)
    local reg hal freq
    read -r reg hal freq <<<"${state:-? ? ?}"
    print "$C_OK" "  $ip: regulatory=$reg HaLow=$hal ${freq} kHz"
}

region_reboot() {
    local ap_node="" front="" ip

    ask "  which node's AP is the laptop on? (IP, or Enter if none/ethernet): "
    local ap=$ANSWER

    for ip in "$@"; do
        if [ "$ip" = "$ap" ]; then
            ap_node=$ip
        else
            front="$front $ip"
        fi
    done

    for ip in $front; do
        reboot_node "$ip"
        region_verify "$ip"
    done

    if [ -n "$ap_node" ]; then
        echo
        print "$C_WARN" "  rebooting your AP node $ap_node last; expect a brief disconnect"
        reboot_node "$ap_node"
        region_verify "$ap_node"
    fi
}

# --- main -------------------------------------------------------------------

pick_target

echo
ask "  regulatory domain, 2-letter code (US, RO, DE, GB, ...): "
CC=${ANSWER^^}

if ! [[ $CC =~ ^[A-Z]{2}$ ]]; then
    print "$C_BAD" "  need a two-letter country code"
    exit 2
fi

NEWPLAN=$(halow_plan "$CC")
PLAN_CHANGED=0

echo
echo "  current -> $CC (HaLow plan $NEWPLAN):"
for ip in $TARGET_IPS; do
    state=$(node_root_script "$ip" "$REMOTE_STATE" 2>/dev/null)
    reg="" hal="" freq=""
    read -r reg hal freq <<<"${state:-? ? ?}"
    printf '    %-15s %-8s (HaLow %-4s, %s kHz)\n' "$ip" "$reg" "$hal" "$freq"
    oldplan=$(halow_plan "${hal:-?}")
    [ "$NEWPLAN" != "$oldplan" ] && PLAN_CHANGED=1
done

if [ "$PLAN_CHANGED" -eq 1 ]; then
    echo
    print "$C_WARN" "  WARNING: HaLow plan changes channel (US 907 MHz / EU 863.5 MHz)."
    print "$C_WARN" "  Nodes on different plans cannot HaLow-mesh."
    print "$C_WARN" "  Switch every fleet node, or the HaLow mesh splits."
fi

echo
ask "  write region config? [y/N]: "
case $ANSWER in
y | Y) ;;
*)
    echo "  cancelled"
    exit 0
    ;;
esac

for ip in $TARGET_IPS; do
    echo
    echo "  $ip:"
    out=$(node_root_script "$ip" "$REMOTE_APPLY" "$CC" 2>&1)
    while IFS= read -r line; do
        echo "    $line"
    done <<<"$out"
done

echo
print "$C_OK" "  config written; inert until each node reboots."
ask "  reboot now (one at a time, your AP node last)? [y/N]: "
case $ANSWER in
y | Y) region_reboot $TARGET_IPS ;;
*) echo "  deferred; reboot when ready." ;;
esac
