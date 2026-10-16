#!/bin/bash
# Interactive fleet check: verifies every node in `nodes` (or the IPs
# given) against the lab radio policy, prints a summary, then prompts
# for an action on every problem found.
#
# Checked per node: reachability, hostname, uptime, version,
# auto_update, core services, radio roles (MT7916 2.4 GHz + MM8108 HaLow
# meshed on bat0, MT7916 5 GHz EUD AP), batman neighbours, HaLow
# frequency/peers/USB errors, voice, GPS, mic level.
#
# usage: scripts/fleet-status.sh [ip...]
set -u
source "$(dirname "$0")/lib.sh"

HALOW_KHZ=863500    # S1G 1 MHz channel 7
MIC_STD=32          # mic capture standard: value 32/35 = 91%
MIC_PCT=90          # pct passed to set-mic-gain.sh (lands on MIC_STD)
AP_PREFIX=rdt-manet # EUD AP SSID = prefix + "-" + hostname minus "mesh-"
REBOOT_WAIT_SEC=300
# systemd-udev-settle.service times out on the CM4 and is always "failed";
# ignore it.
HARMLESS_FAILED=systemd-udev-settle.service
REMOTE="$REPO_ROOT/scripts/remote/fleet-status.sh"

# Problem list: parallel arrays, index i -> ip, label, kind, arg, desc.
# kind is an enum: svc (restart a service), mic (apply the mic standard),
# none (no automatic fix: shell/reboot/skip).
P_IP=() P_LBL=() P_KIND=() P_ARG=() P_DESC=()

# Per-node facts, filled by collect(); count mode is used by recheck().
C_ip="" C_host="" C_unreachable=0 C_notes="" C_count=0 C_count_mode=0

# Append a problem, or just count it while rechecking.
add_problem() {
    if [ "$C_count_mode" -eq 1 ]; then
        C_count=$((C_count + 1))
        return 0
    fi
    P_IP+=("$C_ip") P_LBL+=("$C_host") P_KIND+=("$1") P_ARG+=("$2") P_DESC+=("$3")
    return 0
}

# Seconds -> "1h 23m" | "42m" | "33s".
fmt_up() {
    local s=$1
    if [ "$s" -ge 3600 ]; then
        echo "$((s / 3600))h $(((s % 3600) / 60))m"
    elif [ "$s" -ge 60 ]; then
        echo "$((s / 60))m"
    else
        echo "${s}s"
    fi
}

# True when "drv|type|ch|ssid|inbat" has the wanted driver and type and
# sits on bat0.
radio_ok() {
    local d t c s b
    IFS='|' read -r d t c s b <<<"$1"
    [ "$d" = "$2" ] && [ "$t" = "$3" ] && [ "$b" = "1" ]
}

