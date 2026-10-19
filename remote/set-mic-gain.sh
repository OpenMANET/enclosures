#!/bin/bash
# Runs ON a node as root. Sets the Mic capture level on the OpenVLM
# (USB audio) card and persists it via ALSA state, so alsa-restore.service
# restores it at boot.
#
# usage: set-mic-gain.sh <pct>
set -u

PCT=${1:?usage: set-mic-gain.sh <pct>}

line=$(grep -E 'USB Audio|OpenVLM' /proc/asound/cards 2>/dev/null | head -1)
if [ -z "$line" ]; then
    echo "no OpenVLM/USB audio card present"
    exit 3
fi

idx=$(awk '{print $1}' <<<"$line")
name=$(cut -d'[' -f2 <<<"$line" | cut -d']' -f1 | tr -d ' ')

# $2 = optional PCM playback percentage (empty = skip).
PCM_PCT=${2:-}

amixer -c "$idx" sset "Mic" "${PCT}%" >/dev/null

# AGC fights the capture level; disable it when a preamp is present.
amixer -c "$idx" sset "Auto Gain Control" off >/dev/null 2>&1

if [ -n "$PCM_PCT" ]; then
    amixer -c "$idx" sset "PCM" "${PCM_PCT}%" >/dev/null
fi

alsactl store

mic_live=$(amixer -c "$idx" get Mic \
    | grep -oE '\[[0-9]+%\] \[-?[0-9.]+dB\]' | head -1)
pcm_live=$(amixer -c "$idx" get PCM \
    | grep -oE '\[[0-9]+%\] \[-?[0-9.]+dB\]' | head -1)
state=$(sed -n "/^state\.$name {/,/^}/p" /var/lib/alsa/asound.state \
    | grep -A1 'Mic Capture Volume' | grep -oE 'value [0-9]+' \
    | head -1 | awk '{print $2}')
echo "card=$name idx=$idx mic=$mic_live pcm=$pcm_live state_value=$state"
