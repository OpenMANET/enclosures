#!/usr/bin/env python3
"""
fix_voice_tab.py — idempotent patch for the mesh-status VOICE tab bug.

Bug: in manet_manage.py's render_dashboard() template, the VOICE tab's
<div id="tab-voice"> was nested INSIDE the still-open <div id="tab-config">
(missing a closing </div> before it), so tab-voice was always hidden
whenever tab-config wasn't the active tab — i.e. always, since JS
correctly toggles display on #tab-voice itself but a child of a
display:none parent is never visible regardless of its own display value.

This script finds the exact broken block and rewrites it as two properly
separated sibling <div> panes. Safe to re-run:
  - If the broken pattern is found, patches it and exits 0.
  - If the CORRECT pattern is already present, does nothing and exits 0.
  - If neither pattern matches (file structure has changed upstream),
    makes no changes and exits 1 — so a fleet script calling this can
    tell "already fine" apart from "couldn't find what I expected".
"""

import sys

TARGET = "/usr/local/bin/manet_manage.py"

BROKEN = '''  <div id="tab-config" class="tab-pane" style="display:none">
<!-- \u2500\u2500 VOICE \u2500\u2500 -->
  <div id="tab-voice" class="tab-pane" style="display:none">{VOICE_TAB_HTML}</div>

{CONFIG_TAB_HTML}
  </div>
'''

FIXED = '''  <div id="tab-config" class="tab-pane" style="display:none">
{CONFIG_TAB_HTML}
  </div>

<!-- \u2500\u2500 VOICE \u2500\u2500 -->
  <div id="tab-voice" class="tab-pane" style="display:none">{VOICE_TAB_HTML}</div>
'''


def main():
    try:
        with open(TARGET, encoding='utf-8') as f:
            src = f.read()
    except OSError as e:
        print(f"Could not read {TARGET}: {e}")
        return 1

    if FIXED in src:
        print("Already patched — no changes needed.")
        return 0

    if BROKEN not in src:
        print("Broken pattern not found — file structure may have changed "
              "upstream. Not touching anything; check manually.")
        return 1

    patched = src.replace(BROKEN, FIXED, 1)

    backup = TARGET + '.bak'
    try:
        with open(backup, 'w', encoding='utf-8') as f:
            f.write(src)
    except OSError as e:
        print(f"Could not write backup {backup}: {e}")
        return 1

    try:
        with open(TARGET, 'w', encoding='utf-8') as f:
            f.write(patched)
    except OSError as e:
        print(f"Could not write patched file: {e}")
        return 1

    print(f"Patched {TARGET} (backup at {backup}).")
    return 0


if __name__ == '__main__':
    sys.exit(main())