# Collect one node's facts into C_* globals and record problems.
collect() {
    C_ip=$1
    C_host="" C_up_s=0 C_ver="" C_au="" C_ap_if="" C_failed=""
    C_w0="" C_w1="" C_w2=""
    C_freq=0 C_peers=0 C_usb=0 C_n24=0 C_nhalo=0
    C_vptt="" C_vblocked="" C_gps=none C_gps_active="" C_gps_fix="" C_mic=none
    C_unreachable=0 C_notes=""

    local out
    if ! out=$(node_root_script "$C_ip" "$REMOTE" 2>/dev/null); then
        C_host=$C_ip
        C_unreachable=1
        add_problem none unreachable "node unreachable (power? cable?)"
        return 0
    fi

    local f raw
    while read -r -a f; do
        [ "${#f[@]}" -gt 0 ] || continue
        case ${f[0]} in
        NODE) C_host=${f[1]#host=} C_up_s=${f[2]#up_s=} C_ver=${f[3]#ver=} C_au=${f[4]#au=} ;;
        FAILED)
            if [ "${f[1]}" != "$HARMLESS_FAILED" ]; then
                C_failed="$C_failed ${f[1]}"
            fi
            ;;
        SVC)
            if [ "${f[1]#*=}" != "active" ]; then
                add_problem svc "${f[1]%%=*}" "service ${f[1]%%=*} not active"
            fi
            ;;
        IF)
            if [ "${f[1]}" = "missing" ]; then
                add_problem none if-missing "interface ${f[1]} missing"
            else
                raw="${f[2]#drv=}|${f[3]#type=}|${f[4]#ch=}|${f[5]#ssid=}|${f[6]#in_bat0=}"
                case ${f[1]} in
                wlan0) C_w0=$raw ;;
                wlan1) C_w1=$raw ;;
                wlan2) C_w2=$raw ;;
                esac
            fi
            ;;
        AP) C_ap_if=${f[1]#if=} ;;
        HALOW) C_freq=${f[1]#freq_khz=} C_peers=${f[2]#peers=} C_usb=${f[3]#usb_errors_5min=} ;;
        BAT) C_n24=${f[1]#n24=} C_nhalo=${f[2]#nhalow=} ;;
        VOICE)
            if [ "${f[1]}" = "missing" ]; then
                add_problem svc mesh-voice "voice state file missing"
            else
                C_vptt=${f[1]#ptt=} C_vblocked=${f[2]#blocked=}
            fi
            ;;
        GPS)
            if [ "${f[1]}" = "none" ]; then
                C_gps=none
            else
                C_gps=present C_gps_active=${f[2]#active=} C_gps_fix=${f[3]#fix=}
            fi
            ;;
        MIC)
            if [ "${f[1]}" != "none" ]; then
                C_mic=${f[2]#val=}
            fi
            ;;
        esac
    done <<<"$out"

    [ -n "$C_host" ] || return 0

    # Radio role policy: MT7916 2.4 GHz + MM8108 HaLow meshed on bat0,
    # MT7916 5 GHz as the EUD AP.
    radio_ok "$C_w0" mt7915e mesh \
        || add_problem none if-wlan0 "wlan0 not MT7916 2.4 GHz mesh (want drv=mt7915e type=mesh in_bat0=1)"

    local suffix=${C_host#mesh-}
    if [ "$suffix" = "$C_host" ]; then
        add_problem none hostname "hostname $C_host is not mesh-<id>"
        suffix=""
    fi
    # The AP must not be on bat0, so this is checked by hand, not via
    # radio_ok().
    local d1 t1 c1 ssid b1
    IFS='|' read -r d1 t1 c1 ssid b1 <<<"$C_w1"
    if [ "$d1" != "mt7915e" ] || [ "$t1" != "ap" ] || [ "$b1" != "0" ] \
        || [ "$C_ap_if" != "wlan1" ] \
        || { [ -n "$suffix" ] && [ "$ssid" != "$AP_PREFIX-$suffix" ]; }; then
        add_problem none if-wlan1 "wlan1 not MT7916 5 GHz EUD AP (want drv=mt7915e type=ap off-bat0 ap_if=wlan1 ssid=$AP_PREFIX-$suffix)"
    fi

    radio_ok "$C_w2" morse_usb mesh \
        || add_problem none if-wlan2 "wlan2 not MM8108 HaLow mesh (want drv=morse_usb type=mesh in_bat0=1)"

    [ "$C_freq" = "$HALOW_KHZ" ] || add_problem none halow-freq "HaLow ${C_freq} kHz, want ${HALOW_KHZ}"
    [ "$C_peers" -ge 1 ] || add_problem none halow-peers "no HaLow peers established"
    [ "$C_usb" -eq 0 ] || add_problem none halow-usb "$C_usb HaLow USB errors in the last 5 min"
    [ "$C_n24" -ge 1 ] || add_problem none mesh24 "no batman neighbours on 2.4 GHz"
    [ "$C_nhalo" -ge 1 ] || add_problem none mesh1g "no batman neighbours on HaLow"
    [ -z "$C_failed" ] || add_problem none failed-units "failed units:$C_failed"

    if [ -n "$C_vptt" ]; then
        if [ "$C_vptt" != "true" ] || [ "$C_vblocked" != "false" ]; then
            add_problem svc mesh-voice "voice: ptt=$C_vptt blocked=$C_vblocked"
        fi
    fi

    if [ "$C_gps" = "present" ]; then
        if [ "$C_gps_active" != "active" ]; then
            add_problem svc gpsd "gpsd not active"
        fi
        if [ "$C_gps_fix" != "true" ]; then
            C_notes="$C_notes; no GPS fix yet (indoor?)"
        fi
    fi

    if [ "$C_mic" = "none" ]; then
        C_notes="$C_notes; no OpenVLM board"
    elif [ "$C_mic" != "$MIC_STD" ]; then
        add_problem mic "$MIC_STD" "mic capture $C_mic, standard is $MIC_STD (91%)"
    fi
    return 0
}

# One "drv|type|ch|ssid|inbat" radio line, labeled.
show_radio() {  # $1 label $2 raw
    local d t c s b
    IFS='|' read -r d t c s b <<<"$2"
    echo "     $1: drv=$d type=$t ch=$c ssid=$s bat0=$b"
    return 0
}

show_node() {
    echo "---- $C_host ($C_ip)"
    if [ "$C_unreachable" -eq 1 ]; then
        echo "     unreachable"
        return 0
    fi
    local gps="none"
    if [ "$C_gps" = "present" ]; then
        gps="up (active=$C_gps_active fix=$C_gps_fix)"
    fi
    echo "     up $(fmt_up "$C_up_s")  ver $C_ver  auto_update=$C_au"
    show_radio "2.4G" "$C_w0"
    show_radio "AP" "$C_w1"
    show_radio "1G" "$C_w2"
    echo "     halow: ${C_freq} kHz, ${C_peers} peers, usb_err5m=${C_usb}, ap_if=$C_ap_if"
    echo "     neighbours: 2.4=$C_n24 halow=$C_nhalo   voice: ptt=${C_vptt:-?} blocked=${C_vblocked:-?}"
    echo "     gps: $gps   mic: $C_mic$C_notes"
    return 0
}

run_fix() {  # $1 ip $2 kind $3 arg
    case $2 in
    svc) printf '%s\n' "$SUDO_PASS" | node_ssh "$1" "sudo -S -p '' systemctl restart $3" \
            && echo "     restarted $3" ;;
    mic) node_root_script "$1" "$REPO_ROOT/scripts/remote/set-mic-gain.sh" "$MIC_PCT" \
            && echo "     mic set to standard" ;;
    esac
    return 0
}

