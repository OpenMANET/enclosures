#!/bin/bash
# Runs ON a node as root. Prints "running" while radio-setup.sh runs, else the
# resulting roles. Matches the interpreter line, not "radio-setup" anywhere,
# so this script's own command line does not count.
if pgrep -f '^/bin/bash /usr/local/bin/radio-setup.sh' >/dev/null; then
    echo running
    exit 0
fi
echo "done ap=$(cat /var/lib/ap_interface) mesh_if=$(tr '\n' ' ' </var/lib/mesh_if)"
