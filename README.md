# _LAB-FLEET-OPS — lab-only fleet operations (NOT part of the MANET product)

> ⚠️ **This directory must never be shipped or committed into the MANET
> *product* repo.** It is git-ignored there (`_LAB-FLEET-OPS/` in the product
> `.gitignore`). It collects the scripts, tooling and notes used to bring up and
> run the **3-node CM4 lab fleet** on unrestricted spectrum. The leading
> underscore sorts it to the top so it's easy to spot.
>
> **Off-box backup:** this directory is versioned on its own in the private
> Gitea repo **`robdoe/manet-lab-fleet-ops`**
> (`https://gitea.local.robertdoes.com/robdoe/manet-lab-fleet-ops`). That repo is
> the nested `.git` here; it is separate from the MANET product repo. Real
> `.credentials` is git-ignored even there (see `.credentials.example`).
>
> Contents are original work for this fleet plus useful files copied from the
> two non-primary repos (`~/projects/srs-manet`, `~/dev/mesh-voice-fleet-rollout`).

## The fleet

| Node | Hostname | Role notes |
|------|----------|-----------|
| 10.24.17.14 | mesh-5ab8 | 2× MT7916 (2.4 mesh + 5 GHz AP), USB HaLow, OpenVLM |
| 10.24.17.17 | mesh-c370 | 3 radios: MT7916 2.4 mesh + MT7916(idle) + onboard brcmfmac 5 GHz AP, USB HaLow, OpenVLM |
| 10.24.17.20 | mesh-c5ec | 3 radios: MT7916 2.4 mesh (wlan0) + MT7916 idle (wlan1, masked) + onboard brcmfmac 5 GHz AP (wlan3), USB HaLow (wlan2), OpenVLM. Active 3rd node; supersedes the retired .18 mesh-0825 board. |

> Historical: **.18 mesh-0825** was the original 3rd node — its CM4 corrupted the
> HaLow SAE handshake (see saga below) and was retired in favour of .20 mesh-c5ec.

- LAN/uplink is `10.24.17.x` (external DHCP). **Internal mesh stays `10.30.2.0/24`.**
- SSH is key-based (`~/.ssh/mesh-fleet`), config in `~/.ssh/mesh-fleet.conf`
  (`ssh mesh-5ab8` works). Creds in `.credentials` (radio / sudo pass).
- IPs drift across reboots — every mutating script verifies hostname first.

## What was configured

- **868 MHz HaLow mesh, 1 MHz** (867.5 MHz, EU op_class 66 **ch9**), duty-cycle off,
  MPSW off, enslaved to bat0, `mesh_plink_timeout=0`. 1 MHz gives the best
  range/sensitivity.
  - **TX power on 868 is HARD-clamped to 15 dBm** by the morse firmware (ETSI
    25 mW e.r.p. on 863-868). `tx_max_power_mbm`, `iw ... set txpower`, and
    `reg 00` do **not** move it — verified on hardware (stays 15.00 dBm at
    2400/3000 requests). Lifting it needs a modified BCF or a dot11ah rebuild.
  - **Channel 1 (863.5 MHz) is DISABLED** by the EU self-managed reg domain;
    only 867-868 @ 1 MHz is allowed. Correcting an earlier note: **2 MHz IS
    available on 868** (ch2 @ 864.0, ch6 @ 866.0, op_class 67) — the reg domain
    exposes `(863-865 @ 2)` and `(865-867 @ 2)`. 1 MHz is still preferred for range.
  - **Root cause of the fleet-wide 868 outage (fixed 2026-09-01):** the live
    `wpa_supplicant-wlan2-s1g.conf` on .14 and .17 had drifted to a **US 8 MHz**
    config (op_class 71, ch12, 908 MHz) over EU firmware → `Frequency ... not
    allowed for AP mode` → mesh never joined; the fleet ran on 2.4 GHz alone.
    Rewritten to EU 1 MHz ch9; both nodes now peer (plink ESTAB, -54/-57 dBm).
