# TeleTrack MC001: breadboard bench test (2026-11-13)

**Status: plan only, no code changed.** Everything here is read-only towards
the motor board: no command that moves a motor.

**References:**
- wiring values: `2026-11-09-teletrack-aux-cable.html` (v4);
- board facts: `2026-11-09-teletrack-mc001-board-analysis.md` §8.

## Parts

| Part | Notes |
|---|---|
| Solderless breadboard + jumper wires | Female–male for the Pi header |
| SN74LVC2G07 on a SOT-23-6 → DIP breakout | Check the breakout's pin labels against the package pinout |
| Resistors | 2 × 10 kΩ, 1 × 47 kΩ, 1 × 100 kΩ, plus 2 × 10 kΩ for the stage 1 bus simulation only |
| 12 V bench supply with current limit | Or the mount's own battery/DC input |
| Connection to the board, one of: | (a) a 6P6C RJ12 breakout on the mount's **AUX** jack (mount assembled), or (b) a **JST PH 2.0 mm 6-pin** pigtail into the MC001 "HC" header (board on the bench) |
| Multimeter | |

## Breadboard nets

Five nets on the breadboard, plus the Pi pins:

| Net | Joins |
|---|---|
| **3V3** | Pi pin 17 · buffer VCC · top of R1 and R2 |
| **GND** | Pi pin 9 · buffer GND · bottom of R6 · board/bus GND |
| **TXIN** | Pi pin 8 · buffer 1A · bottom of R1 (10 kΩ to 3V3) |
| **DROPIN** | Pi pin 11 · buffer 2A · bottom of R2 (10 kΩ to 3V3) |
| **BUS** | buffer 1Y · top of R5 (47 kΩ) · board RXTX |
| **DROP** | buffer 2Y · board Drop |
| **RXN** | bottom of R5 · top of R6 (100 kΩ) · Pi pin 10 |

Nothing connects to Pi 5 V pins 2/4, except temporarily in stage 1, and
nothing connects to the board's 12 V pin.

## Stage 0: Pi settings (once)

```
grep -H '^enable_uart' /boot/firmware/config.txt
grep -q '^gpio=17=op,dh' /boot/firmware/config.txt || echo 'gpio=17=op,dh' | sudo tee -a /boot/firmware/config.txt
sudo apt install -y python3-libgpiod gpiod
diofinder-ctl raw '{"cmd":"mount_status"}'      # enabled must be false
sudo reboot
```

Then fetch `tt_discover.py`, as in bring-up checklist §0.1.

## Stage 1: prove the interface with no mount attached

Simulate the motor board's pull-ups:
- 10 kΩ from **BUS** to Pi pin 2 (5 V);
- 10 kΩ from **DROP** to Pi pin 2.

**These two resistors exist only for this stage.**

**Checks:**
1. **Idle levels** (meter): BUS ≈ 4.7 V, DROP ≈ 5.0 V, RXN ≈ 3.2 V. If RXN
   is above 3.4 V, stop and recheck R5/R6.
2. **Echo through the buffer.** The Pi should read its own bytes back:
   ```
   sudo /opt/diofinder/venv/bin/python -c "import serial;s=serial.Serial('/dev/serial0',9600,timeout=1);s.write(b':e1\r');print(s.read(4))"
   ```
   Expect `b':e1\r'`. An empty result means the TX → buffer → BUS → divider
   → RX path is broken.
3. **Drop drive.** Hold GPIO17 low and read DROP with the meter. Expect
   ≈ 0 V while held, back to 5 V after Ctrl-C.
   ```
   sudo gpioset -c gpiochip0 17=0      # libgpiod 2.x
   ```
   With libgpiod 1.x: `sudo gpioset --mode=signal gpiochip0 17=0`.

**Then remove both simulation pull-ups and the Pi pin 2 wire.**

## Stage 2: power the board alone

Before connecting the breadboard:
- **Option (a), AUX jack:** mount power switch on, hand controller unplugged.
- **Option (b), bare board:** 12 V to HC pin 4 and GND to pin 2, current
  limit about 200 mA. The logic draws tens of mA, and the motors stay off
  because no motion command is sent.

| Meter on | Expect (vs GND) |
|---|---|
| HC pin 6 / board VCC | 5.0 V (78M05 output) |
| HC pin 3 / AUX RXTX | ≈ 5 V idle (R3 10 kΩ pull-up) |
| HC pin 1 / AUX Drop | ≈ 5 V idle (R4 10 kΩ pull-up) |
| HC pin 4 / AUX +12 V | supply voltage: **mark it, never to the breadboard** |

## Stage 3: connect and listen

Connect GND, BUS → RXTX and DROP → Drop, with the hand controller still
unplugged. Then:

```
sudo /opt/diofinder/venv/bin/python ~/tt_discover.py --listen 10
```

Expect silence: the motor board never speaks unless asked, and there is no
other master. Garbage here means noise or a wiring fault.

## Stage 4: inquiries (read-only)

```
sudo /opt/diofinder/venv/bin/python ~/tt_discover.py --listen 3 --talk --drop 17
```

**What "responds correctly" looks like:**

| Command | Correct response |
|---|---|
| `:e1`, `:e2` | `=XXYY81`: firmware XX.YY (hex bytes), mount code **0x81** (Multi-Function, set by JP1). Both axes should report the same version |
| `:a1`, `:a2` | Counts per revolution, a large stable number. Expect a value consistent with the Multi-Function gearing (gearbox 719.697 × main gear 131 × encoder counts per motor turn) |
| `:b1` | Timer frequency; fixed by the 20 MHz crystal and firmware |
| `:g1`, `:g2` | High-speed ratio (16/32/64) |
| `:f1`, `:f2` | Status: running = false. init_done may be false before `:F`, which is normal and not sent |
| `:j1`, `:j2` | Position near `0x800000` (printed as ~0) after power-up; stable between reads |
| echo | `yes`: single-wire bus confirmed |
| `:j1` round trip | ~15–25 ms at 9600 baud with no failures over 200 reads |

**Error replies:**
- `!0` (unknown command) on `:d`, `:q`, `:D` or `:s` is acceptable: older or
  DC boards lack them.
- `!` on `:e`, `:a`, `:b` or `:j` is not.

**Encoder check, still read-only:**
- **Mount assembled:** loosen nothing, and **don't force the axes** (the
  manual warns against turning the mount by hand). Use the *hand controller*
  instead:
  1. plug it in;
  2. slew a few degrees;
  3. unplug it;
  4. rerun the `:j` reads. The positions should have changed by roughly the
     angle × counts/rev ÷ 360.
- **Bare board with motors connected:** turning a motor shaft by hand a turn
  or two changes `:j` too. That proves the encoder → LM324 → 74HC14 → PIC
  counter path.

## If it doesn't respond

| Symptom | Check |
|---|---|
| Stage 1 echo fails | Buffer orientation/pinout; R1 present; RX divider; `/dev/serial0` free (`sudo fuser -v /dev/serial0`) |
| Stage 4 `no reply` but the echo is present | Drop polarity: the script retries once inverted. Board unpowered, or 5 V missing on HC pin 6 |
| Stage 4 no echo, no reply | BUS not on RXTX (wrong contact). On the RJ12 route, remember the plug and jack numbering mirror each other |
| Replies garbled or every other one fails | Hand controller still plugged in; bus idle level below 4 V (divider too heavy); missing common ground |
