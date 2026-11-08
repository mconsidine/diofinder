# Orion TeleTrack: bring-up checklist for a direct link (2026-11-08)

**Status: plan and read-only probe only.** Nothing here moves the mount, and
nothing in diofinder's code changes.

**Assumptions for this plan:**
- The TeleTrack's motor board speaks Sky-Watcher's motor-controller protocol.
- Its firmware is **3.39** (to be confirmed by step 6).

Read with `2026-11-08-teletrack-hand-controller-vs-direct.md`.

**Why the cautious order.** The hand-controller (HC) port on Synta mounts
carries **12 V** as well as the data lines. The Pi's GPIO pins take
**3.3 V only**. So the order is:
1. measure;
2. listen only;
3. then talk with inquiries only.

## 1. Documents to fetch (and upload, if you can't reach them from here)

| # | What | URL / how to find it |
|---|---|---|
| 1 | TeleTrack manual for your model number (9481 GoTo has an RS-232 appendix) | https://www.manualsdir.com/manuals/204941/orion-9481.html?page=13 · https://www.manualslib.com/manual/1266647/Orion-Teletrack-9441.html · Orion support site https://www.telescope.com/ (search the model number) |
| 2 | Sky-Watcher **Motor Controller Command Set** (the `:` protocol) | Search the title on https://www.skywatcher.com/ ; a possible direct path (unverified): https://inter-static.skywatcher.com/downloads/skywatcher_motor_controller_command_set.pdf |
| 3 | Sky-Watcher **SynScan Serial Communication Protocol** (HC commands; comparison only) | Search the title on https://www.skywatcher.com/ |
| 4 | Reference implementations (readable from here, no upload needed) | INDI: https://github.com/indilib/indi/blob/master/drivers/telescope/skywatcherAPI.cpp · NexPlane: https://github.com/nforrester/NexPlane/blob/master/skywatcher.py |
| 5 | Photos | The mount's ports (HC, AUX, power) with any labels; the label with the model number; the connector type (6-pin RJ12 or 8-pin RJ45 plug) |

## 2. Pi Zero 2W pins

On the 40-pin header, pin 1 is the corner pin with the square pad, next to the
SD card end. These are physical pin numbers.

| Pi pin | Signal | Use here |
|---|---|---|
| 1 | 3.3 V | Low-voltage reference for a level shifter (only if the bus is 5 V) |
| 2 or 4 | 5 V | High-voltage reference for a level shifter (only if the bus is 5 V) |
| 3, 5 | GPIO2/3 (I2C) | **Already used by the BNO055**; leave alone |
| 6 (or 9, 14) | GND | To the mount's GND pin |
| 8 | GPIO14 TXD (`/dev/serial0` out) | To the mount's data-in line (step 5) |
| 10 | GPIO15 RXD (`/dev/serial0` in) | From the mount's data-out line |

Notes:
- **Disconnect the Virtuoso's MAX3232 from pins 8/10 first.** The Zero 2W has
  only this one free UART, so only one mount can be wired at a time.
- **Never connect the mount's 12 V pin to anything on the Pi.** The Pi keeps its
  own power supply. Only GND is shared.

### The three possible wirings, chosen by the measurement in step 3

**A. Separate TX and RX lines, bus at 3.3 V**
- Pi pin 8 → 1 kΩ → mount RX.
- Mount TX → 1 kΩ → Pi pin 10.
- Pi pin 6 → mount GND.
- The resistors only limit current if something is wired wrongly.

**B. Separate TX and RX lines, bus at 5 V**
- Use a 2-channel BSS138 bidirectional level-shifter board:
  - LV = Pi pin 1, HV = Pi pin 2, GND to both;
  - Pi pin 8 ↔ LV1, HV1 ↔ mount RX;
  - Pi pin 10 ↔ LV2, HV2 ↔ mount TX.
- Without a shifter, at minimum a divider on the mount-TX → Pi-RX line
  (2.2 kΩ from mount TX to Pi pin 10, 3.3 kΩ from Pi pin 10 to GND, giving
  ≈3.0 V). Pi TX at 3.3 V may not register as "high" on a 5 V CMOS input,
  which is why the shifter is preferred.

**C. One shared data line (half-duplex)**
- INDI's driver checks for this on DC-motor Sky-Watcher boards (the AZ GoTo
  family): the board **echoes every byte sent**. That suggests TX and RX share
  one wire.
- Wiring:
  - Pi pin 10 straight to the data line (through the shifter if 5 V);
  - Pi pin 8 to the data line through a Schottky diode (BAT43/1N5819),
    **cathode (band) toward Pi pin 8**;
  - the mount's pull-up holds the line high.
