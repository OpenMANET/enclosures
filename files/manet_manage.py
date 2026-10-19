#!/usr/bin/env python3
"""Provide authenticated management routes for mesh-status.py.

The /manage routes share mesh-status.py's listener and require the admin
password. Node state comes from the Alfred-built registry. Changes affecting
peers are staged over Alfred. Measurements run local clients against each
peer's listening iperf3 daemon.

Routes (relative to /manage):
  GET  /                        - Management UI HTML
  GET  /api/topology            - Mesh topology (nodes, interfaces)
  POST /api/interface/toggle    - Toggle wlan interface on node(s)
  POST /api/halow/channel       - Set HaLow channel/BW on all nodes
  POST /api/wifi/channel        - Set Wi-Fi channel on all nodes
  POST /api/txpower             - Set TX power on node/interface
  POST /api/measure/start       - Start iperf3/ping session
  GET  /api/measure/status      - Current measurement status
  GET  /api/sessions            - List saved sessions
  GET  /api/sessions/<id>       - Get session JSON
  GET  /api/sessions/<id>/csv   - Get session CSV
  DELETE /api/sessions/<id>     - Delete saved session
  GET  /api/uplink/wifi         - Local built-in Wi-Fi uplink status
  POST /api/uplink/wifi         - Set node-local Wi-Fi uplink credentials
  POST /api/uplink/wifi/forget  - Forget a local saved password without disconnecting
"""

import json
import glob
import html
import ipaddress
import subprocess
import re
import stat
import os
import time
import threading
import csv
import io
import shutil
import hashlib
import urllib.request
import secrets
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, unquote

from manet_config_io import load_kv_file
from manet_node_info import get_my_hostname, get_local_uptime, fmt_uptime

from manet_web_limits import STATUS_CACHE, Busy
from manet_recovery_status import recovery_status, RECOVERY_JS
from manet_peer_radios import peer_radio_interfaces
from manet_registry import node_state, parse_registry
from manet_measurement_storage import check_capacity, save_result
from manet_admin import AdminTransport, new_version
from manet_radio import (
    HALOW_POWER_FIXED, get_halow_driver_info, get_iface_txpower_cap,
    halow_channel_options, is_halow_iface, txpower_choices_from_cap,
    validate_wifi_channel,
)

MESH_CONF_FILE  = '/etc/mesh.conf'
MESH_STATE_FILE = '/etc/mesh_ipv4_state'
REGISTRY_FILE   = '/var/run/mesh_node_registry'
SESSIONS_DIR    = '/var/log/manet-measurements'
ALFRED_RADIO_TYPE = 71
ALFRED_RADIO_ACK_TYPE = 72
# Radio commands and ACKs share the password-authenticated Alfred transport.
ADMIN = AdminTransport()
# The unfilled hexagon uses dark strokes on light themes and white on dark.
FER_LOGO_DARK_INK_FILE  = '/usr/local/share/manet/fer-logo-black.png'
FER_LOGO_LIGHT_INK_FILE = '/usr/local/share/manet/fer-logo-white.png'
# /assets/<name> accepts an optional extension for either SVG or PNG.
FER_LOGO_ASSETS = {
    'fer-logo':       FER_LOGO_DARK_INK_FILE,
    'fer-logo-black': FER_LOGO_DARK_INK_FILE,
    'fer-logo-white': FER_LOGO_LIGHT_INK_FILE,
}


def new_csp_nonce():
    """Give each HTML response an unpredictable 256-bit script capability."""
    return secrets.token_urlsafe(32)


def content_security_policy(nonce):
    """Allow only explicitly nonced scripts and the UI's local resources."""
    return ("default-src 'self'; "
            f"script-src 'nonce-{nonce}'; script-src-attr 'none'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'")


def logo_asset_token():
    """Return a logo URL token that invalidates the one-hour cache."""
    parts = []
    for path in (FER_LOGO_DARK_INK_FILE, FER_LOGO_LIGHT_INK_FILE):
        try:
            st = os.stat(path)
            parts.append(f'{st.st_mtime_ns}:{st.st_size}')
        except OSError:
            parts.append('missing')
    # Eight hex digits identify both theme assets for browser cache refresh.
    return hashlib.sha1('|'.join(parts).encode()).hexdigest()[:8]

# The S1G channel plan is region-dependent and lives in manet_radio, so the
# UI, the Alfred applier and mesh-status all offer the same channels.

# Active measurement state
MEASURE_BUDGET_SEC = 3600
MEASURE_SETUP_SEC = 70  # Radio snapshot command timeouts plus local setup.
MEASURE_PAUSE_SEC = 2
MEASURE_ROUTE_SEC = 5
MEASURE_LOCAL_SEC = 5  # Per-test registry, capacity checks and result storage.
_measure_lock   = threading.Lock()
_measure_status = {
    'running': False, 'label': '', 'progress': '', 'error': '',
    'done': 0, 'total': 0, 'started_at': None, 'current_started_at': None,
    'current': None, 'last_result': None,
}
# Monotonic copies of started_at/current_started_at prevent clock steps
# from distorting durations. Wall-clock values date the records.
_measure_mono = {'started': None, 'current': None}


def measure_status_snapshot():
    """The status reply, with durations measured on the monotonic clock."""
    now = time.monotonic()
    with _measure_lock:
        status = dict(_measure_status)
        started, current = _measure_mono['started'], _measure_mono['current']
    status['elapsed'] = int(now - started) if started is not None and status['started_at'] else None
    status['current_elapsed'] = (int(now - current)
                                 if current is not None and status['current_started_at'] else None)
    return status


# Helpers



def get_my_ip():
    """Read this node's current or persistent mesh IPv4 address."""
    state = load_kv_file(MESH_STATE_FILE)
    # mesh-ip-manager.sh only ever writes PERSISTENT_IPV4 to this file
    return state.get('CURRENT_IPV4') or state.get('PERSISTENT_IPV4', '')

def has_internet():
    """Check HTTPS reachability to GitHub with a three-second timeout."""
    try:
        urllib.request.urlopen('https://github.com', timeout=3)
        return True
    except Exception:
        return False

def builtin_wifi_request(action, request=None):
    """Run a local client action behind the existing authenticated admin routes."""
    command = ['/usr/local/bin/manet_wifi_uplink.py', action]
    if action in ('scan', 'scan-plan'):
        iface = (request or {}).get('iface')
        if iface in ('wlan0', 'wlan1'):
            command.append(iface)
            if action == 'scan' and request.get('allow_ap_stop') is True:
                command.append('--allow-ap-stop')
        else:
            raise ValueError('Choose a radio before scanning')
    if action == 'set' and (request or {}).get('iface') not in ('wlan0', 'wlan1'):
        raise ValueError('Choose a built-in radio before connecting')
    try:
        result = subprocess.run(command, input=json.dumps(request or {}),
                                capture_output=True, text=True, timeout=150)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError('Wi-Fi operation failed. Try again.') from error
    try:
        response = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError('Wi-Fi helper returned no status') from error
    if result.returncode:
        raise RuntimeError(response.get('error', 'Wi-Fi operation failed'))
    return response


def wifi_uplink_status():
    """Report the local built-in client without exposing credentials."""
    builtin = builtin_wifi_request('status')
    return {'builtin': builtin,
            'saved_networks': builtin.get('saved_networks', [])}


def get_bat0_active_ifaces():
    """List interfaces reported active by batctl, or empty on failure."""
    try:
        # Match only "interface: active" rows, with a five-second probe limit.
        r = subprocess.run(['batctl', 'if'], capture_output=True, text=True, timeout=5)
        return [
            m.group(1)
            for l in r.stdout.splitlines()
            for m in [re.match(r'^\s*([^:\s]+):\s+active\b', l)]
            if m
        ]
    except Exception:
        return []



def get_session_hop_count(dst_ip):
    """Return hops to dst from the local BATMAN table used by measurements."""
    if not dst_ip:
        return None, 'missing'
    try:
        # Limit route collection to five seconds per measurement.
        r = subprocess.run(['batctl', 'o', '-n'], capture_output=True, text=True,
                           timeout=MEASURE_ROUTE_SEC)
    except Exception:
        return None, 'error'
    nodes = parse_registry(REGISTRY_FILE)
    dst_macs = set()
    for nd in nodes.values():
        if nd.get('IPV4_ADDRESS') == dst_ip:
            dst_macs = {m.strip().lower()
                        for m in (nd.get('MAC_ADDRESSES', '') or '').split(',') if m.strip()}
            break
    if not dst_macs:
        return None, 'unknown'
    fallback = None, 'unknown'
    for line in r.stdout.splitlines():
        # The route marker and padding inside the metric shift token columns.
        row = re.match(
            r'^\s*(\*)?\s*([0-9a-f:]{17})\s+[\d.]+(?:ms|s)\s+'
            r'\(\s*[\d.]+\s*\)\s+([0-9a-f:]{17})(?:\s|$)',
            line, re.I)
        if row and row.group(2).lower() in dst_macs:
            # A direct originator is one hop. Other nexthops need at least two.
            nexthop = row.group(3).lower()
            hops = (1 if nexthop in dst_macs else 2), 'batctl'
            if row.group(1):
                return hops
            if fallback[0] is None:
                fallback = hops
    return fallback


def extract_iperf3_metrics(iperf):
    """Extract throughput, jitter and loss from iperf3 result variants."""
    metrics = {
        'tcp_mbps': None,
        'udp_mbps': None,
        'jitter_ms': None,
        'loss_pct': None,
    }
    if not isinstance(iperf, dict):
        return metrics

    end = iperf.get('end', {}) or {}

    def _mbps(section):
        """Convert a result section's bits per second to decimal Mbit/s."""
        try:
            bps = section.get('bits_per_second')
            if bps is None:
                return None
            return round(float(bps) / 1e6, 2)
        except Exception:
            return None

    # Prefer received TCP totals to sent totals. UDP summaries use end.sum.
    for key in ('sum_received', 'sum_sent', 'sum'):
        section = end.get(key)
        if isinstance(section, dict):
            value = _mbps(section)
            if value is not None:
                if key == 'sum':
                    metrics['udp_mbps'] = value
                elif metrics['tcp_mbps'] is None:
                    metrics['tcp_mbps'] = value

    sum_section = end.get('sum')
    if isinstance(sum_section, dict):
        try:
            if sum_section.get('jitter_ms') is not None:
                metrics['jitter_ms'] = round(float(sum_section.get('jitter_ms')), 3)
        except Exception:
            pass
        try:
            if sum_section.get('lost_percent') is not None:
                metrics['loss_pct'] = round(float(sum_section.get('lost_percent')), 2)
        except Exception:
            pass

    # Some iperf3 results expose only per-stream receiver/sender totals.
    streams = end.get('streams')
    if metrics['tcp_mbps'] is None and isinstance(streams, list):
        received_vals = []
        sent_vals = []
        for stream in streams:
            receiver = stream.get('receiver') if isinstance(stream, dict) else None
            sender = stream.get('sender') if isinstance(stream, dict) else None
            if isinstance(receiver, dict) and receiver.get('bits_per_second') is not None:
                try:
                    received_vals.append(float(receiver.get('bits_per_second')))
                except Exception:
                    pass
            if isinstance(sender, dict) and sender.get('bits_per_second') is not None:
                try:
                    sent_vals.append(float(sender.get('bits_per_second')))
                except Exception:
                    pass
        if received_vals:
            metrics['tcp_mbps'] = round(sum(received_vals) / 1e6, 2)
        elif sent_vals:
            metrics['tcp_mbps'] = round(sum(sent_vals) / 1e6, 2)

    return metrics

def read_alfred_objects(type_id, kind):
    """Return authenticated payloads matching an Alfred type and kind."""
    try:
        # Bound the local Alfred read to five seconds before authenticating.
        r = subprocess.run(['alfred', '-r', str(type_id)],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return []
        return [message.payload for message in ADMIN.messages(type_id, r.stdout)
                if message.payload.get('kind') == kind]
    except Exception:
        return []

def send_alfred_object(type_id, obj):
    """Encrypt and publish an Alfred payload, returning success and detail."""
    try:
        # Seal before publication so plaintext control never enters Alfred.
        # The five-second subprocess limit bounds the local write only.
        payload = json.dumps(ADMIN.seal(type_id, obj), separators=(',', ':'))
        r = subprocess.run(['alfred', '-s', str(type_id)],
                           input=payload, capture_output=True, text=True, timeout=5)
        return r.returncode == 0, (r.stderr or r.stdout or '').strip()
    except Exception as e:
        return False, str(e)

def radio_expected_hosts():
    """List active peers expected to acknowledge a radio command."""
    try:
        # Try an eight-second refresh, then use the available registry on error.
        subprocess.run(['/usr/local/bin/mesh-registry-builder.sh'],
                       capture_output=True, text=True, timeout=8)
    except Exception:
        pass
    nodes = parse_registry(REGISTRY_FILE)
    hosts = []
    for nd in nodes.values():
        host = nd.get('HOSTNAME', '')
        if not host:
            continue
        if node_state(nd) != 'ACTIVE':
            continue
        hosts.append(host)
    my_host = get_my_hostname()
    if my_host and my_host not in hosts:
        hosts.append(my_host)
    return sorted(set(hosts))

def radio_target_for_node(node_ip):
    """Resolve a node identifier to its published radio target."""
    if node_ip == 'all':
        return 'all'
    for nd in parse_registry(REGISTRY_FILE).values():
        if nd.get('IPV4_ADDRESS', '') == node_ip:
            host = nd.get('HOSTNAME', '')
            if host:
                return [host]
    raise ValueError(f'Unknown node IP {node_ip}')

def make_radio_version():
    """Create a fresh radio transaction ID for ACK matching."""
    return new_version()

def radio_ack_snapshot(version):
    """Keep each host's latest authenticated ACK for this transaction."""
    latest = {}
    # Authenticated messages arrive in timestamp/ID order, newest last.
    for ack in read_alfred_objects(ALFRED_RADIO_ACK_TYPE, 'radio_ack'):
        if ack.get('version') != version:
            continue
        host = ack.get('hostname', '')
        if host:
            latest[host] = ack
    return latest

def wait_radio_acks(version, expected_hosts, timeout=75):
    """Poll expected ACKs until completion, rejection or timeout."""
    # Poll every two seconds within the default 75 s monotonic wait window.
    # Each Alfred read can run up to its own five-second timeout.
    deadline = time.monotonic() + timeout
    expected = set(expected_hosts)
    last = {}
    while time.monotonic() < deadline:
        last = radio_ack_snapshot(version)
        ok_hosts = {h for h, a in last.items() if a.get('ok') is True}
        bad = {h: a for h, a in last.items() if a.get('ok') is False}
        missing = sorted(expected - ok_hosts - set(bad.keys()))
        if bad or not missing:
            return {'ok': not bad and not missing, 'acks': last,
                    'missing': missing, 'bad': bad}
        time.sleep(2)
    ok_hosts = {h for h, a in last.items() if a.get('ok') is True}
    bad = {h: a for h, a in last.items() if a.get('ok') is False}
    missing = sorted(expected - ok_hosts - set(bad.keys()))
    return {'ok': False, 'acks': last, 'missing': missing, 'bad': bad}

def cancel_radio_version(version):
    """Publish a cancellation for the named radio transaction."""
    cancel = {
        'kind': 'radio_cancel',
        'version': version,
        'issued_by': get_my_hostname(),
        'issued_at': int(time.time()),
    }
    send_alfred_object(ALFRED_RADIO_TYPE, cancel)

def coordinate_radio_toggle(node_ip, iface, state):
    """Validate an interface toggle and stage it for the selected nodes."""
    if iface not in ('wlan0', 'wlan1', 'wlan2') or state not in ('up', 'down'):
        return {'ok': False, 'error': 'Invalid iface or state'}
    return coordinate_radio_change({'desired': {iface: state}}, node_ip)


def coordinate_radio_change(actions, node_ip='all'):
    """Stage a radio change over Alfred and activate after all expected ACKs.

    Single-node changes use the same transport with a restricted `targets` list.
    """
    if 'uplink_wifi' in actions:
        return {'ok': False, 'error': 'Wi-Fi uplink credentials are node-local'}
    targets = 'all'
    if node_ip != 'all':
        try:
            targets = radio_target_for_node(node_ip)
        except ValueError as e:
            return {'ok': False, 'error': str(e)}

    expected = radio_expected_hosts() if targets == 'all' else list(targets)
    if not expected:
        return {'ok': False, 'error': 'No reachable nodes in registry'}
    if targets == 'all' and len(expected) < 2:
        return {
            'ok': False,
            'error': 'Refusing global radio change: registry sees fewer than 2 reachable nodes. Wait for Alfred registry refresh and retry.',
            'expected': expected,
        }

    # Zero activation time asks receivers to validate and ACK without applying.
    pkg = {
        'kind': 'radio_state',
        'issued_by': get_my_hostname(),
        'issued_at': int(time.time()),
        'activate_at': 0,
        'targets': targets,
    }
    pkg.update(actions)
    pkg['version'] = make_radio_version()

    ok, error = send_alfred_object(ALFRED_RADIO_TYPE, pkg)
    if not ok:
        return {'ok': False, 'error': f'Alfred stage failed: {error}'}

    ack_state = wait_radio_acks(pkg['version'], expected, timeout=75)
    if not ack_state['ok']:
        cancel_radio_version(pkg['version'])
        bad = [
            f"{host}: {ack.get('error') or 'rejected'}"
            for host, ack in sorted(ack_state['bad'].items())
        ]
        parts = []
        if ack_state['missing']:
            parts.append('missing ACK: ' + ', '.join(ack_state['missing']))
        if bad:
            parts.append('rejected: ' + '; '.join(bad))
        return {
            'ok': False,
            'error': '; '.join(parts) or 'ACK timeout',
            'version': pkg['version'],
            'missing': ack_state['missing'],
            'bad': bad,
        }

    # Give staged peers 20 wall-clock seconds to receive the activation message.
    activate_at = int(time.time()) + 20
    pkg['activate_at'] = activate_at
    pkg['issued_at'] = int(time.time())
    ok, error = send_alfred_object(ALFRED_RADIO_TYPE, pkg)
    if not ok:
        cancel_radio_version(pkg['version'])
        return {'ok': False, 'error': f'Alfred activate failed: {error}'}

    return {
        'ok': True,
        'version': pkg['version'],
        'activate_at': activate_at,
        'acked': sorted(ack_state['acks'].keys()),
        'expected': expected,
        'targets': targets,
    }

def get_iw_info(iface):
    """Get current channel/freq/txpower for an interface."""
    info = {}
    try:
        # Bound iw to five seconds and parse its channel, MHz and dBm fields.
        r = subprocess.run(['iw', 'dev', iface, 'info'],
                           capture_output=True, text=True, timeout=5)
        m = re.search(r'channel (\d+).*?(\d+) MHz', r.stdout)
        if m:
            info['channel'] = m.group(1)
            info['freq_mhz'] = m.group(2)
        m = re.search(r'txpower ([\d.]+) dBm', r.stdout)
        if m:
            info['txpower_dbm'] = m.group(1)
    except Exception:
        pass
    cap = get_iface_txpower_cap(iface)
    if cap:
        info['txpower_cap_dbm'] = cap
        info['txpower_options_dbm'] = txpower_choices_from_cap(cap)
    else:
        info['txpower_options_dbm'] = []

    # HaLow uses Morse runtime data because iw may report a Wi-Fi channel.
    if iface == 'wlan2':
        info.update(get_halow_driver_info(iface))

    return info


# Topology

def build_topology():
    """Build the management topology from local and peer observations."""
    nodes_raw = parse_registry(REGISTRY_FILE)
    my_host   = get_my_hostname()
    my_ip     = get_my_ip()
    conf      = load_kv_file(MESH_CONF_FILE)

    # Get local interface state
    active_ifaces = get_bat0_active_ifaces()
    iw_wlan0 = get_iw_info('wlan0')
    iw_wlan1 = get_iw_info('wlan1')
    iw_wlan2 = get_iw_info('wlan2')

    nodes = []
    for nid, nd in nodes_raw.items():
        hostname = nd.get('HOSTNAME', 'unknown')
        ip       = nd.get('IPV4_ADDRESS', '')
        is_me    = (hostname == my_host)
        mcs_map = {
            'wlan0': {'tx_mcs': nd.get('WIFI_24_TX_MCS', ''), 'rx_mcs': nd.get('WIFI_24_RX_MCS', '')},
            'wlan1': {'tx_mcs': nd.get('WIFI_5_TX_MCS', ''), 'rx_mcs': nd.get('WIFI_5_RX_MCS', '')},
            'wlan2': {'tx_mcs': nd.get('HALOW_TX_MCS', ''), 'rx_mcs': nd.get('HALOW_RX_MCS', '')},
        }

        node_info = {
            'id':       nid,
            'hostname': hostname,
            'operator': (conf.get('operator_name', '') if is_me
                         else nd.get('ATAK_USER', '')),
            'ip':       ip,
            'is_me':    is_me,
            'is_gateway': nd.get('IS_GATEWAY', 'false').lower() == 'true',
            'gateway_iface': nd.get('GATEWAY_IFACE', ''),
            'battery':  nd.get('BATTERY_PERCENTAGE', ''),
            'uptime':   fmt_uptime(nd.get('UPTIME_SECONDS', ''), compact=True),
        }

        if is_me:
            live_uptime = get_local_uptime(compact=True)
            if live_uptime:
                node_info['uptime'] = live_uptime
            node_info['interfaces'] = {
                'wlan0': {'active': 'wlan0' in active_ifaces, **iw_wlan0, **mcs_map['wlan0']},
                'wlan1': {'active': 'wlan1' in active_ifaces, **iw_wlan1, **mcs_map['wlan1']},
                'wlan2': {'active': 'wlan2' in active_ifaces, **iw_wlan2, **mcs_map['wlan2']},
            }
            from manet_wifi_uplink import uplink_owner
            owner = uplink_owner()
            if owner in node_info['interfaces']:
                node_info['interfaces'][owner]['uplink'] = True

        else:
            # Use peer INTERFACES_JSON when published, else MCS + DATA_CHANNEL_*
            # to retain channel state in the display.
            node_info['interfaces'] = peer_radio_interfaces(nd)

        nodes.append(node_info)

    # Sort: self first
    nodes.sort(key=lambda n: (not n['is_me'], n['hostname']))

    return {
        'nodes':      nodes,
        'my_hostname': my_host,
        'my_ip':      my_ip,
        'internet':   has_internet(),
        'halow_plan': halow_channel_options(),
        'acs_enabled': str(conf.get('acs', 'n')).lower() in ('y', 'yes', '1', 'true'),
        'recovery': recovery_status(conf, nodes_raw, my_host),
        'timestamp':  int(time.time()),
    }


# Measurements

def ensure_sessions_dir():
    """Create the measurement storage directory if missing."""
    os.makedirs(SESSIONS_DIR, exist_ok=True)


def session_path(label):
    """Only direct, real children of the measurements directory are sessions."""
    # The label becomes a directory and download filename. Reject separators
    # and the two special dot names before resolving it under the storage root.
    if (not isinstance(label, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', label)
            or label in ('.', '..')):
        raise ValueError('Invalid session label: use 1-64 letters, digits, dots, underscores or hyphens')
    root = Path(SESSIONS_DIR).resolve()
    path = root / label
    if path.is_symlink() or path.resolve().parent != root:
        raise ValueError('Invalid session path')
    return path


def list_sessions():
    """List valid session directories with result counts and summaries."""
    ensure_sessions_dir()
    sessions = []
    # Descending labels put newer timestamp-based names before older ones.
    for name in sorted(os.listdir(SESSIONS_DIR), reverse=True):
        try:
            d = session_path(name)
        except ValueError:
            continue
        if os.path.isdir(d):
            files = [f for f in os.listdir(d) if f.endswith('.json')]
            results = get_session_results(name)
            sessions.append({
                'label': name,
                'tests': len(files),
                'summary': summarize_session_results(results),
            })
    return sessions

def get_session_results(label):
    """Read a session's JSON files, skipping symlinks and unreadable results."""
    d = session_path(label)
    results = []
    if not os.path.isdir(d):
        return results
    # Result filenames begin with timestamps, so lexical order groups captures.
    for fname in sorted(os.listdir(d)):
        if fname.endswith('.json') and not (d / fname).is_symlink():
            try:
                with open(os.path.join(d, fname)) as f:
                    results.append(json.load(f))
            except Exception:
                pass
    return results

def delete_session(label):
    """Remove a validated session directory, returning success and detail."""
    try:
        d = session_path(label)
    except ValueError as error:
        return False, str(error)
    if not os.path.isdir(d):
        return False, 'Session not found'
    shutil.rmtree(d)
    return True, ''

def session_to_csv(label):
    """Export session measurements with throughput, latency and loss columns."""
    results = get_session_results(label)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'timestamp', 'session_label', 'test_type',
        'src_node', 'dst_node', 'executing_node', 'executing_ip', 'destination_ip',
        'active_interfaces', 'halow_channel', 'halow_bw',
        'hop_count', 'hop_count_source',
        'tcp_mbps', 'udp_mbps', 'jitter_ms', 'loss_pct',
        'rtt_avg_ms', 'rtt_min_ms', 'rtt_max_ms',
    ])
    for r in results:
        iperf = r.get('iperf3_result', {})
        ping  = r.get('ping_result', {})
        metrics = extract_iperf3_metrics(iperf)
        ping_loss = ping.get('loss_pct', '') if ping else ''
        loss_value = metrics['loss_pct'] if metrics['loss_pct'] is not None else ping_loss
        writer.writerow([
            r.get('timestamp', ''),
            r.get('session_label', ''),
            r.get('test_type', ''),
            r.get('source_node', ''),
            r.get('destination_node', ''),
            r.get('executing_node', ''),
            r.get('executing_ip', ''),
            r.get('destination_ip', ''),
            ','.join(r.get('active_interfaces', [])),
            r.get('halow_channel', ''),
            r.get('halow_bw', ''),
            r.get('hop_count', ''),
            r.get('hop_count_source', ''),
            '' if metrics['tcp_mbps'] is None else metrics['tcp_mbps'],
            '' if metrics['udp_mbps'] is None else metrics['udp_mbps'],
            '' if metrics['jitter_ms'] is None else metrics['jitter_ms'],
            loss_value,
            ping.get('rtt_avg', '') if ping else '',
            ping.get('rtt_min', '') if ping else '',
            ping.get('rtt_max', '') if ping else '',
        ])
    return output.getvalue()

def _stats(values):
    """Summarize nonmissing values with mean, extrema and sample count."""
    nums = [float(v) for v in values if v is not None]
    if not nums:
        return None
    return {
        'avg': round(sum(nums) / len(nums), 2),
        'min': round(min(nums), 2),
        'max': round(max(nums), 2),
        'n': len(nums),
    }

