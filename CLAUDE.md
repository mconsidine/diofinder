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
  │── imu_thread (daemon, in-process) — BNO055 quaternion reader at 20 Hz
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
position/motion info (the 20 Hz reader, the solve-hint / slew-detection /
LX200-pointing consumers, and the calibration loop).

### Inter-process communication

| Channel | Type | Direction | Purpose |
|---------|------|-----------|-------|
| `diofinder_frame_{0,1,2}` | POSIX shared memory | camera → solver, webui | Raw 8-bit frames |
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

### `shared_cfg` keys and owners

| Key | Type | Written by | Read by |
|-----|------|-----------|--------|
| `boresight_y`, `boresight_x` | float | comms (via :CM# or maint) | solver, comms, webui |
| `detect_sigma` | float | comms (via maint) | solver |
| `detect_bg_mode` | str | comms (via maint, incl. `seeing_set`) | solver |
| `detect_bin` | int (1/2/4) | config file only (restart; cache is built at one binning) | solver |
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
| `auto_exposure_peak_floor` | float | *(no writer yet — config-only today; see docs/audit-2026-07.md F-M4)* | comms auto-exposure thread |
| `imu_rate_gate_dps` | float | comms (seed from cfg) | comms LX200 pointing (rate gate in `_imu_predict`) |
| `star_name_brightest` | bool | comms (via maint `solver_params_set`) | solver (centered-star naming) |
| `imu_available` | bool | imu_thread | comms, webui |
| `imu_q` | tuple (w,x,y,z) | imu_thread | comms |
| `imu_t` | float | imu_thread | comms |
| `imu_ref_q/ra/dec/roll/t` | varies | solver (post-solve) | comms (display); prediction reads the atomic `imu_ref` tuple |
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
`etc/diofinder.conf.default` points at it). The `config.py` dataclass fallback
is `imx477_scientific.json`, used only if the conf omits the key. Three profiles
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

**Self-healing (v0.11.19).** Two mechanisms keep a wrong committed FOV from
persisting:

* **Drift recommit**: every 50 solves the rolling median is compared against
  the committed value, with a dead band of 3× the **measured window stddev**
  (floored at `fov_drift_stddev_floor` = 0.01°). The old dead band used the
  0.05° convergence constant (0.15° band, ~44× real measurement noise), which
  let the 13.64-vs-13.55 sensor-mode miscentering sit uncorrected forever.
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
snapshot — state, model age, model kind row/block, served-cached vs fallback
counters), `solve_centroids` (plate-solve a caller-supplied centroid list on
the live solver's resident database — no second DB, used by
`diag_background --solve` and the web-UI background A/B), and the hot-pixel
trio: `dark_capture {"frames":N, "exposure_s"?, "gain"?}` (cap the lens;
median-stacks N frames into a mask saved at
`/var/lib/diofinder/hot_pixel_mask.npz`; optional `exposure_s`/`gain` capture at
a fixed worst-case point with snapshot-and-restore), `hot_pixel_status`, and
`hot_pixel_clear`. `frame_get {"after_seq"?}` returns the newest camera frame
(base64 u8 + shape + seq) via the solver's FrameSlots-bracketed read — never
torn by a concurrent camera write; chain `after_seq` for strictly consecutive
burst frames (the webui live view, debug bundles, and A/B captures all use it,
falling back to a direct SHM read labeled `frames_synced: False` only when the
daemon is down).

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
fallback (5-tuple refs / no fit).

Detection is routed through `diofinder/bg_cache.py::BackgroundCache`, not by calling
`detect_stars` directly. This gives three composable background strategies, all
toggleable from config (and live-overridable via `shared_cfg`):

- **Per-frame background mode** (`detect_bg_mode`): seven modes available:
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

New keys (this release):

