#!/bin/bash
# Action: deploy patched mesh-voice.py and manet_manage.py to target nodes.
# Backs up stock files; auto-rolls back if a service fails to start.
set -u
source "$(dirname "$0")/../lib/common.sh"
source "$(dirname "$0")/../lib/target.sh"

VOICE_FILE="$REPO_ROOT/files/mesh-voice.py"
MANAGE_FILE="$REPO_ROOT/files/manet_manage.py"
REMOTE_DEPLOY="$REPO_ROOT/remote/deploy-mesh-voice.sh"

deploy_node() {
    local ip=$1
    local remote_voice="/tmp/fleet-$$-mesh-voice.py"
    local remote_manage="/tmp/fleet-$$-manet_manage.py"
    local manage_arg=""

    # Upload files.
    node_scp "$ip" "$remote_voice" "$VOICE_FILE" || {
        print "$C_BAD" "  $ip: upload mesh-voice.py failed"
        return 1
    }

    if [ -f "$MANAGE_FILE" ]; then
        node_scp "$ip" "$remote_manage" "$MANAGE_FILE" || {
            print "$C_BAD" "  $ip: upload manet_manage.py failed"
            return 1
        }
        manage_arg=$remote_manage
    fi

    local out
    out=$(node_root_script "$ip" "$REMOTE_DEPLOY" "$remote_voice" "$manage_arg" 2>&1)
    local rc=$?

    # Clean up uploaded files.
    node_ssh "$ip" "rm -f $remote_voice $remote_manage" 2>/dev/null

    if [ $rc -eq 0 ]; then
        print "$C_OK" "  $ip: $out"
    else
        print "$C_BAD" "  $ip: $out"
        return 1
    fi
}

# --- main -------------------------------------------------------------------

print "$C_HEAD" "  deploy patched voice + web UI (voice-rx-gain branch)"
print "$C_DIM" "  mesh-voice.py: RX gain (+18 dB), playback mixer, limiter"
if [ -f "$MANAGE_FILE" ]; then
    print "$C_DIM" "  manet_manage.py: audio sliders in voice tab"
fi
echo

pick_target

echo
ask "  deploy to $(echo "$TARGET_IPS" | wc -w) node(s)? [y/N]: "
case $ANSWER in
y | Y) ;;
*)
    echo "  cancelled"
    exit 0
    ;;
esac

for ip in $TARGET_IPS; do
    deploy_node "$ip"
done