def summarize_session_results(results):
    """Count outcomes and aggregate available measurement metrics."""
    metrics = {
        'tcp_mbps': [], 'udp_mbps': [], 'jitter_ms': [],
        'loss_pct': [], 'rtt_avg': [],
    }
    ok = fail = 0
    for record in results:
        if record.get('ok'):
            ok += 1
        else:
            fail += 1
        summary = summarize_measurement_result(record)
        for key in metrics:
            if summary.get(key) is not None:
                metrics[key].append(summary.get(key))
    return {
        'ok': ok,
        'fail': fail,
        'tcp_mbps': _stats(metrics['tcp_mbps']),
        'udp_mbps': _stats(metrics['udp_mbps']),
        'jitter_ms': _stats(metrics['jitter_ms']),
        'loss_pct': _stats(metrics['loss_pct']),
        'rtt_avg': _stats(metrics['rtt_avg']),
    }

def snapshot_topology():
    """Lightweight topology snapshot for embedding in test results."""
    active = get_bat0_active_ifaces()
    iw2    = get_iw_info('wlan2')
    iw0    = get_iw_info('wlan0')
    return {
        'active_interfaces': active,
        'halow_channel':  iw2.get('channel', ''),
        'halow_bw':       iw2.get('halow_bw', ''),
        'ch_2g':          iw0.get('channel', ''),
    }

def summarize_measurement_result(record):
    """Build the compact result shown in progress and session summaries."""
    summary = {
        'test_type': record.get('test_type', ''),
        'src': record.get('source_node', ''),
        'dst': record.get('destination_node', ''),
        'ok': record.get('ok', False),
    }
    if record.get('error'):
        summary['error'] = record.get('error')

    iperf = record.get('iperf3_result') or {}
    ping = record.get('ping_result') or {}
    metrics = extract_iperf3_metrics(iperf)
    for key, value in metrics.items():
        if value is not None:
            summary[key] = value

    for key in ('rtt_avg', 'rtt_min', 'rtt_max', 'loss_pct'):
        if ping.get(key) is not None:
            summary[key] = ping.get(key)
    if record.get('hop_count') is not None:
        summary['hop_count'] = record.get('hop_count')
    return summary

def run_local_ping(target, count=100, interval=0.2):
    """Ping a peer from this node. Returns the parsed summary, or None."""
    if not target:
        return None
    try:
        # Allow the nominal send schedule plus ten seconds for completion.
        r = subprocess.run(['ping', '-c', str(count), '-i', str(interval), target],
                           capture_output=True, text=True,
                           timeout=count * interval + 10)
    except Exception:
        return None
    # Match four iputils RTT values in ms and a packet-loss percentage and received-packet count.
    rtt = re.search(r'rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)', r.stdout)
    loss = re.search(r'([\d.]+)% packet loss', r.stdout)
    received = re.search(r'(\d+) (?:packets )?received', r.stdout)
    return {
        'output':   r.stdout,
        'rtt_min':  float(rtt.group(1)) if rtt else None,
        'rtt_avg':  float(rtt.group(2)) if rtt else None,
        'rtt_max':  float(rtt.group(3)) if rtt else None,
        'rtt_mdev': float(rtt.group(4)) if rtt else None,
        'loss_pct': float(loss.group(1)) if loss else None,
        'received': int(received.group(1)) if received else 0,
    }


def run_local_iperf3(server_ip, test_type, duration, bitrate,
                     parallel=1, reverse=False):
    """Run iperf3 from this node against a peer's always-on daemon.

    Returns (ok, parsed_json, error).
    """
    cmd = ['iperf3', '-c', server_ip, '-t', str(duration), '-J']
    if test_type in ('udp_throughput', 'udp_jitter', 'packet_loss'):
        cmd += ['-u', '-b', bitrate]
    if parallel > 1:
        cmd += ['-P', str(parallel)]
    if reverse:
        cmd += ['-R']
    # Allow 25 seconds beyond the traffic duration for client overhead.
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=duration + 25)
    try:
        payload = json.loads(r.stdout)
    except Exception:
        payload = {'raw': r.stdout, 'stderr': r.stderr}
    if payload.get('error'):
        return False, payload, payload['error']
    return r.returncode == 0, payload, r.stderr.strip()


def measurement_test_budget(test_type, duration):
    """Reserve command, route, local work and pause time per pair/test."""
    command = 30 if test_type == 'icmp_ping' else duration + 25
    return command + MEASURE_ROUTE_SEC + MEASURE_LOCAL_SEC + MEASURE_PAUSE_SEC


def run_measurement_session(label, pairs, tests, duration, udp_bitrate):
    """Run all test combinations. Blocking: call in thread."""
    global _measure_status
    done = 0
    deadline = time.monotonic() + MEASURE_BUDGET_SEC

    def require_time(seconds=0):
        """Stop between blocking operations if the remaining work cannot fit."""
        if deadline - time.monotonic() < seconds:
            raise TimeoutError('Measurement batch reached its one-hour budget')

    try:
        ensure_sessions_dir()
        session_dir = session_path(label)
        check_capacity(SESSIONS_DIR, session_dir)
        os.makedirs(session_dir, exist_ok=True)
        # A single topology snapshot annotates every result in the batch.
        topo = snapshot_topology()

        executor_name, executor_ip = get_my_hostname(), get_my_ip()
        total = len(pairs) * len(tests)

        for pair in pairs:
            dst_ip   = pair['dst_ip']
            src_name = executor_name
            dst_name = pair['dst_name']

            for test_type in tests:
                test_budget = measurement_test_budget(test_type, duration)
                require_time(test_budget)
                check_capacity(SESSIONS_DIR, session_path(label))
                now = int(time.time())
                with _measure_lock:
                    _measure_mono['current'] = time.monotonic()
                    _measure_status.update({
                        'progress': f'{src_name}→{dst_name} {test_type} ({done+1}/{total})',
                        'done': done,
                        'total': total,
                        'current_started_at': now,
                        'current': {
                            'src': src_name,
                            'dst': dst_name,
                            'test_type': test_type,
                            'index': done + 1,
                            'total': total,
                        },
                    })

                ts    = datetime.now().strftime('%Y%m%dT%H%M%S')
                # Random suffixes avoid same-second and clock-step collisions.
                fname = f'{ts}_{secrets.token_hex(12)}.json'
                result_record = {
                    'session_label':    label,
                    'timestamp':        datetime.now().isoformat(),
                    'test_type':        test_type,
                    'source_node':      src_name,
                    'executing_node':   executor_name,
                    'executing_ip':     executor_ip,
                    'destination_ip':   dst_ip,
                    'destination_node': dst_name,
                    'active_interfaces': topo['active_interfaces'],
                    'halow_channel':    topo['halow_channel'],
                    'halow_bw':         topo['halow_bw'],
                    'ch_2g':            topo['ch_2g'],
                    'gps_source':       None,
                    'gps_destination':  None,
                    'hop_count':        None,
                    'hop_count_source': '',
                }
                hop_count, hop_source = get_session_hop_count(dst_ip)
                result_record['hop_count'] = hop_count
                result_record['hop_count_source'] = hop_source
                # Recheck after local work; keep the full command timeout and
                # room to save its result within the remaining batch budget.
                require_time(test_budget - MEASURE_ROUTE_SEC)

                if test_type == 'icmp_ping':
                    result_record['ping_result'] = run_local_ping(dst_ip)
                    result_record['ok'] = bool(result_record['ping_result'] and
                                               result_record['ping_result'].get('received', 0) > 0)
                    if not result_record['ok']:
                        result_record['error'] = 'ping received no replies'
                else:
                    # Connect the local client to the peer's persistent iperf3
                    # daemon.
                    try:
                        ok, payload, err = run_local_iperf3(
                            dst_ip, test_type, duration, udp_bitrate,
                            parallel=4 if test_type == 'tcp_4stream' else 1,
                            reverse=test_type == 'reverse')
                        result_record['iperf3_result'] = payload
                        result_record['ok'] = ok
                        if not ok:
                            result_record['error'] = err or 'iperf client failed'
                    except Exception as e:
                        result_record['ok'] = False
                        result_record['error'] = str(e)

                # Save result
                save_result(SESSIONS_DIR, session_path(label) / fname,
                            result_record)

                done += 1
                with _measure_lock:
                    _measure_status.update({
                        'done': done,
                        'total': total,
                        'last_result': summarize_measurement_result(result_record),
                    })
                require_time(MEASURE_PAUSE_SEC)
                time.sleep(MEASURE_PAUSE_SEC)  # brief pause between tests
                require_time()

        with _measure_lock:
            _measure_status['running']  = False
            _measure_status['progress'] = f'Done: {done} tests saved'
            _measure_status['error']    = ''
            _measure_status['done']     = done
            _measure_status['total']    = total
            _measure_status['current']  = None
            _measure_status['current_started_at'] = None
    except Exception as e:
        with _measure_lock:
            _measure_status['running']  = False
            _measure_status['progress'] = f'Failed after {done} test(s)'
            _measure_status['error']    = str(e)
            _measure_status['done']     = done
            _measure_status['current']  = None
            _measure_status['current_started_at'] = None



# HTML

# Shared page styling is combined with the voice and config tab styles below.
CSS = """
:root {
  color-scheme: light;
  --bg:      #ebeae8;
  --surface: #ffffff;
  --card:    #ffffff;
  --panel:   #f7f6f3;
  --border:  #d6d2cb;
  --border2: #e7e2da;
  --accent:  #00003f;
  --accent2: #ecb000;
  --info:    #00003f;
  --warn:    #ecb000;
  --fer-yellow:#ecb000;
  --fer-black:#02000d;
  --good:    #16813d;
  --bad:     #c22c25;
  --small-text: 12px;
  --green:   #16a34a;
  --orange:  #8a6a00;
  --red:     #dc2626;
  --text:    #02000d;
  --muted:   #615f68;
  --shadow:  0 18px 50px rgba(2,0,13,.10);
  --font:    Roobert, Arial, sans-serif;
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg:      #02000d;
  --surface: #121118;
  --card:    #17151d;
  --panel:   #0b0a12;
  --border:  #34313b;
  --border2: #24212b;
  --accent:  #00003f;
  --accent2: #ecb000;
  --info:    #9fa8ff;
  --warn:    #ecb000;
  --good:    #70d99a;
  --bad:     #ffaaa4;
  --text:    #f8f6ef;
  --muted:   #aaa5b2;
  --shadow:  0 18px 50px rgba(0,0,0,.36);
}
*{box-sizing:border-box;margin:0;padding:0}
:root{color-scheme:light}
:root[data-theme="dark"]{color-scheme:dark}
:focus-visible{outline:3px solid var(--info);outline-offset:3px}
::selection{background:var(--accent2);color:var(--fer-black)}
@media (pointer:coarse){
  button,summary,a.btn{min-height:44px;min-width:44px}
  input[type=text],input[type=password],input[type=number],select{min-height:44px}
}
html,body{background:var(--bg);color:var(--text);font-family:var(--font);font-size:13px;min-height:100vh;max-width:100%;overflow-x:hidden}

body{font-feature-settings:"cv02","cv03","cv04","cv11"}
body::before{display:none}
body{
  background:
    radial-gradient(circle at top left, rgba(236,176,0,.16), transparent 34%),
    linear-gradient(160deg, transparent 0 56%, rgba(0,0,63,.05) 56% 70%, transparent 70%),
    var(--bg);
}

/* header */
#hdr{background:rgba(255,255,255,.94);backdrop-filter:blur(18px);border-bottom:1px solid var(--border2);padding:0 22px;min-height:58px;display:flex;align-items:center;gap:10px;position:sticky;top:0;z-index:100;box-shadow:0 1px 0 rgba(2,0,13,.05);transition:min-height .18s ease,padding .18s ease,gap .18s ease}
#hdr::after{content:'';position:absolute;left:0;right:0;bottom:0;height:2px;background:linear-gradient(90deg,rgba(236,176,0,.92) 0 36%,rgba(236,176,0,.28) 36% 68%,transparent 68%);pointer-events:none}
:root[data-theme="dark"] #hdr{background:rgba(18,17,24,.92)}
.fer-lockup{display:flex;align-items:center;justify-content:flex-start;height:58px;padding-right:12px;border-right:1px solid var(--border);color:var(--fer-black);overflow:hidden;flex:0 0 auto}
/* the badge is near-square, so height drives the size and width follows the
   aspect ratio: a fixed width would letterbox it and leave dead space */
.fer-logo-img{display:block;width:auto;height:44px;max-width:100%;object-fit:contain;object-position:left center;filter:none;transition:height .18s ease}
:root[data-theme="dark"] .fer-lockup{color:#ffffff}
/* no brightness(0) invert(1) here: that flattens the badge to a solid silhouette */
#hdr-logo{color:var(--text);font-size:17px;letter-spacing:0;font-weight:900;display:flex;align-items:center;min-height:46px;line-height:1}
#hdr-logo span{color:var(--accent2)}
#hdr-node{font-size:12px;color:var(--muted);border-left:1px solid var(--border);padding-left:16px;transition:opacity .18s ease,max-width .18s ease,padding .18s ease,border .18s ease}
#hdr-node strong{color:var(--text)}
#hdr-right{margin-left:auto;display:flex;align-items:center;gap:16px;font-size:var(--small-text)}
#hdr-inet{padding:5px 10px;border-radius:999px;letter-spacing:0;font-size:var(--small-text);font-weight:700}
#hdr-inet.ok{color:var(--text);border:1px solid rgba(22,163,74,.34);background:rgba(22,163,74,.07)}
#hdr-inet.no{color:var(--text);border:1px solid rgba(217,119,6,.34);background:rgba(217,119,6,.07)}
#hdr-clock{color:var(--muted);font-size:var(--small-text)}
.theme-toggle{border:1px solid var(--accent2);background:rgba(236,176,0,.10);color:var(--text);border-radius:999px;padding:6px 10px;font-family:var(--font);font-size:var(--small-text);font-weight:850;cursor:pointer;min-width:74px}
.theme-toggle:hover{background:var(--accent2);color:var(--fer-black);box-shadow:0 8px 22px rgba(236,176,0,.20)}
.overview-link-btn{border:1px solid var(--accent2);background:var(--accent2);color:var(--fer-black);border-radius:999px;padding:6px 12px;font-family:var(--font);font-size:var(--small-text);font-weight:850;cursor:pointer;min-width:92px;transition:background .18s ease,color .18s ease,box-shadow .18s ease,transform .18s ease;box-shadow:0 8px 20px rgba(236,176,0,.16)}
.overview-link-btn:hover{background:#f6c62f;color:var(--fer-black);box-shadow:0 10px 24px rgba(236,176,0,.26);transform:translateY(-1px)}
:root[data-theme="dark"] .overview-link-btn{background:var(--accent2);color:var(--fer-black);border-color:var(--accent2)}
:root[data-theme="dark"] .overview-link-btn:hover{background:#f6c62f;color:var(--fer-black);box-shadow:0 10px 24px rgba(236,176,0,.28)}

/* sidebar nav */
#page{display:flex;min-height:calc(100vh - 58px)}
#nav{background:var(--surface);border-right:1px solid var(--border2);width:160px;flex:0 0 160px;display:flex;flex-direction:column;padding:12px 0;position:sticky;top:58px;height:calc(100vh - 58px);overflow-y:auto;z-index:90;transition:top .18s ease}
.tab{padding:11px 20px;cursor:pointer;font-family:var(--font);font-size:12px;font-weight:700;letter-spacing:0;color:var(--muted);background:transparent;border:0;border-bottom:2px solid transparent;text-align:left;text-transform:none;transition:color .15s ease,border-color .15s ease,background .15s ease;white-space:nowrap;min-height:44px}
.tab:focus-visible{outline-offset:-3px}
.tab:hover{color:var(--text);background:color-mix(in srgb, var(--panel) 92%, transparent)}
.tab.active{color:var(--text);border-bottom-color:var(--fer-yellow);background:transparent}
.tab.active::after{display:none}

/* layout */
#content{padding:22px;max-width:1120px;width:100%;flex:1;min-width:0;position:relative}
#content::before{content:'';display:block;height:36px;margin:-22px -22px 18px;background:
  linear-gradient(90deg, rgba(236,176,0,.24) 0 12%, transparent 12% 100%);
  border-bottom:1px solid var(--border2);transition:height .18s ease,margin .18s ease,opacity .18s ease}
:root[data-theme="dark"] #content::before{background:
  linear-gradient(90deg, rgba(236,176,0,.28) 0 12%, transparent 12% 100%)}
body.chrome-compact #hdr{min-height:50px;gap:14px}
  body.chrome-compact .fer-lockup{height:44px}
  body.chrome-compact .fer-logo-img{height:34px}
body.chrome-compact #hdr-node{opacity:0;max-width:0;padding-left:0;border-left:0;overflow:hidden;white-space:nowrap}
body.chrome-compact #nav{top:50px;height:calc(100vh - 50px)}
body.chrome-compact #content::before{height:8px;margin:-22px -22px 12px;opacity:.72}

/* card */
.card{background:var(--card);border:1px solid var(--border2);border-radius:8px;margin-bottom:16px;position:relative;overflow:hidden;box-shadow:var(--shadow)}
.card::before{display:none}
.card-title{padding:14px 16px;font-size:12px;font-weight:800;letter-spacing:0;color:var(--text);text-transform:none;border-bottom:1px solid var(--border2);display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-width:0}
.card-title::before{display:none}

/* Wi-Fi picker: five to six ordinary rows, with room for longer names. */
#tab-uplink [hidden]{display:none}
#tab-uplink .uplink-body{padding:16px;font-size:16px;line-height:1.45;min-width:0}
#tab-uplink .card-title{font-size:16px}
#tab-uplink p{margin:0 0 12px;overflow-wrap:anywhere}
#tab-uplink h3{font-size:16px;margin:14px 0 8px}
#tab-uplink .btn,#tab-uplink select,#tab-uplink input,#tab-uplink summary{min-height:44px;font-size:16px}
#tab-uplink .btn{color:var(--text);border-color:var(--border)}
#tab-uplink .btn-green{color:var(--fer-black);border-color:var(--accent2)}
#tab-uplink .btn:hover{background:var(--panel);color:var(--text);border-color:var(--accent2)}
#tab-uplink .btn-green:hover{background:var(--accent2);color:var(--fer-black)}
.uplink-help{color:var(--muted);margin-bottom:12px}
.uplink-help summary{cursor:pointer;padding:10px 0;color:var(--text)}
.uplink-toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.uplink-toolbar label{flex-basis:100%}
#uplink-wifi-radio{flex:1;min-width:0;width:160px}
#uplink-wifi-networks{max-height:420px;overflow-y:auto;overscroll-behavior:contain;border:1px solid var(--border);border-radius:8px;background:var(--surface)}
#uplink-wifi-current{display:block;padding:12px;background:var(--panel);border-bottom:2px solid var(--accent2)}
#uplink-wifi-current .uplink-detail{display:block}
.uplink-network{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:3px 8px;width:100%;min-height:72px;padding:12px;text-align:left;border:0;border-bottom:1px solid var(--border2);background:var(--surface);color:var(--text);font:inherit;cursor:pointer}
.uplink-network:last-child{border-bottom:0}
.uplink-network[aria-pressed="true"]{background:var(--panel);box-shadow:inset 4px 0 var(--accent2)}
.uplink-network:disabled{color:var(--muted);background:var(--panel);cursor:not-allowed}
.uplink-network:focus-visible,#uplink-wifi-networks:focus-visible{outline:3px solid var(--accent2);outline-offset:-3px}
.uplink-name{min-width:0;overflow-wrap:anywhere;white-space:pre-wrap;unicode-bidi:plaintext;font-weight:700}
.uplink-detail{grid-column:1 / -1;font-size:14px;overflow-wrap:anywhere}
.uplink-signal{display:flex;align-items:center;gap:6px;font-size:14px;white-space:nowrap}
.uplink-bars{display:inline-flex;align-items:flex-end;gap:2px;height:20px}
.uplink-bars i{width:4px;height:5px;background:var(--border)}
.uplink-bars i:nth-child(2){height:10px}.uplink-bars i:nth-child(3){height:15px}.uplink-bars i:nth-child(4){height:20px}
.uplink-bars .lit{background:currentColor}
#uplink-wifi-feedback,#uplink-wifi-status{font-size:14px;margin-top:12px}
#uplink-wifi-connect{margin:12px 0;padding:12px;border:1px solid var(--border);border-radius:8px}
#uplink-wifi-password-row label{display:block;margin-bottom:6px}
#uplink-wifi-password{width:100%;min-width:0;margin-bottom:12px}
.uplink-saved-mark{display:inline-flex;align-items:center;gap:4px;white-space:nowrap;font-size:14px}
.uplink-saved-mark svg{width:16px;height:16px;flex:none}
#uplink-wifi-saved-password .btn{margin-bottom:12px}
#uplink-wifi-saved-list{list-style:none;margin:0;padding:0;max-height:280px;overflow-y:auto}
.uplink-saved-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;align-items:center;padding:8px 0;border-bottom:1px solid var(--border2)}
.uplink-saved-row .uplink-detail{display:block}
@media(max-width:480px){
  .uplink-dbm{display:none}
  #tab-uplink .uplink-body{padding:12px}
  #tab-uplink .uplink-toolbar{display:grid;grid-template-columns:1fr auto}
  #tab-uplink .uplink-toolbar label{
    position:absolute;width:1px;height:1px;overflow:hidden;
    clip-path:inset(50%);white-space:nowrap
  }
  #tab-uplink .uplink-toolbar .btn{width:auto}
  #tab-uplink .uplink-toolbar select{width:100%}
}

/* rows */
.row{display:flex;align-items:center;gap:12px;padding:10px 16px;border-bottom:1px solid var(--border2);flex-wrap:wrap}
.row:last-child{border-bottom:none}
.row-label{flex:0 0 150px;font-size:var(--small-text);color:var(--muted);letter-spacing:.5px;text-transform:uppercase}

/* inputs */
input[type=text],input[type=password],input[type=number],select{
  background:var(--surface);border:1px solid var(--border);color:var(--text);
  padding:6px 10px;font-family:var(--font);font-size:12px;
  outline:none;transition:border .15s,box-shadow .15s;min-width:150px;max-width:100%;border-radius:8px;
}
input:focus,select:focus{border-color:var(--accent2);box-shadow:0 0 0 3px rgba(236,176,0,.18)}
select option{background:var(--surface)}
input[type=checkbox]{width:15px;height:15px;accent-color:var(--accent)}

/* buttons */
.btn{padding:8px 15px;background:var(--surface);color:var(--accent);border:1px solid var(--accent);border-radius:8px;font-family:var(--font);font-size:12px;font-weight:850;cursor:pointer;letter-spacing:0;text-transform:none;transition:all .15s;position:relative;overflow:hidden}
.btn::before{display:none}
.btn:hover{background:var(--accent);color:#ffffff;border-color:var(--accent);box-shadow:0 8px 22px rgba(2,0,13,.14);transform:translateY(-1px)}
.btn:disabled{opacity:.45;cursor:not-allowed;box-shadow:none;transform:none}
.btn-green{background:var(--accent2);color:var(--fer-black);border-color:var(--accent2)}
.btn-green:hover{background:var(--fer-black);color:#ffffff;border-color:var(--fer-black);box-shadow:0 10px 26px rgba(236,176,0,.18)}
.btn-red{color:#b42318;border-color:#e9b2ad;background:transparent}
.btn-red:hover{background:#b42318;color:#ffffff;border-color:#b42318;box-shadow:0 8px 22px rgba(180,35,24,.14)}
.btn-run{padding:13px 30px;font-size:13px;color:#ffffff;border-color:var(--accent);background:var(--accent);letter-spacing:0}
.btn-run:hover{background:var(--accent2);color:var(--fer-black);border-color:var(--accent2);box-shadow:0 12px 28px rgba(236,176,0,.24)}
:root[data-theme="dark"] .btn{background:transparent;color:var(--accent2);border-color:var(--accent2)}
:root[data-theme="dark"] .btn:hover{background:var(--accent2);color:var(--fer-black);border-color:var(--accent2)}
:root[data-theme="dark"] .btn-green,:root[data-theme="dark"] .btn-run{background:var(--accent2);color:var(--fer-black);border-color:var(--accent2)}
:root[data-theme="dark"] .btn-green:hover,:root[data-theme="dark"] .btn-run:hover{background:#f8f6ef;color:var(--fer-black);border-color:#f8f6ef}
:root[data-theme="dark"] .btn-red{background:transparent;color:#fca5a5;border-color:#7f1d1d}
:root[data-theme="dark"] .btn-red:hover{background:#b42318;color:#ffffff;border-color:#b42318}

/* badges */
.badge{padding:3px 8px;font-size:var(--small-text);font-weight:750;letter-spacing:0;border:1px solid;border-radius:999px}
.b-on {color:var(--text); border-color:rgba(22,163,74,.34);background:rgba(22,163,74,.07)}
.b-off{color:var(--muted); border-color:var(--border);background:var(--panel)}
.b-gw {color:var(--text);border-color:rgba(236,176,0,.48);background:rgba(236,176,0,.14)}
.b-me {color:var(--info);border-color:rgba(0,0,63,.22);background:rgba(0,0,63,.08)}

/* table */
table{width:100%;border-collapse:collapse}
thead tr{border-bottom:1px solid var(--border)}
th{padding:8px 12px;font-size:var(--small-text);letter-spacing:2px;color:var(--muted);text-transform:uppercase;text-align:left}
td{padding:10px 12px;border-bottom:1px solid var(--border2);font-size:12px;vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(236,176,0,.08)}

/* node grid */
.node-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(220px,100%),1fr));gap:12px;padding:16px}
.node-card{background:var(--card);border:1px solid var(--border2);border-radius:8px;padding:14px;position:relative;transition:border .15s,box-shadow .15s}
.node-card:hover{border-color:var(--border);box-shadow:0 10px 26px rgba(18,24,38,.07)}
.node-card.is-me{border-color:rgba(0,0,63,.28);box-shadow:0 10px 26px rgba(0,0,63,.08)}
.node-card.is-gw{border-color:rgba(236,176,0,.5);box-shadow:0 10px 26px rgba(236,176,0,.12)}
.node-name{font-size:14px;font-weight:700;margin-bottom:4px;color:var(--text)}
.node-ip{font-size:var(--small-text);color:var(--muted);margin-bottom:8px}
.node-ifaces{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:8px}
.iface-chip{font-size:var(--small-text);font-weight:750;padding:3px 7px;border:1px solid;border-radius:999px}
.iface-on {color:var(--text);border-color:rgba(22,163,74,.34);background:rgba(22,163,74,.07)}
.iface-off{color:var(--muted);border-color:var(--border);background:var(--panel)}
.node-battery{font-size:var(--small-text);color:var(--muted)}
.node-tags{position:absolute;top:10px;right:10px;display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end;max-width:45%}

/* interface toggle cards */
.iface-block{padding:12px 16px;border-bottom:1px solid var(--border2)}
.iface-block:last-child{border-bottom:none}
.iface-header{display:flex;align-items:center;gap:12px;margin-bottom:6px;flex-wrap:wrap}
.iface-name{font-size:13px;font-weight:700;min-width:60px}
.iface-band{font-size:var(--small-text);color:var(--muted);letter-spacing:.5px}
.iface-controls{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.txpwr-row{display:flex;align-items:center;gap:8px;margin-top:4px;padding-left:72px;font-size:var(--small-text);color:var(--muted);flex-wrap:wrap}

/* global actions bar */
.global-bar{display:flex;gap:8px;flex-wrap:wrap;padding:12px 16px}

/* pairs */
.pairs-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(260px,100%),1fr));gap:6px;padding:12px 16px}
.pair-item,.pair-row{display:flex;align-items:center;gap:8px;padding:8px 10px;background:var(--card);border:1px solid var(--border2);border-radius:8px;cursor:pointer;transition:border .15s,box-shadow .15s;min-width:0}
.pair-item:hover,.pair-row:hover{border-color:var(--border)}
.pair-item input,.pair-row input{flex-shrink:0}
.pair-label{font-size:12px;flex:1}
.pair-arrow{color:var(--info);margin:0 4px}

/* tests */
.tests-wrap{display:flex;flex-wrap:wrap;gap:8px;padding:12px 16px}
.test-chip{display:flex;align-items:center;gap:6px;padding:8px 12px;background:var(--card);border:1px solid var(--border2);border-radius:8px;cursor:pointer;font-size:var(--small-text);font-weight:700;transition:all .15s;letter-spacing:0}
.test-chip:has(input:checked){border-color:var(--accent2);color:var(--text);background:rgba(236,176,0,.12)}
.test-chip:hover{border-color:var(--border)}

/* progress */
.progress-wrap{padding:12px 16px}
.progress-label{font-size:var(--small-text);color:var(--muted);margin-bottom:6px;letter-spacing:.5px}
.progress-bar-bg{height:6px;background:var(--border2);position:relative;border-radius:999px;overflow:hidden}
.progress-bar-fill{height:6px;background:var(--accent2);transition:width .3s;width:0;border-radius:999px}
.progress-bar-fill.running{animation:progressPulse 1.2s ease-in-out infinite}
@keyframes progressPulse{0%{width:15%;opacity:.55}50%{width:80%;opacity:1}100%{width:15%;opacity:.55}}
.progress-text{font-size:12px;color:var(--info);margin-top:6px;min-height:18px}
.progress-text.done{color:var(--green)}
.progress-text.err{color:var(--red)}
.progress-stats{margin-top:10px;display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:6px}
.stat-chip{border:1px solid var(--border2);background:var(--card);border-radius:8px;padding:8px 10px;font-size:var(--small-text);color:var(--muted);min-height:38px}
.stat-chip strong{display:block;color:var(--text);font-size:12px;margin-top:2px;overflow-wrap:anywhere}
.stat-chip.good strong{color:var(--green)}
.stat-chip.warn strong{color:var(--orange)}
.stat-chip.bad strong{color:var(--red)}

/* msg */
#msg{padding:10px 16px;font-size:12px;display:none;margin-bottom:16px;border-left:3px solid;letter-spacing:.3px}
#msg.ok  {border-color:var(--green);color:#136c36;background:#ecfdf3;display:block}
#msg.err {border-color:var(--red);  color:#b42318;background:#fff5f5;display:block}
#msg.info{border-color:var(--info);color:var(--info);background:rgba(0,0,63,.06);display:block}

/* foreground overlay */
#overlay{position:fixed;left:50%;top:18px;transform:translate(-50%,-130%);z-index:10000;width:min(520px,calc(100vw - 24px));background:var(--surface);border:1px solid var(--info);border-radius:8px;box-shadow:0 18px 60px rgba(2,0,13,.22);opacity:0;transition:transform .18s ease,opacity .18s ease;pointer-events:none}
#overlay.show{transform:translate(-50%,0);opacity:1;pointer-events:auto}
#overlay.ok{border-color:#b8e6c8}
#overlay.err{border-color:#f3b6b1}
#overlay.info{border-color:var(--info)}
.overlay-body{display:flex;align-items:flex-start;gap:10px;padding:12px 14px}
#overlay-text{flex:1;font-size:12px;line-height:1.35;overflow-wrap:anywhere}
#overlay-close{background:transparent;border:0;color:var(--muted);font-family:var(--font);font-size:18px;line-height:1;cursor:pointer;padding:0 2px}
#overlay.ok #overlay-text{color:var(--green)}
#overlay.err #overlay-text{color:var(--red)}
#overlay.info #overlay-text{color:var(--info)}

/* session list */
.session-row{display:flex;align-items:center;gap:12px;padding:10px 16px;border-bottom:1px solid var(--border2);flex-wrap:wrap}
.session-row:last-child{border-bottom:none}
.session-label{flex:1;font-size:13px}
.session-count{font-size:var(--small-text);color:var(--muted)}
.session-actions{display:flex;gap:6px}
.session-summary{width:100%;display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:6px;margin-top:4px}
.metric-mini{background:var(--card);border:1px solid var(--border2);border-radius:8px;padding:6px 8px;font-size:var(--small-text);color:var(--muted)}
.metric-mini strong{display:block;color:var(--text);font-size:var(--small-text);margin-top:2px}

.footer-actions{display:flex;justify-content:flex-end;padding:8px 0 6px}
.logout-link-btn{border:1px solid rgba(180,35,24,.26);background:rgba(180,35,24,.05);color:#b42318;border-radius:999px;padding:8px 14px;font-family:var(--font);font-size:var(--small-text);font-weight:850;cursor:pointer;transition:background .18s ease,color .18s ease,box-shadow .18s ease,transform .18s ease}
.logout-link-btn:hover{background:#b42318;color:#ffffff;box-shadow:0 8px 20px rgba(180,35,24,.16);transform:translateY(-1px)}
:root[data-theme="dark"] .logout-link-btn{border-color:rgba(239,68,68,.34);background:rgba(239,68,68,.08);color:#ffb4b4}
:root[data-theme="dark"] .logout-link-btn:hover{background:#ef4444;color:#ffffff;box-shadow:0 8px 20px rgba(239,68,68,.22)}

@media (max-width: 620px), (orientation:landscape) and (max-height:600px) {
  :root{--small-text:14px}
  html,body{font-size:16px}
  #hdr{position:relative;padding:10px 12px;gap:6px;align-items:flex-start;flex-wrap:wrap}
  .fer-lockup{order:1;min-width:0;width:auto;height:46px;padding-right:12px}
  .fer-logo-img{height:36px}
  #hdr-logo{order:2;font-size:16px;letter-spacing:0;flex:1;min-width:140px}
  #hdr-node{order:3;flex:0 0 100%;border-left:0;padding-left:0;font-size:var(--small-text);max-width:100%;overflow-wrap:anywhere}
  #hdr-right{order:4;flex:0 0 100%;margin-left:0;width:100%;justify-content:flex-start;gap:8px;flex-wrap:wrap}
  #hdr-clock{font-size:var(--small-text)}
  #hdr-inet{font-size:var(--small-text);padding:3px 8px}
  .theme-toggle{padding:5px 8px;min-width:66px;font-size:var(--small-text)}
  #page{flex-direction:column}
  #nav{width:100%;flex:0 0 auto;display:grid;grid-template-columns:repeat(3,1fr);position:static;height:auto;border-right:none;border-bottom:1px solid var(--border2);padding:0;overflow:visible;top:auto}
  .tab{padding:9px 4px;font-size:var(--small-text);white-space:nowrap;text-align:center;border-left:none;border-bottom:3px solid transparent;box-sizing:border-box;overflow:hidden;text-overflow:ellipsis}
  .tab.active{border-left-color:transparent;border-bottom-color:var(--fer-yellow);background:transparent}
  #content{padding:10px}
  #content::before{display:none}
  .card{margin-bottom:10px}
  .card-title{padding:9px 10px;letter-spacing:1px}
  .row{padding:10px;gap:8px;align-items:stretch}
  .row-label{flex:0 0 100%;font-size:var(--small-text)}
  input[type=text],input[type=password],input[type=number],select{width:100%!important;min-width:0}
  .btn{width:100%;justify-content:center;text-align:center;padding:9px 10px}
  .btn-run{padding:12px 10px;font-size:12px;letter-spacing:1px}
  .global-bar{padding:10px;display:grid;grid-template-columns:1fr 1fr;gap:8px}
  .global-bar .btn{width:100%;font-size:var(--small-text);letter-spacing:.5px}
  .node-grid{padding:10px;grid-template-columns:1fr}
  .node-card{padding:12px}
  .node-tags{position:static;max-width:none;margin-bottom:6px;justify-content:flex-start}
  .iface-block{padding:10px}
  .iface-header{gap:8px}
  .iface-name{min-width:52px}
  .iface-band{flex:1;min-width:90px}
  .iface-controls{width:100%;display:grid;grid-template-columns:1fr}
  .txpwr-row{padding-left:0;display:grid;grid-template-columns:auto 72px auto 1fr;align-items:center}
  .txpwr-row input{width:72px!important}
  .txpwr-row .btn{width:auto}
  .pairs-grid{padding:10px;grid-template-columns:1fr}
  .pair-row label{min-width:0;overflow-wrap:anywhere}
  .tests-wrap{padding:10px;display:grid;grid-template-columns:1fr;gap:6px}
  .test-chip{width:100%;min-width:0}
  .session-row{padding:10px;align-items:flex-start}
  .session-label,.session-count,.session-actions{width:100%}
  .progress-stats,.session-summary{grid-template-columns:1fr}
  .session-actions{display:grid;grid-template-columns:1fr 1fr}
  table{display:block;overflow-x:auto;-webkit-overflow-scrolling:touch}
  body.chrome-compact #hdr{gap:6px;padding:6px 12px}
  body.chrome-compact #hdr-node{display:none}
  body.chrome-compact #nav{top:0}
  body.chrome-compact #content::before{height:6px;margin:-10px -10px 10px}
  #overlay{top:10px;width:calc(100vw - 16px)}
  #content::before{margin:-10px -10px 12px}
  .tab{font-size:14px;white-space:normal;display:flex;align-items:center;justify-content:center}
  button,.btn,.tab,summary,.test-chip,.pair-row{min-height:44px;min-width:44px}
  input[type=text],input[type=password],input[type=number],select{min-height:44px;font-size:16px}
  input[type=checkbox]{width:44px;height:44px;flex-shrink:0;accent-color:var(--info)}
  .btn,.btn-run,.global-bar .btn,.theme-toggle,.overview-link-btn,.logout-link-btn{font-size:14px}
  .card-title,.row-label,.iface-name,.node-name,.session-label{font-size:16px;letter-spacing:0}
  #hdr-node,#hdr-clock,#hdr-inet,.node-ip,.node-battery,.iface-band,.iface-chip,.badge,
  .pair-label,.test-chip,.progress-label,.progress-text,.stat-chip,.stat-chip strong,
  .session-count,.metric-mini,.metric-mini strong,#overlay-text,.txpwr-row{font-size:14px}
  .node-name,.session-label,.node-ip{overflow-wrap:anywhere;min-width:0}
  .txpwr-row{grid-template-columns:minmax(0,1fr);gap:8px}
  .txpwr-row input{width:100%!important}
  #recovery-summary{padding:10px}
  #overlay-close{flex-shrink:0}
}
@media (orientation:landscape) and (max-height:600px){
  #hdr{position:relative;padding:6px 10px;gap:4px 8px;align-items:center}
  .fer-lockup{height:38px;padding-right:10px}
  .fer-logo-img{height:30px}
  #hdr-logo{min-height:38px;min-width:0;font-size:16px}
  #hdr-node{order:4;font-size:14px}
  #hdr-right{order:3;flex:0 1 auto;width:auto;margin-left:auto}
  #hdr-clock{display:none}
  #page{flex-direction:row}
  #nav{display:flex;flex-direction:column;width:128px;flex:0 0 128px;position:sticky;top:0;height:100svh;overflow-y:auto;border-right:1px solid var(--border2);border-bottom:0}
  .tab{flex-shrink:0;justify-content:flex-start;text-align:left;padding:10px 12px}
  #content{padding:10px}
  #content::before{display:none}
  .node-grid{grid-template-columns:repeat(auto-fit,minmax(min(220px,100%),1fr))}
  .tests-wrap{grid-template-columns:repeat(2,minmax(0,1fr))}
  .iface-controls{display:flex;flex-wrap:wrap}
  .txpwr-row{grid-template-columns:auto minmax(80px,1fr) auto}
  body.chrome-compact #hdr-node{display:block;opacity:1;max-width:none}
  body.chrome-compact #nav{top:0;height:100svh}
}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{transition:none!important;scroll-behavior:auto!important}
}
"""

