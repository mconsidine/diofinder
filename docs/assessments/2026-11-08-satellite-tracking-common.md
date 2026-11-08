# Satellite tracking with a driven mount: common assessment (2026-11-08)

**Status: assessment only, nothing built.** The question: paste one TLE, or
upload a file of element sets as a session's schedule, and have diofinder set
up and drive the mount to follow each pass. This document covers the parts that
are the same for every mount. The mount-specific documents are:

- [`2026-11-08-satellite-tracking-synscan.md`](2026-11-08-satellite-tracking-synscan.md):
  SkyWatcher SynScan, e.g. the Virtuoso (the mount in use)
- [`2026-11-08-satellite-tracking-lx200.md`](2026-11-08-satellite-tracking-lx200.md):
  Meade LX200 / Autostar and LX200-protocol mounts
- [`2026-11-08-satellite-tracking-onstepx.md`](2026-11-08-satellite-tracking-onstepx.md):
  OnStepX controllers
- [`2026-11-08-satellite-tracking-orion-teletrack.md`](2026-11-08-satellite-tracking-orion-teletrack.md):
  Orion TeleTrack (Synta-built)

## 1. Bottom line

- **The satellite maths already exists, but on the laptop.** The off-device
  tools (`tests/sat_identify.py`, `tests/sat_orbit.py`) read element sets,
  propagate them and predict passes. They depend on astropy, which is too heavy
  for the Pi Zero 2W and isn't installed there.
- **Mount motion doesn't exist at all.** The finder only *syncs* a mount, and
  never moves it. That was a deliberate safety decision: `docs/onstep-design.md`
  leaves GoTo "out of scope for v1 … a separate decision", and
  `diofinder/mountlink.py` has no slew, rate or stop command.
- **Most of the work is moving the mount.** That means a motion layer, a
  tracking loop, a pointing model, and safety around all of it. How hard the
  motion layer is depends on what the mount's protocol and firmware allow; the
  per-mount documents cover that.

## 2. What exists and can be reused

| Piece | Where | Reuse |
|---|---|---|
| Element sets: TLE/2LE/3LE, OMM XML/KVN/JSON/CSV, zip/gz, folders, newest epoch wins, Alpha-5 numbers | `tests/sat_identify.py` (`load_elements`, `parse_norad`) | Port the parsing to the device |
| SGP4 propagation with sanity checks (decayed or very old sets rejected) | `tests/sat_identify.py` (`propagate`, `sane_states`) | Port; needs `sgp4` on the device |
| Pass prediction: sunlit, dark sky, minimum elevation, sky region, local/UTC times | `tests/sat_orbit.py` (`predict`, `predict_all`, `parse_sky`) | Port the logic, without astropy |
| Site (latitude, longitude, elevation) | Utilities → Site, `horizon.parse_site` | As is |
| Accurate clock: NTP, or the phone with a measured ±; source recorded | Utilities → Clock, `diofinder/clock_info.py` | As is. Tracking depends on it (1 s ≈ 0.5° along track) |
| Plate solving, J2000, ~1 px | solver | Pointing-model calibration (§5) |
| Live capture of frames + exact UTC + solutions | `diofinder/live_capture.py` | Record each tracked pass for `sat_identify` / IOD reports |
| Serial link to a mount (GPIO UART, MAX3232), lock-protected manager, auto-sync loop | `diofinder/mountlink.py`, `comms_proc._MountManager`, `_mount_loop` | Transport, lock and failure handling. The sync loop must pause during a pass |
| Rotation-axis fit and two-angle error split from plate solves | `diofinder/polar.py`, `diofinder/polar_run.py` (UI hidden) | Pointing-model calibration (§5) |
| Precession (J2000 ↔ JNow) | `diofinder/precession.py` | As is |

## 3. Mount-independent pieces to build

### 3.1 A satellite engine on the Pi

