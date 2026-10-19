#!/bin/bash
# Action: check and optionally set audio levels on target nodes.
#
# Three controls on the CM108B (OpenVLM board):
#   Mic Capture       - transmit side (how loud you send)
#   PCM Playback      - receive side (how loud you hear others)
#   Auto Gain Control - fights the capture level; off with a preamp
#
# Fleet standard:
#   Mic Capture  90% -> value 32/35 (91%, +20 dB)
#   PCM Playback 100% -> value 37/37 (0 dB, maximum)
#   AGC off
set -u
source "$(dirname "$0")/../lib/common.sh"
source "$(dirname "$0")/../lib/target.sh"

REMOTE_SET="$REPO_ROOT/remote/set-mic-gain.sh"
MIC_STD_PCT=90
MIC_STD_VAL=32
PCM_STD_PCT=100
PCM_STD_VAL=37  # 0 dB, maximum output

check_audio() {
    local ip=$1

    local out
    if ! out=$(node_root_script "$ip" "$REPO_ROOT/remote/node-status.sh" 2>/dev/null); then
        print "$C_BAD" "  $ip: unreachable"
        return 0
    fi

    local host mic_val pcm_val agc_val
    host=$(awk '/^NODE /{sub(/host=/, "", $2); print $2}' <<<"$out")
    mic_val=$(awk '/^MIC /{sub(/val=/, "", $3); print $3}' <<<"$out")
    pcm_val=$(awk '/^MIC /{sub(/pcm=/, "", $4); print $4}' <<<"$out")
    agc_val=$(awk '/^MIC /{sub(/agc=/, "", $5); print $5}' <<<"$out")

    if [ -z "$mic_val" ] || [ "$mic_val" = "none" ]; then
        print "$C_DIM" "  ${host:-$ip}: no audio board"
        return 0
    fi

    # Mic capture.
    local mic_str
    if [ "$mic_val" = "$MIC_STD_VAL" ]; then
        mic_str="$C_OK$mic_val"
    else
        mic_str="$C_ORG$mic_val (want $MIC_STD_VAL)"
    fi

    # PCM playback.
    local pcm_str
    if [ "${pcm_val:-?}" = "$PCM_STD_VAL" ]; then
        pcm_str="$C_OK$pcm_val"
    elif [ "${pcm_val:-?}" = "?" ]; then
        pcm_str="${C_DIM}unknown"
    else
        pcm_str="$C_ORG$pcm_val (want $PCM_STD_VAL)"
    fi

    # AGC.
    local agc_str
    if [ "${agc_val:-?}" = "off" ]; then
        agc_str="${C_OK}off"
    elif [ "${agc_val:-?}" = "on" ]; then
        agc_str="${C_ORG}on"
    else
        agc_str="${C_DIM}?"
    fi

    printf '  %-12s  mic: %b%s  pcm: %b%s  agc: %b%s\n' \
        "${host:-$ip}" "$mic_str" "$C_RST" "$pcm_str" "$C_RST" "$agc_str" "$C_RST"
}

set_audio() {
    local ip=$1 mic_pct=$2 pcm_pct=$3

    local result
    if result=$(node_root_script "$ip" "$REMOTE_SET" "$mic_pct" "$pcm_pct" 2>/dev/null); then
        print "$C_OK" "  $ip: $result"
    else
        print "$C_BAD" "  $ip: failed (board missing or node down)"
    fi
}

# --- main -------------------------------------------------------------------

pick_target

echo
print "$C_HEAD" "  audio levels (standard: mic=$MIC_STD_VAL pcm=$PCM_STD_VAL agc=off)"
print "$C_DIM" "  ----------------------------------------------------------------"

for ip in $TARGET_IPS; do
    check_audio "$ip"
done

echo
ask "  apply standard (mic ${MIC_STD_PCT}%, pcm ${PCM_STD_PCT}%, agc off)? [y/N]: "
case $ANSWER in
y | Y)
    for ip in $TARGET_IPS; do
        set_audio "$ip" "$MIC_STD_PCT" "$PCM_STD_PCT"
    done
    ;;
*)
    echo "  no changes"
    ;;
esac
