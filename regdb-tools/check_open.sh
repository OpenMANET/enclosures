#!/usr/bin/env bash
# check_open.sh - verify a node flashed with the open-00 regulatory.db is
# actually running "open". Walks the layers L1-L5 that can each clamp spectrum
# and prints PASS/WARN/FAIL per layer. L6 (driver/BCF) and L7 (RF measurement)
# are reported as manual TODOs because they need vendor tools / a spectrum
# analyzer.
#
# Usage:  sudo ./check_open.sh [phy]        (default: phy0)
#         REGDB=/path/to/regulatory.db ./check_open.sh
#
# Exit code: 0 if L1-L5 all PASS, 1 otherwise.

set -u
PHY="${1:-phy0}"
REGDB="${REGDB:-/lib/firmware/regulatory.db}"
HERE="$(cd "$(dirname "$0")" && pwd)"
rc=0
pass(){ echo "  PASS  $*"; }
warn(){ echo "  WARN  $*"; rc=1; }
fail(){ echo "  FAIL  $*"; rc=1; }
have(){ command -v "$1" >/dev/null 2>&1; }

echo "== check_open.sh (phy=$PHY, regdb=$REGDB) =="

# ---- L1: DB present + contains the open 868 rule + loaded without error ----
echo "L1  regulatory.db loaded and contains open 868"
if [ -r "$REGDB" ]; then
    pass "file present: $REGDB ($(stat -c %s "$REGDB" 2>/dev/null) bytes)"
    if have python3 && [ -r "$HERE/regdb_read.py" ]; then
        if python3 "$HERE/regdb_read.py" "$REGDB" 00 2>/dev/null | grep -q "863.0 - 870.0"; then
            pass "domain 00 has 863-870 rule"
        else
            fail "domain 00 does NOT contain the 863-870 rule (wrong file?)"
        fi
    else
        warn "python3/regdb_read.py unavailable - skipped content check"
    fi
else
    fail "cannot read $REGDB"
fi
if dmesg 2>/dev/null | grep -iE 'regulatory\.db' | grep -qi 'not loaded\|signature'; then
    fail "dmesg shows regulatory.db not loaded / signature error (signed-regdb kernel?)"
else
    dmesg 2>/dev/null | grep -iq 'loaded regulatory' && pass "dmesg: regulatory db loaded"
fi

# ---- L2: active global domain is 00 ----
echo "L2  active regulatory domain is 00"
if have iw; then
    if iw reg get 2>/dev/null | grep -qE '^country 00:'; then
        pass "iw reg get -> country 00"
    else
        cc=$(iw reg get 2>/dev/null | grep -m1 -oE '^country ..' || echo '??')
        fail "active domain is '$cc', not 00  (run: iw reg set 00)"
    fi
else
    fail "iw not installed"
fi

# ---- L3: phy is not self-managed (else it ignores domain 00) ----
echo "L3  $PHY is globally managed (not self-managed)"
if have iw; then
    if iw phy "$PHY" info 2>/dev/null | grep -qi 'self-managed'; then
        warn "$PHY is SELF-MANAGED -> it ignores domain 00; check driver/BCF (L6)"
    else
        pass "$PHY honours the global domain"
    fi
fi

# ---- L4: bands exposed and not no-IR / disabled ----
echo "L4  channels exposed and transmit-capable (no 'no IR'/disabled)"
if have iw; then
    ch=$(iw phy "$PHY" channels 2>/dev/null)
    if [ -n "$ch" ]; then
        total=$(grep -c 'MHz' <<<"$ch")
        blocked=$(grep -cE 'disabled|no IR' <<<"$ch")
        if [ "$total" -gt 0 ] && [ "$blocked" -eq 0 ]; then
            pass "$total channels, none disabled/no-IR"
        else
            warn "$blocked of $total channels are disabled or no-IR (see: iw phy $PHY channels)"
        fi
    else
        warn "no channel info for $PHY"
    fi
fi

# ---- L5: tx power ceiling ----
echo "L5  TX power ceiling"
if have iw; then
    tp=$(iw dev 2>/dev/null | awk '/txpower/{print $2; exit}')
    [ -n "$tp" ] && pass "current txpower ${tp} dBm (regdb ceiling 30; driver may clamp)" \
                 || warn "could not read txpower (interface down?)"
    maxdbm=$(iw phy "$PHY" info 2>/dev/null | grep -oE '\([0-9.]+ dBm\)' | tr -d '() dBm' \
             | sort -n | tail -1)
    [ -n "$maxdbm" ] && echo "        max per-channel allowed: ${maxdbm} dBm"
fi

# ---- L6 / L7: manual ----
echo "L6  driver/BCF clamp (MANUAL):"
echo "        MM8108: morsectrl regulatory ; dmesg | grep -i morse   (BCF/country = real 868 clamp)"
echo "        MT7916: dmesg | grep -iE 'mt76|sku|txpower'            (power-limit tables)"
echo "L7  RF ground truth (MANUAL): measure center freq, bandwidth (1/2/4 MHz) and EIRP"
echo "        on 868 MHz with an SDR/spectrum analyzer - the only real proof."

echo "== result: $([ $rc -eq 0 ] && echo 'L1-L5 PASS' || echo 'issues above (WARN/FAIL)') =="
exit $rc
