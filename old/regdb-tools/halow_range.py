#!/usr/bin/env python3
"""868 MHz Wi-Fi HaLow (802.11ah / MM8108) range calculator.

Computes the two independent range ceilings for a LoS link and reports the
practical range as the smaller of the two:

  1. RF link budget  (free-space path loss vs receiver sensitivity)
  2. Radio horizon   (4/3-earth curvature, set by antenna height)

Also prints the 1st Fresnel-zone clearance needed at the link midpoint.

Examples:
  # defaults: 868 MHz, 21 dBm, 3 dBi both ends, 2 m masts, 10 dB margin
  ./halow_range.py

  # 1 W EIRP into 12 dBi dishes on 10 m masts, 1 MHz channel
  ./halow_range.py --tx 30 --gtx 12 --grx 12 --h1 10 --h2 10 --bw 1

  # sweep all bandwidths for a given setup
  ./halow_range.py --tx 21 --gtx 3 --grx 3 --h1 2 --h2 2
"""
import argparse, math

# MM8108-class MCS10 (most robust) sensitivity per channel bandwidth, dBm.
# Narrower channel -> better sensitivity -> more range. Override with --sens.
SENS = {1: -98.0, 2: -95.0, 4: -92.0, 8: -89.0}

def fspl_distance_km(mapl_db, f_mhz):
    """Distance at which FSPL == mapl_db."""
    return 10 ** ((mapl_db - 32.44 - 20 * math.log10(f_mhz)) / 20.0)

def radio_horizon_km(h1_m, h2_m):
    """4/3-earth radio horizon for two antenna heights (metres)."""
    return 4.12 * (math.sqrt(h1_m) + math.sqrt(h2_m))

def fresnel1_mid_m(d_km, f_mhz):
    """1st Fresnel-zone radius at the midpoint (metres)."""
    return 17.32 * math.sqrt((d_km / 4.0) / (f_mhz / 1000.0))

def one_row(bw, sens, tx, gtx, grx, margin, f):
    eirp = tx + gtx
    mapl = eirp + grx - sens - margin
    return fspl_distance_km(mapl, f)

def main():
    p = argparse.ArgumentParser(description="868 MHz HaLow LoS range calculator")
    p.add_argument("--freq", type=float, default=868.0, help="MHz (default 868)")
    p.add_argument("--tx",   type=float, default=21.0,  help="TX power dBm (default 21)")
    p.add_argument("--gtx",  type=float, default=3.0,   help="TX antenna gain dBi (default 3)")
    p.add_argument("--grx",  type=float, default=3.0,   help="RX antenna gain dBi (default 3)")
    p.add_argument("--margin", type=float, default=10.0, help="fade margin dB (default 10)")
    p.add_argument("--h1",   type=float, default=2.0,   help="antenna 1 height m (default 2)")
    p.add_argument("--h2",   type=float, default=2.0,   help="antenna 2 height m (default 2)")
    p.add_argument("--bw",   type=int,   choices=[1,2,4,8], help="only this channel BW (MHz)")
    p.add_argument("--sens", type=float, help="override RX sensitivity dBm for --bw")
    a = p.parse_args()

    eirp = a.tx + a.gtx
    print("868 MHz HaLow link — EIRP %.0f dBm (%.0f dBm + %.0f dBi), RX gain %.0f dBi, "
          "fade margin %.0f dB\n" % (eirp, a.tx, a.gtx, a.grx, a.margin))

    bws = [a.bw] if a.bw else [1, 2, 4]
    print("RF link-budget range (free-space, MCS10):")
    budget = {}
    for bw in bws:
        s = a.sens if (a.bw and a.sens is not None) else SENS[bw]
        d0  = one_row(bw, s, a.tx, a.gtx, a.grx, 0.0, a.freq)
        dm  = one_row(bw, s, a.tx, a.gtx, a.grx, a.margin, a.freq)
        budget[bw] = dm
        print("   %d MHz (sens %6.1f dBm): theoretical %8.1f km | with margin %8.1f km"
              % (bw, s, d0, dm))

    hz = radio_horizon_km(a.h1, a.h2)
    print("\nRadio horizon (curvature), antennas %.0f m and %.0f m high: %.1f km" % (a.h1, a.h2, hz))

    print("\nPractical range = min(link budget, horizon):")
    for bw in bws:
        practical = min(budget[bw], hz)
        limiter = "horizon-limited" if hz < budget[bw] else "budget-limited"
        fr = fresnel1_mid_m(practical, a.freq)
        print("   %d MHz: %6.1f km  (%s; needs ~%.0f m Fresnel clearance at midpoint, 60%%=%.0f m)"
              % (bw, practical, limiter, fr, 0.6 * fr))

if __name__ == "__main__":
    main()
