# Satellite tracking: performance bottlenecks and what could be stripped (2026-11-08)

**Status: assessment only, no code changed.** This is a companion to the
mount-tracking assessments on `claude/azimuth-on-0.11.74`
(`docs/assessments/2026-11-08-satellite-tracking-common.md` and the per-mount
files). It answers two questions:

1. Where would performance limit diofinder if it drove a mount through
   satellite passes?
2. Would removing code from diofinder make that faster?

Line references are to this branch (v0.11.74 + Utilities power card). Numbers
marked *measured* come from earlier audits (`docs/audit-2026-07.md`,
`docs/decisions/2026-07-15-efficiency-assessments-review.md`). Numbers marked
*estimate* are mine and were not measured on the device.

---

## 1. Bottom line

- **The CPU is not what limits tracking.** These limits are ranked by how much
  they cost:
  1. the serial link to the mount (latency per command);
  2. the mount's own speed and acceleration, including the zenith keyhole;
  3. the clock (1 s of error is about 0.5° along track);
  4. exposure time and star trailing, which stop plate solving during a pass;
  5. distant fifth: CPU 0 contention from the web UI, SkySafari polling and the
     Manager process.

  The tracking loop's own computation (SGP4 plus a small transform at 5–10 Hz)
  takes well under 1 % of one core.
- **One CPU-heavy job does exist:** planning a whole element file on the Pi
  (thousands of objects × a night × a fine time step). That is a batch job, not
  the tracking loop, and it needs its own design (§3.6).
- **Removing code would gain almost nothing.** The existing features cost nearly
  nothing while idle; the costly ones (the solver, the live view and SkySafari
  polling) are the ones you want outside a pass. The real gain comes from a
  **"pass mode" that pauses or retunes things for the length of a pass and then
  restores them** (§4). Several of the things it pauses are also needed for
  correctness, not only for speed (§4.2).

---

## 2. Where time goes today

### 2.1 Cores

| CPU | What runs | Typical load |
|---|---|---|
| 0 | Launcher (IMU thread, 20 Hz), Manager server, comms (LX200 pool of 8, maint socket, auto-exposure / watchdog / mount / sysmon threads), web UI (Flask, JPEG encode), kernel/IRQ | I/O-bound, bursty |
| 1, 2 | Solver (extract, solve, bg_cache worker) | Busy once per frame |
| 3 | Camera (capture, SHM publish) | Light |

(`etc/diofinder.conf.default`: `cpu_comms: 0`, `cpu_solver: 2`,
`cpu_solver_aux: 1`, `cpu_camera: 3`.)

### 2.2 Per-frame solver cost (measured)

- Extraction ≈ 6 ms; the solve itself ≈ 0.5 ms with a good hint, up to
  `solve_timeout_ms` = 1500 ms when it fails.
- bg_cache median rebuild 100–300 ms, holding the GIL. That is in the solver
  process, so it never blocks comms.
- One snapshot of `shared_cfg` per frame (one Manager RPC, 0.3–1 ms).

At the default 0.2 s exposure the solver is idle most of the time.

### 2.3 What a tracking loop would add (estimate)

- **SGP4:** tens of µs per call with the C-accelerated `sgp4` wheel; a few
  hundred µs if only pure Python is available (possible on a 32-bit armhf
  image; to check). Even pure Python at 10 Hz is < 1 % of a core.
- **The rest:** TEME → horizon → axis angles is a few small matrix products.
- **Serial:** 50–90 ms per cycle at 9600 baud for two rate commands plus one
  position read (common assessment §3.7). **That is the loop's real budget,
  and it is spent waiting, not computing.**

---

## 3. Bottlenecks, ranked

### 3.1 Serial round trips (dominant)

- **Wire time:** 9600 baud is about 1 ms per byte. A NexStar-style `P` rate
  command (8 bytes + `#`) plus a `z` read (1 byte out, ~18 back) is ~30 ms on
  the wire per cycle. The hand controller's own processing (unknown; phase 0
  measures it) comes on top.
- **Ceiling:** 5–10 Hz, as the common assessment says. A faster CPU changes
  nothing here.
- **What helps:**
  - feed-forward rates (the loop only corrects, so a low rate is fine);
  - fewer reads (read position every 2nd–3rd cycle);
  - a higher baud rate, or path B / UDP where the mount offers it.
- **Pitfalls in the code as it stands** (`diofinder/mountlink.py`,
  `comms_proc._MountManager`):
  - **Long timeout.** `SerialTransport` uses `timeout=2.0` s
    (`mountlink.py:88`). In a tracking loop one lost reply stalls the loop for
    2 s, which is 2° of satellite motion. A tracking link needs a timeout of
    ~100–200 ms and a "skip this read, keep the last rates" policy.
  - **Shared lock.** `_MountManager` holds a single lock for all serial work,
    and `status()` takes the same lock (`comms_proc.py` ~l.618). A web page
    polling `/api/mount` therefore waits behind a serial transaction. Nothing
    breaks, but status calls should read cached fields without the serial lock
    once a loop holds it at 10 Hz.
  - **Rebuilding on error.** `_ensure()` tears down and reopens the port after
    any error. Reopening a port mid-pass costs much more than a skipped cycle.

