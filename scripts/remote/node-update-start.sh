#!/bin/bash
# Runs ON a node as root. Starts upstream node-update.sh detached: it restarts
# mesh services, which can drop the SSH session that launched it.
# Output goes to /tmp/node-update.log, ending with UPDATER_EXIT=<code>.
LOG=/tmp/node-update.log
rm -f "$LOG"
setsid nohup bash -c "node-update.sh; echo UPDATER_EXIT=\$?" >"$LOG" 2>&1 </dev/null &
echo "node-update started"
