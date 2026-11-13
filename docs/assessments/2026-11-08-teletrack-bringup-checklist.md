# Orion TeleTrack: bring-up checklist for a direct link (2026-11-08, rev. 3)

**Status: plan and read-only probe only.** Nothing here moves the mount, and
nothing in diofinder's code changes.

**Assumption for this plan:** motor firmware **3.39**. The probe confirms or
refutes it.

Rev. 2 uses three uploaded documents:
- Orion **#9441** TeleTrack manual (IN 295 Rev. C);
- Orion **#9481** TeleTrack AZ-G GoTo manual (IN 348 Rev. A, 2008);
- Sky-Watcher **Motor Controller Command Set**.

**Rev. 2 changes the wiring** (§3): on alt-az mounts the motor bus is a single
shared data line plus a "Drop" line.


> **2026-11-13 correction.** The MC001 schematic shows 10 kΩ pull-ups to 5 V
> on RXTX and Drop *on the motor board*, and a PIC Schmitt input needing
> ≥ 4.0 V. So:
> - use a **47 kΩ / 100 kΩ** RX divider (not 10k/20k);
> - fit **no 5 V pull-ups** on the interface board.
>
> The cable drawing `2026-11-09-teletrack-aux-cable.html` (v4) and
> `2026-11-09-teletrack-mc001-board-analysis.md` §8 supersede the wiring
> values below.

## 0. First contact through the AUX jack (rev. 3: no MAX3232, nothing connected yet)

This section supersedes §4–§5 for a first attempt.

**Script:** `docs/assessments/scripts/tt_discover.py`. It is standalone (not
part of the diofinder package) and read-only.
- **Phase 0:** checks the Pi's UART set-up.
- **Phase 1:** listens to the bus while the hand controller runs, and reads
  the motor firmware version straight from the traffic.
- **Phase 2** (only with `--talk` and the hand controller unplugged): sends
  every *inquiry* command in the Sky-Watcher set, decodes the replies, and
  computes the speed resolution and the round-trip time.
- **Output:** a JSON report in the home directory. Tested against simulated
  boards only.

### 0.1 Pi settings (once, over ssh)

Check the UART is on, with no console on it (`install.sh` normally does
this):

```bash
grep -H '^enable_uart' /boot/firmware/config.txt; grep -o 'console=[^ ]*' /boot/firmware/cmdline.txt; readlink -f /dev/serial0
```

Expect: `enable_uart=1`, only `console=tty1` (or a USB-gadget console), and
`/dev/ttyS0`.

Make GPIO17 (the Drop line) start as a high output at boot, so the buffer
never asserts Drop while the Pi is starting:

```bash
grep -q '^gpio=17=op,dh' /boot/firmware/config.txt || echo 'gpio=17=op,dh' | sudo tee -a /boot/firmware/config.txt
```

```bash
sudo apt install -y python3-libgpiod gpiod
```

```bash
diofinder-ctl raw '{"cmd":"mount_status"}'
```
(`enabled` must be false.)

```bash
sudo reboot
```

Get the script (the repository is public):

```bash
curl -fsSL -o ~/tt_discover.py https://raw.githubusercontent.com/mconsidine/diofinder/claude/fervent-darwin-u1h7y3/docs/assessments/scripts/tt_discover.py
```

### 0.2 The AUX cable

No document gives the AUX pinout, so the cable is made after measuring. Each
contact you find connects like this:

| AUX contact (found by measuring) | Goes to |
|---|---|
| +12 V (reads 8–16 V) | **nothing**: cut or tape it |
| GND (continuity to the power-jack sleeve) | Pi pin 9 (and buffer GND) |
| DATA (idles at 3.3 or 5 V, flickers with traffic) | Listening: divider → Pi pin 10. Talking: also buffer output Y1 |
| DROP (dips low in step with traffic) | Talking only: buffer output Y2 |
| Anything else | nothing |

