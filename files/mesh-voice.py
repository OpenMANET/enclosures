#!/usr/bin/env python3
"""Mesh push-to-talk voice over multicast RTP.

GStreamer handles codecs, RTP, jitter buffers and packet fanout. Python reads
PTT, updates peers, adapts packing to loss and writes web UI state.

    TX  alsasrc -> level -> valve -> <enc> -> <pay> -> multiudpsink
    RX  udpsrc  -> rtpbin -+-> <depay> -> <dec> -\
                           +-> <depay> -> <dec> --+-> audiomixer -> alsasink
                                    (one branch per talker)

rtpbin gives each SSRC a jitter buffer to keep sequence numbers separate.
audiomixer combines the decoded audio. PTT gates transmit.
voice_half_duplex=y blocks transmit while receiving.

voice_codec defaults to Lyra, using libgstlyra.so and model weights. Opus uses
stock elements. All nodes must use the same codec. Alfred radio_state stages
mesh-wide changes. Missing Lyra plugins or weights cause a logged fallback
to Opus, which cannot exchange voice with Lyra peers.

Lyra packing adapts to receive loss. Smaller packets use more airtime but
shorten lost audio intervals for concealment. See _tick_packing.

Addressing follows OpenMANET: one multicast group, port 38801 + (n-1)*2 for
channel n. Port+1 is reserved for RTCP. Channel changes need no IGMP rejoin.

Transport constraints:
  * Set ttl-mc explicitly. The default TTL of 1 prevents multihop delivery.
  * DSCP 48 (CS6) gives UP 6 / WMM AC_VO through batman-adv. In
    net/batman-adv/main.c, batadv_interface_tx() calls
    batadv_skb_set_priority(), setting skb->priority to 256 + (TOS >> 5).
    mac80211 uses that priority before DSCP, bypassing Linux 6.12+ RFC 8325
    mapping in cfg80211_classify8021d(). EF (46) gets UP 5 / AC_VI on this
    path. Confirm on-air priority when changing it.

Unicast redundancy defaults off. With multicast_forceflood disabled and at
most multicast_fanout listeners (default 16), batadv_mcast_forw_mode_by_count()
returns BATADV_FORW_UCASTS: one unicast frame per listener, with 802.11 ACKs
and retries. Bench result on wlan2: 200 multicast packets, 200 unicast frames
to the peer, no broadcast frames above baseline. Userspace copies duplicate
this traffic. They remain useful above multicast_fanout or with
multicast_forceflood enabled, when batman-adv selects BATADV_FORW_BCAST.
The Alfred registry supplies peers, including those that never transmit.

Without listeners, batadv_mcast_forw_mode() returns BATADV_FORW_NONE and
drops multicast at the sender. udpsrc joins with auto-multicast=true.
Startup traffic may pause until joins propagate.

_note_pipeline_error backs off bus errors. The stall watchdog checks idle
audio flows and multicast membership. See the watchdog constants below.
Reads /etc/mesh.conf and writes /run/mesh-voice.json.
"""

import errno
import glob
import json
import math
import os
import random
import re
import signal
import socket
import subprocess
import sys
import threading
import time

from manet_registry import parse_registry

# mesh-voice.service is enabled on every node. Exit successfully if the
# GStreamer runtime is missing, so systemd's Restart=on-failure does not retry
# the failed import every ten seconds. Voice remains unavailable on that node.
try:
    import gi

    gi.require_version("Gst", "1.0")
    gi.require_version("GstNet", "1.0")
    from gi.repository import Gio, GLib, Gst, GstNet  # noqa: E402
    GST_IMPORT_ERROR = None
except (ImportError, ValueError) as _exc:      # ValueError: gi typelib missing
    GST_IMPORT_ERROR = _exc

MESH_CONF = os.environ.get("MANET_MESH_CONF", "/etc/mesh.conf")
REGISTRY_FILE = os.environ.get("MESH_REGISTRY_FILE", "/var/run/mesh_node_registry")
STATE_FILE = os.environ.get("MESH_VOICE_STATE", "/run/mesh-voice.json")

# One multicast group for every talk group. The port selects the channel.
TALK_GROUP_ADDR = "239.192.41.1"
TALK_GROUP_BASE_PORT = 38801
TALK_GROUP_PORT_STRIDE = 2
TALK_GROUP_MAX = 32

# Both codecs use payload type 111, the conventional dynamic PT for Opus.
# The PT cannot distinguish Lyra from Opus, so codec selection must be
# coordinated across the mesh. See apply_voice_codec() in manet_radio.py.
RTP_PAYLOAD_TYPE = 111
SAMPLE_RATE = 48000
# Opus uses a 48 kHz RTP clock at any input rate. Lyra requires 16 kHz input and
# a 16 kHz RTP clock.
OPUS_CLOCK_RATE = 48000
LYRA_RATE = 16000
FRAME_MS = 20

# Lyra frame size on the wire, by bitrate. Fixed by the codec, and the reason
# the receiver can recover the packet geometry without any signalling.
LYRA_FRAME_BYTES = {3200: 8, 6000: 15, 9200: 23}

# --- adaptive packing --------------------------------------------------------
# Thresholds for the frames-per-packet controller. See _tick_packing.
BATCTL = "/usr/sbin/batctl"
PACKING_MIN = 1                  # 20 ms packets: most robust, most airtime
PACKING_MAX = 3                  # 60 ms packets: cheapest, audibly bursty
PACKING_DEFAULT = 2              # 40 ms: the measured knee
PACKING_TICK_SEC = 2
PACKING_LOSS_HIGH_PCT = 5.0      # above this, shrink packets (fast)
PACKING_LOSS_LOW_PCT = 1.0       # below this, reclaim airtime (slow)
PACKING_UP_HOLD_SEC = 30         # how long clean before stepping back up
PACKING_MIN_SAMPLE = 25          # packets per window needed to judge at all
PACKING_LINK_FLOOR_MBPS = 2.0    # below this, treat loss as congestion

# OpenVLM is a C-Media CM108B. The PTT switch lands on the codec's GPIO3 and is
# read from USB HID input reports: there is no SBC GPIO involved.
OPENVLM_VID = 0x0D8C
OPENVLM_PID = 0x0012
HID_REPORT_LEN = 5
HID_GPIO3_MASK = 0x04  # IR1 bit 2: PTT
HID_GPIO1_MASK = 0x01  # IR1 bit 0: OpenVLM identity strap
# CM108B datasheet 7.4: IR1[3:0] only reflects live GPIO when IR0[7:6] == 0.
HID_IR0_VALID_MASK = 0xC0

# The CM108B playback mixer powers up at -20 dB of a 0 dB maximum, and no
# restore covers a USB card that enumerates late, so receive is quiet unless
# the daemon sets it. Generic CM108 dongles call the control Speaker.
PLAYBACK_CONTROLS = ("PCM", "Speaker")
AMIXER = "amixer"
AMIXER_TIMEOUT_SEC = 3

# --- receive gain -------------------------------------------------------------
# The card's 0 dB playback maximum leaves headset speech quiet, so each talker
# branch can add software gain ahead of the mixer. A hard-knee limiter follows
# it in float, linear below THRESHOLD (-6 dBFS) and squashing above it by
# RATIO. Measured on a 1 kHz tone at 16 kHz, peak dBFS out:
#   gain   -30 dBFS in   -3 dBFS in
#   +12    -18.0         -4.2
#   +18    -12.0         -2.4
#   +24     -6.0          0.0 (clips)
RX_GAIN_DB_DEFAULT = 18.0
RX_GAIN_DB_MAX = 24.0
RX_LIMIT_THRESHOLD = 0.5
RX_LIMIT_RATIO = 0.05

# Size the warm decode-branch table from the registry. Each Lyra branch uses
# about 5.3 MB. The cap of 64 uses roughly 340 MB on a 3.7 GB node.
# VOICE_TALKER_HEADROOM reserves room for new nodes before eviction.
VOICE_MAX_TALKERS_HARD = 64
VOICE_TALKER_HEADROOM = 2

# Admission happens before rtpbin, whose max-streams only bounds its demux,
# not RTPSession's source records. Keep slots until native source removal.
VOICE_MAX_SOURCES = 128
VOICE_MAX_PARKED = 64
VOICE_SOURCES_PER_IP = 4
VOICE_ADMISSION_BURST = 64
VOICE_ADMISSION_PER_SEC = 1.0
VOICE_IP_BURST = 2
VOICE_IP_ADMISSION_SEC = 30
# GStreamer removes inactive sources after five deterministic RTCP intervals,
# at its next maintenance pass. Fifteen minutes preserves conversation gaps
# and the default ten-minute beacon. Fix bandwidth so source count cannot
# stretch that interval. No RTCP output is connected or transmitted here.
VOICE_RTCP_INTERVAL_SEC = 180

# Muted presence beacon duration in ms. Establishes the RTP source at receivers
# before speech begins.
BEACON_MS = 140

PTT_DEBOUNCE_MS = 150
HALF_DUPLEX_HOLD_MS = 500
RX_IDLE_MS = 500

# Back off retries for missing or unavailable audio hardware. Measured with 5 s
# retries: about 73 journal lines/minute (105k/day), adding card writes and
# displacing other persistent logs.
PIPELINE_RETRY_BASE_SEC = 5
PIPELINE_RETRY_MAX_SEC = 300
# Seconds without another error before the retry delay resets to its base.
PIPELINE_RETRY_RESET_SEC = 900
REGISTRY_POLL_SEC = 30
STATE_WRITE_SEC = 2

# --- stall watchdog ----------------------------------------------------------
# Catches audio stalls without bus errors: a CM108B USB audio reset or a
# multicast membership lost when br0 is rebuilt. Counts flows that run while
# idle. tx_packets and rx_packets stay flat between PTT key-ups, so they cannot
# detect these stalls.
#   tx  capture buffers at the valve, upstream of the drop
#   rx  playback buffers, fed by the silent mixer input
# Both run at about one buffer per 20 ms. A few seconds idle means a stall.
HEALTH_TICK_SEC = 5
# Seconds without flow before a stall. Allows scheduling delays on a busy CM4.
VOICE_STALL_SEC = 15
# Grace after a pipeline is told to play, before its flow is judged at all.
VOICE_STALL_GRACE_SEC = 20
# Stall threshold within the window below. Repeated stalls trigger a process
# restart for fresh ALSA handles, sockets, multicast membership and Lyra models.
VOICE_STALL_MAX_RESTARTS = 3
VOICE_STALL_WINDOW_SEC = 600
# Check membership directly. Receive traffic cannot distinguish a lost join from
# a quiet talk group.
IGMP_PROC = "/proc/net/igmp"

# Loopback suppression retry interval in ms. multiudpsink creates its socket
# during asynchronous startup. Retry until it exists to prevent delayed playback
# of this node's own voice.
LOOPBACK_SUPPRESS_MS = 200
LOOPBACK_SUPPRESS_TRIES = 25          # 5 seconds' worth

# --- transmit high-pass ------------------------------------------------------
# Remove hum, wind and handling noise below about 80 Hz before encoding. Opus
# filters some of this internally. Lyra has no equivalent. audiocheblimit is in
# the installed gstreamer1.0-plugins-good package. Keep poles=4 and type=1 for
# stability and the required stopband response:
#   8 poles: stable at 16 kHz (-66 dB at 20 Hz). At 48 kHz, the 80 Hz
#            normalized frequency is 0.0017 and coefficients lose precision:
#            flat +4.9 dB gain from 20 Hz to 3 kHz, with no filtering.
#   type 2: cutoff is the stopband edge, with ripple in the stopband.
#           At 60 Hz, measures -0.3 dB against type 1's -9.6 dB.
# Measured response at 80 Hz, identical at 16 and 48 kHz:
#   20 Hz -53.2 dB | 50 Hz -17.9 dB | 80 Hz  0.0 dB | 300 Hz +0.2 dB
#   40 Hz -27.2 dB | 60 Hz  -9.6 dB | 120 Hz 0.0 dB | 3 kHz   0.0 dB
# Measured CPU cost: 0.03% of realtime at 16 kHz on a dev box. At 20 times that
# cost, the estimate remains below 1% of one CM4 core.
HIGHPASS_POLES = 4
HIGHPASS_TYPE = 1
HIGHPASS_MAX_HZ = 400

# --- transmit band-pass ------------------------------------------------------
# voice_lowpass_hz selects audiochebband from the same plugin. It splits its
# poles between both edges: 4 poles give only -3.5 dB at 60 Hz. Unlike
# audiocheblimit, 8 poles remain stable at 48 kHz. Measured at 8 poles, 100-4000
# Hz, nearly identical at 16 and 48 kHz:
#   40 Hz -36.9 dB | 100 Hz -0.2 dB | 1 kHz -0.1 dB | 5 kHz  -7.8 dB
#   60 Hz -20.6 dB | 300 Hz -0.0 dB | 4 kHz -0.2 dB | 6 kHz -17.2 dB
# Off by default. Compare speech quality across headsets before enabling.
BANDPASS_POLES = 8

# equalizer-3bands centres: 100 Hz, 1100 Hz and 11 kHz. Gains are live-settable
# from -24 to +12 dB. Force the high band to 0 below EQ_HIGH_MIN_RATE. At Lyra's
# 16 kHz, 11 kHz exceeds Nyquist and causes broadband gain:
#   band2=+6   1 kHz tone rises 2.6 dB, with 4608 clipped samples
#   band2=-12  1 kHz tone falls 6 dB
EQ_HIGH_MIN_RATE = 24000              # 11 kHz band needs headroom above 22 kHz
EQ_BAND_HZ = (100, 1100, 11000)
EQ_MIN_DB = -24.0
EQ_MAX_DB = 12.0
# The shaping settings a reload re-reads, as opposed to the codec and device
# settings that stay start-up only.
SHAPE_KEYS = ("highpass_hz", "lowpass_hz", "eq", "eq_low", "eq_mid", "eq_high")


