#!/bin/bash
# Action: node health check. Collects facts from each target node
# via remote/node-status.sh and prints a color-coded summary.
set -u
source "$(dirname "$0")/../lib/common.sh"
source "$(dirname "$0")/../lib/target.sh"

REMOTE="$REPO_ROOT/remote/node-status.sh"

# Expected radio policy: MT7916 2.4 GHz + MM8108 HaLow meshed on bat0,
# MT7916 5 GHz as EUD AP off bat0.
HALOW_KHZ=863500    # S1G 1 MHz channel 7 (EU plan)
AP_PREFIX=rdt-manet  # EUD AP SSID = prefix + "-" + hostname suffix
MIC_STD=32           # mic capture standard: value 32/35 = 91%
PCM_STD=37           # pcm playback standard: value 37/37 = 0 dB (max)

# Failed units that are harmless on CM4 (udev settle timeout race).
HARMLESS_FAILED="systemd-udev-settle.service manet-radio-names.service"

# True when "drv|type|ch|ssid|inbat" matches the wanted driver, type,
# and sits on bat0.
radio_ok() {
    local d t c s b
    IFS='|' read -r d t c s b <<<"$1"
    [ "$d" = "$2" ] && [ "$t" = "$3" ] && [ "$b" = "1" ]
}

# One radio line, column-aligned.
show_radio() {  # $1 label $2 raw
    local d t c s b bat0str="no"
    IFS='|' read -r d t c s b <<<"$2"
    [ "$b" = "1" ] && bat0str="yes"
    [ "$s" = "none" ] && s="--"
    printf '      %-6s  %-9s  %-5s  ch %-4s  %-15s  bat0 %s\n' \
        "$1" "$d" "$t" "$c" "${s:--}" "$bat0str"
}