**Connector:**
- Count the gold contacts: 4 = RJ11, 6 = RJ12, 8 = RJ45.
- Use a plug-to-screw-terminal breakout of that size.
- Numbering: hold the plug with its contacts toward you and the clip
  underneath; pin 1 is on the left.

**Measuring**, with a meter against the mount's GND:
- the mount powered and the hand controller in the `HC` jack, so the bus is
  busy;
- probe each AUX contact.

If no AUX contact flickers while you press hand-controller keys, AUX isn't a
bus tap. Repeat the measurement on the `HC` jack through an RJ-45 pass-through
breakout instead, and use the same table.

### 0.2a Finding DATA and DROP

The buffer's A1/A2 are its *inputs*, wired to the Pi. The AUX contacts to
find are the ones for its *outputs*: **DATA → Y1** and **DROP → Y2**.

**Step 1: meter only.** Mount on, hand controller in `HC`, black probe on the
mount's GND (the power-jack sleeve). For each AUX contact, note the DC
voltage:
- 8–16 V → +12 V supply. **Mark it; it must never touch the probe below.**
- 0 V and continuity to GND with power off → GND.
- 3.3 or 5 V → candidate signal. Press direction keys: a reading that wavers
  slightly is a good sign, but a meter can't tell DATA from DROP.
- Anything above 5.5 V that isn't the supply → **stop and report it.**

**Step 2: one probe lead into the Pi.** Two resistors, no chip:

```
probe lead ── 10k ──┬── Pi pin 10 (RXD)
                    ├── Pi pin 13 (GPIO27)
                    └── 20k ── Pi pin 9 (GND)
mount GND ──────────────────── Pi pin 9
```

Touch the probe lead only to contacts from step 1 that read 5.5 V or less.
For each candidate, with the hand controller plugged in and direction keys
pressed during the run:

```bash
curl -fsSL -o ~/tt_pinscan.py https://raw.githubusercontent.com/mconsidine/diofinder/claude/fervent-darwin-u1h7y3/docs/assessments/scripts/tt_pinscan.py
```

```bash
sudo /opt/diofinder/venv/bin/python ~/tt_pinscan.py --label pin3 --secs 10
```

**How the script decides** (it records serial bytes on pin 10 and edge
timings on GPIO27):

| Verdict | Meaning |
|---|---|
| `DATA (shared TX/RX line…)` | Readable `:` commands **and** `=` replies on one contact: the single-wire bus. Goes to **Y1** |
| `DATA - host->board only` / `board->host only` | Two separate contacts: an EQ-style bus with separate lines. Then wiring §3's "separate TX and RX" variant applies |
| `DROP-like` | Low pulses lasting milliseconds (one command plus its reply), no readable serial data. Goes to **Y2** |
| `steady HIGH` / `steady LOW` | Idle pull-up, supply or GND; not used |

Each run appends a line to `~/tt_pinscan.jsonl`; send that file back.
Tested against a simulated bus (serial side only). The GPIO edge capture uses
the libgpiod v2 API and is untested on hardware.

### 0.2b AUX pinout from a SynScan AUX-interface schematic (2026-11-09)

The user supplied a schematic page titled "AUX INTERFACE". Its note says:
- part number 51562-83 rev B;
- the AUX pinout "is now consistent with motor board's modular (RJ25)
  connector";
- "previous revisions have incorrect AUX pinout".

The board it comes from is a bus **master** (it has its own 3.3 V
microcontroller), such as a hand-controller-class device. It is not the
motor board.

**Connector J3: 6-pin modular (RJ25/RJ12)**