### 3.2 Mount mechanics

The top speed, acceleration and rate resolution of the mount, plus the alt-az
keyhole near the zenith, decide which passes can be followed at all. This is a
hardware limit; phase 0 measures it. No software change on the Pi moves it.

### 3.3 Clock

- **Scale:** in low orbit the satellite moves about 0.5° per second of clock
  error.
- **What exists:** the Clock card's NTP-style ± and the timesyncd guard. That
  is enough for the finder-camera goal (±3°). For eyepiece goals (±0.3°) the
  clock has to be good to ≲0.5 s, together with element sets that are good to a
  similar time.
- **Rule:** nothing may step the clock during a pass, already noted in the
  common §3.7.

### 3.4 Exposure and trailing: plate solves stop during a pass

- **Plate scale:** ~51″/px. With the mount following a satellite at 1°/s, the
  stars move ~70 px/s across the frame:
  - 14 px of trail at the default 0.2 s;
  - 3.5 px at the 0.05 s floor.
- **Effect:** solves mostly fail during a pass. That is the physics of the
  exposure, not a CPU limit. Short exposure with high gain might keep a few
  bright stars round enough to solve; worth trying on the bench sky, not
  assumed.
- **Latency of optical feedback:** exposure + readout + ~10–20 ms extraction.
  At 0.05 s that is roughly 80–100 ms (estimate). Fine for a slow correction
  term; too slow to be the primary loop. The common §3.4 already makes mount
  position feedback primary, which is correct.

### 3.5 CPU 0 contention (minor, but where the loop would live)

Who shares CPU 0 with a loop in comms:

- **SkySafari:** a reconnect storm of ~4 new TCP connections per second, each a
  `:GR/:GD` with one Manager snapshot. The worker pool made this cheap, but it
  is still continuous.
- **Web UI live view:** solver → display SHM → web UI copy → stretch → JPEG
  encode, every ~1.25 s while a tab is open. JPEG encode at half resolution is
  the largest single CPU 0 task in the web UI (`webui/app.py` ~l.1914–1944).
- **Web UI pollers:** `/api/camera/state`, `/api/mount`, the sysmon card, and so
  on. Each is a maint socket call, and some fan out to a solver RPC.
- **IMU reader:** 20 Hz, one atomic Manager write per sample (measured sub-ms).
- **Manager server:** every `shared_cfg` / `latest_solution` access from every
  process lands here.

**Effect on tracking:** scheduling and GIL jitter in comms is a few ms
typically, tens of ms in a burst (estimate; Python's switch interval is 5 ms,
eight LX200 workers share the GIL). At 1°/s, 10 ms is 36″ and 100 ms is 0.1°.
That's invisible for the finder-camera goal. For the eyepiece goal it matters
only if the loop timestamps a command at the wrong moment. So the
loop should:

- stamp each command with `time.monotonic()` right after the write returns;
- propagate SGP4 to the time the command takes effect, not to the time the
  cycle started;
- keep its own state in-thread, with **no Manager reads per cycle** (take a
  snapshot when the pass starts; publish status at ≤ 1 Hz).

If that still jitters, a separate small process (or a thread raised with
`os.sched_setscheduler` / `os.setpriority` on its native id) avoids the GIL of
comms entirely. Measure before choosing that; it isn't needed by the numbers
above.

### 3.6 Planning a whole element file on the Pi (the one real CPU job)

`sat_orbit.py predict` on the laptop walks every object in the file across the
window. A naive port is expensive on the Pi (estimate):

- **Scale:** 5,000 objects × 12 h at a 10 s step is ~22 M propagations.
- **Time:** about a minute with the vectorised C `SatrecArray`; far longer in
  pure Python. A minute of a solver core is acceptable; tens of minutes is not.

What keeps it small:

- **Filter before propagating:** decayed or stale sets (`--max-age-days`
  already does this); `--norad` lists; inclination and perigee checks against
  the site latitude; and passes that can't reach the minimum elevation.
- **Coarse step, then refine:** a 60 s step finds candidate rises; refine only
  near those.
- **Run it as a background job** on solver cores 1–2 while no pass is active,
  with results cached under `/var/lib/diofinder/sat/`. Never run it in comms.
- **Memory:** 512 MB total, with the star DB in the solver. Satrec objects are
  roughly 1–2 KB each (estimate), so a full public catalog (~30 k) held in
  memory is tens of MB. Load the selected objects only, and parse the file as
  a stream.
- **Simplest option:** plan on the laptop and upload the pass list. The
  laptop tools already do this; the Pi then only follows a short list.