# Browser polling and radio/measurement controls call the management routes.
JS = """
let _topo = null;
let _tab  = 'topology';
let _msgTimer = null;
let _pollTimer = null;
let _overlayTimer = null;
let _autoRefreshTimer = null;
let _autoRefreshBusy = false;
// This page is served under /manage, and its own routes live under that
// prefix. Worked out from the URL rather than baked in, so the page still
// functions if the prefix ever moves.
const MANAGE_BASE = (() => {
  const m = window.location.pathname.match(/^(\\/[^/]*manage)(\\/|$)/);
  return m ? m[1] : '';
})();
// U() is for routes this page owns. /api/admin/* is served at the site root by
// mesh-status.py and must NOT be prefixed.
//
// Any new fetch to a route in this file has to go through U(). Without it the
// request lands on mesh-status.py's root router, which answers 404 with the
// text "Not found", causing JSON.parse to fail.
function U(path) { return MANAGE_BASE + path; }

// Mesh time can differ from this browser's clock (a mesh without GPS or
// internet keeps its own shared time), so countdowns to mesh times such as
// activate_at use meshNow(). Every JSON reply carries the mesh clock; between
// replies it advances on performance.now(), which a correction of this
// device's own clock cannot move.
let meshAnchor = null;
function meshNow() {
  return meshAnchor ? meshAnchor.mesh + (performance.now() - meshAnchor.perf) / 1000
                    : Date.now() / 1000;
}

let manageRetryAt = 0;
async function manageFetch(url, options) {
  if (performance.now() < manageRetryAt) throw new Error('Radio busy; retry shortly');
  const response = await fetch(url, {...options, signal: AbortSignal.timeout(options ? 180000 : 45000)});
  const meshTime = parseFloat(response.headers.get('X-Mesh-Time'));
  if (Number.isFinite(meshTime)) meshAnchor = {mesh: meshTime, perf: performance.now()};
  if (response.status === 503) {
    manageRetryAt = performance.now() + 5000;
    throw new Error('Radio busy; retry shortly');
  }
  if (response.status === 401) {
    const next = window.location.pathname + window.location.search + window.location.hash;
    window.location.replace(U('/login') + '?next=' + encodeURIComponent(next));
    throw new Error('Management login expired. Please sign in again.');
  }
  return response;
}

const VALID_TABS = ['topology','radio','measure','sessions','uplink','voice','config'];
const AUTO_REFRESH_MS = 15000;
const THEME_KEY = 'manetUiTheme';

function preferredTheme() {
  const params = new URLSearchParams(window.location.search);
  const forced = params.get('theme');
  if (forced === 'dark' || forced === 'light') {
    try { localStorage.setItem(THEME_KEY, forced); } catch(e) {}
    return forced;
  }
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved === 'dark' || saved === 'light') return saved;
  } catch(e) {}
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const btn = document.getElementById('theme-toggle');
  if (btn) btn.textContent = theme === 'dark' ? 'Light' : 'Dark';
  document.querySelectorAll('.fer-logo-img[data-light][data-dark]').forEach(img => {
    img.src = theme === 'dark' ? img.dataset.dark : img.dataset.light;
  });
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  try { localStorage.setItem(THEME_KEY, next); } catch(e) {}
  setTheme(next);
}

setTheme(preferredTheme());

let topoBusy = false;
async function fetchTopo() {
  if (topoBusy || document.hidden) return;
  topoBusy = true;
  try {
    const r = await manageFetch(U('/api/topology'));
    _topo = await r.json();
    renderTopology();
    buildHalowConfig();
    updatePairs();
  } catch(e) { showMsg('Topology fetch failed: ' + e, 'err'); }
  finally { topoBusy = false; }
}

function userIsEditing() {
  const el = document.activeElement;
  if (!el) return false;
  return ['INPUT','SELECT','TEXTAREA'].includes(el.tagName);
}

async function autoRefreshUi() {
  if (_autoRefreshBusy || document.hidden || userIsEditing()) return;
  _autoRefreshBusy = true;
  try {
    await fetchTopo();
    if (_tab === 'radio') buildIfaceControl();
    if (_tab === 'sessions') await loadSessions();
  } finally {
    _autoRefreshBusy = false;
  }
}

function startAutoRefresh() {
  clearInterval(_autoRefreshTimer);
  _autoRefreshTimer = setInterval(autoRefreshUi, AUTO_REFRESH_MS);
}

function updateChromeCompact() {
  if (window.innerWidth <= 620) { document.body.classList.remove('chrome-compact'); return; }
  document.body.classList.toggle('chrome-compact', window.scrollY > 24);
}

function showMsg(txt, cls) {
  const el = document.getElementById('msg');
  if (!el) return;
  clearTimeout(_msgTimer);
  el.textContent = txt;
  el.className = cls;
  el.style.display = 'block';
  if (cls !== 'info') {
    _msgTimer = setTimeout(() => { el.style.display = 'none'; }, 8000);
  }
  if (cls === 'err' || cls === 'ok') showOverlay(txt, cls);
}

function showOverlay(txt, cls) {
  const box = document.getElementById('overlay');
  const text = document.getElementById('overlay-text');
  if (!box || !text) return;
  clearTimeout(_overlayTimer);
  text.textContent = txt;
  box.className = (cls || 'info') + ' show';
  _overlayTimer = setTimeout(hideOverlay, cls === 'err' ? 12000 : 5500);
}

function hideOverlay() {
  const box = document.getElementById('overlay');
  if (box) box.className = box.className.replace(' show', '');
}

function setRunButton(running, label) {
  const btn = document.getElementById('btn-run');
  if (!btn) return;
  btn.disabled = !!running;
  btn.textContent = label || (running ? 'RUNNING...' : '▶ RUN MEASUREMENTS');
}

function setButtonBusy(id, busy, label, idleLabel) {
  const btn = document.getElementById(id);
  if (!btn) return;
  btn.disabled = !!busy;
  btn.textContent = busy ? label : idleLabel;
}

function normalizeDbm(value) {
  const num = parseFloat(value);
  if (!Number.isFinite(num)) return '';
  return Number.isInteger(num) ? String(num) : String(num.toFixed(1)).replace(/[.]0$/, '');
}

function txPowerOptions(info) {
  const opts = Array.isArray(info?.txpower_options_dbm) ? info.txpower_options_dbm.map(normalizeDbm).filter(Boolean) : [];
  const cur = normalizeDbm(info?.txpower_dbm);
  if (cur && !opts.includes(cur)) opts.push(cur);
  opts.sort((a, b) => parseFloat(a) - parseFloat(b));
  return opts;
}

function renderTxPowerSelect(id, info) {
  const opts = txPowerOptions(info);
  if (!opts.length) {
    const cur = normalizeDbm(info?.txpower_dbm) || '';
    return `<input id="${esc(id)}" type="number" min="1" max="30" step="1" value="${esc(cur)}" style="width:60px" placeholder="dBm">`;
  }
  const current = normalizeDbm(info?.txpower_dbm) || opts[opts.length - 1];
  return `<select id="${esc(id)}">` +
    opts.map(v => `<option value="${v}"${v === current ? ' selected' : ''}>${v} dBm</option>`).join('') +
    `</select>`;
}

// HaLow power is set by the Morse driver when the radio starts and cannot be
// changed live (reloading the driver can wedge the USB card), so it is shown,
// not offered as a control. The value is what the firmware/driver reports.
function isHalowIface(iface, info) {
  return iface === 'wlan2' || !!(info && info.halow_bw);
}

function renderHalowPower(info) {
  const cur = normalizeDbm(info?.txpower_dbm);
  return `<strong>${cur ? cur + ' dBm' : '-'}</strong> ` +
    `<span style="color:var(--muted)">reported; set when the radio starts, a change needs a reboot</span>`;
}

function updateHalowTxpowerOptions() {
  const el = document.getElementById('halow-txpower');
  if (el) el.innerHTML = renderHalowPower(getNodeInfo('all', 'wlan2'));
}

function getNodeInfo(nodeIp, iface) {
  if (!_topo || !_topo.nodes) return {};
  const node = _topo.nodes.find(n => n.ip === nodeIp || (nodeIp === 'all' && n.is_me));
  if (!node || !node.interfaces) return {};
  return node.interfaces[iface] || {};
}

function syncSelectValue(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  const normalized = value == null ? '' : String(value);
  const hasOption = Array.from(el.options || []).some(opt => opt.value === normalized);
  if (hasOption) el.value = normalized;
}

function radioTargetNodes(nodeIp) {
  if (!_topo || !_topo.nodes) return [];
  if (nodeIp === 'all') return _topo.nodes;
  return _topo.nodes.filter(n => n.ip === nodeIp);
}

function radioStateOk(nodes, iface, state) {
  const expectedActive = state === 'up';
  const bad = [];
  for (const node of nodes) {
    const info = node.interfaces && node.interfaces[iface];
    const active = !!(info && info.active === true);
    if (active !== expectedActive) bad.push(node.hostname || node.ip || 'unknown');
  }
  return {ok: bad.length === 0, bad};
}

function confirmRadioDown(nodeIp, iface) {
  if (nodeIp !== 'all') {
    const node = radioTargetNodes(nodeIp)[0];
    const label = node ? `${node.hostname} (${node.ip})` : nodeIp;
    return confirm(
      `Disable ${iface} on ${label}?\\n\\n` +
      `This will stop the wpa_supplicant service for that radio.`
    );
  }
  const nodes = radioTargetNodes('all');
  const nodeList = nodes.map(n => n.hostname || n.ip).join(', ') || 'all nodes';
  return confirm(
    `Disable ${iface} on ALL nodes?\\n\\n` +
    `Targets: ${nodeList}\\n\\n` +
    `The change will be staged through Alfred, all nodes must ACK, then the wpa_supplicant service for ${iface} will be stopped.`
  );
}

async function verifyRadioExecution(nodeIp, iface, state, activateAt) {
  const delayMs = Math.max(0, ((activateAt || 0) - Math.floor(meshNow()) + 3) * 1000);
  await new Promise(resolve => setTimeout(resolve, delayMs));

  showOverlay(`Verifying ${iface} ${state} after coordinated apply...`, 'info');
  showMsg(`Verifying ${iface} ${state} after coordinated apply...`, 'info');

  let lastBad = [];
  for (let attempt = 0; attempt < 15; attempt++) {
    await fetchTopo();
    const nodes = radioTargetNodes(nodeIp);
    const result = radioStateOk(nodes, iface, state);
    if (nodes.length && result.ok) {
      if (_tab === 'radio') buildIfaceControl();
      const scope = nodeIp === 'all' ? 'all nodes' : (nodes[0].hostname || nodeIp);
      showOverlay(`${iface} ${state} executed on ${scope}`, 'ok');
      showMsg(`${iface} ${state} executed on ${scope}`, 'ok');
      return true;
    }
    lastBad = result.bad;
    await new Promise(resolve => setTimeout(resolve, 3000));
  }

  buildIfaceControl();
  const msg = `${iface} ${state} was scheduled, but execution is not confirmed` +
              (lastBad.length ? ` on: ${lastBad.join(', ')}` : '');
  showOverlay(msg, 'err');
  showMsg(msg, 'err');
  return false;
}

function setProgress(text, state) {
  const card = document.getElementById('progress-card');
  const txt  = document.getElementById('progress-text');
  const fill = document.getElementById('progress-fill');
  if (!card || !txt || !fill) return;
  card.style.display = '';
  txt.textContent = text || '';
  txt.className = 'progress-text' + (state === 'err' ? ' err' : state === 'done' ? ' done' : '');
  fill.className = 'progress-bar-fill' + (state === 'running' ? ' running' : '');
  fill.style.background = state === 'err' ? 'var(--red)' : state === 'done' ? 'var(--green)' : 'var(--accent)';
  fill.style.width = state === 'running' ? '60%' : state ? '100%' : '0';
}

function fmtSecs(seconds) {
  seconds = Math.max(0, Math.floor(seconds || 0));
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m ? `${m}m ${s}s` : `${s}s`;
}

function metricText(stats, unit) {
  if (!stats || stats.avg == null) return '';
  return `avg ${stats.avg}${unit} / min ${stats.min}${unit} / max ${stats.max}${unit}`;
}

function lastResultText(r) {
  if (!r) return 'waiting for first completed test';
  const parts = [];
  if (r.tcp_mbps != null) parts.push(`TCP ${r.tcp_mbps} Mbps`);
  if (r.udp_mbps != null) parts.push(`UDP ${r.udp_mbps} Mbps`);
  if (r.rtt_avg != null) parts.push(`RTT ${r.rtt_avg} ms`);
  if (r.jitter_ms != null) parts.push(`jitter ${r.jitter_ms} ms`);
  if (r.loss_pct != null) parts.push(`loss ${r.loss_pct}%`);
  if (r.error) parts.push(r.error);
  const metrics = parts.length ? parts.join(' · ') : (r.ok ? 'ok' : 'failed');
  return `${r.src} -> ${r.dst} ${r.test_type}: ${metrics}`;
}

function renderMeasureStats(d) {
  const el = document.getElementById('progress-stats');
  if (!el) return;
  const done = d.done || 0;
  const total = d.total || 0;
  const pct = total ? Math.round((done / total) * 100) : 0;
  // Durations are measured by the node on its monotonic clock.
  const elapsed = d.elapsed != null ? fmtSecs(d.elapsed) : '0s';
  const curElapsed = d.current_elapsed != null ? fmtSecs(d.current_elapsed) : '-';
  const cur = d.current ? `${d.current.src} -> ${d.current.dst} ${d.current.test_type}` : '-';
  el.innerHTML = `
    <div class="stat-chip"><span>completed</span><strong>${esc(done)}/${esc(total)} (${pct}%)</strong></div>
    <div class="stat-chip"><span>elapsed</span><strong>${esc(elapsed)}</strong></div>
    <div class="stat-chip"><span>current</span><strong>${esc(cur)}</strong></div>
    <div class="stat-chip"><span>current time</span><strong>${esc(curElapsed)}</strong></div>
    <div class="stat-chip ${d.last_result && d.last_result.ok ? 'good' : ''}" style="grid-column:1/-1"><span>last result</span><strong>${esc(lastResultText(d.last_result))}</strong></div>`;
}

function getInitialTab() {
  const hashTab = window.location.hash ? window.location.hash.substring(1) : '';
  if (VALID_TABS.includes(hashTab)) return hashTab;
  try {
    const saved = localStorage.getItem('perfDashboardTab');
    if (VALID_TABS.includes(saved)) return saved;
  } catch(e) {}
  return 'topology';
}

function showTab(name, updateUrl = true) {
  if (!VALID_TABS.includes(name)) name = 'topology';
  _tab = name;
  try { localStorage.setItem('perfDashboardTab', name); } catch(e) {}
  if (updateUrl && window.location.hash !== '#' + name) {
    history.replaceState(null, '', '#' + name);
  }
  document.querySelectorAll('.tab').forEach(t => {
    const selected = t.dataset.tab === name;
    t.classList.toggle('active', selected);
    t.setAttribute('aria-selected', String(selected));
    t.tabIndex = selected ? 0 : -1;
  });
  document.querySelectorAll('.tab-pane').forEach(p => p.style.display = p.id === 'tab-' + name ? '' : 'none');
  if (name === 'sessions') loadSessions();
  if (name === 'radio') buildIfaceControl();
  if (name === 'uplink') loadWifiUplink();
  if (name === 'config') startConfigPolling(); else stopConfigPolling();
  if (name === 'voice') startVoicePolling(); else stopVoicePolling();
}

// A single tab stop enters navigation; arrows move between sections.
document.getElementById('nav').addEventListener('keydown', event => {
  const tabs = Array.from(document.querySelectorAll('.tab'));
  const index = tabs.indexOf(event.target);
  if (index < 0) return;
  let next;
  if (event.key === 'Home') next = 0;
  else if (event.key === 'End') next = tabs.length - 1;
  else if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (index + 1) % tabs.length;
  else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (index + tabs.length - 1) % tabs.length;
  else return;
  event.preventDefault();
  tabs[next].focus();
  showTab(tabs[next].dataset.tab);
});

// ── Clock ──
function tickLocalTime() {
  const el = document.getElementById('hdr-clock');
  if (el) el.textContent = new Date().toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false});
}
setInterval(tickLocalTime, 1000);
tickLocalTime();

// ── Topology tab ──
function renderTopology() {
  if (!_topo) return;
  document.getElementById('recovery-status').innerHTML = recoveryPanel(_topo.recovery);
  document.getElementById('recovery-summary').textContent = 'Mesh recovery: ' + (_topo.recovery?.summary || 'status unavailable');
  const grid = document.getElementById('node-grid');
  grid.innerHTML = '';
  for (const node of _topo.nodes) {
    const ifaces = node.interfaces || {};
    const chips = ['wlan0','wlan1','wlan2'].map(i => {
      const info = ifaces[i] || {};
      const on = info.active === true;
      const band = i === 'wlan0' ? '2.4G' : i === 'wlan1' ? '5G' : 'HaLow';
      const ch = info.channel ? ` ch${info.channel}` : (info.freq_mhz ? ` ${Math.round(info.freq_mhz)}MHz` : '');
      const mcs = on && (info.tx_mcs || info.rx_mcs) ? ` · ${info.tx_mcs || '-'} / ${info.rx_mcs || '-'}` : '';
      return `<span class="iface-chip ${on ? 'iface-on' : 'iface-off'}">${band}${esc(info.uplink ? ' UPLINK' : (on ? ch : ' OFF'))}${esc(mcs)}</span>`;
    }).join('');
    const bat = node.battery ? `<div class="node-battery">BAT ${esc(node.battery)}%</div>` : '';
    const tags = [
      node.is_me ? '<span class="badge b-me" style="font-size:var(--small-text)">ME</span>' : '',
      node.is_gateway ? `<span class="badge b-gw" style="font-size:var(--small-text)">GW${esc(node.gateway_iface ? '·' + node.gateway_iface : '')}</span>` : '',
    ].filter(Boolean).join('');
    const cls = [node.is_me ? 'is-me' : '', node.is_gateway ? 'is-gw' : ''].join(' ');
    grid.innerHTML += `<div class="node-card ${cls}">
      <div class="node-tags">${tags}</div>
      <div class="node-name">${esc(node.hostname)}</div>
      ${node.operator ? `<div class="node-ip">Operator: ${esc(node.operator)}
        </div>` : ''}
      <div class="node-ip">${esc(node.ip)}</div>
      <div class="node-ifaces">${chips}</div>
      ${bat}
    </div>`;
  }
  // Internet + upload buttons
  const inet = document.getElementById('hdr-inet');
  if (_topo.internet) { inet.textContent = '● INET OK'; inet.className = 'ok'; }
  else { inet.textContent = '○ NO INET'; inet.className = 'no'; }
}

// ── Interface control tab ──
function buildIfaceControl() {
  if (!_topo) return;
  const wrap = document.getElementById('iface-cards');
  wrap.innerHTML = '';
  for (const node of _topo.nodes) {
    const ifaces = node.interfaces || {};
    const card = document.createElement('div');
    card.className = 'card';
    const BANDS = {wlan0: '2.4 GHz', wlan1: '5 GHz', wlan2: 'HaLow'};
    let html = `<div class="card-title">${esc(node.hostname)} &nbsp;<span style="color:var(--muted);font-size:var(--small-text)">${esc(node.ip)}${node.is_me ? ' &bull; THIS NODE' : ''}</span></div>`;
    for (const iface of ['wlan0','wlan1','wlan2']) {
      const info = ifaces[iface] || {};
      const on = info.active === true;
      html += `<div class="iface-block">
        <div class="iface-header">
          <span class="iface-name">${iface}</span>
          <span class="iface-band">${BANDS[iface]}</span>
          <span class="badge ${on ? 'b-on' : 'b-off'}" id="ibadge-${esc(node.id)}-${iface}">${info.uplink ? 'UPLINK' : (on ? 'ACTIVE' : 'DOWN')}</span>
          <div class="iface-controls">
            ${info.uplink ? '<span>Use this node’s Uplink tab</span>' : on
              ? `<button class="btn btn-red" data-ip="${esc(node.ip)}" data-id="${esc(node.id)}" data-iface="${iface}" data-action="toggle-interface" data-state="down">DISABLE</button>`
              : `<button class="btn btn-green" data-ip="${esc(node.ip)}" data-id="${esc(node.id)}" data-iface="${iface}" data-action="toggle-interface" data-state="up">ENABLE</button>`
            }
          </div>
        </div>
        <div class="txpwr-row">
          LINK RATE
          <strong>${esc(info.tx_mcs || '-')}</strong>
          /
          <strong>${esc(info.rx_mcs || '-')}</strong>
          <span style="color:var(--muted)">TX / RX</span>
        </div>
        <div class="txpwr-row">
          TX POWER
          ${info.uplink ? '<span>Automatic uplink power</span>' : isHalowIface(iface, info) ? renderHalowPower(info) : `${renderTxPowerSelect(`txpwr-${node.id}-${iface}`, info)}
          <button class="btn" style="padding:4px 10px;font-size:var(--small-text)" data-ip="${esc(node.ip)}" data-id="${esc(node.id)}" data-iface="${iface}" data-action="set-power">SET</button>`}
        </div>
      </div>`;
    }
    card.innerHTML = html;
    wrap.appendChild(card);
  }
}

async function toggleIface(nodeIp, nodeId, iface, state) {
  if (state === 'down' && !confirmRadioDown(nodeIp, iface)) return;
  const isAll = nodeIp === 'all';
  showOverlay(isAll
    ? `Coordinating ${iface} ${state} on all nodes through Alfred...`
    : `Setting ${iface} ${state} on ${nodeIp}...`, 'info');
  showMsg(isAll
    ? `Staging ${iface} ${state}; waiting for mesh ACKs...`
    : `Setting ${iface} ${state} on ${nodeIp}...`, 'info');
  try {
    const r = await manageFetch(U('/api/interface/toggle'), {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({node_ip: nodeIp, iface, state})
    });
    const d = await r.json();
    if (d.ok) {
      if (d.activate_at) {
        const wait = Math.max(0, d.activate_at - Math.floor(meshNow()));
        showOverlay(`${iface} ${state} ACKed by ${d.acked?.length || 0}/${d.expected?.length || 0} nodes. Applying in ${wait}s...`, 'ok');
        showMsg(`${iface} ${state} scheduled through Alfred`, 'ok');
        verifyRadioExecution(nodeIp, iface, state, d.activate_at);
      } else {
        showOverlay(`${iface} ${state} applied on ${nodeIp}`, 'ok');
        showMsg(`${iface} ${state} applied`, 'ok');
        setTimeout(() => { hideOverlay(); fetchTopo(); }, 3000);
      }
    } else {
      showOverlay(`Radio change failed: ${d.error}`, 'err');
      showMsg('Error: ' + d.error, 'err');
    }
  } catch(e) {
    showOverlay(`Radio change failed: ${e.message}`, 'err');
    showMsg('Error: ' + e.message, 'err');
  }
}

async function setTxPower(nodeIp, nodeId, iface) {
  const dbm = document.getElementById(`txpwr-${nodeId}-${iface}`).value;
  if (!dbm) {
    showMsg(`No TX power options available for ${iface}@${nodeIp}`, 'err');
    return;
  }
  const r = await manageFetch(U('/api/txpower'), {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({node_ip: nodeIp, iface, dbm: parseFloat(dbm)})
  });
  const d = await r.json();
  if (d.ok) showMsg(`TX power set to ${dbm}dBm on ${iface}@${nodeIp}`, 'ok');
  else showMsg('Error: ' + d.error, 'err');
}

async function toggleAll(iface, state) {
  if (state === 'down' && !confirmRadioDown('all', iface)) return;
  showOverlay(`Coordinating ${iface} ${state} on all nodes through Alfred...`, 'info');
  showMsg(`Staging ${iface} ${state}; waiting for all mesh ACKs...`, 'info');
  try {
    const r = await manageFetch(U('/api/interface/toggle'), {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({node_ip: 'all', iface, state})
    });
    const d = await r.json();
    if (d.ok) {
      const wait = d.activate_at ? Math.max(0, d.activate_at - Math.floor(meshNow())) : 0;
      showOverlay(`${iface} ${state} ACKed by ${d.acked?.length || 0}/${d.expected?.length || 0} nodes. Applying in ${wait}s...`, 'ok');
      showMsg(`${iface} ${state} scheduled on all nodes through Alfred`, 'ok');
      verifyRadioExecution('all', iface, state, d.activate_at);
    } else {
      showOverlay(`Radio change failed: ${d.error}`, 'err');
      showMsg('Error: ' + d.error, 'err');
    }
  } catch(e) {
    showOverlay(`Radio change failed: ${e.message}`, 'err');
    showMsg('Error: ' + e.message, 'err');
  }
}

// ── HaLow config tab ──
// The channel and bandwidth menus are not static: the S1G plan is per region,
// and which channels exist depends on the width (a US 8 MHz channel is not a
// 1 MHz channel number). Both are rendered from _topo.halow_plan, which the
// node builds from the same table the apply path validates against.
function halowPlan() {
  return (_topo && _topo.halow_plan) || {region: '', bandwidths: [], channels: {}};
}

function renderHalowBandwidths(preferred) {
  const plan = halowPlan();
  const select = document.getElementById('halow-bw');
  if (!select || !plan.bandwidths.length) return '';
  const want = plan.bandwidths.includes(preferred) ? preferred
             : (plan.bandwidths.includes(select.value) ? select.value : plan.bandwidths[0]);
  select.innerHTML = plan.bandwidths
    .map(bw => `<option value="${esc(bw)}"${bw === want ? ' selected' : ''}>${parseInt(bw)} MHz</option>`)
    .join('');
  select.value = want;
  return want;
}

function renderHalowChannels(bw, preferred) {
  const select = document.getElementById('halow-ch');
  if (!select) return;
  const list = halowPlan().channels[bw] || [];
  if (!list.length) {
    select.innerHTML = '<option value="">n/a</option>';
    return;
  }
  const want = String(preferred || select.value || '');
  const match = list.some(c => String(c.channel) === want) ? want : String(list[0].channel);
  select.innerHTML = list
    .map(c => `<option value="${esc(c.channel)}"${String(c.channel) === match ? ' selected' : ''}>` +
              `ch ${esc(c.channel)} (${esc(c.mhz)} MHz)</option>`)
    .join('');
  select.value = match;
}

function onHalowBandwidthChange() {
  const bw = document.getElementById('halow-bw').value;
  renderHalowChannels(bw, '');
  updateHalowTxpowerOptions();
}

function buildHalowConfig() {
  const managed = _topo.acs_enabled !== false;
  for (const id of ['ch-2g', 'ch-5g', 'btn-apply-2g', 'btn-apply-5g']) {
    const element = document.getElementById(id);
    if (element) element.disabled = managed;
  }
  for (const element of document.querySelectorAll('.acs-channel-note')) {
    element.textContent = managed ? 'Channels are selected automatically by ACS.' : '';
  }
  const halowInfo = getNodeInfo('all', 'wlan2');
  const bw = renderHalowBandwidths(halowInfo.halow_bw);
  renderHalowChannels(bw, halowInfo.channel);
  syncSelectValue('halow-ch', halowInfo.channel);
  syncSelectValue('ch-2g', getNodeInfo('all', 'wlan0').channel);
  syncSelectValue('ch-5g', getNodeInfo('all', 'wlan1').channel);
  for (const iface of ['wlan0', 'wlan1']) {
    const info = getNodeInfo('all', iface);
    const select = document.getElementById(`txpwr-all-${iface}`);
    if (select) {
      select.outerHTML = renderTxPowerSelect(`txpwr-all-${iface}`, info);
    }
  }
  updateHalowTxpowerOptions();
}

async function applyHalow() {
  const ch = document.getElementById('halow-ch').value;
  const bw = document.getElementById('halow-bw').value;
  setButtonBusy('btn-apply-halow', true, 'APPLYING...', 'APPLY TO ALL NODES');
  showOverlay(`Applying HaLow ch${ch} / ${bw}: verifying all nodes...`, 'info');
  try {
    const r = await manageFetch(U('/api/halow/channel'), {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({channel: parseInt(ch), bw})
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    if (!d.ok) {
      const msg = d.rolled_back
        ? `ROLLED BACK: ${d.error}` + (d.unreachable?.length ? ` · not in mesh: ${d.unreachable.join(', ')}` : '')
        : d.error;
      showMsg(msg, 'err');
      return;
    }
    let msg = `HaLow ch${ch} / ${bw} applied to: ${d.applied?.join(', ')}`;
    if (d.unreachable?.length) msg += ` · WARNING: not in mesh: ${d.unreachable.join(', ')}`;
    if (d.warning) msg += ` - WARNING: ${d.warning}`;
    showMsg(msg, (d.warning || d.unreachable?.length) ? 'info' : 'ok');
    await fetchTopo();
  } catch (e) {
    showMsg('HaLow apply failed: ' + e.message, 'err');
  } finally {
    setButtonBusy('btn-apply-halow', false, '', 'APPLY TO ALL NODES');
  }
}

async function apply2G() {
  const ch = document.getElementById('ch-2g').value;
  const dbm = document.getElementById('txpwr-all-wlan0').value;
  setButtonBusy('btn-apply-2g', true, 'APPLYING...', 'APPLY TO ALL NODES');
  showOverlay(`Applying 2.4G ch${ch} / ${dbm} dBm to all nodes...`, 'info');
  showMsg(`Applying 2.4G ch${ch} / ${dbm} dBm to all nodes...`, 'info');
  try {
    const r = await manageFetch(U('/api/wifi/channel'), {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({band: '2.4', channel: parseInt(ch), dbm: parseFloat(dbm)})
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
    showMsg(`2.4G ch${ch} / ${dbm} dBm applied to all nodes`, 'ok');
    await fetchTopo();
  } catch (e) {
    showMsg('2.4G channel failed: ' + e.message, 'err');
  } finally {
    setButtonBusy('btn-apply-2g', false, '', 'APPLY TO ALL NODES');
  }
}

async function apply5G() {
  const ch = document.getElementById('ch-5g').value;
  const dbm = document.getElementById('txpwr-all-wlan1').value;
  setButtonBusy('btn-apply-5g', true, 'APPLYING...', 'APPLY TO ALL NODES');
  showOverlay(`Applying 5G ch${ch} / ${dbm} dBm to all nodes...`, 'info');
  try {
    const r = await manageFetch(U('/api/wifi/channel'), {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({band: '5', channel: parseInt(ch), dbm: parseFloat(dbm)})
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
    showMsg(`5G ch${ch} / ${dbm} dBm applied to all nodes`, 'ok');
    await fetchTopo();
  } catch (e) {
    showMsg('5G channel failed: ' + e.message, 'err');
  } finally {
    setButtonBusy('btn-apply-5g', false, '', 'APPLY TO ALL NODES');
  }
}

// Wi-Fi uplink picker. Keep scan identity separate from periodically polled status.
let uplinkNetworks = [], uplinkSelected = -1, uplinkScanIface = '', uplinkScanDevice = '';
let uplinkScannedAt = 0, uplinkBusy = false, uplinkInitialized = false, uplinkStatus = null;
let uplinkScanId = null;
let uplinkScanResolve = null;
let uplinkSavedNetworks = [], uplinkDifferentPassword = false;

function uplinkSavedMarkup() {
  return '<span class="uplink-saved-mark"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true" focusable="false"><circle cx="8" cy="8" r="5"/><path d="m12 12 9 9m-4-4 3-3m-7-1 3-3"/></svg>Saved</span>';
}

function updateSavedNetworks(networks) {
  if (!Array.isArray(networks)) return;
  const changed = JSON.stringify(networks) !== JSON.stringify(uplinkSavedNetworks);
  uplinkSavedNetworks = networks;
  if (changed) {
    document.getElementById('uplink-wifi-saved-list').innerHTML = networks.map((ap, index) =>
      `<li class="uplink-saved-row"><span><span class="uplink-name">${esc(ap.ssid)}</span>
      <span class="uplink-detail">${ap.security === 'sae' ? 'WPA3' : 'WPA2'}</span></span>
      <button type="button" class="btn" data-action="forget-uplink" data-index="${index}"
      aria-label="Forget ${esc(ap.ssid)} ${ap.security === 'sae' ? 'WPA3' : 'WPA2'}">Forget</button></li>`).join('');
  }
  document.getElementById('uplink-wifi-saved-empty').hidden = networks.length > 0;
  let rowsChanged = false;
  for (const ap of uplinkNetworks) {
    const saved = ap.security !== 'open' && networks.some(n => n.ssid === ap.ssid && n.security === ap.security);
    if (!!ap.saved !== saved) rowsChanged = true;
    ap.saved = saved;
  }
  if (rowsChanged) document.getElementById('uplink-wifi-results').innerHTML = uplinkNetworks.map(uplinkNetworkMarkup).join('');
}

function useDifferentUplinkPassword() {
  if (uplinkBusy) return;
  uplinkDifferentPassword = true;
  updateUplinkControls();
  document.getElementById('uplink-wifi-password').focus();
}

async function forgetUplinkNetwork(index) {
  const ap = uplinkSavedNetworks[index];
  if (uplinkBusy || !ap) return;
  uplinkBusy = true;
  updateUplinkControls();
  const feedback = document.getElementById('uplink-wifi-saved-feedback');
  feedback.textContent = 'Forgetting saved password...';
  try {
    const r = await manageFetch(U('/api/uplink/wifi/forget'), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ssid: ap.ssid, security: ap.security})
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
    updateSavedNetworks(d.saved_networks);
    renderUplinkCurrent();
    feedback.textContent = 'Saved password forgotten. The current connection stays connected.';
    document.getElementById('uplink-wifi-saved-heading').focus();
  } catch (e) { feedback.textContent = uplinkError(e); }
  finally { uplinkBusy = false; updateUplinkControls(); }
}

function uplinkBand(ap) {
  return ap.freq >= 5000 ? '5 GHz' : ap.freq >= 2400 ? '2.4 GHz' : '';
}

function groupUplinkNetworks(networks) {
  const grouped = new Map();
  for (const ap of [...networks].sort((a, b) => b.signal - a.signal)) {
    // Keep security, DFS and availability choices distinct. A hidden name
    // cannot identify a network, so retain each hidden BSSID separately.
    const key = JSON.stringify([ap.ssid ? (ap.ssid_hex || ap.ssid) : ap.bssid, uplinkBand(ap),
      ap.security, !!ap.dfs, !!ap.selectable, ap.reason || '']);
    if (!grouped.has(key)) grouped.set(key, {...ap});
  }
  return [...grouped.values()];
}

function uplinkState(status) {
  if (!status) return 'Checking connection';
  if (status.state === 'blocked' && status.error) return status.error;
  if (!status.enabled) return 'Disconnected';
  if (status.state === 'recovery-failed' || status.error) {
    return 'Connection needs attention. Retry or disconnect';
  }
  if (status.connected || status.state === 'connected' || status.state === 'waiting-address') {
    if (!status.ip) return 'Joined Wi-Fi, waiting for a network address';
    return status.gateway ? 'Connected, sharing internet' : 'Connected, not the current internet connection';
  }
  return status.state === 'idle' ? 'Not connected' : 'Connecting';
}

function uplinkError(error) {
  const message = String(error.message || error);
  if (/fresh list|scan again/i.test(message)) return 'This list is out of date. Scan again and choose the network.';
  if (/Password must/i.test(message)) return 'Check the Wi-Fi password and try again. It may be missing, too short or too long.';
  if (/double quotes/i.test(message)) return 'This radio cannot use a WPA3 password containing double quotes.';
  if (/working mesh radio/i.test(message)) return 'Another mesh radio must stay connected. Restore that connection before trying again.';
  if (/EUD mode|shared AP|EUD AP/i.test(message)) return 'This radio cannot connect while keeping the current phone hotspot. Choose another radio or use a cable.';
  if (/one of this mesh's own hotspots/i.test(message)) return message;
  return 'The radio could not finish: ' + message + '. Please try again.';
}

function uplinkCurrent() {
  if (!uplinkStatus) return null;
  const iface = document.getElementById('uplink-wifi-radio').value;
  const status = uplinkStatus.builtin;
  return status && status.enabled && status.ssid && status.iface === iface ? status : null;
}

function renderUplinkCurrent() {
  const current = uplinkCurrent();
  const box = document.getElementById('uplink-wifi-current');
  box.hidden = !current;
  box.replaceChildren();
  if (!current) return;
  const name = document.createElement('strong');
  name.className = 'uplink-name';
  name.textContent = current.ssid;
  const state = document.createElement('span');
  state.className = 'uplink-detail';
  state.textContent = uplinkState(current) + (uplinkBand(current) ? ' · ' + uplinkBand(current) : '');
  box.append(name, state);
  const details = document.createElement('span');
  details.className = 'uplink-detail';
  const security = {open: 'Open network', wpa2: '🔒 WPA2', sae: '🔒 WPA3'}[current.security] || '';
  const observed = uplinkNetworks.find(ap => ap.bssid === current.bssid);
  details.textContent = security + (observed ? ` · ${observed.signal} dBm` : '') +
    (current.freq >= 5260 && current.freq <= 5720 ? ' · DFS: radar channel' : '');
  box.append(details);
  if (uplinkSavedNetworks.some(ap => ap.ssid === current.ssid && ap.security === current.security)) {
    const saved = document.createElement('span');
    saved.innerHTML = uplinkSavedMarkup();
    box.append(saved);
  }
  if (current.ap_blocked || current.state === 'ap-channel-unavailable' || current.state === 'ap-starting') {
    const note = document.createElement('span');
    note.className = 'uplink-detail';
    note.textContent = current.state === 'ap-channel-unavailable'
      ? 'Phone hotspot off: this channel is unavailable here. Choose another network, or use a cable or another node.'
      : current.state === 'ap-starting' ? 'Starting the phone hotspot on the new channel.'
      : current.ap_blocked === 'dfs-upstream'
      ? 'Phone hotspot off: this radio cannot share this radar channel. Use a cable, another node, or a network without DFS.'
      : 'Phone hotspot could not start. Use a cable or another node. Reconnect Wi-Fi to retry.';
    box.append(note);
  }
}

async function loadWifiUplink() {
  try {
    const r = await manageFetch(U('/api/uplink/wifi'));
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    uplinkStatus = d;
    const radio = document.getElementById('uplink-wifi-radio');
    if (!uplinkInitialized) {
      radio.replaceChildren();
      for (const choice of d.builtin.choices || []) {
        radio.add(new Option(`Built-in ${choice.band} GHz`, choice.iface));
      }
      radio.value = d.builtin.choices?.some(choice => choice.iface === d.builtin.iface)
        ? d.builtin.iface : (d.builtin.choices?.[0]?.iface || '');
      const savedIface = d.builtin.scan?.iface;
      if (d.builtin.choices?.some(choice => choice.iface === savedIface)) {
        radio.value = savedIface;
      }
      uplinkInitialized = true;
      updateUplinkChoice();
    }
    document.getElementById('uplink-wifi-status').textContent =
      d.builtin.choices?.length === 0 ? 'No built-in radio is available for uplink.'
      : uplinkState(d.builtin) + '.';
    restoreUplinkScan();
    updateSavedNetworks(d.saved_networks);
    renderUplinkCurrent();
    updateUplinkControls();
  } catch (e) {
    document.getElementById('uplink-wifi-status').textContent = uplinkError(e);
  }
}

function updateUplinkChoice(restore=true) {
  uplinkNetworks = [];
  uplinkSelected = -1;
  uplinkDifferentPassword = false;
  uplinkScanIface = '';
  uplinkScannedAt = 0;
  uplinkScanId = null;
  document.getElementById('uplink-wifi-password').value = '';
  document.getElementById('uplink-wifi-results').replaceChildren();
  document.getElementById('uplink-wifi-feedback').textContent = 'Scan to find nearby networks.';
  document.getElementById('uplink-wifi-warning').textContent = '';
  if (restore) restoreUplinkScan();
  renderUplinkCurrent();
  updateUplinkControls();
}

function uplinkFresh() {
  const age = Date.now() - uplinkScannedAt;
  return uplinkScanIface === document.getElementById('uplink-wifi-radio').value && age >= 0 && age <= 300000;
}

function updateUplinkControls() {
  const ap = uplinkNetworks[uplinkSelected];
  const useSaved = !!ap?.saved && ap.security !== 'open' && !uplinkDifferentPassword;
  document.getElementById('uplink-wifi-intro').hidden =
    !!uplinkCurrent() || !!uplinkNetworks.length;
  document.getElementById('uplink-wifi-radio').disabled = uplinkBusy || (uplinkStatus?.builtin?.choices?.length ?? 2) < 2;
  document.getElementById('btn-uplink-wifi-scan').disabled = uplinkBusy || !document.getElementById('uplink-wifi-radio').value;
  document.getElementById('btn-uplink-wifi-scan').textContent = uplinkBusy ? 'PLEASE WAIT' : uplinkScanIface ? 'RESCAN' : 'SCAN';
  document.getElementById('uplink-wifi-connect').hidden = !ap;
  document.getElementById('uplink-wifi-password-row').hidden = !ap || ap.security === 'open' || useSaved;
  document.getElementById('uplink-wifi-password').disabled = uplinkBusy || !ap || ap.security === 'open' || useSaved;
  document.getElementById('uplink-wifi-saved-password').hidden = !useSaved;
  document.getElementById('btn-uplink-wifi-password').disabled = uplinkBusy;
  for (const button of document.querySelectorAll('[data-action="forget-uplink"]')) button.disabled = uplinkBusy;
  document.getElementById('btn-uplink-wifi-apply').disabled = uplinkBusy || !ap || !uplinkFresh();
  document.getElementById('btn-uplink-wifi-disconnect').disabled = uplinkBusy || !uplinkCurrent();
  document.getElementById('uplink-wifi-selected').textContent = ap ? 'Selected: ' + ap.ssid : '';
  if (uplinkScanIface && !uplinkFresh()) {
    document.getElementById('uplink-wifi-feedback').textContent = 'This list is out of date. Scan again before connecting.';
  }
  document.getElementById('uplink-wifi-age').textContent = uplinkScanIface
    ? `Last scan: ${Math.max(0, Math.floor((Date.now() - uplinkScannedAt)
      / 1000))} seconds ago.` : '';
  for (const row of document.querySelectorAll('[data-action="select-uplink"]')) {
    const index = Number(row.dataset.index);
    const network = uplinkNetworks[index];
    const current = uplinkCurrent();
    // The connected group is already shown at the top, including after a
    // stronger BSSID appears. Leave other bands/security/DFS choices visible.
    row.hidden = !!(current && (current.connected || current.state === 'connected') &&
      current.ssid === network?.ssid && current.security === network.security &&
      uplinkBand(current) === uplinkBand(network) &&
      (current.freq >= 5260 && current.freq <= 5720) === !!network.dfs);
    row.disabled = uplinkBusy || !uplinkNetworks[index]?.selectable;
    row.setAttribute('aria-pressed', String(index === uplinkSelected));
  }
}

function uplinkNetworkMarkup(ap, index) {
  const strength = ap.signal >= -55 ? 4 : ap.signal >= -67 ? 3 : ap.signal >= -75 ? 2 : 1;
  const signal = ['Weak', 'Fair', 'Good', 'Strong'][strength - 1];
  const security = {open: 'Open network', wpa2: 'WPA2', sae: 'WPA3', unsupported: 'Unsupported security'}[ap.security] || 'Unsupported security';
  const dfs = ap.dfs ? (document.getElementById('uplink-wifi-radio').value === 'wlan1'
    ? 'DFS: radar channel; the phone hotspot may pause or stay off'
    : 'DFS: the router may change channel for radar') : '';
  const bars = [1, 2, 3, 4].map(n => `<i class="${n <= strength ? 'lit' : ''}"></i>`).join('');
  return `<button type="button" class="uplink-network" data-action="select-uplink" data-index="${index}"
    aria-pressed="false" ${ap.selectable ? '' : 'disabled'}>
    <span class="uplink-name">${esc(ap.ssid || 'Hidden network')}</span>
    <span class="uplink-signal" aria-label="${signal} signal, ${ap.signal} dBm"><span class="uplink-bars" aria-hidden="true">${bars}</span><span class="uplink-dbm">${ap.signal} dBm</span></span>
    <span class="uplink-detail">${ap.security !== 'open' ? '<span aria-hidden="true">&#128274;</span> ' : ''}${security} · ${uplinkBand(ap)} · Channel ${ap.channel}${ap.saved && ap.security !== 'open' ? ' · ' + uplinkSavedMarkup() : ''}</span>
    ${dfs ? `<span class="uplink-detail">${dfs}</span>` : ''}
    ${!ap.selectable ? `<span class="uplink-detail">${esc(ap.reason || 'This network cannot be selected')}</span>` : ''}
    </button>`;
}

function selectUplinkNetwork(index) {
  if (uplinkBusy || !uplinkNetworks[index]?.selectable) return;
  uplinkSelected = index;
  uplinkDifferentPassword = false;
  document.getElementById('uplink-wifi-password').value = '';
  updateUplinkControls();
  document.getElementById('uplink-wifi-connect').scrollIntoView({block: 'nearest', behavior: 'smooth'});
}

async function scanWifiUplink() {
  if (uplinkBusy) return;
  const iface = document.getElementById('uplink-wifi-radio').value;
  uplinkBusy = true;
  updateUplinkControls();
  const feedback = document.getElementById('uplink-wifi-feedback');
  feedback.textContent = 'Checking this radio before scanning...';
  try {
    const check = await manageFetch(U('/api/uplink/wifi/scan-plan'), {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({iface})
    });
    const plan = await check.json();
    if (!check.ok || !plan.ok) {
      throw new Error(plan.error || `HTTP ${check.status}`);
    }
    if (plan.interrupts_ap && !confirm('Phones on this hotspot will '
      + 'disconnect for about 15 seconds. Results will appear after '
      + 'reconnecting. Scan now?')) {
      feedback.textContent = 'Scan cancelled.';
      return;
    }
    const previousId = uplinkStatus?.builtin?.scan?.at;
    updateUplinkChoice(false);
    uplinkScanId = previousId;
    feedback.textContent = plan.interrupts_ap
      ? 'Scanning. Reconnect to this hotspot to see the saved results.'
      : 'Scanning for nearby networks...';
    const recovered = new Promise(resolve => { uplinkScanResolve = resolve; });
    const response = (async () => {
      const r = await manageFetch(U('/api/uplink/wifi/scan'), {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({iface, allow_ap_stop: !!plan.interrupts_ap})
      });
      const d = await r.json();
      if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
      return d;
    })();
    // A hotspot drop can leave fetch waiting after the scan has completed.
    // The normal status poll can complete the UI from the saved observation.
    const d = await Promise.race([response, recovered]);
    if (document.getElementById('uplink-wifi-radio').value !== iface) return;
    showUplinkScan(d, iface);
  } catch (e) { feedback.textContent = uplinkError(e); }
  finally {
    uplinkScanResolve = null;
    uplinkBusy = false;
    await loadWifiUplink();
    updateUplinkControls();
  }
}

function showUplinkScan(scan, iface) {
  uplinkNetworks = groupUplinkNetworks(scan.networks || []);
  uplinkScanIface = iface;
  uplinkScanDevice = scan.iface;
  uplinkScanId = scan.at;
  uplinkScannedAt = Date.now() - (scan.age_seconds || 0) * 1000;
  uplinkSelected = -1;
  uplinkDifferentPassword = false;
  document.getElementById('uplink-wifi-password').value = '';
  document.getElementById('uplink-wifi-results').innerHTML =
    uplinkNetworks.map(uplinkNetworkMarkup).join('');
  document.getElementById('uplink-wifi-warning').textContent =
    scan.warning || '';
  document.getElementById('uplink-wifi-feedback').textContent =
    uplinkNetworks.length
    ? `${uplinkNetworks.length} networks. Tap one to connect.`
    : 'No networks found. Move closer to the router and scan again.';
  renderUplinkCurrent();
}

function restoreUplinkScan() {
  const scan = uplinkStatus?.builtin?.scan;
  const iface = document.getElementById('uplink-wifi-radio').value;
  if (!scan || scan.iface !== iface) return;
  if (uplinkBusy) {
    if (uplinkScanResolve && scan.at !== uplinkScanId) uplinkScanResolve(scan);
    return;
  }
  if (scan.at !== uplinkScanId) showUplinkScan(scan, iface);
  document.getElementById('uplink-wifi-warning').textContent =
    scan.warning || '';
}

async function applyWifiUplink(enabled=true) {
  if (uplinkBusy) return;
  const iface = document.getElementById('uplink-wifi-radio').value;
  const ap = uplinkNetworks[uplinkSelected];
  if (enabled && (!ap || !ap.selectable || !uplinkFresh())) {
    document.getElementById('uplink-wifi-feedback').textContent = 'Scan this radio and choose a nearby network first.';
    updateUplinkControls(); return;
  }
  const request = {iface, enabled};
  if (enabled) Object.assign(request, {
    ssid: ap.ssid, bssid: ap.bssid, freq: ap.freq, security: ap.security,
    scan_iface: uplinkScanDevice,
    use_saved: !!ap.saved && ap.security !== 'open' && !uplinkDifferentPassword,
    password: ap.security === 'open' || (ap.saved && !uplinkDifferentPassword)
      ? '' : document.getElementById('uplink-wifi-password').value
  });
  uplinkBusy = true;
  updateUplinkControls();
  try {
    const r = await manageFetch(U('/api/uplink/wifi'), {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(request)
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
    document.getElementById('uplink-wifi-password').value = '';
    uplinkSelected = -1;
    document.getElementById('uplink-wifi-feedback').textContent = enabled
      ? 'Connecting. This node saves the password after joining the network.' : 'Wi-Fi disconnected.';
    await loadWifiUplink();
  } catch (e) { document.getElementById('uplink-wifi-feedback').textContent = uplinkError(e); }
  finally { uplinkBusy = false; updateUplinkControls(); }
}

// Measurements tab
function updatePairs() {
  if (!_topo) return;
  const wrap = document.getElementById('pairs-grid');
  wrap.innerHTML = '';
  // The source comes from local state, independent of peer identity claims.
  const src = {ip: _topo.my_ip, hostname: _topo.my_hostname};
  if (!src.ip) return;
  for (const dst of _topo.nodes) {
    if (dst.ip === src.ip) continue;
    const key = `local-${dst.id}`;
    wrap.innerHTML += `
      <div class="pair-row">
        <input type="checkbox" id="pair-${esc(key)}" value="${esc(key)}" data-src="${esc(src.ip)}" data-dst="${esc(dst.ip)}" data-src-name="${esc(src.hostname)}" data-dst-name="${esc(dst.hostname)}">
        <label for="pair-${esc(key)}" style="flex:none;font-size:12px">${esc(src.hostname)} → ${esc(dst.hostname)}</label>
      </div>`;
  }
}

async function startMeasurement() {
  const label = document.getElementById('session-label').value.trim();
  if (!label) {
    showMsg('Enter a session label', 'err');
    setProgress('Missing session label.', 'err');
    return;
  }

  const pairs = [];
  document.querySelectorAll('#pairs-grid input:checked').forEach(el => {
    pairs.push({
      src_ip: el.dataset.src, dst_ip: el.dataset.dst,
      src_name: el.dataset.srcName || el.dataset.src,
      dst_name: el.dataset.dstName || el.dataset.dst,
    });
  });
  if (!pairs.length) {
    showMsg('Select at least one test pair', 'err');
    setProgress('No source → destination pair selected.', 'err');
    return;
  }

  const tests = [];
  document.querySelectorAll('#tests-grid input:checked').forEach(el => tests.push(el.value));
  if (!tests.length) {
    showMsg('Select at least one test type', 'err');
    setProgress('No test type selected.', 'err');
    return;
  }

  const duration   = parseInt(document.getElementById('duration').value) || 30;
  const udpBitrate = document.getElementById('udp-bitrate').value || '4M';

  clearTimeout(_pollTimer);
  setRunButton(true, 'STARTING...');
  setProgress('Starting measurement session...', 'running');
  showMsg('Starting measurement session...', 'info');

  try {
    const r = await manageFetch(U('/api/measure/start'), {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({label, pairs, tests, duration, udp_bitrate: udpBitrate})
    });
    const d = await r.json();
    if (!r.ok || !d.ok) {
      throw new Error(d.error || `HTTP ${r.status}`);
    }
    setRunButton(true, 'RUNNING...');
    setProgress('Running... waiting for first status update.', 'running');
    showMsg('Measurement is running...', 'info');
    pollStatus();
  } catch (e) {
    setRunButton(false);
    setProgress('Start failed: ' + e.message, 'err');
    showMsg('Start failed: ' + e.message, 'err');
  }
}

async function pollStatus() {
  try {
    const r = await manageFetch(U('/api/measure/status'));
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);

    if (d.running) {
      setRunButton(true, 'RUNNING...');
      setProgress(d.progress || 'Running...', 'running');
      renderMeasureStats(d);
      _pollTimer = setTimeout(pollStatus, 2000);
    } else if (d.error) {
      setRunButton(false);
      setProgress('Error: ' + d.error, 'err');
      renderMeasureStats(d);
      showMsg('Error: ' + d.error, 'err');
    } else {
      setRunButton(false);
      setProgress(d.progress || 'Measurement complete: results saved.', 'done');
      renderMeasureStats(d);
      showMsg('Measurement complete: results saved.', 'ok');
      loadSessions();
    }
  } catch (e) {
    setRunButton(false);
    setProgress('Status failed: ' + e.message, 'err');
    showMsg('Status failed: ' + e.message, 'err');
  }
}

// ── Sessions tab ──
async function loadSessions() {
  const r = await manageFetch(U('/api/sessions'));
  const d = await r.json();
  const list = document.getElementById('sessions-list');
  if (!d.length) {
    list.innerHTML = '<div style="padding:16px;color:var(--muted);font-size:var(--small-text)">No sessions recorded yet.</div>';
    return;
  }
  list.innerHTML = d.map(s => `
    <div class="session-row">
      <div class="session-label">${esc(s.label)}</div>
      <div class="session-count">${esc(s.tests)} test${s.tests !== 1 ? 's' : ''} · ${esc(s.summary?.ok || 0)} ok / ${esc(s.summary?.fail || 0)} fail</div>
      <div class="session-actions">
        <a href="${esc(U(`/api/sessions/${encodeURIComponent(s.label)}/csv`))}" download="${esc(s.label)}.csv"
           class="btn" style="text-decoration:none;font-size:var(--small-text)">CSV</a>
        <a href="${esc(U(`/api/sessions/${encodeURIComponent(s.label)}`))}" target="_blank"
           class="btn" style="text-decoration:none;font-size:var(--small-text)">JSON</a>
        <button class="btn btn-red" style="font-size:var(--small-text)" data-label="${esc(encodeURIComponent(s.label))}" data-action="delete-session">DELETE</button>
      </div>
      <div class="session-summary">
        ${sessionMetric('TCP', s.summary?.tcp_mbps, ' Mbps')}
        ${sessionMetric('UDP', s.summary?.udp_mbps, ' Mbps')}
        ${sessionMetric('RTT', s.summary?.rtt_avg, ' ms')}
        ${sessionMetric('JITTER', s.summary?.jitter_ms, ' ms')}
        ${sessionMetric('LOSS', s.summary?.loss_pct, '%')}
      </div>
    </div>`).join('');
}

function sessionMetric(label, stats, unit) {
  const txt = metricText(stats, unit);
  return `<div class="metric-mini">${label}<strong>${esc(txt || '-')}</strong></div>`;
}

async function deleteSession(encodedLabel) {
  const label = decodeURIComponent(encodedLabel);
  if (!confirm(`Delete measurement session "${label}"? This cannot be undone.`)) return;
  try {
    const r = await manageFetch(U(`/api/sessions/${encodedLabel}`), {method: 'DELETE'});
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || `HTTP ${r.status}`);
    showMsg(`Deleted session ${label}`, 'ok');
    await loadSessions();
  } catch (e) {
    showMsg('Delete failed: ' + e.message, 'err');
  }
}

// One listener also covers controls rebuilt by innerHTML after each poll.
function handleManageClick(event) {
  const control = event.target.closest('[data-action]');
  if (!control || control.disabled) return;
  const data = control.dataset;
  switch (data.action) {
    case 'hide-overlay': hideOverlay(); break;
    case 'theme': toggleTheme(); break;
    case 'overview':
      window.location.href = '/?theme=' + encodeURIComponent(
        document.documentElement.dataset.theme || 'light');
      break;
    case 'logout': window.location.href = '/manage/logout'; break;
    case 'show-tab': showTab(data.tab); break;
    case 'toggle-interface':
      toggleIface(data.ip, data.id, data.iface, data.state); break;
    case 'toggle-all': toggleAll(data.iface, data.state); break;
    case 'set-power': setTxPower(data.ip, data.id, data.iface); break;
    case 'apply-halow': applyHalow(); break;
    case 'apply-2g': apply2G(); break;
    case 'apply-5g': apply5G(); break;
    case 'start-measurement': startMeasurement(); break;
    case 'delete-session': deleteSession(data.label); break;
    case 'apply-uplink': applyWifiUplink(); break;
    case 'scan-uplink': scanWifiUplink(); break;
    case 'select-uplink': selectUplinkNetwork(Number(data.index)); break;
    case 'different-uplink-password': useDifferentUplinkPassword(); break;
    case 'forget-uplink': forgetUplinkNetwork(Number(data.index)); break;
    case 'disconnect-uplink': applyWifiUplink(false); break;
    case 'voice-step': voiceStepChannel(Number(data.step)); break;
    case 'voice-codec': voiceSetCodec(data.codec); break;
    case 'stage-config': stageChanges(); break;
    case 'apply-config': applyChanges(false); break;
    case 'force-config': applyChanges(true); break;
    case 'show-force': showForceModal(); break;
    case 'close-force': closeForceModal(); break;
    case 'cancel-config': cancelPending(); break;
  }
}
document.addEventListener('click', handleManageClick);

window.addEventListener('load', async () => {
  updateChromeCompact();
  showTab(getInitialTab());
  await fetchTopo();
  buildIfaceControl();
  buildHalowConfig();
  const halowBw = document.getElementById('halow-bw');
  if (halowBw) halowBw.addEventListener('change', onHalowBandwidthChange);
  document.getElementById('uplink-wifi-radio').addEventListener('change', updateUplinkChoice);
  setInterval(() => { if (document.getElementById('tab-uplink').style.display !== 'none') loadWifiUplink(); }, 5000);
  startAutoRefresh();
});

window.addEventListener('hashchange', () => showTab(getInitialTab(), false));
window.addEventListener('scroll', updateChromeCompact, {passive: true});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) autoRefreshUi();
});
"""


