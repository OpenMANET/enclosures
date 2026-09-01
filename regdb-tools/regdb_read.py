#!/usr/bin/env python3
"""Read/inspect a wireless regulatory database (RGDB) file.

Auto-detects and decodes BOTH on-disk formats:
  * v20 kernel firmware  (regulatory.db  - e.g. regulatory_24Aug26.db, db2fw.py)
  * v19 CRDA binary      (regulatory.bin - db2bin.py)

No external dependencies (pure struct parsing).

Usage:
  regdb_read.py <file>                 # list every country + its rules
  regdb_read.py <file> RO PL US        # only these countries
  regdb_read.py <file> --list          # just the country codes + rule counts
  regdb_read.py <file> --grep 863      # only rules whose text contains 863
"""
import struct, sys, argparse

def u16(d, o): return struct.unpack('>H', d[o:o+2])[0]
def u32(d, o): return struct.unpack('>I', d[o:o+4])[0]

def flagstr_crda(fl):
    """CRDA/source flag bits (v19)."""
    t = []
    if fl & (1 << 0):  t.append("NO-OFDM")
    if fl & (1 << 3):  t.append("NO-OUTDOOR")
    if fl & (1 << 4):  t.append("DFS")
    if fl & (1 << 7):  t.append("NO-IR")
    if fl & (1 << 11): t.append("AUTO-BW")
    return t

def flagstr_fw(fl):
    """Firmware flag bits (v20), remapped by db2fw.py."""
    t = []
    if fl & (1 << 0): t.append("NO-OFDM")
    if fl & (1 << 1): t.append("NO-OUTDOOR")
    if fl & (1 << 2): t.append("DFS")
    if fl & (1 << 3): t.append("NO-IR")
    if fl & (1 << 4): t.append("AUTO-BW")
    return t

def parse_v20(d):
    """Firmware format: 8-byte header, 4-byte country entries (alpha2 + be16 ptr>>2)."""
    out = {}; i = 8; minoff = len(d)
    while i + 4 <= len(d):
        a2 = d[i:i+2]
        if a2 != b'00' and not (65 <= a2[0] <= 90 and 65 <= a2[1] <= 90):
            break
        coff = u16(d, i+2) << 2
        out[a2.decode()] = coff; minoff = min(minoff, coff); i += 4
        if i >= minoff:
            break
    def rules(coll):
        n = d[coll+1]; res = []; p = coll + 4
        for _ in range(n):
            rp = u16(d, p) << 2; p += 2
            flags = d[rp+1]; eirp = u16(d, rp+2)
            s, e, bw = struct.unpack('>III', d[rp+4:rp+16])
            res.append((s, e, bw, eirp/100.0, flagstr_fw(flags)))
        return res
    return out, rules

def parse_v19(d):
    """CRDA format: 20-byte header, 8-byte country recs, pointer-based rules."""
    _, _, cptr, cnum, _ = struct.unpack('>IIIII', d[:20])
    out = {}
    for k in range(cnum):
        o = cptr + k*8
        a2 = d[o:o+2].decode(); coll = u32(d, o+4)
        out[a2] = coll
    def rules(coll):
        n = u32(d, coll); res = []
        for k in range(n):
            rp = u32(d, coll+4+k*4)
            fr = u32(d, rp); pw = u32(d, rp+4); fl = u32(d, rp+8)
            s, e, bw = struct.unpack('>III', d[fr:fr+12])
            _, eirp = struct.unpack('>II', d[pw:pw+8])
            res.append((s, e, bw, eirp/100.0, flagstr_crda(fl)))
        return res
    return out, rules

def main():
    ap = argparse.ArgumentParser(description="Read a wireless regulatory (RGDB) file")
    ap.add_argument("file")
    ap.add_argument("countries", nargs="*", help="limit to these ISO codes (e.g. RO PL)")
    ap.add_argument("--list", action="store_true", help="just codes + rule counts")
    ap.add_argument("--grep", help="only show rule lines containing this text")
    a = ap.parse_args()

    d = open(a.file, "rb").read()
    if d[:4] != b'RGDB':
        sys.exit("not an RGDB file (magic=%r)" % d[:4])
    ver = u32(d, 4)
    countries, rules = (parse_v20 if ver == 20 else parse_v19)(d)
    fmt = "firmware v20" if ver == 20 else "CRDA v19"
    print("# %s  (%s, %d bytes, %d countries)\n" % (a.file, fmt, len(d), len(countries)))

    want = [c.upper() for c in a.countries] if a.countries else list(countries)
    for c in want:
        if c not in countries:
            print("  (%s not present)" % c); continue
        rs = rules(countries[c])
        if a.list:
            print("  %-4s  %d rules" % (c, len(rs))); continue
        print("country %s (%d rules):" % (c, len(rs)))
        for s, e, bw, eirp, flags in rs:
            line = "   (%.1f - %.1f @ %d), (%.0f dBm)%s" % (
                s/1000, e/1000, bw/1000, eirp, (", " + ", ".join(flags)) if flags else "")
            if a.grep and a.grep not in line:
                continue
            print(line)
        print()

if __name__ == "__main__":
    main()
