# Orion TeleTrack: hand controller vs driving the mount directly (2026-11-08)

**Status: assessment only, nothing built.** This follows
`docs/assessments/2026-11-08-satellite-tracking-orion-teletrack.md` and the
SynScan document on `claude/azimuth-on-0.11.74`. The question is whether a
tracking loop should talk to the TeleTrack's hand controller, or bypass it and
talk to the mount's motor board, given the preference for as few links as
possible in the chain.

**What is assumed and not yet confirmed:**
- The TeleTrack GoTo is a Synta-built SynScan-type mount (earlier assessment).
- Its motor board therefore speaks Sky-Watcher's motor-controller protocol
  (`:` commands, replies `=…`/`!…`, carriage-return terminated).

The manuals and the AUX port's function are not known yet; §6 lists the
documents needed.

**Sources read for this document:**
- INDI's `skywatcherAPI.cpp`/`.h` (GPL; read for behaviour only, nothing
  copied).
- NexPlane (`nforrester/NexPlane`, GPL-3; read for behaviour only). It is an
  open-source program that tracks aircraft and satellites with Celestron and
  Sky-Watcher mounts, and its Sky-Watcher driver bypasses the hand controller.

---

## 1. Bottom line

- **Direct to the motor board fits the preference, if the motor firmware
  allows it.**
  - One hop: Pi → motor board.
  - Raw encoder counts and fine speed control, with no hand-controller
    alignment model in the way.
  - It is also how INDI and NexPlane drive Sky-Watcher mounts for fast
    targets.
- **The deciding fact is the motor-board firmware version.** NexPlane states
  that Sky-Watcher mounts need **motor-controller firmware 3.39 or later** to
  "smoothly track moving targets". It lists the **AZ SynScan** (the family the
  TeleTrack GoTo most likely belongs to) among mounts where 3.39 "is not
  available": they "will probably move, but … not … smoothly track". If the
  TeleTrack's board is older, *neither* path tracks smoothly. The hand
  controller drives the same board with the same limits; it can only hide the
  stop/restart, not avoid it.
- **So the first step is one query, not a decision:** read the motor-board
  version (`:e1`, the INDI "InquireMotorBoardVersion" command). Then:

  | Motor firmware | Recommendation |
  |---|---|
  | ≥ 3.39 | Direct drive (path B). It matches the preference and has working precedents |
  | Older, firmware update available | Update, then direct drive |
  | Older, no update possible | Direct drive still gives GoTos and short steady rate runs, but expect jerks at every speed change. Leapfrog GoTos (common §3.3) are the realistic mode. Or an OnStepX retrofit for real tracking |

---

## 2. The two chains

```
Path A (hand controller):
  Pi UART ─MAX3232─ RS-232 ─▶ hand controller (NexStar/SynScan commands,
                               alignment model, menus) ─TTL bus─▶ motor board ─▶ motors

Path B (direct):
  Pi UART ─(level shift if 5 V)─ TTL ─▶ motor board (":" protocol) ─▶ motors
```

## 3. Comparison

| Aspect | A: through the hand controller | B: direct to the motor board |
|---|---|---|
| **Links in the chain** | Two serial hops and two firmwares. Each tracking command becomes several motor-bus commands issued by the hand controller | One hop, one firmware |
| **Latency** (estimate; measure in phase 0) | Wire time on both hops, plus the hand controller's own processing (unknown; it is a small microcontroller that also runs the display and keypad). A position read (`z`) means the hand controller queries both axes, transforms through its alignment model, then formats the reply | Wire time only. At 9600 baud a step-period command (`:I1xxxxxx`) is ~10 ms with its reply, and a position read (`:j1`) ~12 ms. Two rate updates plus two reads is ~45 ms, so 10 Hz is reachable, more with fewer reads |
| **Comms errors** | More places to fail: two cables, two connectors, the RS-232 converter. The hand controller may ignore or delay commands while busy (menus, its own motor polling). Old Orion firmware may lack the pass-through forms | Fewer parts (the MAX3232 is not needed if the bus is 3.3 V). Every command is acknowledged (`=` ok, `!` + error code), so failures are explicit. But **only one master on the bus**: the hand controller must be unplugged (or in PC-direct mode), never both talking |
| **Speed control** | NexStar variable rate: `P` pass-through, rate in ¼″/s units. Fine on paper, but the controller's own speed caps and mode switching apply, and **whether this Orion firmware accepts it is unknown** | Speed is set as a timer period per microstep, from the axis resolution (`:a`) and the timer frequency (`:b`), both read from the board. Fine resolution in high-speed mode at satellite rates (below) |
| **Speed-mode and direction rules** | The hand controller handles them, but cannot avoid the physical stop they require | The finder must handle them (§4). With firmware ≥ 3.39 this is simple: stay in high-speed mode, change the period while running, stop only on a direction change |
| **Position read-back** | Through the hand controller's alignment model: "alt/az" may be the controller's aligned frame, not raw axes | Raw encoder counts per axis. The finder's pointing model (common §5, levelling sweep from `polar.py`) maps them to the sky |
| **Alignment / pointing model** | The hand controller keeps its own, built from star alignment or the finder's syncs | The finder owns it completely. NexPlane notes the same: bypassing the hand controller loses its alignment, so the program must calibrate |
| **Safety** | The hand controller's keys still work and stay a manual backstop, plus any limits it has | **None from the hand controller** (it is unplugged). Limits, stop, the dead-man watchdog and a stop button all have to be in the finder (common §3.6). A small hardware "kill" switch on the motor power is cheap insurance |
| **Existing code** | `SynScanLink` already speaks this dialect (sync, version, position); add `b`/`z`/`P` | New dialect (`SynMotorLink`). The protocol is small: init, inquire, set mode `:G`, period `:I`, start `:J`, stop `:K`/`:L`, read `:j`, GoTo target `:S`/`:H` |
| **Wiring changes** | None beyond today's (MAX3232 to the hand controller's jack) | Different cable to the mount's HC (or AUX) port; check the bus voltage. **The RJ-style HC port also carries 12 V: a wrong pin can destroy the Pi** |
| **Satellite precedent** | NexPlane uses the hand controller only for **Celestron** (NexStar). For Sky-Watcher it bypasses it | INDI and NexPlane both drive Sky-Watcher motor boards directly for tracking |

