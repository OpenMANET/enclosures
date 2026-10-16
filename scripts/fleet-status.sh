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
# With no argument this is the base entry point for fleet ops: an
# interactive menu that checks the fleet and calls the other fleet
# scripts where needed. With IPs it checks just those, no menu.
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
# cat is an enum: sig (yellow), oth (orange), off (red).
P_IP=() P_LBL=() P_KIND=() P_ARG=() P_DESC=() P_CAT=()

# Per-node facts, filled by collect(); count mode is used by recheck().
C_ip="" C_host="" C_unreachable=0 C_count=0 C_count_mode=0

# Append a problem, or just count it while rechecking.
add_problem() {  # $1 kind $2 arg $3 desc $4 cat
    if [ "$C_count_mode" -eq 1 ]; then
        C_count=$((C_count + 1))
        return 0
    fi
    P_IP+=("$C_ip") P_LBL+=("$C_host") P_KIND+=("$1") P_ARG+=("$2") \
        P_DESC+=("$3") P_CAT+=("${4:-oth}")
    return 0
}

# Sets C_PROB to the color for a problem category.
prob_color() {  # $1 cat
    case $1 in
    sig) C_PROB=$C_WARN ;;
    off) C_PROB=$C_BAD ;;
    *) C_PROB=$C_ORG ;;
    esac
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
    C_unreachable=0

    local out
    if ! out=$(node_root_script "$C_ip" "$REMOTE" 2>/dev/null); then
        C_host=$C_ip
        C_unreachable=1
        add_problem none unreachable "node unreachable (power? cable?)" off
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
                add_problem svc "${f[1]%%=*}" "service ${f[1]%%=*} not active" oth
            fi
            ;;
        IF)
            if [ "${f[1]}" = "missing" ]; then
                add_problem none if-missing "interface ${f[1]} missing" oth
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
                add_problem svc mesh-voice "voice state file missing" oth
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
        || add_problem none if-wlan0 "wlan0 not MT7916 2.4 GHz mesh (want drv=mt7915e type=mesh in_bat0=1)" oth

    local suffix=${C_host#mesh-}
    if [ "$suffix" = "$C_host" ]; then
        add_problem none hostname "hostname $C_host is not mesh-<id>" oth
        suffix=""
    fi
    # The AP must not be on bat0, so this is checked by hand, not via
    # radio_ok().
    local d1 t1 c1 ssid b1
    IFS='|' read -r d1 t1 c1 ssid b1 <<<"$C_w1"
    if [ "$d1" != "mt7915e" ] || [ "$t1" != "ap" ] || [ "$b1" != "0" ] \
        || [ "$C_ap_if" != "wlan1" ] \
        || { [ -n "$suffix" ] && [ "$ssid" != "$AP_PREFIX-$suffix" ]; }; then
        add_problem none if-wlan1 "wlan1 not MT7916 5 GHz EUD AP (want drv=mt7915e type=ap off-bat0 ap_if=wlan1 ssid=$AP_PREFIX-$suffix)" oth
    fi

    radio_ok "$C_w2" morse_usb mesh \
        || add_problem none if-wlan2 "wlan2 not MM8108 HaLow mesh (want drv=morse_usb type=mesh in_bat0=1)" oth

    [ "$C_freq" = "$HALOW_KHZ" ] || add_problem none halow-freq "HaLow ${C_freq} kHz, want ${HALOW_KHZ}" sig
    [ "$C_peers" -ge 1 ] || add_problem none halow-peers "no HaLow peers established" sig
    [ "$C_usb" -eq 0 ] || add_problem none halow-usb "$C_usb HaLow USB errors in the last 5 min" sig
    [ "$C_n24" -ge 1 ] || add_problem none mesh24 "no batman neighbours on 2.4 GHz" sig
    [ "$C_nhalo" -ge 1 ] || add_problem none mesh1g "no batman neighbours on HaLow" sig
    [ -z "$C_failed" ] || add_problem none failed-units "failed units:$C_failed" oth

    if [ -n "$C_vptt" ]; then
        if [ "$C_vptt" != "true" ] || [ "$C_vblocked" != "false" ]; then
            add_problem svc mesh-voice "voice: ptt=$C_vptt blocked=$C_vblocked" oth
        fi
    fi

    if [ "$C_gps" = "present" ]; then
        if [ "$C_gps_active" != "active" ]; then
            add_problem svc gpsd "gpsd not active" oth
        fi
        if [ "$C_gps_fix" != "true" ]; then
            add_problem none gps-nofix "GPS present, no fix (indoor?)" sig
        fi
    fi

    if [ "$C_mic" != "none" ] && [ "$C_mic" != "$MIC_STD" ]; then
        add_problem mic "$MIC_STD" "mic capture $C_mic, standard is $MIC_STD (91%)" oth
    fi
    return 0
}

