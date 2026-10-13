#!/bin/bash
# Runs ON a node as root (via scripts/fleet-check.sh). Read-only.
# One block per node: services, radio roles, HaLow, mesh neighbours, voice.

SERVICES="mesh-voice mesh-status node-manager batman-enslave hostapd wpa_supplicant@wlan0 wpa_supplicant-s1g-wlan2"
USB_ERRORS='rc -19|error -110|errno=-71'

echo "==== $(hostname) ver=$(head -1 /etc/manet_version.txt) up=$(uptime -p | sed 's/up //') $(grep ^auto_update= /etc/mesh.conf)"
echo "failed units: [$(systemctl --failed --no-legend | awk '{print $2}' | tr '\n' ' ')]"

# Services
for s in $SERVICES; do
    printf '%s=%s ' "$s" "$(systemctl is-active "$s")"
done
echo

# Radio roles. Policy: MT7916 2.4 + HaLow in bat0, MT7916 5 GHz = AP.
echo "brcmfmac_loaded=$(lsmod | grep -c '^brcmfmac') ap=$(cat /var/lib/ap_interface) bat0=[$(batctl meshif bat0 if | tr '\n' ' ')]"
for n in wlan0 wlan1 wlan2; do
    drv=$(basename "$(readlink /sys/class/net/$n/device/driver)" 2>/dev/null)
    info=$(iw dev "$n" info 2>/dev/null | awk '/type/{t=$2}/channel/{c=$3}/ssid/{s=$2}END{print t, c, s}')
    printf '%s %s %s | ' "$n" "$drv" "$info"
done
echo

# HaLow: real S1G frequency, established peers, USB link errors.
freq=$(morse_cli -i wlan2 channel 2>&1 | grep -oE 'Operating Frequency: [0-9]+ kHz|failed.*')
estab=$(iw dev wlan2 station dump 2>/dev/null | grep -c 'plink:.*ESTAB')
usb=$(journalctl -k --since -5min --no-pager | grep -cE "$USB_ERRORS")
echo "halow: ${freq:-unknown} peers_established=$estab usb_errors_5min=$usb"

# batman-adv neighbours per radio.
n=$(batctl meshif bat0 n -H 2>/dev/null)
echo "neighbours: 2.4=$(grep -c wlan0 <<<"$n") halow=$(grep -c wlan2 <<<"$n")"

# Voice daemon state.
python3 - <<'PY'
import json
try:
    d = json.load(open("/run/mesh-voice.json"))
except OSError:
    print("voice: no state file")
    raise SystemExit
keys = ["tx", "tx_blocked", "playback_level_pct", "rx_gain_db", "ptt_connected", "stalls", "talker_names"]
print("voice:", {k: d.get(k) for k in keys})
PY
