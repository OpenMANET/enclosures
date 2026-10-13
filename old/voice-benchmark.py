#!/usr/bin/env python3
# voice-benchmark.py — Lyra vs Opus benchmark using the SAME GStreamer Python
# bindings mesh-voice.py uses (gst-launch-1.0 CLI is not installed on the nodes).
# Matches production element config from mesh-voice.py.
import gi, os, resource, time, sys
gi.require_version("Gst", "1.0")
from gi.repository import Gst
Gst.init(None)

MODEL = "/usr/local/share/lyra/model_coeffs"
SECS = 30

def have(el): return Gst.ElementFactory.find(el) is not None

def run_to_eos(desc):
    p = Gst.parse_launch(desc)
    bus = p.get_bus()
    p.set_state(Gst.State.PLAYING)
    msg = bus.timed_pop_filtered(120 * Gst.SECOND,
              Gst.MessageType.EOS | Gst.MessageType.ERROR)
    err = None
    if msg and msg.type == Gst.MessageType.ERROR:
        err = msg.parse_error()
    p.set_state(Gst.State.NULL)
    return err

def bitrate(name, rate, spb, enc, out):
    nbuf = rate * SECS // spb
    try: os.remove(out)
    except OSError: pass
    desc = (f"audiotestsrc num-buffers={nbuf} samplesperbuffer={spb} ! "
            f"audio/x-raw,rate={rate},channels=1,format=S16LE ! audioconvert ! "
            f"{enc} ! filesink location={out}")
    err = run_to_eos(desc)
    if err: print(f"  {name} bitrate: ERROR {err.message}"); return
    b = os.path.getsize(out); os.remove(out)
    print(f"  {name} payload: {b} bytes / {SECS}s = {b*8/SECS/1000:.2f} kbps (codec only, no RTP)")

def cpu(name, rate, spb, enc, dec):
    nbuf = rate * SECS // spb
    desc = (f"audiotestsrc num-buffers={nbuf} samplesperbuffer={spb} ! "
            f"audio/x-raw,rate={rate},channels=1,format=S16LE ! audioconvert ! "
            f"{enc} ! {dec} ! fakesink sync=false")
    r0 = resource.getrusage(resource.RUSAGE_SELF); w0 = time.time()
    err = run_to_eos(desc)
    r1 = resource.getrusage(resource.RUSAGE_SELF); w1 = time.time()
    if err: print(f"  {name} cpu: ERROR {err.message}"); return
    u = r1.ru_utime - r0.ru_utime; s = r1.ru_stime - r0.ru_stime
    cpu_s = u + s; realtime_pct = cpu_s / SECS * 100
    print(f"  {name} encode+decode of {SECS}s audio: CPU {cpu_s:.2f}s "
          f"(user {u:.2f} sys {s:.2f}) = {realtime_pct:.1f}% of one core | "
          f"wall {w1-w0:.1f}s | peak RSS {r1.ru_maxrss/1024:.1f} MB")

print(f"lyraenc={have('lyraenc')} lyradec={have('lyradec')} "
      f"opusenc={have('opusenc')} opusdec={have('opusdec')} "
      f"model={'ok' if os.path.isdir(MODEL) else 'MISSING'}\n")

print("=== LYRA (6000 bps @ 16 kHz) ===")
bitrate("lyra", 16000, 320, f"lyraenc bitrate=6000 model-path={MODEL}", "/tmp/lyra.bin")
cpu("lyra", 16000, 320, f"lyraenc bitrate=6000 model-path={MODEL}", f"lyradec model-path={MODEL}")
print("\n=== OPUS (32000 bps @ 48 kHz, 20 ms, inband-fec) ===")
bitrate("opus", 48000, 960, "opusenc bitrate=32000 frame-size=20 inband-fec=true", "/tmp/opus.bin")
cpu("opus", 48000, 960, "opusenc bitrate=32000 frame-size=20 inband-fec=true", "opusdec plc=true use-inband-fec=true")
