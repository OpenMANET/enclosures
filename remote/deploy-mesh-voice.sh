#!/bin/bash
# Runs ON a node as root. Deploys patched mesh-voice.py and/or
# manet_manage.py, restarts the affected services. Backs up originals.
#
# usage: deploy-voice.sh <mesh-voice.py> [manet_manage.py]
set -u

VOICE_SRC=${1:-}
MANAGE_SRC=${2:-}
VOICE_DEST=/usr/local/bin/mesh-voice.py
MANAGE_DEST=/usr/local/bin/manet_manage.py

deployed=""

# Deploy mesh-voice.py if provided.
if [ -n "$VOICE_SRC" ] && [ -f "$VOICE_SRC" ]; then
    [ -f "${VOICE_DEST}.stock" ] || cp "$VOICE_DEST" "${VOICE_DEST}.stock"
    cp "$VOICE_SRC" "$VOICE_DEST"
    chmod 755 "$VOICE_DEST"
    deployed="mesh-voice"
fi

# Deploy manet_manage.py if provided.
if [ -n "$MANAGE_SRC" ] && [ -f "$MANAGE_SRC" ]; then
    [ -f "${MANAGE_DEST}.stock" ] || cp "$MANAGE_DEST" "${MANAGE_DEST}.stock"
    cp "$MANAGE_SRC" "$MANAGE_DEST"
    chmod 755 "$MANAGE_DEST"
    deployed="$deployed${deployed:+ }manet_manage"
fi

[ -n "$deployed" ] || { echo "nothing to deploy"; exit 1; }
sync

# Restart affected services.
case $deployed in
*mesh-voice*)
    systemctl restart mesh-voice
    sleep 2
    state=$(systemctl is-active mesh-voice 2>/dev/null)
    if [ "$state" != "active" ]; then
        echo "WARNING: mesh-voice failed; restoring backup"
        cp "${VOICE_DEST}.stock" "$VOICE_DEST"
        systemctl restart mesh-voice
        echo "restored mesh-voice: $(systemctl is-active mesh-voice 2>/dev/null)"
        exit 1
    fi
    ;;
esac

case $deployed in
*manet_manage*)
    systemctl restart mesh-status
    sleep 2
    state=$(systemctl is-active mesh-status 2>/dev/null)
    if [ "$state" != "active" ]; then
        echo "WARNING: mesh-status failed; restoring backup"
        cp "${MANAGE_DEST}.stock" "$MANAGE_DEST"
        systemctl restart mesh-status
        echo "restored mesh-status: $(systemctl is-active mesh-status 2>/dev/null)"
        exit 1
    fi
    ;;
esac

echo "deployed: $deployed (all active)"