---

## 4. The speed-mode problem, which matters for satellites

How Sky-Watcher motor boards behave (from INDI's driver):

- **Two speed modes:** low-speed and high-speed. INDI switches at
  **128 × sidereal ≈ 0.53°/s**, which is right in the middle of typical
  satellite rates (0.3–1.5°/s).
- **When a stop is needed:** the board must fully stop before:
  - a change of direction;
  - a switch between low- and high-speed mode;
  - (on older firmware) some changes while moving.

  INDI stops the axis and then polls the status every 100 ms until it stops
  before continuing.
- **Low-speed mode at the top of its range** has poor speed resolution:
  - the timer period is an integer, near its floor of ~6 counts;
  - so one count is a 15 % speed step.
- **High-speed mode** divides the period by the high-speed ratio (`:g`, often
  16 or 32). At satellite rates the period is then large and the speed
  resolution is fine (~1 %).

What this means for a pass:

- **Azimuth** does not reverse during a normal pass. Starting the pass in
  high-speed mode and staying there avoids any stop.
- **Altitude** reverses once, at culmination, where its rate is near zero
  anyway. The required stop costs almost nothing in pointing.
- **NexPlane's approach** (firmware ≥ 3.39): always high-speed mode, update
  the period while moving, stop only on a direction change. That is the
  pattern to copy in design (not in code).
- **On firmware without on-the-fly period changes,** every speed update needs
  a stop and restart. That is the "will move but not smoothly track"
  limitation, and it applies through the hand controller too.

---

## 5. The LX200 point

- **For a Meade LX200, the serial port is already "direct".** The keypad is
  only a keypad. The serial port talks to the mount's own computer, and there
  is no lower layer to reach without replacing the electronics.
- **So LX200 and TeleTrack-direct are both one hop** to one interpreter. The
  difference is the protocol:
  - LX200: coordinate-level, with 0.1°/s custom rates where they exist;
  - Synta motor protocol: axis-level, with fine timer-based rates.
- **Autostar-driven Meade mounts** (ETX/LX90 with a #497) are different: the
  handset *is* the computer, so it cannot be bypassed.
- **The preference holds:** directly driving the TeleTrack makes it match the
  LX200 case (one hop), with a finer rate control than LX200 offers.

---

## 6. Documents and facts needed

Fetching was attempted from the build sandbox. Only GitHub is reachable; the
other sites below failed. Please upload what you can, or the photos.

**Most important**

1. **The TeleTrack's model number** (label on the mount) and the hand
   controller's firmware version (shown at power-on, or `V`).
2. **The mount's instruction manual** for that model. Candidates:
   - 9481 TeleTrack GoTo (RS-232 appendix):
     https://www.manualsdir.com/manuals/204941/orion-9481.html?page=13
   - 9441 TeleTrack: https://www.manualslib.com/manual/1266647/Orion-Teletrack-9441.html
   - Orion's own manuals page (search the model number):
     https://www.telescope.com/
3. **Photos** of the mount's ports (the AUX port, the hand-controller port,
   power) and of any labels next to them. The AUX port's function decides the
   cabling:
   - It may be a second port on the motor bus (then the Pi connects there).
   - It may be an ST-4 autoguider port (guide pulses only: useless at
     satellite rates).
   - It may be a power outlet.

**Protocol references**

4. **Sky-Watcher "Motor Controller Command Set"** (the `:` protocol). Not
   reachable from here; search the title on skywatcher.com. A possible path is
   https://inter-static.skywatcher.com/downloads/skywatcher_motor_controller_command_set.pdf
   (unverified).
5. **Sky-Watcher "SynScan Serial Communication Protocol"** (hand-controller
   commands, for path A). Search the title on skywatcher.com.
6. Celestron **"NexStar Communication Protocol"** (v1.2 or later): defines the
   `P` pass-through forms the SynScan hand controller emulates.

**To measure once wired (phase 0 for this mount)**

- With the hand controller unplugged and the Pi on the HC/AUX port:
  - bus voltage at the TX line (meter, before connecting);
  - `:e1` / `:e2` (motor firmware version);
  - `:a1` / `:a2` (counts per revolution);
  - `:b1` (timer frequency);
  - `:g1` (high-speed ratio);
  - round-trip time for `:j1` over 1,000 reads;
  - whether `:I` changes the speed while running in high-speed mode without a
    stop (the 3.39 behaviour);
  - top speed per axis.
- Through the hand controller: the version, whether `P` variable rate, `z` and
  PC-direct mode work, and the round-trip time for `z`.

## 7. Recommendation

1. Identify the model and the AUX port (items 1–3).
2. Read the motor-board version directly: one query, and the cable is the one
   the direct path needs anyway.
3. If ≥ 3.39 (or updatable): build path B. Keep the hand controller in a
   drawer as the manual backstop between passes, and add a hardware motor
   power switch.
4. If older and not updatable: path B for GoTos and leapfrog tracking. Real
   smooth tracking then needs different hardware (OnStepX), not path A.
