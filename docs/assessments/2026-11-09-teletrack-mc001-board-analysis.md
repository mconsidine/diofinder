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
