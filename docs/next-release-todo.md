# Next release — to-do list (after v0.11.75)

Working branch: `claude/azimuth-on-0.11.74`. Items are grouped by area; each
says why it matters. Items marked ✅ are done on the branch.

**Release candidate:** build #193 (IMU report smoothing behind a default-off
switch, FOV drift recentre, clock fixes, test-mode location) plus the two
follow-ups below (OTA IMU-rate migration, on-screen-only polling). Next: the
on-sky A/B tests and the pruning they unlock (see "Resource assessment").

## Already on the branch (since v0.11.75)

- **Mount sync settle gate works** (`mount_auto_settle_s`): auto mode only
  syncs a solve taken at least `settle_s` after the IMU last saw motion. Before,
  the setting existed but nothing read it. `mount_status.auto_reason` shows the
  auto loop's latest decision.
- **MAX3232, not MAX232**, in code comments, config, web tooltips and docs (the
  Pi's GPIO is 3.3 V; a 5 V MAX232 can damage it).
- **Live capture** (Home page button): records every processed frame with its
  sensor UTC time, solved centre RA/Dec and the matched stars, into
  `livecap_<UTC>.zip` (Utilities → Live captures). Config: `elevation_m`,
  `live_capture_max_mb`, `live_capture_min_free_mb`.
- **Off-device satellite tools**: `tests/satcheck_archive.py` (SatChecker query
  list) and `tests/satcheck_overlay.py` (per-frame WCS from matched stars,
  predicted streaks drawn on frames, `--stack` composite, `--chart`).
- **Verified on sky (capture 2026-11-02 02:07 UTC):** per-frame fits 0.64 px rms;
  the documented roll convention is correct (`reproject_frame.py` default is
  right); stacking aligns real drifting frames. The trail in that capture was
  an aircraft, not STARLINK-30913 (opposite direction; the satellite was in
  Earth's shadow; strobe-like flashes ~0.6 s apart).

## To do — finder

1. ✅ *Done (v0.11.76 base):* **Pause page polling in hidden browser tabs.** The live view (Home and
   Advanced) and the status pollers keep running in background tabs; desktop
   browsers only slow them. Pause on `visibilitychange`, resume on show.
   Files: `webui/templates/home.html`, `camera.html`, other pages with
   `setInterval`.
2. ✅ *Done (v0.11.76 base; the hunting filter's per-sample blend now scales
   with the rate so its lag in seconds is unchanged):* **Default IMU sample rate 20 Hz → 10 Hz** (`imu_poll_hz`). Consumers need far
   less than 20 Hz (SkySafari ~4/s, solves ~3/s, slew detector compares ~0.8 s
   apart); halves the IMU's traffic through CPU 0. Devices with a saved rate
   keep it. Files: `diofinder/config.py`, `etc/diofinder.conf.default`, docs.
3. ✅ *Done (v0.11.76 base; `solution_for_seq`):* **Debug bundle: match each frame to its own solve.** At 0.3 s exposures every
   frame in a bundle had `solution: null` (the solver had moved on before the
   web UI asked). Have the solver keep the last few solutions and look them up
   by seq. Files: `webui/app.py` `_solution_for_seq`, `diofinder/solver_proc.py`,
   comms maint command.
4. ✅ *Done (v0.11.76 base):* **Debug bundle: build to a file, serve a download link.** It is built entirely
   in web UI memory on CPU 0 and returned as one large response — the heaviest
   CPU 0 job, and fragile on phones. Build it in the background, then offer a
   normal GET download (like live captures).
5. ✅ *Done (v0.11.76 base; 3 samples ≈ 15 s for both Tight and Constrained):* **Headroom card: CPU 0 must stay high across several readings** before the
   verdict says "Tight", like RAM and swap already do. A single 5 s reading of
   94% turned the field card "Tight". File: `diofinder/sysmon.py`.
6. ✅ *Done (v0.11.76 base):* **Add `ap-watchdog.sh` to the OTA refresh list** in `scripts/diofinder-update`,
   so watchdog fixes reach devices by update.
7. ✅ *Done (v0.11.76 base):* **Cap or prune `/var/lib/diofinder/bg_runs/`** (Background A/B archives);
   only `captures/` has a size cap today.
8. ✅ *Done (v0.11.76 base):* **SkySafari readout-rate hint** in the UI/docs: each SkySafari poll is a new
   TCP connection (11–30 per 5 s seen), and a lower readout rate cuts CPU 0 load.
9. ✅ *Done (v0.11.76 base):* **Test-suite CI workflow** (pytest with numpy + flask), so regressions show
   up before an image build; only `release.yml` exists.
10. *Deferred (a convenience):* **"Promote tested image" release mode**, so the shipped image is
    byte-identical to the one flashed and tested.
11. ✅ *Done: phone button (Utilities → Clock); daemon sets the clock directly (CAP_SYS_TIME — sudo was blocked by NoNewPrivileges); SkySafari threshold 2 s; last-known time restored at start; only this boot's sync counts as the source.* **Clock:** logs before a time sync carry the image build date (no RTC).
    Check whether later boots restore the last-known time, and consider a web
    UI "set time from this phone" button (`diofinder-set-time` exists).
12. *Dropped (cosmetic):* **Cosmetic picamera2 traceback at every boot** from the `tuning_file=` probe;
    try the working `tuning=` path first. Low priority (touches camera init).
13. ✅ *FOV part done (release-candidate group): recentre on the long-run median of window medians, staying calibrated.* **Watch with field data before changing:** FOV drift recommits after
    refocus (4 in a session, 13.50–13.60°); the default-off features awaiting
    on-sky validation (ROI tracking, bg-cache bin-at-submit, IMU Units B/C).
14. ✅ *Implemented behind `imu_report_smoothing` (default off); needs the on-sky A/B.* **Bigger:** the shelved IMU report smoothing (jumpy crosshair while moving;
    `docs/decisions/2026-11-01-imu-report-smoothing-shelved.md`).
15. ✅ *Done:* test mode uses explicit `test_*` values
    when set, otherwise the configured site, otherwise a placeholder — never 0/0.

16. ✅ *Done (v0.11.76 base):* **Record the clock source in live captures:** when and how the clock was
    last set (SkySafari `:SC`, network time), in `session.json`. Satellite
    timing depends on it, and "clock plausible" only rules out a clock years off.
17. ✅ *Done (v0.11.76 base):* **Matched stars in debug bundles too** (`frames.json`), like live captures,
    so any bundle can be fitted and overlaid offline.
18. ✅ *Done (v0.11.76 base):* **Bump the in-code version to 0.11.76** (`diofinder/config.py`); docs already
    say the settle gate arrived in 0.11.76.
19. ✅ *Done (v0.11.76 base):* **Live capture time limit:** optional auto-stop after N minutes, so a
    forgotten capture doesn't run until the size cap.

## To do — satellite tools (`tests/satcheck_overlay.py`)

1. ✅ *Done (v0.11.76 base):* **Hide predictions in Earth's shadow.** (`--show-unlit` shows them) SatChecker lists everything inside
   the circle, lit or not; at night nearly every low-orbit object is in shadow.
   Use the Sun's position and a cylindrical shadow test (as done by hand for
   STARLINK-30913).
2. ✅ *Done (v0.11.76 base):* **Optionally hide near-stationary objects** (`--show-stationary`, `--min-rate`) (geostationary satellites and
   rocket stages), which are too faint for sub-second exposures and cluttered
   the 2026-11-02 report (≈40 of 51 objects in frame).
3. ✅ *Done (v0.11.76 base):* **Score robustly along the line** (trimmed mean: a median would miss dashed stacked trails), so a predicted track
   that merely crosses a bright trail can't score high (FALCON 9 R/B scored 69σ
   that way).