# One "drv|type|ch|ssid|inbat" radio line, column-aligned.
show_radio() {  # $1 label $2 raw
    local d t c s b bat0str="no"
    IFS='|' read -r d t c s b <<<"$2"
    [ "$b" = "1" ] && bat0str="yes"
    [ "$s" = "none" ] && s="--"
    printf '        %-6s  %-9s  %-5s  ch %-4s  %-15s  bat0 %s\n' \
        "$1" "$d" "$t" "$c" "${s:--}" "$bat0str"
    return 0
}

show_node() {
    local i nprob=$(( ${#P_IP[@]} - ${NODE_PROBLEMS_START:-0} ))
    print "$C_DIM" "----------------------------------------------------------------"
    if [ "$C_unreachable" -eq 1 ]; then
        print "$C_HEAD" "  $C_host $C_DIM($C_ip)$C_RST $C_BAD[unreachable]"
        print "$C_BAD" "      ! node unreachable (power? cable?)"
        return 0
    fi

    local halow gps mic voice
    if [ "$C_freq" = "$HALOW_KHZ" ] && [ "$C_peers" -ge 1 ]; then
        halow="$C_OK up: ${C_freq} kHz, ${C_peers} peers"
    else
        halow="$C_WARN down: ${C_freq} kHz, ${C_peers} peers"
    fi
    if [ "$C_gps" != "present" ]; then
        gps="$C_DIM none"
    elif [ "$C_gps_active" != "active" ]; then
        gps="$C_ORG gpsd down"
    elif [ "$C_gps_fix" = "true" ]; then
        gps="$C_OK up, fix"
    else
        gps="$C_WARN up, no fix (indoor?)"
    fi
    if [ "$C_mic" = "$MIC_STD" ]; then
        mic="$C_OK $C_mic"
    elif [ "$C_mic" = "none" ]; then
        mic="$C_DIM none (board out)"
    else
        mic="$C_ORG $C_mic (want $MIC_STD)"
    fi
    if [ "$C_vptt" = "true" ] && [ "$C_vblocked" = "false" ]; then
        voice="$C_OK ok"
    else
        voice="$C_ORG ptt $C_vptt, blocked $C_vblocked"
    fi

    # Header tag: worst category among this node's problems
    # (0 ok < 1 signal < 2 other < 3 offline).
    local worst=0 tag
    for ((i = ${NODE_PROBLEMS_START:-0}; i < ${#P_IP[@]}; i++)); do
        case ${P_CAT[$i]} in
        sig) [ "$worst" -lt 1 ] && worst=1 ;;
        oth) [ "$worst" -lt 2 ] && worst=2 ;;
        off) worst=3; break ;;
        esac
    done
    case $worst in
    0) tag="$C_OK[ok]" ;;
    1) tag="$C_WARN[$nprob signal problem(s)]" ;;
    2) tag="$C_ORG[$nprob problem(s)]" ;;
    3) tag="$C_BAD[unreachable]" ;;
    esac

    local m24=$C_n24 mhalo=$C_nhalo musb=$C_usb
    [ "$C_n24" -eq 0 ] && m24="$C_WARN 0"
    [ "$C_nhalo" -eq 0 ] && mhalo="$C_WARN 0"
    [ "$C_usb" -gt 0 ] && musb="$C_WARN $C_usb"

    print "$C_HEAD" "  $C_host $C_DIM($C_ip)$C_RST $tag"
    print "" "      up $(fmt_up "$C_up_s")   ver $C_ver   auto_update $C_au"
    print "$C_DIM" "      radios  (2.4G + HaLow mesh on bat0; 5G AP off-mesh)"
    show_radio "2.4G" "$C_w0"
    show_radio "5G AP" "$C_w1"
    show_radio "HaLow" "$C_w2"
    printf '      %-7s 2.4G %b nbrs  HaLow %b nbrs  usb_err5m %b\n' \
        "mesh" "$m24" "$mhalo" "$musb"
    printf '      %-7s %b\n' "haLow" "$halow"
    printf '      %-7s %b\n' "voice" "$voice"
    printf '      %-7s %b\n' "gps" "$gps"
    printf '      %-7s %b\n' "mic" "$mic"

    for ((i = ${NODE_PROBLEMS_START:-0}; i < ${#P_IP[@]}; i++)); do
        prob_color "${P_CAT[$i]}"
        print "$C_PROB" "      ! ${P_DESC[$i]}"
    done
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

# Colors: plain when output is not a terminal.
# Green = ok, yellow = signal problem, orange = other problem,
# red = offline/unreachable.
if [ -t 1 ]; then
    C_OK=$'\e[32m' C_WARN=$'\e[33m' C_ORG=$'\e[38;5;208m' C_BAD=$'\e[31m'
    C_HEAD=$'\e[1;36m' C_DIM=$'\e[2m' C_RST=$'\e[0m'
else
    C_OK="" C_WARN="" C_ORG="" C_BAD="" C_HEAD="" C_DIM="" C_RST=""
fi

# print <color> <text>
print() {
    if [ -n "$2" ]; then
        printf '%s%s%s\n' "$1" "$2" "$C_RST"
    else
        printf '\n'
    fi
    return 0
}

# Read one line from the user; sets ANSWER. Prefers /dev/tty so prompts
# stay interactive even when stdin is redirected. EOF means no user.
ask() {  # $1 prompt text
    local line
    printf '%s' "$1"
    { read -r line < /dev/tty; } 2>/dev/null || read -r line || {
        echo
        echo "no input available, aborting"
        exit 1
    }
    ANSWER=$line
    return 0
}

# Prompt for the problem at index i; sets CHOICE to r/s/R/k/q.
prompt() {
    local i=$1
    echo
    prob_color "${P_CAT[$i]}"
    print "$C_PROB" "  [ $((i + 1))/${#P_IP[@]}] ${P_LBL[$i]} (${P_IP[$i]}): ${P_DESC[$i]}"
    case ${P_KIND[$i]} in
    svc) echo "     recommended fix: systemctl restart ${P_ARG[$i]}" ;;
    mic) echo "     recommended fix: apply mic standard (${MIC_STD} = 91%)" ;;
    esac
    echo "     [r]un fix  [s]hell on node  [R]eboot node  [k]skip  [q]uit"
    ask "     > "
    CHOICE=$ANSWER
    return 0
}

interact() {
    local total=${#P_IP[@]} i=0
    if [ "$total" -eq 0 ]; then
        echo
        print "$C_OK" "  All nodes OK."
        return 0
    fi

    echo
    print "$C_ORG" "  $total problem(s) found."
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
    print "$C_WARN" "  done; run status again to verify the fleet is clean."
    return 0
}

check_fleet() {
    local ip
    for ip in $(fleet_ips "$@"); do
        NODE_PROBLEMS_START=${#P_IP[@]}
        collect "$ip"
        show_node
    done
    interact
    return 0
}

# --- node discovery ------------------------------------------------------
# The user may not know the fleet IPs (laptop on some node's AP or on a
# node's ethernet port). Scan the local /24 for live nodes.

# Ping every host of a /24, print the ones that answer. Wireless drops
# some replies under burst load, so two passes at modest parallelism.
ping_sweep() {  # $1 CIDR (or network prefix), $2 max parallel
    local subnet=$1 pass ip
    case $subnet in
    *.*) subnet="$subnet/24" ;;
    esac
    subnet=${subnet%/*}
    local a b c
    IFS='.' read -r a b c _ <<<"$subnet"
    for pass in 1 2; do
        for ip in $(seq 1 254); do
            printf '%s.%s.%s.%s\n' "$a" "$b" "$c" "$ip"
        done | xargs -n1 -P "$2" \
            sh -c 'ping -q -c1 -W1 "$1" >/dev/null 2>&1 && echo "$1"' _
    done | sort -u
    return 0
}

# One candidate: IP <TAB> state <TAB> hostname <TAB> version.
# state: ok (key auth, MANET) | pass (password auth, MANET) | other.
# stdin is closed: ssh would otherwise eat the caller's input.
probe_host() {  # $1 ip
    local ip=$1
    if node_ssh "$ip" true </dev/null 2>/dev/null; then
        probe_with_key "$ip"
        return 0
    fi
    # Fresh nodes keep the radio password (the old default is SUDO_PASS).
    if printf '%s\n' "$SUDO_PASS" | ssh "${SSH_OPTS[@]}" \
            -o PubkeyAuthentication=no -o PreferredAuthentications=password \
            -o NumberOfPasswordPrompts=1 \
            "$SSH_USER@$ip" true 2>/dev/null; then
        probe_with_password "$ip"
        return 0
    fi
    printf '%s\t%s\n' "$ip" other
    return 0
}

probe_with_key() {  # $1 ip
    local id
    id=$(node_ssh "$1" 'hostname; head -1 /etc/manet_version.txt 2>/dev/null' \
        </dev/null 2>/dev/null)
    if [ -n "${id##*$'\n'}" ]; then
        printf '%s\t%s\t%s\t%s\n' "$1" ok "${id%%$'\n'*}" "${id##*$'\n'}"
    else
        printf '%s\t%s\t%s\n' "$1" other "${id%%$'\n'*}"
    fi
    return 0
}

probe_with_password() {  # $1 ip
    local id
    id=$(printf '%s\n' "$SUDO_PASS" | ssh "${SSH_OPTS[@]}" \
        -o PubkeyAuthentication=no -o PreferredAuthentications=password \
        -o NumberOfPasswordPrompts=1 \
        "$SSH_USER@$1" 'hostname; head -1 /etc/manet_version.txt 2>/dev/null' 2>/dev/null)
    if [ -n "${id##*$'\n'}" ]; then
        printf '%s\t%s\t%s\t%s\n' "$1" pass "${id%%$'\n'*}" "${id##*$'\n'}"
    else
        printf '%s\t%s\t%s\n' "$1" other "${id%%$'\n'*}"
    fi
    return 0
}

# Authorize the fleet key for the radio user (radio password is SUDO_PASS).
copy_key() {  # $1 ip
    printf '%s\n' "$SUDO_PASS" | ssh-copy-id -i "$SSH_KEY" "$SSH_USER@$1"
    return 0
}

scan_menu() {
    local locals guess cidr ip ips probed ip_ state host ver known
    # Every local IPv4 network, one per line, for the prompt's default.
    locals=$(ip -4 -o addr show 2>/dev/null | awk '/ scope global /{
        split($4, a, "/"); split(a[1], o, ".")
        print o[1] "." o[2] "." o[3] ".0/" a[2]}' | awk '!seen[$0]++')
    guess=$(head -1 <<<"$locals")
    ask "   subnet to scan [${guess:-192.168.0.0/24}]${locals:+ (${locals//$'\n'/, })}: "
    cidr=$ANSWER
    [ -n "$cidr" ] || cidr=$guess
    echo "   scanning $cidr ..."
    ips=$(ping_sweep "$cidr" 16)
    if [ -z "$ips" ]; then
        echo "   no live hosts found"
        return 0
    fi

    local iparr=() known_hosts="" line
    mapfile -t iparr <<<"$ips"
    probed=""
    for ip in "${iparr[@]}"; do
        line=$(probe_host "$ip")
        # A node holds a second address (EUD DHCP): mark duplicates.
        case $(cut -f2 <<<"$line") in
        ok | pass)
            host=$(cut -f3 <<<"$line")
            case " $known_hosts " in
            *" $host "*) line="$ip	dup	$host	$(cut -f4 <<<"$line")" ;;
            *) known_hosts="$known_hosts $host" ;;
            esac
            ;;
        esac
        probed="$probed$line"$'\n'
    done

    echo
    echo "   found:"
    local prow=() pline
    mapfile -t prow <<<"$probed"
    for pline in "${prow[@]}"; do
        IFS=$'\t' read -r ip_ state host ver <<<"$pline"
        [ -n "$ip_" ] || continue
        known=""
        grep -qxF "$ip_" "$NODES_FILE" && known=" (already in nodes)"
        echo "   $ip_  $host  ${ver:-no-manet-version}$known"
        if [ "$state" = "pass" ]; then
            echo "       radio password accepted but the fleet key is not authorized"
        elif [ "$state" = "other" ]; then
            echo "       answers ssh but is not a MANET node"
        elif [ "$state" = "dup" ]; then
            echo "       second address of the same host; not added"
        fi
    done

    mapfile -t prow <<<"$probed"
    for pline in "${prow[@]}"; do
        IFS=$'\t' read -r ip_ state host ver <<<"$pline"
        [ -n "$ip_" ] || continue
        grep -qxF "$ip_" "$NODES_FILE" && continue
        [ "$state" = "other" ] && continue
        [ "$state" = "dup" ] && continue
        echo
        echo "   $ip_ ($host)"
        if [ "$state" = "pass" ]; then
            ask "   authorize the fleet key? [y/N]: "
            case $ANSWER in
            y | Y) copy_key "$ip_" || echo "   ssh-copy-id failed" ;;
            esac
        fi
        ask "   add to 'nodes'? [y/N]: "
        case $ANSWER in
        y | Y) printf '%s\n' "$ip_" >> "$NODES_FILE"; echo "   added $ip_" ;;
        esac
    done
    echo
    echo "   'nodes' now:"
    grep -E '^[0-9.]+$' "$NODES_FILE" || echo "   (empty)"
    return 0
}

# Base entry point. No argument: interactive menu. Arguments: check just
# those nodes, no menu.
TARGET="" ANSWER=""

# Ask which node(s) the next action applies to; blank = all.
pick_target() {
    ask "   target: enter/all for every node, or one IP from 'nodes': "
    case $ANSWER in
    '' | a | all) TARGET="" ;;
    *)
        if grep -qxF "$ANSWER" "$NODES_FILE"; then
            TARGET=$ANSWER
        else
            echo "   $ANSWER not in 'nodes'; targeting all"
            TARGET=""
        fi
        ;;
    esac
    return 0
}

# Run one of the fleet scripts against TARGET (blank = every node).
run_script() {  # $1 script under scripts/, $2... args that precede the IPs
    local s=$1 ips
    shift
    ips=$TARGET
    if [ -z "$ips" ]; then
        ips=$(fleet_ips | tr '\n' ' ')
    fi
    "$REPO_ROOT/scripts/$s" "$@" $ips
    return 0
}

# One menu line: cyan number, plain name, dim description.
menu_item() {  # $1 num $2 name $3 desc
    printf '    %s%s.%s  %-13s %s%s%s\n' \
        "$C_HEAD" "$1" "$C_RST" "$2" "$C_DIM" "$3" "$C_RST"
    return 0
}

menu() {
    while :; do
        echo
        print "$C_DIM" "----------------------------------------------------------------"
        print "$C_HEAD" "  MANET lab fleet - $(fleet_ips | wc -l) nodes"
        echo
        menu_item 1 "status"       "full check, prompt per problem"
        menu_item 2 "update"       "upstream node-update.sh, one node at a time"
        menu_item 3 "voice patch"  "deploy patched mesh-voice.py (PR #8)"
        menu_item 4 "web UI fix"   "deploy blank-VOICE-tab fix"
        menu_item 5 "radio policy" "blacklist brcmfmac + radio-setup (reboots)"
        menu_item 6 "auto-update"  "set auto_update y|n"
        menu_item 7 "mic standard" "set OpenVLM mic capture to ${MIC_STD} (91%)"
        menu_item 8 "node scan"    "find fleet nodes on this machine's network"
        menu_item q "quit"         ""
        ask "    > "
        case $ANSWER in
        1) check_fleet ;;
        2) pick_target; run_script update-node.sh ;;
        3) pick_target; run_script deploy-mesh-voice.sh ;;
        4) pick_target; run_script deploy-voice-tab-fix.sh ;;
        5)
            pick_target
            echo "   reboots each target; do not target the node whose AP you are on"
            run_script apply-radio-policy.sh
            ;;
        6)
            pick_target
            ask "   auto_update y|n: "
            case $ANSWER in
            y | n) run_script set-auto-update.sh "$ANSWER" ;;
            *) echo "   need y or n" ;;
            esac
            ;;
        7) pick_target; run_script set-mic-gain.sh ;;
        8) scan_menu ;;
        q) return 0 ;;
        *) echo "   unknown choice" ;;
        esac
        ask "   press enter to return to the menu: "
    done
}

if [ $# -gt 0 ]; then
    check_fleet "$@"
else
    menu
fi