def log(msg):
    """Print a timestamped voice message and flush it immediately."""
    print("[%s] - VOICE: %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg),
          flush=True)


def now_ms():
    """Return monotonic milliseconds for PTT and receive timing."""
    return int(time.monotonic() * 1000)


def sd_notify(msg):
    """Send a systemd notification when NOTIFY_SOCKET is set.

    Use one Unix datagram because nodes do not install python3-systemd.
    Do nothing when NOTIFY_SOCKET is unset, including shell invocations.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]        # abstract namespace
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.sendto(msg.encode(), addr)
        finally:
            sock.close()
    except OSError:
        pass


def group_hex_forms(addr):
    """Return both byte orders of a dotted-quad as uppercase hex.

    /proc/net/igmp prints a native-endian u32. For 239.192.41.1, match
    0129C0EF on little-endian nodes and EFC02901 on big-endian nodes.
    """
    packed = socket.inet_aton(addr)
    return {packed.hex().upper(), packed[::-1].hex().upper()}


TALK_GROUP_HEX = group_hex_forms(TALK_GROUP_ADDR)


def igmp_groups(iface):
    """Return joined multicast groups, or None when membership is unknown.

    An unreadable proc file or an absent interface returns None. Callers must
    not treat either as a lost membership.
    """
    groups = None
    current = None
    try:
        with open(IGMP_PROC, "r") as fh:
            for line in fh:
                if not line.startswith((" ", "\t")):
                    # Device header: "2\tbr0       :     3      V3"
                    fields = line.split()
                    current = fields[1].rstrip(":") if len(fields) > 1 else None
                    if current == iface:
                        groups = set()
                    continue
                if current != iface:
                    continue
                fields = line.split()
                if fields:
                    groups.add(fields[0].upper())
    except (OSError, AttributeError):
        return None
    return groups


# --- configuration -----------------------------------------------------------

def read_kv_file(path):
    """Parse a key=value file, tolerating comments, blanks and quoting."""
    out = {}
    try:
        with open(path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                out[key.strip()] = val.strip().strip("'\"")
    except OSError:
        pass
    return out


def conf_int(conf, key, default, low=None, high=None):
    """Read an integer setting, defaulting on invalid or out-of-range input."""
    try:
        val = int(conf.get(key, "").strip())
    except (ValueError, AttributeError):
        return default
    if low is not None and val < low:
        return default
    if high is not None and val > high:
        return default
    return val


# Allowed characters for config values interpolated into Gst.parse_launch().
# Spaces, "!" and property separators can be parsed as pipeline syntax. Restrict
# each value to its interface, ALSA device or path syntax. These keys are local
# to /etc/mesh.conf. Only voice_codec is staged over Alfred, with a two-value
# check in mesh-radio-state. Validate local edits to keep typos from producing
# misleading pipeline errors in the root daemon.
IFACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
# plughw:1,0 / default / hw:CARD=Device,DEV=0
ALSA_DEV_RE = re.compile(r"^[A-Za-z0-9_.,:=-]{1,64}$")
MODEL_PATH_RE = re.compile(r"^[A-Za-z0-9_./-]{1,255}$")


def conf_str(conf, key, default, pattern):
    """A config string that is safe to interpolate, or the default instead."""
    val = conf.get(key, "").strip()
    if not val:
        return default
    if not pattern.match(val):
        log("%s=%r rejected (unsupported characters), using %r"
            % (key, val, default))
        return default
    return val


def conf_float(conf, key, default, low=None, high=None):
    """Read a float setting, defaulting on parse or bound-check failure."""
    try:
        val = float(conf.get(key, "").strip())
    except (ValueError, AttributeError):
        return default
    if low is not None and val < low:
        return default
    if high is not None and val > high:
        return default
    return val


def conf_bool(conf, key, default):
    """Read a common yes/no spelling, defaulting on unrecognized input."""
    val = conf.get(key, "").strip().lower()
    if val in ("y", "yes", "true", "1", "on"):
        return True
    if val in ("n", "no", "false", "0", "off"):
        return False
    return default


def talk_group_port(channel):
    """Map a talk group to its RTP port, defaulting invalid groups to one."""
    if channel < 1 or channel > TALK_GROUP_MAX:
        channel = 1
    return TALK_GROUP_BASE_PORT + (channel - 1) * TALK_GROUP_PORT_STRIDE


class Config:
    """Load voice settings and apply their defaults and validation rules."""
    def __init__(self):
        """Read local configuration for transport, codec, audio and PTT."""
        conf = read_kv_file(MESH_CONF)
        self.enabled = conf_bool(conf, "voice", False)
        self.iface = conf_str(conf, "voice_iface", "br0", IFACE_RE)
        self.channel = conf_int(conf, "voice_channel", 1, 1, TALK_GROUP_MAX)
        # 48 = CS6, not 46/EF: see the module docstring for why.
        self.dscp = conf_int(conf, "voice_dscp", 48, 0, 63)
        # Lyra is the default and needs libgstlyra.so plus model weights.
        # Missing Lyra components fall back to stock Opus in _codec_stages().
        self.codec = conf.get("voice_codec", "lyra").strip().lower()
        if self.codec not in ("opus", "lyra"):
            self.codec = "opus"
        self.bitrate = conf_int(conf, "voice_bitrate", 32000, 6000, 128000)
        # Lyra is not free-rate: 3200, 6000 and 9200 are the only trained
        # operating points, and each maps to a fixed frame size (8/15/23 B).
        self.lyra_bitrate = conf_int(conf, "voice_lyra_bitrate", 6000)
        if self.lyra_bitrate not in (3200, 6000, 9200):
            self.lyra_bitrate = 6000
        # Frames per RTP packet. 2 (40 ms) measured as the knee: at 10 % loss,
        # 20 ms losses are inaudible, 40 ms barely audible, 60 ms clearly
        # audible, 80 ms unpleasant. Going from 1 to 2 takes 43 % off the wire.
        self.lyra_fpp = conf_int(conf, "voice_lyra_frames_per_packet", 2, 1, 6)
        self.lyra_model = conf_str(conf, "voice_lyra_model",
                                   "/usr/local/share/lyra/model_coeffs",
                                   MODEL_PATH_RE)
        # Headers cost 42-54 bytes per packet: RTP 12, UDP 8, IP 20, Ethernet
        # 14. Payloads are 32-132 bytes. Measured wire cost: 16 kbps at 60 ms is
        # below 6 kbps at 20 ms, at the cost of latency and longer audio gaps on
        # loss. Opus permits 2.5/5/10/20/40/60 ms. This setting accepts
        # 10/20/40/60 ms.
        self.frame_ms = conf_int(conf, "voice_frame_ms", FRAME_MS)
        if self.frame_ms not in (10, 20, 40, 60):
            self.frame_ms = FRAME_MS
        self.ttl = conf_int(conf, "voice_ttl", 32, 1, 255)
        self.jitter_ms = conf_int(conf, "voice_jitter_ms", 100, 20, 500)
        self.loss_pct = conf_int(conf, "voice_loss_pct", 20, 0, 100)
        # Off by default: batman-adv already fans multicast out as unicast.
        self.unicast = conf_bool(conf, "voice_unicast", False)
        self.max_peers = conf_int(conf, "voice_unicast_max_peers", 16, 0, 128)
        # Off by default. Separate receive branches allow overlapping talkers.
        self.half_duplex = conf_bool(conf, "voice_half_duplex", False)
        # Warm decode-branch floor, default 8. Lyra costs about 5.3 MB per
        # branch on a CM4, roughly 40 MB for 8. _size_talker_table raises it for
        # peers.
        self.max_talkers = conf_int(conf, "voice_max_talkers", 8, 1,
                                   VOICE_MAX_TALKERS_HARD)
        # Periodic beacon interval in seconds, default 600. Covers missed peer
        # arrivals and refreshes retained sources before native idle expiry.
        # Beacons also run at startup and when peers appear. 0 disables them.
        self.beacon_sec = conf_int(conf, "voice_beacon_sec", 600, 0, 3600)
        # High-pass cutoff in Hz, default 80. 0 disables the high-pass filter.
        # 80 Hz preserves male fundamentals around 85 Hz and above. Raise toward
        # 120 Hz for mains hum: 60 Hz sees only -9.6 dB at 80 Hz.
        self.highpass_hz = conf_int(conf, "voice_highpass_hz", 80, 0,
                                    HIGHPASS_MAX_HZ)
        # Low-pass cutoff in Hz. 0 leaves high-pass only, the default. Both
        # edges select a band-pass element with its own pole count. Compare
        # 100-4000 Hz across headsets for clarity and thinness.
        self.lowpass_hz = conf_int(conf, "voice_lowpass_hz", 0, 0, 8000)
        # Three-band EQ, off by default. Compare the formant/presence lift
        # across headsets. Enabled EQ gains retune live on SIGHUP without an
        # audio gap.
        self.eq = conf_bool(conf, "voice_eq", False)
        self.eq_low = conf_float(conf, "voice_eq_low", 0.0,
                                 EQ_MIN_DB, EQ_MAX_DB)
        self.eq_mid = conf_float(conf, "voice_eq_mid", 0.0,
                                 EQ_MIN_DB, EQ_MAX_DB)
        self.eq_high = conf_float(conf, "voice_eq_high", 0.0,
                                  EQ_MIN_DB, EQ_MAX_DB)
        # Stall watchdog defaults on to catch silent audio failures.
        # voice_watchdog=n disables stall checks but keeps systemd keep-alives.
        self.watchdog = conf_bool(conf, "voice_watchdog", True)
        self.watchdog_sec = conf_int(conf, "voice_watchdog_sec",
                                     VOICE_STALL_SEC, 5, 300)
        self.ptt_mode = conf.get("voice_ptt", "openvlm").strip().lower()
        # Empty means autodetect the OpenVLM card.
        self.alsa_in = conf_str(conf, "voice_alsa_in", "", ALSA_DEV_RE)
        self.alsa_out = conf_str(conf, "voice_alsa_out", "", ALSA_DEV_RE)
        # Playback mixer level set on the autodetected card, default 100 (its
        # 0 dB maximum). Not applied when voice_alsa_out overrides the device.
        self.playback_pct = conf_int(conf, "voice_playback_pct", 100, 0, 100)
        # Software gain on each talker before the mixer, limited. 0 bypasses.
        self.rx_gain_db = conf_float(conf, "voice_rx_gain_db",
                                     RX_GAIN_DB_DEFAULT, 0.0, RX_GAIN_DB_MAX)
        # Bench mode: a 440 Hz tone in place of the mic and a null sink in
        # place of the speaker, so the transport can be proven on a node that
        # has no audio hardware fitted yet.
        self.test_tone = conf_bool(conf, "voice_test_tone", False)
        self.port = talk_group_port(self.channel)


# --- OpenVLM PTT -------------------------------------------------------------

def hidraw_candidates():
    """Return CM108-family /dev/hidraw* devices as (path, is_openvlm) pairs.

    Probe GPIO1 where possible and sort strapped OpenVLM devices first.
    """
    want = "%04X:%04X" % (OPENVLM_VID, OPENVLM_PID)
    found = []
    for dev in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(dev, "device/uevent")) as fh:
                uevent = fh.read()
        except OSError:
            continue
        # HID_ID=0003:00000D8C:00000012
        match = re.search(r"HID_ID=[0-9A-Fa-f]+:0*([0-9A-Fa-f]{4}):0*([0-9A-Fa-f]{4})",
                          uevent)
        if not match:
            continue
        if ("%s:%s" % (match.group(1), match.group(2))).upper() != want:
            continue
        path = "/dev/" + os.path.basename(dev)
        found.append((path, probe_openvlm_strap(path)))
    # Strapped devices first.
    found.sort(key=lambda item: not item[1])
    return found


def probe_openvlm_strap(path):
    """True when GPIO1 reads high, which is how an OpenVLM identifies itself."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        data = os.read(fd, HID_REPORT_LEN)
    except OSError:
        return False
    finally:
        os.close(fd)
    report = parse_hid_report(data)
    if report is None:
        return False
    _ir0, ir1 = report
    return bool(ir1 & HID_GPIO1_MASK)


def parse_hid_report(data):
    """Return (IR0, IR1) from a CM108B input report, or None if unusable."""
    if not data or len(data) < 3:
        return None
    # The kernel prepends a report ID on full-length reports.
    start = 1 if len(data) >= HID_REPORT_LEN else 0
    if len(data) < start + 2:
        return None
    ir0 = data[start]
    ir1 = data[start + 1]
    if ir0 & HID_IR0_VALID_MASK:
        return None
    return ir0, ir1


class OpenVLMPTT(threading.Thread):
    """Read hidraw and call on_change(pressed) on the GLib main loop.

    Reopen after hot-plug or USB reset, including enumeration after startup.
    """

    daemon = True

    def __init__(self, on_change, on_presence):
        """Set main-loop callbacks and initialize the HID reader state."""
        super().__init__(name="openvlm-ptt")
        self._on_change = on_change
        self._on_presence = on_presence
        self._stop = threading.Event()
        self.connected = False
        self.device = None

    def stop(self):
        """Request termination when the reader next checks its stop event."""
        self._stop.set()

    def run(self):
        """Open preferred PTT hardware and retry discovery after disconnects."""
        # Failed discovery or open waits two seconds, interruptible by stop().
        while not self._stop.is_set():
            devices = hidraw_candidates()
            if not devices:
                self._set_presence(False, None)
                self._stop.wait(2.0)
                continue
            path, strapped = devices[0]
            try:
                fd = os.open(path, os.O_RDONLY)
            except OSError as exc:
                if exc.errno not in (errno.ENOENT, errno.EACCES, errno.EBUSY):
                    log("PTT: open %s: %s" % (path, exc))
                self._set_presence(False, None)
                self._stop.wait(2.0)
                continue
            log("PTT: OpenVLM on %s (identity strap %s)"
                % (path, "present" if strapped else "absent"))
            self._set_presence(True, path)
            try:
                self._read_loop(fd)
            finally:
                os.close(fd)
                self._set_presence(False, None)
                # A release must never be lost with the device.
                GLib.idle_add(self._on_change, False)
                log("PTT: OpenVLM disconnected, waiting for reconnect")

    def _read_loop(self, fd):
        """Read GPIO3 edges, debounce them and queue main-loop callbacks."""
        pressed = False
        last_edge = 0
        while not self._stop.is_set():
            try:
                data = os.read(fd, HID_REPORT_LEN)
            except OSError:
                return
            if not data:
                return
            report = parse_hid_report(data)
            if report is None:
                continue
            _ir0, ir1 = report
            state = bool(ir1 & HID_GPIO3_MASK)
            if state == pressed:
                continue
            stamp = now_ms()
            # Ignore edges less than 150 ms after the last accepted change.
            if stamp - last_edge < PTT_DEBOUNCE_MS:
                continue
            last_edge = stamp
            pressed = state
            GLib.idle_add(self._on_change, pressed)

    def _set_presence(self, connected, path):
        """Publish hardware presence changes on the GLib main loop."""
        if connected == self.connected and path == self.device:
            return
        self.connected = connected
        self.device = path
        GLib.idle_add(self._on_presence, connected)


