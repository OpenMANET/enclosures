#!/bin/bash
# Runs ON a node as root. Read-only: current region configuration.
# Prints: regulatory_domain halow_regulatory_domain halow_freq_khz
set -u

reg=$(grep '^regulatory_domain=' /etc/mesh.conf 2>/dev/null | cut -d= -f2)
hal=$(grep '^halow_regulatory_domain=' /etc/mesh.conf 2>/dev/null | cut -d= -f2)
freq=$(morse_cli -i wlan2 channel 2>/dev/null \
    | grep -oE 'Operating Frequency: [0-9]+' | grep -oE '[0-9]+')
echo "${reg:-?} ${hal:-?} ${freq:-?}"
