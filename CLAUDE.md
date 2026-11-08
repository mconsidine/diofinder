# diofinder — developer guide

## Target platform

- **Hardware**: Raspberry Pi Zero 2W (quad-core Cortex-A53, 512 MB RAM)
- **Camera**: Arducam IMX477 (12 MP Sony sensor, 1.55 µm pixel pitch, 4056×3040 array)
- **OS**: Debian GNU/Linux 13 "Trixie" (Pi OS Trixie Lite)
- **Python**: 3.11+, installed at `/opt/diofinder/`
- **Service**: `systemd` unit `diofinder.service`, managed with `sudo systemctl {start,stop,restart,status} diofinder`
- **Host serial tether**: the USB CDC-ACM serial device enumerates as
  `/dev/ttyACM0` on Linux but **`/dev/tty.usbmodem*`** on macOS (digits vary per
  port/session; `ls /dev/tty.usbmodem*` to find it). There is no `/dev/ttyACM0`
  on macOS.

---

## Process architecture

```
diofinder_main.py (launcher, CPU 0)
  │── imu_thread (daemon, in-process) — BNO055 quaternion reader at 10 Hz (`imu_poll_hz`)
  │
  ├── camera_proc   (CPU 3)          — captures frames → shared memory
  ├── solver_proc   (CPUs 1+2+3)     — extracts stars, plate-solves
  └── comms_proc    (CPU 0)          — LX200 TCP server + maintenance socket
```

The IMU thread runs inside the **launcher** process (same CPU 0 as comms,
but no GIL contention with the LX200 handler).

comms/webui share CPU 0 with the kernel (both are I/O-bound; kernel+IRQ load
is far below one core), freeing CPU 1 as a third solver core. CPU affinity is set with `os.sched_setaffinity`.

See `docs/imu.md` for a rendered flowchart of how the BNO055 feeds
position/motion info (the IMU reader, the solve-hint / slew-detection /
LX200-pointing consumers, and the calibration loop).

### Inter-process communication

| Channel | Type | Direction | Purpose |
|---------|------|-----------|-------|
| `diofinder_frame_{0,1,2}` | POSIX shared memory | camera → solver, webui | Raw 8-bit frames |
| `diofinder_display` | POSIX shared memory (seqlock) | solver → webui | Live-view frame, written only while a viewer is active (`display_wanted_until`); web UI reads direct, no maint round-trip |
| `FrameSlots` | `multiprocessing.Value` + lock | camera → solver | Ring-buffer slot index |
| `latest_solution` | `Manager().dict()` | solver → comms | Most recent plate-solve result |
| `shared_cfg` | `Manager().dict()` | main → all | Live-mutable settings |
| `align_request_q` | `mp.Queue` | comms → solver | :CM# alignment requests |
| `align_response_q` | `mp.Queue` | solver → comms | Alignment results |
| `solver_cmd_q` | `mp.Queue` | comms → solver | Maintenance commands |
| `solver_cmd_reply_q` | `mp.Queue` | solver → comms | Maintenance replies |
| `camera_cmd_q` | `mp.Queue` | comms → camera | Exposure/gain commands |
| `camera_cmd_reply_q` | `mp.Queue` | camera → comms | Camera replies |
| `/run/diofinder/maint.sock` | Unix domain socket | webui/ctl → comms | JSON maintenance API |

**Manager snapshot rule.** Take a multi-key snapshot of `shared_cfg` /
`latest_solution` with `diofinder.mp_util.snapshot(proxy)` (= `proxy.copy()`,
ONE IPC round-trip). **Never `dict(proxy)`**: on a `DictProxy` that is
`keys()` plus one `__getitem__` RPC *per key* — N+1 round-trips to the Manager
server on CPU 0 (measured 9.3 ms vs 0.14 ms for 81 keys on x86; worse on the
A53). Previously the solver's per-frame snapshot and comms' `:GR/:GD` /
`status` snapshots all used `dict(proxy)`. High-rate loops (the IMU reader)
should also TTL-cache their settings reads rather than `.get()` per sample.

### `shared_cfg` keys and owners