# NODE CONFIG tab

# Keep CSS/JS in plain strings substituted into the dashboard f-string
# so their braces need no escaping.

VOICE_TAB_CSS = r"""
.voice-wrap { padding: 20px 24px; display: grid; gap: 18px;
              grid-template-columns: repeat(auto-fit, minmax(min(300px, 100%), 1fr)); }
.voice-card { border: 1px solid var(--border); background: var(--card); }
.voice-card-hdr { font-size: 11px; color: var(--muted); font-weight: 800;
                  padding: 10px 14px; border-bottom: 1px solid var(--border); }
.voice-card-body { padding: 14px; }
.voice-row { display: flex; justify-content: space-between; align-items: center;
             padding: 7px 0; border-bottom: 1px solid var(--border); font-size: 12px; }
.voice-row:last-child { border-bottom: 0; }
.voice-row .k { color: var(--muted); }
.voice-row .v { font-variant-numeric: tabular-nums; }
.voice-dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%;
             background: #bbb; margin-right: 7px; vertical-align: middle; }
.voice-dot.on { background: #2e9e4f; }
.voice-dot.tx { background: #d9534f; }
.voice-dot.rx { background: #2f6fd0; }
.voice-dot.warn { background: #d1a000; }
/* The PTT indicator is the one thing an operator glances at, so it is big. */
.voice-ptt { text-align: center; padding: 18px 0; }
.voice-ptt-ring { width: 84px; height: 84px; border-radius: 50%; margin: 0 auto 10px;
                  border: 3px solid var(--border); background: var(--panel); }
.voice-ptt.tx .voice-ptt-ring { border-color: #d9534f; background: #ffe9e8; }
.voice-ptt.rx .voice-ptt-ring { border-color: #2f6fd0; background: #e8f0ff; }
.voice-ptt-label { font-size: 12px; font-weight: 800; letter-spacing: .04em; }
.voice-peers { width: 100%; border-collapse: collapse; font-size: 12px; }
.voice-peers th { text-align: left; color: var(--muted); font-weight: 700;
                  padding: 5px 6px; border-bottom: 1px solid var(--border); }
.voice-peers td { padding: 5px 6px; border-bottom: 1px solid var(--border);
                  font-variant-numeric: tabular-nums; }
.voice-empty { color: var(--muted); font-size: 12px; padding: 10px 0; }
.voice-note { color: var(--muted); font-size: 11px; padding: 10px 14px 0; }

/* Talk group picker. Sized for a thumb on an EUD screen rather than a mouse:
   44px is the smallest touch target that is reliably hittable, and the stepper
   mirrors the rotary switch on the enclosure so both feel like the same
   control. */
.voice-tg-stepper { display: flex; align-items: center; justify-content: center;
                    gap: 18px; padding: 4px 0 14px; }
.voice-tg-step { width: 56px; height: 56px; font-size: 28px; line-height: 1;
                 border: 1px solid var(--border); background: var(--card);
                 color: inherit; border-radius: 8px; cursor: pointer;
                 -webkit-tap-highlight-color: transparent; }
.voice-tg-step:active { background: var(--border); }
.voice-tg-step:disabled { opacity: .35; cursor: default; }
.voice-tg-now { min-width: 84px; text-align: center; font-size: 40px;
                font-weight: 800; font-variant-numeric: tabular-nums; }
.voice-tg-grid { display: grid; gap: 6px;
                 grid-template-columns: repeat(8, minmax(0, 1fr)); }
.voice-tg-cell { height: 44px; font-size: 13px; border: 1px solid var(--border);
                 background: var(--card); color: inherit; border-radius: 6px;
                 cursor: pointer; -webkit-tap-highlight-color: transparent;
                 font-variant-numeric: tabular-nums; }
.voice-tg-cell.sel { background: #2f6f3e; border-color: #2f6f3e; color: #fff;
                     font-weight: 800; }
.voice-tg-msg { min-height: 16px; font-size: 11px; padding-top: 10px;
                color: var(--muted); }
.voice-tg-msg.err { color: #b3261e; }
/* Codec picker. Two mutually exclusive options rather than a dropdown: this
   is a mesh-wide change with a real failure mode, so it should not be
   possible to change it by scrolling past it. */
.voice-codec-pick { display: grid; gap: 8px; grid-template-columns: 1fr 1fr; }
.voice-codec-opt { display: flex; flex-direction: column; gap: 3px; padding: 12px 10px;
                   border: 1px solid var(--border); border-radius: 8px;
                   background: var(--card); color: inherit; cursor: pointer;
                   text-align: left; font: inherit; }
.voice-codec-opt b { font-size: 15px; }
.voice-codec-opt span { font-size: 11px; color: var(--muted); }
.voice-codec-opt.sel { background: #2f6f3e; border-color: #2f6f3e; color: #fff; }
.voice-codec-opt.sel span { color: rgba(255,255,255,.85); }
.voice-codec-opt:disabled { opacity: .45; cursor: default; }
@media (max-width: 760px), (orientation:landscape) and (max-height:600px) {
  .voice-wrap { padding: 0; }
  .voice-card { min-width: 0; }
  .voice-row { gap: 10px; flex-wrap: wrap; font-size: 16px; }
  .voice-row .v { min-width: 0; overflow-wrap: anywhere; }
  .voice-card-hdr { font-size: 16px; }
  .voice-note, .voice-empty, .voice-tg-msg, .voice-codec-opt span,
  .voice-peers th, .voice-peers td { font-size: 14px; }
  .voice-tg-cell { min-width: 44px; font-size: 16px; }
  .voice-tg-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
}
@media (max-width: 420px) {
  .voice-tg-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
}
.voice-audio-slider { padding: 6px 14px 12px; }
.voice-audio-slider input[type=range] { width: 100%; accent-color: #2e9e4f;
  cursor: pointer; }
.voice-agc-toggle { cursor: pointer; display: inline-flex; align-items: center;
  gap: 6px; }
.voice-agc-toggle input { accent-color: #2e9e4f; width: 16px; height: 16px; }
"""

