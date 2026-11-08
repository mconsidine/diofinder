# Satellite tracking: SkyWatcher SynScan mounts (Virtuoso) — assessment (2026-11-08)

**Status: assessment only, nothing built.** Read
[`2026-11-08-satellite-tracking-common.md`](2026-11-08-satellite-tracking-common.md)
first: it covers the satellite engine on the Pi, the planning page, the tracking
loop, the pointing model (including reuse of the polar alignment code), safety
and testing. This document covers what is specific to SkyWatcher's SynScan
mounts, starting with the Virtuoso the finder is wired to today.

## 1. Bottom line

- **Feasible.** The serial link and SynScan command layer already exist; motion
  commands and a tracking loop do not.
- **Two ways to drive the motors:**
  - **A.** Through the SynScan hand controller's NexStar-style commands.
  - **B.** Through the motor controller's own `:`-command protocol.
- **Which one works on this mount is not known yet.** A short probe of the real
  mount (phase 0) decides it. Path B is the better tracking path if it's
  reachable.

## 2. What exists today (SynScan-specific)

- **Code:** `diofinder/mountlink.py` `SynScanLink` speaks the NexStar-derived
  SynScan ASCII set over the GPIO UART through a **MAX3232** (3.3 V ↔ RS-232) to
  the hand controller's serial port:
  - `Kx` echo (ping)
  - `V` (firmware version)
  - `e` (get precise RA/Dec)
  - `s` (precise sync)
- **Epoch:** the mount's own (`mount_epoch`, JNow by default).
- **Safety:** there is **no motion command**, by design.
- **Wiring:** `comms_proc._MountManager` owns the single serial connection
  under a lock. `_mount_loop` runs the auto-sync with its gates (fresh solve,
  matches, settled, deadband, interval).
- **Config:** `mount_*` keys. Tests: `tests/test_mountlink.py`.

## 3. Control paths

### Path A: hand controller, NexStar-style commands

The SynScan hand controller emulates much of Celestron's NexStar serial
protocol, which already underlies `SynScanLink`. In that family:

| Need | NexStar-family command (to verify on this hand controller) |
|---|---|
| GoTo alt/az | `b` (precise GoTo azimuth/altitude) |
| GoTo RA/Dec | `r` (precise) |
| Read alt/az | `z` (precise) |
| Variable-rate motion per axis | `P` pass-through: axis byte 16 (azimuth/RA) or 17 (altitude/Dec), a direction byte, and the rate × 4 split into high and low bytes |
| Fixed-rate motion | `P` pass-through, fixed rates 1–9 |
| Stop | variable rate 0 on each axis |
| Tracking off | `T` (tracking mode 0) |

- **Unknowns.** The Celestron protocol document defines the pass-through forms
  above. A user report says pass-through commands "appear to be specific to the
  mount manufacturer or even mount version". So whether the Virtuoso's SynScan
  AZ hand controller accepts the variable-rate form, its top rate and its rate
  resolution must be measured.
- **Pros:** reuses the existing link and wiring; the hand controller stays the
  safety backstop (its own direction keys, its own limits).
- **Cons:** each command passes through the hand controller (added latency).
  Top rate and resolution are whatever the controller allows. The hand
  controller's alignment model sits between our commands and the motors, so
  "alt/az" may be the controller's aligned frame rather than raw encoder angles
  (to verify).

### Path B: motor-controller protocol (`:` commands)

SkyWatcher motor boards speak their own protocol:
- commands start with `:` and end with a carriage return;
- replies start with `=`;
- per-axis counters and a speed ("tracking") mode set by a timer preset computed
  by the host from the axis resolution.

INDI's SkyWatcher alt-az driver (`skywatcherAltAzMount`) talks to the motor
controller directly, without the hand controller, and supports sync and
tracking.

- **Access:**
  - through the hand controller's **PC-direct (pass-through) mode**, where
    supported;
  - or a direct connection to the motor board;
  - on Wi-Fi models (AZ-GTi, Virtuoso GTi) over UDP port 11880.
- **Pros:** precise speed control and raw encoder positions. That is what
  smooth satellite tracking wants, and what INDI uses.