recheck() {
    C_count_mode=1
    C_count=0
    collect "$1"
    C_count_mode=0
    echo "     re-check ${C_host}: ${C_count} problem(s) remain"
    return 0
}

shell_node() {
    echo "     shell on $1 (exit to return)"
    printf '%s\n' "$SUDO_PASS" | node_ssh "$1" -t "sudo -S -p '' bash"
    return 0
}

reboot_node() {
    echo "     rebooting $1"
    printf '%s\n' "$SUDO_PASS" | node_ssh "$1" "sudo -S -p '' systemctl reboot"
    wait_for_node "$1" "$REBOOT_WAIT_SEC" || echo "     WARNING: $1 not back within $((REBOOT_WAIT_SEC / 60)) min"
    return 0
}

# Prompt for the problem at index i; sets CHOICE to r/s/R/k/q.
prompt() {
    local i=$1
    echo
    echo "[ $((i + 1))/${#P_IP[@]}] ${P_LBL[$i]} (${P_IP[$i]}): ${P_DESC[$i]}"
    case ${P_KIND[$i]} in
    svc) echo "     recommended fix: systemctl restart ${P_ARG[$i]}" ;;
    mic) echo "     recommended fix: apply mic standard (${MIC_STD} = 91%)" ;;
    esac
    echo "     [r]un fix  [s]hell on node  [R]eboot node  [k]skip  [q]uit"
    { read -r CHOICE < /dev/tty; } 2>/dev/null || read -r CHOICE || {
        echo "no input available, aborting"
        exit 1
    }
}

interact() {
    local total=${#P_IP[@]} i=0
    if [ "$total" -eq 0 ]; then
        echo
        echo "All nodes OK."
        return 0
    fi

    echo
    echo "$total problem(s) found."
    while [ "$i" -lt "$total" ]; do
        prompt "$i"
        case $CHOICE in
        r)
            if [ "${P_KIND[$i]}" = "none" ]; then
                echo "     no automatic fix for this one; try [s]hell or [R]eboot"
                continue
            fi
            run_fix "${P_IP[$i]}" "${P_KIND[$i]}" "${P_ARG[$i]}"
            recheck "${P_IP[$i]}"
            ;;
        s) shell_node "${P_IP[$i]}" ;;
        R)
            reboot_node "${P_IP[$i]}"
            recheck "${P_IP[$i]}"
            ;;
        q) echo "aborted" ;;
        k | '') : ;;
        *) echo "invalid choice"; continue ;;
        esac
        if [ "$CHOICE" = "q" ]; then
            return 0
        fi
        i=$((i + 1))
    done
    echo
    echo "done; run this script again to verify the fleet is clean."
    return 0
}

main() {
    local ip start n i
    for ip in $(fleet_ips "$@"); do
        start=${#P_IP[@]}
        collect "$ip"
        show_node
        n=$(( ${#P_IP[@]} - start ))
        if [ "$n" -eq 0 ]; then
            [ "$C_unreachable" -eq 0 ] && echo "     ok"
        else
            for ((i = start; i < start + n; i++)); do
                echo "     ! ${P_DESC[$i]}"
            done
        fi
    done
    interact
    return 0
}

main "$@"
