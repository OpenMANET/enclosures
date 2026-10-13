#!/bin/bash
# ============================================================================
# 10-deploy-halow-1mhz.sh <ip> <expected-hostname>
#
# Brings up 868 MHz HaLow as a batman-adv mesh interface (wlan2), tuned for
# maximum range on EU-region MM8108 silicon:
#   * EU regulatory, 1 MHz channel 9 (867.5 MHz, op_class 66). NOTE: channel 1
#     (863.5 MHz) is DISABLED by the morse EU self-managed reg domain — only
#     867-868 @ 1 MHz is permitted (plus 2 MHz ch2/ch6 in 863-867). Configuring
#     the disabled 863.5 channel is a silent mesh failure ("Frequency ... not
#     allowed for AP mode"). 1 MHz gives the best range/sensitivity.
#     (A US 8 MHz config, op_class 71 ch12/908 MHz, over EU firmware is the same
#      failure mode — that mismatch is what took the whole fleet's 868 down.)
#   * duty-cycle disabled, MPSW off. TX power: the morse firmware HARD-clamps
#     868 to 15 dBm (ETSI 25 mW e.r.p. on 863-868) regardless of
#     tx_max_power_mbm / iw txpower / reg 00 — none of those move it above 15.
#     Lifting the 15 dBm ceiling needs a modified BCF or a dot11ah rebuild.
#     mesh_plink_timeout=0 (enforced by the stock
#     batman-enslave-watch, which also auto-enslaves wlan2 into bat0).
#   * Writes 10-wlan2.link (morse MAC -> wlan2) so a USB-HaLow-at-boot
#     enumeration race can't rename the radios (fixes the wlanX swap seen
#     when the image was first flashed with USB disabled).
#
# Requires: USB host already enabled (run 00-fix-usb-host.sh first) so wlan2
# (morse_usb) exists. Reads mesh_ssid / mesh_key from the node's /etc/mesh.conf.
# ============================================================================
set -uo pipefail
SD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; source "$SD/lib.sh"
IP="${1:?usage: $0 <ip> <hostname>}"; HOST="${2:?usage: $0 <ip> <hostname>}"

verify_hostname "$IP" "$HOST" || exit 1

