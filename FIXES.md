# Fix history

Changes applied to the fleet on top of upstream
[very-srs/MANET](https://github.com/very-srs/MANET), newest first. Upstream is
the source of truth: each entry says whether it is upstream, proposed upstream,
or lab-only, and when it can be dropped.

Scripts referenced here live in `scripts/`. All of them read `.credentials` and
`nodes`, and take optional node IPs (default: every node).

---

## 2026-10-17: Status topology enriched with band, RSSI, GPS + map view

**What.** The mesh graph in the node status page now shows which band
carries each link (2.4G blue / 5G purple / HaLow amber), the measured RSSI
on link labels and the node list, and a GPS badge per node. A **Mesh/Map
toggle** adds a second view: with 2+ GPS-fixed nodes it projects them onto
an equirectangular graticule; with fewer (the bench case) it draws a local
radial view placing each peer at its signal-estimated range with range
rings. No tiles or external libs — works offline.

**How.** `mesh-status.py` (upstream file) extended: `run_iw_station_rssi()`
reads per-station signal from each bat0 member; `assemble_status_data()`
adds per-node `link_band`/`link_rssi`/`gps` and per-edge `band`/`rssi`,
plus an `ifaces` map. HaLow is detected by driver (Morse reports an
internal frequency, e.g. 5660 for the 863.5 MHz plan). The map reuses the
distance model from the fleet distance checker.

**Operators + GPS ranges.** A per-node `operator_name` (Identity section in
Node Config) is published mesh-wide as the ATAK user and shown on every
status page: an Operators section lists each node's name + GPS fix + true
GPS (haversine) distance from this node, and a GPS Ranges section lists
pairwise distances. Three commits on the branch: `2308da9` (enrichment),
`bf280b6` (map view), `4465160` (operators + GPS). Deployed to all four
nodes; verified end-to-end (names aggregate cross-node; GPS ranges need
2+ fixes, currently only c370 has one). Awaiting PR to very-srs/MANET.

---

## 2026-10-16: c5ec HaLow restored by replacing its USB cable (hardware)

**Problem.** c5ec's HaLow died the same way c370's had: `-71`/`-110` USB
timeouts then an unreachable chip (`-19`), 0 peers. Its board (`…2f:12`)
failed, and the old board (`…28:7e`) failed on the same cable/port too —
two boards, one cable, so the cable was the suspect (mirrors c370).

**Fix.** New USB cable on c5ec, keeping board `…28:7e`. After it: zero USB
errors, chip answers at 863.5 MHz, 3 HaLow peers. `…28:7e` is now proven
good; c5ec's old cable is confirmed bad (second bad cable after c370's).

**Board placement now.** c5ec: `…28:7e`. Shelf: `…2f:12` (c5ec's original,
untested on a good cable). c5ec's `wlan2` pin re-set to `…28:7e`
(`/etc/systemd/network/10-wlan2.link`), so cold boots are stable.


## 2026-10-16: Mic capture raised to 91% (closest to 90%) fleet-wide

`Mic` capture on the OpenVLM voice card set from 57% (20/35, +8 dB) to
91% (32/35, +20 dB) on all four nodes; 90% is not a reachable step (1 dB
per step). Persisted via `alsactl store` into the card-named section of
`/var/lib/alsa/asound.state` (`state.OpenVLM` on c370/c5ec, `state.Device`
on 0825/5ab8 — the same boards enumerate under different card names), so
`alsa-restore.service` restores it at boot.

Note: hot-plugging the OpenVLM board does not re-run `alsa-restore`, so a
freshly plugged board starts at the ~57% default until re-set or reboot.

Also confirmed: c370 now has both the u-blox 7 GPS (gpsd active, fix) and
the OpenVLM board; `mesh-voice` re-attached to the hot-plugged card
automatically (PR #8 hot-plug handling).

Adopted as the fleet standard for current and future nodes; apply with
`scripts/set-mic-gain.sh` (defaults to 90%).

---

## 2026-10-13: c370 HaLow restored by replacing its USB cable (hardware)

**Problem.** c370's HaLow radio died about 10-20 s after loading (USB `-71`,
then `-110` timeouts and a dead chip, `-19`), on three different Morse boards.
Two of those boards worked in 0825, so the fault was on c370's side.

