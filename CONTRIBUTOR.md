# MANET contributor notes

Project conventions and hard-won lessons that are not written down in the
repository. Read this together with `CLAUDE.md`, `CODING-STYLE.md`, and the
`README.md` for the area you are changing. Where this file and the repository
disagree, the repository's code is the source of truth, and please say so.

## Writing rules for anything that lands in the repo

These apply to code comments, READMEs, the field guide, commit messages, PR
text, and release notes.

- No em dashes, anywhere. Reword, or use a comma, colon, period, or
  parentheses.
- No semicolons in field guide prose. Use two sentences.
- Always use the serial (Oxford) comma: "addresses, channels, and services".
- Keep AI-flavored prose out. Watch for stacked "rather than", every bullet
  starting with a bolded lead-in, "not X, but Y" constructions, and filler
  summaries. Plain, direct sentences.
- READMEs and the guide address the reader directly. They describe how
  things work. They do not record test status, caveats about what is
  unverified, or development history.
- The repository is public. Nothing in it may mention AI tools, coding
  agents, chat sessions, design handoffs, or review transcripts. No
  `Co-Authored-By` or "Generated with" trailers.
- Commit messages are one short line. No body, no bullet list, no trailers.
- Do not put test counts in commits, PRs, issues, or releases. Describe the
  behavior checked and its limits instead.
- Release notes are one flat list of user-visible changes in plain language.
  No internals, no status words.
- The field guide has three tiers per page: Marine (very simple, crayon-style
  drawings), User (what to do and why), Developer (mechanisms, files,
  invariants, failure cases). A behavior change updates every affected tier.
  See `docs/guide/README.md`.
- Never link a tracked document to an untracked file. The local build check
  passes but the GitHub Pages deploy fails. Run
  `python3 docs/guide/build.py --check` from a clean checkout of what you
  push, and confirm the Documentation workflow succeeds. Pushed is not live.

## Engineering principles

- **Frugal on the radio.** No new resident daemons or polling loops for small
  jobs. Prefer event-driven triggers, one-shot units, and fixing the cause at
  the source. Idle CPU on a CM4 matters.
- **OpenBSD-style tools.** Least privilege, a real `usage()` on every script,
  a man page for operator-facing tools, clear diagnostics, correct shebangs.
  Do not reformat existing code for style.
- **Threat model.** Users of a mesh are trusted. The shared admin password
  exists to stop accidents, not attackers. The bug class that matters is
  unauthenticated exposure of a secret. Mesh membership alone must not
  authorize control changes.
- **Wired Ethernet EUDs are the main use case.** Design anything EUD-facing
  for a phone or laptop on the radio's Ethernet port first.
- **ATAK is on by default** on every radio. `atak=n` is the only off switch.
  Keep its idle cost low, but never gate it off for resources.
- **Time.** All radios agreeing on the same time matters more than the time
  being correct. Sync once from internet, GPS, or a peer, then stop. No
  ongoing NTP chatter.
- **Sealed radios.** Radios may be sealed in enclosures. Fixes ship through the
  tools update archive (kernels, firmware, and config migrations included).
  Reflashing is not a maintenance plan. For a conditional fix on already
  deployed radios, use a one-shot, self-deleting systemd unit carried by the
  update.
- **Existing installs are test systems.** Do not add migration code only to
  keep old updaters working.
- **Tests.** Run the suite from the dev venv built by
  `bash MANET/packaging/setup-dev-env.sh` (protobuf 4.21.12, the version the
  radios run). A newer system protobuf accepts generated files the radios
  reject. Regenerate `NodeInfo_pb2.py` only with the venv's protoc.

## Hardware and driver lessons

- **Identify HaLow by its driver, never by frequency.** The Morse driver
  reports sub-1 GHz channels under placeholder 5 GHz channel numbers (for
  example EU channel 1 appears as 5 GHz channel 132, 5660 MHz). Code that
  classified interfaces by `iw` frequency once treated HaLow as a 5 GHz radar
  channel and shut it down. Check `/sys/class/net/IFACE/device/driver`
  (`morse_usb`, `morse_spi`) or the role files in `/var/lib`. Test
  frequency-dependent code with more than one HaLow region and channel.
- **Transmit power:** set it per PHY (`iw phy ... set txpower`). A per-device
  setting sticks until reboot.
- **A missing interface is usually software.** Before blaming hardware, check
  `lspci`, `/etc/modprobe.d`, and `dmesg`. Interface order does not identify
  hardware.
- **Morse USB HaLow can wedge** with `-71 EPROTO`. It needs a real power
  cycle. A warm reboot does not clear it.
