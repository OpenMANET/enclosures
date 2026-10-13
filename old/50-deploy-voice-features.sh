#!/bin/bash
# ============================================================================
# 50-deploy-voice-features.sh <ip> <expected-hostname>
#
# Installs the patched mesh-voice.py (node-files/mesh-voice.py) which adds:
#   * PTT keying sidetone   -- local pop on key + roger beep on release
#   * Free-speak latch       -- triple-tap PTT toggles hands-free (mic latched
#                               open); triple-tap again to leave. Rising/falling
#                               latch beeps announce the mode.
# Both are LOCAL only (mixed into the speaker path, never transmitted) and are
# enabled via /etc/mesh.conf keys added here. Backs up the stock daemon once.
# Also installs manet-audio-level.service, which forces the OpenVLM playback to
# 0 dB at boot (the stock alsa-restore is unreliable for this USB card).
# Idempotent; restarts mesh-voice.
# ============================================================================
set -uo pipefail
SD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; source "$SD/lib.sh"
IP="${1:?usage: $0 <ip> <hostname>}"; HOST="${2:?usage: $0 <ip> <hostname>}"
DAEMON="$SD/node-files/mesh-voice.py"
ALVL="$SD/node-files/manet-audio-level.sh"
ALVLSVC="$SD/node-files/manet-audio-level.service"
[[ -f "$DAEMON" ]] || { fail "missing $DAEMON"; exit 1; }
python3 -m py_compile "$DAEMON" || { fail "patched daemon has a syntax error"; exit 1; }

verify_hostname "$IP" "$HOST" || exit 1

log "installing boot-time OpenVLM audio-level fix ..."
put_scp "$ALVL"    "$IP" /usr/local/bin/manet-audio-level.sh 755
put_scp "$ALVLSVC" "$IP" /etc/systemd/system/manet-audio-level.service 644
ssh_rootp "$IP" "systemctl daemon-reload; systemctl enable manet-audio-level.service >/dev/null 2>&1; /usr/local/bin/manet-audio-level.sh"

log "backing up stock mesh-voice.py (once) + installing patched daemon ..."
ssh_root "$IP" "test -f /usr/local/bin/mesh-voice.py.stock-bak || cp /usr/local/bin/mesh-voice.py /usr/local/bin/mesh-voice.py.stock-bak"
put_scp "$DAEMON" "$IP" /usr/local/bin/mesh-voice.py 755

log "enabling features in /etc/mesh.conf ..."
for kv in voice_ptt_beep=y voice_ptt_latch=y voice_latch_presses=3 voice_latch_window_ms=1500; do
  k="${kv%%=*}"
  ssh_root "$IP" "grep -q \"^$k=\" /etc/mesh.conf && sed -i \"s|^$k=.*|$kv|\" /etc/mesh.conf || echo \"$kv\" >> /etc/mesh.conf"
done

log "restarting mesh-voice (stop -> wait for the card to free -> start) ..."
ssh_rootp "$IP" 'systemctl stop mesh-voice.service
  for i in $(seq 1 10); do fuser /dev/snd/pcmC0D0p >/dev/null 2>&1 || break; sleep 1; done
  systemctl start mesh-voice.service; sleep 3'

# VOICE-tab HTML-nesting fix for the /manage UI (from mesh-voice-fleet-rollout):
# manet_manage.py nested <div id=tab-voice> inside the display:none
# <div id=tab-config>, so the VOICE tab was always hidden (blank page at
# /manage/#voice). Idempotent patcher; restart mesh-status if it changed anything.
VTAB="$SD/node-files/fix_voice_tab.py"
if [[ -f "$VTAB" ]]; then
  log "applying /manage VOICE-tab fix ..."
  put_scp "$VTAB" "$IP" /tmp/fix_voice_tab.py 755
  vout=$(ssh_rootp "$IP" 'python3 /tmp/fix_voice_tab.py 2>&1'); echo "  $vout"
  echo "$vout" | grep -qi 'Patched' && ssh_rootp "$IP" 'systemctl restart mesh-status.service'
  ssh_cmd "$IP" 'rm -f /tmp/fix_voice_tab.py' 2>/dev/null
fi
echo "  --- status ---"
ssh_rootp "$IP" '
echo "  mesh-voice: $(systemctl is-active mesh-voice.service)"
echo "  config: $(grep -E "^voice_ptt_(beep|latch)=" /etc/mesh.conf | tr "\n" " ")"
echo "  log: $(journalctl -u mesh-voice.service --no-pager -n 5 2>/dev/null | grep -iE "codec|ptt|audio|latch|error|traceback" | tail -3)"
python3 -c "import json;d=json.load(open(\"/run/mesh-voice.json\"));print(\"  json latched=\",d.get(\"latched\"),\"ptt_connected=\",d.get(\"ptt_connected\"))" 2>/dev/null'
ok "voice features deployed on $HOST"