VOICE_TAB_HTML = r"""
<div class="voice-wrap">

  <div class="voice-card">
    <div class="voice-card-hdr">PUSH TO TALK</div>
    <div class="voice-card-body">
      <div class="voice-ptt" id="voice-ptt">
        <div class="voice-ptt-ring"></div>
        <div class="voice-ptt-label" id="voice-ptt-label">--</div>
      </div>
      <div class="voice-row"><span class="k">PTT hardware</span>
        <span class="v"><span class="voice-dot" id="voice-hw-dot"></span><span id="voice-hw">--</span></span></div>
      <div class="voice-row"><span class="k">PTT mode</span><span class="v" id="voice-mode">--</span></div>
      <div class="voice-row"><span class="k">Device</span><span class="v" id="voice-dev">--</span></div>
    </div>
  </div>

  <div class="voice-card">
    <div class="voice-card-hdr">SERVICE</div>
    <div class="voice-card-body">
      <div class="voice-row"><span class="k">State</span>
        <span class="v"><span class="voice-dot" id="voice-svc-dot"></span><span id="voice-svc">--</span></span></div>
      <div class="voice-row"><span class="k">Uptime</span><span class="v" id="voice-uptime">--</span></div>
      <div class="voice-row"><span class="k">Talk group</span><span class="v" id="voice-group">--</span></div>
      <div class="voice-row"><span class="k">Interface</span><span class="v" id="voice-iface">--</span></div>
      <div class="voice-row"><span class="k">Codec</span><span class="v" id="voice-codec">--</span></div>
      <div class="voice-row"><span class="k">QoS</span><span class="v" id="voice-qos">--</span></div>
      <div class="voice-row"><span class="k">Talkers heard</span><span class="v" id="voice-talkers">--</span></div>
      <div class="voice-row"><span class="k">Audio path</span>
        <span class="v"><span class="voice-dot" id="voice-health-dot"></span><span id="voice-health">--</span></span></div>
    </div>
  </div>

  <div class="voice-card">
    <div class="voice-card-hdr">TALK GROUP</div>
    <div class="voice-card-body">
      <div class="voice-tg-stepper">
        <button class="voice-tg-step" id="voice-tg-down" data-action="voice-step" data-step="-1"
                aria-label="Previous talk group">&minus;</button>
        <div class="voice-tg-now"><span id="voice-tg-cur">--</span></div>
        <button class="voice-tg-step" id="voice-tg-up" data-action="voice-step" data-step="1"
                aria-label="Next talk group">+</button>
      </div>
      <div class="voice-tg-grid" id="voice-tg-grid"></div>
      <div class="voice-tg-msg" id="voice-tg-msg"></div>
    </div>
    <div class="voice-note">Select the talk group for this radio. Other radios
      keep their current group.</div>
  </div>

  <div class="voice-card">
    <div class="voice-card-hdr">CODEC</div>
    <div class="voice-card-body">
      <div class="voice-codec-pick">
        <button class="voice-codec-opt" id="voice-codec-lyra" data-action="voice-codec" data-codec="lyra">
          <b>Lyra</b><span>6 kbps neural: default</span></button>
        <button class="voice-codec-opt" id="voice-codec-opus" data-action="voice-codec" data-codec="opus">
          <b>Opus</b><span>32 kbps, stock elements</span></button>
      </div>
      <div class="voice-tg-msg" id="voice-codec-msg"></div>
    </div>
    <div class="voice-note">All radios must use the same codec to hear each
      other. The change waits for every node to acknowledge, then switches them
      together. If any node fails to acknowledge, the codec stays unchanged.
      Audio pauses briefly during the switch.</div>
  </div>

  <div class="voice-card">
    <div class="voice-card-hdr">TRAFFIC</div>
    <div class="voice-card-body">
      <div class="voice-row"><span class="k">Sent</span><span class="v" id="voice-tx">--</span></div>
      <div class="voice-row"><span class="k">Received</span><span class="v" id="voice-rx">--</span></div>
      <div class="voice-row"><span class="k">Lost</span><span class="v" id="voice-lost">--</span></div>
      <div class="voice-row"><span class="k">Late</span><span class="v" id="voice-late">--</span></div>
      <div class="voice-row"><span class="k">Duplicates</span><span class="v" id="voice-dup">--</span></div>
    </div>
    <div class="voice-note">Lost counts frames the jitter buffer never saw.
      Duplicates are expected when unicast redundancy is on: each is a
      multicast copy that also arrived by unicast.</div>
  </div>

  <div class="voice-card">
    <div class="voice-card-hdr">UNICAST REDUNDANCY</div>
    <div class="voice-card-body">
      <div class="voice-row"><span class="k">Enabled</span>
        <span class="v"><span class="voice-dot" id="voice-uni-dot"></span><span id="voice-uni">--</span></span></div>
      <div id="voice-peer-wrap"></div>
    </div>
    <div class="voice-note">Peers come from the Alfred node registry, so a node
      that has never transmitted still receives a unicast copy.</div>
  </div>

  <div class="voice-card">
    <div class="voice-card-hdr">GAIN</div>
    <div class="voice-card-body">
      <div class="voice-row"><span class="k">Receive gain</span>
        <span class="v"><span id="voice-rxgain-val">--</span> dB</span></div>
      <div class="voice-audio-slider">
        <input type="range" id="voice-rxgain" min="0" max="24" step="1" value="18">
      </div>
      <div class="voice-row"><span class="k">Playback level</span>
        <span class="v"><span id="voice-playpct-val">--</span>%</span></div>
      <div class="voice-audio-slider">
        <input type="range" id="voice-playpct" min="0" max="100" step="5" value="100">
      </div>
      <div class="voice-row"><span class="k">Auto gain control</span>
        <span class="v"><label class="voice-agc-toggle"><input type="checkbox" id="voice-agc">
          <span id="voice-agc-label">--</span></label></span></div>
      <div class="voice-tg-msg" id="voice-audio-msg"></div>
    </div>
    <div class="voice-note">Receive gain boosts each talker's audio before the
      mixer, with a limiter. 0 bypasses it. Playback level sets the hardware
      mixer on the audio card. AGC should be off when an external preamp is
      present. All are per-radio settings.</div>
  </div>

</div>
"""

