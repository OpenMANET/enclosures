#!/bin/bash
# Fix the blank VOICE tab on the manage page (MANET 0.569 through 0.572).
#
# The voice pane was nested inside the NODE CONFIG pane, so selecting VOICE
# showed an empty page. This moves it out and restarts the web server.
# Run on the radio as root:  sudo bash patch-voice-tab.sh
# Safe to run twice. A backup is kept next to the original.
set -eu

TARGET=/usr/local/bin/manet_manage.py
BACKUP=$TARGET.before-voice-tab-fix

usage() {
  echo "usage: sudo bash $0" >&2
  exit 2
}
[ $# -eq 0 ] || usage
[ "$(id -u)" -eq 0 ] || {
  echo "patch-voice-tab: run as root (sudo)" >&2
  exit 1
}
[ -f "$TARGET" ] || {
  echo "patch-voice-tab: $TARGET not found; is this a MANET radio?" >&2
  exit 1
}

rc=0
python3 - "$TARGET" "$BACKUP" <<'PY' || rc=$?
import os, shutil, sys, tempfile

target, backup = sys.argv[1], sys.argv[2]
old = '''  <!-- ── NODE CONFIG ── -->
  <!-- The old /admin page. Same staging/ACK/apply flow, now behind the same
       password as everything else here rather than open on port 80. -->
  <div id="tab-config" class="tab-pane" style="display:none">
<!-- ── VOICE ── -->
  <div id="tab-voice" class="tab-pane" style="display:none">{VOICE_TAB_HTML}</div>

{CONFIG_TAB_HTML}'''
new = '''  <!-- ── VOICE ── -->
  <div id="tab-voice" class="tab-pane" style="display:none">{VOICE_TAB_HTML}</div>

  <!-- ── NODE CONFIG ── -->
  <!-- The old /admin page. Same staging/ACK/apply flow, now behind the same
       password as everything else here rather than open on port 80. -->
  <div id="tab-config" class="tab-pane" style="display:none">
{CONFIG_TAB_HTML}'''

with open(target, encoding='utf-8') as f:
    src = f.read()
if new in src and old not in src:
    print('patch-voice-tab: already fixed, nothing to do')
    sys.exit(3)
if src.count(old) != 1:
    print('patch-voice-tab: unexpected file contents; not changing anything',
          file=sys.stderr)
    sys.exit(1)

if not os.path.exists(backup):
    shutil.copy2(target, backup)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix='.manet_manage.')
try:
    fixed = src.replace(old, new)
    compile(fixed, target, 'exec')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(fixed)
    shutil.copymode(target, tmp)
    os.replace(tmp, target)
except BaseException:
    os.unlink(tmp)
    raise
print('patch-voice-tab: fixed ' + target + ' (backup: ' + backup + ')')
PY

case $rc in
0) ;;
3) exit 0 ;;
*) exit "$rc" ;;
esac

systemctl restart mesh-status.service
sleep 2
if systemctl is-active --quiet mesh-status.service; then
  echo "patch-voice-tab: web server restarted; reload the manage page"
else
  echo "patch-voice-tab: web server failed to start; restoring backup" >&2
  cp -p "$BACKUP" "$TARGET"
  systemctl restart mesh-status.service
  exit 1
fi
