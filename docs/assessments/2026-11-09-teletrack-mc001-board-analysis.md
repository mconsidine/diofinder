# TeleTrack motor board: photo analysis (2026-11-09)

**Status: assessment only, from five user photos; nothing measured yet.**

Photos:
1. motor board, solder side;
2. motor board, component side, HC connector at the bottom;
3. the same, rotated 180°;
4. jack board, solder side;
5. jack board, component side.

## 1. What the boards are

### Motor board: silkscreen "SYNTA MC001 Rev. D"

Date code on the MCUs: 1540, i.e. 2015, week 40.

| Ref | Part (marking read) | Role (inferred) | Confidence |
|---|---|---|---|
| U4, U5 | Microchip **PIC16F886-I/SO** (28-pin SOIC) | **One microcontroller per axis.** The protocol's channel `1` (Az) and `2` (Alt) are two separate PICs sharing one serial line. That explains the single-wire bus and the Drop line | High (parts); medium (one-per-axis) |
| U6 | ST **L293DD** | Dual full H-bridge (600 mA/channel): drives the two DC motors, one bridge each | High |
| U3 | TI **LM324A** quad op-amp (14-pin) | Analog conditioning: encoder signal squaring and/or motor-current sensing | Medium |
| U2 | 14-pin SOIC, marking unreadable | Logic or comparator. Possibly the bus interface (joining both PICs' TX onto the shared line) or encoder buffering. **Needs a close-up photo** | Low |
| U1 | DPAK regulator near the HC connector | 12 V → 5 V for the logic | High (role) |
| D1 | SMA diode marked "M1" (1N4001-class) | Reverse-polarity protection on the 12 V input | High |
| C1, C3 | Electrolytics | Input/regulator bulk capacitance | High |
| L1, L2 | Inductors/ferrites | Supply filtering | Medium |
| X1 | HC-49/S crystal | Clock, the source of the `:b` timer frequency | High |
| T1 | 4-lead small package near "SW" | Transistor or switch-input part; marking not readable | Low |
| D3 | Small SOD diode | Unknown | — |
| JP1 | 2-pin jumper (near X1) | Unknown option (bootloader or mode?) | Low |
| JP2, JP3, R23–R25 | Jumpered pin strip beside a "Ver." label | Most likely hardware-version or mount-model straps read by the firmware. Not ICSP: PIC ICSP needs 5 lines | Low–medium |

**Connectors on the motor board:**

| Label | Pins | Use (inferred) |
|---|---|---|
| HC | 6 | Everything from the panel: 12 V in, GND and the serial bus (TX/RX/Drop). It's the board's only external power+data connector |
| Az | 6 | Azimuth DC motor + optical encoder (typically M+, M−, +5 V, GND, encoder A, encoder B) |
| Alt | 6 | Altitude motor + encoder, same pattern |
| SW | 2 | A switch input. Possibly the "level switch" bit in the `:f` status word (B1 of the third digit), or the power switch |

### HC cable

A 6-wire cable colour-coded in **resistor order**:

| Pin | Colour |
|---|---|
| 1 | brown |
| 2 | red |
| 3 | orange |
| 4 | yellow |
| 5 | green |
| 6 | blue |

- **Motor board end:** the HC header's square pad (pin 1) is the brown wire.
- **Jack board end:** the six solder pads beside one jack take the wires in
  the same order. Brown and blue are certain at the two ends; the middle
  order is consistent with the photos.

### Jack board: "DW340708A1"

| Item | Notes |
|---|---|
| 2 × 6P6C (RJ12) jacks | Six pins each, in two staggered rows of three. These are the panel's `HC` and `AUX` jacks |
| 6 cable pads | Beside the right-hand jack (component side) |
| 1 axial resistor | Bands look like brown-black-black-brown-brown = **1 kΩ 1 %**; confirm with a meter |
| 2 "LED" pads | Panel power LED |
| 2-pin "CONTROL" header | Probably the power switch / 12 V input from the panel. On the solder side, cable pad 2 (red) is traced to it |

**Conclusion for the link:**
- There is only one 6-wire cable from the jack board to the motor board, so
  **both panel jacks are taps of the same 6-conductor bus**.
- The AUX jack is a second connection to the motor board's bus, as the manual
  ("not used for normal operation") and the AUX schematic suggest.
- The resistor sits in a path between the two jacks, perhaps in series with
  one AUX line, but most jack-to-pad traces are on the component side under
  the jacks, so the photos can't show which.

## 2. What the photos can't settle

- **Which cable pin is which signal.** The power and data traces from the HC
  header run on the component side and under parts.
- **The jack-pin ↔ cable-pad mapping**, for the same reason.
- **The motor firmware version.** The "Ver." sticker area is blank.
  - **Hint against the 3.39 assumption:** an MC001 with two PIC16F886s is
    the older 8-bit DC-servo board. NexPlane lists the AZ SynScan family as
    having no 3.39 firmware. The command set's "2.xx" high-speed behaviour
    probably applies.
  - Only `:e1` settles it.

## 3. Continuity checks that finish the mapping

Do these with a meter in beep mode, **power off, battery holders removed**.

PIC16F886 SOIC-28 pins that matter:

| Pin | Function |
|---|---|
| 1 | MCLR/Vpp |
| 8, 19 | VSS (GND) |
| 20 | VDD (+5 V) |
| 9, 10 | OSC1, OSC2 (crystal) |
| 17 | RC6/TX |
| 18 | RC7/RX |
| 27 | RB6/ICSPCLK |
| 28 | RB7/ICSPDAT |

Pin 1 is at the dot. Numbering runs down that side, then back up the other.

**A. Motor board HC header (cable colours) → signals:**

| HC pin / colour | Beep to | Meaning if it beeps |
|---|---|---|
| each | U4/U5 pin 8 or 19 | GND |
| each | D1's anode (D1 then feeds U1's input) | +12 V input |
| each | U4 pin 17 **and** U5 pin 17 (directly, or via U2/a resistor) | Bus: board transmit |
| each | U4 pin 18 **and** U5 pin 18 | Bus: board receive |
| each | U4/U5 pin 1, 27 or 28 | A programming or bootloader line (a candidate for **PDL**) |
| each | any U2 pin | Bus interface through U2: note which pin |

