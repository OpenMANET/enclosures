#!/bin/bash
# ============================================================================
# 20-open-24-5ghz.sh <ip> <expected-hostname>
#
# Opens the 2.4/5 GHz (MT7916) side to the unrestricted world domain for the
# lab, and maxes TX power. 5 GHz stays the EUD AP; 2.4 GHz + 868 stay meshed.
#
#   * Installs srs-manet's regulatory_open00.db as /lib/firmware/regulatory.db
#     (backs up the stock one). Its "00" domain is fully open: 2.4/5/6 GHz at
#     30 dBm, no DFS, no NO-IR — so AP/mesh can radiate on any channel.
#   * Removes every "PL" hint that otherwise reverts the domain on boot:
#     /etc/default/crda, the cfg80211 modprobe option, mesh.conf, and the
#     wpa_supplicant wlan0/wlan1 (+ -lobby) country lines -> 00.
#   * hostapd can't run with country_code=00, so we drop country_code and set
#     ieee80211d=0; hostapd then uses the open phy regulatory directly.
#   * Installs manet-regdom-open.service: an authoritative `iw reg set 00` late
#     in boot (belt-and-suspenders against anything re-hinting a country).
#   * Raises the AP + mesh TX power services to 30 dBm (chip clamps 2.4 to its
#     ~23 dBm ceiling; 5 GHz reaches 30). NOTE: high power on a close bench can
#     desense receivers — it's the right call for a range test, not co-location.
#
# The morse HaLow phy is self-managed and unaffected (stays EU 868).
# ============================================================================
set -uo pipefail
SD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; source "$SD/lib.sh"
IP="${1:?usage: $0 <ip> <hostname>}"; HOST="${2:?usage: $0 <ip> <hostname>}"
DB="$SD/regulatory_open00.db"
[[ -f "$DB" ]] || { fail "missing $DB"; exit 1; }

verify_hostname "$IP" "$HOST" || exit 1

# 1. open regdb (backup stock once)
log "installing open00 regulatory.db ..."
ssh_root "$IP" "test -f /lib/firmware/regulatory.db.stock-bak || cp /lib/firmware/regulatory.db /lib/firmware/regulatory.db.stock-bak"
put_scp "$DB" "$IP" /lib/firmware/regulatory.db 644

# 2. remove all PL hints -> 00, persistently
log "setting regulatory domain 00 everywhere ..."
ssh_root "$IP" '
  echo REGDOMAIN=00 > /etc/default/crda
  echo "options cfg80211 ieee80211_regdom=00" > /etc/modprobe.d/cfg80211.conf
  sed -i "s/^regulatory_domain=.*/regulatory_domain=00/" /etc/mesh.conf
  for f in /etc/wpa_supplicant/wpa_supplicant-wlan0*.conf /etc/wpa_supplicant/wpa_supplicant-wlan1*.conf; do
    [ -f "$f" ] && sed -i "s/^country=.*/country=00/" "$f"; done
  sed -i "/^country_code=/d; s/^ieee80211d=1/ieee80211d=0/" /etc/hostapd/hostapd.conf
'

# 3. authoritative boot-time reg set
put_file "$IP" /etc/systemd/system/manet-regdom-open.service \
"[Unit]
Description=Force open regulatory domain 00 (lab)
After=network-online.target hostapd.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStartPre=/bin/sleep 8
ExecStart=/usr/sbin/iw reg set 00
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target"

# 4. max TX power (5 GHz AP -> 30, 2.4 mesh -> 30 (clamps to ~23))
log "raising TX power services to max ..."
ssh_root "$IP" '
  sed -i "s/set txpower fixed 500/set txpower fixed 3000/" /etc/systemd/system/ap-txpower.service 2>/dev/null || true
  sed -i "s/wlan0 set txpower fixed 2300/wlan0 set txpower fixed 3000/; s/wlan1 set txpower fixed 2400/wlan1 set txpower fixed 3000/" /etc/systemd/system/manet-txpower.service 2>/dev/null || true
'

# 5. apply live
log "applying live (reg 00 + restart AP/mesh/txpower) ..."
ssh_rootp "$IP" '
  systemctl daemon-reload
  systemctl enable manet-regdom-open.service >/dev/null 2>&1
  iw reg reload 2>/dev/null; sleep 1; iw reg set 00; sleep 1
  systemctl restart hostapd.service 2>/dev/null || true
  systemctl restart wpa_supplicant@wlan0.service 2>/dev/null || true
  systemctl restart manet-txpower.service 2>/dev/null || true
  sleep 2; systemctl restart ap-txpower.service 2>/dev/null || true
  sleep 3
'

echo "  --- open 2.4/5 status ---"
ssh_rootp "$IP" '
  echo    "  reg: $(iw reg get | grep -m1 country)";
  ap=$(cat /var/lib/ap_interface 2>/dev/null);
  echo -n "  AP ($ap): "; systemctl is-active hostapd;
  echo    "  AP txpower: $(iw dev $ap info 2>/dev/null | awk "/txpower/{print \$2,\$3}")";
  echo    "  wlan0 txpower: $(iw dev wlan0 info 2>/dev/null | awk "/txpower/{print \$2,\$3}")"'
