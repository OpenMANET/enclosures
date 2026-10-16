#!/bin/bash
# Runs ON a node as root (via scripts/fleet-status.sh). Read-only.
# Emits one fact per line: "TAG key=value ...", values contain no spaces.
set -u

SERVICES="mesh-voice mesh-status node-manager batman-enslave hostapd wpa_supplicant@wlan0 wpa_supplicant-s1g-wlan2"

host=$(hostname)
up_s=$(awk '{printf "%d", $1}' /proc/uptime)
ver=$(head -1 /etc/manet_version.txt 2>/dev/null)
au=$(awk -F= '/^auto_update=/{print $2}' /etc/mesh.conf)
echo "NODE host=$host up_s=$up_s ver=${ver:-?} au=${au:-?}"

for u in $(systemctl --failed --no-legend 2>/dev/null | awk '{print $2}'); do
    echo "FAILED $u"
done

for s in $SERVICES; do
    echo "SVC $s=$(systemctl is-active "$s" 2>/dev/null)"
done

# Interface roles: driver, type (lowercase), channel, ssid, in_bat0.
batifs=$(batctl meshif bat0 if 2>/dev/null)
for n in wlan0 wlan1 wlan2; do
    if [ ! -e "/sys/class/net/$n" ]; then
        echo "IF $n missing"
        continue
    fi
    drv=$(basename "$(readlink "/sys/class/net/$n/device/driver" 2>/dev/null)" 2>/dev/null)
    info=$(iw dev "$n" info 2>/dev/null)
    type=$(awk '/type /{print $2; exit}' <<<"$info" | tr '[:upper:]' '[:lower:]')
    ch=$(awk '/channel /{print $2; exit}' <<<"$info")
    ssid=$(awk '$1 == "ssid"{print $2; exit}' <<<"$info")
    inbat=$(grep -c "^$n:" <<<"$batifs")
    echo "IF $n drv=${drv:-?} type=${type:-?} ch=${ch:-0} ssid=${ssid:-none} in_bat0=$inbat"
done
echo "AP if=$(cat /var/lib/ap_interface 2>/dev/null || echo none)"

freq=$(morse_cli -i wlan2 channel 2>&1 | grep -oE 'Operating Frequency: [0-9]+' | awk '{print $3}')
estab=$(iw dev wlan2 station dump 2>/dev/null | grep -c 'plink:.*ESTAB')
usb=$(journalctl -k --since -5min --no-pager 2>/dev/null | grep -cE 'rc -19|error -110|errno=-71')
echo "HALOW freq_khz=${freq:-0} peers=$estab usb_errors_5min=$usb"

nb=$(batctl meshif bat0 n -H 2>/dev/null)
echo "BAT n24=$(grep -c wlan0 <<<"$nb") nhalow=$(grep -c wlan2 <<<"$nb")"

python3 - <<'PY'
import json
try:
    d = json.load(open("/run/mesh-voice.json"))
except OSError:
    print("VOICE missing")
    raise SystemExit
print("VOICE ptt=%s blocked=%s" % (
    str(bool(d.get("ptt_connected"))).lower(),
    str(bool(d.get("tx_blocked"))).lower()))
PY

if lsusb 2>/dev/null | grep -q 'u-blox'; then
    fix=$(grep -oE '"has_fix": ?(true|false)' /run/gps_status.json 2>/dev/null | grep -oE 'true|false')
    echo "GPS present active=$(systemctl is-active gpsd 2>/dev/null) fix=${fix:-unknown}"
else
    echo "GPS none"
fi

line=$(grep -E 'USB Audio|OpenVLM' /proc/asound/cards | head -1 || true)
if [ -n "$line" ]; then
    idx=$(awk '{print $1}' <<<"$line")
    val=$(amixer -c "$idx" get Mic 2>/dev/null | grep -oE 'Mono: Capture [0-9]+' | head -1 | awk '{print $3}')
    echo "MIC idx=$idx val=${val:-?}"
else
    echo "MIC none"
fi