- **Shared TX and RX:** if one HC pin beeps to both pin 17 and pin 18 (TX and
  RX joined), the bus is single-wire at the board.
- **Drop:** the HC pin with no direct PIC beep that runs into U2 or a
  transistor is most likely Drop.

**B. Jack board, each jack contact → cable pads:** beep each of the six
contacts of each jack to the six cable pads.
- Record the jack-pin → pad table for both jacks.
- Note which jack pin goes through the 1 kΩ resistor (it will read about
  1 kΩ instead of a beep).

For jack pin numbering, look into the jack with the latch slot down: pin 1
is on the left. Confirm against the square pad on the solder side.

**C. Then, powered** (battery holders back in or 12 V supplied, hand
controller unplugged): DC voltage of each cable pad against GND. Expect:
- 12 V on the supply wire;
- about 5 V idle on TX/RX/Drop.

With A + B + C the AUX pinout is known exactly. The cable (bring-up
checklist §0.2b, `2026-11-09-teletrack-aux-cable.html`) can then be confirmed
or corrected.

## 4. A shortcut the open mount allows

The bus is now physically accessible. The Pi interface can attach to the
6-wire cable (or to the jack board's cable pads) without an RJ12 plug. That
means only GND, TX/RX (or the joint line) and Drop, never the 12 V conductor.
For a first listen-only test, a clip on the identified data wire plus GND is
enough.

## 5. Photos that would help

- A sharp close-up of **U2** and **T1** markings.
- The component side of the jack board **without the cable covering** the
  traces next to the jacks.
- The panel itself, showing which jack is labelled HC and which AUX.

---

## 6. Update from the close-up photos (same day)

### Parts now identified

| Ref | Marking | Part | Role |
|---|---|---|---|
| U2 | `74HC14D` + lot code | **74HC14 hex Schmitt-trigger inverter** | Squares up slow or noisy edges. Most likely the optical-encoder channels (after U3, the LM324 op-amp), and possibly the serial-bus input. Read the U3 → U2 path as the encoder front end |
| T1 | `357T` / `XB` / `LG06`, 4-lead mini-flat, pin-1 dot | **Lite-On LTV-357T optocoupler** (phototransistor output; the second line is its rank/date) | Isolates the **SW** connector. Sky-Watcher boards have a camera **snap/shutter port** switched by the `:O` "Set Aux Switch On/Off" command (INDI calls it `SetSnapPort`). SW → the panel's camera "Cable" jack is the natural reading. The SynScan hand controller's own SHUTTER/COMMON pins are also optoisolated |
| R6 | `561` | 560 Ω | Optocoupler LED current resistor (or output series resistor) |
| D3 | `T4`, SOD-323 | 1N4148WS small-signal diode (the usual "T4" marking) | Single diode near the MCUs. A diode with a 10 kΩ pull-up is the classic way to put a push-pull UART TX onto a shared wire. It may be the bus TX path; confirm with the continuity checks |
| R3, R4, R12, R14, R19, R20 | `103` | 10 kΩ | Pull-ups / biasing |
| R11, R13 | `102` | 1 kΩ | Around the LM324: encoder or current-sense network |

### Jack board silkscreen

| Silkscreen | Jack | Connection |
|---|---|---|
| **AUX** | the jack beside the six cable pads | **Directly to the 6-wire cable**, so straight to the motor board's HC connector |
| **HC** | the jack beside the axial resistor | Through the jack board with the **~1 kΩ resistor** in one of its lines; which line still needs the meter |

So **AUX is the direct, unresistored tap of the motor bus**: the right port
for the Pi. The 1 kΩ on the HC side is probably a series protection resistor
for the hand-controller cable.

### Which TeleTrack this is

The board has a camera snap output (SW via T1), and the jack board has a
"CONTROL" header and LED pads. That matches the **#9441 TeleTrack**:
- its panel has a camera "Cable Jack" for the "Cruise and Shoot" mode;
- its hand controller has no computer port;
- its slew speeds are 32×, 64× and 800× sidereal.

The #9481 AZ-G panel in its manual shows no camera jack. **Confirm from the
label on the mount.**

If it is the 9441, driving the motor board directly through AUX is the
**only** computer route. That is the plan already.

### Still to measure (§3 unchanged)

- The jack-pin ↔ cable-wire table for AUX. Expected straight through, but
  confirm.
- Which HC-jack line carries the 1 kΩ.
- Which cable wire is 12 V, GND, data and Drop. Use the PIC16F886
  continuity checks in §3.

---

## 7. Panel schematic: "Pannel of Synta Multi-Function Mount" (user upload, 2026-11-13)

This sheet is the mount's own jack/power panel, and it settles the pinout.

**Panel jacks.** Both are RJ-11(6-6). The sheet's "RJ11 Pins" drawing numbers
1–6 left to right, looking into the jack with the latch slot down.

| Panel pin | HC jack | AUX jack | Bus net |
|---|---|---|---|
| 1 | PDL | PDL | HC 1 ↔ AUX 1 only, with **R1 5.1 kΩ to GND**. Not wired to the motor board |
| 2 | RXTX | RXTX | Tied to pin 4 on each jack |
| 3 | +12 V | +12 V | After the SPST power switch |
| 4 | RXTX | RXTX | **The single data wire** (motor-board "RXTX") |
| 5 | GND | GND | |
| 6 | Drop | Drop | |

**"MC" connector (JST PHR-6, "To motor controller"):**

| Pin | Signal |
|---|---|
| 1 | Drop |
| 2 | GND |
| 3 | RXTX |
| 4 | +12 V |
| 5 | LED (motor board drives the "Power / Battery Low" LED, D1, to GND) |
| 6 | VCC (not used on the panel) |

**Other parts on the sheet:**
- **Power:** PWR1 Power In jack and a battery box through D2 (1N4001).
- **Power Out:** PWR2 "Power for external device" is wired *before* the
  power switch.
- **Shutter:** a 2.5 mm phone jack to the motor board's **"SW"** connector
  (SW1/SWCOM). This confirms SW is the camera snap output through the
  LTV-357T optocoupler.

**Reconciliation with the accessory-side "AUX INTERFACE" sheet** (1 DROP,
2 GND, 3 TX, 4 +12 V, 5 RX, 6 PDL):
- It is the **exact mirror** (n ↔ 7 − n) of the panel numbering.
- One sheet numbers from the plug face, the other from the jack face.
- Both describe the same contacts.
- The accessory's separate TX and RX pins land on the mount's two RXTX
  contacts, which are the same wire.

So:
- **The cable drawing is physically correct** (plug-side numbering, plug
  pin 1 = DROP = the rightmost contact seen looking into the jack).
- **The bus is single-wire at the mount.** The Pi hears an echo of every
  byte it sends, which `tt_discover.py` already handles.
- **PDL** is a link between the HC and AUX jacks with a 5.1 kΩ pull-down,
  probably presence detection between a hand controller and an AUX device.
  The Pi leaves it unconnected.

**Cable colours, if the jack board's cable follows the MC connector numbering
(brown = 1):**

| Colour | Signal |
|---|---|
| brown | Drop |
| red | GND |
| orange | RXTX |
| yellow | +12 V |
| green | LED |
| blue | VCC |

Check: **yellow should read 12 V** with the switch on, red should be GND.

**Corrections to §1 and §6:**
- The jack board's axial resistor is **R1, 5.1 kΩ** (the bands read
  green-brown-black-brown-brown), the **PDL pull-down**. It is not a series
  resistor on an HC line. Both jacks are wired in parallel; AUX has no
  extra protection compared with HC.