# Voice polls local daemon state. Talk groups stay local and codecs are staged.
VOICE_TAB_JS = r"""
// ── VOICE tab ───────────────────────────────────────────────────────────────
// Read-only. Voice is carried by mesh-voice.py over multicast RTP with a
// headset PTT on the node itself; nothing here touches audio. Polls only while
// the tab is visible.
const VOICE_POLL_MS = 2000;
let voicePollTimer = null;

function startVoicePolling() {
  if (voicePollTimer) return;
  refreshVoice();
  voicePollTimer = setInterval(refreshVoice, VOICE_POLL_MS);
}
function stopVoicePolling() {
  clearInterval(voicePollTimer);
  voicePollTimer = null;
}

function vTxt(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = (val === undefined || val === null || val === '') ? '--' : val;
}
function vDot(id, cls) {
  const el = document.getElementById(id);
  if (el) el.className = 'voice-dot' + (cls ? ' ' + cls : '');
}
function vDuration(sec) {
  if (!sec && sec !== 0) return '--';
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? (h + 'h ' + m + 'm') : (m ? (m + 'm ' + s + 's') : (s + 's'));
}

const VOICE_TG_MAX = 32;
let voiceChannel  = null;   // last channel the daemon reported
let voiceTgBusy   = false;  // suppress polling clobber while a change is in flight

function voiceTgBuildGrid() {
  const g = document.getElementById('voice-tg-grid');
  if (!g || g.childElementCount) return;          // build once
  for (let n = 1; n <= VOICE_TG_MAX; n++) {
    const b = document.createElement('button');
    b.className = 'voice-tg-cell';
    b.textContent = n;
    b.dataset.ch = n;
    b.addEventListener('click', () => voiceSetChannel(n));
    g.appendChild(b);
  }
}

function voiceTgPaint(ch) {
  const cur = document.getElementById('voice-tg-cur');
  if (cur) cur.textContent = (ch === null || ch === undefined) ? '--' : ch;
  document.querySelectorAll('.voice-tg-cell').forEach(b => {
    b.classList.toggle('sel', Number(b.dataset.ch) === ch);
  });
  const dn = document.getElementById('voice-tg-down');
  const up = document.getElementById('voice-tg-up');
  if (dn) dn.disabled = voiceTgBusy || !ch || ch <= 1;
  if (up) up.disabled = voiceTgBusy || !ch || ch >= VOICE_TG_MAX;
}

function voiceTgMsg(text, isErr) {
  const m = document.getElementById('voice-tg-msg');
  if (!m) return;
  m.textContent = text || '';
  m.className = 'voice-tg-msg' + (isErr ? ' err' : '');
}

var voiceCodec = null, voiceCodecBusy = false;

function voiceCodecPaint(codec) {
  ['lyra', 'opus'].forEach(c => {
    const b = document.getElementById('voice-codec-' + c);
    if (!b) return;
    b.classList.toggle('sel', c === codec);
    b.disabled = voiceCodecBusy;
  });
}

function voiceCodecMsg(text, isErr) {
  const m = document.getElementById('voice-codec-msg');
  if (!m) return;
  m.textContent = text || '';
  m.className = 'voice-tg-msg' + (isErr ? ' err' : '');
}

// Confirm the mesh-wide codec change and disable the button while waiting
// for every node to acknowledge it.
async function voiceSetCodec(codec) {
  if (voiceCodecBusy || codec === voiceCodec) return;
  const other = codec === 'lyra' ? 'Opus' : 'Lyra';
  if (!confirm('Switch the whole mesh from ' + other + ' to ' +
               (codec === 'lyra' ? 'Lyra' : 'Opus') + '?\n\n' +
               'Every node restarts its voice pipeline. Audio drops for a few ' +
               'seconds. If any node does not ACK, the change is cancelled.')) return;
  voiceCodecBusy = true;
  voiceCodecPaint(voiceCodec);
  voiceCodecMsg('Staging across the mesh: waiting for every node to ACK…');
  try {
    const r = await manageFetch(U('/api/voice/codec'), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({codec})
    });
    const j = await r.json();
    if (j.ok) {
      voiceCodec = j.codec;
      // coordinate_radio_change() returns once every node has ACKed, but the
      // package activates on a common clock ~20 s later, so this is staged,
      // not done. The poll above repaints from the daemon when it lands.
      voiceCodecMsg('Staged on ' + ((j.acked || []).length || 'all') +
                    ' node(s): all switching to ' +
                    (j.codec === 'lyra' ? 'Lyra' : 'Opus') + ' together in ~20 s');
    } else {
      voiceCodecMsg(j.error || 'Change failed', true);
    }
  } catch (e) {
    voiceCodecMsg('Change failed: ' + e, true);
  } finally {
    voiceCodecBusy = false;
    voiceCodecPaint(voiceCodec);
  }
}

function voiceStepChannel(delta) {
  if (voiceChannel === null) return;
  const n = voiceChannel + delta;
  if (n >= 1 && n <= VOICE_TG_MAX) voiceSetChannel(n);
}

async function voiceSetChannel(ch) {
  if (voiceTgBusy || ch === voiceChannel) return;
  voiceTgBusy = true;
  // Paint the target immediately. The daemon takes a moment to retune and the
  // 2 s poll would otherwise snap the number back to the old group and make
  // the button feel broken.
  voiceChannel = ch;
  voiceTgPaint(ch);
  voiceTgMsg('Switching to talk group ' + ch + '…');
  try {
    const r = await manageFetch(U('/api/voice/channel'), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({channel: ch})
    });
    const j = await r.json();
    if (j.ok) {
      voiceTgMsg(j.reloaded ? ('Talk group ' + j.channel)
                            : ('Talk group ' + j.channel + ': saved; applies when voice starts'));
    } else {
      voiceTgMsg(j.error || 'Change failed', true);
      voiceChannel = null;            // force the next poll to resync
    }
  } catch (e) {
    voiceTgMsg('Change failed: ' + e, true);
    voiceChannel = null;
  } finally {
    voiceTgBusy = false;
    voiceTgPaint(voiceChannel);
  }
}

// Audio-path health, from the same two flows the daemon's watchdog judges:
// capture buffers reaching the valve and playback buffers reaching the sink.
// Both run continuously on a healthy node whether or not anyone is talking,
// which is exactly why the packet counters cannot be used for this.
function voicePaintHealth(d, running) {
  if (!running) { vDot('voice-health-dot', ''); vTxt('voice-health', '--'); return; }
  if (d.tx_blocked) {
    vDot('voice-health-dot', 'warn');
    vTxt('voice-health', 'TX blocked: ' + d.tx_blocked);
    return;
  }
  if (d.watchdog === false) { vDot('voice-health-dot', ''); vTxt('voice-health', 'Not watched'); return; }
  // The daemon owns the threshold (voice_watchdog_sec), so take it from the
  // state file rather than keeping a second copy here that can disagree.
  const lim = d.watchdog_sec || 15;
  const bad = [];
  // null means the flow has never run: no audio device, not a stall.
  if (d.capture_idle === null) bad.push('no capture');
  else if (d.capture_idle > lim) bad.push('capture stalled');
  if (d.playback_idle === null) bad.push('no playback');
  else if (d.playback_idle > lim) bad.push('playback stalled');
  if (d.igmp_joined === false) bad.push('not in multicast group');
  const stalls = d.stalls ? ' (' + d.stalls + ' stall' + (d.stalls === 1 ? '' : 's') + ')' : '';
  vDot('voice-health-dot', bad.length ? 'warn' : 'on');
  vTxt('voice-health', (bad.length ? bad.join(', ') : 'Flowing') + stalls);
}

let voiceFetchBusy = false;
async function refreshVoice() {
  if (voiceFetchBusy || document.hidden) return;
  voiceFetchBusy = true;
  let d;
  try {
    const r = await manageFetch(U('/api/voice'));
    if (!r.ok) return;
    d = await r.json();
  } catch (e) { return; }
  finally { voiceFetchBusy = false; }

  const running = d.service === 'running';
  // The daemon writes this on its way out when it has decided to rebuild the
  // whole stack, so the tab says why for the ten seconds systemd takes rather
  // than flipping to a bare "Stopped".
  const restarting = d.service === 'restarting';

  vDot('voice-svc-dot', running ? 'on' : (restarting ? 'warn' : ''));
  vTxt('voice-svc', running ? 'Running'
        : restarting ? 'Restarting (' + (d.fault || 'stalled') + ')'
        : (d.stale ? 'Stopped (stale state)' : 'Stopped'));
  vTxt('voice-uptime', running ? vDuration(d.uptime) : '--');
  vTxt('voice-group', d.group ? (d.group + ':' + d.port + '  (ch ' + d.channel + ')') : '--');

  voiceTgBuildGrid();
  // Do not let a poll land on top of a change the operator just made.
  if (!voiceTgBusy) {
    if (d.channel) voiceChannel = d.channel;
    voiceTgPaint(voiceChannel);
  }
  voicePaintHealth(d, running);
  vTxt('voice-iface', d.interface);
  // Show Lyra's adaptive frames/packet alongside bitrate to explain airtime
  // usage. Opus keeps a fixed frame size.
  vTxt('voice-codec', d.bitrate
        ? ((d.codec === 'lyra' ? 'Lyra ' : 'Opus ')
           + (d.bitrate / 1000).toFixed(d.codec === 'lyra' ? 1 : 0) + ' kbps / '
           + (d.frame_ms || 20) + ' ms'
           + (d.frames_per_packet ? ' (' + d.frames_per_packet + ' fr/pkt)' : ''))
        : '--');
  // The picker shows the configured codec: that is the mesh-wide setting the
  // buttons actually change. d.codec is what the daemon *built* with, and the
  // two differ when a missing lyra plugin or model dir forced a fallback. That
  // gap is worth shouting about rather than hiding: on a Lyra mesh a node that
  // fell back to Opus is deaf and mute, not merely lower quality.
  const cfgCodec = d.codec_configured || d.codec;
  if (!voiceCodecBusy && cfgCodec && cfgCodec !== voiceCodec) {
    voiceCodec = cfgCodec;
    voiceCodecPaint(voiceCodec);
  }
  if (!voiceCodecBusy) {
    if (d.codec_fallback) {
      voiceCodecMsg('Configured for ' + (cfgCodec === 'lyra' ? 'Lyra' : 'Opus') +
                    ' but running ' + (d.codec === 'lyra' ? 'Lyra' : 'Opus') +
                    ': plugin or model weights missing. This node cannot hear ' +
                    'the rest of the mesh.', true);
    } else if (document.getElementById('voice-codec-msg') &&
               document.getElementById('voice-codec-msg').classList.contains('err')) {
      voiceCodecMsg('');
    }
  }
  vTxt('voice-qos', (d.dscp !== undefined && d.dscp !== null)
        ? ('DSCP ' + d.dscp + (d.dscp === 46 ? ' (EF)' : '')) : '--');
  // Receive is conference style: one decode branch per talker, all mixed. This
  // is the count of streams currently being decoded, not a peer count.
  // SSRC is the sender's mesh address, so talkers can be named rather than
  // counted. The cap tracks the node registry, hence showing both.
  var tkNames = Array.isArray(d.talker_names) ? d.talker_names : [];
  vTxt('voice-talkers', (d.talkers === undefined || d.talkers === null) ? '--'
        : (tkNames.length ? tkNames.join(', ')
                          : (d.talkers + (d.talkers === 1 ? ' stream' : ' streams')))
          + (d.max_talkers ? '  (warm ' + d.talkers + '/' + d.max_talkers + ')' : ''));

  // PTT
  const ptt = document.getElementById('voice-ptt');
  let label = 'SERVICE OFF';
  let cls = '';
  if (running) {
    if (d.tx)       { label = 'TRANSMITTING'; cls = 'tx'; }
    else if (d.rx)  { label = 'RECEIVING';    cls = 'rx'; }
    else if (d.ptt_connected) label = 'IDLE';
    else            label = 'NO PTT HARDWARE';
  }
  if (ptt) ptt.className = 'voice-ptt' + (cls ? ' ' + cls : '');
  vTxt('voice-ptt-label', label);

  const hwOk = !!d.ptt_connected;
  vDot('voice-hw-dot', running ? (hwOk ? 'on' : 'warn') : '');
  vTxt('voice-hw', !running ? '--' : (hwOk ? 'Connected' : 'Not detected'));
  vTxt('voice-mode', d.ptt_mode);
  vTxt('voice-dev', d.ptt_device);

  vTxt('voice-tx', d.tx_packets);
  vTxt('voice-rx', d.rx_packets);
  vTxt('voice-lost', (d.rx_loss_pct !== undefined && d.rx_loss_pct !== null)
        ? (d.rx_lost + '  (' + d.rx_loss_pct + '%)') : d.rx_lost);
  vTxt('voice-late', d.rx_late);
  vTxt('voice-dup', d.rx_duplicates);

  vDot('voice-uni-dot', d.unicast ? 'on' : '');
  vTxt('voice-uni', d.unicast === undefined ? '--' : (d.unicast ? 'Yes' : 'No'));

  voiceAudioPaint(d);

  const wrap = document.getElementById('voice-peer-wrap');
  if (!wrap) return;
  const peers = d.peers || [];
  wrap.textContent = '';

  if (!d.unicast || !peers.length) {
    const msg = document.createElement('div');
    msg.className = 'voice-empty';
    msg.textContent = !d.unicast ? 'Multicast only.'
                                 : 'No active peers in the registry.';
    wrap.appendChild(msg);
    return;
  }

  // Hostnames arrive from other nodes over Alfred, so they are built as text
  // nodes rather than concatenated into innerHTML.
  const table = document.createElement('table');
  table.className = 'voice-peers';
  const head = table.insertRow();
  ['Node', 'Address'].forEach(t => {
    const th = document.createElement('th');
    th.textContent = t;
    head.appendChild(th);
  });
  peers.forEach(p => {
    const row = table.insertRow();
    row.insertCell().textContent = p.hostname || '(unknown)';
    row.insertCell().textContent = p.ip || '';
  });
  wrap.appendChild(table);
}

// ── AUDIO sliders ────────────────────────────────────────────────────────────
let voiceAudioBusy = false;
let voiceAudioLoaded = false;

function voiceAudioMsg(text, isErr) {
  const m = document.getElementById('voice-audio-msg');
  if (!m) return;
  m.textContent = text || '';
  m.className = 'voice-tg-msg' + (isErr ? ' err' : '');
}

function voiceAudioPaint(d) {
  const rxg = d.rx_gain_db;
  const pct = d.playback_level;
  const agc = d.agc;
  vTxt('voice-rxgain-val', rxg !== undefined && rxg !== null ? rxg : '--');
  vTxt('voice-playpct-val', pct !== undefined && pct !== null ? pct : '--');
  // AGC state from the daemon.
  const agcCb = document.getElementById('voice-agc');
  const agcLbl = document.getElementById('voice-agc-label');
  if (agcCb && agc !== undefined && agc !== null && !voiceAudioBusy) {
    agcCb.checked = agc;
    if (agcLbl) agcLbl.textContent = agc ? 'On' : 'Off';
  }
  // Seed the sliders from the daemon once, then let user drag freely.
  if (!voiceAudioLoaded && !voiceAudioBusy) {
    const rs = document.getElementById('voice-rxgain');
    const ps = document.getElementById('voice-playpct');
    if (rs && rxg !== undefined && rxg !== null) rs.value = rxg;
    if (ps && pct !== undefined && pct !== null) ps.value = pct;
    if (rxg !== undefined || pct !== undefined) voiceAudioLoaded = true;
  }
}

async function voiceAudioSet(rxGain, playPct, agc) {
  if (voiceAudioBusy) return;
  voiceAudioBusy = true;
  voiceAudioMsg('Applying…');
  try {
    const body = {};
    if (rxGain !== undefined) body.rx_gain_db = Number(rxGain);
    if (playPct !== undefined) body.playback_pct = Number(playPct);
    if (agc !== undefined) body.agc = Boolean(agc);
    const r = await manageFetch(U('/api/voice/audio'), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    const j = await r.json();
    if (j.ok) {
      voiceAudioMsg('Applied');
      setTimeout(() => voiceAudioMsg(''), 2000);
    } else {
      voiceAudioMsg(j.error || 'Failed', true);
    }
  } catch (e) {
    voiceAudioMsg('Failed: ' + e, true);
  } finally {
    voiceAudioBusy = false;
  }
}

(function initAudioSliders() {
  const rxs = document.getElementById('voice-rxgain');
  const pps = document.getElementById('voice-playpct');
  const agc = document.getElementById('voice-agc');
  if (rxs) {
    rxs.addEventListener('input', () => vTxt('voice-rxgain-val', rxs.value));
    rxs.addEventListener('change', () => voiceAudioSet(rxs.value, undefined, undefined));
  }
  if (pps) {
    pps.addEventListener('input', () => vTxt('voice-playpct-val', pps.value));
    pps.addEventListener('change', () => voiceAudioSet(undefined, pps.value, undefined));
  }
  if (agc) {
    agc.addEventListener('change', () => {
      const lbl = document.getElementById('voice-agc-label');
      if (lbl) lbl.textContent = agc.checked ? 'On' : 'Off';
      voiceAudioSet(undefined, undefined, agc.checked);
    });
  }
})();
"""

VOICE_STATE_FILE = '/run/mesh-voice.json'

# CM108B USB identifiers for the OpenVLM board.
_OPENVLM_VID = 0x0D8C
_OPENVLM_PID = 0x000C


def find_openvlm_card():
    """ALSA card index for the CM108B, via /proc/asound/card*/usbid."""
    want = "%04x:%04x" % (_OPENVLM_VID, _OPENVLM_PID)
    for path in sorted(glob.glob("/proc/asound/card*/usbid")):
        try:
            with open(path) as fh:
                if fh.read().strip().lower() != want:
                    continue
        except OSError:
            continue
        match = re.search(r"card(\d+)", path)
        if match:
            return int(match.group(1))
    return None


VOICE_CHANNEL_MAX = 32
# Lyra is the default. Opus uses stock elements and cannot interoperate
# with Lyra. See set_voice_codec_all().
VOICE_CODECS = ('lyra', 'opus')


def set_voice_channel(channel):
    """Set this radio's talk group without a mesh-wide change.

    Rewrite only voice_channel in mesh.conf, preserving other lines byte for
    byte, file mode and ownership. Atomic replacement protects mesh_key and
    admin_password from a truncated config after a crash.
    """
    try:
        channel = int(channel)
    except (TypeError, ValueError):
        return {'ok': False, 'error': 'Talk group must be a number'}
    if not 1 <= channel <= VOICE_CHANNEL_MAX:
        return {'ok': False, 'error': 'Talk group must be 1-%d' % VOICE_CHANNEL_MAX}

    try:
        with open(MESH_CONF_FILE) as f:
            lines = f.readlines()
    except OSError as e:
        return {'ok': False, 'error': 'Cannot read %s: %s' % (MESH_CONF_FILE, e)}

    out, replaced = [], False
    for line in lines:
        if line.split('=', 1)[0].strip() == 'voice_channel':
            if not replaced:
                out.append('voice_channel=%d\n' % channel)
                replaced = True
            continue          # Collapse duplicate keys.
        out.append(line)
    if not replaced:
        if out and not out[-1].endswith('\n'):
            out[-1] += '\n'
        out.append('voice_channel=%d\n' % channel)

    tmp = MESH_CONF_FILE + '.tmp'
    try:
        st = os.stat(MESH_CONF_FILE)
        with open(tmp, 'w') as f:
            f.writelines(out)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, stat.S_IMODE(st.st_mode))
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except PermissionError:
            pass              # Non-root: preserve mode, keep writer ownership.
        os.replace(tmp, MESH_CONF_FILE)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return {'ok': False, 'error': 'Cannot write %s: %s' % (MESH_CONF_FILE, e)}

    # SIGHUP retunes mesh-voice without interrupting audio. Reload failure
    # is nonfatal because the saved config takes effect at the next start.
    reloaded = False
    try:
        rc = subprocess.run(['systemctl', 'reload', 'mesh-voice'],
                            timeout=10, stderr=subprocess.DEVNULL)
        reloaded = rc.returncode == 0
    except Exception:
        pass

    return {'ok': True, 'channel': channel, 'reloaded': reloaded}


def set_voice_audio(req):
    """Set per-radio audio levels: voice_rx_gain_db, voice_playback_pct, AGC.

    Writes mesh.conf settings with the same atomic-replace pattern as
    set_voice_channel(), then reloads mesh-voice via SIGHUP. AGC is a raw
    ALSA mixer control set directly via amixer.
    """
    updates = {}
    if 'rx_gain_db' in req:
        try:
            val = float(req['rx_gain_db'])
        except (TypeError, ValueError):
            return {'ok': False, 'error': 'rx_gain_db must be a number'}
        if not 0.0 <= val <= 24.0:
            return {'ok': False, 'error': 'rx_gain_db must be 0-24'}
        updates['voice_rx_gain_db'] = '%.1f' % val

    if 'playback_pct' in req:
        try:
            val = int(req['playback_pct'])
        except (TypeError, ValueError):
            return {'ok': False, 'error': 'playback_pct must be a number'}
        if not 0 <= val <= 100:
            return {'ok': False, 'error': 'playback_pct must be 0-100'}
        updates['voice_playback_pct'] = str(val)

    # AGC is a hardware mixer toggle, not a mesh.conf key.
    agc_req = req.get('agc')
    agc_set = False
    if agc_req is not None:
        card = find_openvlm_card()
        if card is not None:
            state = 'on' if agc_req else 'off'
            try:
                subprocess.run(['amixer', '-q', '-c', str(card), 'sset',
                                'Auto Gain Control', state],
                               capture_output=True, timeout=3)
                agc_set = True
            except (OSError, subprocess.SubprocessError):
                pass

    if not updates and not agc_set:
        return {'ok': False, 'error': 'No settings provided'}

    reloaded = False
    if updates:
        try:
            with open(MESH_CONF_FILE) as f:
                lines = f.readlines()
        except OSError as e:
            return {'ok': False, 'error': 'Cannot read %s: %s' % (MESH_CONF_FILE, e)}

        replaced = set()
        out = []
        for line in lines:
            key = line.split('=', 1)[0].strip()
            if key in updates:
                if key not in replaced:
                    out.append('%s=%s\n' % (key, updates[key]))
                    replaced.add(key)
                continue
            out.append(line)

        for key, val in updates.items():
            if key not in replaced:
                if out and not out[-1].endswith('\n'):
                    out[-1] += '\n'
                out.append('%s=%s\n' % (key, val))

        tmp = MESH_CONF_FILE + '.tmp'
        try:
            st = os.stat(MESH_CONF_FILE)
            with open(tmp, 'w') as f:
                f.writelines(out)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, stat.S_IMODE(st.st_mode))
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except PermissionError:
                pass
            os.replace(tmp, MESH_CONF_FILE)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return {'ok': False, 'error': 'Cannot write %s: %s' % (MESH_CONF_FILE, e)}

        try:
            rc = subprocess.run(['systemctl', 'reload', 'mesh-voice'],
                                timeout=10, stderr=subprocess.DEVNULL)
            reloaded = rc.returncode == 0
        except Exception:
            pass

    return {'ok': True, 'reloaded': reloaded}


def set_voice_codec_all(codec):
    """Stage a voice codec change over Alfred for every node.

    Codecs determine RTP clock rate and receiver decoding.
    Different codecs cannot interoperate. coordinate_radio_change() requires
    all expected ACKs before a shared activation time and cancels on a missing
    ACK. Talk groups remain per-radio settings in set_voice_channel().
    """
    codec = (codec or '').strip().lower()
    if codec not in VOICE_CODECS:
        return {'ok': False,
                'error': 'Codec must be one of: %s' % ', '.join(VOICE_CODECS)}
    result = coordinate_radio_change({'voice_codec': {'codec': codec}})
    if result.get('ok'):
        result['codec'] = codec
    return result