- **Dependencies:** `sgp4` plus numpy only. `sgp4` publishes ARM wheels; it would
  be pinned and hash-checked like the other wheels (see
  `docs/supply-chain-pinning.md`).
- **Replacing astropy:**
  - SGP4's frame (TEME) is turned into the local horizon with the Greenwich
    sidereal angle and the site's position on the Earth.
  - A low-precision Sun position is enough for the "sunlit" and "dark sky"
    checks.
  - Polar motion, nutation detail and aberration (~20″) are well below the
    pointing needs.
- **Target accuracy:** about 0.01° in position, and better than 10 ms in time
  handling.
- **Cost:** one SGP4 evaluation takes tens of microseconds, so 10–20 per second
  in the tracking loop is negligible even on CPU 0.
- **Size:** moderate. It's a port of tested logic, and the laptop tools give
  reference answers to test against.

### 3.2 Input, storage and the session schedule (web UI)

- **New "Satellites" page.**
  - Paste one TLE, or upload a file (any format in §2).
  - Element sets are stored under `/var/lib/diofinder/sat/`.
  - Uploads from the phone are the field route (no internet there). A CelesTrak
    URL can be offered when the finder is online.
- **Pass list for a window** (default: tonight), in local time:
  - rise, highest and set times;
  - maximum altitude, direction, sky-region filter;
  - element-set age;
  - whether the mount can follow it (§3.4).
- **Schedule.**
  - Simplest form: the element file, plus optionally a list of catalog numbers
    and a time window. The finder works out the passes itself; no separate
    schedule format is needed at first.
  - Overlapping passes are flagged and resolved by a rule: the user's order, or
    the higher pass. The slew time between consecutive passes is included.
  - The user approves the queue.
- **Useful on its own:** this phase needs no mount motion at all. A pass list on
  the phone in the field is useful by itself.

### 3.3 Motion interface (what every mount adapter must provide)

A small interface beside the existing `sync()`:

