# MANET lab fleet ops

Operator tooling for a fleet of [MANET](https://github.com/very-srs/MANET)
mesh radios. Not part of the upstream project.

Field guide (developer tier):
https://very-srs.github.io/MANET/index.html?tier=developer

## Quick start

```
cp .credentials.example .credentials   # fill in SSH_USER, SUDO_PASS, SSH_KEY
vi nodes                                # one IPv4 per line
bash fleet-status.sh
```

## Layout

```
fleet-status.sh          entry point: interactive menu
lib/
  common.sh              shared helpers (SSH, color, print, fleet_ips)
  target.sh              pick_target prompt (all or one IP)
actions/
  status.sh              node health check + summary
  update.sh              trigger upstream node-update.sh
  auto-update.sh         toggle auto_update=y|n in mesh.conf
  mic-gain.sh            check/set audio levels (mic, pcm, agc)
  region.sh              region swap (free-form CC, EU/US HaLow plan)
  deploy-voice.sh        deploy patched mesh-voice.py to fleet
remote/
  node-status.sh         runs on-node: emits TAG key=value health facts
  node-update.sh         runs on-node: starts update detached
  set-mic-gain.sh        runs on-node: set + persist mic, pcm, agc
  region-state.sh        runs on-node: read current region config
  region-apply.sh        runs on-node: write new CC + manet-region.py
  deploy-mesh-voice.sh   runs on-node: deploy mesh-voice.py with rollback
files/
  mesh-voice.py          patched mesh-voice.py (voice-rx-gain branch)
nodes                    one IPv4 per line
.credentials             SSH_USER, SUDO_PASS, SSH_KEY (not tracked)
deprecated/              old scripts, reference only
```

## Nodes

Four CM4 radios on 10.30.2.x. Each runs the MANET stack with:

- MT7916 2.4 GHz mesh + MM8108 HaLow mesh on bat0
- MT7916 5 GHz as the EUD access point (off bat0)
- OpenVLM voice board (CM108B)

## Access model

```
laptop --ssh (key)--> radio@node --sudo -S (password)--> root
```

Credentials live in `.credentials` (gitignored). Remote scripts are
copied to the node, run as root, then deleted.
