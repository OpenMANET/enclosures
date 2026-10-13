#!/bin/bash
# Runs ON a node as root. Installs /tmp/mesh-voice.py over the stock daemon.
# usage: install-mesh-voice.sh <patched-md5>
set -eu

WANT_MD5=$1
NEW=/tmp/mesh-voice.py
BIN=/usr/local/bin/mesh-voice.py
CARD_RELEASE_WAIT_SEC=10
CARD_SETTLE_SEC=2
START_WAIT_SEC=15

# Refuse a corrupt copy.
[ "$(md5sum < "$NEW" | cut -d' ' -f1)" = "$WANT_MD5" ] || { echo "$(hostname): copy md5 mismatch"; exit 1; }
python3 -m py_compile "$NEW"

if [ "$(md5sum < "$BIN" | cut -d' ' -f1)" = "$WANT_MD5" ]; then
    echo "$(hostname): already installed"
    rm -f "$NEW"
    exit 0
fi
install -m 755 "$NEW" "$BIN"
rm -f "$NEW"

# Stop, wait for the card to be released, then start. Starting while the old
# process still holds the PCM fails as "device busy" until the 5 s retry.
systemctl stop mesh-voice.service
for _ in $(seq 1 "$CARD_RELEASE_WAIT_SEC"); do
    fuser /dev/snd/pcmC*D0[cp] >/dev/null 2>&1 || break
    sleep 1
done
sleep "$CARD_SETTLE_SEC"
systemctl start mesh-voice.service
sleep "$START_WAIT_SEC"

echo "$(hostname): mesh-voice $(systemctl is-active mesh-voice.service)"
journalctl -u mesh-voice.service --since "-20s" --no-pager -o cat | grep -E "audio:|Traceback" || true