# --- peers -------------------------------------------------------------------

def read_registry(exclude_ips):
    """Return active registry peers as [(ip, hostname), ...].

    Parse NODE_<id>_<KEY>='value' lines without sourcing the shell file.
    Skip STALE nodes, which no longer publish telemetry, to avoid wasted air.
    """
    nodes = parse_registry(REGISTRY_FILE)

    peers = []
    for fields in nodes.values():
        ip = fields.get("IPV4_ADDRESS", "").strip()
        if not ip or ip in exclude_ips:
            continue
        if fields.get("NODE_STATE", "ACTIVE") != "ACTIVE":
            continue
        peers.append((ip, fields.get("HOSTNAME", "").strip()))
    # Stable lexical address/name order also determines capped unicast targets.
    peers.sort()
    return peers


def iface_ipv4(name):
    """Return the first IPv4 address on an interface, or None if absent.

    Used with multicast-iface for multiudpsink's bind-address. br0 carries
    mesh and EUD DHCP gateway addresses. Either belongs to the interface.
    """
    # Pass the mesh.conf interface name as an argument to avoid shell parsing.
    # Bound the wait: build() calls this during retunes on the GLib main loop.
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", name],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.stdout.splitlines():
        match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", line)
        if match:
            # Constrained to digits and dots by the pattern above, which is
            # what makes it safe as multiudpsink's bind-address.
            return match.group(1)
    return None


def node_ssrc(addr):
    """Use all 32 SSRC bits for each run, naming talkers by their UDP address.

    A reused source with reset sequence numbers can discard a whole first
    transmission. An eight-bit generation was too small even with expiry.
    """
    if not addr:
        return None
    # GstRTPBasePayload reserves UINT32_MAX to request its own random SSRC.
    # Keep my_ssrc identical to the wire identity for loopback suppression.
    ssrc = random.getrandbits(32)
    while ssrc == 0xffffffff:
        ssrc = random.getrandbits(32)
    return ssrc


class RtpSources:
    """Bound ingress records and admission work without delaying accepted RTP.

    Called by the UDP streaming thread and native timeout callbacks. Rejected
    identities allocate no record or GLib work item. Retired slots remain
    charged until RTPSession actually forgets them, not just clear-ssrc.
    """

    def __init__(self):
        """Initialize bounded source records and full admission buckets."""
        self.lock = threading.RLock()
        self.sources = {}
        self.senders = {}
        self.tokens = float(VOICE_ADMISSION_BURST)
        self.updated = time.monotonic()
        self.dropped = 0

    def admit(self, ssrc, ip, now, port=0):
        """Pass known sources or spend global and per-IP new-source tokens."""
        with self.lock:
            source = self.sources.get(ssrc)
            if source is not None:
                if (source['ip'] == ip and source['port'] == port
                        and not source['retired'] and not source['expired']):
                    return True
                self.dropped += 1
                return False
            refill = max(0, now - self.updated) * VOICE_ADMISSION_PER_SEC
            self.tokens = min(VOICE_ADMISSION_BURST, self.tokens + refill)
            self.updated = now
            sender = self.senders.get(ip)
            tokens = (min(VOICE_IP_BURST, sender['tokens'] +
                          max(0, now - sender['updated']) / VOICE_IP_ADMISSION_SEC)
                      if sender else VOICE_IP_BURST)
            if (len(self.sources) >= VOICE_MAX_SOURCES or self.tokens < 1
                    or tokens < 1
                    or (sender and len(sender['sources']) >= VOICE_SOURCES_PER_IP)):
                self.dropped += 1
                return False
            if sender is None:
                sender = {'sources': set()}
                self.senders[ip] = sender
            sender.update(tokens=tokens - 1, updated=now)
            sender['sources'].add(ssrc)
            self.tokens -= 1
            self.sources[ssrc] = dict(ip=ip, port=port, retired=False,
                                      expired=False, cleared=False)
            return True

    def get(self, ssrc):
        """Return a stable snapshot, without keeping the lock during Gst calls."""
        with self.lock:
            source = self.sources.get(ssrc)
            return dict(source) if source is not None else None

    def mark(self, ssrc, **flags):
        """Mark only admitted slots, including streams without a receive pad."""
        with self.lock:
            if ssrc in self.sources:
                self.sources[ssrc].update(flags)

    def pending(self):
        """Snapshot bounded cleanup work for the existing state timer."""
        with self.lock:
            return [(ssrc, dict(s)) for ssrc, s in self.sources.items()
                    if s['expired'] or (s['retired'] and not s['cleared'])]

    def forget(self, ssrc):
        """Release the sender budget once its final native source is gone."""
        with self.lock:
            source = self.sources.pop(ssrc, None)
            if source is not None:
                sender = self.senders[source['ip']]
                sender['sources'].discard(ssrc)
                if not sender['sources']:
                    del self.senders[source['ip']]