| Pin | Net | On that board |
|---|---|---|
| 1 | DROP | Open-drain: U8A (74HCT2G125, input tied to GND) pulls it low only while the board's `DROP_OUT` is low. 100 kΩ pull-up to 5 V (R5). Also read back by the microcontroller (sees another master holding the bus) |
| 2 | GND | Ground |
| 3 | AUX.TX | U8B (74HCT2G125) drives the board's transmit data onto this pin **only while `DROP_OUT` is asserted**; tri-state otherwise. 5 V push-pull while enabled |
| 4 | DIST_12V+ | 12 V supply. **Never to the Pi** |
| 5 | AUX.RX | Straight to the microcontroller's receive input; 100 kΩ pull-up to 5 V (R8) |
| 6 | PDL | Straight to the microcontroller; function not shown (unknown). Leave unconnected |

All four signals pass through ferrite beads (FB3–FB6) and a VEMI45AA
four-channel EMI/ESD filter (U6).

**What it shows about the bus:**
- **Logic level:** 5 V (HCT drivers, 5 V pull-ups). The Pi needs the RX
  divider and an open-drain or level-shifted TX.
- **Who drives:** a master drives TX only between asserting and releasing
  DROP; otherwise its TX floats. So a host may drive the line high while it
  holds DROP. The open-drain buffer plus a pull-up gives the same result.
- **DROP is shared busy/arbitration** between masters: each master pulls it
  low and reads it.
- **TX and RX are separate pins at the connector.** Whether they are joined
  inside the motor board (the command set's "most Alt/Az mounts") shows up in
  the pin scan:
  - commands **and** replies on both pins 3 and 5 → joined;
  - commands only on pin 3 and replies only on pin 5 → separate.

**Applied to the TeleTrack's AUX jack** (still to be confirmed with the meter
and the pin scan, since earlier revisions differed):
- Pin 4 should read 8–16 V.
- Pin 2 should show continuity to the power-jack sleeve.
- Pins 1, 3 and 5 should idle near 5 V.

| AUX pin | Connect to |
|---|---|
| 1 DROP | Buffer Y2, plus 100 kΩ pull-up to Pi pin 2 (5 V) |
| 2 GND | Pi pin 9 |
| 3 AUX.TX (data into the motor board) | Buffer Y1, plus 10 kΩ pull-up to Pi pin 2 (5 V) |
| 4 +12 V | nothing |
| 5 AUX.RX (data from the motor board) | 10 kΩ → Pi pin 10; Pi pin 10 → 20 kΩ → GND |
| 6 PDL | nothing |

The pull-ups go on the buffer's *outputs*, which tolerate 5 V. Nothing at
5 V reaches a Pi pin.

The buffer inputs are unchanged:
- A1 = Pi pin 8 (TX), with 10 kΩ to 3.3 V;
- A2 = Pi pin 11 (GPIO17, Drop), with 10 kΩ to 3.3 V;
- VCC = Pi pin 17; GND = Pi pin 9.

**Pin scan with this pinout:** probe pins 1, 3, 5 and 6 only. Never probe
pin 4.

