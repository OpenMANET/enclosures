# manet-lab-fleet-ops

Operations for a four-node MANET lab fleet running
[very-srs/MANET](https://github.com/very-srs/MANET).

**Upstream is the source of truth.** Nodes run stock MANET releases. This repo
only records the fleet, the few local deviations, and scripts to apply and
check them. Anything that belongs in the product goes upstream as a PR. A local
fix is dropped once a release contains it. History of every change:
[FIXES.md](FIXES.md).

`old/` holds tooling for an earlier MANET release. It is kept for reference
only: do not deploy from it.

---

## Fleet

Status as of 2026-10-13. MANET **0.569** on all nodes, kernel `6.18.33-manet`,
hardware Raspberry Pi CM4 Rev 1.1.

| Node | Mesh IP | EUD AP SSID (MT7916 5 GHz) | Onboard brcmfmac | HaLow MAC | OpenVLM |
|---|---|---|---|---|---|
| mesh-c370 | 10.30.2.46 | `rdt-manet-c370` | present, blacklisted | 0c:bf:74:00:28:67 | yes |
| mesh-0825 | 10.30.2.61 | `rdt-manet-0825` | none | 0c:bf:74:00:28:f5 | yes |
| mesh-c5ec | 10.30.2.101 | `rdt-manet-c5ec` | present, blacklisted | 0c:bf:74:00:28:7e | yes |
| mesh-5ab8 | 10.30.2.166 | `rdt-manet-5ab8` | none | 0c:bf:74:00:2f:00 | yes |

Power: the nodes run from one shared lab bench power supply via twist-lock
connectors. Handling one node's connector can trip the supply's short-circuit
protection and cut power to the others (seen 2026-10-13 at 16:19 for c370,
0825 and 5ab8, and 18:23 for 0825). Expect unclean shutdowns: journals end
without a shutdown sequence.

Mesh: SSID `rdt-manet`, `10.30.2.0/24`, `eud=wireless`, HaLow regulatory
domain EU. Each node also holds a second address in the subnet (its EUD DHCP
gateway). IPs are in `nodes`; SSH access in `.credentials`
(copy `.credentials.example`).

### Radio layout (lab policy)

```
            +-------------------- node --------------------+
 2.4 GHz    | MT7916 wlan0  ch 1   mesh --+                 |
            |                             +-- bat0 -- br0   |
 868 MHz    | Morse  wlan2  863.5 MHz     |    (batman-adv) |
 (HaLow)    |               1 MHz  mesh --+                 |
 5 GHz      | MT7916 wlan1  ch 36  EUD AP rdt-manet-<id>    |
 onboard    | brcmfmac      blacklisted                     |
            +-----------------------------------------------+
```

Only the MT7916 serves 2.4/5 GHz. Upstream would use the onboard brcmfmac
for the AP, so it is blacklisted (`scripts/apply-radio-policy.sh`).
`iw` shows HaLow as "channel 132 (5660 MHz)", which is the driver's mapping;
`morse_cli -i wlan2 channel` reports the real 863.5 MHz.

### Current status

| | c370 | 0825 | c5ec | 5ab8 |
|---|---|---|---|---|
| Services (voice, status, manager, batman, hostapd, supplicants) | up | up | up | up |
| 2.4 GHz mesh | up, 3 peers | up, 3 peers | up, 3 peers | up, 3 peers |
| HaLow mesh | up, 3 peers | up, 3 peers | up, 3 peers | up, 3 peers |
| 5 GHz AP on MT7916 | up | up | up | up |
| Voice (patched daemon) | up | up | up | up |
| `auto_update` | n | n | n | n |

Re-check any time with `scripts/fleet-check.sh` (read-only).

---

## Local deviations from upstream

| Deviation | Status | Drop when | Apply |
|---|---|---|---|
| Patched `mesh-voice.py`: OpenVLM hot-plug, playback held at max, +18 dB receive gain, PTT tones | [PR #8](https://github.com/very-srs/MANET/pull/8) | a release contains PR #8 | `scripts/deploy-mesh-voice.sh` |
| Blank VOICE tab fix in `manet_manage.py` | queued by maintainer | next release | `scripts/deploy-voice-tab-fix.sh` |
| brcmfmac blacklisted on c370, c5ec | lab policy | never (policy) | `scripts/apply-radio-policy.sh <ip>` |
| `auto_update=n` | temporary | a release contains PR #8 | `scripts/set-auto-update.sh y\|n` |
| OpenVLM mic capture at 90% (lands 91%, +20 dB) | fleet standard since 2026-10-16 | never (policy) | `scripts/set-mic-gain.sh` |
| Band priority: HaLow `hop_penalty` (link-quality / sticky) | lab policy, persistent | never (policy) | `scripts/fleet-status.sh` menu 10 |

Voice config keys added by the patch (`/etc/mesh.conf`, read at start-up):
`voice_playback_pct` (100), `voice_rx_gain_db` (18, max 24),
`voice_ptt_beep_pct` (20, 0 = off). The fleet runs the defaults.

Mic capture is not in `/etc/mesh.conf`; it is the ALSA `Mic` control on the
OpenVLM card. Fleet standard is 90% (the codec steps 1 dB, so it lands on
91% / +20 dB / value 32 of 35), persisted in the card-named section of
`/var/lib/alsa/asound.state` so `alsa-restore.service` restores it at boot.
Note: hot-plugging a fresh board starts at the ~57% default — run
`scripts/set-mic-gain.sh <ip>` after plugging it in.

**Band priority (2.4 GHz vs HaLow).** batman-adv already routes each hop over
its best band by measured link quality, so close nodes ride 2.4 GHz and the
route migrates to sub-1 GHz HaLow as they drift apart. The band swapper
(`scripts/fleet-status.sh`, menu 10) only tunes how eager that migration is,
by the HaLow hardif's `hop_penalty`: **link quality** (penalty 0, the default)
switches the instant 2.4 GHz measures worse; **sticky** (penalty N) holds 2.4
GHz until HaLow is clearly better, reducing flapping for mobile nodes. The
choice is persisted per node in `/etc/manet-band-halow-hop-penalty` and
re-applied at boot by `manet-band-priority.service` (installed on demand), so
it survives reboots and battery swaps. The same menu offers a handoff **test**
that briefly takes one node's 2.4 GHz radio down (it stays meshed over HaLow)
and shows the other nodes' routes flipping to HaLow and back.

**Distance checker.** Menu 11 estimates node ranges from 2.4 GHz RSSI using a
log-distance path-loss model (`d = 10^((TX+G_tx+G_rx−FSPL(1m)−RSSI)/(10N))`,
antenna gains default 0 dBi, exponent N=2 — tune `DIST_G_*`/`DIST_N` at the
top of `scripts/fleet-status.sh`). It prints a from/to matrix; a `no 2.4G` cell
means that pair only links over HaLow (beyond 2.4 GHz range). Ranges are
approximate but good for seeing which nodes are clustered vs far apart. (HaLow
RSSI isn't used because the morse driver reports it on a non-dBm scale.)

---

## Scripts

All take optional node IPs; default is every node in `nodes`.

| Script | Does | Changes nodes |
|---|---|---|
| `scripts/fleet-check.sh` | services, radio roles, HaLow, neighbours, voice | no |
| `scripts/fleet-status.sh` | **base entry point**: interactive menu — full check with per-problem prompts (fix / shell / reboot) plus the other fleet scripts, all or one node | only what you choose |
| `scripts/update-node.sh` | upstream `node-update.sh`, one node at a time, stops on failure | yes |
| `scripts/deploy-mesh-voice.sh` | installs `fixes/mesh-voice-audio/mesh-voice.py`, idempotent | restarts voice |
| `scripts/deploy-voice-tab-fix.sh` | runs `fixes/voice-tab/voice_webGUI_fix.sh`, idempotent | restarts web UI |
| `scripts/apply-radio-policy.sh <ip>` | blacklists brcmfmac, reboots, reruns `radio-setup.sh` | reboots |
| `scripts/set-auto-update.sh y\|n` | sets `auto_update` | yes |
| `scripts/set-mic-gain.sh [pct]` | OpenVLM mic capture, fleet standard 90%; persists ALSA state | the audio card |

`scripts/lib.sh` holds the SSH/sudo helpers; `scripts/remote/` holds the parts
that run on the node.

### Updating to a new release

```
scripts/set-auto-update.sh y           # optional; or update by hand:
scripts/update-node.sh <ip>            # one node first, check it
scripts/fleet-check.sh <ip>
scripts/update-node.sh                 # the rest
```

Then re-apply what the release still lacks:
- `scripts/deploy-mesh-voice.sh`, unless the release contains PR #8. Check
  first: the patched file targets 0.569/0.572 and would revert newer upstream
  voice changes. If upstream moved, rebase the PR branch and regenerate
  `fixes/mesh-voice-audio/`.
- `scripts/deploy-voice-tab-fix.sh`, unless the release has the tab fix. It
  reports "unexpected file contents" and changes nothing if the file moved on.

The brcmfmac blacklist, the ALSA mic level and the band-priority setting all
survive updates (they live outside the files the release replaces).

---

## Known issues

- **HaLow links can stay blocked after a node's HaLow restarts.** A few SAE
  failures make wpa_supplicant block the peer for 300 s (`MESH-SAE-AUTH-BLOCKED`,
  plink `BLOCKED`/`LISTEN`). They cleared on their own within about 10 min after
  c370's HaLow came back. Check with `iw dev wlan2 station dump`; wait one or two
  block periods before intervening.
- **Replacing a HaLow board breaks the next boot (upstream naming).** Upstream
  pins wlan names by MAC. A new board matches no pin, loads before the MT7916,
  takes `wlan0`, and `manet-radio-names.py` refuses to move it ("occupied by an
  unpinned interface"): no mesh, no AP. Recovery: boot without the board, plug
  it in while running (it gets `wlan2`), rerun `radio-setup.sh` to pin its MAC.
  A `Driver=morse_usb` link file does not help, since every name is taken by
  then. Candidate for an upstream issue.
- **"Device busy" after a voice restart.** Occasionally one error pair, then
  recovery on the daemon's 5 s retry. Noted in PR #8.
- **mesh-0825 PTT stuck keyed** once (10 min, nobody on it). Cleared by a voice
  restart; check its headset and cable if it recurs.
- **Repeated names in `talker_names`.** Expected: a restarted node gets a new
  stream ID and receivers keep the old one.
- **HaLow USB cable is a recurring fault (c370, c5ec).** A bad cable makes the
  chip throw `-71`/`-110` then go unreachable (`-19`), 0 peers, within minutes.
  Two different boards failed on the same cable before the swap proved it. When
  a node's HaLow dies this way, swap in a known-good cable first (fixed both
  nodes) before suspecting the board.
- **c370 intermittently drops for seconds, then recovers (hypothesis).** It is
  the only node running HaLow + GPS together, and both share one carrier-board
  USB port through an *unpowered* hub. Suspected cause: the Waveshare CM4
  carrier board can't hold 5 V for both under load, so the USB port brown-outs
  and the node briefly loses the radios (and sometimes SSH). Untested; a
  powered hub or splitting the two devices across ports is the fix to try.

---

## Laptop access

The laptop reaches the fleet by joining a node's EUD AP (`rdt-manet-<id>`),
which puts it in `10.30.2.0/24`. NetworkManager can hop to another known
network of equal priority and cut it off; lower the other profile's
`connection.autoconnect-priority` if that happens.