| Key | Default | Notes |
|-----|---------|-------|
| `seeing_mode` | `good` | Active Good/Bad preset (see below). |
| `detect_kernel_sigma` | `1.5` | Matched-filter PSF width (sycamore≥0.12; capability-probed). |
| `detect_max_axis_ratio` | `0.0` | Trail rejection; 0→`float("inf")` (off), else 1.5–10.0. |
| `detect_local_noise` | `true` | Per-window noise in the matched filter (sycamore≥0.12). |
| `star_db_deep` | `""` | Optional deeper-magnitude db for the Bad preset; applied only if the file exists. |
| `auto_exposure_enabled` | `true` | **Flipped to ON** this release. |
| `watchdog_enabled` | `true` | Solver-hang watchdog (comms thread). |
| `watchdog_timeout_s` | `30.0` | Staleness before the solver is declared hung. |
| `extractor_backend` | `sycamore` | Centroid extractor: `sycamore` (matched filter + bg_cache) or `tetra3` (AstroKeith's olive-solve `get_centroids_from_image`). Live-mutable; set by the Legacy preset. |

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

```bash
# Toggle on and watch the state machine:
diofinder-ctl raw '{"cmd":"solver_params_set","args":{"tracking_enabled":true}}'
diofinder-ctl raw '{"cmd":"tracking_status"}'   # {enabled, state, frames_tracked, frames_full, recover_fail}
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
to `/var/lib/diofinder/bg_runs/`), there are two paths:

* **`tests/replay_corpus.py`** (off-device or on-device): the offline sweep.
  `--corpus` accepts a **directory of PNGs OR a `.zip`** (burst archives /
  debug bundles — only the raw `*.png` frames are extracted). It sweeps
  presets × `--bg-modes` × `--sweep-sigma` × `--sweep-kernel`, honours
  `--fov`/`--fov-err`/`--max-stars`, echoes the effective parameters in the run
  header + footer, and selects a winner (best solve rate, then median solve_ms,
  then star count). `--apply [--apply-mode good|bad]` persists the winner via
  the maint socket (`solver_params_set`/`match_params_set` with `persist`, plus
  an optional `seeing_override_save source=replay`) — the same machinery
  `auto_tune commit` uses. On-device, `--apply` needs the daemon running.
* **Dashboard "Tune from burst" card** (`webui/app.py` `/tune/start`,
  `/api/tune`, `/tune/apply`; `_tune_worker`): the on-device, button-driven
  sibling. It replays a chosen `bg_ab_*.zip` through the **live** solver
  (`solve_centroids`, resident DB — memory-safe, no second copy) over a grid of
  **detection** params (bg_mode × sigma × kernel; solving uses the live
  geometry), shows the winning combination, and "Apply winner" persists it
  (`solver_params_set persist=true`, optional `seeing_override_save
  source=replay`). For a sweep that *also* varies fov/max_stars, use
  `replay_corpus.py` on the same zip.

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

## Solver-hang watchdog & IPC cleanup

`comms_proc._watchdog_loop` (daemon thread, gated by `watchdog_enabled`)
watches `latest_solution["epoch_monotonic"]`. The solver publishes every frame
including dark ones, so if the epoch stops advancing for `watchdog_timeout_s`
(default 30) the solver is hung: it logs CRITICAL and `os._exit(1)` so systemd
restarts the unit. The watchdog **arms only after the first non-zero epoch**, so
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

## Key file locations (on device)

| Path | Contents |
|------|---------|
| `/opt/diofinder/` | Installed Python package |
| `/etc/diofinder/diofinder.conf` | Runtime configuration |
| `/var/lib/diofinder/` | Star databases (`.npz`), debug ZIPs, saved frames |
| `/var/lib/diofinder/hot_pixel_mask.npz` | Hot-pixel mask (from `dark_capture`) |
| `/var/lib/diofinder/seeing_overrides.json` | Saved Good/Bad seeing overrides (factory presets stay immutable) |
| `/var/lib/diofinder/star_names.csv` | Star naming catalog (from astro_databases release); powers the "Centered star" label (default: the BRIGHTEST cataloged star within `star_name_radius_deg` (2°) of the boresight, falling back to nearest; the expert toggle `star_name_brightest=false` reverts to pure nearest). Optional — missing file disables naming. Refreshed by `diofinder-db-update`. |
| `/var/lib/diofinder/captures/` | PNG captures when `save_failed_frames=true` (100 MB cap, oldest evicted) |
| `/run/diofinder/maint.sock` | Maintenance Unix socket |
| `/var/lib/diofinder/version` | Running release tag + ISO date. Stamped at image build (`install.sh`, from `DIOFINDER_VERSION`) and rewritten by `diofinder-update`. The `version` maint command resolves it as: this file → `git describe` of `/opt/diofinder` → in-code `cfg.version` (so a fresh burn reports its real tag instead of the stale default). |
| `/usr/local/bin/diofinder-ctl` | CLI wrapper for the maint socket |
| `/usr/local/bin/diofinder-update` | OTA update script (`--ref BRANCH` to track a branch; `webui Update` page wraps it). Images are git-provisioned by `install.sh` so OTA works on imaged devices. |
| `/usr/local/bin/diofinder-bg-setup` | Show/set background mode + sizes via the maint socket |
| `/usr/local/bin/diofinder-bg-test` | On-device background-mode A/B on saved/live frames; `--solve` adds live-solver match rates |
| `/usr/local/bin/ap.sh` | Switch wlan0 to access-point mode |
| `/usr/local/bin/station.sh` | Connect wlan0 to a station network |

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