**Corroboration (2026-11-09).** The open-source SkyWatcherWiFi adapter
(github.com/vlaate/SkyWatcherWiFi, `schematic.png`) wires the older
Sky-Watcher 6-position serial jack (four contacts, an "RJ11" in a 6P body)
as:
- position 2 GND;
- position 3 "to TX" (driven by the adapter's TX);
- position 4 VCC 12 V;
- position 5 "to RX" (to the adapter's RX);
- positions 1 and 6 not connected.

That matches AUX pins 2–5 here exactly. The AUX connector looks like the
standard Sky-Watcher serial jack, with DROP and PDL added on the two outer
positions.

**Sky-Watcher manuals (read 2026-11-09):** the SynScan hand controller's
RJ-45 mount port is:
- 1 GND;
- 3 DROP;
- 5 RX(TTL);
- 6 TX(TTL);
- 8 Vpp+ (the supply rail).

The manual also says "all Alt-azimuth mounts" have a 6-pin RJ-12 hand-control
port. So the AUX signal set matches what the hand controller uses. Details:
`2026-11-09-synscan-docs-findings.md`.

**Not found:** the rest of the schematic set ("NXW210", "51562-83"). Neither
number has any public match. The Sky-Watcher download site and the usual
forums (Stargazers Lounge, IceInSpace, Astropolis) are not reachable from
this sandbox.

**Listening hardware (phase 1):** two resistors, no chip.
- DATA → 10 kΩ → Pi pin 10;
- Pi pin 10 → 20 kΩ → Pi pin 9 (GND).

That divider gives 3.3 V from a 5 V bus. On a measured 3.3 V bus it gives
2.2 V, still a valid high.

**Talking hardware (phase 2):** an SN74LVC2G07 (dual open-drain buffer,
breakout board):

| Buffer pin | Connection |
|---|---|
| VCC | Pi pin 17 (3.3 V) |
| GND | Pi pin 9 |
| A1 | Pi pin 8 (TXD), plus 10 kΩ to 3.3 V |
| Y1 | AUX DATA |
| A2 | Pi pin 11 (GPIO17), plus 10 kΩ to 3.3 V |
| Y2 | AUX DROP |

Keep the listening divider in place. Pins 1, 3, 5 and 6 stay with the BNO055.

### 0.3 Runs

1. **Environment and listening**, hand controller plugged in. Press direction
   keys during the 30 s, and power the hand controller up during it if
   possible:

```bash
sudo /opt/diofinder/venv/bin/python ~/tt_discover.py --listen 30 --scan-bauds
```

2. **Talking** (buffer fitted, hand controller **unplugged**, mount
   power-cycled):

```bash
sudo /opt/diofinder/venv/bin/python ~/tt_discover.py --listen 5 --talk --drop 17
```

The script refuses to talk if it hears traffic (another master). If `:e1`
gets no answer with Drop active-low, it tries the other polarity once.

3. **Send back** the `~/teletrack_discover_*.json` files. They hold:
   - the environment;
   - the raw traffic (first 60 lines plus hex);
   - every inquiry reply, decoded;
   - echo (single-wire) yes/no;
   - the Drop polarity that worked;
   - the speed-resolution table;
   - round-trip statistics.

   That is enough to choose the control path and the loop rate.

## 1. What the documents settle

### Both TeleTrack models

- **Mount panel:** `HC` jack, `AUX` jack ("not used for normal operation"),
  `12V DC IN`, `12V DC OUT`, power switch. The 9441 also has a camera
  "Cable"/snap jack.
- **Power:** 12 V nominal (8–16 V), tip positive; or 8 × AA inside.

### 9481 TeleTrack AZ-G (GoTo)

- **HC cable:** RJ-45 at the mount's `HC` jack.
- **Computer port on the hand controller:** a separate **RJ-12 RS-232 port**
  (9600 8N1), Celestron NexStar 5i/8i–compatible commands. Pinout (manual,
  Appendix B), named from the PC's side:

  | RJ-12 pin | Signal |
  |---|---|
  | 1, 4, 6 | NC |
  | 2 | RD: the hand controller *transmits* here |
  | 3 | GND |
  | 5 | TD: the hand controller *receives* here |

- **Variable-rate command** (path A): 8 bytes `80, 3, 16|17, 6|7, hi, lo, 0, 0`
  (axis 16 = azimuth, 17 = altitude; 6 = positive, 7 = negative). The rate is
  in **¼″/s** units, 16-bit (up to ~4.5°/s); the reply is `#` (35).
- **Other commands:** 24-bit "slow GoTo" and "set position" commands.
- **Utility → PC Direct:** the hand controller passes the computer's bytes
  straight to the motor board. It is used for "motor control board firmware
  upgrades (v. 2.0 or later)", so the board's firmware can be upgraded through
  the hand controller.
- **Motors:** DC servo with optical encoders, **1,452,425 counts/rev**
  (0.89″/count). Top slew rate 1000× sidereal (~4.2°/s).
- **Hand controller's default altitude slew limit: +60° to −15°**, adjustable
  under "Set Slew Limits". Matters for path A: passes above 60° need it
  raised.

### 9441 TeleTrack (not GoTo)

- **Hand controller:** GUIDE/SLOW/FAST (32×, 64×, 800× sidereal) and "cruise"
  presets. **No computer port.**
- **So on a 9441 the only computer route is direct to the motor bus** (`HC` or
  `AUX` jack), assuming it is the same Synta board family.

### Sky-Watcher command set, §6 "Hardware"

- UART 9600, 8 data bits, 1 stop, no parity.
- **Signal level 5 V or 3.3 V.**
- *"On most of the Alt/Az mount, TX and RX lines are connected together, and
  there is another line (Drop) to indicate that the TX/RX bus is busy. The Drop
  line is controlled by the master only … pull the Drop line to low level when
  it starts to send a command and keep pulling it low until it receives the full
  response … or a time-out occurs … release the TX/RX bus as soon as possible
  after the last bit of the command is shifted out."*
- *"The motor controller pulls its TX line to high level with a 5.1K to 10K
  resistor … other devices can pull the TX line to low level without
  problem."* So the host must drive the bus **open-drain**: pull low, never
  drive high.
- **Firmware note:** 2.xx boards move "up to 32 steps per interrupt" at high
  speed; 3.xx always 1. The command table marks `:I` (step period) as "not
  supported … when motor is slewing in high speed mode". NexPlane reports
  on-the-fly changes working on ≥ 3.39. Confirm on this board before relying on
  it.
- **Error codes:** 0 unknown command, 1 length, 2 motor not stopped, 3 invalid
  character, 4 not initialised, 5 driver sleeping.

**Open:** the RJ-45 `HC` jack's pinout (which pins carry 12 V, GND, data and
Drop) is in none of the documents. It has to be measured (§4 step 3). The
`AUX` jack is probably a second tap of the same bus (it exists on the non-GoTo
9441 too), but that is unconfirmed.

## 2. Which model do you have?

| Model | First connection (no new hardware) | Direct link |
|---|---|---|
| **9481** | Today's MAX3232 cable to the hand controller's **RJ-12** port, hand controller in **PC Direct** mode. The probe script talks to the motor board *through* the hand controller and reads the firmware version with zero risk to the 12 V bus | §3 wiring on the mount's `HC` (or `AUX`) jack |
| **9441** | None: no computer port | §3 wiring is the only route |

## 3. Wiring for the direct link (single-wire bus + Drop)

### Pi Zero 2W pins (physical numbers; pin 1 is the square pad)

| Pi pin | Signal | Use |
|---|---|---|
| 1 | 3.3 V | **Already used by the BNO055** |
| 3, 5 | I2C | **Already used by the BNO055** |
| 6 | GND | **Already used by the BNO055** |
| 8 | GPIO14 TXD | Bus data out (through the open-drain buffer) |
| 9 or 14 | GND | Mount GND and buffer GND |
| 10 | GPIO15 RXD | Bus data in |
| 11 | GPIO17 | Drop line (through the open-drain buffer) |
| 17 | 3.3 V | Buffer supply |

### Open-drain interface (works for a 3.3 V or a 5 V bus)

Use one **SN74LVC2G07** (dual open-drain buffer, 5 V-tolerant outputs,
powered at 3.3 V; a breakout board is easiest). A **SN74LVC07A** (six
buffers) also works.

```
Pi pin 17 (3.3V) ── VCC   SN74LVC2G07   GND ── Pi pin 9
Pi pin 8  (TXD)  ── A1        Y1 ──────────┬──────── mount DATA (TX/RX bus)
                                           │
Pi pin 10 (RXD)  ───────── 10k ────────────┤  (see RX note below)
Pi pin 11 (GPIO17) ── A2      Y2 ──────────────────── mount DROP
Pi pin 9 (GND) ──────────────────────────────────────── mount GND
mount +12V ── NOT CONNECTED (tape it off)
```

How it behaves:
- **TX:** Pi TX low → Y1 pulls the bus low. Pi TX high → Y1 lets go, and the
  mount's own 5.1–10 kΩ pull-up raises the bus. The Pi never drives the bus
  high, as the command set requires. The board echoes nothing; you simply read
  your own bytes back, because RX listens on the same wire. The probe discards
  them.
- **Drop:** GPIO17 low → Y2 pulls Drop low (asserted). GPIO17 high → released.
  This is the probe's default polarity.
- **RX:**
  - **Bus at 3.3 V:** the 10 kΩ series resistor alone is enough.
  - **Bus at 5 V:** replace it with a divider: 10 kΩ from the bus to pin 10,
    and 20 kΩ from pin 10 to GND (gives 3.3 V). Or use a **74LVC1G125** buffer
    powered at 3.3 V (its input tolerates 5 V).
  - **Never connect a 5 V bus straight to pin 10.**
- **If the Drop line turns out to need driving high** (it idles low with no
  pull-up), stop: re-measure before adapting anything.

If step 3 instead finds **separate TX and RX lines** (an "EQ-style" bus), use
the buffer only on TX, wire RX as above, and leave Drop unused (`--drop`
omitted).

### For the 9481 hand-controller route (§2)

Today's MAX3232 wiring is unchanged:
- MAX3232 RS-232 output → RJ-12 **pin 5**;
- RJ-12 **pin 2** → MAX3232 RS-232 input;
- RJ-12 **pin 3** → GND.

Leave pins 1, 4 and 6 unconnected.

## 4. Task list

1. **Model.** Read the label: 9441 or 9481. On a 9481, note the hand
   controller's firmware: Utility → Show Information → Version. It also shows
   the **motor controller version** while the hand controller is connected to
   the mount, which answers the 3.39 question with no wiring at all.
2. **9481 only: read the motor board through the hand controller.**
   - Connect today's MAX3232 cable to the hand controller's RJ-12 port.
   - Select Utility → PC Direct.
   - Run the probe with no `--drop` (§5.3).
   - Press ESC on the hand controller to leave PC Direct afterwards.
3. **Measure the mount's `HC` and `AUX` jacks** through an RJ-45 pass-through
   breakout with screw terminals (Pi not connected):
   - **Mount off:** find GND by continuity to the power-jack sleeve (the
     outside, since the tip is positive).
   - **Mount on, hand controller plugged in through the breakout:** measure
     every pin against GND:
     - ~12 V (or the battery voltage) is the supply: **tape it off**;
     - a pin idling at 3.3 V or 5 V that flickers when keys are pressed is the
       data bus;
     - a pin that dips low in step with that traffic is Drop.

     A cheap USB logic analyser (sigrok/PulseView) makes this certain.
   - Repeat on `AUX` and note whether it mirrors `HC`.
4. **Build the interface** (§3), matching the measured voltage.
5. **Prepare the Pi** (§5.1):
   - nothing holding `/dev/serial0`;
   - diofinder's mount link disabled;
   - a loopback test with the buffer disconnected;
   - libgpiod present.
6. **Listen** (§5.2):
   - Connect **only** GND and the data line (through the RX resistor or
     divider) to pin 10, with the hand controller plugged in.
   - Run the sniffer while the hand controller boots and you press keys. You
     should see `:e1`, `=…`, `:j1`… at 9600 baud.
   - This confirms the bus and the levels, and shows the firmware reply.
7. **Talk, inquiries only** (§5.3):
   - Unplug the hand controller (one master) and connect the full interface.
   - Power-cycle the mount, then run the probe with `--drop 17`.
   - The probe prints: firmware, mount code, counts/rev, high-speed ratio,
     timer frequency, status, position, the speed step at 0.25–2°/s in both
     speed modes, and the round-trip time.
8. **Record the results** in the TeleTrack assessment.
9. **Not in this checklist:** anything that moves the mount (`:F :G :I :J :K
   :L :S :H`); it needs the safety layer first.

## 5. Commands (ssh session on the finder)

### 5.1 Prepare

```bash
diofinder-ctl raw '{"cmd":"mount_status"}'
```
(`enabled` must be false; if not: `diofinder-ctl raw '{"cmd":"mount_set","args":{"enabled":false}}'`)

```bash
sudo fuser -v /dev/serial0
```

```bash
sudo apt install -y python3-libgpiod gpiod
```

```bash
gpioinfo gpiochip0 | grep -E "line +17:"
```
(should show GPIO17 unused)

Loopback: jumper between Pi pins 8 and 10 only.

```bash
sudo /opt/diofinder/venv/bin/python -c "import serial;s=serial.Serial('/dev/serial0',9600,timeout=1);s.write(b'loopback-ok\r');print(s.read(12))"
```

### 5.2 Listen-only script

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

### 5.3 Read-only probe script

```bash
cat > ~/swprobe.py <<'EOF2'
#!/usr/bin/env python3
"""Read-only probe of a Sky-Watcher/Synta motor board (":" protocol).
Sends only inquiry commands (e a g f j b); never moves the mount.

  swprobe.py [--port /dev/serial0] [--baud 9600] [--drop GPIO] [--reads 200]

--drop GPIO: BCM number of the Pi pin wired (through an open-drain buffer)
to the bus "Drop" line. The line is asserted for each command+reply, as the
Sky-Watcher command set requires on single-wire alt-az buses. Omit it on a
board with separate TX/RX lines."""
import argparse, sys, time, serial

ap = argparse.ArgumentParser()
ap.add_argument("--port", default="/dev/serial0")
ap.add_argument("--baud", type=int, default=9600)
ap.add_argument("--drop", type=int, default=None)
ap.add_argument("--drop-active-high", action="store_true",
                help="Pi pin HIGH asserts Drop (wiring through an inverting transistor)")
ap.add_argument("--reads", type=int, default=200)
a = ap.parse_args()
s = serial.Serial(a.port, a.baud, bytesize=8, parity="N", stopbits=1, timeout=0.3)

drop = None
if a.drop is not None:
    import gpiod                                   # python3-libgpiod (v2 API)
    from gpiod.line import Direction, Value
    on, off = ((Value.ACTIVE, Value.INACTIVE) if a.drop_active_high
               else (Value.INACTIVE, Value.ACTIVE))
    drop = gpiod.request_lines("/dev/gpiochip0", consumer="swprobe", config={
        a.drop: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=off)})
    def assert_drop(x): drop.set_value(a.drop, on if x else off)
else:
    def assert_drop(x): pass

def ask(cmd):
    """Send ':<cmd>\\r'; return (data, round_trip_ms, echoed)."""
    s.reset_input_buffer()
    out = (":" + cmd + "\r").encode()
    buf = b""; echoed = False
    assert_drop(True)
    try:
        t0 = time.monotonic(); s.write(out); s.flush()
        while True:
            c = s.read(1)
            if not c:
                raise TimeoutError(f"{cmd}: no reply (got {buf!r})")
            buf += c
            if c == b"\r":
                if buf == out:        # single-wire bus: our own bytes come back first
                    buf = b""; echoed = True; continue
                break
        ms = (time.monotonic() - t0) * 1000
    finally:
        assert_drop(False)
    if buf.startswith(b"!"):
        raise RuntimeError(f"{cmd}: error reply {buf!r} (0 unknown cmd, 1 length, "
                           "2 not stopped, 3 bad char, 4 not initialised, 5 sleeping)")
    if not buf.startswith(b"="):
        raise RuntimeError(f"{cmd}: unexpected {buf!r}")
    return buf[1:-1].decode(), ms, echoed

def le24(h):  # 24-bit value sent low byte first: 0x123456 -> "563412"
    return int(h[4:6] + h[2:4] + h[0:2], 16)

r, ms, echo = ask("e1")
print(f"motor firmware : {int(r[0:2],16)}.{int(r[2:4],16):02d}  mount code 0x{r[4:6]}"
      f"   (raw {r}, {ms:.0f} ms, echo={'yes: single-wire bus' if echo else 'no'})")
for ax in (1, 2):
    cpr = le24(ask(f"a{ax}")[0]); hsr = int(ask(f"g{ax}")[0], 16)
    st = int(ask(f"f{ax}")[0], 16); pos = le24(ask(f"j{ax}")[0]) - 0x800000
    print(f"axis {ax}: counts/rev {cpr}  ({cpr/360:.1f}/deg)  high-speed ratio {hsr}  "
          f"position {pos} counts ({pos*360/cpr:+.3f} deg)  status 0x{st:03X} "
          f"[running={bool(st&0x10)} blocked={bool(st&0x20)} init_done={bool(st&0x01)}]")
tf = le24(ask("b1")[0]); print(f"timer frequency: {tf} Hz")
cpr1 = le24(ask("a1")[0])
for dps in (0.25, 0.5, 1.0, 2.0):
    lo = tf * 360 / dps / cpr1
    print(f"  {dps:4.2f} deg/s: T1 preset {lo:8.1f} (1 count = {100/max(lo,1):.2f}% speed step)"
          f"  high-speed: {lo*hsr:9.1f} ({100/max(lo*hsr,1):.3f}%)")
times = sorted(ask("j1")[1] for _ in range(a.reads))
print(f":j1 round trip over {len(times)} reads: min {times[0]:.1f}  median "
      f"{times[len(times)//2]:.1f}  95% {times[int(len(times)*.95)]:.1f}  max {times[-1]:.1f} ms")
EOF2
```

9481 through the hand controller in PC Direct (MAX3232 cable, no Drop):

```bash
sudo /opt/diofinder/venv/bin/python ~/swprobe.py --port /dev/serial0
```

Direct on the mount's bus (§3 interface, Drop on GPIO17):

```bash
sudo /opt/diofinder/venv/bin/python ~/swprobe.py --port /dev/serial0 --drop 17
```

Reading the output:
- `motor firmware : 3.39 mount code 0x81`: the two version bytes are hex
  (`27` = 39).
- The mount code identifies the board family:
  - `0x80`–`0x82` are the GT/MF/114GT DC-servo alt-az family;
  - `0x90` is the Dob;
  - `0xA5` is the AZ-GTi.
- `echo=yes` confirms the single-wire bus.
- The speed-step lines show how finely each speed mode can set a satellite
  rate. For the 9481's 1,452,425 counts/rev, high-speed mode gives sub-0.5 %
  steps at 0.25–2°/s. Low-speed mode at 1–2°/s is coarse.

Both scripts were checked against a simulated single-wire board (a
pseudo-terminal that echoes and answers). The Drop handling uses the libgpiod
v2 Python API; it, and everything else, still needs a run on real hardware.

## 6. If something goes wrong

| Symptom | Likely cause |
|---|---|
| Sniffer prints nothing | Wrong pin, or AUX is not a bus tap; try 115200 only if the docs above prove wrong |
| Sniffer prints garbage | Wrong baud, or not a TTL bus (re-measure; RS-232 swings negative) |
| Probe: `no reply` | Drop polarity (try `--drop-active-high` only if your Drop wiring inverts), hand controller still plugged in, buffer unpowered, mount off |
| Probe: `!4` (not initialised) on `:j`/`:f` | Normal on some boards before `:F` (init done); inquiries `:e :a :b :g` still answer. Do not send `:F` yet |
| Replies every other time | Two masters on the bus (hand controller still connected), or Drop not asserted |
| PC Direct (9481) gives no reply | The hand controller's firmware may not relay all commands; note the hand-controller version |
