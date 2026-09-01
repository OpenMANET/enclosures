#!/bin/bash
# ============================================================================
# manet-audio-level.sh — set the OpenVLM USB audio playback to max at boot.
#
# The stock per-card restore is unreliable for this USB card on this image:
# 90-alsa-restore.rules is broken (`GOTO ... has no matching label`) and the
# OpenVLM (CM108, usbid 0d8c:0012) enumerates late, so alsa-restore misses it
# and the card comes up at its -20 dB hardware default -> quiet on receive.
# This waits for the card, forces PCM to 0 dB, and re-stores asound.state.
# Ordered Before mesh-voice by manet-audio-level.service.
# ============================================================================
set -u
log() { logger -t manet-audio-level "$*"; echo "manet-audio-level: $*"; }

card=""
for _ in $(seq 1 40); do
    for p in /proc/asound/card*/usbid; do
        [ -e "$p" ] || continue
        if grep -qiE "0d8c:0012" "$p"; then
            card=$(basename "$(dirname "$p")" | tr -dc '0-9'); break
        fi
    done
    [ -n "$card" ] && break
    sleep 1
done
if [ -z "$card" ]; then
    log "OpenVLM card (0d8c:0012) not found after 40s; leaving audio untouched"
    exit 0
fi

amixer -c "$card" sset PCM 100% unmute >/dev/null 2>&1
alsactl store >/dev/null 2>&1 || true
now=$(amixer -c "$card" sget PCM 2>/dev/null | grep -m1 'Front Left:' | sed 's/.*Playback //')
log "card $card PCM playback set to ${now:-unknown}"
exit 0