| Key | Type | Written by | Read by |
|-----|------|-----------|--------|
| `boresight_y`, `boresight_x` | float | comms (via :CM# or maint) | solver, comms, webui |
| `detect_sigma` | float | comms (via maint) | solver |
| `detect_bg_mode` | str | comms (via maint, incl. `seeing_set`) | solver |
| `detect_bin` | int (1/2/4) | config file only (restart; cache is built at one binning) | solver |
| `bg_cache_bin_at_submit` | bool | comms (via maint `solver_params_set`) | solver bg_cache (P5 opt-in; A/B before default) |
| `detect_kernel_sigma` | float (1.0–4.0) | comms (via maint / `seeing_set`) | solver (sycamore≥0.12) |
| `detect_max_axis_ratio` | float (0=off, else 1.5–10.0) | comms (via maint / `seeing_set`) | solver |
| `detect_local_noise` | bool | comms (via maint) | solver (sycamore≥0.12) |
| `detect_tophat_radius` | int | comms (via maint) | solver |
| `detect_bg_block_size` | int | comms (via maint) | solver, bg_cache |
| `detect_uniform_filter_size` | int | comms (via maint) | solver |
| `detect_noise_mode` | str (`mad`/`global_rms`) | comms (via maint) | solver |
| `extractor_backend` | str (`sycamore`/`tetra3`) | comms (via maint `solver_params_set` / `seeing_set`) | solver |
| `min_centroids` | int | comms (via maint / `seeing_set`) | solver |
| `max_solve_stars` | int (4–200) | comms (via maint `solver_params_set`) | solver |
| `fov_max_error_deg` | float (0.05–5.0) | comms (via maint `solver_params_set`) | solver (calibrator, loose/blind tolerance only) |
| `solve_timeout_ms` | int | comms (via maint / `seeing_set`) | solver |
| `match_radius` | float (0.005–0.05) | comms (via maint / `seeing_set`) | solver |
| `match_threshold` | float (1e-9–1e-3) | comms (via maint / `seeing_set`) | solver |
| `seeing_mode` | str (`good`/`bad`) | comms (via `seeing_set`) | comms, webui |
| `tracking_enabled` | bool | comms (via maint `solver_params_set`) | solver |
| `tracking_window_px` | int | comms (via maint `solver_params_set`) | solver |
| `tracking_min_recover` | int | comms (via maint `solver_params_set`) | solver |
| `test_mode` | bool | comms (via maint) | camera |
| `auto_exposure_enabled` | bool | comms (via maint `auto_exposure_set`) | comms auto-exposure thread |
| `auto_exposure_target_stars` | int | comms (via `seeing_set`) | comms auto-exposure thread |
| `auto_exposure_target_matches` | int | comms (via `seeing_set`) | comms auto-exposure thread |
| `auto_exposure_max_s` | float | comms (via `seeing_set`) | comms auto-exposure thread |
| `auto_exposure_max_gain` | float | comms (via `seeing_set`) | comms auto-exposure thread |
| `auto_exposure_peak_floor` | float | comms (via maint `auto_exposure_set {"peak_floor":...}`) | comms auto-exposure thread |
| `imu_rate_gate_dps` | float | comms (seed from cfg; live via `solver_params_set` — Camera-page "IMU pointing gate" slider, v0.11.50) | comms LX200 pointing (rate gate in `_imu_predict`; raise to stop a parked scope's SkySafari jitter) |
| `imu_predict_hold_solve_s` | float | comms (seed from cfg; `solver_params_set`) | comms LX200 pointing (P1 — `_imu_predict` reports a solve fresher than this instead of IMU extrapolation; the SkySafari reticle-oscillation fix. 0 disables) |
| `imu_predict_enabled` | bool | comms (seed from cfg; `solver_params_set` — Camera-page "Use IMU between solves" toggle) | comms LX200 pointing (master switch; false → `_imu_predict` returns None so `:GR/:GD` report plate solves only, IMU never used for pointing. Solve hint + slew detection still use the IMU) |
| `imu_enabled` | bool | main (seed from cfg); comms (`solver_params_set` — Camera-page "IMU sensor" toggle) | imu_thread (park switch; false → the reader thread stops sampling/publishing and drops `imu_available`, the same end state as unplugging the BNO055. Reversible; re-enabling re-probes) |
| `imu_poll_hz` | int (1–50) | main (seed from cfg); comms (`solver_params_set`) | imu_thread (reader poll rate; read live each loop. 10 = shipped since v0.11.76 (was 20); 5–10 cuts per-sample Manager IPC on CPU 0 with no visible pointing change) |
| `imu_hunt_filter` | bool | main (seed from cfg) | imu_thread (P3 — `_hunt_filter` damps BNO055 fusion hunting in the published quaternion; real motion snaps through. false = raw passthrough) |
| `imu_report_smoothing`, `imu_report_gain` | bool / float (0.05–1) | comms (seed from cfg; `solver_params_set` — Advanced-page "Smooth crosshair (experimental)" toggle) | comms `_report_radec` (opt-in report smoother, `diofinder/report_filter.py`; off = shipped path byte-for-byte); imu_thread (publishes a 2.5 s `(t, q)` history as the `imu` tuple's 3rd element while on); solver (`_mid_exposure_imu_q` anchors `imu_ref` at the frame's mid-exposure while on) |
| `imu_exact_predict` | bool | comms (seed from cfg; `solver_params_set`) | comms LX200 pointing (kill switch for the exact quaternion path; false → legacy C-matrix) |
| `report_epoch` | str (`jnow`/`j2000`) | comms (seed from cfg; `solver_params_set`) | comms LX200 boundary — precesses J2000→JNow for `:GR/:GD` + the status `report_*`, JNow→J2000 for the `:CM#` align target (v0.11.53). `j2000` = raw kill switch |
| `mount_enabled`, `mount_mode`, `mount_epoch` | bool / str (`manual`/`auto`) / str (`jnow`/`j2000`) | comms (seed from cfg; `mount_set`) | comms `_mount_loop` + `mount_sync`/`mount_status` — outbound SynScan mount sync (the mount's OWN epoch, distinct from `report_epoch`). `mount_auto_*` gates are read live too. Transport keys (`mount_protocol`/`mount_serial_port`/`mount_serial_baud`) are cfg-only (restart) |
| `live_capture` | dict `{on, id, site, test_mode}` | comms (maint `live_capture_start`/`_stop`) | solver `LiveRecorder` (start/stop; `site` = lat/lon/elevation from the conf at start) |
| `matches_wanted_until` | float (monotonic) | comms (maint `matches_want`, ≤120 s; debug bundles) | solver (`return_matches` on while fresh or while live capture records; matches kept per seq for `solution_for_seq`) |
| `live_capture_status` | dict | solver `LiveRecorder` (≤1/s while recording, + on state changes) | comms `live_capture_status` → Home/Utilities pollers |
| `display_wanted_until` | float (monotonic) | comms (via maint `display_start` keepalive) | solver (demand-gates the `diofinder_display` write; no viewer → no copy) |
| `star_name_brightest` | bool | comms (via maint `solver_params_set`) | solver (centered-star naming) |
| `star_name_whole_fov` | bool | comms (via maint `solver_params_set`) | solver (centered-star naming — brightest anywhere in the frame vs within `star_name_radius_deg` of boresight; implies brightest mode; an align anchor when boresight is off) |
| `star_name_dso` | bool | comms (via maint `solver_params_set`) | solver (centered-**object** label — names the Messier DSO the aim point is on, from `messier.csv`; sets separate `dso_*` solution fields; display only) |
| `imu_available` | bool | imu_thread | comms, webui |
| `imu` | tuple ((w,x,y,z), t[, history]) | imu_thread | comms, solver (via `imu_math.get_imu_qt`; ONE atomic write per read since v0.11.24; a 3rd element, the last 2.5 s of `(t, q)` samples, only while `imu_report_smoothing` is on — the split `imu_q`/`imu_t` keys are legacy-read only) |
| `imu_ref_q/ra/dec/roll/t` | varies | *(no longer published since v0.11.24 — readers use the `imu_ref` tuple; comms still accepts them from older solvers)* | — |
| `imu_ref` | tuple (q, ra, dec, roll, t, sky_q) | solver (post-solve, single RPC) | comms LX200 pointing (tear-proof reference). `sky_q` (the solved attitude quaternion, v0.11.23) enables the exact frame-corrected prediction; comms still accepts 5-tuples from older solvers |
| `solver_busy_t` | float | solver (set_db load window) | comms watchdog (skips enforcement while fresh, 120 s bound) |
| `imu_frame_R` | list[9] or None | solver (quality-gated Kabsch fit post-solve) | solver hint path (body→camera delta conjugation) |
| `imu_frame_quality` | dict | solver | webui/diagnostics |
| `imu_calib_n`, `imu_calib_quality`, `imu_calib_C` | varies | solver | comms |
| `fov_deg` | float | solver (post-calibration) | comms, webui |

---

## Camera sensor mode — critical detail

`camera_proc._init_camera` requests output size `(960, 760)` via
`create_still_configuration`. Without a `sensor=` hint, libcamera silently
selects the **1332×990 sub-mode** (2×2-binned readout from only the central
~65 % of the sensor), which reduces the effective FOV from the expected ~13.5°
to ~8.8° for a 25 mm lens.

The fix — already applied — is to pass `sensor={"output_size": (4056, 3040)}`
so the ISP downscales from the full array with a uniform 4× factor:

```
Full sensor 4056 × 1.55 µm = 6.287 mm
Crop to 3840 wide (aspect-ratio match), scale 4× → 960 px
Effective pixel = 1.55 × 4 = 6.2 µm
Plate scale (25 mm FL) = 6.2 × 206.265 / 25 = 51.15 arcsec/px
FOV = 960 × 51.15 / 3600 = 13.64°
```

The `sensor_full_width` / `sensor_full_height` config keys select the sensor
mode. **Default since v0.11.15 is 2028×1520 — the IMX477's 2×2-binned
*full-FOV* mode**: same 13.64° FOV and plate scale after ISP scaling, ~4× less
sensor/ISP/memory bandwidth, a 40 fps mode ceiling, and slightly better SNR
from on-sensor binning. Set 4056×3040 to revert to the full-resolution readout
(conf edit + restart). Measured on-sky FOV in the binned mode is **~13.54°**
(bundle replay, n=12: 13.515–13.557) vs the theoretical 13.64 — the shipped
`fov_deg`/`arcsec_per_pixel` are recentered there (v0.11.17) so the calibrated
±0.1° window brackets the true value with margin.

**Capture cadence:** `_init_camera` passes `buffer_count=2` and drops the RAW
stream (`raw=None`, with a legacy fallback for older picamera2). With the
still-configuration default of one buffer the sensor cannot expose frame N+1
while frame N's buffer is held, so the frame period collapses to ~2× the
exposure time (measured: 1.0 s exposure → 0.5 fps). Two buffers restore
~1/exposure cadence — double the solve rate at long exposures — and dropping
the unused RAW stream (~18.5 MB CMA per buffer at full res) more than pays for
the second main buffer.

**Camera-settings epoch:** every successful *effective* exposure/gain change bumps
`shared_cfg["camera_settings_epoch"]` (single writer: camera_proc). The
solver forwards it to `bg_cache.note_camera_settings`, which flushes the
temporal frame stack and marks the model stale — frames captured at the old
setting don't share the new pedestal, so a stack spanning an auto-exposure
step (×1.5) would under-/over-subtract for up to two stack periods. No-op
sets (restoring an identical value, e.g. auto_tune/dark_capture restore
paths) do **not** bump the epoch (v0.11.20), and a mid-build flush discards
the in-flight model (generation guard in `bg_cache`) instead of being
clobbered by its publish. Capture failures no longer publish the stale
buffer — the camera backs off 0.5 s and retries, so a dead camera surfaces
as a stale epoch rather than frozen-but-fresh pointing.

---

## Libcamera tuning

The shipped default is **`imx477_finder.json`** (repo `tuning/`, installed to
`/usr/share/libcamera/ipa/rpi/vc4/` by `install.sh`; `camera_tuning_file` in
`etc/diofinder.conf.default` points at it). The `config.py` dataclass default
is the same file (it was `imx477_scientific.json` until the 2026-11-04
default audit), so a conf that omits the key runs the camera a fresh image
runs; a missing file falls back to libcamera's default tuning. Three profiles
switch live via `tuning_set` (`finder`/`scientific`/`standard`; restart
required); the Camera/Status pages and `diofinder-ctl` expose them.

`imx477_finder.json` is **derived from `imx477_scientific.json`** with exactly
two edits: `rpi.dpc` `strength: 0` (DPC deletes 1–2 px faint stars — its defect
signature is identical to a faint star) and a steeper asinh `rpi.contrast`
`gamma_curve` (companding so faint stars survive the 12→8-bit reduction). These
two are the *only* detection-hostile stages that **no libcamera runtime control
can disable**. Everything else is neutralized at runtime in `camera_proc.py`
`_init_camera` (`AeEnable=False`, `AwbEnable=False`, `NoiseReductionMode=0`,
`Sharpness=0`, `Saturation=0`), so `agc`/`awb`/denoise/`sharpen`/`ccm` need no
tuning edit, and `black_level`/`geq` are benign and left as-is. Full
stage-by-stage analysis: `docs/imx477-tuning-comparison.md`.

---

## FOV calibration

The solver accumulates a rolling window of 30 solved FOV measurements.
Once the window standard deviation falls below `fov_calibrated_stddev` (default
0.05°), `fov_calibrated` is set `true` in the config file and the search
tolerance tightens from `fov_max_error_deg` (0.3° since v0.11.21) to
`fov_calibrated_max_error_deg` (0.1°). This makes subsequent solves faster
and more robust against false positives.

**Escape-hatch escalation (v0.11.24)**: every 3rd FallbackGate fire retries
with `fov_estimate=None` — olive-solve then searches the database's full FOV
range — so a lens change (e.g. 25 mm → 50 mm) can recover; the ±0.3° loose
window around the old committed estimate mathematically never could. Gate
streak/fire counters are surfaced in `calibration_status` (`fallback` key).
**When changing lenses** also set `fov_deg` in the conf to a rough estimate
for the new lens — the reset procedure below clears the calibration state
but keeps `fov_deg` as the starting search estimate.

**Self-healing (v0.11.19).** Two mechanisms keep a wrong committed FOV from
persisting:

* **Drift recentre**: every 50 solves the window median joins a short
  history (5); the **long-run value** = the median of those window medians
  (≥ 3 needed) is compared against the committed value, with a dead band of
  3× the **measured window stddev** (floored at `fov_drift_stddev_floor` =
  0.01°). Past it the commit is **recentred on the long-run value directly**,
  staying CALIBRATED (tight tolerance kept). The old dead band used the 0.05°
  convergence constant (0.15° band, ~44× real measurement noise), which let the
  13.64-vs-13.55 sensor-mode miscentering sit uncorrected forever; v0.11.19–75
  then compared each single window and dropped to CALIBRATING, which chased
  the ±0.05° field-to-field wander (field log 2026-11-01: four recommits in an
  evening, 13.504–13.599, one 0.06° off truth). `calibration_status` shows
  `window_medians` and `long_run_median`.
* **Failure-driven loose fallback** (`FallbackGate`, config
  `fov_fallback_fails` = 20, 0 disables): after 20 consecutive failed solve
  *attempts* (detection healthy — the solver only attempts with
  ≥ min_centroids stars), the solver retries the same centroids with the
  loose `fov_max_error_deg` window and **no attitude hint**, repeating every
  10th failure. A retry that solves at a FOV outside the tight window calls
  `force_recalibrate()` so the calibrator relearns. One mechanism escapes both
  self-sustaining failure classes: a committed FOV that excludes reality (the
  calibrator only learns from successes) and a poisoned hint (on solver
  wheels without the blind-fallback pass).

If you change the lens or camera mode, reset calibration:
```bash
sudo sed -i 's/^fov_calibrated:.*/fov_calibrated: false/' /etc/diofinder/diofinder.conf
sudo systemctl restart diofinder
```

Or use the **Config** page → calibration section → Reset button.

---

## Adding an LX200 command

All LX200 handling lives in `diofinder/comms_proc.py::_handle_lx200_command`.

1. Add an `if cmd == ":XX":` (or `cmd.startswith(":XX")`) branch.
2. Return a `bytes` object ending in `b"#"` per LX200 convention, or `b""` for
   commands that expect no reply (`:M*`, `:R*`, `:Q`).
3. Update the docstring listing implemented commands.
4. Test with `nc diofinder.local 4060` (telnet/netcat) or add a simple test to `tests/diag_solve.py`.

There is no registration table — the function is a plain if/elif chain.

**Worker-pool server (v0.11.58, was per-connection threads in v0.11.52).**
`_serve_lx200`'s accept loop only enqueues sockets; a fixed pool of
`_LX200_POOL_WORKERS`=8 long-lived workers drains a bounded queue
(`_LX200_QUEUE_MAX`=16, overflow sheds) and each runs `_serve_lx200_client` to
completion — not one-at-a-time, so a blocking `:CM#` align or a half-open phone
occupies one worker but can't starve other clients' `:GR/:GD` polls (the
reconnect→broken-pipe storm). The pool replaced thread-per-connection because
SkySafari opens a **new TCP connection per poll** (readout-rate reconnect storm,
~4/s at rate 4) — spawning/tearing down a thread that often loaded the Zero 2W's
shared CPU 0 (transient "camera unavailable"/jitter); the pool has zero
per-connection thread churn (`docs/lx200-connection-pool-design.md`). Same 8-way
concurrency ceiling and shed-on-overload as the old semaphore cap.
`_handle_lx200_command` runs concurrently across workers; the only
shared-mutation hazard is the `:CM#` align exchange on the shared
`align_response_q`, serialized by `_align_lock`. **Held sync (v0.11.54).** A `:CM#` must survive a marginal sky:
the solver keeps the pending align request across frames (`_align_promote`) and
only replies FAILURE when the ~13 s hold window expires — *not* on the first
frame that fails to solve. Before this, the request was consumed and failed on
frame one, so a sync issued during a solve drought (≈50 % NoMatch is common)
died instantly and never moved the boresight; the 15 s comms timeout
(`CommsAlignState.DEFAULT_TIMEOUT_S`, whose comment always promised "several
attempts") was never actually used for retries. `:GR/:GD` **hold the last solved RA/Dec** during a solve drought
(it lingers in `latest_solution` because `_empty_solution` omits `ra_deg` and
the publish is a dict *merge*); the `status` maint result exposes
`pointing_age_s` + `pointing_stale` (> `_POINTING_STALE_S`=10 s) so the web UI
can flag a held-but-old crosshair.

**Epoch boundary (v0.11.53).** diofinder solves in **J2000/ICRS** internally
(the Gaia/Hipparcos catalog frame — no precession anywhere in the pipeline).
SkySafari's LX200 link and OnStepX use **JNow**, so `diofinder/precession.py`
(IAU 1976, `math`-only) converts *only at the comms boundary*: outbound
`:GR/:GD` (and the status `report_ra_deg/dec_deg`) J2000→JNow via
`_report_radec`; inbound the `:CM#` align target JNow→J2000 in `_do_alignment`.
Everything internal (solver, `imu_ref`, boresight, calibration) stays J2000.
Gated by `shared_cfg["report_epoch"]` (`jnow` default; `j2000` = raw kill
switch). Reused by the planned OnStep sync (`docs/onstep-design.md`).

**Longitude boundary.** Same rule for site longitude: everything inside
diofinder (`longitude_deg`, `test_longitude_deg`, the Config page "+E",
`horizon.local_sidereal_time_deg`) is **East-positive**, but LX200 `:Sg`/`:Gg`
are **West-positive** (Meade; SkySafari, OnStep, AstroKeith's eFinder). `:Sg`
converts in via `horizon.lx200_longitude_to_east`, `:Gg` out via
`east_longitude_to_lx200`. Before this, `:Sg` stored the raw value, so a US site
came out ~145° of sidereal time off (wrong live Alt/Az and horizon-reject).
**Test-mode site (v0.11.76):** `horizon.test_site(cfg)` = explicit `test_latitude_deg`/`test_longitude_deg` (0,0 or the old shipped 40/-75 placeholder count as unset) > the real site > 40 N / 75 W; never 0,0. Used by test-mode Alt/Az and by live captures started in test mode.
`test_epoch_utc` is parsed by `horizon.parse_utc_timestamp`, which takes any
ISO-8601 form incl. a pasted `frames.json` `exposure_start_utc`. Tests:
`tests/test_lx200_site.py`.

---

## Adding a maintenance socket command

The maintenance socket is the internal RPC bus used by the web UI and
`diofinder-ctl`. All commands are handled in
`diofinder/comms_proc.py::_handle_maint_command`.

Notable commands beyond the basics: `solver_params_get`/`solver_params_set`
(sigma 0–20, kernel_sigma 1.0–4.0, max_axis_ratio 0=off/1.5–10.0, local_noise,
bg mode/sizes, noise mode, `extractor_backend` sycamore/tetra3 (independent of
the Legacy preset — flip just the extractor for a clean A/B), min_centroids,
max_solve_stars 4–200,
fov_max_error_deg 0.05–5.0 (the loose blind-solve search tolerance; the
calibrated tight tolerance is owned by the calibration machinery),
solve_timeout_ms),
`match_params_get`/`match_params_set` (match_radius 0.005–0.05, match_threshold
1e-9–1e-3), `seeing_get`/`seeing_set` (apply the Good/Bad presets in
`diofinder/seeing.py`; `seeing_set {"mode":"good"|"bad"}` routes every preset key
through the right channel — shared_cfg for live solver keys, a solver `set_db`
command for the database switch — persists via `config.save_keys`, and
invalidates the solver cache; `seeing_get` returns the mode, the preset table,
the effective per-key values, a `drift` map of keys the user has overridden
since, plus `lineage` (factory/tuned/custom) and a per-mode `overrides`
summary), `seeing_override_save`/`seeing_override_clear` (manage the saved
override layer — see Seeing presets), `auto_exposure_set` (toggle the comms-side
auto-exposure controller),
`auto_tune`/`auto_tune_status`/`auto_tune_cancel` (offline coordinate-search
sweep — see the Auto-exposure / gain controller section),
`tuning_set` (switch the libcamera tuning profile — `finder` (default) /
`scientific` / `standard`; restart required — see **Libcamera tuning** below),
`bg_cache_status` (live temporal-cache
snapshot — state, model age, model kind row/block/image, served-cached vs
fallback counters, plus `resolved` (v0.11.47): the effective-background
resolver — requested mode → effective mode → the path actually serving
detection this instant (cached-row/block/image or per-frame) with the reason
and a one-sentence `summary`, computed by `bg_cache.resolve_effective` from
the same facts `detect()` decides with; the Background and Advanced pages
render it verbatim), `bg_preview` (v0.11.49: reconstruct at full frame
resolution the background a mode subtracts, for the Background page's `/bg.jpg`
visual A/B — returns the paired frame + background bytes for one seq plus a
`preview_source` label. Backed by `bg_cache.preview_background` (the single home
for preview math; the webui no longer reimplements `_compute_background`).
Spatial modes reconstruct per-frame; **`temporal_median` renders the solver's
live cached median stack** (`_model.bg_image`) — the only way to see it, since
it exists only in the solver process — degrading to per-frame `block_percentile`
when no stack is built yet), `solve_centroids` (plate-solve a caller-supplied centroid list on
the live solver's resident database — no second DB, used by
`diag_background --solve` and the web-UI background A/B), and the hot-pixel
trio: `dark_capture {"frames":N, "exposure_s"?, "gain"?}` (cap the lens;
median-stacks N frames into a mask saved at
`/var/lib/diofinder/hot_pixel_mask.npz`; optional `exposure_s`/`gain` capture at
a fixed worst-case point with snapshot-and-restore), `hot_pixel_status`, and
`hot_pixel_clear`. `frame_get {"after_seq"?}` returns the newest camera frame
(base64 u8 + shape + seq) via the solver's FrameSlots-bracketed read — never
torn by a concurrent camera write; chain `after_seq` for strictly consecutive
burst frames (debug bundles and A/B captures use it, and it is the live view's
*fallback*). `display_start` arms the demand-gated `diofinder_display` segment
(the web live view's fast path — see below). `solve_stats {"after"?}` returns
the solver's rolling per-successful-solve records (FULL vs TRACKING) for the
tracking A/B harness. `solution_for_seq {"seqs":[...]}` returns the solution
published for specific frames from the solver's last ~48 publishes
(`_SolverState.recent_solutions`, fed by `SolutionTap`). Debug bundles use it so
each burst frame gets its own result even at 0.3 s exposures, where
`latest_solution` has always moved on by the time the web UI asks (every frame
used to come back `null`).

1. Add an `if cmd == "my_command":` branch anywhere in the function.
2. Read arguments from the `args` dict (always a plain dict, may be empty).
3. Return `MaintResponse(ok=True, result={...})` or
   `MaintResponse(ok=False, error="...")`.
4. If the command mutates solver or camera state, route it through
   `_call_solver` / `_call_camera` rather than touching shared state directly.
5. If the command changes solver-visible state, call `_invalidate_solver_cache`
   so the next read fetches fresh data.
6. Add a corresponding route in `webui/app.py` that calls
   `_safe_call("my_command", args)`.

To add solver-side handling, mirror the pattern in
`diofinder/solver_proc.py::_handle_solver_cmd` using the `SolverCmd` /
`SolverCmdReply` dataclasses in `diofinder/worker_cmds.py`.

---

## Adding a web UI endpoint

All routes are in `webui/app.py`.

```python
@app.route("/my/endpoint", methods=["POST"])
def my_endpoint():
    """One-line description of what this does."""
    r = _safe_call("my_command", {"key": value})
    if not r.ok:
        return r.error, 500
    return redirect(url_for("some_page"))
```

- Use `_safe_call` for all daemon communication — it never raises.
- API endpoints that return JSON follow the pattern
  `return jsonify({"ok": r.ok, "result": r.result, "error": r.error})`.
- Form-submit routes redirect back to a page; AJAX routes return JSON.
- The webui process runs on CPU 0 alongside `comms_proc` and the IMU thread.

Templates live in `webui/templates/`. Static assets in `webui/static/`.
The Jinja2 environment has a `log10` filter registered for log-scale sliders.

**Hidden tabs don't poll (v0.11.76).** `base.html` wraps `window.setInterval`
before any page script runs: callbacks are skipped while `document.hidden`,
and every registered poller runs once when the tab is shown again. So the live
view (`/frame.jpg` every 1.25 s), status pollers and any future
`setInterval` stop costing CPU 0 for background tabs; browsers only slow those
down. One-off `setTimeout`s (debounces, the reboot-wait ping) are unaffected.
**Off-screen cards don't poll either (v0.11.76):** `window.dioOnScreen(el)`
(base.html, IntersectionObserver; true when unknown) gates the Home and
Advanced live-view JPEG, the Advanced camera-state sync (5 s, was 3 s), the
background-mode summary and the mount status (every 30 s while the link is
off). Use it in any new poller for a card that can be scrolled away.

**Info tooltips (v0.11.51).** Long explanatory prose on the settings pages is
collapsed behind an ⓘ icon via one reusable macro: `{% from "_macros.html"
import tip %}` then `{{ tip("Help <b>text</b>.") }}` next to a label/heading.
Shared CSS (`static/style.css`, night-vision red) + JS (`base.html`: tap
toggles, second tap / tap-away / Esc closes; blur-on-close + `@media
(hover:hover)` keep touch honest). The text is rendered `| safe`, so pass only
template-authored strings and escape any literal `"` as `&quot;`. **Keep inline
(do not tooltip):** short labels, safety/action-critical warnings (cap the
lens, park the mount, factory/calibration reset), the Config table's per-row
descriptions, `.muted` "daemon not reachable" fallbacks, and live status
lines.

### Live-view frame path (`diofinder_display`)

The hot `/frame.jpg` poll uses a **dedicated display SHM segment**, not the
`frame_get` maint round-trip. `frame_get` copied the frame in the solver,
pickled it through the reply queue, base64-encoded it in comms, and parsed
~1 MB of JSON in the browser — ~6 copies + base64 + JSON per poll, with the
copy and pickle stolen from the *solver* process and the command serviced only
between frames. So every open live-view tab measurably slowed the solve loop.

Now: `diofinder/display_shm.py` defines a fourth SHM segment (allocated by the
launcher). The solver writes the frame it already holds into it under a
best-effort **seqlock** (even/odd generation counter) — but only while
`display_wanted_until` is fresh, so an unattended finder pays nothing. The web
UI's `_live_frame` sends a throttled `display_start` keepalive (every ~2 s),
then `DisplayReader.read()` copies the segment **directly** with a double-seq
tear check, no maint round-trip and no base64. On a torn read, an absent
segment (older daemon), or before the solver has armed, it falls back to
`_daemon_frame` (`frame_get`). Debug bundles and A/B bursts deliberately stay
on `frame_get` — the display segment is a best-effort preview, not a
strictly-consecutive source. Round-trip unit-tested in
`tests/test_display_shm.py`.

**Daemon-restart survival (v0.11.42)**: every `diofinder.service` (re)start
unlinks and recreates the segment, which orphans a long-lived reader — it
would keep serving the last frame ever written to the old mapping, with a
valid never-advancing seq, forever (the live-view-frozen-until-webui-restart
bug). `_live_frame` therefore (1) stats the segment's `/dev/shm` inode each
poll and re-attaches when it changes or disappears, (2) serves `frame_get`
instead once a seq has been frozen > 15 s (writer-stopped backstop), and
(3) retries a failed attach every poll instead of stickily recording `hw`
with an unavailable reader (webui-boots-before-daemon race). `DisplayWriter`
resumes its generation counter from the segment's current seq on attach.
Regression tests: `tests/test_live_frame.py`.

---

## Extraction

See `docs/pipeline.md` for rendered flowcharts of the full detection→solve
pipeline (the `extractor_backend` tetra3-vs-sycamore branch) and how the
Seeing / Star-detection / Sky-background controls map onto the settings.

Star extraction uses **sycamore** `star_detect` (matched-filter gate, hardcoded
since v0.9.0 — the `gate_mode` parameter was removed; passing it raises
`TypeError`).
Wheels are not committed to git: `release.yml` downloads the sycamore and
olive-solve wheels from their repos' GitHub releases at image-build time (pin
with the `SYCAMORE_TAG` / `OLIVE_SOLVE_TAG` repo variables; unset = latest),
and on-device `diofinder-update` refreshes them from the latest releases. A
wheel placed manually in `vendor/wheels/` overrides the download.

**Wheel versions are first-class diagnostics** (v0.11.18): `diofinder/wheels.py`
reports the installed olive-solve (`tetra3`) and sycamore (`star_detect`)
versions; they appear in the solver startup log, the `version` maint command
(`wheels` key), the Home/Update pages, and every debug bundle's
`effective_params.json`. `diofinder-update` prints a per-wheel refresh summary
(old → new, or a loud "NOT refreshed" warning) both after the wheel step and as
the final lines of the run — a silently stale wheel has masqueraded as an
application regression before (v0.11.15 code + pre-v0.1.3 olive-solve = no
blind-hint fallback = post-slew re-acquisition deadlock). The solve hint applies the
IMU delta in the **camera frame** since v0.11.22 when a quality-gated fit is
available: `diofinder/imu_frame.py` Kabsch-fits the IMU-body→camera rotation
from the 3-D rotation-vector pairs harvested between consecutive solves
(gates: ≥4 magnitude-consistent pairs, axis diversity ≥0.25, R²≥0.9 — an
alt-only slew history is refused as unobservable), publishes it as
`shared_cfg["imu_frame_R"]`, and the hint cone tightens to 1.2× the measured
rotation. Without a fit the defensive body-frame path remains: cone
`max(2°, 2.5×` the rotation`)` (v0.11.18 — the raw body-frame delta was
measured landing 1.76× the slew angle from truth; the old 1.5× cone could
exclude truth entirely). olive-solve ≥0.1.3's blind fallback backstops both.
Since v0.11.23 the same fit also drives the **LX200 pointing prediction** in
comms (`_imu_predict`): when `imu_frame_R` and the `imu_ref` 6-tuple's sky
quaternion are both present, the prediction is exact quaternion composition —
delta conjugated through the fit, composed onto the reference attitude,
converted via `imu_math.quat_to_radec` (boresight = row 0 of R(q)) — with no
small-angle approximation, no 5° clamp, no pole guard, and no dependence on
the C-matrix calibration. The legacy C-matrix small-angle path remains the
fallback (5-tuple refs / no fit). **Kill switch** (v0.11.28):
`imu_exact_predict=false` (`solver_params_set`, or the conf, seeded into
shared_cfg at comms start) forces the C-matrix path even when a fit is
present — a field-reversible fallback to the pre-v0.11.23 behavior if the
crosshair looks wrong during a slew.

**Anti-oscillation (SkySafari reticle jump during motion).** Three layers stop
the `:GR/:GD` report flip-flopping between the plate solve and a jittery IMU
prediction (root cause: the BNO055 in IMUPLUS "hunts" between two orientations
~0.8° apart while stationary, which the rate gate misreads as slewing).
**P1 — solve-freshness gate** (`imu_predict_hold_solve_s`, default 1.0 s): the
between-solve prediction only fills gaps, so whenever a confident solve landed
within the window `_imu_predict` returns None and the *solve* is reported, not
the extrapolation; the IMU takes over only once solves lapse. 0 disables.
**P2 — motion-gate hysteresis** (`_IMU_GATE_HYST_FRAC` = 0.5): once engaged the
motion latch stays alive down to half the engage rate, so a slew whose measured
rate wobbles around the gate between solves doesn't disengage-and-re-engage
(the frame-to-frame predict↔hold thrash). It only *sustains* an engaged latch —
it never re-engages from rest, so the stationary-drift gate is unchanged.
**P3 — fusion-hunt suppression** (`imu_hunt_filter`, default on,
`imu_proc._hunt_filter`): damps the two-state quaternion hunt at the source
(adaptive nlerp) *before* it reaches any consumer; a change past `_HUNT_SNAP_DEG`
(2°) snaps through unfiltered so genuine slews and the solve-hint Kabsch fit are
unbiased. All three are unit-tested (`tests/test_imu_smoothing.py`,
`tests/test_imu_hunt_filter.py`) with no hardware.
**Report smoother (v0.11.76, opt-in `imu_report_smoothing`, default OFF
pending on-sky A/B):** the fix for jumpiness *during movement* (a new solve
hard-replaced the reported position). `diofinder/report_filter.py`
`ReportSmoother`: the report = target (IMU prediction without the P1 hold, or
the solve) + an offset. IMU source: each new solve pulls the report
`imu_report_gain` (0.3) of the gap (the report carried forward by the measured
motion, so the slew between polls is not counted as error) and corrects a
learned drift rate (alpha-beta/PI; cancels a consistent IMU scale error,
clamped to 0.3× the slew rate, dropped when the scope stops); a gap > 1° snaps.
Solve source (parked / no IMU): steps ≤ 0.05° are averaged, bigger steps are
followed at once (no lag on a pan without IMU), and the offset fades (0.7 s) so
it always settles on the solves. Source switches are continuous. Plus
mid-exposure timing: while on, the IMU thread publishes its last 2.5 s of
samples inside the `imu` tuple and the solver anchors `imu_ref` at the frame's
exposure midpoint (`frame_meta` sensor timestamp; `_mid_exposure_imu_q`). The
Kabsch fit / solve hint still use the snapshot attitude. Unit tests:
`tests/test_report_filter.py` (simulated slew with a 10% IMU scale error,
parked jitter, no-IMU pan, snap, pole, routing, mid-exposure). History and the
rejected v0.11.71 deadband: `docs/decisions/2026-11-01-imu-report-smoothing-shelved.md`.

Detection is routed through `diofinder/bg_cache.py::BackgroundCache`, not by calling
`detect_stars` directly. This gives three composable background strategies, all
toggleable from config (and live-overridable via `shared_cfg`):

- **Per-frame background mode** (`detect_bg_mode`): eight modes available
  (all per-mode facts — cache kind, size param, UI labels — live in the
  `diofinder/bg_modes.py` registry since v0.11.48; bg_cache, comms
  validation, and both web menus derive from it). **Menu trim (v0.11.76):** the
  registry's `menu` flag keeps only `block_percentile` (default),
  `row_percentile`, `uniform_mean` and `temporal_median` in the Advanced and
  Background "set the mode" menus; the other four stay fully supported (conf,
  maint, presets, the Background preview) and still appear in a menu while
  they are the active mode:
  - `row_percentile` — default, cheapest; per-row percentile floor
  - `line_median` — robust to per-row offset / vignetting
  - `column_percentile` — per-column percentile floor (sycamore >= 0.10.0)
  - `row_column_percentile` — separable 2-D: row then column (sycamore >= 0.10.0)
  - `block_percentile` — bilinear interpolation of per-tile medians, tile size `detect_bg_block_size` (default 0 → sycamore uses 32); removes 2-D spatial gradients cheaply (sycamore >= 0.10.0)
  - `uniform_mean` — 25×25 sliding-window mean via summed-area table; exact tetra3/olive-solve default pipeline; filter size `detect_uniform_filter_size` (default 0 → sycamore uses 25) (sycamore >= 0.11.0)
  - `top_hat` — morphological white top-hat; removes large-scale vignetting / sky-glow that per-row floors can't see; slow (~100 ms); structuring-element radius `detect_tophat_radius` (default 12); needs **sycamore >= 0.9.0** (degrades to `line_median` on older wheels)
  - `temporal_median` — subtract the binned **temporal median stack itself** per-pixel (sycamore >= 0.13, probed via `HAS_BG_IMAGE`): gradients, vignetting AND per-pixel fixed-pattern structure removed in one pass — everything the stack knows, not a row/tile summary. Cache-only by nature; degrades to per-frame `block_percentile` during warm-up/slew or on older wheels

  The **noise estimator** is also selectable via `detect_noise_mode`:
  - `mad` — median absolute deviation (default, robust)
  - `global_rms` — `sqrt(mean(pixel²))` global root square; matches the tetra3/olive-solve default; use with `uniform_mean` to replicate that pipeline exactly

  Modes `column_percentile`, `row_column_percentile`, and `uniform_mean`
  require full-image spatial preprocessing and are **not compatible with the
  temporal cache** — they force per-frame detection. `row_percentile`,
  `line_median`, and `top_hat` compose with the per-row cached model.

  As of **sycamore ≥ 0.12**, `block_percentile` is **also cache-compatible**:
  `bg_cache._build_model` builds a block-median grid (via
  `compute_block_medians_py`, tile size from `detect_bg_block_size`, 0→32) of
  the temporal median stack instead of per-row offsets, and steady-state
  detection calls `detect_stars_with_cache(..., block_offsets=...)`. On older
  wheels (no `compute_block_medians_py` / no `block_offsets` kwarg)
  `block_percentile` falls back to the per-frame path exactly as before. The
  cache tracks which model kind (row vs. block) the active mode wants and
  rebuilds when the mode switches (e.g. a Good→Bad seeing toggle).

- **Temporal "analytic-threading" cache** (`bg_cache_enabled`, default true): a
  worker thread in `solver_proc` median-stacks recent frames into a per-row
  background + noise model; steady-state detection consumes it via
  `detect_stars_with_cache` (√N noise reduction + free hot-pixel rejection),
  falling back to per-frame detection during slew (IMU-driven) and warm-up. Set
  `bg_cache_enabled: false` to disable if its per-frame submit/stack bookkeeping
  proves too costly. The model's noise estimate is computed **in float**
  end-to-end (median → bin → MAD); the earlier uint8 casts quantized it to
  {0.5, 1.48, 2.97, …} — a 3× threshold jump between adjacent states, observed
  live as `noise` flipping 0.50↔1.48 on faint sky. The stack is also **flushed
  on any exposure/gain change** (`camera_settings_epoch` → `note_camera_settings`;
  see the camera section) so mixed-pedestal frames never build a model. The temporal model is orthogonal to the per-frame mode and
  composes with `row_percentile`, `line_median`, and `top_hat`.

  **`bg_cache_bin_at_submit`** (v0.11.26, opt-in, default false, live-mutable
  via `solver_params_set`): bins each frame to the detection resolution at
  submit (stored as uint16 block *sums*) and median-stacks there, instead of
  stacking full-res and binning the median — ~2× less stack memory and ~4×
  fewer median elements (the ~100–300 ms GIL-held rebuild). It is a
  **different noise estimator** (spatial-mean and temporal-median don't
  commute), offline-quantified within ~1–2% of the default on real sky with
  matching star counts at the operating sigma (`tests/test_bg_cache_bin_at_submit.py`).
  A/B on a clear night (`bg_cache_status.bin_at_submit` + `solve_stats`)
  before flipping the default. No effect at `detect_bin=1`.

A/B these on-device with `tests/diag_background.py` (e.g. `--inject-gradient 40`
to stress the glow case); the upstream extractor harness is
`sycamore-extract/tests/ab_background.py`.

---

## Config file

`/etc/diofinder/diofinder.conf` — key: value pairs, `#` for comments.

`diofinder/config.py::load_config` reads the file, applies `DIOFINDER_<KEY>`
environment overrides, and returns a `Config` dataclass. Unknown keys are
logged and ignored. Missing file uses all defaults.

`config.save_keys(updates)` rewrites changed keys in place, preserving
comments and unrecognised lines. It is the only function that writes to the
config file at runtime. Since v0.11.20 it is **cross-process safe**: an
exclusive flock on a `.lock` sidecar serializes solver-side calibration
commits against comms-side persists (the unlocked RMW could drop the other
process's keys), and the write goes through temp-file + `os.replace` so a
power cut can never truncate the conf. Float values are formatted `%.10g`
(the old `%.6f` rendered any float < 5e-7 as `0.000000` — a persisted
`match_threshold: 1e-7` round-tripped to 0.0, which admits no match at all).

**Directory permission requirement (v0.11.30 fix)**: the `.lock` sidecar and
the `.tmp` file used for the atomic replace are both *new* files created next
to `diofinder.conf`, so the unprivileged `diofinder` user needs write
permission on the **`/etc/diofinder` directory itself**, not just on the conf
file — a distinction the installer got wrong (it `chown`ed only the file) and
that broke every settings-persist call with `PermissionError` on
`diofinder.conf.lock`. If you ever see that error, `sudo chown
diofinder:diofinder /etc/diofinder` fixes it immediately;
`scripts/install.sh` and `scripts/firstboot.sh` both self-heal it now.

**Factory reset (v0.11.31)**: `conf_migrate.factory_reset()` overwrites the
live conf with the shipped `diofinder.conf.default` **verbatim** — unlike
`save_keys`, which merges specific keys into the existing file, this is a
full-file replace (same `.lock`/temp-file/`os.replace` discipline). The webui
Config page's "Factory reset" card (visible in both novice and expert mode —
it is a page-level action, not a per-key edit) posts to `/factory_reset`,
which — like `/update` — runs `sudo /usr/local/bin/diofinder-factory-reset`
detached via `systemd-run` (outside the maint socket, since the script
restarts the very daemon that would otherwise service the request) with
optional `--clear-overrides`/`--clear-hot-pixel-mask` flags from the
checkboxes, then restarts both `diofinder.service` and
`diofinder-webui.service`. The script re-chowns the conf + its `.lock` to
`diofinder:diofinder` after writing (it runs as root) — the same
directory-ownership class of bug as the v0.11.30 fix, just scoped to these
two files. Does not touch the star database, the code checkout, or Wi-Fi
(NetworkManager-managed, not in this file); latitude/longitude ARE in the
conf and do reset to 0/0.

**Shipped-default report** (`conf_migrate.diff_from_default`, Update page +
end of `diofinder-update`): keys whose value differs from
`diofinder.conf.default`. A key the device's conf lacks (added in a later
release) runs the `Config` dataclass default, so it is listed only when that
built-in value differs from the shipped one, as `<not set: using X>`.
`tests/test_conf_migrate.py` pins the built-in and shipped defaults equal
except `conf_version` and `fov_calibrated` (built-in false: a conf without it
re-learns the FOV). **Site** (Utilities → Site card, `/api/site`,
`/site/set` → maint `site_get`/`site_set`, `horizon.parse_site`: numbers or
N/S/E/W letters, longitude East-positive, 0..360 folded): latitude,
longitude and **elevation** (LX200 has no elevation command, so SkySafari
can't send it); persisted, live in comms, latitude pushed to the solver's
polar module like `:St` (other solver consumers read the site at restart).
Tests `tests/test_site.py`.

**One-shot conf migrations** (`conf_migrate.MIGRATIONS`, run by the launcher
at start): a rule rewrites a key only while it still holds the old default,
and carries the `conf_version` that introduced it, so a device already at v1
gets only the v2 rules (a v1 key the user set back on purpose stays). v2
(v0.11.76): `imu_poll_hz` 20 → 10, so OTA-updated cards get the new rate.

New keys (this release):

| Key | Default | Notes |
|-----|---------|-------|
| `seeing_mode` | `good` | Active Good/Bad preset (see below). |
| `detect_kernel_sigma` | `1.5` | Matched-filter PSF width (sycamore≥0.12; capability-probed). |
| `detect_max_axis_ratio` | `0.0` | Trail rejection; 0→`float("inf")` (off), else 1.5–10.0. |
| `detect_local_noise` | `true` | Per-window noise in the matched filter (sycamore≥0.12). |
| `star_db_deep` | `""` | Optional deeper-magnitude db for the Bad preset; applied only if the file exists. |
| `auto_exposure_enabled` | `false` | **Default OFF since v0.11.43** (was ON v0.11.x–v0.11.42): exposure/gain stay where the user set them unless the controller is explicitly enabled (Camera page toggle / `auto_exposure_set`). Existing devices keep whatever their conf has persisted. |
| `watchdog_enabled` | `true` | Solver-hang watchdog (comms thread). |
| `watchdog_timeout_s` | `30.0` | Staleness before the solver is declared hung. |
| `extractor_backend` | `sycamore` | Centroid extractor: `sycamore` (matched filter + bg_cache) or `tetra3` (AstroKeith's olive-solve `get_centroids_from_image`). Live-mutable; set by the Legacy preset. |
| `imu_persist_bno055` | `false` | **Unit B, default OFF pending field validation.** Persist the BNO055's own accel/gyro calibration profile across power cycles (the chip has no flash). `imu_proc` restores a saved profile at init (CONFIG mode) and saves one once the chip is gyro+accel-calibrated, plate solves confirm the IMU tracks truth, and the scope is still. Off = shipped behavior byte-for-byte. |
| `imu_solve_cal_enabled` | `false` | **Unit C, default OFF pending on-sky validation.** Derive the static accel-tilt bias + gyro scale-factor from solve-vs-IMU disagreement (`imu_solve_cal.py`) and correct the IMU (Mode 1, software-only, reversible). Solver runs the estimator on RAW IMU data (needs a Unit A extrinsic + valid latitude/longitude); comms `_imu_predict` applies the correction. Persists `imu_solve_cal.json`; self-heals on divergence. |
| `imu_solve_cal_write_chip` | `false` | **Unit C Mode 2 (reserved, not implemented).** Would push the derived accel offset into the BNO055 registers via the Unit B channel. |
| `horizon_reject_enabled` | `false` | **Opt-in, default OFF.** Enables olive-solve's per-star horizon prune on the blind/hinted solve path (`solver_proc` passes `observer_latitude`/`observer_lst` via `diofinder/horizon.py`; LST = GMST(UTC)+longitude). Drops below-horizon candidate patterns before verification and rejects sub-horizon false positives. Value is on the **blind/lost-in-space** path only (a good hint already constrains tracking); needs olive-solve ≥ the horizon release (older wheels ignore the kwargs). **Self-gates**: no-op unless the site is set (lat/lon ≠ 0,0) and the clock is plausible (year ≥ 2025) — a wrong UTC (Pi has no RTC) rotates the horizon and would reject valid solves. Per-star prune only (no `min_boresight_altitude`). `verify_attitude` fast path is unaffected. |

---

## Seeing presets

See `docs/skyglow-strategy.md` for a decision flowchart + lever reference on
solving under light pollution / a sky gradient (which preset to start from and
which knob to pull next).

`diofinder/seeing.py` holds three flat preset tables — `SEEING_PRESETS["good"]`,
`["bad"]`, and `["legacy"]`. Since v0.11.15 the **Good** preset (and the config
default) uses `detect_bg_mode="block_percentile"` — cache-compatible and
gradient-aware at similar cost to the old `row_percentile`. Each key in a preset is *also* an individually
adjustable config key, so applying a preset is exactly equivalent to setting
each by hand. All three presets carry the **same key set** so a toggle fully
re-tunes the pipeline (including resetting `extractor_backend`).

**"Legacy" preset / `tetra3` extractor backend.** Legacy is an exact re-creation
of the AstroKeith `eFinder_cli` `original`-branch pipeline: it sets
`extractor_backend="tetra3"`, which routes detection through the olive-solve
binding's `get_centroids_from_image_fast` (local_mean background + global-RMS
noise + sigma threshold, *no* matched filter, *no* temporal cache, *no*
hot-pixel — `downsample=1`, `min_area=5`, `max_area=100`) instead of sycamore +
`bg_cache`. The tetra3 extractor returns `[row, col]` centroids directly (no x/y
swap, unlike sycamore). It is **capability-probed** in `solver_proc` via
`hasattr(solver_t3, "get_centroids_from_image_fast")` — if the installed
olive-solve wheel was built without the `extractor` feature (it is on by
default), detection falls back to sycamore and warns once. Legacy is the
**baseline to improve upon**, not the recommended default.

**Diagnostics / runtime reproduction.** Live solves and the diagnostic scripts
can diverge (the classic "fails live, solves in diag"): `diag_solve.py`
historically extracted at `bin=1` with sycamore defaults and the *loose* config
FOV tolerance, while the live solver uses `cfg.detect_bin` (2), the temporal
cache, the *calibrated* (tight) FOV tolerance, the config solve timeout, and an
IMU attitude hint. Two aids close the gap: (1) `debug_collect` writes
`effective_params.json` (live `solver_params_get` / `match_params_get` /
`bg_cache_status` / `seeing_get` + the calibrated FOV estimate & tolerance
actually in force) into the bundle; (2) `diag_solve.py --match-runtime` reads
those knobs (detect_bin, kernel_sigma, noise_mode, bg_mode, max_axis_ratio,
`extractor_backend`) and reproduces the live pipeline (`--bin` / `--backend`
override individually). `diag_solve.py --bundle <zip>` goes further: it replays a
downloaded debug bundle **fully offline on any machine** — it reads the live
knobs from the bundle's `effective_params.json` (the *calibrated* FOV tolerance
+ shared_cfg drift that `diofinder.conf` alone misses), points `DIOFINDER_CONFIG` at
the bundle's `diofinder.conf`, and solves the bundle's `frame_*_raw.png`. The
matching star database must be present locally (the bundle omits the `.npz`).
`debug_collect` captures a **burst** of raw frames (default 12, `?frames=N`,
clamped 1–30, bounded by a ~40 s wall-clock budget) — enough to measure a solve
*rate* and to reconstruct the temporal background cache (needs ≥ `bg_cache_stack`
consecutive frames), not just a single snapshot. Raw PNGs are saved for every
frame; the large arcsinh **display JPGs are capped at the first 2** to keep the
bundle email-friendly.

**Built to a file, downloaded by link (v0.11.76).** The bundle button posts
`/debug/start`; a background thread builds the zip straight to
`/var/lib/diofinder/diofinder_debug_<YYYYmmddHHMMSS>.zip` (`.tmp` then
`os.replace`; newest 10 kept), the page polls `/api/debug`, then fetches
`/debug/download/<name>` as an ordinary download — the zip never sits in web UI
memory or one long response. `/debug/collect` stays for scripts (builds, then
serves from disk; GET or POST, so `curl …/debug/collect?frames=12 -o b.zip`
works). Each `frames.json` solution also carries **`matched`**
(`[x, y, ra, dec, mag]` rows, like live captures): the bundle asks
`matches_want {"seconds": 90}` first, which sets `shared_cfg["matches_wanted_until"]`
so the solver passes `return_matches` and keeps them in
`_SolverState.recent_matches` for `solution_for_seq`.

**Exact per-frame timing & attitude (v0.11.46).** `diofinder/frame_meta.py`:
the camera captures each frame via a picamera2 *request* and publishes the
libcamera metadata — `SensorTimestamp` (CLOCK_BOOTTIME ns at first-row readout
start), actual `ExposureTime`, actual `AnalogueGain` — into a small
`shared_cfg["frame_meta"]` ring keyed by FrameSlots seq, together with the
measured boottime→wall offset. Consumers derive
`exposure_start = SensorTimestamp + wall_offset − ExposureTime` to
millisecond accuracy. `frame_get` replies attach the matching entry (`meta`),
and every published `latest_solution` carries the `seq` of the frame it came
from. `debug_collect` uses both to write **`frames.json`** into the bundle:
filename → {seq, `exposure_start_utc`, `readout_start_utc`, actual
`exposure_s`/`gain`, the frame's **own** solve result (matched by seq — not
the previous frame's, which is what the older `ref_*` fields in `imu.json`
record), and the IMU snapshot}. The solve result carries both the **aim-point**
`ra_deg`/`dec_deg` (the boresight-pixel pointing that `:GR/:GD` report) **and**
the **image-center** `center_ra_deg`/`center_dec_deg` (the raw plate-solve
pointing at the geometric frame center — equal to the aim point only until a
`:CM#` align offsets the boresight); both J2000/ICRS, plus `roll_deg` and
`is_mirrored` — enough to reproject a saved frame onto a J2000 sky chart
(`roll_deg` is north's angle CCW from image "up"/y=0; when `is_mirrored` is
true the frame must be flipped before the roll rotation applies).
**`tests/reproject_frame.py BUNDLE.zip [frame]`** does this turnkey off-device
(flip-if-mirrored, then rotate `-roll`), writing a North-up/East-left PNG with
the image-center RA/Dec + a compass overlay for chart comparison. **`tests/bundle_solve.py BUNDLE.zip [--out
FILE.json] [--db PATH]`** re-solves every bundled frame offline (same
effective-params hydration as `diag_solve.py --bundle`, plus the loose-window
retry) and emits a single JSON packet mapping each `frame_XX_raw.png` to
solved RA/Dec/roll/FOV/matches merged with the bundle's exact capture
metadata.

* `seeing_set {"mode": "good"|"bad"}` (comms maint): switches the solver
  database **first** (the only fallible step — a set_db failure now aborts the
  toggle before anything is written, keeping it atomic; v0.11.20), then writes
  every preset key to `shared_cfg` (live solver/auto-exposure keys), persists
  all of it with `config.save_keys`, and calls `_invalidate_solver_cache`.
* `seeing_get` returns the mode, both preset tables, the **effective** value of
  every preset-controlled key (shared_cfg over cfg), and a `drift` map of keys
  the user has individually overridden since applying a preset.
* `star_db="deep"` resolves to `star_db_deep` only when that names a file that
  exists; otherwise it stays on the standard `solver_db`.
* The toggle appears on the Status page and the Config page; `diofinder-ctl
  seeing {get|set good|set bad}` is the CLI equivalent.

Every preset key is independently tunable through `solver_params_set`
(detection/solve keys) and `match_params_set` (match radius/threshold); the
Camera page exposes sliders for all of them.

### Saved overrides (factory / tuned / custom)

The factory `SEEING_PRESETS` table is **immutable**. An *override* is an
optional, persisted, **sparse** layer between factory and live hand-edits:

```
factory preset  (SEEING_PRESETS, immutable)
   ⊕ saved override   (only the keys it changes; /var/lib/diofinder/seeing_overrides.json)
   ⊕ live hand-edits  (shared_cfg drift)
   = active config
```

* Overrides are **explicit to apply**: a plain `seeing_set {"mode":...}` always
  loads the factory preset. `seeing_set {"mode":..., "use_override": true}`
  overlays the saved override (via `seeing.merged_preset`). An override may
  carry absolute `exposure_s`/`gain` (factory presets can't) — `seeing_set`
  routes those to the camera, everything else to `shared_cfg`.
* `seeing_override_save {"mode"?, "values"?, "source"?}`: persists a sparse
  override (default = the preset keys currently drifting from factory + the
  camera exposure/gain, mirroring auto_tune's sparseness; `source` defaults to
  `manual`). `seeing_override_clear` deletes it. Storage helpers live in
  `diofinder/seeing.py`
  (`save_override`/`clear_override`/`get_override`/`load_overrides`, atomic
  JSON write), unit-tested in `tests/test_seeing_overrides.py`.
* `auto_tune commit=true` saves the winner as the tuned mode's override
  (`source="auto_tune"`) **and** applies it live, so the factory preset stays
  pristine and the result is a labelled, reversible artifact.
* **Lineage** (`seeing.classify_lineage`, surfaced by `seeing_get`): **tuned**
  if an override exists and every override key matches the effective config;
  **factory** if the effective config matches the factory preset; **custom**
  otherwise (hand-edits on top). The Config page shows a Factory/Tuned/Custom
  badge plus Apply / Save-from-current / Clear controls; `diofinder-ctl seeing
  {apply-override|save-override|clear-override}` is the CLI equivalent.

---

## Tracking mode (experimental, opt-in)

A steady-state ROI tracking mode lives in `diofinder/tracking.py` (pure numpy, no
`star_detect`/`tetra3`/`picamera2` import — unit-tested in `tests/test_tracking.py`)
and is wired into `solver_proc.solver_main` as a small **FULL ↔ TRACKING** state
machine. **Default OFF** (`tracking_enabled: false`) pending on-sky validation.

### What it does

After a run of confident full-frame solves it switches star **detection** from
full-frame extraction to small ROI windows placed around the previous frame's
solved star positions (`tracking_window_px`-square windows; overlapping windows
are deduped by proximity, edge windows are clamped inward). This saves the
dominant full-frame extraction cost (~6 ms → target ~1.5 ms). Detection has two
capability-probed paths:

* **sycamore ≥ 0.14** (`getattr(star_detect, "HAS_ROI", False)`):
  `tracking.roi_detect_native` builds the (N, 4) window list
  (`tracking.build_windows`) and hands the full frame + all windows to the
  native `star_detect.detect_stars_roi` in ONE call (one GIL round-trip; the
  windows fan out on sycamore's bounded pool; per-window line_median floor +
  whole-window MAD noise; at most one star per window, full-frame coords).
* **Older wheels**: `tracking.roi_detect` slices numpy windows in Python and
  calls the injected per-window detector (routed through `bg_cache.detect`)
  once per window, offsetting coordinates back to full-frame.

The recovered centroids are then solved via one of two capability-probed paths:

* **olive-solve ≥ 0.1.6** (`hasattr(t3, "verify_attitude")`): the true
  **verify-only** entry point — the catalog is projected through `last_sky_q`,
  matched, verified (binomial FPR) and refined, with the 4-star pattern hash
  **skipped entirely**. A wrong/stale attitude returns NoMatch fast, which
  drops the lock to FULL exactly like a failed solve (re-acquisition is always
  the full solver, never verify). Measured x86: 0.01 ms vs 0.5 ms full solve,
  identical RA/Dec.
* **Older wheels**: the existing `solve_from_centroids` under a tight attitude
  hint (`strict_hint=True`, 2° cone seeded from `last_sky_q`) — constrained
  but still pattern-hashing.

### State machine

* **FULL** — full-frame `bg_cache.detect` + the existing blind/IMU-propagated
  hint solve (`strict_hint=False`). The shipped behaviour, byte-for-byte.
* Enter **TRACKING** after `tracking_lock_frames` consecutive successful solves
  (and `tracking_enabled`), once the last solve produced ≥ `tracking_min_recover`
  centroids to predict from.
* **TRACKING** — ROI detection around the previous frame's solved centroids
  (`tracking.centroids_to_xy` inverts the solver's (row,col) back to (x,y)). v1
  uses the raw previous positions with **no** IMU/sidereal shift (sub-pixel drift
  between frames at this cadence is < 1 px, inside the window). If ≥
  `tracking_min_recover` stars recover, verify/solve as described above.
* **Fall back to FULL** on any of: `tracking_enabled` false, ROI recovered <
  `tracking_min_recover`, the solve failed (too few / no match / solver raised),
  or `bg_cache.state()` is `SLEWING` (the same slew signal `note_motion` /
  `note_solve_result` drive). Then re-acquire blind and re-lock.

Publishing, calibration/polar updates, align handling, `note_solve_result`, and
every `latest_solution` field are **identical** to the FULL path — tracking only
changes how centroids are obtained and the hint tightness, never the outputs.

The default-off guard: `tracking_on = shared_cfg.get("tracking_enabled",
cfg.tracking_enabled)`. When false, `tracking_state` is forced to `FULL`,
`tracking_active` is `False`, `served_by_tracking` stays `False`, and the
original `bg_cache.detect(frame_buf, …)` runs unchanged.

### Config keys

| Key | Default | Live-mutable | Notes |
|-----|---------|--------------|-------|
| `tracking_enabled` | `false` | yes (shared_cfg) | master switch |
| `tracking_window_px` | `48` | yes (shared_cfg) | ROI side length (px) |
| `tracking_lock_frames` | `3` | no (config/restart) | good solves before TRACKING |
| `tracking_min_recover` | `5` | yes (shared_cfg) | min ROI stars to stay tracking |

`tracking_enabled`, `tracking_window_px`, `tracking_min_recover` are added to
`solver_params_get` / `solver_params_set` (in shared_cfg keys table above) so
they A/B-toggle live without a restart.

### How to A/B it

**On-sky A/B harness (the graduation gate).** `tests/ab_tracking.py`
(`diofinder-ctl ab-tracking`, or the **Camera page → Experimental A/B card**,
v0.11.27) runs the comparison in flight: point the scope at a star field,
focus until it solves, keep it **stationary**, then

```bash
sudo python3 /opt/diofinder/tests/ab_tracking.py --window 30
# or: diofinder-ctl ab-tracking --window 30
```

The reusable driver is `ab_tracking.run_ab(window, settle, lock_timeout,
maint_call, progress)` → a structured result dict (it restores
`tracking_enabled` on exit); the CLI, and the web UI's `_ab_worker`
background thread (`/abtracking/start` + `/api/abtracking` poller), both wrap
it. The Experimental A/B card also exposes the two live default-off toggles
that were otherwise CLI-only — **ROI tracking mode** (`/tracking/set`, with a
live `tracking_status` readout) and **bg-cache bin-at-submit** (P5,
`/bincache/set`, with the `bg_cache_status.bin_at_submit` badge).

It drives a FULL window then a TRACKING window on the same field (changing
only the *live* `tracking_enabled`, restored on exit), and reports solve rate,
latency, matches, and — the gate that matters — whether TRACKING's pointing
agrees with FULL's (a `keep-off` verdict + reasons if it diverges or scatters).
The data comes from the solver's rolling per-solve ring buffer
(`SOLVER_OP_SOLVE_STATS` → comms `solve_stats`), tagged FULL vs TRACKING; each
phase is anchored on the solver's own monotonic `now` so the windows are
clock-correct. Exit 0 = safe to enable, 1 = correctness FAIL or a precondition
(not solving / never locked / scope moving) wasn't met. Pure decision logic
(`_summarize`/`_verdict`) is unit-tested in `tests/test_ab_tracking.py`.

Manual toggling + counters:

```bash
diofinder-ctl raw '{"cmd":"solver_params_set","args":{"tracking_enabled":true}}'
diofinder-ctl raw '{"cmd":"tracking_status"}'   # {enabled, state, frames_tracked, frames_full, recover_fail, solve_fail}
diofinder-ctl raw '{"cmd":"solver_params_set","args":{"tracking_enabled":false}}'
```

The `tracking_status` maint command (comms) → `SOLVER_OP_TRACKING_STATUS`
(solver) returns the live counters; the solver mirrors them into `_SolverState`
each frame.

---

## Auto-exposure / gain controller

`comms_proc._auto_exposure_loop` (daemon thread, gated by
`auto_exposure_enabled`) runs every 5 s and drives the camera toward the
*cheapest* operating point that still yields a confident solve. The pure
decision lives in `_auto_exposure_decision` (unit-tested in
`tests/test_auto_exposure.py`, no hardware needed).

* **Metric**: matched stars when the frame is solving (`solved=True`), steering
  toward `auto_exposure_target_matches`; falls back to raw detected-star count
  (`auto_exposure_target_stars`) while lost-in-space / slewing, where
  `matches == 0` carries no exposure information.
* **Gain-priority ladder, exposure anchored to nominal**: exposure is the
  *expensive* axis (it sets frame cadence, pointing-feedback latency, and star
  trailing on a moving mount) and gain is a latency-free trim, so **gain is the
  primary knob**. When starved it raises gain first and stretches exposure only
  once gain is at `auto_exposure_max_gain` (gain can't manufacture photons, so a
  genuinely dark scene still needs integration time); when over-served it drops
  gain first, shortening exposure only at the gain floor. When settled but
  exposure has drifted off the anchor `auto_exposure_nominal_s` (0 → the
  configured `exposure_s`) and gain has headroom, it walks exposure one step
  back toward nominal and lets the gain ladder restore brightness next cycle —
  so a starved episode never leaves permanent latency.
* **Saturation** (`peak ≥ 250`) overrides everything and backs off (gain first).
* **Low-contrast floor** (`auto_exposure_peak_floor`, default 70): the asymmetric
  partner of the saturation guard. When the frame `peak` is below the floor the
  controller **never sheds brightness** (no gain-down, no exposure-shorten) even
  when match-rich — a dim frame is one fluctuation from dropping below
  `min_centroids`, so it holds the operating point instead of walking off the
  detection cliff. Empirically solves span peak 36–247 but reducing past ~peak 35
  starves detection, so the floor parks the steady point with margin. `peak == 0`
  (dark/mid-slew frame) carries no contrast info and does not trip it.
  Live-mutable via `shared_cfg`.
* **Forced raise when unsolved and low-contrast** (v0.11.29): while lost in
  space, the star-count fallback is not just untrustworthy below the peak
  floor — it can be actively backwards. A near-black, quantization-limited
  frame lets a `sigma * noise` threshold trip on pure noise (the noise floor
  itself is often clamped, e.g. MAD-floored at 0.5 DN, so the threshold sits
  only a couple of DN above background), so the raw star count can look
  *plentiful* while matching nothing. Treated as an ordinary "over-served"
  reading, that count would suppress the raise the frame actually needs and
  the low-contrast guard would only hold — a real deadlock observed live
  (peak pinned 20-22, "stars" 183-353, 0 matches, stuck ~4.5 minutes / 146
  consecutive failed solves). `not solved and low_contrast` is now forced
  into the starved/raise branch regardless of what the star count reads —
  peak, not the corrupted count, drives the decision at that operating
  point. Unit-tested in `tests/test_auto_exposure.py`.
* **Raise debounce** (`_AE_RAISE_DEBOUNCE`, default 2; pure helper
  `_ae_apply_raise_debounce`, unit-tested): a brightness *raise* (gain-up or
  exposure-up) only takes effect after it has been the intended action for N
  consecutive cycles; any non-raise (reduce/hold) resets the streak. This stops
  the controller chasing a single transient dark frame (passing cloud / wind
  smear) right after a good solving run — the oscillation seen on real sky.
  Reductions and the saturation backoff are **not** debounced (act at once), and
  once confirmed it keeps raising every cycle, so a genuine cold-start ramp is
  delayed by at most one cycle.
* **Reversal damping** (pure helper `_ae_apply_reversal_damping`, v0.11.18,
  unit-tested): when the ladder reverses direction, the proposed step is
  replaced by its square root — a constant ×1.22 half-step per reversal — so
  the operating point can land inside the deadband instead of limit-cycling
  across it: the full ×1.5 gain step straddles the 0.8×–1.5× star-count
  deadband when the two rungs sit on opposite sides (observed live: gain
  2.1↔3.2 for minutes), while a ×1.22 rung always fits inside the 1.875-wide
  relative band. Same-direction moves keep full steps; the saturation backoff
  is never damped.
* Wide deadband (0.8×–1.5× of target) so it settles instead of oscillating;
  sub-5 ms exposure moves are ignored. At most one axis changes per cycle.
* Bounds: `auto_exposure_min_s`/`max_s`, `auto_exposure_min_gain`/`max_gain`.
  `target_matches`, `target_stars`, `max_s`, and `max_gain` are live-mutable via
  `shared_cfg` (the seeing presets write them); the floors are config-only.

A full multi-axis sweep over sigma/kernel/bg-mode *as well* as exposure/gain is
deliberately **not** done in this live loop — that is the offline `auto_tune`
sweep below.

### Offline `auto_tune` sweep

`auto_tune` (comms maint) is a **user-initiated, bounded coordinate search** for
the cheapest operating point — `(exposure, gain, sigma, kernel_sigma, bg_mode)`
— that still clears the seeing-mode match target on the *current* sky. It is
**not** a live controller: it runs as a background thread (the maint socket has
a 15 s read timeout, far shorter than a multi-point sweep), reports progress via
`auto_tune_status`, and is abortable via `auto_tune_cancel`. The pure
selection/merit logic (`_auto_tune_select` / `_auto_tune_cost`) is unit-tested
in `tests/test_auto_tune.py`.

* **Precondition**: a fresh solution with signal (`peak ≥ 20`, age < 10 s) —
  i.e. the finder is pointed at stars and roughly stationary. The always-on
  auto-exposure loop is paused for the duration so it doesn't fight the sweep.
* **Phase 1 (photometric)**: drives exposure+gain with the same ladder logic as
  the live controller (`_auto_tune_photometric` reuses `_auto_exposure_decision`).
* **Phase 2 (detection sweep)**: for each `(bg_mode, kernel_sigma, sigma)`
  candidate it asks the solver for `frames_per_point` single-frame samples via
  `SOLVER_OP_AUTO_TUNE_EVAL`. That op extracts with the candidate params
  **forced per-frame** (`bg_cache.detect(force_per_frame=True)` — the live
  temporal cache is left untouched) and solves on the resident DB, returning
  `{solved, matches, stars, peak, solve_ms}`. One frame per call keeps each
  solver-blocking call well inside the 30 s watchdog window. **Signal gating**:
  the sweep settles the camera once after Phase 1, and per sample retries up to
  `_AT_MAX_SAMPLE_RETRY` grabs to land a *signal-bearing* frame
  (`peak ≥ signal_floor`, default 20). A no-signal grab (dark / caught mid
  exposure-change / momentarily starved sky) is a capture artifact and is
  **dropped, not scored 0** — only frames that HAD signal vote, so a candidate
  isn't penalised for the live view briefly going dark while the sweep runs.
  Aggregation is the pure, unit-tested `_auto_tune_row` over the valid samples
  (a candidate with no valid samples is *indeterminate* and excluded, never 0).
* **Merit**: among candidates clearing the match-rate floor and ≥ 80 % of the
  match target, minimize `w·solve_ms + w·kernel_sigma − w·sigma + bg_cost` — i.e.
  prefer fast solves, a tight kernel, a high sigma, and a cheap background.
  `_AT_BG_COST` scores **every** sweepable `bg_mode` (so a user-supplied
  `bg_modes` list is ranked deliberately, not via the fallback).
  `detect_bin` (restart) and `star_db` (heavy reload) are deliberately **not**
  swept. The **default** `bg_modes` are just the two the seeing presets ship
  (`row_percentile`, `block_percentile`) to keep the sweep bounded; others
  (`uniform_mean`, `column_percentile`, …) can be opted in via the `bg_modes`
  arg.
* **noise_mode pairing** (since v0.11.15): candidates with
  `bg_mode="uniform_mean"` are swept **as the pair** `(uniform_mean,
  global_rms)` — the only combination that matches the tetra3/olive-solve
  reference pipeline — and a winning pair commits `detect_noise_mode` alongside
  `detect_bg_mode` (both commit paths). Other modes keep the robust `mad`
  default. `uniform_mean` still isn't in the *default* `bg_modes` sweep (opt in
  via the `bg_modes` arg). `temporal_median` is deliberately **not sweepable**:
  the eval op forces per-frame extraction, where it degrades to
  block_percentile and would just re-measure that.
* **commit=true** applies the winner live (`config.save_keys` + `shared_cfg` +
  `_invalidate_solver_cache`) **and** saves it as the tuned mode's override
  (`source="auto_tune"`), so the factory preset stays untouched and `seeing_get`
  lineage reads **tuned**. **commit=false** restores the camera and changes
  nothing — but the winner is kept in `auto_tune_status`, so you can apply it
  **after** seeing the result via `auto_tune_apply_last` (the "Apply result"
  button) instead of having to decide commit up front. `auto_tune_apply_last`
  mirrors the commit path (shared_cfg + `config.save_keys` + camera exposure/gain
  + `save_override` source=`auto_tune` + cache invalidate) from the stored
  result; it errors if a sweep is running or no result exists.
* CLI: `diofinder-ctl auto-tune {start [--mode] [--commit] [--wait]|status|cancel}`.
  Web UI: an "Auto-tune (current sky)" card on the Camera page (start/cancel +
  **Apply result** + progress poller via `/api/autotune`). The Camera page also
  live-syncs its sliders/selects (`/api/camera/state`) so auto-exposure,
  auto-tune, and preset changes show without a reload.

The Background A/B (`_bgrun_worker`) reads the **live effective** detection
params (`solver_params_get` → sigma/kernel_sigma/noise_mode/max_axis_ratio) and
the real exposure/gain, not the config file, and stamps them into the report —
so "A/B matches" reflects the live sycamore pipeline. It is **sycamore-only**:
when `extractor_backend=tetra3` (Legacy) the A/B does not represent the live
extractor and says so (banner + report `*** NOTE ***`); use `diag_solve.py
--bundle` to evaluate Legacy.

### Hindsight tuning from a saved burst

auto_tune is **live-only**. To find the best parameters *in hindsight* for an
already-captured burst (the `bg_ab_*.zip` archives the Background A/B run writes
to `/var/lib/diofinder/bg_runs/`; since v0.11.76 `_bgrun_prune` keeps the newest
10 within 300 MB and removes each run's loose frames folder once zipped), use
**`tests/replay_corpus.py`** (off-device or on-device), the offline sweep.
  `--corpus` accepts a **directory of PNGs OR a `.zip`** (burst archives /
  debug bundles — only the raw `*.png` frames are extracted). It sweeps
  presets × `--bg-modes` × `--sweep-sigma` × `--sweep-kernel`, honours
  `--fov`/`--fov-err`/`--max-stars`, echoes the effective parameters in the run
  header + footer, and selects a winner (best solve rate, then median solve_ms,
  then star count). `--apply [--apply-mode good|bad]` persists the winner via
  the maint socket (`solver_params_set`/`match_params_set` with `persist`, plus
  an optional `seeing_override_save source=replay`) — the same machinery
  `auto_tune commit` uses. On-device, `--apply` needs the daemon running.
The on-device "Tune from burst" card (a subset of this sweep run through the
live solver) was **removed in v0.11.76**: it duplicated `replay_corpus.py`
while costing the Pi's CPU. The Advanced page still lists the saved archives
for download.

## Hot-pixel mask

`diofinder/hot_pixel.py` builds a static hot-pixel mask from a capped-lens dark
capture and repairs masked pixels (8-neighbor mean, precomputed neighbor index
arrays, pure vectorized numpy, <1 ms) before each detection. This gives
hot-pixel rejection during slews, when the temporal cache is offline.

* `dark_capture {"frames": N, "exposure_s"?, "gain"?}` → solver grabs N SHM
  frames ~0.3 s apart, median-stacks, flags pixels exceeding
  `median + max(5·(1.4826·MAD), 3 DN)`, saves `/var/lib/diofinder/hot_pixel_mask.npz`
  (indices + shape + count), loads it. **Sanity guard** (`implausibly_large`,
  >0.5 % of the frame, abs floor 64): a capture that saw the sky (lens not
  capped) flags 100k+ pixels — the global threshold picks up every star and
  the bright half of any gradient — and repairing them corrupts centroid
  geometry so nothing solves (observed live: 195,356-px mask, healthy star
  counts, zero solves). Such a mask is **refused on save** (clear error telling
  the user to cap the lens) and **ignored on load** (protects devices already
  carrying one). The **3 DN threshold floor** (`MIN_THRESH_DN`, v0.11.18)
  guards the opposite failure: a properly capped dark frame is so uniform that
  MAD quantizes to 0, and a bare k·MAD threshold degenerates to "every pixel
  above the median" (observed live: 345,278 px = 47% of the frame from a
  correctly capped capture). With the floor a capped capture yields a normal
  few-hundred-pixel mask, so when the guard trips the sensor genuinely saw
  light. When `exposure_s`/`gain` are supplied,
  comms snapshots the live exposure/gain, pauses auto-exposure, captures at the
  requested **fixed worst-case** point, then restores (try/finally). The web UI
  buttons pass `0.9 s` + `gain 16` (the auto-exposure ceiling) so the mask is a
  conservative superset covering every shorter/lower-gain operating setting.
  Omitting both keeps the legacy behaviour (capture at the live setting).
* The solver loads the mask at startup if present.
* `hot_pixel_status` (count, mtime, loaded) and `hot_pixel_clear`.
* Camera page: "Capture dark frame" button (warns to cap the lens) + status.

---

## Live capture (frames + UTC + pointing, e.g. for satellites)

User-facing guide to the whole satellite workflow (setup, capturing, complete
workflows, every script and option): **`docs/satellite-user-guide.md`**.
Picking up the satellite work in a new chat: start from the latest handoff,
**`docs/decisions/2026-11-08-satellite-tools-handoff.md`**. Driving a mount
through a pass (assessment only, nothing built):
`docs/assessments/2026-11-08-satellite-tracking-common.md` and the per-mount
documents beside it.

`diofinder/live_capture.py`. The Home page **Start/Stop live capture** button
(`/livecap/toggle` → maint `live_capture_start`/`_stop`) records every frame the
solver processes, each paired with **its own** solve result, and on stop closes
`livecap_<UTC start>.zip` in `live_capture_dir`
(`/var/lib/diofinder/live_captures`). Utilities → **Live captures** lists,
downloads (`/livecap/download/<name>`) and deletes (`/livecap/delete/<name>`,
POST) archives; names must match `livecap_<YYYYMMDDTHHMMSSZ>.zip` exactly.

* **Where it runs: the solver process.** It already holds the frame and the
  result, so pairing is exact and needs no IPC. `SolutionTap` wraps
  `latest_solution` so every publish is seen (no edits to the many publish
  sites); `sync()` reads the control dict from the per-frame snapshot. Idle
  cost: one dict compare per frame. While recording the loop pays one frame
  `np.copy`. PNG encode (Pillow, level 3, ~200 KB per 960×760 frame) and file
  writes happen on a writer thread behind a 3-frame queue, and frames are
  **dropped (counted), never waited on**. Dark frames (peak < 20) are never
  recorded; they skip the solver path.
* **Archive:** `session.json` (format `diofinder-livecap/1`, observer
  lat/lon (East+)/elevation, FOV width + half-diagonal radius, start/end,
  frames/dropped/stop reason, `clock_plausible`, `clock` = where the time came
  from (`diofinder/clock_info.py`: network time synchronized, or the last
  SkySafari check/set with its drift, from `/var/lib/diofinder/clock_sync.json`;
  SkySafari sets whole seconds; a record from an earlier boot is reported as
  history, `trusted` says whether the clock was set this boot), `test_mode`),
  `frames.jsonl` (one record per frame: seq, sensor `exposure_start_utc` /
  `exposure_mid_utc` / `jd_start` / `jd_mid` / `exposure_s` / gain (from
  frame_meta; `time_source: handover` fallback when missing),
  `center_ra_deg`/`center_dec_deg` (J2000 image centre) + aim point, roll,
  mirrored, FOV, matches; unsolved frames carry `last_solved`, captured at
  enqueue time; solved frames carry `matched` = the solver's matched stars as
  `[x, y, ra, dec, mag]` rows), then `frame_000001.png`…
* **Matched stars:** while recording, the solve calls pass
  `return_matches=live_rec.active` (otherwise `False`, unchanged). Just before
  each publish, `note_matches(seq, matched_centroids, matched_stars)` attaches
  them to that frame only (matched by seq). They let an offline tool fit an
  exact per-frame WCS, with no roll or mirror convention to guess.
* **Robustness:** recording goes to `livecap_<id>.partial/`, flushed per frame;
  stop zips it STORED file by file (deleting each source, so peak extra disk use
  is one frame) via `.zip.tmp` then rename. `recover_partials` zips any
  leftover `.partial` at solver start (crash / power cut). It stops by itself at
  `live_capture_max_mb` (4000), after `live_capture_max_minutes` (60; 0 = no
  limit), or when free disk falls below `live_capture_min_free_mb` (1000).
  **Size cap (v0.11.76):** after each capture closes and at solver start
  (`recover_partials`), `prune_archives` deletes the OLDEST finished
  archives until all together are under `live_capture_keep_mb` (8000; 0 =
  keep all); the newest is never deleted. That stop comes from the writer thread
  through a flag, never a blocking put on its own queue. A session id is never
  restarted, and a new start is refused while the previous one is still
  zipping. comms warns when the clock is implausible (no RTC) or the site is
  0,0; `elevation_m` is a new conf key, used only here.
* **Offline:** `tests/satcheck_archive.py ARCHIVE.zip [--dry-run] [--group N]`
  (stdlib) lists SatChecker `/fov/satellite-passes/` hits per frame.
  `tests/satcheck_overlay.py ARCHIVE.zip` draws each predicted streak onto the
  frame itself via the fitted WCS, scores it and writes `report.csv`;
  `--stack` aligns and stacks each query group (brightest minus median), so a
  pass shows as dashes between its rails. Predictions in Earth's shadow and
  near-stationary objects are hidden by default (`--show-unlit`,
  `--show-stationary`, `--min-rate`); labels and arrows give each
  prediction's compass direction and rate. In the stack, straight trails are
  detected, timed frame by frame (direction + rate, even when the dashes
  merge), matched to a prediction only if it lies on the same line AND moves
  the same way at a similar speed, and written to `trails.csv`; unmatched
  ones are outlined red, "flashing → aircraft?" when the brightness along
  them comes in regular knots. The
  response structure is `data.satellites[name].positions[{ra, dec,
  julian_date}]`.
  **`tests/sat_identify.py ARCHIVE.zip --elements classfd.zip inttles.zip
  [--download] [--responses …]`** (needs `pip install sgp4`) goes past
  SatChecker's public catalog: each timed trail becomes per-frame RA/Dec
  observations, matched against supplied element sets (TLE/3LE, OMM CSV/XML/
  JSON, or zips; newest epoch per object wins; `--download` fetches Mike
  McCants' `classfd.zip`/`inttles.zip` from mmccants.org) by line, direction
  and rate with the along-track time FREE (stale elements keep their plane;
  the report gives "N s early/late"); a match is then refined (moved to the
  pass epoch, mean anomaly + node fitted). Outputs: `observations_iod.txt`
  (IOD format for Find_Orb / sat_id / satfit / SeeSat-L; `--station`),
  `identification.txt/json`, and the refined elements as classic TLE
  (Alpha-5 for 100000–339999), CCSDS OMM KVN and XML, and CelesTrak-style
  OMM JSON/CSV (9-digit catalog numbers: OMM only; SGP4's Satrec can't hold
  them, so they ride in `ElementSet.extra`). Besides the stack trails,
  `tests/sat_tracks.py` finds **frame-by-frame tracks** (each frame minus the
  steady sky of its neighbours, aligned on their own WCS; residual blobs
  linked along a smooth sky path; fixed-pattern pixels dropped): a
  **hand-tracked** satellite (a dot near the centre while the field sweeps)
  and arcs longer than one stack group; a longer linked track replaces the
  stack trails it continues (`--no-link`, `--link-interp`). **Photometry**
  (`tests/sat_photometry.py`): frames linearized through the tone curve
  (recorded in `session.json` `tone` by `live_capture.tone_info` since
  v0.11.76; older captures assume `tuning/imx477_finder.json`), per-frame zero
  point + shared vignetting from the matched stars, dash light measured
  against the steady sky; per-track magnitude ± (incl. a 0.15 mag colour
  term), `lightcurve_<id>.csv/.png`, Lomb-Scargle flash period (numpy FAP —
  astropy's needs scipy), IOD behaviour S/F/R/I/X (periodic → F if
  flash-like, else R; aperiodic → X if flash-like, else I; "flash-like" =
  duty cycle < 0.3 in linear light, so strobes with a ~0 baseline count). An
  unidentified flashing track (stack flash ratio, or 0.5–2 s flash period with
  > 2 mag amplitude) gets a "CHECK: may be an AIRCRAFT" note in the report and
  the post draft. **Reports**
  (checked against G. Lewis' IOD Version 0 description, which the user
  supplied — satobs.org is not reachable from the sandbox): `--points-per-pass`
  (4) IOD lines per track; angle format 2, whose position uncertainty (cols
  63–64) is in ARCMINUTES (`iod_line` converts from degrees; MX rounded up),
  uncertainty codes from the arc scatter + WCS fit
  and from the capture's clock record (`clock_uncertainty`: NTP 0.05 s,
  phone 0.2 s, SkySafari 1 s, else 10 s + a do-not-post warning;
  `--time-unc`), `--status` sky code E/G/F/P/B/T, magnitude (67–70) /
  uncertainty (72–73) / flash period (75–80) per the spec (`--no-iod-mag`),
  unknown objects `99999 99 099A` (`--unknown-desig`) named `unkYYMMDD`;
  the same points in R.D.E. format (`observations_rde.txt`: unknown =
  `9900000`, pieces A=01 skipping I/O, `--time-standard`); and
  `seesat_post.txt` (station/equipment/conditions/clock header + IOD + name +
  refined TLE; `--conditions`, `--equipment`). `observations.json` (format
  `diofinder-sat-observations/1`: site, clock, tracks with per-point sigma) feeds
  **`tests/sat_orbit.py`**: `fit` (one pass → circular 4-element orbit with a
  4.5 % mean-motion model error, so timing is honest; two+ passes → all 6
  elements, revolution count by a mean-motion scan, eccentricity by multi-start;
  `--start` = refine a known element set) → `fit.json` (with covariance),
  `fit.tle`, `fit_omm.*`; `predict` → visible passes (sunlit, dark sky,
  `--min-elevation`) with alt/az/RA/Dec pointing tables and timing ± (flagged
  "timing UNKNOWN" past 1/6 revolution). Given a multi-object file (e.g.
  `classfd.zip`) and no `--norad`/`--name` filter it predicts every object
  (`predict_all`, one shared `Sky` grid; < 2 rev/day skipped unless
  `--all-orbits`) and lists all passes in time order with each element set's
  age (`--tables` for pointing tables; `predictions.csv`); element sets more
  than `--max-age-days` (30; 0 = all) from the window are skipped in such
  listings (one object: a warning). **SGP4 sanity** (`sat_identify.sane_states`,
  inside `propagate` and `sat_oem.states`): long past a decay SGP4 returns
  err 0 with nonsense (inside the Earth at 30 km/s, beyond the Moon), seen as
  a 3-year-old classfd set "passing" every 2.5 min at 2°/s; states below
  80 km altitude, unbound, or with an osculating semi-major axis outside
  ×0.75–1.33 of the elements' get err 7. Passes also break at time gaps in the
  dark-only grid. **Sky region**
  (sites with buildings): `--sky east|west|north|south` (ne/nw/se/sw or a
  comma list = their intersection; east/west of the meridian, north/south of
  the east-west line through the zenith) keeps only passes whose whole
  visible part lies there; `--sky-mode part` cuts each pass to that stretch
  instead (`parse_sky` / `in_sky`, applied in `_passes_from`). **CCSDS OEM**
  (`tests/sat_oem.py`): `sat_orbit ephem ELEMENTS|fit.json` writes SGP4 state
  vectors every `--step` s (KVN `.oem` or `--xml`; frame TEME default,
  GCRF/EME2000/ICRF via astropy, ITRF; UTC; start snapped to a whole second;
  no covariance), and `predict` accepts an OEM (KVN/XML, any segments, UTC/
  TAI/TT/GPS, calendar or day-of-year epochs; Lagrange at the file's degree,
  only inside its span — `predict_oem`). OEM files are ignored as element
  sources; PreviSat `.gp` files are read in folder walks. Tests:
  `tests/test_sat_oem.py`. **Times:**
  `--start`/`--end` (an end replaces `--hours`; a bare `HH:MM` counts from the
  start's date, and an end before the start means the next day) are LOCAL
  (this computer's zone) unless `--utc` / `--in-tz ZONE` or the text carries
  `Z`/an offset; printed reports use `--out-tz` (default `utc`, so they can be
  shared; `local`, `UTC-5`, IANA names — Windows needs `pip install tzdata`;
  `sat_identify` and `iss_check` take `--out-tz` too). JSON, IOD, R.D.E.,
  TLE/OMM and the SeeSat-L draft are always UTC (`sat_identify.parse_time` /
  `when` / `set_output_tz`). **Element sources**
  (every script: `sat_identify --elements`, `sat_orbit fit --start` /
  `predict`, `iss_check --tle`) go through `sat_identify.load_elements` /
  `expand_element_sources`: files (TLE/2LE/3LE, CCSDS OMM XML/KVN/JSON/CSV —
  CelesTrak GP and Space-Track formats), `.zip`/`.gz` of them, folders (walked),
  URLs (downloaded, cached 2 h as CelesTrak asks), or comma-separated lists of
  any of these; per catalog number the newest epoch wins (`near_jd=` picks the
  epoch closest to a time instead). **`tests/iss_check.py ARCHIVE.zip`**
  (internet; `--tle` to use a saved set) treats CelesTrak's current ISS
  elements as ground truth for a capture of an ISS pass: across-track rms
  (astrometry), along-track offset in seconds (= the finder's clock error), a
  one-pass circular `sat_orbit` fit made without the ISS elements compared
  with them (period, inclination, node, km at +1 orbit/+6 h/+24 h, next-pass
  timing), and measured vs rough predicted magnitude; `iss_report.txt/json`,
  `iss_residuals.png`, `iss_sky.png` plus all sat_identify outputs. Tests:
  `tests/test_iss_check.py` (synthetic ISS pass with a 0.7 s clock error).
  Tests: `tests/test_sat_identify.py`
  (end to end on a synthetic capture drawn from a real classfd element set,
  37 s timing error), `tests/test_sat_photometry_tracks.py` (tone round trip,
  calibration, dash flux + flash period, light-curve classes, IOD columns,
  clock codes, linking, an end-to-end hand-tracked pass), `tests/test_sat_orbit.py`
  (one-pass circular with honest period error, two-pass recovery ~km after 3 d,
  prediction vs truth, CLI). Hand-tracking flags and the raw-frame decision:
  `docs/next-release-todo.md`.
* Tests: `tests/test_live_capture.py` (recorder end-to-end on a temp dir,
  writer-side stop, drop-not-block, recovery, comms commands, web routes, the
  SatChecker query builder).

## Solver-hang watchdog & IPC cleanup

`comms_proc._watchdog_loop` (daemon thread, gated by `watchdog_enabled`)
watches `latest_solution["epoch_monotonic"]`. The solver publishes every
bright frame, and dark (starless) frames publish a heartbeat at most once per
`_DARK_PUBLISH_FLOOR_S` (3 s) — a **time** floor, not the old every-Nth-frame
throttle, which at a long manual exposure (> 6 s) + a dark scene multiplied
the epoch gap past the 30 s watchdog and spuriously restarted the unit
(v0.11.28 fix; `_dark_publish_due` is pure/unit-tested). If the epoch stops
advancing for `watchdog_timeout_s` (default 30) the solver is hung: it logs
CRITICAL and `os._exit(1)` so systemd restarts the unit. A **blocking** camera stall (capture that hangs rather
than raises) is detected by the solver since v0.11.24: consecutive same-seq
timeout returns from `acquire_read_slot` for ~30 s freeze the publish epoch,
which converts the stall into a watchdog restart instead of re-solving the
frozen frame forever. The watchdog **arms only after the first non-zero epoch**, so
a slow boot / first DB load never trips it — but since v0.11.23 a **first-publish
deadline** (300 s) backstops that rule: a solver that wedges during startup and
never publishes at all now exits for a systemd restart instead of hanging
invisibly forever.

`systemd/diofinder.service` has `ExecStartPre=-/bin/sh -c 'rm -f …'` lines (the
`-` prefix makes failure non-fatal) that remove stale
`/dev/shm/diofinder_frame_*` and `/run/diofinder/maint.sock` before each start.

`scripts/calibrate_lens.py` is an **off-device** helper (not installed on the
Pi): point it at a directory of solved-frame PNGs and it runs `tetra3rs`
`calibrate_camera` to fit SIP distortion and prints the `distortion:` value to
set in `diofinder.conf`.

---

## Mount link — outbound sync to a GoTo mount (default OFF)

`diofinder/mountlink.py` turns the plate solver into an **alignment source**:
after a confident solve it pushes the solved position to a mount as a **SYNC
(never a slew)**, correcting the mount's pointing model. The finder serves
SkySafari over Wi-Fi and drives the mount over the GPIO serial line at the same
time. Full design + rationale in `docs/onstep-design.md`.

**v1 dialect: SynScan** (SkyWatcher Virtuoso). The transport is the **GPIO
UART** `/dev/serial0` (GPIO14/15) through a **MAX3232 → true RS-232** to the
Virtuoso's wired serial port (a 3.3 V MAX3232 — **not** a 5 V MAX232, which can
damage the Pi's 3.3 V GPIO); the tether (USB CDC-ACM console) is untouched.
`mountlink.py` is **dialect- and transport-pluggable**:

- `SerialTransport` (pyserial, imported lazily — the module and its tests need
  no hardware). NexStar/SynScan replies are `#`-terminated; `read_reply`
  returns the bytes before it.
- `SynScanLink` (NexStar-derived ASCII): `sync(ra, dec)` (precise `s` command),
  `version()`, `get_radec()`, `ping()`. Pure encode/decode
  (`deg_to_nexstar_hex` / `nexstar_hex_to_deg`) unit-tested. **Sync-only — there
  is no slew/GoTo command in the module; nothing here can move the mount.**
- Epoch: the finder solves in **J2000**; SynScan mounts use **JNow**. The
  conversion happens in the link via `diofinder.precession`, gated by the
  **mount's own** epoch (`mount_epoch`, distinct from `report_epoch` — the mount
  could be J2000 while SkySafari is JNow).
- `make_link(protocol, transport, epoch)` selects the dialect; future
  `lx200`/`alpaca` drop in behind the same `sync()` shape.

**Comms integration** (`comms_proc.py`):

- `_mount_loop` — daemon thread (mirrors `_auto_exposure_loop`), gated by
  `mount_enabled` + `mount_mode=="auto"`. Manual mode idles (the user drives it
  via `mount_sync`). Never dies; a serial failure tears down the link and
  retries next cycle.
- `_MountManager` (module singleton) owns the one serial connection, shared by
  the loop and the manual commands under a lock so pushes can't interleave.
- `mountlink.should_sync(solution, now, last_sync, gates, moving, last_motion_t)` —
  the pure auto-push policy (solved & fresh, ≥ `min_matches`, not slewing,
  **settled**, outside the arcmin deadband, past the min interval), unit-tested
  in `tests/test_mountlink.py`. **Settle gate** (`mount_auto_settle_s`, wired
  v0.11.76 — before that the key existed but nothing read it): the solve must
  be published ≥ `settle_s` after the IMU last saw motion, so a frame from the
  tail of a slew is never synced. `_mount_loop` keeps `last_motion_t` via the
  pure `mountlink.track_last_motion` (first still reading starts a full settle
  period); with no IMU (`_imu_is_moving` → None) the gate is skipped. 0 = off.
  The loop's latest decision is in `mount_status.auto_reason`.
- Maint commands: `mount_status`, `mount_sync` (one-shot manual push of the
  fresh solved position — `no fresh solution` otherwise), `mount_test` (open +
  query version, for wiring setup), `mount_set` (live keys: enabled/mode/epoch +
  the auto gates; transport keys are cfg-only/restart).
- Slew detection uses a **dedicated** IMU rate-state (`_imu_is_moving`) so the
  mount push never perturbs the LX200 pointing path's motion gate.

**Web UI**: a "Mount sync (SynScan)" card on the Camera/Advanced page (enable
toggle, mode + mount-epoch selects, **Test connection** / **Sync now** buttons,
a live status readout via `/api/mount`). Routes `/mount/{set,test,sync}` +
`/api/mount`.

**Config keys** (`mount_*`): `mount_enabled` (false), `mount_protocol`
(`synscan`), `mount_serial_port` (`/dev/serial0`), `mount_serial_baud` (9600),
`mount_epoch` (`jnow`), `mount_mode` (`manual`), and the auto gates
`mount_auto_{max_age_s,min_matches,settle_s,deadband_arcmin,min_interval_s}`.

**Out-of-the-box readiness** (all handled by `install.sh`): `pyserial` ships in
`requirements.txt` (lazy-imported, only needed when enabled); the `diofinder`
user is in `dialout`; `enable_uart=1`; and `/dev/serial0` is freed for the
mount — the UART kernel console is stripped from `cmdline.txt` and
`serial-getty@serial0/@ttyAMA0/@ttyS0` are masked (the login console lives on
the USB gadget `ttyGS0`). So on a fresh image the link works the moment it is
enabled + wired. **Failure is never silent and never disruptive:** manual mode
(default) is a no-op until you press Test/Sync, which show the error in the UI;
auto mode logs one WARNING and backs off `_MOUNT_FAIL_BACKOFF_S` (30 s) — no
per-cycle spam — with `last_error`/`syncs_fail` always in `mount_status`. The
finder's solving/LX200 path is unaffected regardless.

---

## Key file locations (on device)

| Path | Contents |
|------|---------|
| `/opt/diofinder/` | Installed Python package |
| `/etc/diofinder/diofinder.conf` | Runtime configuration |
| `/var/lib/diofinder/` | Star databases (`.npz`), debug ZIPs, saved frames |
| `/var/lib/diofinder/hot_pixel_mask.npz` | Hot-pixel mask (from `dark_capture`) |
| `/var/lib/diofinder/seeing_overrides.json` | Saved Good/Bad seeing overrides (factory presets stay immutable) |
| `/var/lib/diofinder/imu_extrinsic.json` | Persisted camera↔IMU mounting extrinsic (`imu_frame_R`, Unit A). Written by the solver when the Kabsch fit clears the strict save gate; seeded at boot so the exact LX200 prediction is live from the first solve. A good live fit that diverges (remount) overwrites it — self-healing, like the FOV recommit. Managed by `imu_persist.py`; cleared by factory reset `--clear-imu-calib`. |
| `/var/lib/diofinder/bno055_calib.json` | Persisted BNO055 accel/gyro calibration profile (22-byte blob, Unit B; gated by `imu_persist_bno055`, default off). Written/restored by `imu_proc.py` via `imu_persist.py`; cleared by factory reset `--clear-imu-calib`. |
| `/var/lib/diofinder/imu_solve_cal.json` | Persisted plate-solve-derived accel-tilt bias + gyro scale-factor (Unit C; gated by `imu_solve_cal_enabled`, default off). Learned by the solver from solve-vs-IMU residuals (`imu_solve_cal.py`), applied as the Mode-1 software correction in comms; self-heals on divergence; cleared by factory reset `--clear-imu-calib`. |
| `/var/lib/diofinder/star_names.csv` | Star naming catalog (from astro_databases release); powers the "Centered star" label (default: the BRIGHTEST cataloged star within `star_name_radius_deg` (2°) of the boresight, falling back to nearest; the expert toggle `star_name_brightest=false` reverts to pure nearest; `star_name_whole_fov=true` widens the brightest search to the whole frame — a stable align anchor when boresight is off). Optional — missing file disables naming. Refreshed by `diofinder-db-update`. |
| `/var/lib/diofinder/messier.csv` | Messier catalog (from astro_databases release ≥ `2026.07.01`); powers the "Centered object" DSO label (`messier.py`: the Messier object the aim point is on, matched by each object's own extent). Display only — separate from the star label, never touches the solve/align/aim. Gated by `star_name_dso` (default on). Optional — missing file disables it. Refreshed by `diofinder-db-update`. |
| `/var/lib/diofinder/live_captures/` | Live-capture archives `livecap_<UTC>.zip` (Home-page button; frames + `frames.jsonl` + `session.json`). `*.partial/` = recording in progress, zipped on stop or at the next solver start. Oldest deleted automatically past `live_capture_keep_mb` (8000 MB; the newest is kept); delete by hand from Utilities → Live captures. |
| `/var/lib/diofinder/captures/` | PNG captures when `save_failed_frames=true` (100 MB cap, oldest evicted) |
| `/run/diofinder/maint.sock` | Maintenance Unix socket |
| `/var/lib/diofinder/version` | Running release tag + ISO date. Stamped at image build (`install.sh`, from `DIOFINDER_VERSION`) and rewritten by `diofinder-update`. The `version` maint command resolves it as: this file → `git describe` of `/opt/diofinder` → in-code `cfg.version` (so a fresh burn reports its real tag instead of the stale default). |
| `/usr/local/bin/diofinder-ctl` | CLI wrapper for the maint socket |
| `/usr/local/bin/diofinder-update` | OTA update script (`--ref BRANCH` to track a branch; `--with-db` also refreshes the star DB via `diofinder-db-update`; `webui Update` page wraps it). Images are git-provisioned by `install.sh` so OTA works on imaged devices. **Wheel refresh installs the NEWER of {the vendor/wheels/ wheel baked at image build, the latest release wheel}, compared by version** — the fix for the shadowing bug where a burned image's never-cleaned `vendor/wheels/` permanently masked every newer release wheel (OTA could never advance olive-solve/sycamore). `DIOFINDER_WHEELS_LOCAL=1` forces the vendored wheel; `OLIVE_SOLVE_TAG`/`SYCAMORE_TAG` pin an exact release (authoritative, may downgrade). The star DB is reported-but-not-pulled by default (large, version-coupled, alters FOV calibration); `--with-db` opts into refreshing it. |
| `/usr/local/bin/diofinder-factory-reset` | Restores `diofinder.conf` from the shipped `diofinder.conf.default` and restarts the service; `--clear-overrides`/`--clear-hot-pixel-mask` additionally delete those artifacts. The webui's Config-page "Factory reset" card wraps it (`/factory_reset`). Runs outside the maint socket (like `/update`) since it restarts the very daemon that hosts it. |
| `/usr/local/bin/diofinder-bg-setup` | Show/set background mode + sizes via the maint socket |
| `/usr/local/bin/diofinder-bg-test` | On-device background-mode A/B on saved/live frames; `--solve` adds live-solver match rates |
| `/usr/local/bin/ap.sh` | Switch wlan0 to access-point mode |
| `/usr/local/bin/diofinder-wifi-boot` | The one place that sets which network wins at boot (`wifi_boot_network`: `ap` default / `saved`) in the NM profiles' autoconnect + priority; `get`/`apply`/`boot`/`status`. Called by `firstboot.sh`, `ap.sh`, `station.sh`, factory reset, and the Wi-Fi page's "Network at boot" card (via `wifi_boot_set` maint + sudo `apply`). NM picks the boot connection before `firstboot.sh` runs, so the choice must live in the profiles; boot only repairs a mismatch. See `docs/networking.md`; tests `tests/test_wifi_boot.py`. |
| `/usr/local/bin/diofinder-set-time` | Sets the clock; sudoers `etc/sudoers.d/diofinder-clock`. Two forms: `"YYYY-MM-DD HH:MM:SS"` (absolute UTC, whole seconds, via `timedatectl`) and `--offset SECONDS` (fractional; python reads and sets the clock in ONE process). The Utilities page **Clock** card measures NTP-style (several `/api/time` exchanges, the shortest round trip kept; error ≤ half of it, shown as ±, because a phone in Wi-Fi power save can hold replies back for most of a second — a single exchange misread that as clock error) and posts `{offset_s, rtt_ms}` to `/time/set` → `clock_info.set_clock_offset(direct=False)` → `--offset` (legacy `phone_ms` + half `rtt_ms` still accepted): the old form (wait for the phone's next whole second, then pass it as text) left the finder 0.8–0.9 s behind, because sudo + bash + `timedatectl set-ntp` took that long to start on the Zero and the named second had passed. `/api/time` shows finder vs device time (half-RTT corrected) and the source. **While network time is synchronized** (`/run/systemd/timesync/synchronized`) the card treats the finder as the reference: it shows the *device's* error ("1.00 ± 0.02 s ahead of network time"), disables Set, and `/time/set` returns 409 — timesyncd, re-enabled by the helper, would step a phone setting straight back (field 2026-11-04: "moved by +0.98 s", then −1.00 s again; the persistent 0.8–1 s reported earlier was the phone running ~1 s fast, not the finder). Every check/set is recorded by `diofinder/clock_info.py` in `/var/lib/diofinder/clock_sync.json` (with the boot id). **The daemon does not use it** (v0.11.76): `diofinder.service` has `NoNewPrivileges=true`, which blocks sudo, so it carries `AmbientCapabilities=CAP_SYS_TIME` and `clock_info.set_clock` sets the clock directly (helper as fallback). SkySafari's `:SC` sync sets the clock when it is ≥ 2 s off (≥ 10 s if a phone set it this boot; never over synchronized network time). |
| `/var/lib/diofinder/clock_last_known.json` | Last trusted time, saved by comms every 5 min (and after each SkySafari/phone set) while the clock is trusted. At start-up the launcher moves a clock that is behind it forward (`clock_info.restore_at_start`; never back, not when network time is synced) and records `restored`, which `clock_info.current()` reports as NOT accurate. |
| `/usr/local/bin/diofinder-power` | `reboot` / `poweroff`, a few seconds out via `systemd-run` (escapes the webui sandbox; lets the "Rebooting…" page reach the browser). Backs the Utilities page **Power** card (`/power/<action>`, confirm prompts, says which network it comes back on); sudoers `etc/sudoers.d/diofinder-power` allows exactly those two arguments. Tests `tests/test_power.py`. |
| `/usr/local/bin/station.sh` | Connect wlan0 to a station network |
| `/usr/local/bin/ap-watchdog.sh` | Mid-session AP-fallback watchdog (`diofinder-ap-watchdog.service`): if a station link drops and stays idle past the grace window (`DIOFINDER_APWD_GRACE`, 30 s), forces the self-AP back up via `ap.sh`. Closes the boot-only gap in `diofinder-ensure-ap`; see `docs/networking.md`. |

---

## Running tests

See `tests/README.md` for the full test catalogue. Quick smoke-test on device:

```bash
cd /opt/diofinder
sudo bash tests/diag_services.sh                  # check all processes alive
sudo python3 tests/diag_solve.py --live-shm       # one-shot solve with current image
sudo python3 tests/bench_pipeline_combos.py --live-shm  # pipeline timing
```

`DIOFINDER_LOGLEVEL=DEBUG sudo systemctl restart diofinder` enables verbose logging.