- **Always `sync` after editing files on a radio** before it might lose power.
  A power cut has left NUL bytes in a config file written a minute earlier.
- **Waveshare CM4 carriers run near their power limit.** Under-voltage
  warnings on a good supply are normal there and are not the fault.
- **batman-adv does not route to its chosen gateway by itself.**
  `gateway-route-manager.sh` turns batman's gateway pick into the default
  route.
- **apt can replace the MANET kernel.** The kernel packages are pinned. Run
  `apt-get -s` before any apt change on a radio.
- **Platforms.** CM4 is the verification target. Rock 3A works. Raspberry Pi 5
  is deprioritized: do not spend effort on it.
- **Target hardware.** The goal is the Raven carrier board, which
  BuildsByShane is making for the project, with an enclosure in development.
  Do not publish details about it beyond that.

## Web UI audio adjustment (voice tab mic and headset levels)

This is designed but parked. No code sets mic gain today. Most open
questions depend on the Raven board's mic front end, which is not final.

### What is wanted

1. A per-radio **mic type** choice on the voice tab: dynamic (through the
   internal preamp) or electret.
2. **Trim sliders** for mic capture level and headset output level.
3. Settings stored per radio in `/etc/mesh.conf` and reapplied by
   `mesh-voice` on every pipeline build, because the audio chip forgets its
   mixer state when it is replugged.
4. A **live input level meter** during push to talk, with a clipping warning,
   so a hot input is visible instead of silently distorting.
5. Graceful handling of an electret mic that is too hot after the dynamic-mic
   preamp (see the constraint below).

### Hard constraint

The input must work with **stock ComTac headsets**, which ship a 3M PELTOR
MT33 boom mic: dynamic, 150 ohm, about -66 dBV/Pa, no bias. Users plug in the
headset they already own. Never propose a mic swap or a board that assumes an
electret. An external preamp of roughly 25 to 35 dB is required. The MT33
straight into the CM108B cannot work at any setting (about +2.4 dB SNR at
best). With a preamp, about 40 dB SNR was measured with mic boost off, Mic
capture at 0 dB, and AGC off.

### Audio chip controls (CM108B on the OpenVLM board)

- **EEPROM `mic-boost`** (+12 or +22 dB analog). It loads only at chip
  power-on. A USB re-authorize does not reload it, so it cannot back a live
  UI control. Never enable it on a radio that has the external preamp: the
  extra gain stacks on top and clips.
- **ALSA `Mic Capture Volume`**, -22 to +23 dB. The noise floor stayed flat
  from -22 to +11 dB, so it is probably an analog stage before the ADC
  (unconfirmed against a datasheet). If so, it can absorb a hot input that
  reaches the chip unclipped. This is the natural backing for the mic trim.
- **ALSA `Auto Gain Control`.** Works, but costs about 24 dB of noise floor.
  Keep it off with a preamp.
- **ALSA `PCM` playback**, 0 to 37 (-37 to 0 dB). Backing for the headset
  trim.
- OpenVLM CLI 1.0.4 is current. 1.0.2 to 1.0.4 changed only playback
  defaults and documentation.

### The electret problem

An electret produces roughly 15 mV close-talking versus 1 to 3 mV for the
MT33, so it arrives 14 to 24 dB hotter. After about +28 dB of preamp, loud
speech peaks reach 1.5 to 2 V and clip inside a 3.3 V mid-rail preamp.
Software attenuation after that point cannot undo it. The proposed hardware
answer is a preamp gain or bypass switch driven by a spare CM108B GPIO,
selected by the mic-type setting, plus switched electret bias so a dynamic
mic never sees DC. Whether that exists depends on the carrier board.

### Open questions

- Which electret headsets need support, and do they have their own mic amp
  or need bias from the radio?
- The carrier board's preamp gain, output swing, and clipping point.
- Confirm `Mic Capture Volume` is before the ADC (datasheet or a tone test).
- Measure an electret through the preamp at several capture settings.

### Where the code is

- `MANET/node_tools/mesh-voice.py`: the GStreamer pipelines. The TX chain is
  `alsasrc -> level -> valve -> encoder -> payloader -> multiudpsink`. The
  existing `level` element is the natural source for a level meter. TX
  shaping already exists (`voice_highpass_hz`, `voice_eq*`) and retunes live
  on SIGHUP without restarting.
- `MANET/node_tools/manet_manage.py`: the management UI, including the VOICE
  tab and the `/api/voice` endpoints.
- `MANET/node_tools/README.md`, "Push-to-Talk Voice" section: every
  `voice_*` setting, the codec, and the audio hardware notes.
- Voice defaults to the Lyra codec. All radios on a mesh must use the same
  codec to hear each other.