- The probe script below discards the echo.

## 3. Task list

1. **Identify the port.** Model number, manual, photos (§1). Decide whether the
   Pi goes on the HC port (HC unplugged) or the AUX port:
   - If AUX is a second connection on the motor bus, that's ideal: the HC can
     stay plugged in for listening.
   - If AUX is an ST-4 autoguider port, it's useless at satellite rates.
   - If AUX is a power outlet, it's useless for this.
2. **Get a breakout.** An RJ12 or RJ45 "pass-through" breakout with screw
   terminals, so the HC can stay connected while you measure and listen.
   Optional but very helpful: a ~$10 USB logic analyser (sigrok/PulseView)
   to see which pin each side transmits on.
3. **Measure (Pi not connected).**
   - With the mount **off**: find GND by continuity to the power-jack sleeve.
   - With the mount **on**, HC plugged in through the breakout, measure each
     pin against GND:
     - ~12 V → the supply pin: **tape it off**;
     - 0 V → GND;
     - a steady ~3.3 V or ~5 V → an idle UART line (UART idles high);
     - a line that flickers or dips while you press HC keys → data.
   - Record the pin numbers and the voltage. This picks wiring A, B or C (§2).
4. **Prepare the Pi (over ssh).** Commands in §4.1:
   - check `/dev/serial0`;
   - make sure nothing holds it (diofinder's mount link stays disabled);
   - do a loopback test with a jumper between pins 8 and 10.
5. **Listen only.** Wire **only** GND and mount data → Pi pin 10. Leave Pi pin 8
   disconnected. Run `swsniff.py` (§4.2) while the HC powers up and you press
   keys:
   - lines like `:e1\r`, `=032781\r`, `:j1\r` confirm the protocol, the baud
     rate (try 9600, then 115200) and the levels;
   - the `:e1` reply already shows the firmware version and the mount code.
6. **Talk, inquiries only.** Unplug the HC (one master on the bus) and connect
   Pi pin 8 as in §2. Power-cycle the mount. Run `swprobe.py` (§4.3): firmware
   and mount code, counts per revolution, high-speed ratio, timer frequency,
   status, position, and the `:j1` round-trip time over 200 reads.
7. **Record the results** in the TeleTrack assessment:
   - firmware version;
   - echo yes/no;
   - counts/rev;
   - high-speed ratio;
   - timer frequency;
   - round-trip times;
   - pinout and voltage.

   These set the control-loop design (rate resolution, loop rate) before any
   motion code is written.
8. **Not in this checklist** (needs the safety layer first): `:F` (init done),
   `:G` mode, `:I` period, `:J` start, `:K`/`:L` stop, `:S`/`:H` GoTo.

## 4. Commands (ssh session on the finder)

### 4.1 Prepare and loopback-test the UART

```bash
ls -l /dev/serial0
```

```bash
id -nG | tr ' ' '\n' | grep -x dialout || echo "not in dialout: use sudo below"
```

```bash
diofinder-ctl raw '{"cmd":"mount_status"}'
```
(`enabled` must be false; if not: `diofinder-ctl raw '{"cmd":"mount_set","args":{"enabled":false}}'`)

```bash
sudo fuser -v /dev/serial0
```
(prints nothing when the port is free)

Loopback test: put a jumper wire between pins 8 and 10, nothing else connected.

```bash
sudo /opt/diofinder/venv/bin/python -c "import serial;s=serial.Serial('/dev/serial0',9600,timeout=1);s.write(b'loopback-ok\r');print(s.read(12))"
```
Expect `b'loopback-ok\r'`; remove the jumper afterwards.

### 4.2 Listen-only script

```bash
cat > ~/swsniff.py <<'EOF2'
#!/usr/bin/env python3
"""Listen-only: print what the hand controller and motor board say on the bus.
Wire ONLY the Pi's RX (pin 10) and GND. Usage: swsniff.py [PORT] [BAUD] [SECONDS]"""
import sys, time, serial
port = sys.argv[1] if len(sys.argv) > 1 else "/dev/serial0"
baud = int(sys.argv[2]) if len(sys.argv) > 2 else 9600
secs = float(sys.argv[3]) if len(sys.argv) > 3 else 20
s = serial.Serial(port, baud, timeout=0.2); end = time.monotonic() + secs; line = b""
while time.monotonic() < end:
    c = s.read(1)
    if not c: continue
    line += c
    if c == b"\r":
        print(f"{time.monotonic():10.3f}  {line!r}"); line = b""
EOF2
```

```bash
sudo /opt/diofinder/venv/bin/python ~/swsniff.py /dev/serial0 9600 30
```

### 4.3 Read-only probe script

```bash
cat > ~/swprobe.py <<'EOF2'
#!/usr/bin/env python3
"""Read-only probe of a Sky-Watcher/Synta motor board (":" protocol).
Sends only inquiry commands; never moves the mount. Usage:
  swprobe.py [PORT] [BAUD]      default /dev/serial0 9600"""
import sys, time, serial
port = sys.argv[1] if len(sys.argv) > 1 else "/dev/serial0"
baud = int(sys.argv[2]) if len(sys.argv) > 2 else 9600
s = serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1, timeout=0.3)

def ask(cmd):
    """Send ':<cmd>\\r', return (reply_without_= , round_trip_ms, echoed)."""
    s.reset_input_buffer()
    out = (":" + cmd + "\r").encode()
    t0 = time.monotonic(); s.write(out); s.flush()
    buf = b""; echoed = False
    while True:
        c = s.read(1)
        if not c:
            raise TimeoutError(f"{cmd}: no reply (got {buf!r})")
        buf += c
        if c == b"\r":
            if buf == out:          # DC-motor boards echo the command first
                buf = b""; echoed = True; continue
            break
    ms = (time.monotonic() - t0) * 1000
    if buf.startswith(b"!"):
        raise RuntimeError(f"{cmd}: error reply {buf!r}")
    if not buf.startswith(b"="):
        raise RuntimeError(f"{cmd}: unexpected {buf!r}")
    return buf[1:-1].decode(), ms, echoed

def le24(h):  # 6 hex digits, bytes reversed
    return int(h[4:6] + h[2:4] + h[0:2], 16)

r, ms, echo = ask("e1")
print(f"motor firmware : {int(r[0:2],16)}.{int(r[2:4],16):02d}  mount code 0x{r[4:6]}"
      f"   (raw {r}, {ms:.0f} ms, echo={'yes' if echo else 'no'})")
for ax in (1, 2):
    cpr = le24(ask(f"a{ax}")[0]); hsr = int(ask(f"g{ax}")[0], 16)
    st = int(ask(f"f{ax}")[0], 16); pos = le24(ask(f"j{ax}")[0]) - 0x800000
    print(f"axis {ax}: counts/rev {cpr}  ({cpr/360:.1f}/deg)  high-speed ratio {hsr}  "
          f"position {pos} counts ({pos*360/cpr:+.3f} deg)  status 0x{st:03X} "
          f"[running={bool(st&0x10)} blocked={bool(st&0x20)} init_done={bool(st&0x01)}]")
tf = le24(ask("b1")[0]); print(f"timer frequency: {tf} Hz")
times = []
for _ in range(int(sys.argv[3]) if len(sys.argv) > 3 else 200):
    times.append(ask("j1")[1])
times.sort()
print(f":j1 round trip over {len(times)} reads: min {times[0]:.1f}  median "
      f"{times[len(times)//2]:.1f}  95% {times[int(len(times)*.95)]:.1f}  max {times[-1]:.1f} ms")
EOF2
```

```bash
sudo /opt/diofinder/venv/bin/python ~/swprobe.py /dev/serial0 9600 200
```

How to read the output:
- `motor firmware : 3.39  mount code 0x81` means version 3.39 (the two
  version bytes are hex: `27` = 39).
- The mount code identifies the board family:
  - `0x80`–`0x82` are the GT/MF/114GT DC-motor alt-az boards;
  - `0x90` is the Dob;
  - `0xA5` is the AZ-GTi.
- `echo=yes` means a shared data line (wiring C).
- Counts/rev and the timer frequency set the step-period resolution at
  satellite rates.

Both scripts were checked against a simulated board (a pseudo-terminal that
echoes and answers like a DC-motor controller). They have not been run on real
hardware.

## 5. If something goes wrong

| Symptom | Likely cause |
|---|---|
| Sniffer prints nothing | Wrong pin, wrong baud (try 115200), or AUX is not a bus port |
| Sniffer prints garbage | Wrong baud, or inverted/RS-232 levels (then it is not a TTL port: stop and re-measure) |
| Probe: `no reply` | Pi TX not reaching the bus (wiring C diode reversed?), HC still plugged in, or mount not powered |
| Probe: `!` error replies | The command isn't supported by this board; note which |
| Replies only every other time | Two masters on the bus: unplug the HC |
