# TeleTrack MC001: checking the PICs with a PICkit 3 (2026-11-13)

**Status: plan only.** Strictly read-only: identify and read the chips, never
erase or program them.

## 0. Read this first

- **Don't erase or program.**
  - The Synta motor firmware is almost certainly **code-protected**, and no
    image of it is in hand.
  - Any Erase or Program operation, including Program after an accidental
    Erase, **wipes the firmware for good**.
  - Use only **Connect, Read, Blank Check and Verify-against-a-saved-read**.
- **Check over the bus first; it's safer.** On this board, U4 answers the
  axis-1 commands (`:e1`, `:j1`, …) and U5 the axis-2 ones (RB3 strap: U4 = VCC,
  U5 = GND).
  - Both answering in `tt_discover.py --talk` already proves both PICs run
    their firmware, which ICSP can't show.
  - Use the PICkit when one axis is silent, or to back up readable memory.

## 1. ICSP pins

Both chips are PIC16F886-I/SO (SOIC-28, wide body). The board has **no ICSP
header**: the schematic's PGC/PGD nets only end in labels.

| PICkit 3 pin | Signal | U4 (axis 1 / Az) | U5 (axis 2 / Alt) |
|---|---|---|---|
| 1 (▶ arrow) | MCLR/Vpp | pin 1 (MCLR1, 10 kΩ R19 to VCC) | pin 1 (MCLR2, R20) |
| 2 | VDD (sense only, §2) | pin 20, or board VCC (HC pin 6) | same |
| 3 | GND | pin 8 or 19, or HC pin 2 | same |
| 4 | PGD / ICSPDAT | pin 28 (RB7, "PGD1") | pin 28 ("PGD2") |
| 5 | PGC / ICSPCLK | pin 27 (RB6, "PGC1") | pin 27 ("PGC2") |
| 6 | PGM (LVP) | **leave unconnected** | **leave unconnected** |

**Pins 1, 27 and 28 sit together at the dot end of each chip:**
- pin 1 is first on the dot side;
- pins 28 and 27 face it on the opposite side.

So three short wires (or a SOIC-28 test clip) cover the signal lines. Power
and GND can come from the HC connector instead.

**Why LVP must stay off:** U4's RB3 is the PGM pin, and it is **tied to VCC**
as the axis strap. Use high-voltage programming mode (the PICkit 3 default).
Never select "low-voltage programming".

## 2. Power and settings

**Power:**
- Power the board as in normal use: 12 V to HC pin 4 (or the mount switched
  on), hand controller unplugged.
- **Don't power the target from the PICkit:** the 78M05 rail also feeds the
  other PIC, the LM324, the 74HC14 and the encoder LEDs.

**MPLAB IPE settings:**
- **Device:** PIC16F886.
- **Tool:** PICkit 3.
- **Advanced Mode → Power:** *"Power target circuit from Tool" = OFF*; VDD 5.0.
- **Program Speed:** Low, which is gentler on hand-tacked wires.
- If your IPE release no longer lists PICkit 3 (Microchip has been retiring
  it), use an older MPLAB X/IPE release that does, or the community
  "PICkitminus" software.

**Side effect while connected:** the two PICs share one crystal (U4's OSC1
comes from U5's OSC2). Holding **U5** in programming mode also stops U4's
clock. Harmless, but don't expect the other axis to answer on the bus at the
same time.

## 3. Procedure (per chip: do U4, power-cycle, then U5)

1. **Connect.** IPE should report the target found, PIC16F886
   (device ID ≈ 0x206x; the low bits are the silicon revision), and VDD
   ≈ 5.0 V.
2. **Read.** Program memory, configuration word(s), user ID and EEPROM.
   **Export → Hex** and save as `U4_read.hex` / `U5_read.hex`.
3. **Note the configuration word.**
   - `CP` and `CPD` show code and data protection.
   - With CP on, program memory reads back as zeros. That is expected, not a
     fault.
4. **Note the checksum** IPE shows after the read.
5. **Disconnect**, release reset (or close IPE), remove the leads and
   power-cycle the board.

## 4. Reading the results

| Observation | Meaning |
|---|---|
| Device ID 0x206x found on both chips | Both chips alive; ICSP path, VDD, MCLR circuit fine |
| "Target not found" or ID 0x0000/0x3FFF on one chip | Check leads and VDD first. If they're right and the other chip reads fine, that chip is suspect |
| Both chips CP on, identical config words, user IDs and checksums | Same firmware image, consistent with health |
| CP **off** | The read **is the firmware**: keep both hex files as a backup. U4 and U5 should be identical apart from per-chip calibration, if any |
| Config words or checksums differ between U4 and U5 | Possible corruption or different firmware; compare with the bus check (`:e1` vs `:e2` versions) |
| EEPROM differs between chips | May be normal (per-axis settings or position) |

## 5. Related

- `2026-11-09-teletrack-mc001-board-analysis.md` §8: the schematic facts used
  here (MCLR pull-ups, RB3 strap, shared crystal, PGC/PGD nets).
- `2026-11-13-teletrack-breadboard-bench-test.md`: the bus-level check to do
  first.