---

## 4. "Strip" vs "pause": recommendations

### 4.1 Removing code: not worth it for speed

| Candidate | Cost while idle | Verdict |
|---|---|---|
| Legacy tetra3 extractor backend | None unless selected | Keep (it is the A/B baseline) |
| Tracking (ROI) mode | None while off (default) | Keep |
| Web UI A/B, tune-from-burst, bg preview, auto-tune | Threads only start on demand | Keep |
| Star names / Messier labels | One small CSV in memory; per-solve lookup only on a solve | Keep |
| sysmon thread (5 s) | /proc reads, negligible | Keep; useful for checking a pass |
| bg_cache temporal model | Copies every 4th frame in steady state; it already drops to per-frame while slewing | Keep |
| Legacy `imu_q/imu_t`, 5-tuple `imu_ref` reads | Dead fallbacks, µs | Could be removed as cleanup, no speed gain |

None of these would give the tracking loop a measurable gain. Removing them
risks regressions in features that work.

### 4.2 Pausing things during a pass: worth doing

Proposed: a "pass mode" entered a few seconds before the pass and left after
it. Each row says what it gains.

| During a pass | Why | Gain |
|---|---|---|
| **Auto-exposure paused** (`_ae_paused` exists) | Unsolved frames make it raise gain, then *lengthen exposure*, which trails stars more. It would also bump `camera_settings_epoch` and flush bg_cache | Correctness first, CPU second |
| **Mount auto-sync paused** (`_mount_loop`) | Must not sync the mount to a trailed or stale solve mid-pass, and must not contend for the serial lock | Correctness and serial time |
| **Solver: fix the exposure short, and skip expensive retries** | With trailed stars, many frames still pass `min_centroids` (`detect_max_axis_ratio` is off by default), so each attempt can run to the 1500 ms timeout. After 20 failures the FallbackGate adds loose blind retries and, every 3rd fire, a full-FOV search. Options: drop `solve_timeout_ms` to ~100–200 ms for the pass, suspend the FallbackGate, or solve only every Nth frame | Frees solver cores; avoids the small risk of a spurious loose "solve" calling `force_recalibrate()` |
| **Keep the solver heartbeat** | The watchdog restarts the unit after 30 s without a publish. A pass is 2–10 min. Any pause of solving must keep publishing the epoch (as dark frames already do, `_dark_publish_due`) | Avoids a mid-pass restart |
| **Live view off, or slowed** (no `display_start` keepalive) | Removes the JPEG encode and SHM copies from CPU 0 and the solver | CPU 0 |
| **SkySafari: answer from the tracking loop's own position, or throttle** | It may stay connected (common §3.6). `:GR/:GD` can return the loop's last commanded or read position, with no Manager read or IMU prediction | CPU 0, and less confusing |
| **IMU poll rate lowered** (e.g. 5 Hz via `imu_poll_hz`) | The IMU shows "moving" the whole pass, so its slew detection carries no new information. Keep it at a low rate as a bump or stall detector | Small CPU 0 / Manager gain |
| **Satellites page polls at ≤ 1 Hz**; other pages' pollers stop | Each poll is a maint call | CPU 0 |
| **Live capture**: optional, PNG level 3 on a writer thread in the solver process (azimuth branch) | Encoding contends for the solver's GIL, not for comms. Fine while solves are paused; check the frame rate if both run | Choice per pass |

Everything in this table is already switchable through `shared_cfg` or the
existing maint commands, or nearly so. Pass mode can be a short sequence:
snapshot, apply, then restore. Nothing has to be deleted.

---

## 5. What to measure in phase 0 (adds to the probe list)

1. Round-trip time per command on the real mount: rate set, position read,
   GoTo; minimum, median and worst over 1,000 cycles at 9600 baud.
2. `sgp4` on the device: is the C extension present (`Satrec` from
   `sgp4.wrapper` imports)? What is µs per propagation, scalar and array?
3. Loop timing jitter inside comms with SkySafari connected and the live view
   open, vs pass mode: histogram of cycle period and command time stamps.
4. Solve rate at 0.05 s / high gain while the mount slews at 0.5 and 1°/s
   (does anything solve?).
5. sysmon high-water marks for CPU 0 and memory across a simulated 10-minute
   pass.

## 6. Answer in one paragraph

Performance on the Pi will be limited by the serial link and the mount, then
by the clock and trailed frames. The CPU comes last. Keep the loop in comms
(or a small process if phase 0 shows jitter), avoid Manager reads per cycle,
use short serial timeouts, and plan whole element files off the tracking path
(on the laptop, or as a filtered background job). Don't strip code for speed;
add a reversible pass mode that pauses auto-exposure and auto-sync, cuts solve
timeouts and retries, keeps the watchdog heartbeat, turns off the live view
and lowers the IMU and polling rates for the length of the pass.