# Find the morse (HaLow) interface + its MAC, regardless of current wlanX name.
read -r HIF HMAC < <(ssh_rootp "$IP" '
  for i in /sys/class/net/wlan*; do n=$(basename $i);
    d=$(basename $(readlink -f $i/device/driver) 2>/dev/null);
    [ "$d" = morse_usb ] && { echo "$n $(cat $i/address)"; break; }; done')
[[ -n "${HMAC:-}" ]] || { fail "no morse_usb interface found (USB host enabled? run 00-fix-usb-host.sh)"; exit 1; }
ok "morse HaLow radio: $HIF ($HMAC)"

SSID=$(ssh_root  "$IP" "grep '^mesh_ssid=' /etc/mesh.conf | cut -d= -f2" | tr -d '\r')
KEY=$(ssh_root   "$IP" "grep '^mesh_key='  /etc/mesh.conf | cut -d= -f2" | tr -d '\r')
[[ -n "$SSID" && -n "$KEY" ]] || { fail "could not read mesh_ssid/mesh_key"; exit 1; }
log "mesh ssid=$SSID"

# 1. Deterministic name: morse MAC -> wlan2. If the morse radio isn't already
#    wlan2 (a USB-at-boot enumeration race can put it on wlan0), write the
#    .link and reboot so udev renames it deterministically before we configure.
put_file "$IP" /etc/systemd/network/10-wlan2.link \
"[Match]
MACAddress=$HMAC
Type=wlan

[Link]
Name=wlan2"
if [[ "$HIF" != "wlan2" ]]; then
  warn "morse is $HIF, not wlan2 — rebooting to apply deterministic naming ..."
  ssh_root "$IP" "reboot" >/dev/null 2>&1 || true
  wait_online "$IP" || exit 1
  verify_hostname "$IP" "$HOST" || exit 1
  read -r HIF HMAC < <(ssh_rootp "$IP" '
    for i in /sys/class/net/wlan*; do n=$(basename $i);
      d=$(basename $(readlink -f $i/device/driver) 2>/dev/null);
      [ "$d" = morse_usb ] && { echo "$n $(cat $i/address)"; break; }; done')
  [[ "$HIF" == "wlan2" ]] || { fail "morse still $HIF after reboot (expected wlan2)"; exit 1; }
  ok "morse HaLow radio is now wlan2 ($HMAC)"
fi

# 2. s1g mesh supplicant config (EU, 1 MHz, ch1). sae_pwe=1 (H2E) matches the
#    stock fleet; identical config across nodes is required for SAE to peer.
put_file "$IP" /etc/wpa_supplicant/wpa_supplicant-wlan2-s1g.conf \
"country=\"EU\"
ctrl_interface=/var/run/wpa_supplicant_s1g
sae_pwe=1
max_peer_links=10
mesh_fwding=0
network={
    ssid=\"$SSID\"
    key_mgmt=SAE
    mode=5
    channel=9
    op_class=66
    country=\"EU\"
    s1g_prim_chwidth=0
    s1g_prim_1mhz_chan_index=0
    dtim_period=1
    mesh_rssi_threshold=-85
    dot11MeshHWMPRootMode=0
    dot11MeshGateAnnouncements=0
    mbca_config=1
    mbca_min_beacon_gap_ms=25
    mbca_tbtt_adj_interval_sec=60
    dot11MeshBeaconTimingReportInterval=10
    mbss_start_scan_duration_ms=2048
    mesh_beaconless_mode=0
    mesh_dynamic_peering=0
    sae_password=\"$KEY\"
    pairwise=CCMP
    ieee80211w=2
    beacon_int=1000
    group_rekey=0
}" 600

# 3. networkd MTU (batman needs 1532)
put_file "$IP" /etc/systemd/network/30-wlan2.network \
"[Match]
MACAddress=$HMAC

[Network]

[Link]
RequiredForOnline=no
MTUBytes=1532"

# 4. supplicant service
put_file "$IP" /etc/systemd/system/wpa_supplicant-s1g-wlan2.service \
"[Unit]
Description=WPA supplicant (S1G/HaLow) for wlan2
After=sys-subsystem-net-devices-wlan2.device
Requires=sys-subsystem-net-devices-wlan2.device

[Service]
Type=simple
ExecStartPre=/usr/local/bin/unblock-wifi-rfkill.sh
ExecStartPre=/bin/sleep 3
ExecStart=/usr/sbin/wpa_supplicant_s1g -c /etc/wpa_supplicant/wpa_supplicant-wlan2-s1g.conf -i wlan2 -D nl80211
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target"

# 5. tx power + lab optimisations (duty-cycle off, MPSW off)
put_file "$IP" /etc/systemd/system/halow-txpower-wlan2.service \
"[Unit]
Description=HaLow wlan2: max TX power + disable duty-cycle/MPSW (lab)
After=wpa_supplicant-s1g-wlan2.service
Wants=wpa_supplicant-s1g-wlan2.service

[Service]
Type=oneshot
ExecStartPre=/bin/sleep 5
ExecStart=/usr/sbin/iw dev wlan2 set txpower fixed 2400
ExecStartPost=-/usr/local/bin/morse_cli -i wlan2 duty_cycle disable
ExecStartPost=-/usr/local/bin/morse_cli -i wlan2 mpsw -e 0
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target"

# 6. register with batman-enslave-watch + morse EU throttles-off (persist)
ssh_root "$IP" "grep -qx wlan2 /var/lib/halow_if 2>/dev/null || echo wlan2 >> /var/lib/halow_if"
ssh_root "$IP" "grep -q enable_auto_duty_cycle /etc/modprobe.d/morse.conf || echo options morse enable_auto_duty_cycle=0 enable_auto_mpsw=0 >> /etc/modprobe.d/morse.conf"

# 7. (re)start
ssh_root "$IP" "systemctl daemon-reload; systemctl enable wpa_supplicant-s1g-wlan2.service halow-txpower-wlan2.service; systemctl restart wpa_supplicant-s1g-wlan2.service; sleep 6; systemctl restart halow-txpower-wlan2.service"

sleep 8
echo "  --- HaLow status ---"
ssh_rootp "$IP" '
  echo -n "  wlan2 type: "; iw dev wlan2 info | awk "/type/{print \$2}";
  echo -n "  channel:    "; morse_cli -i wlan2 channel -j | tr -d "\n ";echo;
  echo -n "  txpower:    "; iw dev wlan2 info | awk "/txpower/{print \$2,\$3}";
  echo -n "  in bat0:    "; batctl if 2>/dev/null | grep -q "^wlan2:" && echo yes || echo no;
  echo    "  peers:      $(iw dev wlan2 station dump | awk "/mesh plink/{print \$3}" | tr "\n" " ")"'