def voice_status():
    """Return voice state from mesh-voice.py's local file and systemd unit.

    Only the daemon touches the audio path.
    """
    out = {'service': 'stopped', 'enabled': False, 'state_age': None}

    # Use mesh.conf when the daemon is stopped, including voice=n with no
    # state file. A running daemon's tuned group overrides it below.
    conf = load_kv_file(MESH_CONF_FILE)
    out['voice_configured'] = conf.get('voice', 'n').strip().lower() in (
        'y', 'yes', 'true', '1', 'on')
    try:
        ch = int(conf.get('voice_channel', 1))
        out['channel'] = ch if 1 <= ch <= VOICE_CHANNEL_MAX else 1
    except (TypeError, ValueError):
        out['channel'] = 1

    # Probe activity and enablement independently, with three seconds per call.
    try:
        rc = subprocess.run(['systemctl', 'is-active', '--quiet', 'mesh-voice'],
                            timeout=3, stderr=subprocess.DEVNULL)
        out['service'] = 'running' if rc.returncode == 0 else 'stopped'
    except Exception:
        pass
    try:
        rc = subprocess.run(['systemctl', 'is-enabled', '--quiet', 'mesh-voice'],
                            timeout=3, stderr=subprocess.DEVNULL)
        out['enabled'] = rc.returncode == 0
    except Exception:
        pass

    try:
        with open(VOICE_STATE_FILE) as f:
            state = json.load(f)
        # Reject stale daemon state. Prefer the boot-clock stamp across
        # wall-clock steps, falling back to wall time for files without a boot
        # stamp.
        if isinstance(state.get('updated_boot'), (int, float)):
            age = time.clock_gettime(time.CLOCK_BOOTTIME) - state['updated_boot']
        else:
            age = time.time() - state.get('updated', 0)
        # State older than 30 seconds cannot claim the daemon is running.
        if 0 <= age < 30:
            out.update(state)
            out['state_age'] = int(age)
        else:
            out['service'] = 'stopped'
            out['stale'] = True
    except (OSError, ValueError):
        pass

    # AGC is a hardware mixer state, not in the daemon's state file.
    card = find_openvlm_card()
    if card is not None:
        try:
            r = subprocess.run(['amixer', '-c', str(card), 'get',
                                'Auto Gain Control'],
                               capture_output=True, text=True, timeout=3)
            if r.returncode == 0:
                out['agc'] = '[on]' in r.stdout
        except (OSError, subprocess.SubprocessError):
            pass

    return out


CONFIG_TAB_CSS = r"""

body { overflow-y: auto; }
/* ── Admin layout ── */
.admin-body { display: flex; gap: 0; height: calc(100vh - 34px); overflow: hidden; }
.admin-form-col { flex: 1; min-width: 0; overflow-y: auto; padding: 20px 24px; border-right: 1px solid var(--border); }
.admin-status-col { width: 320px; flex-shrink: 0; overflow-y: auto; padding: 16px; background: var(--surface); }
.admin-col-hdr { font-size: 11px; color: var(--muted); font-weight: 800; letter-spacing: 0; text-transform: none;
                 padding-bottom: 10px; border-bottom: 1px solid var(--border); margin-bottom: 14px; }
/* Section styling */
.cfg-section { margin-bottom: 22px; }
.cfg-section-title { font-size: 12px; color: var(--text); font-weight: 800; letter-spacing: 0; text-transform: none;
                      padding: 0 0 8px 0; border-bottom: 1px solid var(--border); margin-bottom: 12px; }
.cfg-row { display: flex; align-items: flex-start; gap: 10px; margin-bottom: 10px; }
.cfg-row label { flex: 0 0 180px; font-size: 11px; color: var(--muted); padding-top: 6px; }
.cfg-row .hint { display: block; font-size: 9px; color: var(--muted); margin-top: 2px; }
.cfg-row input[type=text], .cfg-row input[type=password], .cfg-row select {
  flex: 1; min-width: 0; width: 100%; background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  color: var(--text); font-family: var(--font); font-size: 12px;
  padding: 5px 8px; outline: none; }
.cfg-row input:focus, .cfg-row select:focus { border-color: var(--accent); }
.cfg-row input[type=checkbox] { width: 16px; height: 16px; margin-top: 6px; accent-color: var(--accent); }
.cfg-readonly { flex: 1; font-size: 12px; color: var(--text); padding: 6px 0; }
@media (max-width: 760px), (orientation:landscape) and (max-height:600px) {
  .admin-body { flex-direction: column; height: auto; overflow: visible; }
  .admin-form-col { min-width: 0; padding: 16px; border-right: 0; }
  .admin-status-col { width: auto; min-width: 0; border-top: 1px solid var(--border); }
  .cfg-row { flex-wrap: wrap; }
  .cfg-row label { flex-basis: 100%; font-size: 16px; padding-top: 0; }
  .cfg-row { gap: 6px; margin-bottom: 18px; }
  .cfg-row .hint { font-size: 14px; line-height: 1.45; }
  .cfg-row input[type=text], .cfg-row input[type=password], .cfg-row select {
    flex: 1 0 100%; min-width: 0; width: 100%!important; max-width: 100%; min-height: 44px; font-size: 16px;
  }
  .cfg-row input[type=checkbox], #f-no-rollback {
    width: 44px!important; height: 44px!important; margin: 0; flex-shrink: 0; accent-color: var(--info);
  }
  .cfg-readonly { font-size: 16px; overflow-wrap: anywhere; min-width: 0; }
  .admin-actions { flex-wrap: wrap; }
}
/* Danger badge on dangerous fields */
.danger-badge { display: inline-block; font-size: 9px; font-weight: bold; color: var(--bad); background: #ef444415;
                border: 1px solid #ef444430; border-radius: 2px; padding: 1px 5px;
                margin-left: 6px; vertical-align: middle; letter-spacing: .5px; }
/* Action buttons */
.admin-actions { display: flex; gap: 10px; margin-top: 20px; padding-top: 16px; border-top: 1px solid var(--border); }
.cfg-btn { padding: 9px 16px; border-radius: 8px; font-size: 12px; font-weight: 850; letter-spacing: 0;
       font-family: var(--font); cursor: pointer; border: 1px solid var(--accent); text-transform: none; background:var(--surface); color:var(--accent); transition:all .15s; }
.cfg-btn-stage  { background: var(--surface); color: var(--accent); border-color: var(--accent); }
.cfg-btn-stage:hover:not(:disabled)  { background: var(--accent); color: #ffffff; }
.cfg-btn-apply  { background: var(--accent2); color: var(--fer-black); border-color: var(--accent2); }
.cfg-btn-apply:hover:not(:disabled)  { background: var(--fer-black); color: #ffffff; border-color: var(--fer-black); }
.cfg-btn-force  { background: rgba(236,176,0,.10); color: var(--text); border-color: rgba(236,176,0,.48); }
.cfg-btn-force:hover:not(:disabled)  { background: var(--accent2); color: var(--fer-black); border-color: var(--accent2); }
.cfg-btn-cancel { background: transparent; color: #b42318; border-color: #e9b2ad; }
.cfg-btn-cancel:hover:not(:disabled) { background: #b42318; color: #ffffff; border-color: #b42318; }
:root[data-theme="dark"] .cfg-btn-stage { background: transparent; color: var(--accent2); border-color: var(--accent2); }
:root[data-theme="dark"] .cfg-btn-stage:hover:not(:disabled), :root[data-theme="dark"] .cfg-btn-apply:hover:not(:disabled), :root[data-theme="dark"] .cfg-btn-force:hover:not(:disabled) { background:#f8f6ef; color:var(--fer-black); border-color:#f8f6ef; }
:root[data-theme="dark"] .cfg-btn-apply { background: var(--accent2); color: var(--fer-black); border-color: var(--accent2); }
:root[data-theme="dark"] .cfg-btn-force { background: rgba(236,176,0,.10); color: var(--accent2); border-color: rgba(236,176,0,.48); }
:root[data-theme="dark"] .cfg-btn-cancel { color:#fca5a5; border-color:#7f1d1d; }
:root[data-theme="dark"] .cfg-btn-cancel:hover:not(:disabled) { background:#b42318; color:#ffffff; border-color:#b42318; }
.cfg-btn:disabled { opacity: 0.35; cursor: not-allowed; }
/* Status column: node ACK table */
.ack-table { width: 100%; border-collapse: collapse; }
.ack-table th { font-size: 9px; color: var(--muted); text-align: left; padding: 4px 6px;
                letter-spacing: .8px; text-transform: uppercase; border-bottom: 1px solid var(--border); }
.ack-table td { font-size: 11px; padding: 6px; border-bottom: 1px solid var(--border2); }
.ack-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 5px; }
.ack-dot-yes  { background: var(--good); }
.ack-dot-no   { background: var(--muted); }
.ack-dot-self { background: var(--info); }
/* Pending config info box */
.pending-box { background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
               padding: 10px 12px; margin-bottom: 14px; }
.pending-box.pending-active { border-color: #f59e0b40; background: #f59e0b08; }
.pending-label { font-size: 9px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }
.pending-version { overflow-wrap: anywhere; font-size: 12px; font-family: var(--font); font-weight: 800; color: var(--info); }
.pending-stat { font-size: 10px; color: var(--muted); margin-top: 4px; }
.pending-stat span { color: var(--text); }
/* Progress bar */
.ack-progress { height: 4px; background: var(--border); border-radius: 2px; margin-top: 8px; }
.ack-progress-bar { height: 100%; border-radius: 2px; background: var(--good); transition: width .5s; }
/* Warning modal */
#cfg-force-modal { display:none; position:fixed; top:0;left:0;right:0;bottom:0;
               background:#000a; z-index:100; align-items:center; justify-content:center; }
#cfg-force-modal.show { display:flex; }
.modal-box { background: var(--surface); border: 1px solid #ef444440; border-radius: 4px;
             padding: 24px; max-width: 380px; }
.modal-title { color: var(--bad); font-size: 14px; font-weight: bold; margin-bottom: 10px; }
.modal-body { color: var(--muted); font-size: 12px; line-height: 1.6; margin-bottom: 16px; }
.modal-actions { display:flex; gap:10px; justify-content:flex-end; }
/* Toast */
#cfg-toast { position:fixed; bottom:20px; right:20px; background: var(--surface); border:1px solid var(--border);
         border-radius:4px; padding:10px 16px; font-size:12px; opacity:0; transition:opacity .3s;
         pointer-events:none; z-index:200; }
#cfg-toast.show { opacity:1; }
#cfg-toast.toast-ok   { border-color:#22c55e60; color:var(--good); }
#cfg-toast.toast-err  { border-color:#ef444460; color:var(--bad); }
#cfg-toast.toast-warn { border-color:#f9731660; color:var(--warn); }
@media (max-width: 760px), (orientation:landscape) and (max-height:600px) {
  .admin-col-hdr, .cfg-section-title { font-size: 16px; }
  .cfg-btn { min-height: 44px; font-size: 16px; }
  .cfg-row .danger-badge { font-size: 12px; }
  .pending-label, .pending-stat, .modal-body, #cfg-toast { font-size: 14px; }
  .pending-version, .modal-title { font-size: 16px; }
  .ack-table { display: table; table-layout: fixed; }
  .ack-table th, .ack-table td { font-size: 14px; padding: 8px 3px; overflow-wrap: anywhere; letter-spacing: 0; }
  #cfg-force-modal { padding: 12px; }
  .modal-box { width: 100%; max-height: calc(100dvh - 24px); overflow-y: auto; padding: 18px; }
  .modal-actions { flex-wrap: wrap; }
  #cfg-toast { left: 12px; right: 12px; overflow-wrap: anywhere; }
}
"""

CONFIG_TAB_HTML = r"""
<div class="admin-body">
    <!-- ── Left: Config Form ── -->
    <div class="admin-form-col">

      <div class="cfg-section">
        <div class="cfg-section-title">EUD / Client Connection</div>
        <p class="cfg-section-note" style="font-size:var(--small-text);color:var(--muted);margin:0 0 10px;line-height:1.5">Applies on this node only. Mesh Network settings below still go to every radio.</p>

        <div class="cfg-row">
          <label for="f-eud">Connection Mode<span class="hint">How clients connect to this node</span></label>
          <select id="f-eud">
            <option value="wired">Wired (USB/Ethernet)</option>
            <option value="wireless">Wireless (5GHz AP)</option>
            <option value="auto">Auto (Wireless unless wired)</option>
          </select>
        </div>
        <div class="cfg-row">
          <label>AP SSID<span class="hint">Set at flash; not changed from here</span></label>
          <span id="f-lan-ap-ssid" class="cfg-readonly">-</span>
        </div>
        <div class="cfg-row">
          <label for="f-lan-ap-key">AP Password<span class="hint">Client WiFi password</span></label>
          <input type="password" id="f-lan-ap-key" autocomplete="new-password">
        </div>
        <div class="cfg-row">
          <label>Max EUDs per Node<span class="hint">Set at flash; not changed from here</span></label>
          <span id="f-max-euds" class="cfg-readonly">-</span>
        </div>
      </div>

      <div class="cfg-section">
        <div class="cfg-section-title">Mesh Network</div>

        <div class="cfg-row">
          <label for="f-mesh-ssid">Mesh SSID<span class="hint">BATMAN mesh network name
            <span class="danger-badge">⚠ DANGEROUS</span></span></label>
          <input type="text" id="f-mesh-ssid">
        </div>
        <div class="cfg-row">
          <label for="f-mesh-key">Mesh SAE Key<span class="hint">WPA3-SAE passphrase
            <span class="danger-badge">⚠ DANGEROUS</span></span></label>
          <input type="password" id="f-mesh-key" autocomplete="new-password">
        </div>
        <div class="cfg-row">
          <label for="f-ipv4-network">IP Range (CIDR)<span class="hint">Mesh network address space
            <span class="danger-badge">⚠ DANGEROUS</span></span></label>
          <input type="text" id="f-ipv4-network">
        </div>
        <div class="cfg-row">
          <label for="f-regulatory-domain">Regulatory Domain<span class="hint">2-letter country code for RF. Takes effect when each radio reboots; switching between US and Europe also moves HaLow to that region's default channel</span></label>
          <input type="text" id="f-regulatory-domain" maxlength="2" style="width:48px;flex:none;">
        </div>
        <div class="cfg-row">
          <label for="f-acs">Auto Channel<span class="hint">Scan and select best channel</span></label>
          <input type="checkbox" id="f-acs">
        </div>
      </div>

      <div class="cfg-section">
        <div class="cfg-section-title">Services</div>

        <div class="cfg-row">
          <label for="f-mtx">MediaMTX<span class="hint">RTSP/WebRTC streaming</span></label>
          <input type="checkbox" id="f-mtx">
        </div>
        <div class="cfg-row">
          <label for="f-mumble">Mumble<span class="hint">Voice communications</span></label>
          <input type="checkbox" id="f-mumble">
        </div>
        <div class="cfg-row">
          <label for="f-auto-update">Auto Update<span class="hint">Automatic MANET tool updates</span></label>
          <input type="checkbox" id="f-auto-update">
        </div>
      </div>

      <div class="cfg-section">
        <div class="cfg-section-title">Security</div>

        <div class="cfg-row">
          <label for="f-admin-password">Admin Password<span class="hint">This admin interface</span></label>
          <input type="password" id="f-admin-password" autocomplete="new-password">
        </div>
      </div>

      <div class="admin-actions">
        <button class="cfg-btn cfg-btn-stage"  id="btn-stage"  data-action="stage-config">Stage Changes</button>
        <button class="cfg-btn cfg-btn-apply"  id="btn-apply"  data-action="apply-config" disabled>Apply Now</button>
        <button class="cfg-btn cfg-btn-force"  id="btn-force"  data-action="show-force" style="display:none">Force Apply</button>
        <button class="cfg-btn cfg-btn-cancel" id="btn-cancel" data-action="cancel-config" style="display:none">Cancel</button>
      </div>
      <div id="action-msg" style="font-size:var(--small-text);color:var(--muted);margin-top:8px;min-height:14px;"></div>
    </div>

    <!-- ── Right: Deployment Status ── -->
    <div class="admin-status-col">
      <div class="admin-col-hdr">Deployment Status</div>

      <!-- Pending config box -->
      <div class="pending-box" id="pending-box">
        <div class="pending-label">Pending Config</div>
        <div id="pending-version" class="pending-version">None</div>
        <div id="pending-stat"   class="pending-stat" style="display:none"></div>
        <div id="ack-progress-wrap" style="display:none">
          <div class="ack-progress"><div class="ack-progress-bar" id="ack-bar" style="width:0%"></div></div>
        </div>
      </div>

      <!-- Node ACK table -->
      <div class="admin-col-hdr" style="margin-top:14px">Nodes (<span id="node-count">-</span>)</div>
      <table class="ack-table" id="ack-table">
        <thead><tr><th>Node</th><th>IP</th><th>ACK</th></tr></thead>
        <tbody id="ack-tbody"></tbody>
      </table>

      <!-- Dangerous change warning -->
      <div id="danger-warn" style="display:none;margin-top:14px;padding:10px;
           background:#ef444408;border:1px solid #ef444430;border-radius:3px;
           font-size:var(--small-text);color:var(--bad);line-height:1.6;">
        ⚠ Staged changes include <strong>DANGEROUS</strong> settings (mesh SSID, key, or IP range).
        All nodes will briefly disconnect while applying. Ensure 100% ACK before applying.
        <label style="display:flex;align-items:center;gap:6px;margin-top:8px;color:var(--muted);cursor:pointer">
          <input type="checkbox" id="f-no-rollback" style="width:14px;height:14px;margin:0">
          Skip the safety net: keep the change even if the mesh does not come back
        </label>
      </div>

      <!-- Rollback armed on this node -->
      <div id="rollback-box" style="display:none;margin-top:14px;padding:10px;
           background:#f59e0b0d;border:1px solid #f59e0b40;border-radius:3px;
           font-size:var(--small-text);color:var(--text);line-height:1.6;">
        <strong>Rollback armed</strong>
        <div id="rollback-detail" style="color:var(--muted);margin-top:4px"></div>
      </div>
    </div>
  </div>

  <!-- Force apply modal -->
  <div id="cfg-force-modal">
    <div class="modal-box">
      <div class="modal-title">⚠ Force Apply</div>
      <div class="modal-body" id="force-modal-body">
        Not all nodes have acknowledged the pending config.
        Forcing apply will push changes to this node only: unreachable nodes
        will remain on the old config and may need manual intervention.
      </div>
      <div class="modal-actions">
        <button class="cfg-btn cfg-btn-cancel" data-action="close-force">Cancel</button>
        <button class="cfg-btn cfg-btn-force"  data-action="force-config">Force Apply</button>
      </div>
    </div>
  </div>

  <div id="cfg-toast"></div>
"""

# Config controls use the status server's authenticated staging and ACK APIs.
CONFIG_TAB_JS = r"""

const POLL_MS = 5000;
let STATUS = null;
let pollTimer = null;

// ── Init ────────────────────────────────────────────────────────────────────
// Polls only while the NODE CONFIG tab is on screen: this is a 5 s poll and
// there is no reason to run it while the operator is looking at the topology.
function startConfigPolling() {
  if (pollTimer) return;
  refreshStatus();
  pollTimer = setInterval(refreshStatus, POLL_MS);
}
function stopConfigPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}

// ── Fetch status ─────────────────────────────────────────────────────────────
let configFetchBusy = false;
async function refreshStatus() {
  if (configFetchBusy || document.hidden) return;
  configFetchBusy = true;
  try {
    const r = await manageFetch('/api/admin/status');
    if (!r.ok) return;
    STATUS = await r.json();
    renderStatus(STATUS);
  } catch(e) {}
  finally { configFetchBusy = false; }
}

// ── Populate form from current config ────────────────────────────────────────
function populateForm(cfg) {
  setVal('f-eud',              cfg.eud              || 'wired');
  setText('f-lan-ap-ssid',     cfg.ap_ssid          || 'Unavailable');
  setVal('f-lan-ap-key',       cfg.lan_ap_key        || '');
  setText('f-max-euds',        cfg.max_euds_per_node || '-');
  setVal('f-mesh-ssid',        cfg.mesh_ssid         || '');
  setVal('f-mesh-key',         cfg.mesh_key          || '');
  setVal('f-ipv4-network',     cfg.ipv4_network      || '');
  setVal('f-regulatory-domain',cfg.regulatory_domain || 'US');
  setChk('f-acs',              cfg.acs        === 'y');
  setChk('f-mtx',              cfg.mtx        === 'y');
  setChk('f-mumble',           cfg.mumble     === 'y');
  setChk('f-auto-update',      cfg.auto_update=== 'y');
  setVal('f-admin-password',   cfg.admin_password    || '');
}

function setVal(id, v) { const el = document.getElementById(id); if (el) el.value = v; }
function setText(id, v) { const el = document.getElementById(id); if (el) el.textContent = v; }
function setChk(id, v) { const el = document.getElementById(id); if (el) el.checked = v; }
function getVal(id)    { const el = document.getElementById(id); return el ? el.value.trim() : ''; }
function getChk(id)    { const el = document.getElementById(id); return el ? el.checked : false; }

// ── Render status panel ───────────────────────────────────────────────────────
let formPopulated = false;
function renderStatus(s) {
  // Populate form once from live config
  if (!formPopulated && s.current_config) {
    populateForm(s.current_config);
    formPopulated = true;
  }

  document.getElementById('node-count').textContent = s.total_nodes || '0';

  const pending  = s.pending;
  const pBox     = document.getElementById('pending-box');
  const pVersion = document.getElementById('pending-version');
  const pStat    = document.getElementById('pending-stat');
  const pWrap    = document.getElementById('ack-progress-wrap');
  const ackBar   = document.getElementById('ack-bar');
  const rb = s.rollback || {};
  const rbBox = document.getElementById('rollback-box');
  if (rbBox) {
    rbBox.style.display = rb.armed ? '' : 'none';
    if (rb.armed) {
      const left = rb.seconds_left > 0
        ? `${Math.ceil(rb.seconds_left / 60)} min remaining`
        : 'deadline passed, deciding on the next cycle';
      document.getElementById('rollback-detail').textContent =
        `Version ${(rb.version || '').slice(0, 8)} is on trial. This node had ` +
        `${rb.peers_before} peer(s) before the change; if none are back by the ` +
        `deadline it restores the previous config by itself. ${left}.`;
    }
  }

  const dangerWarn = document.getElementById('danger-warn');
  const btnApply = document.getElementById('btn-apply');
  const btnForce = document.getElementById('btn-force');
  const btnCancel= document.getElementById('btn-cancel');

  if (pending && pending.version) {
    pBox.className = 'pending-box pending-active';
    pVersion.textContent = 'v' + pending.version;

    const nodes = s.nodes || [];
    const acked = nodes.filter(n => n.ack === pending.version).length;
    const total = nodes.length;
    const pct   = total > 0 ? Math.round(acked / total * 100) : 0;

    pStat.style.display = '';
    pStat.innerHTML = `<span>${esc(acked)}/${esc(total)}</span> nodes ACKed &nbsp; <span>${pct}%</span>`;
    pWrap.style.display = '';
    ackBar.style.width = pct + '%';
    ackBar.style.background = acked === total ? 'var(--good)' : 'var(--warn)';

    const isDangerous = pending.dangerous === true;
    dangerWarn.style.display = isDangerous ? '' : 'none';

    const allAcked = acked === total && total > 0;
    btnApply.disabled = !allAcked;
    btnApply.style.display = '';
    btnForce.style.display = !allAcked ? '' : 'none';
    btnCancel.style.display = '';

    const activateAt = pending.activate_at || 0;
    if (activateAt > 0) {
      const secs = Math.max(0, activateAt - Math.floor(meshNow()));
      document.getElementById('action-msg').textContent =
        secs > 0 ? `Applying in ${secs}s...` : 'Applying now...';
    }
  } else {
    pBox.className = 'pending-box';
    pVersion.textContent = 'None';
    pStat.style.display = 'none';
    pWrap.style.display = 'none';
    dangerWarn.style.display = 'none';
    btnApply.disabled = true;
    btnApply.style.display = 'none';
    btnForce.style.display = 'none';
    btnCancel.style.display = 'none';
    document.getElementById('action-msg').textContent = '';
  }

  // Node ACK table
  const tbody = document.getElementById('ack-tbody');
  tbody.innerHTML = (s.nodes || []).map(n => {
    const pendingVer = pending && pending.version;
    const isAcked    = pendingVer && n.ack === pendingVer;
    const isSelf     = n.hostname === (STATUS && STATUS.my_hostname);
    let dotCls, ackLabel;
    if (!pendingVer) {
      dotCls   = 'ack-dot-self';
      ackLabel = '-';
    } else if (isSelf && isAcked) {
      dotCls   = 'ack-dot-self';
      ackLabel = 'Self ✓';
    } else if (isAcked) {
      dotCls   = 'ack-dot-yes';
      ackLabel = '✓';
    } else {
      dotCls   = 'ack-dot-no';
      ackLabel = 'Waiting';
    }
    const stale = n.node_state !== 'ACTIVE';
    const nameStyle = stale ? 'color:var(--muted)' : '';
    return `<tr>
      <td style="${nameStyle}">${esc(n.hostname)}</td>
      <td style="color:var(--muted);font-size:var(--small-text)">${esc(n.ip)}</td>
      <td><span class="ack-dot ${dotCls}"></span>${ackLabel}</td>
    </tr>`;
  }).join('');
}

// ── Read form into config object ──────────────────────────────────────────────
function readForm() {
  return {
    eud:               getVal('f-eud'),
    lan_ap_key:        getVal('f-lan-ap-key'),
    mesh_ssid:         getVal('f-mesh-ssid'),
    mesh_key:          getVal('f-mesh-key'),
    ipv4_network:      getVal('f-ipv4-network'),
    regulatory_domain: getVal('f-regulatory-domain'),
    acs:               getChk('f-acs')          ? 'y' : 'n',
    mtx:               getChk('f-mtx')          ? 'y' : 'n',
    mumble:            getChk('f-mumble')        ? 'y' : 'n',
    auto_update:       getChk('f-auto-update')   ? 'y' : 'n',
    admin_password:    getVal('f-admin-password'),
  };
}

// ── Stage changes ─────────────────────────────────────────────────────────────
async function stageChanges() {
  const cfg = readForm();
  cfgShowMsg('Staging...', 'muted');
  try {
    const r   = await manageFetch('/api/admin/stage', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({config: cfg})
    });
    const res = await r.json();
    if (r.ok && res.ok) {
      if (res.local_only) {
        toast('Applied on this node', 'ok');
        cfgShowMsg(res.applied && res.applied.length
          ? 'Saved on this node: ' + res.applied.join(', ')
          : 'No EUD/AP changes to apply.', 'ok');
        await refreshStatus();
        return;
      }
      toast('Changes staged: waiting for nodes to ACK', 'ok');
      cfgShowMsg('Staged. Waiting for ' + (STATUS && STATUS.total_nodes || '?') + ' nodes to ACK.', 'ok');
      await refreshStatus();
    } else {
      toast('Stage failed: ' + (res.error || r.status), 'err');
      cfgShowMsg('Stage failed.', 'err');
    }
  } catch(e) {
    toast('Stage error: ' + e, 'err');
  }
}

// ── Apply changes ─────────────────────────────────────────────────────────────
async function applyChanges(force) {
  closeForceModal();
  cfgShowMsg('Sending activate signal...', 'ok');
  try {
    const r   = await manageFetch('/api/admin/activate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({force: force, no_rollback: skipRollback()})
    });
    const res = await r.json();
    if (r.ok && res.ok) {
      toast('Activate signal sent: applying in 60s', 'ok');
      cfgShowMsg(res.no_rollback
        ? 'All nodes will apply in ~60s. Safety net skipped: this change is permanent.'
        : 'All nodes will apply in ~60s. Dangerous changes roll back automatically if the mesh does not re-form.',
        'ok');
      await refreshStatus();
    } else {
      toast('Activate failed: ' + (res.error || r.status), 'err');
    }
  } catch(e) {
    toast('Activate error: ' + e, 'err');
  }
}

// ── Cancel pending ────────────────────────────────────────────────────────────
async function cancelPending() {
  try {
    const r   = await manageFetch('/api/admin/cancel', {method: 'POST'});
    const res = await r.json();
    if (r.ok && res.ok) {
      toast('Pending config cancelled', 'warn');
      cfgShowMsg('', '');
      await refreshStatus();
    }
  } catch(e) {}
}

function skipRollback() {
  const el = document.getElementById('f-no-rollback');
  return !!(el && el.checked);
}

// ── Force modal ───────────────────────────────────────────────────────────────
function showForceModal() {
  const s     = STATUS;
  const nodes = s && s.nodes || [];
  const pv    = s && s.pending && s.pending.version;
  const acked = pv ? nodes.filter(n => n.ack === pv).length : 0;
  const total = nodes.length;
  document.getElementById('force-modal-body').textContent =
    `${acked} of ${total} nodes have ACKed. Forcing will apply on all reachable nodes. ` +
    `Unreachable nodes (${total - acked}) will remain on the old config until they reconnect. ` +
    (skipRollback()
      ? 'The safety net is switched off: nodes will keep the change even if the mesh does not come back.'
      : 'The safety net still applies: a node that loses the mesh restores its previous config.');
  document.getElementById('cfg-force-modal').classList.add('show');
}
function closeForceModal() {
  document.getElementById('cfg-force-modal').classList.remove('show');
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function cfgShowMsg(msg, cls) {
  const el = document.getElementById('action-msg');
  el.textContent = msg;
  el.style.color = cls === 'ok' ? 'var(--good)' : cls === 'err' ? 'var(--bad)' : 'var(--muted)';
}

let toastTimer = null;
function toast(msg, cls) {
  const el = document.getElementById('cfg-toast');
  el.textContent = msg;
  el.className = 'show toast-' + cls;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.className = '', 3000);
}
"""


