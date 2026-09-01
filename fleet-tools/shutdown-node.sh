#!/bin/bash
# ============================================================================
# shutdown-node.sh — interactive, safe node shutdown
#
# - Discovers live nodes (same as discover-fleet.sh)
# - Menu-select which one to shut down
# - Blinks that node's onboard LED for ~20s so you can physically confirm
#   which unit it is before committing (self-restoring — see below)
# - Requires TYPING the exact hostname to confirm (not just y/n) — cheap
#   insurance against fat-fingering the wrong menu number
# - Re-verifies hostname immediately before executing, in case the IP
#   drifted in the seconds you spent reading/typing the confirmation
# - Logs every action (attempted, cancelled, or completed) with a
#   timestamp to shutdown-actions.log next to this script
# - Wrong selection or mistaken run: cancel returns you to the menu,
#   nothing destructive happens until you've typed the hostname correctly
#
# LED identify design note: the remote blink command is entirely
# self-contained and time-bounded (~20s) — it restores the LED's original
# trigger on its own after that window regardless of what this script
# does locally. That means a dropped SSH connection or a Ctrl-C here
# can't leave a node's LED stuck in a blinking/off state.
# ============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib.sh"

SUBNET="${SUBNET:-10.30.2}"
LOG_FILE="$SCRIPT_DIR/shutdown-actions.log"
LED_BLINK_SECONDS="${LED_BLINK_SECONDS:-20}"

log_action() {
  echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') | $*" >>"$LOG_FILE"
}

# Fires a self-contained, self-restoring LED blink on the target node.
# Backgrounds and detaches remotely so this returns immediately — the
# blink runs independently of this script's own lifetime.
led_identify() {
  local ip="$1"
  local remote_cmd
  remote_cmd=$(
    cat <<EOF
LED=\$(ls -d /sys/class/leds/PWR /sys/class/leds/*act* /sys/class/leds/*ACT* /sys/class/leds/led0 2>/dev/null | head -1)
if [ -z "\$LED" ]; then exit 0; fi
ORIG=\$(grep -o '\[[a-z0-9_-]*\]' "\$LED/trigger" 2>/dev/null | tr -d '[]')
echo none > "\$LED/trigger" 2>/dev/null
END=\$(( \$(date +%s) + $LED_BLINK_SECONDS ))
while [ \$(date +%s) -lt \$END ]; do
  echo 1 > "\$LED/brightness" 2>/dev/null; sleep 0.15
  echo 0 > "\$LED/brightness" 2>/dev/null; sleep 0.15
done
echo "\${ORIG:-mmc0}" > "\$LED/trigger" 2>/dev/null
EOF
  )
  { printf '%s\n' "$SUDO_PASS"; } |
    ssh $SSH_OPTS "$SSH_USER@$ip" \
      "sudo -S bash -c 'nohup bash -c \"$remote_cmd\" >/tmp/led-identify.log 2>&1 & disown; exit'" \
      >/dev/null 2>&1
}

confirm_and_shutdown() {
  local name="$1" ip="$2"

  echo
  verify_hostname "$ip" "$name" || {
    fail "aborting selection — hostname no longer matches, IP may have drifted"
    log_action "ABORTED (pre-check mismatch) name=$name ip=$ip"
    return
  }

  log "starting LED identify blink on $name (~${LED_BLINK_SECONDS}s)..."
  led_identify "$ip"
  ok "LED should be blinking on the physical unit now — go check which one lights up"
  echo

  read -r -p "Type the hostname '$name' exactly to confirm SHUTDOWN, or press Enter to cancel: " typed

  if [[ "$typed" != "$name" ]]; then
    warn "cancelled — returning to node selection"
    log_action "CANCELLED name=$name ip=$ip typed='$typed'"
    return
  fi

  # Re-verify immediately before executing — defense against drift during
  # the time spent reading/typing the confirmation.
  verify_hostname "$ip" "$name" || {
    fail "aborting: hostname no longer matches at execution time — nothing was shut down"
    log_action "ABORTED (post-check mismatch) name=$name ip=$ip"
    return
  }

  ssh_sudo "$ip" "shutdown -h now" >/dev/null 2>&1
  ok "shutdown command sent to $name ($ip)"
  log_action "SHUTDOWN name=$name ip=$ip"
}

main_menu() {
  echo "=== Discovering nodes on ${SUBNET}.0/24 ==="
  discover_fleet "$SUBNET"

  if [[ ${#HOSTNAME_TO_IP[@]} -eq 0 ]]; then
    echo "No nodes discovered."
    exit 1
  fi
  generate_ssh_config >/dev/null 2>&1

  # Stable, sorted ordering so menu numbers don't shuffle between runs.
  mapfile -t names < <(printf '%s\n' "${!HOSTNAME_TO_IP[@]}" | sort)

  echo
  echo "=== Select a node to shut down ==="
  local i=1
  for n in "${names[@]}"; do
    printf "  %d) %-14s %s\n" "$i" "$n" "${HOSTNAME_TO_IP[$n]}"
    i=$((i + 1))
  done
  echo "  q) Quit"
  echo

  read -r -p "Choice: " choice

  if [[ "$choice" == "q" || "$choice" == "Q" ]]; then
    echo "Bye."
    exit 0
  fi

  if ! [[ "$choice" =~ ^[0-9]+$ ]] || ((choice < 1 || choice > ${#names[@]})); then
    warn "invalid choice"
    main_menu
    return
  fi

  local selected="${names[$((choice - 1))]}"
  confirm_and_shutdown "$selected" "${HOSTNAME_TO_IP[$selected]}"

  echo
  read -r -p "Press Enter to return to the menu, or 'q' to quit: " again
  [[ "$again" == "q" || "$again" == "Q" ]] && exit 0
  main_menu
}

main_menu