- The red wire traced to the "CONTROL" header on the jack board is GND on
  this sheet. "CONTROL" is probably the power-switch/LED harness. The board
  (DW340708A1) may be a later revision than this sheet: verify with the
  meter.

---

## 8. MC001 board schematic (user upload, 2026-11-13)

The full motor-board schematic confirms and corrects §1–§7.

### HC connector (PH-6)

| Pin | Net | Notes |
|---|---|---|
| 1 | Drop | **10 kΩ pull-up to 5 V (R4)** → RB2 (pin 23) of both PICs, as an input |
| 2 | GND | |
| 3 | RXTX | **10 kΩ pull-up to 5 V (R3)** |
| 4 | Vin (+12 V) | L1 (FEB1A ferrite) → D1 1N4001 → C1 100 µF → U1 **UA78M05CDCYR** (TI 78M05) → VCC 5 V |
| 5 | LED | U4 RC4 (pin 15) through R5 1 kΩ: the panel's power/battery-low LED |
| 6 | VCC | +5 V **out** from the board; the panel leaves it unused |

This matches the panel sheet's "MC" connector pin for pin.

### The single-wire bus

Each PIC16F886:
- **RC6/TX (pin 17)** reaches RXTX through a **1N4148** (D2 for U4, D3 for
  U5), cathode at the PIC: a diode-OR open-drain.
