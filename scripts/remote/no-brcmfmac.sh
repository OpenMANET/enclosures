#!/bin/bash
# Runs ON a node as root. Blacklists the onboard brcmfmac radio.
# Upstream radio-setup.sh gives a non-mesh radio (brcmfmac) the EUD AP, which
# pushes the MT7916 5 GHz radio into the mesh. Without brcmfmac it picks the
# MT7916 5 GHz radio for the AP and meshes 2.4 GHz with HaLow.
# Prints "reboot" when brcmfmac is loaded and a reboot is needed.
set -eu

CONF=/etc/modprobe.d/manet-lab-no-brcmfmac.conf

printf '%s\n' "# manet-lab-fleet-ops: only the MT7916 serves 2.4/5 GHz; keep the onboard radio off." \
    "blacklist brcmfmac" "blacklist brcmfmac_wcc" "blacklist brcmutil" > "$CONF"

if lsmod | grep -q '^brcmfmac'; then
    echo reboot
fi
