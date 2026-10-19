#!/bin/bash
# Fleet manager for MANET lab nodes. Interactive menu that dispatches
# to action scripts under actions/.
#
# usage: fleet-status.sh
set -u
source "$(dirname "$0")/lib/common.sh"

menu() {
    while :; do
        echo
        print "$C_DIM" "================================================================"
        print "$C_HEAD" "  MANET fleet manager"
        print "$C_DIM" "================================================================"
        cat <<'EOF'

  1. Node status check
  2. Update nodes
  3. Toggle auto-update (y/n)
  4. Mic/audio gain check & set
  5. Region swap (EU/US)
  6. Deploy patched mesh-voice
  q. Quit

EOF
        ask "  > "
        echo
        case $ANSWER in
        1) bash "$REPO_ROOT/actions/status.sh" ;;
        2) bash "$REPO_ROOT/actions/update.sh" ;;
        3) bash "$REPO_ROOT/actions/auto-update.sh" ;;
        4) bash "$REPO_ROOT/actions/mic-gain.sh" ;;
        5) bash "$REPO_ROOT/actions/region.sh" ;;
        6) bash "$REPO_ROOT/actions/deploy-voice.sh" ;;
        q | Q) break ;;
        '') continue ;;
        *) echo "  unknown choice" ;;
        esac
    done
}

menu