4. ✅ *Done (v0.11.76 base):* **Label each prediction with its direction of travel** (compass point, rate, arrow).
5. ✅ *Done (v0.11.76 base):* **Flag detected trails that match no prediction,** with rate and flash
   pattern, as possible aircraft (`trails.csv`). Each trail is timed frame by frame, so a
   prediction on the same line but moving the other way is rejected. On the 2026-11-02
   capture it independently reports the trail as unidentified/flashing, 0.37°/s toward SW,
   "not STARLINK-30913: moves the opposite way".


## Resource assessment (2026-11-02)

**Web UI clutter is not a resource problem.** Pages poll only while their tab
is visible, and (v0.11.76) only for cards on screen, so cards cost nothing
unless looked at. The Advanced page was the most expensive page to leave open;
it now fetches the live view, camera state, background summary and mount status
only while each is on screen (mount: every 30 s while the link is off).

**Always-on costs (all earn their keep):** SkySafari polling (largest CPU 0 load
while connected; the readout rate is the lever), the IMU reader (10 Hz; OTA cards
now migrate from 20), per-frame shared-state writes (frame timing, solution, IMU
reference), the temporal background cache (100–300 ms rebuilds on the solver
cores: the reason to test bin-at-submit), and small background loops.

### Waiting on a decision (A/B or validation), then keep one and remove the other

1. **Smooth crosshair** (`imu_report_smoothing`): if it wins, remove what it
   replaces: the hold-on-fresh-solve gate, the alpha-beta rate filter, and the
   legacy C-matrix IMU fit (still computed on every moving solve; it only serves
   the `imu_exact_predict=false` fallback).
