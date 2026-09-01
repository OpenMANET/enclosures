#!/bin/bash
# ============================================================================
# manet-enforce-radio-policy.sh — enforce the lab radio-role policy on a node.
#
# Policy (all nodes, present and future):
#   * MESH  = USB MM8108 HaLow (morse_usb) + the 2.4 GHz interface of the MT7916
#   * 5 GHz EUD user AP = the 5 GHz interface of the MT7916
#   * Any OTHER Wi-Fi (e.g. the onboard CM4 brcmfmac) is NOT used — blacklisted.
#
# Idempotent: on a node that already complies (e.g. mesh-5ab8, no onboard radio,
# AP already on the MT7916 5 GHz iface) it does nothing. Radios are matched by
# driver + band, not by fixed wlanN, so it is robust to naming and to future
# hardware. Persistent: hostapd.conf / ap_interface / modprobe blacklist all
# survive reboot. Run once per node (e.g. via 40-enforce-radio-policy.sh or
# setup-node.sh); no resident component.
# ============================================================================
set -u
log() { logger -t manet-radio-policy "$*"; echo "radio-policy: $*"; }

is_5g() {
    # A radio is the 5 GHz one if its phy lists any 5 GHz channel. iw prints
    # frequencies as e.g. "5180.0 MHz" (note the decimal), so match that shape.
    local phy; phy=$(basename "$(readlink -f "/sys/class/net/$1/phy80211" 2>/dev/null)" 2>/dev/null)
    iw phy "$phy" info 2>/dev/null | grep -qE "5[0-9]{3}\.[0-9]+ MHz"
}

morse="" mt24="" mt5="" others=""
for d in /sys/class/net/wlan*; do
    [ -e "$d" ] || continue
    n=$(basename "$d")
    drv=$(basename "$(readlink -f "$d/device/driver" 2>/dev/null)" 2>/dev/null)
    case "$drv" in
        morse_usb) morse="$n" ;;
        mt7915e|mt7921e|mt7996e) if is_5g "$n"; then mt5="$n"; else mt24="$n"; fi ;;
        *) others="$others $n:$drv" ;;   # brcmfmac and anything else
    esac
done
log "detected: morse=${morse:-none} mt7916-2.4=${mt24:-none} mt7916-5G=${mt5:-none} other=[${others:- none}]"

# --- 1. 5 GHz AP must be the MT7916 5 GHz interface -------------------------
if [ -n "$mt5" ]; then
    cur_ap=$(cat /var/lib/ap_interface 2>/dev/null)
    if [ "$cur_ap" != "$mt5" ] || ! grep -q "^interface=$mt5\$" /etc/hostapd/hostapd.conf 2>/dev/null; then
        log "moving 5 GHz AP -> $mt5 (was ${cur_ap:-unset})"
        systemctl stop hostapd 2>/dev/null
        sed -i "s/^interface=.*/interface=$mt5/" /etc/hostapd/hostapd.conf
        echo "$mt5" > /var/lib/ap_interface
        # AP radio must never be meshed
        systemctl mask "wpa_supplicant@$mt5.service" >/dev/null 2>&1
        for f in "/etc/wpa_supplicant/wpa_supplicant-$mt5.conf" \
                 "/etc/wpa_supplicant/wpa_supplicant-$mt5-lobby.conf"; do
            [ -f "$f" ] && mv -f "$f" "$f.disabled-lab"
        done
        [ -f /var/lib/mesh_5_if ] && sed -i "/^$mt5\$/d" /var/lib/mesh_5_if
        [ -f /var/lib/mesh_if ]   && sed -i "/^$mt5\$/d" /var/lib/mesh_if
        systemctl start hostapd 2>/dev/null
        sleep 3
        log "hostapd on $mt5: $(systemctl is-active hostapd)"
    else
        log "AP already on $mt5 — ok"
    fi
else
    log "WARNING: no 5 GHz MT7916 interface found — cannot place AP per policy"
fi

# --- 2. Disable any other Wi-Fi (onboard CM4 brcmfmac, etc.) ----------------
if [ -n "$others" ]; then
    cat > /etc/modprobe.d/manet-blacklist-onboard-wifi.conf <<'EOF'
# MANET policy: do NOT use the onboard CM4 Wi-Fi (or any non-MT7916/-morse radio).
# Mesh = MT7916 2.4 GHz + USB MM8108 HaLow; 5 GHz AP = MT7916. Onboard is unused.
blacklist brcmfmac
blacklist brcmfmac_cyw
EOF
    for entry in $others; do
        n="${entry%%:*}"
        ip link set "$n" down 2>/dev/null
        log "disabled other radio $entry (down now; module blacklisted -> gone after reboot)"
    done
else
    log "no other/onboard Wi-Fi present — ok"
fi
log "done"
