#!/bin/bash
# Set this node's regulatory_domain in /etc/mesh.conf, then reconcile every
# radio config with manet-region.py (morse/cfg80211/crda/hostapd/s1g).
# Inert until the next boot. No reboot here.
# usage: region-apply.sh <CC>
set -u
CC=${1:?usage: region-apply.sh <CC>}
if ! [[ $CC =~ ^[A-Z]{2}$ ]]; then
    echo "region-apply: invalid country code '$CC' (want two uppercase letters)"
    exit 2
fi
if grep -q '^regulatory_domain=' /etc/mesh.conf; then
    sed -i "s/^regulatory_domain=.*/regulatory_domain=$CC/" /etc/mesh.conf
else
    echo "regulatory_domain=$CC" >> /etc/mesh.conf
fi
echo "regulatory_domain=$CC"
exec python3 /usr/local/bin/manet-region.py apply
