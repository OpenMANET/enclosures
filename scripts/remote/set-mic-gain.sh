#!/bin/bash
# Set the Mic capture level on the OpenVLM (USB audio) card and persist it
# into the ALSA state, so alsa-restore.service restores it at boot.
# Runs on the node as root.
# usage: set-mic-gain.sh <pct>
set -u

PCT=${1:?usage: set-mic-gain.sh <pct>}

line=$(grep -E 'USB Audio|OpenVLM' /proc/asound/cards | head -1) || {
    echo "no OpenVLM/USB audio card present; nothing to set"
    exit 3
}
idx=$(printf '%s\n' "$line" | awk '{print $1}')
name=$(printf '%s\n' "$line" | cut -d'[' -f2 | cut -d']' -f1 | tr -d ' ')

amixer -c "$idx" sset "Mic" "${PCT}%" >/dev/null
alsactl store

live=$(amixer -c "$idx" get Mic | grep -oE '\[[0-9]+%\] \[-?[0-9.]+dB\]' | head -1)
state=$(sed -n "/^state\.$name {/,/^}/p" /var/lib/alsa/asound.state \
    | grep -A1 'Mic Capture Volume' | grep -oE 'value [0-9]+' | head -1 | awk '{print $2}')
echo "card=$name idx=$idx live=$live state_value=$state"