2. **bg-cache bin-at-submit** (since v0.11.26): would save solver CPU and memory.
3. **ROI tracking mode** (since v0.11.27): the most complex optional solver code;
   run its A/B or remove it.
4. **IMU Unit B (`imu_persist_bno055`) and Unit C (`imu_solve_cal_enabled`)**:
   validate or remove. Unit C's chip-write mode (`imu_solve_cal_write_chip`) was
   **deferred by design, not dropped**: the 2026-07-21 IMU decision staged it
   after Unit C's software correction (Mode 1) is field-validated, and it needs
   Unit B on (it writes through B's channel). Keep the reserved key.
5. **`horizon_reject_enabled`**: low value (blind solves only, needs a correct clock).

### Overlaps / candidates to drop

- `/debug/collect` (old one-shot bundle route): KEPT — it is the documented
  `curl` path; it only accepted POST, so that command failed. Fixed (GET too).
- ✅ *Done:* three tuners → two. Kept auto-tune (live sky) and
  `replay_corpus.py` (laptop); removed the on-device "Tune from burst" card
  (it duplicated replay_corpus while using the Pi's CPU).
- `save_failed_frames` / `save_solved_frames`: KEPT for now —
  `scripts/calibrate_lens.py` (lens distortion fit) reads the frames
  `save_solved_frames` writes. Could go once it reads live-capture zips.
- Polar alignment: stays hidden (decided 2026-11-02).
- ✅ *Done:* background-mode menus trimmed to block_percentile, row_percentile,
  uniform_mean, temporal_median (the rest still work and show while active).
- Kept on purpose: the Legacy/tetra3 baseline (expert-only), debug bundles vs
  live captures (they complement each other), the Background A/B run.

### Still open

- On-sky A/B tests above; check SkySafari now sets the clock (log shows
  `Clock synced … (direct)`; Utilities → Clock names SkySafari).
- ✅ *Done:* live captures capped (`live_capture_keep_mb`, 8000 MB, oldest
  deleted first, newest always kept).
- Mid-exposure IMU timing for the Kabsch pairs and the solve hint (crosshair
  reference only so far).
- Deferred: "promote tested image" release mode; more mount dialects
  (LX200, Alpaca, OnStep).
- ✅ *Done:* OTA cards move `imu_poll_hz` 20 → 10 once (conf migration v2,
  per-rule versions); on-screen-only polling.

## Satellite identification, photometry, IOD reports and orbits — done, awaiting a real capture

Off-device scripts (all in `tests/`, run on a laptop; need numpy, astropy,
matplotlib, Pillow, `sgp4`; no scipy):

- `sat_identify.py ARCHIVE.zip --elements classfd.zip inttles.zip` finds trails
  (stacks) and frame-by-frame tracks (`sat_tracks.py`), identifies them against
  element files with a free time offset, refines the match, measures
  magnitudes and light curves (`sat_photometry.py`), and writes IOD lines, a
  SeeSat-L message draft, refined elements (TLE incl. Alpha-5, OMM
  KVN/XML/JSON/CSV) and `observations.json`.
- `sat_orbit.py fit observations.json [more.json]` fits elements to one or
  more passes; `sat_orbit.py predict fit.json --site-from observations.json`
  lists the next visible passes with pointing tables and timing uncertainty.

What each piece does and how far to trust it:

- **Identification** (unchanged): plane matched, along-track time free;
  verified on synthetic captures drawn from a real classfd element set.
- **Photometry**: the saved 8-bit frames are inverted through the tone curve
  (`rpi.contrast` gamma curve, recorded in `session.json` `tone` from this
  release; older captures assume the shipped finder curve), calibrated per frame
  against the solver's matched stars (zero point + a shared vignetting term),
  and the dash's light is measured in the frame minus the steady sky (stars
  under the trail removed). Expected ±0.2–0.3 mag for magnitude 3–6; a colour
  term of 0.15 mag is included in the reported uncertainty. Saturated dashes
  are reported as a bright limit and their magnitude is left out of the IOD
  lines. A crushed sky (most pixels 0) biases faint trails slightly faint.
- **Light curves**: one sample per 3 px along each dash (time from the
  exposure window), or one value per frame for dots (hand-tracked). Lomb-
  Scargle (astropy power, numpy false-alarm estimate) gives a flash period;
  behaviour code S / F / R / I / X. Periods shorter than ~4 px of travel along the dash
  are not claimed (the PSF and the pixel grid fake them).
- **IOD reports**: `--points-per-pass` (default 4) lines spread over the arc,
  preferring dashes wholly inside the frame; position uncertainty = the arc's
  own scatter combined with the frame WCS fit; time uncertainty from the
  capture's clock record (network time 0.05 s, phone 0.2 s, SkySafari 1 s,
  otherwise 10 s with a "do not post" warning; `--time-unc` overrides);
  `--status` sky code E/G/F/P/B/T; unidentified objects as `99999 99 099A`
  (the SeeSat-L sample's convention; `--unknown-desig`), named `unkYYMMDD`.
  Checked against G. Lewis' IOD Version 0 description (2002 clarification):
  angle format 2 gives its position uncertainty in arcminutes (fixed: it was
  written in degrees, 60x too pessimistic); behaviour S/F/R/I/X; magnitude
  cols 67–70, uncertainty 72–73, flash period 75–80 as implemented. The same
  points also go out in R.D.E. format (`observations_rde.txt`). Still to
  confirm on the list: the unknown-object convention, and whether the R.D.E.
  piece numbering skips I and O (assumed, as COSPAR letters do).
- **Orbits** (`sat_orbit.py`): one pass → a circular orbit (4 elements)
  with an explicit 4.5 % mean-motion model error (simulated: a circular fit
  to an e = 0.046 pass is 6 % off), so the next pass's timing is minutes
  uncertain and later passes are flagged "timing UNKNOWN". Two passes hours
  apart → all 6 elements with the revolution count resolved by a mean-motion
  scan and the eccentricity by multi-start; simulated position error ~1–2 km
  after 1–3 days, timing ±1 s. A known element set (`--start`) with one pass
  fits only node and mean longitude, like `sat_identify`'s refinement.

### Ground truth: an ISS pass (`tests/iss_check.py`)

Capture an ISS pass, then `python tests/iss_check.py livecap_….zip` (online:
CelesTrak's current ISS elements are the truth). It reports the across-track
error (astrometry; expect ~1 px), the along-track offset in seconds (your
clock error; the ISS elements are good to < 0.1 s near their epoch), a
one-pass orbit fitted WITHOUT the ISS elements against them, and the
magnitude against a rough prediction (the ISS usually saturates: a bright
limit). Caveats: use elements fetched within a day or two of the pass (a
reboost moves the ISS); pick a pass in darkness with the ISS sunlit and at
least ~30 deg up; short exposures (0.2-0.3 s) keep the dashes inside the
frame at ~1 deg/s. Synthetic test: 0.7 s clock error recovered to 0.001 s.

### Element sources (all scripts)

Files (TLE/2LE/3LE, OMM XML/KVN/JSON/CSV), .zip/.gz of them, folders, URLs
(cached 2 h), comma lists; newest epoch per object wins. Not accepted (not
needed yet): ephemerides (CCSDS OEM state vectors, e.g. NASA's ISS
trajectory file, or SP3) — an even better ISS truth than a TLE, worth adding
if the TLE comparison ever becomes the limit.

### Hand-tracking a satellite for a long arc — implemented, untested on sky

What is in place: `sat_tracks.py` subtracts the steady sky built from
neighbouring frames (aligned on their own WCS) from each frame, so an object
that the scope follows (a dot near the centre while the stars stream through)
is found; residual blobs are linked frame to frame along a smooth sky path
(missed frames allowed), hot pixels / fixed-pattern defects (same pixel in
many frames) and stationary objects are dropped, and a linked track that
continues a stack trail replaces it. Tested on a synthetic 24-frame tracked
pass (~10° arc across many fields; identified, 0.002° rms).

Flags for trying it:

- Keep trail rejection off on the finder (`detect_max_axis_ratio` 0, the
  default): while panning the stars are short trails and must still be used.
- Use short exposures (0.1–0.3 s) and keep the push steady; a frame that does
  not plate-solve has no WCS and is left out of the track (interpolated WCS is
  off by default: `--link-interp`).
- Plate solving while panning by hand has **not** been tested on sky. If it
  fails, the fallback would be solving with the IMU hint on, or pausing briefly
  every few seconds.
- The linker costs ~0.4 s per frame on a laptop (a 10-minute capture at 1 fps
  is ~4 minutes).

### Raw (12-bit) frames — not needed

The cost of saving the sensor's raw frames was storage and Wi-Fi transfer, not
CPU: at full resolution a raw frame is ~18.5 MB (2028×1520 binned: ~6 MB)
against ~0.2 MB for the 8-bit PNG, so a 10-minute capture would be several GB.
Writing them is cheap on the CPU (no PNG compression). Photometry does not
need them: the tone curve is invertible to ±0.2–0.3 mag. If finer photometry
is ever wanted, a middle option is a 2×2-binned 16-bit raw (~1014×760, ~1.5 MB
per frame) saved alongside the PNGs.

Still needed: a capture with real sunlit satellite trails (dusk, 1–2 h after
sunset), ideally one fixed-field and one hand-tracked pass, and a second pass
of the same object to try the two-pass orbit fit.
