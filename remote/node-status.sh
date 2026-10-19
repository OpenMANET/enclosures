#!/bin/bash
# Runs ON a node as root. Read-only health probe.
# Emits one fact per line: "TAG key=value ...", no spaces in values.
set -u

# --- core services to check -------------------------------------------------

SERVICES="mesh-voice mesh-status node-manager batman-enslave hostapd"
SERVICES="$SERVICES wpa_supplicant@wlan0 wpa_supplicant-s1g-wlan2"

# systemd-udev-settle.service always times out on CM4; ignore it.
HARMLESS_FAILED="systemd-udev-settle.service"

# --- node identity ----------------------------------------------------------

host=$(hostname)
up_s=$(awk '{printf "%d", $1}' /proc/uptime)
ver=$(head -1 /etc/manet_version.txt 2>/dev/null)
au=$(awk -F= '/^auto_update=/{print $2}' /etc/mesh.conf)
echo "NODE host=$host up_s=$up_s ver=${ver:-?} au=${au:-?}"

# --- failed systemd units ---------------------------------------------------

for u in $(systemctl --failed --no-legend 2>/dev/null | awk '{print $2}'); do
    [ "$u" = "$HARMLESS_FAILED" ] && continue
    echo "FAILED $u"
done

# --- service status ---------------------------------------------------------

for s in $SERVICES; do
    echo "SVC $s=$(systemctl is-active "$s" 2>/dev/null)"
done

# --- wireless interfaces: driver, type, channel, ssid, bat0 membership ------

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

# --- HaLow status -----------------------------------------------------------

freq=$(morse_cli -i wlan2 channel 2>&1 \
    | grep -oE 'Operating Frequency: [0-9]+' | awk '{print $3}')
estab=$(iw dev wlan2 station dump 2>/dev/null | grep -c 'plink:.*ESTAB')
usb=$(journalctl -k --since -5min --no-pager 2>/dev/null \
    | grep -cE 'rc -19|error -110|errno=-71')
echo "HALOW freq_khz=${freq:-0} peers=$estab usb_errors_5min=$usb"

# --- batman neighbours ------------------------------------------------------

nb=$(batctl meshif bat0 n -H 2>/dev/null)
echo "BAT n24=$(grep -c wlan0 <<<"$nb") nhalow=$(grep -c wlan2 <<<"$nb")"

# --- voice ------------------------------------------------------------------

python3 - <<'PY'
import json, sys
try:
    d = json.load(open("/run/mesh-voice.json"))
except OSError:
    print("VOICE missing")
    sys.exit()
print("VOICE ptt=%s blocked=%s" % (
    str(bool(d.get("ptt_connected"))).lower(),
    str(bool(d.get("tx_blocked"))).lower()))
PY

# --- GPS --------------------------------------------------------------------

if lsusb 2>/dev/null | grep -q 'u-blox'; then
    fix=$(grep -oE '"has_fix": ?(true|false)' /run/gps_status.json 2>/dev/null \
        | grep -oE 'true|false')
    echo "GPS present active=$(systemctl is-active gpsd 2>/dev/null) fix=${fix:-unknown}"
else
    echo "GPS none"
fi

# --- mic level --------------------------------------------------------------

line=$(grep -E 'USB Audio|OpenVLM' /proc/asound/cards 2>/dev/null | head -1)
if [ -n "$line" ]; then
    idx=$(awk '{print $1}' <<<"$line")
    val=$(amixer -c "$idx" get Mic 2>/dev/null \
        | grep -oE 'Mono: Capture [0-9]+' | head -1 | awk '{print $3}')
    pcm=$(amixer -c "$idx" get PCM 2>/dev/null \
        | grep -E 'Front Left|Mono:' | grep -oE 'Playback [0-9]+' \
        | head -1 | awk '{print $2}')
    agc=$(amixer -c "$idx" get "Auto Gain Control" 2>/dev/null \
        | grep -oE '\[(on|off)\]' | tr -d '[]')
    echo "MIC idx=$idx val=${val:-?} pcm=${pcm:-?} agc=${agc:-?}"
else
    echo "MIC none"
fi
