#!/usr/bin/env python3
"""Identify which AUX/HC-jack contact is DATA and which is DROP. LISTEN-ONLY.

Wiring (one probe lead, moved from contact to contact):
  probe lead -> 47k -> NODE ;  NODE -> 100k -> Pi pin 9 (GND)
  (light load: the mount bus has 10k pull-ups and PIC inputs need >= 4.0 V)
  NODE -> Pi pin 10 (RXD)  and  NODE -> Pi pin 13 (GPIO27)
  mount GND -> Pi pin 9
NEVER touch the probe to a contact that measured above 5.5 V on the meter
(the 12 V pin would put ~8 V on the Pi and destroy it).

Run once per contact, hand controller plugged in, pressing direction keys:
  tt_pinscan.py --label pin3 --secs 10
Prints a verdict and appends a JSON line to ~/tt_pinscan.jsonl
"""
import argparse, json, os, statistics, threading, time, datetime
import serial

ap = argparse.ArgumentParser()
ap.add_argument("--label", required=True, help="which contact the probe is on, e.g. pin3")
ap.add_argument("--secs", type=float, default=10.0)
ap.add_argument("--port", default="/dev/serial0")
ap.add_argument("--baud", type=int, default=9600)
ap.add_argument("--gpio", type=int, default=27, help="BCM GPIO on the same node (pin 13)")
ap.add_argument("--no-gpio", action="store_true")
a = ap.parse_args()

# --- UART capture in a thread
uart = bytearray()
stop = threading.Event()
def rx():
    s = serial.Serial(a.port, a.baud, timeout=0.1); s.reset_input_buffer()
    while not stop.is_set():
        uart.extend(s.read(256))
    s.close()
t = threading.Thread(target=rx, daemon=True); t.start()

# --- edge capture on the GPIO
edges = []; start_level = None; end_level = None
if not a.no_gpio:
    import gpiod
    from gpiod.line import Direction, Edge, Bias, Value
    req = gpiod.request_lines("/dev/gpiochip0", consumer="tt_pinscan", event_buffer_size=1024, config={
        a.gpio: gpiod.LineSettings(direction=Direction.INPUT, edge_detection=Edge.BOTH,
                                   bias=Bias.DISABLED)})
    start_level = req.get_value(a.gpio) == Value.ACTIVE
    t_end = time.monotonic() + a.secs
    while time.monotonic() < t_end:
        if req.wait_edge_events(datetime.timedelta(seconds=0.2)):
            for ev in req.read_edge_events():
                edges.append((ev.timestamp_ns, ev.event_type == ev.Type.RISING_EDGE))
    end_level = req.get_value(a.gpio) == Value.ACTIVE
    req.release()
else:
    time.sleep(a.secs)
stop.set(); t.join()

# --- low-pulse widths (falling edge -> next rising edge)
lows = []; fall = None
for ts, rising in edges:
    if not rising: fall = ts
    elif fall is not None: lows.append((ts - fall) / 1e6); fall = None
lines = [l for l in bytes(uart).split(b"\r") if l]
valid = set(b":=!\r0123456789ABCDEF")
frac = sum(b in valid for b in uart) / len(uart) if uart else 0.0
n_cmd = sum(l.startswith(b":") for l in lines); n_rep = sum(l.startswith(b"=") for l in lines)

if frac > 0.8 and n_cmd and n_rep: verdict = "DATA (shared TX/RX line: commands AND replies)"
elif frac > 0.8 and n_cmd:          verdict = "DATA - host->board direction only (separate-line bus)"
elif frac > 0.8 and n_rep:          verdict = "DATA - board->host direction only (separate-line bus)"
elif lows and statistics.median(lows) > 2.0 and frac < 0.5:
    verdict = "DROP-like (low pulses of milliseconds, no readable serial data)"
elif not edges and start_level:     verdict = "steady HIGH, no activity (idle pull-up, or a supply below 5.5 V)"
elif not edges and start_level is False: verdict = "steady LOW (GND, or an asserted/unused line)"
elif not edges and a.no_gpio and not uart: verdict = "no serial data"
else:                               verdict = "activity but unclassified - send the JSON line"

rec = {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
       "label": a.label, "secs": a.secs, "verdict": verdict,
       "uart_bytes": len(uart), "uart_valid_fraction": round(frac, 3),
       "commands": n_cmd, "replies": n_rep, "sample_lines": [l.decode("latin1") for l in lines[:12]],
       "edges": len(edges), "start_high": start_level, "end_high": end_level,
       "low_pulses": len(lows),
       "low_ms_median": round(statistics.median(lows), 3) if lows else None,
       "low_ms_min": round(min(lows), 3) if lows else None,
       "low_ms_max": round(max(lows), 3) if lows else None}
print(f"{a.label}: {verdict}")
print(f"  serial: {len(uart)} bytes, {frac*100:.0f}% protocol chars, {n_cmd} commands, {n_rep} replies")
if not a.no_gpio:
    print(f"  line: starts {'HIGH' if start_level else 'LOW'}, {len(edges)} edges, {len(lows)} low pulses"
          + (f" (median {rec['low_ms_median']} ms, {rec['low_ms_min']}-{rec['low_ms_max']} ms)" if lows else ""))
for l in rec["sample_lines"][:6]: print("   ", repr(l))
with open(os.path.expanduser("~/tt_pinscan.jsonl"), "a") as f:
    f.write(json.dumps(rec) + "\n")