def render_dashboard(nonce=None):
    """Assemble the management page with local identity and tab assets."""
    nonce = nonce or new_csp_nonce()
    hostname = html.escape(get_my_hostname())
    ip       = html.escape(get_my_ip())
    logo_v   = logo_asset_token()
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MANET // MANAGE</title>
<style>{CSS}{CONFIG_TAB_CSS}{VOICE_TAB_CSS}
</style>
</head><body>

<div id="overlay" class="info">
  <div class="overlay-body">
    <div id="overlay-text"></div>
    <button id="overlay-close" data-action="hide-overlay" aria-label="Close">&times;</button>
  </div>
</div>

<div id="hdr">
  <div class="fer-lockup" title="FER" aria-label="FER">
    <img class="fer-logo-img" src="/assets/fer-logo-black?v={logo_v}" data-light="/assets/fer-logo-black?v={logo_v}" data-dark="/assets/fer-logo-white?v={logo_v}" alt="FER">
  </div>
  <div id="hdr-logo">MANET//<span>MANAGE</span></div>
  <div id="hdr-node"><strong>{hostname}</strong> &nbsp;{ip}</div>
  <div id="hdr-right">
    <span id="hdr-inet" class="no">○ NO INET</span>
    <span id="hdr-clock"></span>
    <button id="overview-link" class="overview-link-btn" type="button" data-action="overview">OVERVIEW</button>
    <button id="theme-toggle" class="theme-toggle" type="button" data-action="theme">Dark</button>
  </div>
</div>

<div id="page">
<div id="nav" role="tablist" aria-label="Management sections">
  <button type="button" role="tab" id="nav-topology" class="tab active" data-tab="topology" data-action="show-tab" aria-controls="tab-topology" aria-selected="true" tabindex="0">TOPOLOGY</button>
  <button type="button" role="tab" id="nav-radio" class="tab" data-tab="radio" data-action="show-tab" aria-controls="tab-radio" aria-selected="false" tabindex="-1">RADIO CONFIG</button>
  <button type="button" role="tab" id="nav-measure" class="tab" data-tab="measure" data-action="show-tab" aria-controls="tab-measure" aria-selected="false" tabindex="-1">MEASURE</button>
  <button type="button" role="tab" id="nav-sessions" class="tab" data-tab="sessions" data-action="show-tab" aria-controls="tab-sessions" aria-selected="false" tabindex="-1">SESSIONS</button>
  <button type="button" role="tab" id="nav-uplink" class="tab" data-tab="uplink" data-action="show-tab" aria-controls="tab-uplink" aria-selected="false" tabindex="-1">UPLINK</button>
  <button type="button" role="tab" id="nav-voice" class="tab" data-tab="voice" data-action="show-tab" aria-controls="tab-voice" aria-selected="false" tabindex="-1">VOICE</button>
  <button type="button" role="tab" id="nav-config" class="tab" data-tab="config" data-action="show-tab" aria-controls="tab-config" aria-selected="false" tabindex="-1">NODE CONFIG</button>
</div>

<div id="content">
  <div id="msg"></div>
  <details class="card"><summary id="recovery-summary" style="cursor:pointer">Mesh recovery</summary><div id="recovery-status"></div></details>

  <!-- ── TOPOLOGY ── -->
  <div id="tab-topology" class="tab-pane" role="tabpanel" aria-labelledby="nav-topology" tabindex="0">
    <div class="card">
      <div class="card-title">Mesh Nodes</div>
      <div class="node-grid" id="node-grid">
        <div style="padding:20px;color:var(--muted);font-size:var(--small-text)">Loading topology...</div>
      </div>
    </div>
  </div>

  <!-- ── RADIO CONFIG ── -->
  <div id="tab-radio" class="tab-pane" role="tabpanel" aria-labelledby="nav-radio" tabindex="0" style="display:none">
    <div class="card">
      <div class="card-title">Global Actions</div>
      <div class="global-bar" id="global-bar">
        <button class="btn btn-green" data-action="toggle-all" data-iface="wlan0" data-state="up">↑ 2.4G ALL</button>
        <button class="btn btn-red"   data-action="toggle-all" data-iface="wlan0" data-state="down">↓ 2.4G ALL</button>
        <button class="btn btn-green" data-action="toggle-all" data-iface="wlan1" data-state="up">↑ 5G ALL</button>
        <button class="btn btn-red"   data-action="toggle-all" data-iface="wlan1" data-state="down">↓ 5G ALL</button>
        <button class="btn btn-green" data-action="toggle-all" data-iface="wlan2" data-state="up">↑ HALOW ALL</button>
        <button class="btn btn-red"   data-action="toggle-all" data-iface="wlan2" data-state="down">↓ HALOW ALL</button>
      </div>
    </div>
    <div id="iface-cards"></div>
    <div class="card">
      <div class="card-title">HaLow</div>
      <div class="row">
        <span class="row-label">Channel</span>
        <select id="halow-ch"><option value="">Loading...</option></select>
      </div>
      <div class="row">
        <span class="row-label">Bandwidth</span>
        <select id="halow-bw"><option value="">Loading...</option></select>
      </div>
      <div class="row">
        <span class="row-label">TX Power</span>
        <span id="halow-txpower">Loading...</span>
      </div>
      <div class="row">
        <span class="row-label"></span>
        <button class="btn btn-green" id="btn-apply-halow" data-action="apply-halow">APPLY TO ALL NODES</button>
      </div>
    </div>
    <div class="card">
      <div class="card-title">2.4 GHz</div>
      <div class="row">
        <span class="row-label">Channel</span>
        <select id="ch-2g" disabled>
          ${''.join(f'<option value="{c}"{" selected" if c==6 else ""}>ch {c} ({2407+c*5} MHz)</option>' for c in range(1,14))}
        </select>
      </div>
      <div class="row">
        <span class="row-label">TX Power</span>
        <select id="txpwr-all-wlan0"><option value="">Loading...</option></select>
      </div>
      <div class="row">
        <span class="row-label"></span>
        <button class="btn btn-green" id="btn-apply-2g" data-action="apply-2g" disabled>APPLY TO ALL NODES</button>
      </div>
      <div class="acs-channel-note"></div>
    </div>
    <div class="card">
      <div class="card-title">5 GHz</div>
      <div class="row">
        <span class="row-label">Channel</span>
        <select id="ch-5g" disabled>
          <option value="36">ch 36 (5180 MHz)</option>
          <option value="40">ch 40 (5200 MHz)</option>
          <option value="44">ch 44 (5220 MHz)</option>
          <option value="48">ch 48 (5240 MHz)</option>
          <option value="52">ch 52 (5260 MHz)</option>
          <option value="56">ch 56 (5280 MHz)</option>
          <option value="60">ch 60 (5300 MHz)</option>
          <option value="64">ch 64 (5320 MHz)</option>
          <option value="100">ch 100 (5500 MHz)</option>
          <option value="104">ch 104 (5520 MHz)</option>
          <option value="108">ch 108 (5540 MHz)</option>
          <option value="112">ch 112 (5560 MHz)</option>
          <option value="116">ch 116 (5580 MHz)</option>
          <option value="120">ch 120 (5600 MHz)</option>
          <option value="124">ch 124 (5620 MHz)</option>
          <option value="128">ch 128 (5640 MHz)</option>
          <option value="132">ch 132 (5660 MHz)</option>
          <option value="136">ch 136 (5680 MHz)</option>
          <option value="140">ch 140 (5700 MHz)</option>
          <option value="149">ch 149 (5745 MHz)</option>
          <option value="153">ch 153 (5765 MHz)</option>
          <option value="157">ch 157 (5785 MHz)</option>
          <option value="161">ch 161 (5805 MHz)</option>
          <option value="165">ch 165 (5825 MHz)</option>
        </select>
      </div>
      <div class="row">
        <span class="row-label">TX Power</span>
        <select id="txpwr-all-wlan1"><option value="">Loading...</option></select>
      </div>
      <div class="row">
        <span class="row-label"></span>
        <button class="btn btn-green" id="btn-apply-5g" data-action="apply-5g" disabled>APPLY TO ALL NODES</button>
      </div>
      <div class="acs-channel-note"></div>
    </div>
  </div>

  <!-- ── MEASURE ── -->
  <div id="tab-measure" class="tab-pane" role="tabpanel" aria-labelledby="nav-measure" tabindex="0" style="display:none">
    <div class="card">
      <div class="card-title">Session Label</div>
      <div class="row">
        <span class="row-label">Label</span>
        <input type="text" id="session-label" placeholder="e.g. outdoor-50m  /  line-of-sight-100m" style="width:320px">
      </div>
    </div>
    <div class="card">
      <div class="card-title">Test Pairs &nbsp;<span style="color:var(--muted);font-size:var(--small-text)">Select source → destination</span></div>
      <div class="pairs-grid" id="pairs-grid">
        <div style="padding:12px;color:var(--muted);font-size:var(--small-text)">Loading nodes...</div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Test Types</div>
      <div class="tests-wrap" id="tests-grid">
        <label class="test-chip"><input type="checkbox" value="tcp_1stream"    checked> TCP 1-STREAM</label>
        <label class="test-chip"><input type="checkbox" value="tcp_4stream"          > TCP 4-STREAM</label>
        <label class="test-chip"><input type="checkbox" value="udp_throughput" checked> UDP THROUGHPUT</label>
        <label class="test-chip"><input type="checkbox" value="udp_jitter"           > UDP JITTER</label>
        <label class="test-chip"><input type="checkbox" value="packet_loss"          > PACKET LOSS</label>
        <label class="test-chip"><input type="checkbox" value="reverse"              > REVERSE</label>
        <label class="test-chip"><input type="checkbox" value="icmp_ping"      checked> ICMP PING</label>
      </div>
    </div>
    <div class="card">
      <div class="card-title">Parameters</div>
      <div class="row">
        <span class="row-label">Duration</span>
        <input type="number" id="duration" value="30" min="5" max="300" style="width:80px">
        <span style="font-size:var(--small-text);color:var(--muted)">seconds</span>
      </div>
      <div class="row">
        <span class="row-label">UDP Bitrate</span>
        <select id="udp-bitrate">
          <option value="1M">1 Mbps</option>
          <option value="2M">2 Mbps</option>
          <option value="4M" selected>4 Mbps &nbsp;[HaLow typical max]</option>
          <option value="10M">10 Mbps</option>
          <option value="50M">50 Mbps</option>
          <option value="100M">100 Mbps</option>
        </select>
      </div>
    </div>
    <div style="padding:16px 0">
      <button class="btn btn-run" id="btn-run" data-action="start-measurement">&#9654; RUN MEASUREMENTS</button>
    </div>
    <div class="card" id="progress-card" style="display:none">
      <div class="card-title">Progress</div>
      <div class="progress-wrap">
        <div class="progress-bar-bg"><div class="progress-bar-fill" id="progress-fill"></div></div>
        <div class="progress-text" id="progress-text"></div>
        <div class="progress-stats" id="progress-stats"></div>
      </div>
    </div>
  </div>

  <!-- ── SESSIONS ── -->
  <div id="tab-sessions" class="tab-pane" role="tabpanel" aria-labelledby="nav-sessions" tabindex="0" style="display:none">
    <div class="card">
      <div class="card-title">Saved Sessions</div>
      <div id="sessions-list">
        <div style="padding:16px;color:var(--muted);font-size:var(--small-text)">Loading...</div>
      </div>
    </div>
  </div>

  <!-- Wi-Fi internet connection -->
  <div id="tab-uplink" class="tab-pane" role="tabpanel" aria-labelledby="nav-uplink" tabindex="0" style="display:none">
    <div class="card">
      <div class="card-title">Wi-Fi internet (this node)</div>
      <div class="uplink-body">
        <p id="uplink-wifi-intro">Choose a radio, scan, then tap the Wi-Fi
        network you want to use for internet.
        Its password is saved only on this node.</p>
        <div class="uplink-toolbar">
          <label for="uplink-wifi-radio">Radio</label>
          <select id="uplink-wifi-radio" disabled></select>
          <button class="btn" id="btn-uplink-wifi-scan" data-action="scan-uplink">SCAN</button>
        </div>
        <h3 id="uplink-wifi-heading">Nearby networks</h3>
        <div id="uplink-wifi-networks" role="group" aria-labelledby="uplink-wifi-heading" tabindex="0">
          <div id="uplink-wifi-current" hidden></div>
          <div id="uplink-wifi-results"></div>
        </div>
        <p id="uplink-wifi-feedback" role="status" aria-live="polite">Scan to find nearby networks.</p>
        <p id="uplink-wifi-age"></p>
        <p id="uplink-wifi-warning" role="status" aria-live="polite"></p>
        <div id="uplink-wifi-connect" hidden>
          <p id="uplink-wifi-selected"></p>
          <div id="uplink-wifi-saved-password" hidden>
            <p>Uses the saved password</p>
            <button type="button" class="btn" id="btn-uplink-wifi-password" data-action="different-uplink-password">Use a different password</button>
          </div>
          <div id="uplink-wifi-password-row">
            <label for="uplink-wifi-password">Password</label>
            <input type="password" id="uplink-wifi-password" autocomplete="new-password" autocapitalize="none" spellcheck="false">
          </div>
          <button class="btn btn-green" id="btn-uplink-wifi-apply" data-action="apply-uplink" disabled>CONNECT</button>
        </div>
        <details class="uplink-help">
          <summary>How this affects your connection</summary>
          <p>The page asks before a scan needs to stop your phone hotspot.
          A built-in radio used for internet stops carrying mesh traffic.
          If your phone uses this node's Wi-Fi and you choose the 2.4 GHz
          radio, only HaLow connects the other nodes.</p>
          <p>With the 5 GHz radio, your phone hotspot shares the nearby
          network's channel. On a radar channel (DFS), the hotspot may pause
          or stay off. Keep access through a cable or another node.
          A working internet cable takes priority over Wi-Fi.</p>
        </details>
        <p id="uplink-wifi-status" role="status">Checking connection...</p>
        <button class="btn" id="btn-uplink-wifi-disconnect" data-action="disconnect-uplink">DISCONNECT</button>
        <h3 id="uplink-wifi-saved-heading" tabindex="-1">Saved networks</h3>
        <p>Passwords stay on this node. Both built-in radios share this list. Forget removes
        the saved password without disconnecting the current connection. Next time
        you choose that network, enter its password again.</p>
        <p id="uplink-wifi-saved-empty">No saved networks yet. Connect to a secured network to save its password.</p>
        <ul id="uplink-wifi-saved-list" aria-labelledby="uplink-wifi-saved-heading"></ul>
        <p id="uplink-wifi-saved-feedback" role="status" aria-live="polite"></p>
      </div>
    </div>
  </div>
  <!-- ── VOICE ── -->
  <div id="tab-voice" class="tab-pane" role="tabpanel" aria-labelledby="nav-voice" tabindex="0" style="display:none">{VOICE_TAB_HTML}</div>

  <!-- ── NODE CONFIG ── -->
  <!-- The old /admin page. Same staging/ACK/apply flow, now behind the same
       password as everything else here rather than open on port 80. -->
  <div id="tab-config" class="tab-pane" role="tabpanel" aria-labelledby="nav-config" tabindex="0" style="display:none">
{CONFIG_TAB_HTML}
  </div>

  <div class="footer-actions">
    <button class="logout-link-btn" type="button" data-action="logout">LOGOUT</button>
  </div>
</div><!-- #content -->
</div><!-- #page -->

<script nonce="{nonce}">{RECOVERY_JS}
{JS}
{CONFIG_TAB_JS}
{VOICE_TAB_JS}</script>
</body></html>"""


# Request Handler

ASSET_CONTENT_TYPES = {
    '.svg':  'image/svg+xml',
    '.png':  'image/png',
    '.jpg':  'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.webp': 'image/webp',
}

def asset_content_type(path):
    """Choose a MIME type by suffix, falling back to binary content."""
    ext = os.path.splitext(path)[1].lower()
    return ASSET_CONTENT_TYPES.get(ext, 'application/octet-stream')

def logo_asset_file(url_path):
    """Map /assets/fer-logo-white[.png|.svg] to the installed file."""
    if not url_path.startswith('/assets/'):
        return None
    name = os.path.splitext(url_path[len('/assets/'):])[0]
    return FER_LOGO_ASSETS.get(name)

class ManageRoutes:
    """Provide routes for mesh-status.py using its send_json/send_html methods.

    Prefixed methods avoid shadowing do_* dispatch. mesh-status checks the
    password cookie before calling them.
    """
    def log_message(self, fmt, *args):
        """Suppress HTTP access log output."""
        pass  # suppress access logs

    def manage_do_GET(self):
        """Serve management assets, status and saved measurement results."""
        # mesh-status strips /manage and verifies the session before dispatch.
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip('/') or '/'

        logo_file = logo_asset_file(parsed.path)
        if logo_file:
            try:
                with open(logo_file, 'rb') as f:
                    body = f.read()
                self.send_response(200)
                self.send_header('Content-Type', asset_content_type(logo_file))
                self.send_header('Content-Length', str(len(body)))
                # Logo URLs use a metadata token and allow one hour of caching.
                self.send_header('Cache-Control', 'public, max-age=3600')
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'Not found')

        elif path in ('/', '/index.html'):
            self.send_html(render_dashboard)

        elif path == '/api/topology':
            try:
                self.send_json(STATUS_CACHE.get('topology', build_topology))
            except Busy:
                raise
            except Exception as e:
                self.send_json({'error': str(e)}, 500)

        elif path == '/api/measure/status':
            # measure_status_snapshot takes _measure_lock itself.
            self.send_json(measure_status_snapshot())

        elif path == '/api/uplink/wifi':
            self.send_json(wifi_uplink_status())

        elif path == '/api/voice':
            try:
                # Voice follows its two-second state cadence. Other probes use
                # the shared cache's default five-second lifetime.
                self.send_json(STATUS_CACHE.get('voice', voice_status, ttl=2))
            except Busy:
                raise
            except Exception as e:
                self.send_json({'error': str(e)}, 500)

        elif path == '/api/sessions':
            self.send_json(list_sessions())

        elif path.startswith('/api/sessions/'):
            parts = path[len('/api/sessions/'):].split('/')
            # Decode before validation so escaped separators cannot bypass it.
            label = unquote(parts[0])
            try:
                session_path(label)
            except ValueError as error:
                self.send_json({'ok': False, 'error': str(error)}, 400)
                return
            if len(parts) > 1 and parts[1] == 'csv':
                csv_data = session_to_csv(label).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/csv')
                self.send_header('Content-Disposition', f'attachment; filename="{label}.csv"')
                self.send_header('Content-Length', str(len(csv_data)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(csv_data)
            else:
                self.send_json(get_session_results(label))

        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not found')

    def manage_do_DELETE(self):
        """Delete a session after authentication by the outer handler."""
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip('/') or '/'

        if path.startswith('/api/sessions/'):
            label = unquote(path[len('/api/sessions/'):].split('/')[0])
            try:
                session_path(label)
            except ValueError as error:
                self.send_json({'ok': False, 'error': str(error)}, 400)
                return
            ok, error = delete_session(label)
            self.send_json({'ok': ok, 'error': error})
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not found')

    def manage_do_POST(self):
        """Dispatch authenticated changes and start bounded measurement work."""
        global _measure_status
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip('/') or '/'
        # The outer handler serializes writes and supplies bounded body reads.
        body = self.read_body() or b'{}'

        if path == '/api/interface/toggle':
            try:
                req      = json.loads(body)
                node_ip  = req.get('node_ip', '')
                iface    = req.get('iface', '')
                state    = req.get('state', '')
                self.send_json(coordinate_radio_toggle(node_ip, iface, state))
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/voice/channel':
            # Local only: a talk group is this radio's setting, not the mesh's.
            try:
                req = json.loads(body)
                self.send_json(set_voice_channel(req.get('channel')))
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/voice/codec':
            # Mesh-wide, unlike the talk group: the codecs cannot hear each
            # other, so this is staged over Alfred like a channel or key change.
            try:
                req = json.loads(body)
                self.send_json(set_voice_codec_all(req.get('codec')))
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/voice/audio':
            try:
                req = json.loads(body)
                self.send_json(set_voice_audio(req))
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/txpower':
            try:
                req = json.loads(body)
                if is_halow_iface(req.get('iface', '')):
                    self.send_json({'ok': False, 'error': HALOW_POWER_FIXED})
                    return
                self.send_json(coordinate_radio_change(
                    {'txpower': {req.get('iface', ''): req.get('dbm')}},
                    req.get('node_ip', 'all')))
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/halow/channel':
            # A HaLow-only mesh loses connectivity while nodes change channels.
            # Alfred stages all nodes for the same activate_at time.
            try:
                req = json.loads(body)
                if not int(req.get('channel', 0)):
                    self.send_json({'ok': False, 'error': 'Missing channel'})
                    return
                if req.get('dbm') is not None:
                    self.send_json({'ok': False, 'error': HALOW_POWER_FIXED})
                    return
                self.send_json(coordinate_radio_change({'halow_channel': {
                    'channel': int(req['channel']),
                    'bw': req.get('bw', '2MHz'),
                }}))
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/wifi/channel':
            try:
                req = json.loads(body)
                band = req.get('band')
                validate_wifi_channel(band, req.get('channel'))
                self.send_json(coordinate_radio_change({'wifi_channel': {
                    'band': band,
                    'channel': req.get('channel'),
                    'dbm': req.get('dbm'),
                }}))
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path == '/api/measure/start':
            # Reserve running state under the same lock used by worker updates.
            with _measure_lock:
                if _measure_status['running']:
                    self.send_json({'ok': False, 'error': 'Measurement already running'})
                    return
                try:
                    req    = json.loads(body)
                    label  = req.get('label', '')
                    check_capacity(SESSIONS_DIR, session_path(label))
                    pairs  = req.get('pairs', [])
                    tests  = req.get('tests', [])
                    dur    = int(req.get('duration', 30))
                    bitrate = req.get('udp_bitrate', '4M')
                    allowed_tests = {'tcp_1stream', 'tcp_4stream', 'udp_throughput',
                                     'udp_jitter', 'packet_loss', 'reverse', 'icmp_ping'}
                    if (not label or not isinstance(pairs, list) or not 1 <= len(pairs) <= 64
                            or not isinstance(tests, list) or not 1 <= len(tests) <= 7
                            or any(not isinstance(t, str) or t not in allowed_tests for t in tests)
                            or not 5 <= dur <= 300):
                        self.send_json({'ok': False, 'error':
                            'Choose 1-64 pairs, 1-7 test types, and a duration of 5-300 seconds'}, 400)
                        return
                    # Budget command deadlines: 30 s per ping, duration + 25 s
                    # per iperf3 invocation, at most 3600 s for the batch.
                    # Include setup and every pair/test's route, storage and
                    # pause. The worker rechecks time if local work runs slow.
                    budget = MEASURE_SETUP_SEC + len(pairs) * sum(
                        measurement_test_budget(t, dur) for t in tests)
                    if budget > MEASURE_BUDGET_SEC:
                        self.send_json({'ok': False, 'error':
                            'A measurement batch must fit within one hour; choose fewer pairs or shorter tests'}, 400)
                        return
                    local_ip = get_my_ip()
                    for pair in pairs:
                        if (not isinstance(pair, dict) or not local_ip
                                or pair.get('src_ip') != local_ip):
                            raise ValueError('Measurements must run from this node')
                        pair['dst_ip'] = str(ipaddress.IPv4Address(pair.get('dst_ip', '')))
                        if not isinstance(pair.get('dst_name'), str):
                            raise ValueError('Destination name must be text')
                        pair['src_name'] = get_my_hostname()
                    _measure_status['running']  = True
                    _measure_status['label']    = label
                    _measure_status['progress'] = 'Starting...'
                    _measure_status['error']    = ''
                    _measure_status['done']     = 0
                    _measure_status['total']    = len(pairs) * len(tests)
                    _measure_status['started_at'] = int(time.time())
                    _measure_mono['started'] = time.monotonic()
                    _measure_mono['current'] = None
                    _measure_status['current_started_at'] = None
                    _measure_status['current'] = None
                    _measure_status['last_result'] = None
                    t = threading.Thread(
                        target=run_measurement_session,
                        args=(label, pairs, tests, dur, bitrate),
                        daemon=True
                    )
                    t.start()
                    self.send_json({'ok': True})
                except ValueError as e:
                    _measure_status['running'] = False
                    self.send_json({'ok': False, 'error': str(e)}, 400)
                except Exception as e:
                    _measure_status['running'] = False
                    self.send_json({'ok': False, 'error': str(e)})

        elif path in ('/api/uplink/wifi/scan', '/api/uplink/wifi/scan-plan'):
            try:
                action = path.rsplit('/', 1)[1]
                self.send_json(dict(builtin_wifi_request(
                    action, json.loads(body)), ok=True))
            except Exception as error:
                self.send_json({'ok': False, 'error': str(error)})

        elif path == '/api/uplink/wifi/forget':
            try:
                self.send_json(dict(builtin_wifi_request('forget', json.loads(body)), ok=True))
            except Exception as error:
                self.send_json({'ok': False, 'error': str(error)})

        elif path == '/api/uplink/wifi':
            try:
                req = json.loads(body)
                result = builtin_wifi_request('set', req)
                self.send_json(dict(result, ok=True))
            except Exception as error:
                self.send_json({'ok': False, 'error': str(error)})

        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not found')
