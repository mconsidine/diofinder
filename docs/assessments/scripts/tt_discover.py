#!/usr/bin/env python3
"""TeleTrack / Sky-Watcher motor-bus discovery. READ-ONLY: it never sends a
command that moves, initialises or configures the mount.

Phase 0  environment checks (UART, console, GPIO library)
Phase 1  LISTEN: record bus traffic (hand controller plugged in, Pi RX only)
Phase 2  TALK (only with --talk; hand controller UNPLUGGED): inquiry
         commands only, echo/Drop detection, decoding, timing
Writes everything to ~/teletrack_discover_<UTC>.json

  tt_discover.py                         # phase 0 + 20 s listen at 9600
  tt_discover.py --scan-bauds            # also listen at 4800/19200/115200
  tt_discover.py --talk --drop 17        # phase 2, Drop on GPIO17 (pin 11)
"""
import argparse, json, os, re, subprocess, sys, time, datetime

ap = argparse.ArgumentParser()
ap.add_argument("--port", default="/dev/serial0")
ap.add_argument("--baud", type=int, default=9600)
ap.add_argument("--listen", type=float, default=20.0, help="seconds (0 = skip)")
ap.add_argument("--scan-bauds", action="store_true")
ap.add_argument("--talk", action="store_true")
ap.add_argument("--drop", type=int, default=None, help="BCM GPIO for the Drop line")
ap.add_argument("--drop-active-high", action="store_true")
ap.add_argument("--reads", type=int, default=200)
ap.add_argument("--out", default=os.path.expanduser("~"))
ap.add_argument("--force", action="store_true", help="continue despite phase-0 problems")
a = ap.parse_args()
R = {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
     "args": vars(a)}

def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except Exception as e:
        return f"<{e}>"

def save():
    os.makedirs(a.out, exist_ok=True)
    p = os.path.join(a.out, "teletrack_discover_%s.json"
                     % R["utc"].replace(":", "").replace("-", "")[:15])
    with open(p, "w") as f:
        json.dump(R, f, indent=1, default=str)
    print(f"\nreport written: {p}")

# ---------------------------------------------------------------- phase 0
print("== Phase 0: environment")
env = {
    "serial0": sh(f"readlink -f {a.port}"),
    "enable_uart": sh("grep -h '^enable_uart' /boot/firmware/config.txt /boot/config.txt 2>/dev/null"),
    "gpio_cfg": sh("grep -h '^gpio=' /boot/firmware/config.txt /boot/config.txt 2>/dev/null"),
    "serial_console": sh("grep -oE 'console=(serial0|ttyS0|ttyAMA0)[^ ]*' /boot/firmware/cmdline.txt /boot/cmdline.txt 2>/dev/null"),
    "port_users": sh(f"fuser {a.port} 2>&1"),
    "model": sh("tr -d '\\0' < /proc/device-tree/model"),
}
try:
    import serial; env["pyserial"] = serial.VERSION
except ImportError:
    sys.exit("pyserial missing: run with /opt/diofinder/venv/bin/python")
try:
    import gpiod; env["libgpiod"] = getattr(gpiod, "__version__", "present")
except ImportError:
    env["libgpiod"] = None
R["env"] = env
for k, v in env.items():
    print(f"  {k:15s} {v or '-'}")
problems = []
if not env["enable_uart"].endswith("=1"): problems.append("enable_uart=1 missing in config.txt")
if env["serial_console"]: problems.append("a serial console is on the UART (cmdline.txt)")
if env["port_users"].strip(): problems.append("something has the port open: " + env["port_users"])
if a.drop is not None and not env["libgpiod"]: problems.append("--drop needs: sudo apt install python3-libgpiod")
for p in problems: print("  PROBLEM:", p)
R["problems"] = problems
if problems and not a.force:
    save(); sys.exit(1)