def local_ipv4_addresses():
    """Return local addresses to exclude from unicast targets."""
    addrs = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            addrs.add(info[4][0])
    except Exception:
        pass
    # argv and a timeout, for the same reasons as iface_ipv4 above.
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                             capture_output=True, text=True, timeout=5)
        for line in out.stdout.splitlines():
            match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", line)
            if match:
                addrs.add(match.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    return addrs


# --- ALSA --------------------------------------------------------------------

def find_openvlm_card():
    """ALSA card index for the CM108B, via /proc/asound/card*/usbid."""
    want = "%04x:%04x" % (OPENVLM_VID, OPENVLM_PID)
    for path in sorted(glob.glob("/proc/asound/card*/usbid")):
        try:
            with open(path) as fh:
                if fh.read().strip().lower() != want:
                    continue
        except OSError:
            continue
        # The matched proc directory supplies the numeric ALSA card index.
        match = re.search(r"card(\d+)", path)
        if match:
            return int(match.group(1))
    return None


def set_playback_level(card, pct):
    """Set the playback mixer on the given ALSA card to pct percent.

    Try PCM first (OpenVLM), then Speaker (generic CM108 dongles). Called from
    iface_ipv4, because build() runs on the GLib main loop.
    """
    for control in PLAYBACK_CONTROLS:
        try:
            out = subprocess.run([AMIXER, "-q", "-c", str(card), "sset",
                                  control, "%d%%" % pct, "unmute"],
                                 capture_output=True, text=True,
                                 timeout=AMIXER_TIMEOUT_SEC)
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode == 0:
            return control
    return None


def alsa_device(explicit, card):
    """Choose an override, detected card or ALSA default device."""
    if explicit:
        return explicit
    if card is not None:
        # plughw allows ALSA to convert formats unsupported by the card.
        return "plughw:%d" % card
    return "default"


# --- the daemon --------------------------------------------------------------

class MeshVoice:
    """Manage PTT, RTP pipelines, peer state and audio health on the node."""
    def __init__(self, cfg):
        """Initialize pipelines, counters, peers and watchdog clocks."""
        self.cfg = cfg
        self.loop = GLib.MainLoop()
        self.tx = None
        self.rx = None
        self.valve = None
        self.sink = None
        self.payloader = None
        self.mixer = None
        self.rtpbin = None
        self.rtp_session = None
        self.sources = RtpSources()
        self._discarded_pads = {}
        # One decode branch and one jitter buffer per talker, keyed by
        # rtpbin pad name and SSRC respectively.
        self.rx_branches = {}
        self.jitterbuffers = {}
        self.branch_seen = {}
        # Talkers evicted by the cap: pad name -> (rtpbin pad, fakesink, probe).
        # See _park_branch.
        self.rx_parked = {}
        self._parking = set()
        self._reviving = {}
        self.volume = None
        self.shaper = None
        self.equalizer = None
        self.txhead = None
        # Transmit gain, at most 1.0. Applied before EQ to reserve boost
        # headroom.
        self._tx_gain = 1.0
        self._shape_sig = None
        self._raw_rate = SAMPLE_RATE
        self._eq_high_warned = None
        self.my_ssrc = None
        # Sender IP -> peer name, rebuilt from the registry each poll.
        self.talker_names = {}
        self._known_peer_ips = set()
        self._beacon_pending = None
        self._beacon_end = None
        self._bind_ip = None
        self._tx_blocked = "waiting for IPv4 on %s" % cfg.iface
        self._tx_blocked_logged = False

        # Adaptive packing state. `packing` mirrors the payloader property so
        # the state file and the web UI can report it without querying GStreamer
        # from the wrong thread.
        self.packing = cfg.lyra_fpp
        self.rx_loss_pct = 0.0
        self._pk_last_pushed = 0
        self._pk_last_lost = 0
        self._pk_clean_since = time.monotonic()

        self.ptt_pressed = False
        self.ptt_connected = False
        self.transmitting = False
        self.last_rx_ms = 0
        self.rx_active = False
        self.rx_packets = 0
        self.tx_packets = 0
        # Own packets dropped on receive. Nonzero indicates failed socket
        # loopback suppression. The receive probe still prevents local audio
        # playback.
        self.rx_loopback = 0
        # Per-pipeline restart state, keyed "tx"/"rx". See _note_pipeline_error.
        self._pipeline_fault = {}
        self.peers = []
        self.local_ips = local_ipv4_addresses()
        self.ptt = None
        self.started_at = time.monotonic()

        # Watchdog state keyed by "tx"/"rx". Probes write _flow_count, _flow_ts
        # and _ever_flowed from streaming threads. Other state belongs to the
        # main loop. Judge only flows that have run. Missing OpenVLM hardware
        # must stay on the bus-error backoff path instead of causing a 10 s
        # process restart loop.
        self._flow_count = {"tx": 0, "rx": 0}
        self._flow_ts = {"tx": 0.0, "rx": 0.0}
        self._ever_flowed = {"tx": False, "rx": False}
        self._playing_since = {"tx": 0.0, "rx": 0.0}
        self._stalls = {"tx": 0, "rx": 0}
        self._stall_history = {"tx": [], "rx": []}
        self._igmp_ok = None
        self._igmp_lost_since = 0.0
        # Set by _panic. main() returns it, and a non-zero exit is what asks
        # systemd to rebuild the whole voice stack.
        self.exit_code = 0
        self._fault = None
        # name -> (bus, handler id), so a retune can take down what it replaces.
        self._bus_watch = {}

    # -- pipelines --

    def _codec_stages(self):
        """Return pipeline stages and rates for the configured codec.

        Returns (enc, pay, depay, dec, encoding_name, packet_ms, raw_rate,
        clock_rate). Transport is shared and measured with Opus.
        Opus uses a 48 kHz RTP clock at any input rate. Lyra requires 16 kHz
        for both raw caps and RTP. Missing Lyra components fall back to Opus.
        """
        if self.cfg.codec == "lyra":
            missing = [n for n in ("lyraenc", "lyradec", "rtplyrapay",
                                   "rtplyradepay")
                       if Gst.ElementFactory.find(n) is None]
            if missing:
                log("voice_codec=lyra but %s not registered: falling back to "
                    "opus. Install libgstlyra.so and %s."
                    % (", ".join(missing), self.cfg.lyra_model))
            elif not os.path.isdir(self.cfg.lyra_model):
                log("voice_codec=lyra but model dir %s is missing: falling "
                    "back to opus" % self.cfg.lyra_model)
            else:
                # Preserve adapted packing across SIGHUP rebuilds. Reading cfg
                # here would disagree with self.packing and the UI, and
                # _set_packing's equality check would prevent correction until
                # the next adaptation. Resetting 1 frame to 2 doubles each loss
                # from 20 to 40 ms.
                fpp = self.packing
                packet_ms = fpp * 20
                log("codec: lyra %d bps, %d frame(s)/packet (%d ms)"
                    % (self.cfg.lyra_bitrate, fpp, packet_ms))
                return (
                    "lyraenc name=enc bitrate=%d model-path=%s"
                    % (self.cfg.lyra_bitrate, self.cfg.lyra_model),
                    "rtplyrapay name=pay pt=%d frames-per-packet=%d"
                    % (RTP_PAYLOAD_TYPE, fpp),
                    "rtplyradepay",
                    "lyradec model-path=%s" % self.cfg.lyra_model,
                    "LYRA", packet_ms, LYRA_RATE, LYRA_RATE)

        log("codec: opus %d bps, %d ms frames"
            % (self.cfg.bitrate, self.cfg.frame_ms))
        return (
            "opusenc name=enc bitrate=%d frame-size=%d inband-fec=true "
            "packet-loss-percentage=%d audio-type=voice"
            % (self.cfg.bitrate, self.cfg.frame_ms, self.cfg.loss_pct),
            "rtpopuspay name=pay pt=%d" % RTP_PAYLOAD_TYPE,
            "rtpopusdepay",
            "opusdec plc=true use-inband-fec=true",
            "OPUS", self.cfg.frame_ms, SAMPLE_RATE, OPUS_CLOCK_RATE)

    def _eq_gains(self, raw_rate):
        """The three EQ gains, with the high band forced off when unusable."""
        high = self.cfg.eq_high
        if high and raw_rate < EQ_HIGH_MIN_RATE:
            if self._eq_high_warned != (high, raw_rate):
                self._eq_high_warned = (high, raw_rate)
                log("voice_eq_high=%+.1f ignored: the %d Hz band needs a raw "
                    "rate of at least %d Hz and this codec runs at %d, where "
                    "the band degenerates into broadband gain"
                    % (high, EQ_BAND_HZ[2], EQ_HIGH_MIN_RATE, raw_rate))
            high = 0.0
        return self.cfg.eq_low, self.cfg.eq_mid, high

    def _shape_signature(self, raw_rate):
        """Return shaping elements and raw rate for the pipeline layout.

        Matching signatures allow live property changes. A changed signature
        requires rebuilding the pipeline.
        """
        if not self.cfg.highpass_hz and not self.cfg.lowpass_hz:
            filt = None
        elif self.cfg.lowpass_hz and self.cfg.highpass_hz:
            filt = "band"
        else:
            filt = "limit"
        return (filt, bool(self.cfg.eq), raw_rate)

    def _shaping_stage(self, raw_rate):
        """Return a shaping fragment ending in "!", or "" when disabled.

        Set both cutoff frequencies to 0 and voice_eq=n to bypass shaping.
        Filters and EQ require F32/F64. Convert back to S16LE for the encoders.
        """
        filt, eq, _ = self._shape_signature(raw_rate)
        parts = []

        if filt == "band":
            if Gst.ElementFactory.find("audiochebband") is None:
                log("voice_lowpass_hz set but audiochebband is not registered; "
                    "no band-pass. Install gstreamer1.0-plugins-good.")
            else:
                parts.append("audiochebband name=shape mode=band-pass "
                             "lower-frequency=%d upper-frequency=%d poles=%d "
                             "type=%d"
                             % (self.cfg.highpass_hz, self.cfg.lowpass_hz,
                                BANDPASS_POLES, HIGHPASS_TYPE))
                log("transmit band-pass: %d-%d Hz, %d-pole chebyshev type %d"
                    % (self.cfg.highpass_hz, self.cfg.lowpass_hz,
                       BANDPASS_POLES, HIGHPASS_TYPE))
        elif filt == "limit":
            if Gst.ElementFactory.find("audiocheblimit") is None:
                log("voice_highpass_hz=%d but audiocheblimit is not registered, "
                    "so no transmit high-pass. Install "
                    "gstreamer1.0-plugins-good." % self.cfg.highpass_hz)
            else:
                # A single configured edge uses the matching low-pass or
                # high-pass mode.
                mode, cut = (("high-pass", self.cfg.highpass_hz)
                             if self.cfg.highpass_hz
                             else ("low-pass", self.cfg.lowpass_hz))
                parts.append("audiocheblimit name=shape mode=%s cutoff=%d "
                             "poles=%d type=%d"
                             % (mode, cut, HIGHPASS_POLES, HIGHPASS_TYPE))
                log("transmit %s: %d Hz, %d-pole chebyshev type %d"
                    % (mode, cut, HIGHPASS_POLES, HIGHPASS_TYPE))

        if eq:
            if Gst.ElementFactory.find("equalizer-3bands") is None:
                log("voice_eq=y but equalizer-3bands is not registered. "
                    "Install gstreamer1.0-plugins-good.")
            else:
                low, mid, high = self._eq_gains(raw_rate)
                # Attenuate before EQ to prevent clipping at the S16LE
                # conversion. Use a separate element: vol is downstream of that
                # conversion and belongs to PTT and beacon control.
                parts.insert(0, "volume name=txhead volume=1.0")
                parts.append("equalizer-3bands name=eq band0=%.2f band1=%.2f "
                             "band2=%.2f" % (low, mid, high))
                log("transmit eq: %+.1f/%+.1f/%+.1f dB at %d/%d/%d Hz"
                    % (low, mid, high, *EQ_BAND_HZ))

        if not parts:
            return ""
        return ("audioconvert ! " + " ! ".join(parts)
                + " ! audioconvert ! audio/x-raw,format=S16LE ! ")

    def _apply_shaping(self, raw_rate):
        """Apply configured cutoffs and gains to the running elements.

        These properties are controllable while PLAYING, so tuning needs no
        audio gap or TFLite model reload.
        """
        changed = False
        if self.shaper is not None:
            if self.cfg.lowpass_hz and self.cfg.highpass_hz:
                want = (("lower-frequency", float(self.cfg.highpass_hz)),
                        ("upper-frequency", float(self.cfg.lowpass_hz)))
            else:
                want = (("cutoff",
                         float(self.cfg.highpass_hz or self.cfg.lowpass_hz)),)
            for prop, value in want:
                # Ignore changes within 0.01 Hz when retuning filter properties.
                if abs(self.shaper.get_property(prop) - value) > 0.01:
                    self.shaper.set_property(prop, value)
                    changed = True
        if self.equalizer is not None:
            for band, value in enumerate(self._eq_gains(raw_rate)):
                prop = "band%d" % band
                # Ignore gain differences within 0.01 dB.
                if abs(self.equalizer.get_property(prop) - value) > 0.01:
                    self.equalizer.set_property(prop, value)
                    changed = True
        if changed:
            # Adjust headroom during transmission to prevent EQ clipping. The
            # gain change can step the level.
            self._apply_tx_gain(raw_rate)
        return changed

    def _apply_tx_gain(self, raw_rate):
        """Attenuate before EQ by the largest positive band gain.

        The shaping block converts back to S16LE. At 48 kHz, band1=+3 on a
        0.8 full-scale tone clips 12157 samples in one second without headroom.
        Attenuation preserves peaks at reduced level. Playback has 20 dB spare.
        """
        boost = max([0.0] + [g for g in self._eq_gains(raw_rate) if g > 0])
        # Convert dB attenuation to a linear audio amplitude multiplier.
        self._tx_gain = 10.0 ** (-boost / 20.0)
        if self.txhead is not None:
            self.txhead.set_property("volume", self._tx_gain)
        if boost:
            log("transmit headroom: %.3f (-%.1f dB) ahead of the eq boost"
                % (self._tx_gain, boost))

    def _rx_gain_stage(self):
        """Return the talker gain and limiter fragment, or "" at 0 dB.

        Float, so the boosted signal survives until the limiter folds it
        back under full scale. Per talker, so separate levels stay separate.
        """
        if not self.cfg.rx_gain_db:
            return ""
        return ("audioconvert ! capsfilter caps=\"audio/x-raw,format=F32LE\" ! "
                "audioamplify amplification=%.4f clipping-method=none ! "
                "audiodynamic mode=compressor characteristics=hard-knee "
                "threshold=%.2f ratio=%.2f ! "
                % (10.0 ** (self.cfg.rx_gain_db / 20.0), RX_LIMIT_THRESHOLD,
                   RX_LIMIT_RATIO))

    def _set_playback_level(self):
        """Apply voice_playback_pct to the autodetected card's mixer.

        Call after both pipelines are told to play. A mixer write just ahead
        of the PCM open failed it as busy in 1 of 3 restarts on a CM4.
        """
        self.playback_level = None
        card = find_openvlm_card()
        if card is None or self.cfg.alsa_out:
            return
        control = set_playback_level(card, self.cfg.playback_pct)
        if control is None:
            log("audio: could not set playback level on card %d; it stays at "
                "the card's own level" % card)
            return
        self.playback_level = self.cfg.playback_pct
        log("audio: playback %s set to %d%% on card %d"
            % (control, self.cfg.playback_pct, card))

    def build(self):
        """Construct audio pipelines with transport and health probes."""
        if self.cfg.test_tone:
            src_desc = ("audiotestsrc name=cap is-live=true wave=sine freq=440")
            playback_desc = "fakesink name=play sync=false"
            log("audio: BENCH MODE: 440 Hz tone in, null sink out")
        else:
            card = find_openvlm_card()
            dev_in = alsa_device(self.cfg.alsa_in, card)
            dev_out = alsa_device(self.cfg.alsa_out, card)
            src_desc = "alsasrc device=%s name=cap" % dev_in
            playback_desc = "alsasink name=play device=%s sync=false" % dev_out
            log("audio: capture=%s playback=%s (openvlm card %s)"
                % (dev_in, dev_out, card if card is not None else "not found"))

        # auto-multicast=false avoids a sender join conflicting with udpsrc's
        # join.
        bind_ip = iface_ipv4(self.cfg.iface)
        self._bind_ip = None
        if not bind_ip:
            log("TX: waiting for IPv4 on %s" % self.cfg.iface)

        (enc_desc, pay_desc, depay_desc, dec_desc, encoding, packet_ms,
         raw_rate, clock_rate) = self._codec_stages()

        # Report the effective codec to the UI. Missing Lyra components fall
        # back to Opus, which cannot exchange voice with Lyra peers.
        self.effective_codec = encoding.lower()

        # sync=false avoids sink latency because alsasrc clocks live capture.
        # Put shaping before level so the UI measures transmitted audio after
        # hum and rumble removal.
        self._shape_sig = self._shape_signature(raw_rate)
        self._raw_rate = raw_rate
        # The level element publishes RMS every 200,000,000 ns (200 ms).
        tx_desc = (
            "{src} ! "
            "audioconvert ! audioresample ! "
            "audio/x-raw,rate={rate},channels=1,format=S16LE ! "
            "{hpf}"
            "level name=lvl interval=200000000 ! "
            "volume name=vol ! "
            "valve name=ptt drop=true ! "
            "{enc} ! {pay} ! "
            "multiudpsink name=sink clients=\"{clients}\" "
            "  qos-dscp={dscp} ttl-mc={ttl} loop=false auto-multicast=false "
            "  multicast-iface={iface} {bind} sync=false async=false"
        ).format(src=src_desc, rate=raw_rate, hpf=self._shaping_stage(raw_rate),
                 enc=enc_desc, pay=pay_desc,
                 clients=("%s:%d" % (TALK_GROUP_ADDR, self.cfg.port)
                          if bind_ip else ""), dscp=self.cfg.dscp,
                 ttl=self.cfg.ttl, iface=self.cfg.iface,
                 bind=("bind-address=%s" % bind_ip) if bind_ip else "")

        # Conference receive.
        # rtpbin gives each SSRC a jitter buffer. audiomixer sums decoded audio.
        # A shared jitter buffer interleaves independent sequence numbers. The
        # silent source keeps audiomixer and alsasink running between talkers,
        # so each transmission starts without a DAC startup delay.
        # Retain sources across conversation gaps, then let the native session
        # expire them. Measured on a CM4 with Lyra, for 3.00 s bursts:
        #   branch rebuilt on demand   2.82 s arrived  (180 ms lost)
        #   branch pre-built, attached 2.92 s arrived  ( 80 ms lost)
        #   talker already established 3.04 s arrived  (nothing lost)
        # Tried: pad block, probation down, min-upstream-latency up. No effect.
        # The remaining delay is source establishment in rtpbin.
        # ignore-inactive-pads keeps the mixer from waiting on retained idle
        # branches.
        self.rx_branches = {}
        self.jitterbuffers = {}
        self.branch_seen = {}
        self.rx_parked = {}
        self._parking = set()
        self._reviving = {}
        self.sources = RtpSources()
        self._discarded_pads = {}
        # Rebuilt jitter buffers reset cumulative loss counters. Reset the
        # sampling baseline to avoid a negative first delta. Keep
        # _pk_clean_since here: rebuilding does not change link quality.
        self._pk_last_pushed = 0
        self._pk_last_lost = 0
        self.my_ssrc = None
        # Sender IP -> peer name for the UI, filled from the registry.
        self.talker_names = {}
        self._known_peer_ips = set()
        # Request one MiB of UDP buffering and at least two packets of jitter.
        rx_desc = (
            "rtpbin name=rtpbin latency={jitter} do-lost=true autoremove=true "
            "  max-streams={sources} "
            "udpsrc name=src address={group} port={port} "
            "  multicast-iface={iface} auto-multicast=true buffer-size=1048576 "
            "  retrieve-sender-address=true "
            "  caps=\"application/x-rtp,media=(string)audio,"
            "clock-rate=(int){rate},encoding-name=(string){encoding},"
            "payload=(int){pt}\" ! rtpbin.recv_rtp_sink_0 "
            "audiomixer name=mix ignore-inactive-pads=true ! "
            "  audioconvert ! audioresample ! {play} "
            "audiotestsrc name=silence wave=silence is-live=true ! "
            "  audio/x-raw,rate={arate},channels=1,format=S16LE ! mix. "
        ).format(group=TALK_GROUP_ADDR, port=self.cfg.port,
                 iface=self.cfg.iface, rate=clock_rate, encoding=encoding,
                 pt=RTP_PAYLOAD_TYPE, arate=raw_rate,
                 jitter=max(self.cfg.jitter_ms, packet_ms * 2),
                 play=playback_desc, sources=VOICE_MAX_SOURCES)

        self.tx = Gst.parse_launch(tx_desc)
        self.rx = Gst.parse_launch(rx_desc)
        self.valve = self.tx.get_by_name("ptt")
        self.sink = self.tx.get_by_name("sink")
        self.payloader = self.tx.get_by_name("pay")
        self.volume = self.tx.get_by_name("vol")
        # May be None: either stage can be absent or its element missing.
        self.shaper = self.tx.get_by_name("shape")
        self.equalizer = self.tx.get_by_name("eq")
        self.txhead = self.tx.get_by_name("txhead")
        # Now that txhead exists, set the headroom the configured boost needs.
        self._apply_tx_gain(raw_rate)

        # Use all 32 bits for restart identity. Names come from UDP metadata.
        ssrc = node_ssrc(bind_ip)
        if ssrc is not None and self.payloader is not None:
            self.payloader.set_property("ssrc", ssrc)
            self.my_ssrc = ssrc
        self.mixer = self.rx.get_by_name("mix")
        self.rtpbin = self.rx.get_by_name("rtpbin")
        self._configure_rtp_session()

        # Save codec stages for the per-talker branch handler.
        self._rx_depay_desc = depay_desc
        self._rx_dec_desc = dec_desc
        self._rx_raw_rate = raw_rate
        self.rtpbin.connect("pad-added", self._on_rtp_pad_added)
        self.rtpbin.connect("pad-removed", self._on_rtp_pad_removed)
        # Collect per-talker jitter buffers for _tick_packing's aggregate loss.
        self.rtpbin.connect("new-jitterbuffer", self._on_new_jitterbuffer)
        self.rtpbin.connect("on-timeout", self._on_source_timeout)
        self.rtpbin.connect("on-bye-timeout", self._on_source_timeout)

        # Retain buses and handler IDs for teardown. Remove watches here as well
        # as in _release_pipelines so repeated build() calls cannot orphan them.
        # See _remove_bus_watches for descriptor costs.
        self._remove_bus_watches()
        for pipeline, name in ((self.tx, "tx"), (self.rx, "rx")):
            bus = pipeline.get_bus()
            bus.add_signal_watch()
            self._bus_watch[name] = (bus, bus.connect("message",
                                                      self._on_bus_message,
                                                      name))

        # Count inbound packets and drive the half-duplex gate straight off the
        # socket, before the jitter buffer adds its own delay.
        src = self.rx.get_by_name("src")
        src.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER,
                                            self._on_rx_buffer)
        # Count RTP buffers reaching the send sink after encoding and
        # packetization.
        self.sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER,
                                                   self._on_tx_buffer)

        # Watch capture upstream of the valve's drop, including idle PTT. Watch
        # playback at the sink, fed continuously by the silent mixer input. See
        # the watchdog constants.
        self.valve.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, self._on_capture_buffer)
        play = self.rx.get_by_name("play")
        if play is not None:
            play.get_static_pad("sink").add_probe(
                Gst.PadProbeType.BUFFER, self._on_playback_buffer)
        self._bind_ip = bind_ip

    def start(self):
        """Start pipelines, PTT input and periodic main-loop callbacks."""
        # Start the receiver before transmit so its multicast join can begin.
        self._play("rx")
        self._play("tx")
        self._set_playback_level()
        self._suppress_multicast_loopback()
        self._tx_ready()
        log("listening on %s:%d (channel %d) via %s"
            % (TALK_GROUP_ADDR, self.cfg.port, self.cfg.channel, self.cfg.iface))

        if self.cfg.ptt_mode == "openvlm":
            self.ptt = OpenVLMPTT(self.on_ptt, self.on_ptt_presence)
            self.ptt.start()
        elif self.cfg.ptt_mode == "always":
            log("PTT: always-on (open mic)")
            self.ptt_connected = True
            self.on_ptt(True)
        else:
            log("PTT: mode %r: receive only" % self.cfg.ptt_mode)

        if self.cfg.beacon_sec:
            # Announce the new SSRC before the first PTT press so receivers can
            # establish a source for this run.
            self._schedule_beacon(1500)
            GLib.timeout_add_seconds(self.cfg.beacon_sec, self._tick_beacon)
            log("beacon: ssrc 0x%08x, on start-up and on new peers "
                "(safety net every %ds)"
                % (self.my_ssrc or 0, self.cfg.beacon_sec))
        self.refresh_peers()
        GLib.timeout_add_seconds(REGISTRY_POLL_SEC, self._tick_peers)
        if self.cfg.codec == "lyra":
            # Keep the callback through fallback; a rebuild may restore Lyra.
            GLib.timeout_add_seconds(PACKING_TICK_SEC, self._tick_packing)
        GLib.timeout_add_seconds(STATE_WRITE_SEC, self._tick_state)
        # Check the 500 ms receive-idle window every 100 ms for the UI/PTT gate.
        GLib.timeout_add(100, self._tick_rx_decay)
        GLib.timeout_add_seconds(HEALTH_TICK_SEC, self._tick_health)
        if self.cfg.watchdog:
            log("watchdog: stall after %ds with no audio flowing; whole-stack "
                "restart after %d in-process restarts inside %ds"
                % (self.cfg.watchdog_sec, VOICE_STALL_MAX_RESTARTS,
                   VOICE_STALL_WINDOW_SEC))
        else:
            log("watchdog: stall detection disabled by voice_watchdog=n")
        # Type is simple, but WatchdogSec= in the unit is enough for systemd to
        # pass NOTIFY_SOCKET, and READY=1 is harmless either way.
        sd_notify("READY=1")

    def _suppress_multicast_loopback(self, attempt=0, sink=None):
        """Disable local multicast playback through the send socket.

        With auto-multicast=false, multiudpsink ignores loop. ttl-mc and
        qos-dscp work on the wire. Set multicast-loopback on used-socket,
        retrying until asynchronous startup creates it. Failure can echo
        voice one jitter buffer late and falsely mark RX active.
        _on_rx_buffer drops own-SSRC packets if this fails, so retunes proceed.
        """
        if sink is not None and sink is not self.sink:
            return False              # pipeline replaced by retune
        sink = self.sink
        if sink is None:
            return False
        try:
            sock = sink.get_property("used-socket")
        except Exception as exc:
            log("warning: could not read the send socket: %s" % exc)
            return False
        if sock is None:
            if attempt < LOOPBACK_SUPPRESS_TRIES:
                GLib.timeout_add(LOOPBACK_SUPPRESS_MS,
                                 self._suppress_multicast_loopback,
                                 attempt + 1, sink)
                return False
            log("warning: multiudpsink never produced a send socket after "
                "%.1fs, so multicast loopback could not be suppressed; the "
                "own-SSRC drop on receive is what is stopping the operator "
                "hearing themselves"
                % (LOOPBACK_SUPPRESS_TRIES * LOOPBACK_SUPPRESS_MS / 1000.0))
            return False
        try:
            sock.set_property("multicast-loopback", False)
            still_on = sock.get_property("multicast-loopback")
        except Exception as exc:
            log("warning: could not disable multicast loopback: %s" % exc)
            return False
        if still_on:
            log("warning: multicast loopback still enabled after clearing it")
        elif attempt:
            log("multicast loopback suppressed on attempt %d" % (attempt + 1))
        return False

    def stop(self, keep_state=False):
        """Stop PTT and pipelines, optionally retaining UI fault state."""
        if self.ptt:
            self.ptt.stop()
        self._release_pipelines()
        # Keep fault state through systemd's 10 s restart delay after _panic.
        # Remove it on clean shutdown so stopped voice has no stale running
        # state.
        if not keep_state:
            try:
                os.unlink(STATE_FILE)
            except OSError:
                pass
        self.loop.quit()

    # -- events --

    def _on_bus_message(self, _bus, message, which):
        """Handle pipeline errors and retain the microphone RMS level."""
        if message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            # USB resets can remove audio devices. Keep the process alive and
            # use pipeline backoff until the device returns.
            self._note_pipeline_error(which, err.message, debug)
        elif message.type == Gst.MessageType.ELEMENT:
            struct = message.get_structure()
            if struct and struct.get_name() == "level":
                self._mic_db = (struct.get_value("rms") or [-90.0])[0]

    def reload(self):
        """Apply talk-group and transmit-shaping changes on SIGHUP.

        Retune cutoffs and gains live. Rebuild for a changed group or shaping
        layout. Codec, bitrate and devices remain startup settings.
        If rebuilding fails, restore the working group and shaping. If that
        also fails, exit for a systemd restart. In-process retunes avoid a
        service restart and Lyra model reload for each channel selection.
        """
        new = Config()
        was_transmitting = self.transmitting
        old_channel = self.cfg.channel
        raw_rate = self._raw_rate

        # Shaping is copied across before anything is compared, because the
        # signature is computed from self.cfg. Keep the old values so a failed
        # rebuild can put them back with the talk group.
        shape_was = {k: getattr(self.cfg, k) for k in SHAPE_KEYS}
        for key in SHAPE_KEYS:
            setattr(self.cfg, key, getattr(new, key))
        restructure = (self._shape_sig is not None
                       and self._shape_signature(raw_rate) != self._shape_sig)

        if new.channel == self.cfg.channel and not restructure:
            # Cutoffs and gains only, which every one of these elements accepts
            # while PLAYING. No rebuild, no gap, no model reload.
            if self._apply_shaping(raw_rate):
                log("reload: transmit shaping retuned in place")
            else:
                log("reload: nothing changed (talk group %d)" % old_channel)
            # Playback level can be reapplied without a rebuild.
            if new.playback_pct != self.cfg.playback_pct:
                self.cfg.playback_pct = new.playback_pct
                self._set_playback_level()
            # RX gain applies to new branches only; update the config so the
            # next branch or rebuild uses it.
            self.cfg.rx_gain_db = new.rx_gain_db
            self.write_state()
            return False

        if restructure:
            log("reload: transmit shaping changed shape, rebuilding")

        if self._retune(new.channel, new.port):
            # The rebuilt valve starts closed. Restore transmission if PTT
            # remains held.
            if was_transmitting:
                self._set_tx(True)

            # Keep adapted packing but reset loss measurements for the selected
            # group.
            self._pk_last_pushed = 0
            self._pk_last_lost = 0
            self._pk_clean_since = time.monotonic()
            self.rx_loss_pct = 0.0

            if new.channel != old_channel:
                log("reload: talk group %d -> %d (port %d)"
                    % (old_channel, new.channel, new.port))
            else:
                log("reload: rebuilt on talk group %d" % old_channel)
            self.write_state()
            return False

        # Restore the working group and shaping after a failed retune.
        log("reload: rebuild on talk group %d failed, reverting to the "
            "previous settings" % new.channel)
        for key, value in shape_was.items():
            setattr(self.cfg, key, value)
        if self._retune(old_channel, talk_group_port(old_channel)):
            if was_transmitting:
                self._set_tx(True)
            log("reload: back on talk group %d" % old_channel)
            self.write_state()
            return False

        # Both builds failed, leaving no audio path. Restart from mesh.conf,
        # which contains the operator's requested group.
        self._panic("reload to talk group %d failed and reverting to %d "
                    "failed too" % (new.channel, old_channel))
        return False

    def _retune(self, channel, port):
        """Rebuild both pipelines on a talk group and check startup success.

        Return False on build or state-change failure. _play accepts a pending
        asynchronous start. Catch all build errors, including AttributeError
        from missing elements: PyGObject swallows signal-handler exceptions.
        """
        self.cfg.channel = channel
        self.cfg.port = port
        self._release_pipelines()
        # Rebuild pipelines, bus watches and probes for the new port. Keep the
        # PTT reader and GLib timers installed by start() without duplicates.
        try:
            self.build()
        except Exception as exc:
            log("retune: build on talk group %d failed: %s" % (channel, exc))
            return False
        if not self._play("rx") or not self._play("tx"):
            return False
        self._suppress_multicast_loopback()
        self._tx_ready()
        # Clear faults from replaced pipelines so the watchdog can check new
        # ones. An RX fault otherwise persists until traffic arrives on a quiet
        # group.
        self._pipeline_fault.clear()
        self._igmp_lost_since = 0.0
        return True

    def _remove_bus_watches(self):
        """Remove signal watches and disconnect handlers before releasing buses.

        gst_bus_add_signal_watch() creates a GSource retaining the bus and its
        GstPoll control pipe. Setting pipelines to NULL does not remove watches.
        Measured per retune without removal: 4 leaked fds and 2 live GSources.
        With removal, fd use stays flat. Sweeping a rotary switch through the
        groups would exhaust the default 1024-descriptor limit.
        """
        for name, (bus, handler) in self._bus_watch.items():
            try:
                if handler:
                    bus.disconnect(handler)
                bus.remove_signal_watch()
            except Exception as exc:
                log("teardown: %s bus watch: %s" % (name, exc))
        self._bus_watch = {}

    def _release_pipelines(self):
        """Stop both pipelines and release watches, timers and element handles.

        Clear handles because build() can fail between assignments. A stale
        valve must not refer to a pipeline already torn down by _retune.
        """
        self._set_tx(False)
        self._bind_ip = None
        for timer in ("_beacon_pending", "_beacon_end"):
            source = getattr(self, timer)
            if source is not None:
                GLib.source_remove(source)
                setattr(self, timer, None)
        self._remove_bus_watches()
        for pipeline in (self.tx, self.rx):
            if pipeline:
                pipeline.set_state(Gst.State.NULL)
        for name in list(self.rx_branches) + list(self.rx_parked):
            self._remove_branch(name)
        for source in self._reviving.values():
            GLib.source_remove(source)
        for pad, probe in self._discarded_pads.values():
            pad.remove_probe(probe)
        self.tx = self.rx = None
        self.valve = self.sink = self.payloader = None
        self.volume = self.mixer = self.rtpbin = None
        self.shaper = self.equalizer = self.txhead = None
        self.rx_branches = {}
        self.jitterbuffers = {}
        self.branch_seen = {}
        self.rx_parked = {}
        self._parking = set()
        self._reviving = {}
        self._discarded_pads = {}
        self.rtp_session = None
        self.sources = RtpSources()

    def _play(self, which):
        """Request PLAYING and report explicit startup failures.

        Port conflicts and busy ALSA devices can fail after parse_launch().
        Wait with get_state() to detect state-change failures. Accept ASYNC
        after 5 s because live pipelines may still be settling. Bus watches
        handle errors that arrive later.
        """
        pipeline = self.tx if which == "tx" else self.rx
        if pipeline is None:
            log("%s pipeline missing" % which)
            return False
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            log("%s pipeline refused to start" % which)
            return False
        ret, state, _pending = pipeline.get_state(5 * Gst.SECOND)
        if ret == Gst.StateChangeReturn.FAILURE:
            log("%s pipeline failed to reach PLAYING" % which)
            return False
        if ret == Gst.StateChangeReturn.ASYNC:
            log("%s pipeline still settling after 5s (%s)"
                % (which, Gst.Element.state_get_name(state)))
        self._mark_playing(which)
        return True

    def _mark_playing(self, which):
        """Reset watchdog grace and last-flow clocks on pipeline startup.

        _playing_since grants startup grace. _flow_ts prevents the first check
        from counting the replaced pipeline's idle time.
        """
        now = time.monotonic()
        self._playing_since[which] = now
        self._flow_ts[which] = now

    def _panic(self, reason):
        """Save the fault and exit for a systemd restart after 10 seconds.

        Restart=on-failure with RestartSec=10 reloads mesh.conf, ALSA handles,
        sockets, multicast membership and Lyra models in a fresh process.
        Self-restart via systemctl blocks on this unit or races shutdown.
        """
        log("voice stack restart: %s" % reason)
        self._fault = reason
        self.exit_code = 1
        try:
            self.write_state()
        except Exception:
            pass
        self.stop(keep_state=True)

    def _note_pipeline_error(self, which, text, debug):
        """Log a pipeline error and schedule a restart with retry backoff.

        Log each changed error in full with its retry delay. Count identical
        errors to limit journal traffic from missing hardware. Emit the tally
        on recovery or when the error changes.
        """
        state = self._pipeline_fault.get(which)
        now = time.monotonic()

        if state is None or now - state["last_ts"] > PIPELINE_RETRY_RESET_SEC:
            state = {"delay": PIPELINE_RETRY_BASE_SEC, "text": None, "repeats": 0,
                     "last_ts": now, "capped": False}
            self._pipeline_fault[which] = state
        state["last_ts"] = now

        if text == state["text"]:
            state["repeats"] += 1
            # Log once at maximum backoff so persistent failure remains visible.
            if state["delay"] >= PIPELINE_RETRY_MAX_SEC and not state["capped"]:
                state["capped"] = True
                log("%s pipeline: still failing after %d attempt(s): retrying "
                    "every %ds, further identical errors suppressed"
                    % (which, state["repeats"], PIPELINE_RETRY_MAX_SEC))
        else:
            self._flush_pipeline_repeats(which, state)
            state["text"] = text
            state["repeats"] = 0
            state["capped"] = False
            log("%s pipeline error: %s (%s): retrying in %ds"
                % (which, text, debug, state["delay"]))

        # Double retries from five seconds to five minutes after each error.
        GLib.timeout_add_seconds(state["delay"], self._restart, which)
        state["delay"] = min(state["delay"] * 2, PIPELINE_RETRY_MAX_SEC)
        if which == "tx":
            self._tx_ready()
            self.write_state()

    def _flush_pipeline_repeats(self, which, state):
        """Emit the suppressed-repeat tally, if there is one."""
        if state and state["repeats"]:
            log("%s pipeline: previous error repeated %d more time(s)"
                % (which, state["repeats"]))
            state["repeats"] = 0

    def _note_pipeline_ok(self, which):
        """Clear a pipeline fault when a buffer flows and log recovery."""
        state = self._pipeline_fault.pop(which, None)
        if state and state["text"] is not None:
            self._flush_pipeline_repeats(which, state)
            log("%s pipeline recovered" % which)

    def _restart(self, which):
        """Cycle one pipeline and reset watchdog grace for its next start."""
        pipeline = self.tx if which == "tx" else self.rx
        was_transmitting = self.transmitting
        if which == "tx":
            self._set_tx(False)
        # Suppress restart messages along with repeated identical errors.
        state = self._pipeline_fault.get(which)
        if not state or not state["repeats"]:
            log("%s pipeline restarting" % which)
        pipeline.set_state(Gst.State.NULL)
        pipeline.set_state(Gst.State.PLAYING)
        # Reset watchdog clocks so replaced-flow idle time cannot trigger
        # another restart on the next tick and exhaust escalation within 15 s.
        self._mark_playing(which)
        if which == "tx" and was_transmitting:
            self._set_tx(True)
        return False  # one-shot

    def _on_tx_buffer(self, _pad, _info):
        """Count RTP buffers and clear a recovered transmit fault."""
        self.tx_packets += 1
        # Use flowing data as recovery evidence. Skip fault handling in normal
        # flow.
        if self._pipeline_fault:
            self._note_pipeline_ok("tx")
        return Gst.PadProbeReturn.OK

    def _on_rx_buffer(self, _pad, info):
        """Admit RTP before rtpbin allocates native session or stream objects."""
        buf = info.get_buffer()
        if buf is None or buf.get_size() <= 12:
            return Gst.PadProbeReturn.DROP
        header = buf.extract_dup(0, 12)
        # Our payloaders send RTP v2 without CSRCs. Refuse CSRC lists because
        # RTPSession would create extra source records outside our SSRC budget.
        if (header[0] >> 6 != 2 or header[0] & 15
                or header[1] & 127 != RTP_PAYLOAD_TYPE):
            return Gst.PadProbeReturn.DROP
        size = buf.get_size()
        offset = 12
        if header[0] & 16:               # optional RTP header extension
            if size < 16:
                return Gst.PadProbeReturn.DROP
            offset = 16 + 4 * int.from_bytes(buf.extract_dup(14, 2), "big")
        if header[0] & 32:               # optional trailing padding
            padding = buf.extract_dup(size - 1, 1)[0]
            if not padding:
                return Gst.PadProbeReturn.DROP
            size -= padding
        if offset >= size:
            return Gst.PadProbeReturn.DROP
        ssrc = int.from_bytes(header[8:12], "big")
        if ssrc == self.my_ssrc:
            self.rx_loopback += 1
            return Gst.PadProbeReturn.DROP
        meta = GstNet.buffer_get_net_address_meta(buf)
        if meta is None or not isinstance(meta.addr, Gio.InetSocketAddress):
            return Gst.PadProbeReturn.DROP
        ip = meta.addr.get_address().to_string()
        # Pin the endpoint as well as the IP budget. Changing the UDP port for
        # an existing SSRC would otherwise grow native collision-address state.
        if not self.sources.admit(ssrc, ip, time.monotonic(), meta.addr.get_port()):
            return Gst.PadProbeReturn.DROP
        self.rx_packets += 1
        self.last_rx_ms = now_ms()
        if self._pipeline_fault:
            self._note_pipeline_ok("rx")
        if not self.rx_active:
            self.rx_active = True
        return Gst.PadProbeReturn.OK

    def _on_capture_buffer(self, _pad, _info):
        """Count capture at the valve, including when PTT is released."""
        self._flow_count["tx"] += 1
        self._flow_ts["tx"] = time.monotonic()
        self._ever_flowed["tx"] = True
        return Gst.PadProbeReturn.OK

    def _on_playback_buffer(self, _pad, _info):
        """Count playback buffers, including the mixer's silent input."""
        self._flow_count["rx"] += 1
        self._flow_ts["rx"] = time.monotonic()
        self._ever_flowed["rx"] = True
        return Gst.PadProbeReturn.OK

    # -- stall watchdog --

    def _tick_health(self):
        """Detect audio flows that stall without a bus error.

        Skip _pipeline_fault entries owned by _note_pipeline_error's backoff.
        Also skip flows that never ran, including missing OpenVLM hardware.
        """
        # Notify systemd independently of voice_watchdog. Only systemd can
        # detect a blocked GLib loop because local checks run on that loop.
        sd_notify("WATCHDOG=1")
        if not self.cfg.watchdog:
            return True

        now = time.monotonic()
        for which in ("tx", "rx"):
            if which in self._pipeline_fault:
                continue            # the error path owns this one
            if not self._ever_flowed[which]:
                continue            # never worked, so nothing has regressed
            since = self._playing_since[which]
            if not since or now - since < VOICE_STALL_GRACE_SEC:
                continue
            idle = now - self._flow_ts[which]
            if idle < self.cfg.watchdog_sec:
                continue
            self._on_stall(which, "no %s for %.0fs"
                           % ("capture buffers" if which == "tx"
                              else "playback buffers", idle))

        self._check_igmp(now)
        return True

    def _check_igmp(self, now):
        """Check for multicast membership lost while the receiver stays PLAYING.

        udpsrc joins once at startup. Rebuilding br0 or removing and re-adding
        its mesh interface can lose the join without a bus error. Quiet-group
        traffic cannot reveal this. On a two-node mesh, losing the join can
        break both directions because batman-adv drops groups with no listeners.
        """
        if "rx" in self._pipeline_fault:
            return
        joined = igmp_groups(self.cfg.iface)
        if joined is None:
            self._igmp_ok = None            # unknown is not a fault
            return
        self._igmp_ok = bool(joined & TALK_GROUP_HEX)
        if self._igmp_ok:
            self._igmp_lost_since = 0.0
            return
        if not self._igmp_lost_since:
            self._igmp_lost_since = now     # one miss could be a rejoin in flight
            return
        if now - self._igmp_lost_since >= self.cfg.watchdog_sec:
            self._igmp_lost_since = 0.0
            self._on_stall("rx", "multicast membership for %s dropped on %s"
                           % (TALK_GROUP_ADDR, self.cfg.iface))

    def _on_stall(self, which, why):
        """Restart a stalled pipeline or escalate repeated stalls to systemd.

        The third stall within ten minutes triggers a process restart for
        faults that in-process pipeline restarts do not clear.
        """
        self._stalls[which] += 1
        now = time.monotonic()
        history = [t for t in self._stall_history[which]
                   if now - t < VOICE_STALL_WINDOW_SEC]
        history.append(now)
        self._stall_history[which] = history
        log("%s pipeline stalled: %s (%d since start, %d inside %ds)"
            % (which, why, self._stalls[which], len(history),
               VOICE_STALL_WINDOW_SEC))
        if len(history) >= VOICE_STALL_MAX_RESTARTS:
            self._panic("%s pipeline stalled %d times in %ds and restarting it "
                        "did not fix it"
                        % (which, len(history), VOICE_STALL_WINDOW_SEC))
            return
        self._restart(which)

    def _tick_rx_decay(self):
        """Clear receive activity after more than 500 ms without packets."""
        if self.rx_active and now_ms() - self.last_rx_ms > RX_IDLE_MS:
            self.rx_active = False
        return True

    def on_ptt_presence(self, connected):
        """Update hardware presence and close transmit on disconnect."""
        self.ptt_connected = connected
        if not connected and self.transmitting:
            self._set_tx(False)
        return False  # idle_add one-shot

    def on_ptt(self, pressed):
        """Apply a PTT edge subject to the half-duplex receive gate."""
        self.ptt_pressed = pressed
        if pressed:
            if self.cfg.half_duplex and self._remote_active():
                log("TX: blocked: half duplex, remote active")
                return False
            self._set_tx(True)
        else:
            self._set_tx(False)
        return False

    def _remote_active(self):
        """Check for remote receive activity within the half-duplex hold."""
        return self.rx_active and now_ms() - self.last_rx_ms < HALF_DUPLEX_HOLD_MS

    # -- conference receive --

    def _configure_rtp_session(self):
        """Use native inactivity expiry, with a PTT-sized retention interval.

        GStreamer 1.26 RTPSession expires at five deterministic RTCP intervals.
        Fix bandwidth high enough that the participant count cannot extend the
        180-second minimum. No RTCP packets are transmitted by this pipeline.
        The native RTCP timer also runs on receive-only sessions without an
        RTCP sink. See docs/node-tools-internals.md for the tagged sources.
        """
        self.rtp_session = self.rtpbin.emit("get-internal-session", 0)
        self.rtp_session.set_property("rtcp-min-interval",
                                      VOICE_RTCP_INTERVAL_SEC * Gst.SECOND)
        self.rtp_session.set_property("bandwidth", 1000000.0)
        # Explicit since 1.24. Older bench GStreamer always expires inactivity.
        if self.rtp_session.find_property("timeout-inactive-sources"):
            self.rtp_session.set_property("timeout-inactive-sources", True)

    def _talker_name(self, ssrc):
        """Resolve a source using the UDP sender address and mesh registry."""
        source = self.sources.get(ssrc)
        ip = source["ip"] if source else None
        return self.talker_names.get(ip) or ip or ("0x%08x" % ssrc)

    def _on_source_timeout(self, rtpbin, _session, ssrc):
        """Mark expiry, keeping its slot until the native table removes it."""
        if rtpbin == self.rtpbin:
            self.sources.mark(ssrc, expired=True)

    def _reap_sources(self):
        """Reconcile native expiry and retire overflow on the existing timer.

        clear-ssrc removes the demux stream, including its jitterbuffer, but
        does NOT remove the RTPSession source record. Retired sources keep an
        admission slot and reject packets until native inactivity removes that
        record. on-timeout is emitted BEFORE the native hash table removal.
        """
        if self.rtp_session is None:
            return
        for ssrc, source in self.sources.pending():
            if source["expired"]:
                if self.rtp_session.emit("get-source-by-ssrc", ssrc) is not None:
                    continue
            self.rtpbin.emit("clear-ssrc", 0, ssrc)
            # A source still in probation may never have had a receive pad.
            self.jitterbuffers.pop(ssrc, None)
            names = (list(self.rx_branches) + list(self.rx_parked)
                     + list(self._discarded_pads))
            for name in names:
                if self._ssrc_of(name) == ssrc:
                    self._remove_branch(name)
            self.sources.mark(ssrc, cleared=True)
            if source["expired"]:
                self.sources.forget(ssrc)

    def _on_new_jitterbuffer(self, _rtpbin, jitterbuffer, session, ssrc):
        """Retain a new talker's jitter buffer for loss statistics.

        _tick_packing sums receive loss across talkers to estimate link loss.
        """
        with self.sources.lock:
            source = self.sources.get(ssrc)
            if (_rtpbin != self.rtpbin or source is None
                    or source["expired"] or source["retired"]):
                return
            self.jitterbuffers[ssrc] = jitterbuffer
        who = self._talker_name(ssrc)
        log("rx: new talker %s (ssrc 0x%08x)" % (who, ssrc))

    def _on_rtp_pad_added(self, _rtpbin, pad):
        """Attach a decode branch to a new talker's receive pad.

        _park_branch handles later revival of evicted talkers.
        """
        name = pad.get_name()
        if not name.startswith("recv_rtp_src_"):
            return
        source = self.sources.get(self._ssrc_of(name))
        if _rtpbin != self.rtpbin or source is None:
            return
        if source["retired"] or source["expired"]:
            self._discard_pad(name, pad)
            return
        if name in self.rx_branches or name in self.rx_parked:
            return
        # Park failed branches too. An unlinked rtpbin pad would raise bus
        # errors.
        if not self._attach_branch(pad):
            self._park_branch(name, pad)
            return
        self._prune_branches()

    def _attach_branch(self, pad):
        """Build one talker's decode branch and feed it into the mixer."""
        name = pad.get_name()
        try:
            # Use an explicit capsfilter at the end of a bin description. Bare
            # "audio/x-raw,..." is parsed as an element and fails with "no
            # element audio". Every branch must reach the mixer in the same
            # format.
            depay = Gst.parse_bin_from_description(
                "%s ! %s ! %saudioconvert ! audioresample ! capsfilter "
                "caps=\"audio/x-raw, rate=(int)%d, channels=(int)1, "
                "format=(string)S16LE\""
                % (self._rx_depay_desc, self._rx_dec_desc,
                   self._rx_gain_stage(), self._rx_raw_rate),
                True)
        except GLib.Error as exc:
            log("rx: cannot build branch for %s: %s" % (name, exc))
            return False

        self.rx.add(depay)
        mixpad = self.mixer.request_pad_simple("sink_%u")
        if mixpad is None:
            log("rx: audiomixer refused a pad for %s" % name)
            self.rx.remove(depay)
            return False

        if (pad.link(depay.get_static_pad("sink")) != Gst.PadLinkReturn.OK
                or depay.get_static_pad("src").link(mixpad)
                != Gst.PadLinkReturn.OK):
            log("rx: link failed for %s" % name)
            self.mixer.release_request_pad(mixpad)
            self.rx.remove(depay)
            return False

        depay.sync_state_with_parent()
        # Retain the rtpbin pad for parking and revival.
        self.branch_seen[name] = time.monotonic()
        # Touch on every decoded buffer so the LRU below evicts the talker who
        # has been quiet longest, not whoever happened to arrive first.
        probe = depay.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, self._on_decoded_buffer, name)
        entry = (pad, depay, mixpad, probe)
        with self.sources.lock:
            source = self.sources.get(self._ssrc_of(name))
            alive = source and not source['retired'] and not source['expired']
            if alive:
                self.rx_branches[name] = entry
        if not alive:
            self.branch_seen.pop(name, None)
            self._destroy_decoder(entry)
            return False
        log("rx: talker branch up (%d active)" % len(self.rx_branches))
        return True

    def _on_decoded_buffer(self, _pad, _info, name):
        """Touch an existing decoder without resurrecting removed records."""
        if name in self.rx_branches:
            self.branch_seen[name] = time.monotonic()
        return Gst.PadProbeReturn.OK

    def _schedule_beacon(self, delay_ms):
        """Queue a beacon after delay_ms if none is pending."""
        if self._beacon_pending is None:
            self._beacon_pending = GLib.timeout_add(
                delay_ms, self._beacon_once)

    def _beacon_once(self):
        """Clear the pending timer and attempt one presence beacon."""
        self._beacon_pending = None
        self._tick_beacon()
        return False

    def _tick_beacon(self):
        """Send muted RTP so receivers establish this source before speech.

        Tried: injecting a peer SSRC locally. A 3 s burst yields zero amplitude.
        Invented sequence numbers and timestamps cause jitter-buffer resync.
        Use the sender's payloader: mute, open for BEACON_MS, close, unmute.
        At default 600 s and 40 ms packets, periodic traffic is about 21
        packets/hour. batman-adv drops it without listeners.
        """
        if self.transmitting or self.ptt_pressed:
            return True                      # never interrupt a transmission
        if (not self.valve or not self.volume or self._beacon_end is not None
                or not self._tx_ready()):
            return True
        self.volume.set_property("volume", 0.0)
        self.valve.set_property("drop", False)
        self._beacon_end = GLib.timeout_add(BEACON_MS, self._end_beacon)
        return True

    def _end_beacon(self):
        """End a muted beacon while preserving active PTT transmission."""
        self._beacon_end = None
        # _set_tx restores volume on PTT. Keep the valve open if transmission
        # starts.
        if not self.transmitting and self.valve:
            self.valve.set_property("drop", True)
        if self.volume:
            self.volume.set_property("volume", 1.0)
        return False                          # one-shot

    def _size_talker_table(self, peer_count):
        """Grow the warm branch table to cover known peers plus headroom.

        Keep at least the configured floor and at most VOICE_MAX_TALKERS_HARD.
        Retaining potential talkers avoids first-contact loss after eviction.
        """
        want = min(peer_count + VOICE_TALKER_HEADROOM, VOICE_MAX_TALKERS_HARD)
        if want > self.cfg.max_talkers:
            log("rx: %d nodes known: raising warm talker table %d -> %d "
                "(~%d MB with lyra)"
                % (peer_count, self.cfg.max_talkers, want, want * 5))
            self.cfg.max_talkers = want
        elif peer_count + VOICE_TALKER_HEADROOM > VOICE_MAX_TALKERS_HARD:
            log("rx: %d nodes known but the warm talker table is capped at %d: "
                "the least recently heard will be evicted and pay a "
                "first-contact delay when they next speak"
                % (peer_count, VOICE_MAX_TALKERS_HARD))

    def _prune_branches(self):
        """Evict the quietest branches until the configured talker cap is met.

        Measured Lyra cost: 5.3 MB per branch,
        roughly 40 MB for the default floor of 8 against 3.4 GB free on a CM4.
        Park evicted pads for revival, with first-contact loss on next speech.
        See _park_branch and the conference receive measurements.
        """
        while len(self.rx_branches) > self.cfg.max_talkers:
            with self.sources.lock:
                if len(self.rx_branches) <= self.cfg.max_talkers:
                    break
                oldest = min(self.rx_branches,
                             key=lambda name: self.branch_seen.get(name, 0),
                             default=None)
                entry = self.rx_branches.pop(oldest, None)
                self.branch_seen.pop(oldest, None)
            if entry is None:
                continue
            pad = entry[0]
            self._destroy_decoder(entry)
            self._park_branch(oldest, pad)
            log("rx: evicted least-recent talker branch (cap %d)"
                % self.cfg.max_talkers)

    def _park_branch(self, name, pad):
        """Park an evicted talker's pad on a fakesink with a buffer probe.

        The fakesink avoids NOT_LINKED bus errors. Its buffer probe schedules
        branch revival when the talker returns, restoring a receive path.
        """
        # Different jitterbuffer threads can create pads concurrently. Reserve
        # capacity before calling Gst, which releases the Python GIL.
        with self.sources.lock:
            if name in self._parking or name in self.rx_parked:
                return
            source = self.sources.get(self._ssrc_of(name))
            room = (source and not source['retired'] and not source['expired']
                    and len(self.rx_parked) + len(self._parking) < VOICE_MAX_PARKED)
            if room:
                self._parking.add(name)
        if not room:
            self._discard_pad(name, pad)
            return
        try:
            self._make_parked_sink(name, pad)
        finally:
            with self.sources.lock:
                self._parking.discard(name)

    def _make_parked_sink(self, name, pad):
        """Build a sink for a reserved parking slot, outside the state lock."""
        sink = Gst.ElementFactory.make("fakesink", None)
        if sink is None:
            self._discard_pad(name, pad)
            return
        sink.set_property("sync", False)
        sink.set_property("async", False)
        self.rx.add(sink)
        if pad.link(sink.get_static_pad("sink")) != Gst.PadLinkReturn.OK:
            log("rx: could not park %s" % name)
            self.rx.remove(sink)
            self._discard_pad(name, pad)
            return
        sink.sync_state_with_parent()
        probe = pad.add_probe(Gst.PadProbeType.BUFFER, self._on_parked_buffer,
                              name)
        with self.sources.lock:
            source = self.sources.get(self._ssrc_of(name))
            alive = source and not source['retired'] and not source['expired']
            if alive:
                self._parking.discard(name)
                self.rx_parked[name] = (pad, sink, probe)
        if not alive:
            pad.remove_probe(probe)
            pad.unlink(sink.get_static_pad("sink"))
            sink.set_state(Gst.State.NULL)
            self.rx.remove(sink)

    def _discard_pad(self, name, pad):
        """Drop overflow until the main loop can remove its native stream."""
        with self.sources.lock:
            self.sources.mark(self._ssrc_of(name), retired=True)
            if name not in self._discarded_pads:
                probe = pad.add_probe(Gst.PadProbeType.BUFFER,
                                      lambda _p, _i: Gst.PadProbeReturn.DROP)
                self._discarded_pads[name] = (pad, probe)

    def _on_parked_buffer(self, _pad, _info, name):
        """Schedule decoder revival when a parked talker sends a buffer.

        The streaming thread sends this buffer to the fakesink. Relinking and
        element creation run on the main loop. Rebuilding costs about 180 ms
        of initial audio, per the conference receive measurements.
        """
        with self.sources.lock:
            if name in self.rx_parked and name not in self._reviving:
                self._reviving[name] = GLib.idle_add(self._revive_branch, name)
        return Gst.PadProbeReturn.OK

    def _revive_branch(self, name):
        """Take a talker off the fakesink and give them a decoder again."""
        with self.sources.lock:
            self._reviving.pop(name, None)
            entry = self.rx_parked.pop(name, None)
        if entry is None:
            return False
        pad, sink, probe = entry
        if probe:
            pad.remove_probe(probe)
        pad.unlink(sink.get_static_pad("sink"))
        sink.set_state(Gst.State.NULL)
        self.rx.remove(sink)
        who = self._talker_name(self._ssrc_of(name))
        log("rx: parked talker %s is back, rebuilding their branch" % who)
        if not self._attach_branch(pad):
            self._park_branch(name, pad)      # keep the pad linked and watched
            return False
        self._prune_branches()
        return False

    @staticmethod
    def _ssrc_of(pad_name, default=0):
        """SSRC out of an rtpbin pad name: recv_rtp_src_<session>_<ssrc>_<pt>."""
        try:
            return int(pad_name.split("_")[4])
        except (IndexError, ValueError):
            return default

    def _on_rtp_pad_removed(self, _rtpbin, pad):
        """Release a talker's branch when rtpbin removes its receive pad.

        Release decoder memory and CPU resources, including Lyra's TFLite
        interpreter, so repeated sessions cannot accumulate them.
        """
        if _rtpbin != self.rtpbin:
            return
        name = pad.get_name()
        if not name.startswith("recv_rtp_src_"):
            return
        self._remove_branch(name)
        self.jitterbuffers.pop(self._ssrc_of(name), None)

    def _destroy_decoder(self, entry):
        """Release the decoded-buffer probe, links, bin and mixer request pad."""
        pad, branch, mixpad, probe = entry
        branch.get_static_pad("src").remove_probe(probe)
        pad.unlink(branch.get_static_pad("sink"))
        branch.get_static_pad("src").unlink(mixpad)
        branch.set_state(Gst.State.NULL)
        self.rx.remove(branch)
        self.mixer.release_request_pad(mixpad)

    def _remove_branch(self, name):
        """Release all Python-owned objects for a removed or retired pad."""
        with self.sources.lock:
            revival = self._reviving.pop(name, None)
            parked = self.rx_parked.pop(name, None)
            entry = self.rx_branches.pop(name, None)
            self.branch_seen.pop(name, None)
            discarded = self._discarded_pads.pop(name, None)
        if revival is not None:
            GLib.source_remove(revival)
        if discarded is not None:
            pad, probe = discarded
            pad.remove_probe(probe)
        if parked is not None:
            pad, sink, probe = parked
            if probe:
                pad.remove_probe(probe)
            pad.unlink(sink.get_static_pad("sink"))
            sink.set_state(Gst.State.NULL)
            self.rx.remove(sink)
        if entry is not None:
            self._destroy_decoder(entry)

    def _rx_loss_stats(self):
        """(pushed, lost) summed over every current talker's jitter buffer."""
        pushed = lost = 0
        for jb in list(self.jitterbuffers.values()):
            try:
                stats = jb.get_property("stats")
                pushed += stats.get_value("num-pushed") or 0
                lost += stats.get_value("num-lost") or 0
            except Exception:
                continue
        return pushed, lost

    # -- adaptive packing --

    def _link_mbps(self):
        """Return batman-adv's lowest neighbour throughput estimate in Mbit/s.

        Return None for unavailable batctl, insufficient privilege or no mesh.
        Callers treat None as unknown capacity.
        """
        try:
            # Bound the main-loop throughput probe to three seconds.
            out = subprocess.run([BATCTL, "meshif", "bat0", "n"],
                                 capture_output=True, text=True, timeout=3)
            if out.returncode != 0:
                return None
        except (OSError, subprocess.SubprocessError):
            return None
        rates = []
        for line in out.stdout.splitlines():
            # "0c:bf:74:00:2b:f1    0.108s (       43.2) [     wlan2]"
            # Parenthesized decimal values are BATMAN V rates in Mbit/s.
            m = re.search(r"\(\s*([0-9]+\.?[0-9]*)\s*\)", line)
            if m:
                try:
                    rates.append(float(m.group(1)))
                except ValueError:
                    pass
        return min(rates) if rates else None

    def _set_packing(self, fpp, why):
        """Request a new Lyra frame count per packet and log the reason."""
        if fpp == self.packing or self.payloader is None:
            return
        self.packing = fpp
        try:
            self.payloader.set_property("frames-per-packet", fpp)
        except Exception as exc:
            log("packing: could not set frames-per-packet=%d: %s" % (fpp, exc))
            return
        # Estimate kbps from 50 frames/s, eight bits/byte and 86 header bytes.
        log("packing: %d frame(s)/packet (%d ms, ~%.1f kbps on air): %s"
            % (fpp, fpp * 20, 0.4 * (86.0 / fpp + LYRA_FRAME_BYTES.get(
                self.cfg.lyra_bitrate, 15)), why))

    def _tick_packing(self):
        """Adapt frames per packet to receive loss at fixed codec bitrate.

        A 20 ms frame costs 101 bytes on air, including 86 bytes of headers.
        Packing 2 frames instead of 1 saves 43%. Reducing 6000 to 3200 bps saves
        12% with audible quality loss. Smaller packets shorten each audio gap.
        Listening test at 10% loss:
          20 ms  inaudible       40 ms  barely audible
          60 ms  clearly audible 80 ms  unpleasant
        Use smaller packets under loss unless the throughput floor indicates
        congestion, where extra packets can worsen loss. Receive loss summed
        across talkers is a proxy for transmit loss: multicast RTP has no back
        channel, and asymmetric links can invalidate the estimate.
        """
        if self.cfg.codec != "lyra":
            return False        # no adaptation for this codec
        if getattr(self, "effective_codec", self.cfg.codec) != "lyra":
            self._pk_clean_since = time.monotonic()
            return True         # fallback payloader has no Lyra packing
        if self.transmitting:
            # Skip while transmitting: local traffic consumes airtime, and its
            # packets are excluded from receive statistics.
            self._pk_clean_since = time.monotonic()
            return True

        pushed, lost = self._rx_loss_stats()

        d_pushed = pushed - self._pk_last_pushed
        d_lost = lost - self._pk_last_lost
        self._pk_last_pushed, self._pk_last_lost = pushed, lost
        total = d_pushed + d_lost
        if total < PACKING_MIN_SAMPLE:
            # Too little traffic in this window to judge. Hold, and do not let
            # silence count as "clean" toward reclaiming airtime.
            self._pk_clean_since = time.monotonic()
            return True

        loss_pct = 100.0 * d_lost / total
        self.rx_loss_pct = loss_pct

        floor = PACKING_MIN
        mbps = self._link_mbps()
        if mbps is not None and mbps < PACKING_LINK_FLOOR_MBPS:
            # The link is already struggling for capacity. Do not answer loss
            # by offering more packets.
            floor = max(floor, PACKING_DEFAULT)

        now = time.monotonic()
        if loss_pct > PACKING_LOSS_HIGH_PCT:
            self._pk_clean_since = now
            if self.packing > floor:
                self._set_packing(self.packing - 1,
                                  "loss %.1f%% > %.0f%%" % (loss_pct,
                                                            PACKING_LOSS_HIGH_PCT))
        elif loss_pct < PACKING_LOSS_LOW_PCT:
            if now - self._pk_clean_since >= PACKING_UP_HOLD_SEC:
                if self.packing < PACKING_MAX:
                    self._set_packing(self.packing + 1,
                                      "clean %.0fs at %.1f%% loss"
                                      % (now - self._pk_clean_since, loss_pct))
                self._pk_clean_since = now
        else:
            # Reset the clean timer between thresholds. Recovery requires
            # sustained loss below the low threshold.
            self._pk_clean_since = now
        return True

    def _tx_ready(self):
        """Require both the configured address and a device-bound socket."""
        reason = "waiting for IPv4 on %s" % self.cfg.iface
        try:
            sock = self.sink.get_property("used-socket")
            if sock is None:
                fault = self._pipeline_fault.get("tx")
                if fault and fault["text"]:
                    reason = "transmit pipeline unavailable: %s" % fault["text"]
                elif self._bind_ip:
                    reason = "waiting for transmit pipeline"
            elif self._bind_ip:
                reason = "send socket is not bound to %s" % self.cfg.iface
                try:
                    # GStreamer only warns if SO_BINDTODEVICE fails.
                    # Duplicate the fd so this check leaves GStreamer's socket
                    # open. Device names occupy a 16-byte socket option buffer.
                    with socket.fromfd(Gio.Socket.get_fd(sock), socket.AF_INET,
                                       socket.SOCK_DGRAM) as fd:
                        addr = fd.getsockname()[0]
                        device = fd.getsockopt(socket.SOL_SOCKET,
                                               socket.SO_BINDTODEVICE, 16)
                    if (addr == self._bind_ip
                            and device.rstrip(b"\0") == self.cfg.iface.encode()):
                        reason = None
                except (AttributeError, OSError, GLib.Error):
                    pass
        except (AttributeError, OSError, GLib.Error):
            if self._bind_ip:
                reason = "transmit pipeline unavailable"
        self._tx_blocked = reason
        if reason is None:
            self._tx_blocked_logged = False
        return reason is None

    def _set_tx(self, on):
        """Gate transmit on socket readiness and restore volume for speech."""
        if on and not self._tx_ready():
            self._set_tx(False)
            if not self._tx_blocked_logged:
                log("TX: blocked: %s" % self._tx_blocked)
                self._tx_blocked_logged = True
            self.write_state()
            return
        changed = on != self.transmitting
        self.transmitting = on
        if on and self.volume:
            # Restore volume after a beacon before opening PTT.
            self.volume.set_property("volume", 1.0)
        if self.valve:
            self.valve.set_property("drop", not on)
        if changed:
            log("TX: %s" % ("start" if on else "stop"))

    # -- peers --

    def _tick_peers(self):
        """Check interface binding, resume held PTT and refresh peer state."""
        resume = self._tx_blocked is not None
        bind_ip = iface_ipv4(self.cfg.iface)
        if bind_ip != self._bind_ip:
            resume = True
            self._set_tx(False)
            self._bind_ip = None
            if bind_ip:
                log("TX: rebinding to %s on %s" % (bind_ip, self.cfg.iface))
                if not self._retune(self.cfg.channel, self.cfg.port):
                    self._panic("voice interface rebind failed")
                    return False
                self.local_ips = local_ipv4_addresses()
                if self.cfg.beacon_sec:
                    self._schedule_beacon(500)
        if not self._tx_ready():
            self._set_tx(False)
        elif resume and self.ptt_pressed and not self.transmitting:
            self.on_ptt(True)
        self.refresh_peers()
        self.write_state()
        return True

    def refresh_peers(self):
        """Set multicast and optional unicast destinations from active peers.

        The clients property is writable while PLAYING. Always read the registry
        for talker names, branch sizing and new-peer beacons.
        """
        clients = ["%s:%d" % (TALK_GROUP_ADDR, self.cfg.port)]

        # Read the registry even with unicast disabled for names, sizing and
        # beacons.
        registry = read_registry(self.local_ips)

        peers = []
        if self.cfg.unicast:
            peers = registry
            if self.cfg.max_peers and len(peers) > self.cfg.max_peers:
                peers = peers[:self.cfg.max_peers]
            clients += ["%s:%d" % (ip, self.cfg.port) for ip, _ in peers]

        # Every known node is a potential talker, so the warm-branch table and
        # the ssrc->name map are both driven from the registry.
        self._size_talker_table(len(registry))
        self.talker_names = {ip: host or ip for ip, host in registry}

        # Announce to new peers before waiting for the periodic beacon.
        new_ips = {ip for ip, _ in registry} - self._known_peer_ips
        if new_ips and self.cfg.beacon_sec:
            log("beacon: %d new node(s) in registry: announcing" % len(new_ips))
            self._schedule_beacon(500)
        self._known_peer_ips = {ip for ip, _ in registry}

        if peers != self.peers:
            log("peers: %d unicast target(s)%s" % (
                len(peers),
                (": " + ", ".join(h or ip for ip, h in peers)) if peers else ""))
        self.peers = peers
        if self.sink:
            self.sink.set_property("clients", ",".join(clients)
                                   if self._bind_ip else "")

    # -- state --

    def _idle(self, which):
        """Seconds since this flow last produced, or None if it never has."""
        if not self._ever_flowed[which]:
            return None
        return round(max(0.0, time.monotonic() - self._flow_ts[which]), 1)

    def _tick_state(self):
        """Write UI state and retain the periodic callback."""
        self._reap_sources()
        self.write_state()
        return True

    def write_state(self):
        """Atomically publish transport, audio and watchdog state for the UI."""
        # Aggregate late, duplicate, lost and pushed packets for the UI.
        stats = {}
        late = dups = 0
        for jb in list(self.jitterbuffers.values()):
            try:
                s = jb.get_property("stats")
                late += s.get_value("num-late") or 0
                dups += s.get_value("num-duplicates") or 0
            except Exception:
                continue
        pushed, lost = self._rx_loss_stats()
        stats = {"num-lost": lost, "num-late": late, "num-duplicates": dups,
                 "num-pushed": pushed}
        codec = getattr(self, "effective_codec", self.cfg.codec)

        state = {
            # Preserve the fault in the VOICE tab through systemd's 10 s restart
            # delay.
            "service": "restarting" if self._fault else "running",
            "fault": self._fault,
            "uptime": int(time.monotonic() - self.started_at),
            "ptt_mode": self.cfg.ptt_mode,
            "ptt_connected": self.ptt_connected,
            "ptt_active": self.ptt_pressed,
            "ptt_device": self.ptt.device if self.ptt else None,
            "tx": self.transmitting,
            "tx_blocked": self._tx_blocked,
            "bind_address": self._bind_ip,
            "rx": self.rx_active,
            "channel": self.cfg.channel,
            "group": TALK_GROUP_ADDR,
            "port": self.cfg.port,
            "interface": self.cfg.iface,
            "dscp": self.cfg.dscp,
            "codec": codec,
            "codec_configured": self.cfg.codec,
            "codec_fallback": codec != self.cfg.codec,
            "bitrate": (self.cfg.lyra_bitrate if codec == "lyra"
                        else self.cfg.bitrate),
            "frame_ms": (self.packing * 20 if codec == "lyra"
                         else self.cfg.frame_ms),
            "frames_per_packet": (self.packing if codec == "lyra"
                                  else None),
            "rx_loss_pct": round(self.rx_loss_pct, 1),
            "unicast": self.cfg.unicast,
            "talkers": len(self.rx_branches),
            "talkers_parked": len(self.rx_parked),
            "talker_names": sorted(
                self._talker_name(sr)
                for sr in list(self.jitterbuffers)),
            "max_talkers": self.cfg.max_talkers,
            "rtp_sources": len(self.sources.sources),
            "rtp_source_limit": VOICE_MAX_SOURCES,
            "rtp_admission_drops": self.sources.dropped,
            "parked_limit": VOICE_MAX_PARKED,
            "peers": [{"ip": ip, "hostname": host} for ip, host in self.peers],
            "tx_packets": self.tx_packets,
            "rx_packets": self.rx_packets,
            "rx_lost": stats.get("num-lost", 0),
            "rx_late": stats.get("num-late", 0),
            "rx_duplicates": stats.get("num-duplicates", 0),
            "rx_loopback": self.rx_loopback,
            "highpass_hz": self.cfg.highpass_hz,
            "lowpass_hz": self.cfg.lowpass_hz,
            "eq": ([round(g, 2) for g in self._eq_gains(self._raw_rate)]
                   if self.cfg.eq else None),
            "tx_gain_db": round(20.0 * math.log10(self._tx_gain), 1),
            "rx_gain_db": self.cfg.rx_gain_db,
            "playback_level": getattr(self, "playback_level", None),
            # Watchdog capture/playback flows run while idle. Packet counters
            # stay flat between transmissions.
            "watchdog": self.cfg.watchdog,
            "watchdog_sec": self.cfg.watchdog_sec,
            "capture_buffers": self._flow_count["tx"],
            "playback_buffers": self._flow_count["rx"],
            # None means the flow has never run, including absent OpenVLM
            # hardware. The watchdog checks only flows that have previously
            # produced audio.
            "capture_idle": self._idle("tx"),
            "playback_idle": self._idle("rx"),
            "stalls": self._stalls["tx"] + self._stalls["rx"],
            "igmp_joined": self._igmp_ok,
            "updated": int(time.time()),
            # Boot-clock stamp for cross-process freshness checks. Wall time is
            # for display and can step during time sync.
            "updated_boot": time.clock_gettime(time.CLOCK_BOOTTIME),
        }
        # Readers see a complete snapshot even when polling during a write.
        tmp = STATE_FILE + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(state, fh)
            os.replace(tmp, STATE_FILE)
        except OSError as exc:
            log("state write failed: %s" % exc)


def main():
    """Start configured voice and return its shutdown or restart exit status."""
    if GST_IMPORT_ERROR is not None:
        log("GStreamer Python bindings unavailable (%s): voice disabled. "
            "Install python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base "
            "gstreamer1.0-plugins-good gstreamer1.0-alsa." % GST_IMPORT_ERROR)
        return 0

    cfg = Config()
    if not cfg.enabled and "--force" not in sys.argv:
        log("voice=n in %s: nothing to do" % MESH_CONF)
        return 0

    Gst.init(None)

    voice = MeshVoice(cfg)
    try:
        voice.build()
    except GLib.Error as exc:
        log("pipeline build failed: %s" % exc)
        return 1

    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig,
                             lambda *_: (voice.stop(), False)[1])
    # SIGHUP applies mesh.conf changes from the web UI. Return True to retain
    # the handler for subsequent reloads and future rotary-switch input.
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGHUP,
                         lambda *_: (voice.reload(), True)[1])

    voice.start()
    try:
        voice.loop.run()
    except KeyboardInterrupt:
        voice.stop()
    # _panic sets a nonzero exit status to trigger Restart=on-failure.
    return voice.exit_code


if __name__ == "__main__":
    sys.exit(main())