**Fix.** New USB cable between c370's carrier board and the HaLow board.
After it: zero USB errors over 10+ minutes, HaLow at 863.5 MHz, peers with all
three nodes.

**Board placement then.** c370: `…28:67` (0825's original). 0825: `…28:f5` (the
spare). `…28:7e` (c370's original) went to the shelf, then to c5ec and was
proven good there (see the 2026-10-16 cable entry). Each node's `wlan2` pin
(`/etc/systemd/network/10-wlan2.link`) matches its current board.

**Swapping a HaLow board (procedure).** A new board's MAC matches no pin and a
cold boot can leave the node with no mesh or AP. Instead:
1. Boot the node without the board (or keep it running).
2. Hot-plug the board; it takes `wlan2`.
3. Rerun `radio-setup.sh` (or `scripts/apply-radio-policy.sh <ip>`) to pin it.
4. Expect HaLow links to sit `BLOCKED`/`LISTEN` for up to about 10 min while
   SAE blocks from earlier failures expire.

---

## 2026-10-13: Radio policy, MT7916 only (lab-only)

**Problem.** c370 and c5ec have the CM4's onboard brcmfmac radio. Upstream
`radio-setup.sh` gives a non-mesh radio priority for the EUD AP ("Priority 1:
Use non-mesh interface"), so on those nodes the AP ran on brcmfmac (5 dBm) and
the MT7916 5 GHz radio joined the mesh. Lab policy is that only the MT7916
serves 2.4/5 GHz.

**Fix.** Blacklist brcmfmac in `/etc/modprobe.d/manet-lab-no-brcmfmac.conf`,
reboot, rerun `radio-setup.sh` (upstream's documented retry). It then selects
MT7916 5 GHz (wlan1) for the AP, and bat0 holds 2.4 GHz (wlan0) + HaLow (wlan2).
0825 and 5ab8 have no brcmfmac and needed nothing.

```
scripts/apply-radio-policy.sh <ip>     # reboots the node if brcmfmac is loaded
```

Do not run it against the node whose AP the laptop is on; join another node's
`rdt-manet-<id>` AP first.

**Persistence.** The modprobe file survives node-update.sh (it is not an
upstream file). A reflash loses it.

---

## 2026-10-13: Auto-update disabled (lab-only, temporary)

**Why.** Every upstream update path (uplink dispatcher hook, clock-sync path
unit) runs `node-update.sh --routine` when `auto_update=y`. A new release would
silently replace the patched `mesh-voice.py`.

```
scripts/set-auto-update.sh n          # current state
scripts/set-auto-update.sh y          # once a release contains PR #8
```

---

## 2026-10-13: Voice audio, OpenVLM follow, PTT tones (proposed upstream)

**Upstream PR:** https://github.com/very-srs/MANET/pull/8 (branch
`rdtrob:voice-openvlm-audio`). Drop this fix once a release contains it.

**Problems found.**
1. *No audio when the OpenVLM is plugged in after boot.* `mesh-voice.py`
   picked the ALSA device once at start-up. c5ec booted at 00:43, its OpenVLM
   enumerated at 11:42, and voice stayed on HDMI (`default`) until restarted.
   PTT showed connected, so it looked like a dead link.
2. *Quiet receive.* The CM108B playback mixer powers up at -20 dB and nothing
   in the product sets it. Even at the card's 0 dB maximum, received speech
   measured -40 to -44 dBFS.
3. *No PTT feedback.*

**Changes (`MANET/node_tools/mesh-voice.py`).**

| Change | Config key (`/etc/mesh.conf`) | Default |
|---|---|---|
| Health tick rebuilds onto the OpenVLM card when it appears or is replugged | | |
| Playback mixer set after the device opens, reapplied every 30 s | `voice_playback_pct` | `100` |
| Per-talker software gain + hard-knee limiter before the mixer | `voice_rx_gain_db` | `18` (0-24) |
| Local-only PTT key / roger / refused tones | `voice_ptt_beep_pct` | `20` (0 = off) |
| State file: `audio_card`, `playback_level_pct`, `rx_gain_db`, `mic_db`, `rx_db` | | |

Measured: +18 dB brings speech to about -22 to -26 dBFS and keeps a -3 dBFS
input under full scale; +24 clips.

**Files.** `fixes/mesh-voice-audio/mesh-voice.py` (deployable, md5
`b5f66be4...`) and `mesh-voice-audio.patch` (diff against upstream `a959ed0`,
0.572; the 0.569 daemon is byte-identical to it).

```
scripts/deploy-mesh-voice.sh          # idempotent; re-run after any node-update.sh
```

**Open.** A restart can still log one "device busy" pair and recover within
5 s (0/6 after moving the mixer write, but seen once more on a deploy).

---

## 2026-10-13: Lab audio-level service removed (superseded)

The old `manet-audio-level.service` (from `old/`, an earlier release) was
briefly reinstalled, then removed from all nodes: the daemon now owns the
playback level, and the service also forced Mic to +23 dB, against upstream's
guidance on AGC and noise floor. Mic is back to the card default (+8 dB).

---

## 2026-10-13: Blank VOICE tab on /manage (upstream, next release)

**Problem.** `manet_manage.py` (0.569-0.572) nested `<div id="tab-voice">`
inside the hidden NODE CONFIG pane, so VOICE rendered blank.

**Fix.** `fixes/voice-tab/voice_webGUI_fix.sh` moves the pane out, keeps a
backup until restart succeeds, restarts `mesh-status`, restores on failure.
Idempotent. The maintainer has the fix queued for the next release; drop this
then.

```
scripts/deploy-voice-tab-fix.sh
```

---

## 2026-10-13: Fleet updated to 0.569; mesh-0825 interrupted update

`node-update.sh` (`scripts/update-node.sh`) reported 0.569 as newest on c370,
c5ec and 5ab8. mesh-0825 was on 0.559 with an update interrupted on
2026-10-09 23:34: `/var/lib/manet-update/in-progress` present, recovery
archive missing, so `manet-update-recover`/`-finish` failed at boot and the
updater refused with "Recovery archive is missing; keep in-progress and repair
the installation locally".

**Repair (manual, one-off).** Move the marker aside, re-run the updater, clear
the stale unit failures:

```
sudo mv /var/lib/manet-update/in-progress /root/   # deleted once verified
scripts/update-node.sh 10.30.2.61
sudo systemctl reset-failed manet-update-finish manet-update-recover
```

It installed 0.569 cleanly. The update log line
`manet-mt7916-firmware: disabled; stock firmware selected` is expected: the
patched MT7916 firmware is only used with `positioning=y`.

---

## 2026-10-13: Operational issues found (not code fixes)

- **mesh-0825 PTT stuck keyed** about 10 min with nobody on it; cleared by
  restarting `mesh-voice`. Cause unknown (button, cable, or missed HID release).
- **Power loss**: c370, 0825 and 5ab8 rebooted at the same second (16:19:53),
  and 0825 again at 18:23. Confirmed cause: all nodes share one bench power
  supply, and handling a twist-lock connector trips its short-circuit
  protection.
- **c370 HaLow**: three boards failed on c370 while two of them worked in
  0825; resolved by a new USB cable (entry above). The board swaps exposed
  the upstream MAC-pin naming problem: c370 booted twice with no mesh or AP.
  A `Driver=morse_usb` link file (`fixes/radio-policy/12-manet-lab-halow.link`)
  was tried and did not help; it was removed and is kept only as a record.
- **Laptop NetworkManager** hopped from `rdt-manet-c370` to `rdt-guests`
  (equal priority), cutting the laptop off the mesh.
