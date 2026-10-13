#!/bin/bash
# ============================================================================
# status.sh [ip ...]   — one-shot health check of the lab fleet.
#
# Per node: hostname, reg domain, per-interface driver/type/txpower,
# AP state, batman interfaces + neighbor count, HaLow channel + mesh peers,
# and key service states. Defaults to the three lab nodes if no IPs given.
# ============================================================================
set -uo pipefail
SD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SD/lib.sh"
NODES=("$@")[[ ${#NODES[@]} -eq 0 ]] && NODES=(10.30.2.46 10.30.2.61 10.30.2.101 10.30.2.166)

for IP in "${NODES[@]}"; do
  hn=$(ssh_cmd "$IP" hostname 2>/dev/null)
  echo "======================================================================"
  echo " $IP  (${hn:-UNREACHABLE})"
  echo "======================================================================"
  [[ -z "$hn" ]] && {
    echo "  (no SSH — powered off or IP drifted)"
    echo
    continue
  }
  ssh_rootp "$IP" '
    echo "  reg domain : $(iw reg get | grep -m1 country | sed "s/^/  /")"
    echo "  interfaces :"
    for i in /sys/class/net/wlan*; do n=$(basename $i);
      drv=$(basename $(readlink -f $i/device/driver) 2>/dev/null);
      typ=$(iw dev $n info 2>/dev/null | awk "/type/{print \$2}");
      tx=$(iw dev $n info 2>/dev/null | awk "/txpower/{print \$2\$3}");
      printf "    %-6s %-10s %-11s %s\n" "$n" "$drv" "$typ" "$tx"; done
    ap=$(cat /var/lib/ap_interface 2>/dev/null)
    echo "  AP ($ap)   : $(systemctl is-active hostapd 2>/dev/null)"
    echo "  bat0 ifaces: $(batctl if 2>/dev/null | tr "\n" " ")"
    echo "  bat0 neigh : $(( $(batctl n 2>/dev/null | grep -cE "0c:bf:74|00:0a:52") - 1 ))"
    if iw dev wlan2 info >/dev/null 2>&1 && systemctl is-active --quiet wpa_supplicant-s1g-wlan2.service; then
      echo "  HaLow chan : $(morse_cli -i wlan2 channel -j 2>/dev/null | tr -d "\n ")"
      echo "  HaLow peers: $(iw dev wlan2 station dump 2>/dev/null | awk "/Station/{s=\$2} /mesh plink:/{printf s\"=\"\$3\"  \"}")"
    else
      echo "  HaLow      : supplicant inactive"
    fi
    echo "  services   :"
    for s in hostapd wpa_supplicant@wlan0 wpa_supplicant-s1g-wlan2 batman-enslave-watch mesh-status node-manager mesh-voice manet-regdom-open manet-wlan-naming manet-audio-level manet-cpu-perf; do
      printf "    %-28s %s\n" "$s" "$(systemctl is-active $s.service 2>/dev/null)"; done
  '
  echo
done
