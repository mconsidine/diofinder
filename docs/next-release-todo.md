# Next release — to-do list (after v0.11.75)

Working branch: `claude/azimuth-on-0.11.74`. Items are grouped by area; each
says why it matters. Nothing under "To do" has been started.

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

1. **Pause page polling in hidden browser tabs.** The live view (Home and
   Advanced) and the status pollers keep running in background tabs; desktop
   browsers only slow them. Pause on `visibilitychange`, resume on show.
   Files: `webui/templates/home.html`, `camera.html`, other pages with
   `setInterval`.
2. **Default IMU sample rate 20 Hz → 10 Hz** (`imu_poll_hz`). Consumers need far
   less than 20 Hz (SkySafari ~4/s, solves ~3/s, slew detector compares ~0.8 s
   apart); halves the IMU's traffic through CPU 0. Devices with a saved rate
   keep it. Files: `diofinder/config.py`, `etc/diofinder.conf.default`, docs.
3. **Debug bundle: match each frame to its own solve.** At 0.3 s exposures every
   frame in a bundle had `solution: null` (the solver had moved on before the
   web UI asked). Have the solver keep the last few solutions and look them up
   by seq. Files: `webui/app.py` `_solution_for_seq`, `diofinder/solver_proc.py`,
   comms maint command.
4. **Debug bundle: build to a file, serve a download link.** It is built entirely
   in web UI memory on CPU 0 and returned as one large response — the heaviest
   CPU 0 job, and fragile on phones. Build it in the background, then offer a
   normal GET download (like live captures).
5. **Headroom card: CPU 0 must stay high across several readings** before the
   verdict says "Tight", like RAM and swap already do. A single 5 s reading of
   94% turned the field card "Tight". File: `diofinder/sysmon.py`.
6. **Add `ap-watchdog.sh` to the OTA refresh list** in `scripts/diofinder-update`,
   so watchdog fixes reach devices by update.
7. **Cap or prune `/var/lib/diofinder/bg_runs/`** (Background A/B archives);
   only `captures/` has a size cap today.
8. **SkySafari readout-rate hint** in the UI/docs: each SkySafari poll is a new
   TCP connection (11–30 per 5 s seen), and a lower readout rate cuts CPU 0 load.
9. **Test-suite CI workflow** (pytest with numpy + flask), so regressions show
   up before an image build; only `release.yml` exists.
10. **"Promote tested image" release mode**, so the shipped image is
    byte-identical to the one flashed and tested.
11. **Clock:** logs before a time sync carry the image build date (no RTC).
    Check whether later boots restore the last-known time, and consider a web
    UI "set time from this phone" button (`diofinder-set-time` exists).
12. **Cosmetic picamera2 traceback at every boot** from the `tuning_file=` probe;
    try the working `tuning=` path first. Low priority (touches camera init).
13. **Watch with field data before changing:** FOV drift recommits after
    refocus (4 in a session, 13.50–13.60°); the default-off features awaiting
    on-sky validation (ROI tracking, bg-cache bin-at-submit, IMU Units B/C).
14. **Bigger:** the shelved IMU report smoothing (jumpy crosshair while moving;
    `docs/decisions/2026-11-01-imu-report-smoothing-shelved.md`).
15. **Needs a decision:** whether test-mode defaults should use `test.png`'s real
    capture site/time (would publish a location).

## To do — satellite tools (`tests/satcheck_overlay.py`)

1. **Hide predictions in Earth's shadow.** SatChecker lists everything inside
   the circle, lit or not; at night nearly every low-orbit object is in shadow.
   Use the Sun's position and a cylindrical shadow test (as done by hand for
   STARLINK-30913).
2. **Optionally hide near-stationary objects** (geostationary satellites and
   rocket stages), which are too faint for sub-second exposures and cluttered
   the 2026-11-02 report (≈40 of 51 objects in frame).
3. **Score with the median along the line, not the mean,** so a predicted track
   that merely crosses a bright trail can't score high (FALCON 9 R/B scored 69σ
   that way).
4. **Label each prediction with its direction of travel.**
5. **Flag detected trails that match no prediction,** with rate and flash
   pattern, as possible aircraft.