check_node() {
    local ip=$1
    local host="" up_s=0 ver="" au=""
    local w0="" w1="" w2="" ap_if=""
    local freq=0 peers=0 usb=0 n24=0 nhalo=0
    local vptt="" vblocked="" gps=none gps_active="" gps_fix=""
    local mic=none pcm=none agc=""
    local failed="" problems=0

    print "$C_DIM" "  ----------------------------------------------------------------"

    local out
    if ! out=$(node_root_script "$ip" "$REMOTE" 2>/dev/null); then
        print "$C_HEAD" "  $ip ${C_BAD}[unreachable]"
        print "$C_BAD" "    ! node unreachable (power? cable?)"
        return 0
    fi

    # Parse TAG key=value lines from the remote probe.
    local f raw
    while read -r -a f; do
        [ "${#f[@]}" -gt 0 ] || continue
        case ${f[0]} in
        NODE)
            host=${f[1]#host=} up_s=${f[2]#up_s=}
            ver=${f[3]#ver=} au=${f[4]#au=}
            ;;
        FAILED)
            # Skip known-harmless failures.
            case " $HARMLESS_FAILED " in
            *" ${f[1]} "*) ;;
            *) failed="$failed ${f[1]}" ;;
            esac
            ;;
        SVC)
            if [ "${f[1]#*=}" != "active" ]; then
                problems=$((problems + 1))
            fi
            ;;
        IF)
            if [ "${f[1]}" = "missing" ]; then
                problems=$((problems + 1))
            else
                raw="${f[2]#drv=}|${f[3]#type=}|${f[4]#ch=}|${f[5]#ssid=}|${f[6]#in_bat0=}"
                case ${f[1]} in
                wlan0) w0=$raw ;; wlan1) w1=$raw ;; wlan2) w2=$raw ;;
                esac
            fi
            ;;
        AP) ap_if=${f[1]#if=} ;;
        HALOW) freq=${f[1]#freq_khz=} peers=${f[2]#peers=} usb=${f[3]#usb_errors_5min=} ;;
        BAT) n24=${f[1]#n24=} nhalo=${f[2]#nhalow=} ;;
        VOICE)
            if [ "${f[1]}" = "missing" ]; then
                problems=$((problems + 1))
            else
                vptt=${f[1]#ptt=} vblocked=${f[2]#blocked=}
            fi
            ;;
        GPS)
            if [ "${f[1]}" != "none" ]; then
                gps=present gps_active=${f[2]#active=} gps_fix=${f[3]#fix=}
            fi
            ;;
        MIC)
            if [ "${f[1]}" != "none" ]; then
                mic=${f[2]#val=}
                pcm=${f[3]#pcm=}
                agc=${f[4]#agc=}
            fi
            ;;
        esac
    done <<<"$out"

    [ -n "$host" ] || return 0

    # --- check radio policy -------------------------------------------------

    local issue_list=""

    radio_ok "$w0" mt7915e mesh \
        || issue_list="$issue_list\n    ! wlan0: want MT7916 2.4 GHz mesh on bat0"

    local suffix=${host#mesh-}
    local d1 t1 c1 ssid b1
    IFS='|' read -r d1 t1 c1 ssid b1 <<<"$w1"
    if [ "$d1" != "mt7915e" ] || [ "$t1" != "ap" ] || [ "$b1" != "0" ] \
        || [ "$ap_if" != "wlan1" ] \
        || { [ "$suffix" != "$host" ] && [ "$ssid" != "$AP_PREFIX-$suffix" ]; }; then
        issue_list="$issue_list\n    ! wlan1: want MT7916 5 GHz EUD AP off bat0"
    fi

    radio_ok "$w2" morse_usb mesh \
        || issue_list="$issue_list\n    ! wlan2: want MM8108 HaLow mesh on bat0"

    [ "$freq" = "$HALOW_KHZ" ] || issue_list="$issue_list\n    ! HaLow ${freq} kHz, want ${HALOW_KHZ}"
    [ "$peers" -ge 1 ] 2>/dev/null || issue_list="$issue_list\n    ! no HaLow peers"
    [ "$usb" -eq 0 ] 2>/dev/null || issue_list="$issue_list\n    ! $usb HaLow USB errors (5 min)"
    [ "$n24" -ge 1 ] 2>/dev/null || issue_list="$issue_list\n    ! no 2.4 GHz batman neighbours"
    [ "$nhalo" -ge 1 ] 2>/dev/null || issue_list="$issue_list\n    ! no HaLow batman neighbours"
    [ -z "$failed" ] || issue_list="$issue_list\n    ! failed units:$failed"

    if [ -n "$vptt" ]; then
        if [ "$vptt" != "true" ] || [ "$vblocked" != "false" ]; then
            issue_list="$issue_list\n    ! voice: ptt=$vptt blocked=$vblocked"
        fi
    fi

    if [ "$gps" = "present" ]; then
        [ "$gps_active" = "active" ] || issue_list="$issue_list\n    ! gpsd not active"
        [ "$gps_fix" = "true" ] || issue_list="$issue_list\n    ! GPS present, no fix"
    fi

    if [ "$mic" != "none" ] && [ "$mic" != "$MIC_STD" ]; then
        issue_list="$issue_list\n    ! mic capture $mic, standard is $MIC_STD"
    fi

    if [ "$pcm" != "none" ] && [ "$pcm" != "$PCM_STD" ]; then
        issue_list="$issue_list\n    ! pcm playback $pcm, standard is $PCM_STD"
    fi

    if [ "$agc" = "on" ]; then
        issue_list="$issue_list\n    ! AGC is on (costs 24 dB noise floor with preamp)"
    fi

    # --- header tag ---------------------------------------------------------

    local tag
    if [ -z "$issue_list" ]; then
        tag="${C_OK}[ok]"
    else
        local n
        n=$(printf '%b' "$issue_list" | grep -c '!')
        tag="${C_ORG}[$n problem(s)]"
    fi

    # --- display ------------------------------------------------------------

    print "$C_HEAD" "  $host $C_DIM($ip)$C_RST $tag"
    print "" "    up $(fmt_uptime "$up_s")   ver $ver   auto_update $au"

    print "$C_DIM" "    radios (2.4G + HaLow mesh on bat0; 5G AP off-mesh)"
    show_radio "2.4G" "$w0"
    show_radio "5G AP" "$w1"
    show_radio "HaLow" "$w2"

    # Mesh summary line.
    local m24=$n24 mhalo=$nhalo musb=$usb
    [ "$n24" -eq 0 ] 2>/dev/null && m24="$C_WARN 0"
    [ "$nhalo" -eq 0 ] 2>/dev/null && mhalo="$C_WARN 0"
    [ "$usb" -gt 0 ] 2>/dev/null && musb="$C_WARN $usb"
    printf '    %-7s 2.4G %b nbrs  HaLow %b nbrs  usb_err5m %b\n' \
        "mesh" "$m24" "$mhalo" "$musb"

    # HaLow.
    local halow
    if [ "$freq" = "$HALOW_KHZ" ] && [ "$peers" -ge 1 ] 2>/dev/null; then
        halow="$C_OK up: ${freq} kHz, ${peers} peers"
    else
        halow="$C_WARN down: ${freq} kHz, ${peers} peers"
    fi
    printf '    %-7s %b\n' "halow" "$halow"

    # Voice.
    local voice
    if [ "$vptt" = "true" ] && [ "$vblocked" = "false" ]; then
        voice="$C_OK ok"
    else
        voice="$C_ORG ptt $vptt, blocked $vblocked"
    fi
    printf '    %-7s %b\n' "voice" "$voice"

    # GPS.
    local gps_str
    if [ "$gps" != "present" ]; then
        gps_str="$C_DIM none"
    elif [ "$gps_active" != "active" ]; then
        gps_str="$C_ORG gpsd down"
    elif [ "$gps_fix" = "true" ]; then
        gps_str="$C_OK up, fix"
    else
        gps_str="$C_WARN up, no fix"
    fi
    printf '    %-7s %b\n' "gps" "$gps_str"

    # Audio: mic, pcm, agc.
    local mic_str pcm_str agc_str
    if [ "$mic" = "$MIC_STD" ]; then
        mic_str="$C_OK $mic"
    elif [ "$mic" = "none" ]; then
        mic_str="$C_DIM none (board out)"
    else
        mic_str="$C_ORG $mic (want $MIC_STD)"
    fi

    if [ "$pcm" = "$PCM_STD" ]; then
        pcm_str="$C_OK $pcm"
    elif [ "$pcm" = "none" ]; then
        pcm_str="$C_DIM none"
    else
        pcm_str="$C_ORG $pcm (want $PCM_STD)"
    fi

    if [ "$agc" = "off" ]; then
        agc_str="$C_OK off"
    elif [ "$agc" = "on" ]; then
        agc_str="$C_ORG on"
    else
        agc_str="$C_DIM ?"
    fi
    printf '    %-7s mic %b%s  pcm %b%s  agc %b%s\n' \
        "audio" "$mic_str" "$C_RST" "$pcm_str" "$C_RST" "$agc_str" "$C_RST"

    # Issues.
    if [ -n "$issue_list" ]; then
        printf '%b\n' "$issue_list" | while IFS= read -r line; do
            [ -n "$line" ] && print "$C_ORG" "$line"
        done
    fi
}

# --- main -------------------------------------------------------------------

pick_target

for ip in $TARGET_IPS; do
    check_node "$ip"
done
print "$C_DIM" "  ----------------------------------------------------------------"
