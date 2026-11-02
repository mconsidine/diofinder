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
   validate or remove; Unit C's chip-write mode was never implemented: remove.
5. **`horizon_reject_enabled`**: low value (blind solves only, needs a correct clock).

### Overlaps / candidates to drop

- `/debug/collect` (old one-shot bundle route): unused by the web UI; drop.
- Three tuners: auto-tune (live), the "Tune from burst" card (on device),
  `replay_corpus.py` (off device): keep two.
- `save_failed_frames` / `save_solved_frames`: live capture covers their use.
- Polar alignment: hidden from the menu since earlier; restore or remove.
- Background modes: 8 in the menu; trim to the ones in use (zero cost otherwise).
- Kept on purpose: the Legacy/tetra3 baseline (expert-only), debug bundles vs
  live captures (they complement each other), the Background A/B run.

### Still open

- On-sky A/B tests above; check SkySafari now sets the clock (log shows
  `Clock synced … (direct)`; Utilities → Clock names SkySafari).
- Live captures are never pruned: add a size cap like bundles and A/B runs.
- Mid-exposure IMU timing for the Kabsch pairs and the solve hint (crosshair
  reference only so far).
- Deferred: "promote tested image" release mode; more mount dialects
  (LX200, Alpaca, OnStep).
- ✅ *Done:* OTA cards move `imu_poll_hz` 20 → 10 once (conf migration v2,
  per-rule versions); on-screen-only polling.

## Satellite identification and element refinement — steps 1–3 done (`tests/sat_identify.py`)

Goal: an off-device script that takes a live capture, finds the satellite
trails in its stacked frames, and identifies them, including against
classified-object element sets when SatChecker has no match.

Assessment:

- **Identify first, derive second.** A finder trail spans 10–60 s from one
  site. Deriving a usable TLE from scratch from such a short single arc is
  ill-conditioned: the direction and rate are well measured, the range (so the
  altitude and the orbit) is not. With an assumed circular orbit you get a
  coarse orbit, good as a search key, not as a TLE to publish.
- **Old element sets are not a blocker.** SGP4 propagates either way in time;
  a weeks-old element set keeps its orbital plane far longer than its timing.
  So match on the plane (cross-track position, direction, rate) and let the
  along-track time float (a fitted "N seconds early/late"). The best match's
  elements can then be refined to the observation (mean anomaly and mean
  motion), which is the honest form of "an estimated TLE".
- **Element sources:** CelesTrak GP (public catalog), CelesTrak supplemental GP
  (operator data, e.g. Starlink, more accurate), Mike McCants' classified file
  (`classfd.tle`; amateur-tracked classified objects). Space-Track needs an
  account. SatChecker covers the public catalog only, so the classified file is
  where the extra matches would come from. URLs to confirm on your laptop (this
  sandbox cannot reach them).
- **Output observations in the standard IOD format** too, so existing tools can
  take over: Bill Gray's Find_Orb (fits element sets, including TLEs, to
  satellite astrometry) and sat_id, Scott Campbell's satfit (the Python port
  from the TruSat project), and the SeeSat-L community.
- **Accuracy budget:** ~1 px (51″) across-track; along-track is set by the
  clock: 0.1 s at 0.5°/s is ~3.5 px, so set the clock from the phone first.
  Exposure start/end times come from the sensor timestamps already recorded.

Status (2026-11-02): steps 1–3 below are implemented in `tests/sat_identify.py`
(per-frame observations as IOD + JSON; identification against McCants/CelesTrak
files with a free time offset; refinement; output as classic TLE incl. Alpha-5,
CCSDS OMM KVN/XML and OMM JSON/CSV). Verified end to end on a synthetic capture
drawn from a real classfd element set (found among 436, timing error recovered to
<1 s, refined elements reproduce the pass). On the 2026-11-02 capture the
aircraft trail is correctly unmatched. Still needed: a capture with real sunlit
satellite trails; step 4 is not started.

Plan (each step useful on its own):
1. Per-trail observations: from the frame-by-frame timing `satcheck_overlay`
   already does, RA/Dec at exposure start/mid/end per frame; write IOD + JSON.
2. Identify: local SGP4 (`sgp4` package) against downloaded element files;
   score cross-track distance, direction and rate with a free time offset;
   report candidates with their offset; shadow test as now.
3. Refine the best candidate's elements to the observations (2 parameters).
4. Optional, research-grade: circular-orbit fit from the arc alone, flagged as
   such; validate on a capture with a known Starlink pass.

Needs: a capture with real sunlit satellite trails (dusk, 1–2 h after sunset)
for validation; the 2026-11-02 capture's only trail was an aircraft.