- **RC7/RX (pin 18)** sits directly on RXTX.

Both axis controllers share the one wire. Each hears its own and the other's
transmissions, and so does the Pi (the echo `tt_discover.py` strips).

### Axis identity and straps

- **RB3 (pin 24):** VCC on U4, GND on U5. This is how each PIC knows whether
  it answers channel 1 (Az) or channel 2 (Alt).
- **MCLR:** each has a 10 kΩ pull-up (R19, R20).
- **ICSP:** PGC/PGD on RB6/RB7 (PGC1/PGD1, PGC2/PGD2).
- **PDL:** not on this board (it only links the panel's HC and AUX jacks).

### Clock

- One **20 MHz crystal** X1 (C10/C11 15 pF) on U5.
- U4's OSC1 is driven from U5's OSC2.

### JP1–JP3: the mount-type straps

They go to RA0–RA2, with 47 kΩ pull-ups R23–R25. The table on the sheet:

| Mount | Gearbox | JP1 | JP2 | JP3 | Main gear |
|---|---|---|---|---|---|
| 80GT | 985.8868 | OFF | OFF | OFF | 131 |
| **Multi-Function** | **719.697** | **ON** | OFF | OFF | **131** |
| 114GT | 985.8868 | OFF | ON | OFF | 191 |
| DOB | 985.8868 | OFF | OFF | ON | 160 |

- The photos show a jumper fitted at JP1, so **this board is configured as
  the "Multi-Function" mount**, the TeleTrack (#9441-type).
- That matches the panel sheet's title "Pannel of Synta Multi-Function Mount"
  and INDI's mount code `MF = 0x81`. Expect `:e1` to end in `81`.

### Motors

- U6 L293DD bridges, driven from CCP1/CCP2 PWM:
  - AZPWM± on U4 RC1/RC2;
  - ALTPWM± on U5 RC1/RC2;
  - enables AZEN/ALTEN on RB0.
- Motor supply is +12 V via L2.

**Az and Alt connectors (PH-6):**

| Pin | Az | Alt |
|---|---|---|
| 1 | AZM+ | ALTM+ |
| 2 | AZM− | ALTM− |
| 3 | VCC | VCC |
| 4 | IREDA (encoder IR LED; returns via R21/R22, 0 Ω) | IREDB |
| 5 | IR2A | IR2B |
| 6 | IR1A | IR1B |

### Encoders

Two phototransistor channels per axis:
- LM324 comparators U3A–D, with 1 kΩ/10 kΩ hysteresis networks;
- then 74HC14 Schmitt inverters U2;
- then PA1/PA2 (U4 RC0/T1CKI and RA4/T0CKI) and PB1/PB2 (U5).

So the counts come from the PICs' timer counter inputs.

### Snap

- SWA (U4 RA5) → T1 **LTV-357T** LED, with R6 560 Ω to GND.
- The phototransistor's C/E go to the "SW" connector, then the panel's
  2.5 mm shutter jack.

### Consequences for the Pi interface (cable drawing updated, version 4)

1. **The bus pull-ups are on the motor board** (10 kΩ to 5 V on both RXTX
   and Drop). The interface board's own 5 V pull-ups (old R3/R4) are dropped,
   and with them any connection to the Pi's 5 V. That also avoids back-feeding
   the mount's 5 V rail through the pull-ups when the mount is off.
2. **The RX divider must be light.**
   - The PIC16F886's USART input is a Schmitt trigger needing **VIH ≥
     0.8 VDD = 4.0 V**.
   - The old 10k/20k divider (30 kΩ to GND) would sit against the board's
     10 kΩ pull-up and hold the idle bus at about **3.75 V**: below 4.0 V, so
     the motor board could misread its own bus.
   - **47 kΩ / 100 kΩ** keeps the bus at about 4.7 V and gives about 3.2 V at
     the Pi.
   - A 3.3 V-powered 74LVC1G17 buffer with a 5 V-tolerant input would load it
     even less.
3. **Pi TX must not drive the bus directly.** 3.3 V is below the PIC's 4.0 V
   VIH, and the bus is shared. The open-drain SN74LVC2G07 stays.

The bring-up checklist §0.2–§0.2b still shows the old 10k/20k divider and 5 V
pull-ups. **The cable drawing (`2026-11-09-teletrack-aux-cable.html`,
version 4) is now the reference.**