| Operation | Purpose |
|---|---|
| `capabilities()` | Which of the following the mount supports, its axis type (alt-az or equatorial), maximum speed, rate resolution, position-read latency |
| `goto_altaz(alt, az)` or `goto_radec(ra, dec)` | Put the scope ahead of the satellite before the pass |
| `set_axis_rates(r1, r2)` | Signed speed per axis, deg/s. The core of smooth tracking |
| `read_position()` | Axis positions for feedback (encoder or the mount's own frame) |
| `stop()` | Immediate stop of both axes. Must always work |

- **Fallback when there's no rate control** ("leapfrog"): repeatedly GoTo a point
  a few seconds ahead of the satellite, let it cross the field, and repeat. It
  only needs GoTo, and works on almost any GoTo mount, but the satellite only
  passes through the view intermittently.

### 3.4 The tracking loop and the pass sequence

- **Loop rate:** 5–10 Hz, limited by serial round trips (§3.7).
  - Each cycle: predict where the satellite will be at the next cycle, convert
    it to axis targets through the pointing model (§5), and set each axis's speed
    to the predicted rate (feed-forward) plus a correction toward the target from
    the position read (feedback).
- **Pass sequence:**
  1. Slew to a point on the predicted track about 20–60 s ahead.
  2. Wait.
  3. Start the rate loop a few seconds before the satellite arrives.
  4. Track until it sets, the pass leaves the sky region, or a limit is reached.
  5. Stop, then go to the next pass.
- **Overhead passes** (axis-singularity, the "keyhole").
  - An alt-az mount can't follow near the zenith. A pass peaking at 85° needs on
    the order of 10°/s in azimuth for a few seconds.
  - An equatorial mount has the same problem near the celestial pole and, for a
    German equatorial, cannot do a meridian flip mid-pass.
  - The planner must check every pass against the mount's speed and acceleration
    limits. It then either caps the altitude (and accepts a gap), or flags the
    pass as not trackable.
- **Element-set timing errors.**
  - Stale or classified elements can be seconds to minutes off along the track.
    The UI needs **time-nudge buttons** (±0.5 s, ±5 s) that shift the whole
    prediction while tracking.
  - An optional later step is optical closed loop: detect the satellite in the
    live frames and steer to centre it. That's research-grade on the Pi (the
    laptop's frame-by-frame track finder takes about 0.4 s per frame).
- **Plate solving during a pass will mostly fail.**
  - At 1°/s and a 0.2 s exposure, stars trail by about 14 px.
  - So the loop relies on the mount's position feedback, not on solves.
  - Solves before and after each pass keep the pointing model honest.
- **Around each pass:**
  - The automatic mount sync is paused.
  - A live capture can start and stop with each pass, so every tracked pass is
    recorded for `sat_identify` and the IOD/R.D.E. reports.

### 3.5 What accuracy is needed

| Goal | Tolerance |
|---|---|
| Keep the satellite in the finder camera (13.5° field) | ±3° — easy, even with rough elements |
| Keep it in a low-power eyepiece (~1°) | ±0.3° — needs a good pointing model and a good clock |
| High-power viewing or imaging through the main scope (~0.2°) | ±0.05° — needs closed-loop correction; probably beyond what the mount-side feedback alone gives |

**Decide the goal early:** it sets how much of §5 and of the optical loop is
needed.

### 3.6 Safety (required before anything moves)

- **Altitude limits** (never below a set angle; never into the tripod/ground).
- **Azimuth cable-wrap limit.**
- **Speed and acceleration caps** below the mount's maxima.
- **A large STOP** on every page, plus `:Q` on the LX200 port.
- **A dead-man watchdog:** if the tracking loop stalls, the comms process
  restarts, or the controlling page disconnects, the mount stops. It is
  enforced in comms, not in the browser.
- **An explicit "arm" step** per session before the first motion, with the
  limits shown.
- **SkySafari:** it can stay connected (it only reads the position). Its own
  alignment and GoTo commands must be refused or queued while a pass is running.
- **Logging** of every motion command, for after-the-fact diagnosis.

### 3.7 CPU, serial and timing budget

- **Serial:** at 9600 baud a 10–20 byte command and its reply take about
  15–30 ms. Two rate commands plus one position read per cycle is 50–90 ms,
  which is why 5–10 Hz is the practical ceiling over serial. Network transports
  (UDP/TCP) are faster but add jitter.
- **Where the loop runs:** in comms on CPU 0. The work is small (SGP4 + a 2×3
  transform), but it must not block the LX200 worker pool.
- **Timestamps:** commands are stamped with the monotonic clock, and the
  satellite prediction uses the real-time clock. Nothing changes the clock
  during a pass (Clock card and SkySafari time sync are held off).

### 3.8 Testing

- **A mount simulator per protocol:** a fake serial device with axis dynamics
  (speed and acceleration limits, position read-back). The whole loop is tested
  without hardware, like the rest of the suite.
- **Bench tests** indoors with the real mount: rates, limits, STOP, watchdog.
  No sky needed.
- **Sky tests:** the ISS first (bright, accurate elements), then fainter
  objects. `tests/iss_check.py` already measures clock and pointing errors from
  a capture.

## 4. Suggested order (all mounts)

| Phase | Content | Size |
|---|---|---|
| 0 | **Probe the mount**: a script that queries version, axis type, GoTo forms, rate commands, maximum speed, rate resolution, read latency. Decides the control path (per-mount documents) | Small |
| 1 | Device satellite engine + Satellites page (planning only) | Moderate |
| 2 | Motion interface for the mount, safety (§3.6), simulator, bench tests | Moderate–large |
| 3 | Pointing model and its calibration (§5) | Moderate |
| 4 | Tracking loop, pass sequence, keyhole check, time nudge, automatic capture, queue | Large |
| 5 (optional) | Optical closed loop on the satellite | Large, research |

Sizes are relative, not estimates in days.

## 5. Pointing model: getting from mount axes to "true" pointing

The mount reports (and is commanded in) its own axis angles. Tracking needs
those angles to match true sky directions to a fraction of the eyepiece field.

### 5.1 Terms

| Term | What it is | How diofinder can measure it |
|---|---|---|
| Index offsets | The zero points of the two axes | One plate solve, compared with the mount's own position read (what sync does today) |
| Axis tilt (2 numbers) | Alt-az: azimuth axis not vertical (levelling). Equatorial: polar axis not on the pole (polar alignment) | Axis fit from plate solves, §5.2 |
| Axis non-perpendicularity | The second axis not at 90° to the first | Axis fit on the second axis, §5.2 |
| Collimation | Finder optical axis vs the main scope's axis | The existing boresight offset (`:CM#` align) |
| Refraction | Up to ~0.5° at the horizon, ~0.03° at 30° | Formula; or keep the model in the catalog frame (below) |

### 5.2 Reusing the polar alignment code

The polar routine (`diofinder/polar.py`, `diofinder/polar_run.py`; its UI is
hidden at present) already does most of what is needed:

- **What it does:** while the mount turns about one axis only, the plate-solved
  positions trace a circle on the sky. `fit_axis` fits the plane of that circle
  (SVD), and its normal is the rotation axis. `decompose_alignment_error` splits
  the axis's offset from where it should be into the two mechanical adjustments.
  `summarize_alignment` also reports how well the points fit a plane (a slip or
  a non-single-axis move shows up as a large residual).
- **Equatorial mounts:** that is the polar alignment, unchanged: RA axis vs the
  celestial pole.
- **Alt-az mounts:** the same fit on an **azimuth-only rotation**. Three changes
  are needed:
  - Each solve is first converted from RA/Dec into the local horizon frame
    *using its own timestamp*. The sky turns during the capture; the
    current routine works in RA/Dec, which is fine for an RA axis but not for an
    azimuth axis.
  - The fitted normal is compared with the **zenith**, not the pole.
    `fit_axis`'s sign convention (pick +Z) becomes "pick the upward normal".
  - The split becomes north–south and east–west tilt: a levelling error of the
    base.
- **Second axis:** an altitude-only (or declination-only) sweep gives that axis
  the same way. Its angle to the first axis gives the non-perpendicularity.
- **Capture:** `polar_run.PolarAligner` captures points by detecting "slewed,
  then dwelling" from successive solves; that suits hand moves. Once the finder
  can move the mount, the capture becomes automatic: move a set amount, stop,
  wait for 2–3 stable solves, record, repeat. The dwell detection and the
  residual check stay useful as quality gates.
- **Result:** with a few automatic moves, the finder measures the levelling (or
  polar alignment), the axis orthogonality and the zero points. These are the
  "true pointing" corrections. They can be shown to the user (as the polar
  routine does) as well as applied.

### 5.3 Refraction and frames

- **Refraction cancels in the plate-solve frame.** A plate solve says which
  catalog (refraction-free) direction lies at the boresight. A satellite seen at
  that pixel is refracted by the same amount, so mapping axis angles to the
  *catalog frame* makes refraction cancel to first order.
- **But it doesn't cancel across altitudes.** Refraction changes with altitude,
  and the model is fitted from solves at a few altitudes. Either include a
  standard refraction term, or calibrate across the altitudes the passes will
  use. Below about 20° the difference matters at eyepiece tolerances.
- **Time:** every solve and every command uses the same clock (§3.7).

## 6. Risks and open questions (all mounts)

- Each mount family's actual command support and limits (per-mount documents);
  the probe of phase 0 answers them.
- The accuracy goal (§3.5) decides whether mount feedback alone is enough.
- Whether moving the mount is acceptable at all, given §3.6: the earlier design
  deliberately kept the finder sync-only.
- Element-set quality: classified objects can be minutes off, so the nudge
  controls are essential, not optional.