# ---------------------------------------------------------------- phase 1
VALID = set(b":=!\r0123456789ABCDEF")
def listen(baud, secs):
    s = serial.Serial(a.port, baud, timeout=0.2)
    s.reset_input_buffer()
    raw = bytearray(); t_end = time.monotonic() + secs; stamps = []
    while time.monotonic() < t_end:
        c = s.read(256)
        if c:
            stamps.append(round(time.monotonic(), 3)); raw += c
    s.close()
    lines = [l for l in bytes(raw).split(b"\r") if l]
    good = sum(1 for b in raw if b in VALID) / len(raw) if raw else 0.0
    cmds = {}
    for l in lines:
        if l.startswith(b":") and len(l) >= 3:
            cmds[l[1:3].decode("latin1")] = cmds.get(l[1:3].decode("latin1"), 0) + 1
    fw = None
    for i, l in enumerate(lines[:-1]):          # ":e1" followed by "=xxxxxx"
        if l in (b":e1", b":e2") and lines[i + 1].startswith(b"=") and len(lines[i + 1]) == 7:
            h = lines[i + 1][1:].decode(); fw = f"{int(h[0:2],16)}.{int(h[2:4],16):02d} code 0x{h[4:6]}"
    return {"baud": baud, "seconds": secs, "bytes": len(raw), "valid_char_fraction": round(good, 3),
            "lines": len(lines), "commands_seen": cmds, "firmware_from_traffic": fw,
            "first_lines": [l.decode("latin1") for l in lines[:60]],
            "raw_hex_head": bytes(raw[:256]).hex()}

if a.listen > 0:
    print(f"\n== Phase 1: listening {a.listen:.0f} s at {a.baud} (press hand-controller keys now)")
    L = [listen(a.baud, a.listen)]
    if a.scan_bauds:
        for b in (4800, 19200, 115200):
            print(f"   ...also {b} baud for 5 s"); L.append(listen(b, 5.0))
    R["listen"] = L
    for r in L:
        print(f"  {r['baud']:6d} baud: {r['bytes']} bytes, {r['lines']} lines, "
              f"protocol-looking chars {r['valid_char_fraction']*100:.0f}%")
        if r["commands_seen"]: print(f"     commands seen: {r['commands_seen']}")
        if r["firmware_from_traffic"]: print(f"     MOTOR FIRMWARE (from traffic): {r['firmware_from_traffic']}")
        for l in r["first_lines"][:15]: print(f"     {l!r}")
    if not L[0]["bytes"]:
        print("  nothing heard: wrong pin, AUX not a bus tap, mount off, or the bus is idle")

# ---------------------------------------------------------------- phase 2
if not a.talk:
    print("\n(phase 2 skipped: add --talk with the hand controller UNPLUGGED)")
    save(); sys.exit(0)

print("\n== Phase 2: inquiry commands (read-only)")
s = serial.Serial(a.port, a.baud, timeout=0.3)
s.reset_input_buffer(); time.sleep(1.5)
if s.in_waiting:
    R["talk_aborted"] = "traffic on the bus: another master (hand controller?) is active"
    print("  ABORT:", R["talk_aborted"]); save(); sys.exit(1)

drop_req = None; polarity = {"high_asserts": a.drop_active_high}
if a.drop is not None:
    from gpiod.line import Direction, Value
    def _lv(x): return Value.ACTIVE if x else Value.INACTIVE
    def drop_set(assert_it):
        lvl = assert_it if polarity["high_asserts"] else not assert_it
        drop_req.set_value(a.drop, _lv(lvl))
    drop_req = gpiod.request_lines("/dev/gpiochip0", consumer="tt_discover", config={
        a.drop: gpiod.LineSettings(direction=Direction.OUTPUT,
                                   output_value=_lv(not polarity["high_asserts"]))})
else:
    def drop_set(assert_it): pass

def ask(cmd, timeout=0.3):
    s.timeout = timeout; s.reset_input_buffer()
    out = (":" + cmd + "\r").encode(); buf = b""; echoed = False
    drop_set(True)
    try:
        t0 = time.monotonic(); s.write(out); s.flush()
        while True:
            c = s.read(1)
            if not c:
                return {"cmd": cmd, "ok": False, "error": "timeout", "partial": buf.decode("latin1"), "echo": echoed}
            buf += c
            if c == b"\r":
                if buf == out: buf = b""; echoed = True; continue
                break
        ms = round((time.monotonic() - t0) * 1000, 2)
    finally:
        drop_set(False)
    r = buf[:-1].decode("latin1")
    return {"cmd": cmd, "ok": r.startswith("="), "reply": r, "ms": ms, "echo": echoed}