- **Open 2.4/5 GHz** via srs-manet's `regulatory_open00.db` + `reg 00`
  (30 dBm, no DFS/NO-IR). 5 GHz = EUD AP; 2.4 GHz + 868 = mesh. On mesh-c370 the
  redundant 5 GHz mesh (self-interfering with its own AP, 0 peers) was removed
  (on mesh-c370: `wpa_supplicant@wlan1` masked + its `wlan1(-lobby).conf` moved to
  `.disabled-lab`, so `mesh-boot-lobby` can't re-mesh it; `wlan1` stays down).
- **Max TX power**: 5 GHz AP 30 dBm, 2.4 mesh 23–30 dBm (chip-clamped),
  HaLow 15 dBm.
- **Audio playback maxed** (2026-09-01): OpenVLM USB card `PCM` playback was at
  −20 dB (46%) — quiet on receive. Set to **0 dB (100%)** on all 3 nodes and
  persisted with `alsactl store` (restored each boot by `alsa-restore.service`,
  `WantedBy=sound.target`). Mic left at +8 dB (57%) with AGC on. Nothing in the
  product sets ALSA levels via amixer, so `asound.state` is the persistence path.

## Scripts (run from this directory)

| Script | Purpose |
|--------|---------|
| `lib.sh` | shared SSH/sudo/put_file/verify helpers (safe against cached-sudo password leak + `&&` sudo-scope bugs) |
| `.credentials` | `SSH_USER` / `SUDO_PASS` / `SSH_KEY` |
| `setup-node.sh <ip> <host>` | full bring-up: runs 00 → 10 → 20 in order |
| `00-fix-usb-host.sh <ip> <host>` | enable CM4 USB host (`otg_mode=1`) so HaLow + OpenVLM enumerate; reboots |
| `10-deploy-halow-1mhz.sh <ip> <host>` | 868 HaLow mesh (1 MHz, max power, duty/MPSW off); writes deterministic `wlan2.link`, reboots if naming needs it |
| `20-open-24-5ghz.sh <ip> <host>` | install open00 regdb, force reg 00 persistently, open AP (drop country_code + `ieee80211d=0`), max TX power |
| `30-fix-wlan-naming.sh <ip> <host>` | install the **boot-time** deterministic wlan-naming service (`node-files/`), closing the morse↔brcmfmac swap race for good |
| `node-files/` | on-node payloads: `manet-fix-wlan-naming.sh` + `manet-wlan-naming.service` |
| `status.sh [ip ...]` | one-shot health check of all nodes |
| `voice-benchmark.py` | Lyra-vs-Opus codec benchmark (bitrate + CPU + RSS) via the GStreamer Python bindings (nodes have no `gst-launch-1.0`); scp to a node and `python3` it |
| `regulatory_open00.db(.txt)` | the open world-domain regdb + source |
| `regdb-tools/` | from srs-manet: `regdb_read.py`, `check_open.sh` (on-node L1–L5 verifier), `halow_range.py` (868 range calc) |
| `fleet-tools/` | from mesh-voice-fleet-rollout: `check-halow.sh`, `fleet-status.sh`, `recover-wlan-swap.sh`, `shutdown-node.sh`, `shutdown-all-nodes.sh` |

## Gotchas learned (read before touching HaLow)

- **CM4 USB was disabled fleet-wide** by a provisioning bug: the USB-enable line
  sits under `[cm5]` in `config.txt` while these are CM4s, and `otg_mode=1` under
  `[cm4]` is commented out. Nothing on USB (HaLow, OpenVLM) enumerates until fixed
  (`00-fix-usb-host.sh`).
- **Interface-name swap (SOLVED at boot — see below):** when the USB HaLow
  enumerates late, udev's `.link` renames can't resolve a 2-cycle swap (morse
  ↔ brcmfmac on `wlan2`↔`wlan3`, or morse grabbing `wlan0`), so a node can boot
  with the s1g supplicant/hostapd bound to the wrong radio. MAC-pinning **all**
  radios (`10-wlan0/1/2/3.link`) helps but is still a **race** — seen live on both
  .20 and .17. **After a HaLow module swap the morse MAC changes**, so the
  matching `10-wlan2.link` must be updated to the new MAC (e.g. .20 =
  `0c:bf:74:00:2f:12`, .17 = `0c:bf:74:00:28:67`).
- **Spare MT7916 re-enslaved into bat0:** the enslave-watch enslaves every iface
  in `/var/lib/mesh_if` + `/var/lib/halow_if`. If the idle radio (wlan1) is left
  in `/var/lib/mesh_if` it keeps getting pulled into bat0 on boot (harmless while
  down, but untidy). Fix: `mesh_if` = `wlan0` only (matches .17). Done on .20.
- **Reg 00 didn't persist** with the modprobe option alone — crda (`/etc/default/crda`)
  and the wpa `country=` lines re-assert the old domain on boot. `20-open-24-5ghz.sh`
  fixes all of them and installs `manet-regdom-open.service` (authoritative
  `iw reg set 00` late in boot).
- **HaLow peering / SAE:** do **not** `systemctl restart` the s1g supplicant on a
  node with an *established* mesh link — teardown triggers a Morse firmware
  ENODEV stuck state that needs a reboot. To (re)form the mesh, reboot the nodes
  (clean, block-free) and wait 1–2 min; SAE mutual-blocks last 300 s. Bring a new
  node up *after* the others have settled.

## Boot-time deterministic wlan naming (`30-fix-wlan-naming.sh`, 2026-09-01)

The permanent fix for the interface-name swap race. Two pieces, installed by
`30-fix-wlan-naming.sh` and deployed on all 3 nodes:

- **`/usr/local/bin/manet-fix-wlan-naming.sh`** — reads the MAC→name mapping from
  `/etc/systemd/network/10-wlan*.link`, waits (≤40 s) for every pinned radio to
  enumerate (the USB morse is the slow one), then forces the actual interfaces
  to match. It parks every mismatched iface under a `tmp-<name>` first, so any
  permutation — including a full 2-cycle swap — resolves cleanly.
- **`manet-wlan-naming.service`** — oneshot, `Before=` the wireless services
  (`wpa_supplicant-s1g-wlan2`, `hostapd`, `wpa_supplicant@wlan0`,
  `batman-enslave[-watch]`), so naming is correct *before* anything binds a radio.

This makes `fleet-tools/recover-wlan-swap.sh` a manual fallback rather than a
routine necessity. **Verified 2026-09-01 on mesh-c370:** a simulated wlan2↔wlan3
swap was fully corrected by the script (parked to temp, renamed back), and a
real reboot ran the service before the wireless stack and came up clean
(wlan2=morse, wlan3=brcmfmac, 868 ESTAB=2). Enabled on all 3 nodes.

## 868 TX-power / BCF experiment (step 2) — what was tried and why it failed

Goal: lift the **15 dBm** 868 clamp for more range. Findings (2026-09-01):

- The clamp is **not** the Linux regdb (which allows 30 dBm on 867-868) — it's the
  **BCF power table**. The BCF (`bcf_boardtype_0807.bin`) is a standard **ELF**
  with per-region sections (`.regdom_EU/US/GB/AU/CA/JP`). In `.regdom_EU`, each
  channel record is TLV: `c9`=centre-freq(kHz LE), `ca`=BW(01=1MHz), `cc`=max
  power in **0.25 dBm units**. EU/GB = `60` = **15.00 dBm**; the *same silicon*
  runs 21.75 dBm in AU/US regdoms → the PA can do ~22 dBm.
- Patched `.regdom_EU` `cc` 60→88 (22 dBm) with `objcopy`/`dd`. The whole-file
  CRC is standard zlib crc32 (matched the driver's load log), **but each regdom
  section has a 4-byte firmware checksum** that is *not* any standard
  crc32/crc32c/fnv/sum over any byte range tried. Deploying the patched BCF →
  `morse_firmware_init failed: -5` (FW rejected the regdom), no `wlan2`.
- **Byte-patching the BCF is not viable without Morse's `bcftool`** (recomputes
  that fw checksum). → to exceed 15 dBm, get the Morse SDK/`bcftool`, or rebuild
  `dot11ah`. For range, antennas + mast height beat the power bump anyway
  (`halow_range.py`: 15.5 km @ 15 dBm/6 dBi/5 m masts vs 11.7 km @ 22 dBm/2 m).
- ⚠️ **The failed init damaged .20's original HaLow module**: it wedged the USB
  chip (needed a cold power-cycle to re-enumerate), and afterwards the chip could
  no longer complete SAE (`MESH-SAE-AUTH-FAILURE` + `SAE: Failed to retransmit`
  to both peers) despite byte-identical config, `health: success`, warm reboots,
  and a clean block-free 3-node reform. **A module swap fixed it** (new module
  peered instantly) — proving the module, not the CM4/platform, was at fault.
  **Do not byte-patch a BCF on hardware you can't afford to brick.**

## The mesh-0825 (.18) HaLow saga — hardware, not software

`.18` **never** kept a HaLow SAE peering with the .14↔.17 pair, while those two are
rock-solid. Ruled out, exhaustively: config (byte-identical md5), firmware + wpa
binary (identical md5), `sae_pwe` 0/1/2, many clean reboots, block-expiry waits,
and a **HaLow module swap** (the new module *did* establish once — so the module
is good). The fault is **intrinsic to that CM4 unit** (marginal USB/platform
corrupting the SAE handshake). → **CM4 board being swapped.** Its HaLow config is
left in place but the supplicant is disabled; after the board swap re-enable with:

```bash
ssh mesh-0825 'sudo systemctl enable --now wpa_supplicant-s1g-wlan2 halow-txpower-wlan2'
# or just re-run:  ./setup-node.sh <ip> mesh-0825
```

## Voice (task 2) — DONE

Voice is **not** a lab task — it ships in the product: `MANET/node_tools/mesh-voice.py`
(`mesh-voice.service`) + `MANET/lyra_arm64/` (Lyra plugin + models), documented in
`MANET/node_tools/README.md § Push-to-Talk Voice`. The old `_LAB-FLEET-OPS/voice/`
mirror (from mesh-voice-fleet-rollout) was **redundant and has been removed**.

Live state (2026-09-01): `mesh-voice.service` active/enabled on all 3 nodes, codec
**lyra fleet-wide** (no fallback split), Lyra plugin installed, PTT via OpenVLM
(`ptt_mode=openvlm`, `/dev/hidraw0`), confirmed RX between nodes at **0.0% loss**
(~17.5 kbps on air, 3 frames/packet @ 60 ms). Playback level fixed to 0 dB (above).

### Lyra vs Opus benchmark (CM4, `voice-benchmark.py`, 2026-09-01, all 3 nodes)

| Codec | Config | Payload bitrate | enc+dec CPU (30 s audio) |
|-------|--------|-----------------|--------------------------|
| **Lyra** | 6000 bps @ 16 kHz | **6.0 kbps** (~17.5 kbps on air) | **~15.5 s = ~52 % of one core** |
| **Opus** | 32000 bps @ 48 kHz, 20 ms, FEC | 32.4 kbps (~44 kbps on air) | ~3.0 s = ~10 % of one core |

**Verdict:** Lyra is the right default here — it's **~5.4× lighter on the wire**,
which is what matters on the ~17.5 kbps-usable 868 HaLow link (Opus's ~44 kbps
on-air would swamp it). The cost is **~5× more CPU** (neural TFLite/XNNPACK
encoder+decoder), but ~52 % of *one* core on a quad-core CM4 is fine. Opus is the
fallback for CPU-bound or bandwidth-rich setups. RSS is ~46 MB either way (process
baseline; per-talker branch cost is the ~5.3 MB lyra / 0.6 MB opus in node_tools).

## Reboot persistence test (task 3 — DONE, 2026-09-01)

All three nodes rebooted **together** (block-free SAE reform) and re-checked with
`status.sh`. Everything we changed this session **persisted** and the full
dual-band mesh re-formed on its own:

| Check | .14 mesh-5ab8 | .17 mesh-c370 | .20 mesh-c5ec |
|-------|:---:|:---:|:---:|
| 868 HaLow 867.5 MHz/1 MHz/15 dBm | 2 ESTAB | 2 ESTAB | 2 ESTAB |
| 2.4 mesh (wlan0) | ✅ | ✅ | ✅ |
| 5 GHz EUD AP | ✅ wlan1 | ✅ wlan3 | ✅ wlan3 |
| bat0 = wlan0+wlan2, 4 neigh | ✅ | ✅ | ✅ |
| reg domain 00 | ✅ | ✅ | ✅ |
| ALSA PCM 100% / 0 dB | ✅ | ✅ | ✅ |
| wlan2 = morse (naming pin) | ✅ | ✅ | ✅ |
| spare wlan1 out of bat0 | n/a | ✅ | ✅ |
| 8/8 services active | ✅ | ✅ | ✅ |

Services verified active on every node: `hostapd`, `wpa_supplicant@wlan0`,
`wpa_supplicant-s1g-wlan2`, `batman-enslave-watch`, `mesh-status`, `node-manager`,
`mesh-voice`, `manet-regdom-open`. `status.sh` node list updated .18 → .20.

## Session resume (for the next agent / window)

- **Full transcript** (pi): session id `01a05a17-2fcd-7f46-99e5-34d49a79343c`,
  stored under `~/.pi/agent/sessions/--home-robdoe-dev--/`. Resume from
  `/home/robdoe/dev` with `pi -c` (latest) or `pi --session 01a05a17`.
- **Source of truth** is *this README* — read it top to bottom to reconstruct the
  fleet without the transcript. Key runtime facts: 3 nodes (.14/.17/.20), mesh
  SSID `A-PN-MN`, internal mesh `10.30.2.0/24`, creds in `.credentials`, helpers
  in `lib.sh`, health via `status.sh`. Open item: 868 >15 dBm needs Morse `bcftool`.

## Still pending

- Nothing outstanding except the vendor-gated 868 power lift (Morse `bcftool`).
