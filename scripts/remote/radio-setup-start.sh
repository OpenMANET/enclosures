#!/bin/bash
# Runs ON a node as root. Reruns upstream radio-setup.sh (the documented
# retry) detached, so it survives the SSH session dropping when it restarts
# networking. The caller polls radio-setup-done.sh.
setsid nohup /usr/local/bin/radio-setup.sh >/dev/null 2>&1 </dev/null &
echo "radio-setup started"