first = ask("e1")
if not first["ok"] and a.drop is not None:
    print("  no answer with Drop polarity", "high" if polarity["high_asserts"] else "low",
          "- trying the other polarity once")
    polarity["high_asserts"] = not polarity["high_asserts"]; first = ask("e1")
R["drop_high_asserts"] = polarity["high_asserts"] if a.drop is not None else None
if not first["ok"]:
    R["talk"] = [first]; print("  no reply to :e1 ->", first); save(); sys.exit(1)

def le24(h): return int(h[4:6] + h[2:4] + h[0:2], 16)
INQ = ["e1", "e2", "a1", "a2", "b1", "g1", "g2", "f1", "f2", "j1", "j2", "d1", "d2",
       "i1", "i2", "h1", "h2", "c1", "c2", "m1", "m2", "D1", "s1", "s2",
       "q1000000", "q2000000", "q1010000", "q2010000"]
ERR = {"0": "unknown command", "1": "length", "2": "motor not stopped", "3": "bad char",
       "4": "not initialised", "5": "sleeping", "7": "PEC training", "8": "no PEC data"}
T = []
for c in INQ:
    r = ask(c); T.append(r)
    if r["ok"]:
        d = r["reply"][1:]
        try:
            if c[0] == "e": r["decoded"] = f"firmware {int(d[0:2],16)}.{int(d[2:4],16):02d}, mount code 0x{d[4:6]}"
            elif c[0] in "abcdhimsD" and len(d) == 6:
                v = le24(d); r["decoded"] = v - 0x800000 if c[0] in "jdhm" else v
            elif c[0] == "j" and len(d) == 6: r["decoded"] = le24(d) - 0x800000
            elif c[0] == "g": r["decoded"] = int(d, 16)
            elif c[0] == "f" and len(d) == 3:
                v = int(d, 16)
                r["decoded"] = {"tracking_mode": bool(v & 0x100), "ccw": bool(v & 0x200), "fast": bool(v & 0x400),
                                "running": bool(v & 0x010), "blocked": bool(v & 0x020),
                                "init_done": bool(v & 0x001), "level_switch": bool(v & 0x002)}
        except (ValueError, IndexError):
            r["decoded"] = "(unexpected length: %r)" % d
    elif r.get("reply", "").startswith("!"):
        r["decoded"] = ERR.get(r["reply"][2:3], r["reply"])
    print(f"  :{c:9s} -> {r.get('reply', r.get('error')):10s} {r.get('decoded', '')}")
R["talk"] = T
R["echo_single_wire"] = first["echo"]

get = {r["cmd"]: r.get("decoded") for r in T if r["ok"]}
cpr, tf, hsr = get.get("a1"), get.get("b1"), get.get("g1")
if cpr and tf:
    print(f"\n  counts/rev {cpr} ({cpr/360:.1f}/deg, {1296000/cpr:.3f} arcsec/count), timer {tf} Hz, high-speed ratio {hsr}")
    R["speed_table"] = []
    for dps in (0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0):
        lo = tf * 360 / dps / cpr; hi = lo * (hsr or 1)
        row = {"deg_s": dps, "x_sidereal": round(dps / 0.0041781, 1), "t1_low": round(lo, 1),
               "step_low_pct": round(100 / lo, 3) if lo >= 1 else None,
               "t1_high": round(hi, 1), "step_high_pct": round(100 / hi, 3) if hi >= 1 else None}
        R["speed_table"].append(row)
        print(f"   {dps:4.2f} deg/s ({row['x_sidereal']:6.1f}x): low-speed T1 {lo:9.1f} "
              f"(step {row['step_low_pct']}%)   high-speed T1 {hi:9.1f} (step {row['step_high_pct']}%)")

times = []
for _ in range(a.reads):
    r = ask("j1")
    if r["ok"]: times.append(r["ms"])
times.sort()
if times:
    R["j1_rtt_ms"] = {"n": len(times), "min": times[0], "median": times[len(times)//2],
                      "p95": times[int(len(times)*.95)], "max": times[-1],
                      "failures": a.reads - len(times)}
    print(f"\n  :j1 round trip: {R['j1_rtt_ms']}")
save()