- **Cons:**
  - more protocol to implement (axis resolution, timer presets, speed vs GoTo
    modes, stop and acceleration behaviour);
  - the hand controller's safety features are bypassed, so §3.6 of the common
    document carries all the safety.
  - INDI's driver is GPL; read it for behaviour, don't copy code.

### Recommendation

1. **Phase 0 probe** against the actual Virtuoso:
   - hand controller model and firmware (`V`);
   - whether `b`, `z` and the `P` variable-rate form work, and what top rate
     each axis reaches;
   - smallest rate step;
   - command latency;
   - whether PC-direct mode exists on this hand controller.
2. If path A gives smooth rates up to about 1.5°/s with fine resolution, start
   there: smallest change, existing wiring.
3. If not, use path B, which needs PC-direct mode or a direct cable.
4. If neither gives usable rate control, fall back to "leapfrog" GoTos (common
   §3.3) for finder-camera tracking only.

## 4. Mount specifics that matter

- **Alt-az fork/arm.** The zenith keyhole applies (common §3.4).
- **Original Virtuoso vs Virtuoso GTi.** The original has a SynScan AZ hand
  controller with a serial jack (today's wiring). The GTi is Wi-Fi first: path B
  over UDP 11880, or the GTi's own serial port if it has one. Confirm the exact
  model before phase 0.
- **Top speed and acceleration.** Unknown. Satellites need about 1–1.5°/s;
  overhead passes far more in azimuth, which is where the altitude cap comes in.
- **Freedom-Find encoders.** The Virtuoso keeps its position when pushed by hand.
  Hand moves during a session don't destroy the pointing model, but the tracking
  loop must notice them (position jump) and stop.
- **Sync semantics.**
  - Path A: SynScan's sync updates the controller's own model. The finder can
    keep using `sync()` between passes so the controller's alt/az stays true, or
    rely on its own model (common §5).
  - Path B: the finder owns the whole model, built from plate solves (common
    §5.2, levelling from an azimuth-only sweep).

## 5. Pointing model (SynScan)

- **Levelling:** the Virtuoso stands on a table or tripod, so levelling error is
  likely the biggest term. The azimuth-only sweep adapted from
  `diofinder/polar.py` measures it directly (common §5.2): the finder commands
  the azimuth moves itself and records a solve after each.
- **Under path A,** the hand controller's alignment (built from the finder's
  syncs) may absorb some of this. Under path B the finder's model is the only
  one.

## 6. Phases (SynScan)

| Phase | SynScan work | Size |
|---|---|---|
| 0 | Probe script for the items in §3; record the results in this document | Small |
| 1 | Common: device engine + Satellites page | Moderate |
| 2 | `SynScanLink` motion: path A (`b`, `z`, `P` rate, stop) **or** a new path-B link; SynScan simulator for tests; safety | Moderate (A) / large (B) |
| 3 | Pointing model with automatic levelling sweep | Moderate |
| 4 | Tracking loop and pass sequence | Large |

## 7. Sources

- Existing code: `diofinder/mountlink.py`, `diofinder/comms_proc.py`,
  `docs/onstep-design.md` §11 (SkyWatcher, Virtuoso over GPIO).
- [Celestron NexStar communication protocol](https://www.as.up.krakow.pl/rzeczy/1154108406_nexstarcommprot.pdf):
  `P` pass-through variable and fixed rates; family background for SynScan.
- [ZWO forum: SynScan pass-through in practice](https://bbs.zwoastro.com/d/11637/3):
  pass-through framing; manufacturer/version differences; rate-0 stop.
- [SkyWatcher motor controller command set](https://casite-460905.cloudaccess.net/media/kunena/attachments/5531/skywatcher_motor_controller_command_set.pdf):
  `:` framing, speed (tracking) mode, timer presets.
- [Stellarmate: SkyWatcher Alt-Az driver](https://stellarmate.com/devices/mounts/skywatcher/266-skywatcher-alt-az.html):
  direct motor-controller control without the hand controller; Wi-Fi over UDP.
- [EQMOD satellite tracking notes](https://eq-mod.sourceforge.net/faqs/sattrack.html):
  custom tracking mode, rates in degrees per second.
- [SynScan V4 hand controller manual](https://cdn.shopify.com/s/files/1/0080/7095/5123/files/Synscan_V4_Hand_Control_Manual_SSHCV4-F-161208V1-EN.pdf?40).
