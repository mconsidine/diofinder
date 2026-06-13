# diofinder — developer guide

## Target platform

- **Hardware**: Raspberry Pi Zero 2W (quad-core Cortex-A53, 512 MB RAM)
- **Camera**: Arducam IMX477 (12 MP Sony sensor, 1.55 µm pixel pitch, 4056×3040 array)
- **OS**: Debian GNU/Linux 13 "Trixie" (Pi OS Trixie Lite)
- **Python**: 3.11+, installed at `/opt/efinder/`
- **Service**: `systemd` unit `efinder.service`, managed with `sudo systemctl {start,stop,restart,status} efinder`

---

## Process architecture

```
efinder_main.py (launcher, CPU 0)
  │
  ├── camera_proc   (CPU 3)          — captures frames → shared memory
  ├── solver_proc   (CPUs 1+2+3)     — extracts stars, plate-solves
  └── comms_proc    (CPU 0)          — LX200 TCP server + maintenance socket
        └── imu_thread (daemon)      — BNO055 quaternion reader at 20 Hz
```

comms/webui share CPU 0 with the kernel (both are I/O-bound; kernel+IRQ load
is far below one core), freeing CPU 1 as a third solver core. CPU affinity is set with `os.sched_setaffinity`.

### Inter-process communication

| Channel | Type | Direction | Purpose |
|---------|------|-----------|-------|
| `efinder_frame_{0,1,2}` | POSIX shared memory | camera → solver, webui | Raw 8-bit frames |
| `FrameSlots` | `multiprocessing.Value` + lock | camera → solver | Ring-buffer slot index |
| `latest_solution` | `Manager().dict()` | solver → comms | Most recent plate-solve result |
| `shared_cfg` | `Manager().dict()` | main → all | Live-mutable settings |
| `align_request_q` | `mp.Queue` | comms → solver | :CM# alignment requests |
| `align_response_q` | `mp.Queue` | solver → comms | Alignment results |
| `solver_cmd_q` | `mp.Queue` | comms → solver | Maintenance commands |
| `solver_cmd_reply_q` | `mp.Queue` | solver → comms | Maintenance replies |
| `camera_cmd_q` | `mp.Queue` | comms → camera | Exposure/gain commands |
| `camera_cmd_reply_q` | `mp.Queue` | camera → comms | Camera replies |
| `/run/efinder/maint.sock` | Unix domain socket | webui/ctl → comms | JSON maintenance API |

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
| `min_centroids` | int | comms (via maint / `seeing_set`) | solver |
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
| `imu_available` | bool | imu_thread | comms, webui |
| `imu_q` | tuple (w,x,y,z) | imu_thread | comms |
| `imu_t` | float | imu_thread | comms |
| `imu_ref_q/ra/dec/roll/t` | varies | solver (post-solve) | comms |
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

The `sensor_full_width` / `sensor_full_height` config keys (default 4056×3040)
let this be overridden for other camera modules without a code change.

---

## FOV calibration

The solver accumulates a rolling window of 30 solved FOV measurements.
Once the window standard deviation falls below `fov_calibrated_stddev` (default
0.05°), `fov_calibrated` is set `true` in the config file and the search
tolerance tightens from `fov_max_error_deg` (1.0°) to
`fov_calibrated_max_error_deg` (0.1°). This makes subsequent solves faster
and more robust against false positives.

If you change the lens or camera mode, reset calibration:
```bash
sudo sed -i 's/^fov_calibrated:.*/fov_calibrated: false/' /etc/efinder/efinder.conf
sudo systemctl restart efinder
```

Or use the **Config** page → calibration section → Reset button.

---

## Adding an LX200 command

All LX200 handling lives in `efinder/comms_proc.py::_handle_lx200_command`.

1. Add an `if cmd == ":XX":` (or `cmd.startswith(":XX")`) branch.
2. Return a `bytes` object ending in `b"#"` per LX200 convention, or `b""` for
   commands that expect no reply (`:M*`, `:R*`, `:Q`).
3. Update the docstring listing implemented commands.
4. Test with `nc efinder.local 4060` (telnet/netcat) or add a simple test to `tests/diag_solve.py`.

There is no registration table — the function is a plain if/elif chain.

---

## Adding a maintenance socket command

The maintenance socket is the internal RPC bus used by the web UI and
`efinder-ctl`. All commands are handled in
`efinder/comms_proc.py::_handle_maint_command`.

Notable commands beyond the basics: `solver_params_get`/`solver_params_set`
(sigma 0–20, kernel_sigma 1.0–4.0, max_axis_ratio 0=off/1.5–10.0, local_noise,
bg mode/sizes, noise mode, min_centroids, solve_timeout_ms),
`match_params_get`/`match_params_set` (match_radius 0.005–0.05, match_threshold
1e-9–1e-3), `seeing_get`/`seeing_set` (apply the Good/Bad presets in
`efinder/seeing.py`; `seeing_set {"mode":"good"|"bad"}` routes every preset key
through the right channel — shared_cfg for live solver keys, a solver `set_db`
command for the database switch — persists via `config.save_keys`, and
invalidates the solver cache; `seeing_get` returns the mode, the preset table,
the effective per-key values, and a `drift` map of keys the user has overridden
since), `auto_exposure_set` (toggle the comms-side auto-exposure controller),
`auto_tune`/`auto_tune_status`/`auto_tune_cancel` (offline coordinate-search
sweep — see the Auto-exposure / gain controller section),
`tuning_set` (switch the libcamera tuning between `imx477_scientific.json` and
`imx477.json`; restart required), `bg_cache_status` (live temporal-cache
snapshot — state, model age, model kind row/block, served-cached vs fallback
counters), `solve_centroids` (plate-solve a caller-supplied centroid list on
the live solver's resident database — no second DB, used by
`diag_background --solve` and the web-UI background A/B), and the hot-pixel
trio: `dark_capture {"frames":N}` (cap the lens; median-stacks N frames into a
mask saved at `/var/lib/efinder/hot_pixel_mask.npz`), `hot_pixel_status`, and
`hot_pixel_clear`.

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
`efinder/solver_proc.py::_handle_solver_cmd` using the `SolverCmd` /
`SolverCmdReply` dataclasses in `efinder/worker_cmds.py`.

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

Star extraction uses **sycamore** `star_detect` (matched-filter gate, hardcoded
since v0.9.0 — the `gate_mode` parameter was removed; passing it raises
`TypeError`).
Wheels are not committed to git: `release.yml` downloads the sycamore and
olive-solve wheels from their repos' GitHub releases at image-build time (pin
with the `SYCAMORE_TAG` / `OLIVE_SOLVE_TAG` repo variables; unset = latest),
and on-device `efinder-update` refreshes them from the latest releases. A
wheel placed manually in `vendor/wheels/` overrides the download.

Detection is routed through `efinder/bg_cache.py::BackgroundCache`, not by calling
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
  proves too costly. The temporal model is orthogonal to the per-frame mode and
  composes with `row_percentile`, `line_median`, and `top_hat`.

A/B these on-device with `tests/diag_background.py` (e.g. `--inject-gradient 40`
to stress the glow case); the upstream extractor harness is
`sycamore-extract/tests/ab_background.py`.

---

## Config file

`/etc/efinder/efinder.conf` — key: value pairs, `#` for comments.

`efinder/config.py::load_config` reads the file, applies `EFINDER_<KEY>`
environment overrides, and returns a `Config` dataclass. Unknown keys are
logged and ignored. Missing file uses all defaults.

`config.save_keys(updates)` rewrites changed keys in place, preserving
comments and unrecognised lines. It is the only function that writes to the
config file at runtime.

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

---

## Seeing presets

`efinder/seeing.py` holds two flat preset tables, `SEEING_PRESETS["good"]` and
`["bad"]`. Each key in a preset is *also* an individually adjustable config
key, so applying a preset is exactly equivalent to setting each by hand.

* `seeing_set {"mode": "good"|"bad"}` (comms maint): writes every preset key to
  `shared_cfg` (live solver/auto-exposure keys), switches the solver database
  in-process via the `set_db` solver command when `star_db` differs, persists
  all of it with `config.save_keys`, and calls `_invalidate_solver_cache`.
* `seeing_get` returns the mode, both preset tables, the **effective** value of
  every preset-controlled key (shared_cfg over cfg), and a `drift` map of keys
  the user has individually overridden since applying a preset.
* `star_db="deep"` resolves to `star_db_deep` only when that names a file that
  exists; otherwise it stays on the standard `solver_db`.
* The toggle appears on the Status page and the Config page; `efinder-ctl
  seeing {get|set good|set bad}` is the CLI equivalent.

Every preset key is independently tunable through `solver_params_set`
(detection/solve keys) and `match_params_set` (match radius/threshold); the
Camera page exposes sliders for all of them.

---

## Tracking mode (experimental, opt-in)

A steady-state ROI tracking mode lives in `efinder/tracking.py` (pure numpy, no
`star_detect`/`tetra3`/`picamera2` import — unit-tested in `tests/test_tracking.py`)
and is wired into `solver_proc.solver_main` as a small **FULL ↔ TRACKING** state
machine. **Default OFF** (`tracking_enabled: false`) pending on-sky validation.

### What it does

After a run of confident full-frame solves it switches star **detection** from
full-frame extraction to small ROI windows placed around the previous frame's
solved star positions (`tracking.roi_detect` slices a `tracking_window_px`-square
window per predicted star — sycamore has no ROI API, so we slice the numpy frame
ourselves, call the injected per-window detector, take the brightest detection,
and offset its coordinates back to full-frame; overlapping windows are deduped by
proximity, edge windows are clamped inward). This saves the dominant full-frame
extraction cost (~6 ms → target ~1.5 ms). The recovered centroids are then solved
with the **existing** `solve_from_centroids` under a tight attitude hint
(`strict_hint=True`, 2° cone seeded from `last_sky_q`) instead of the blind path.

### HONEST scope / known limitation (NOT verify-only)

This is **ROI-windowed detection + tight-hint solving**. It does **not** skip the
solver's 4-star pattern hashing. olive-solve's Python API exposes **no** pure
verify-only entry point (project the catalog through a known attitude, match,
refine, skipping the hash), so a true verify-only fast path is impossible from
here and is left as **future work requiring an olive-solve API addition**. Do not
describe this as verify-only. The win is the saved extraction cost plus a
constrained (faster, fewer false positives) solve, not a skipped solver.

### State machine

* **FULL** — full-frame `bg_cache.detect` + the existing blind/IMU-propagated
  hint solve (`strict_hint=False`). The shipped behaviour, byte-for-byte.
* Enter **TRACKING** after `tracking_lock_frames` consecutive successful solves
  (and `tracking_enabled`), once the last solve produced ≥ `tracking_min_recover`
  centroids to predict from.
* **TRACKING** — `roi_detect` around the previous frame's solved centroids
  (`tracking.centroids_to_xy` inverts the solver's (row,col) back to (x,y)). v1
  uses the raw previous positions with **no** IMU/sidereal shift (sub-pixel drift
  between frames at this cadence is < 1 px, inside the window). If ≥
  `tracking_min_recover` stars recover, solve with the tight hint.
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
efinder-ctl raw '{"cmd":"solver_params_set","args":{"tracking_enabled":true}}'
efinder-ctl raw '{"cmd":"tracking_status"}'   # {enabled, state, frames_tracked, frames_full, recover_fail}
efinder-ctl raw '{"cmd":"solver_params_set","args":{"tracking_enabled":false}}'
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
* **Exposure-priority ladder**: when starved it raises exposure first and only
  climbs gain once exposure is at `auto_exposure_max_s`; when over-served it
  gives gain back first (cheap — only noise), then shortens exposure (which
  costs star trailing + latency on a moving mount).
* **Saturation** (`peak ≥ 250`) overrides everything and backs off (gain first).
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
  solver-blocking call well inside the 30 s watchdog window.
* **Merit**: among candidates clearing the match-rate floor and ≥ 80 % of the
  match target, minimize `w·solve_ms + w·kernel_sigma − w·sigma + bg_cost` — i.e.
  prefer fast solves, a tight kernel, a high sigma, and a cheap background.
  `detect_bin` (restart) and `star_db` (heavy reload) are deliberately **not**
  swept.
* **commit=true** persists the winner via `config.save_keys` (live solver keys
  also written to `shared_cfg`, `_invalidate_solver_cache` called); this drifts
  the active config away from the named seeing preset (visible in `seeing_get`'s
  drift map). **commit=false** restores the camera and changes nothing.
* CLI: `efinder-ctl auto-tune {start [--mode] [--commit] [--wait]|status|cancel}`.
  Web UI: an "Auto-tune (current sky)" card on the Camera page (start/cancel +
  progress poller via `/api/autotune`).

## Hot-pixel mask

`efinder/hot_pixel.py` builds a static hot-pixel mask from a capped-lens dark
capture and repairs masked pixels (8-neighbor mean, precomputed neighbor index
arrays, pure vectorized numpy, <1 ms) before each detection. This gives
hot-pixel rejection during slews, when the temporal cache is offline.

* `dark_capture {"frames": N}` → solver grabs N SHM frames ~0.3 s apart,
  median-stacks, flags pixels exceeding `median + 5·(1.4826·MAD)`, saves
  `/var/lib/efinder/hot_pixel_mask.npz` (indices + shape + count), loads it.
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
a slow boot / first DB load never trips it.

`systemd/efinder.service` has `ExecStartPre=-/bin/sh -c 'rm -f …'` lines (the
`-` prefix makes failure non-fatal) that remove stale
`/dev/shm/efinder_frame_*` and `/run/efinder/maint.sock` before each start.

`scripts/calibrate_lens.py` is an **off-device** helper (not installed on the
Pi): point it at a directory of solved-frame PNGs and it runs `tetra3rs`
`calibrate_camera` to fit SIP distortion and prints the `distortion:` value to
set in `efinder.conf`.

---

## Key file locations (on device)

| Path | Contents |
|------|---------|
| `/opt/efinder/` | Installed Python package |
| `/etc/efinder/efinder.conf` | Runtime configuration |
| `/var/lib/efinder/` | Star databases (`.npz`), debug ZIPs, saved frames |
| `/var/lib/efinder/hot_pixel_mask.npz` | Hot-pixel mask (from `dark_capture`) |
| `/var/lib/efinder/captures/` | PNG captures when `save_failed_frames=true` (100 MB cap, oldest evicted) |
| `/run/efinder/maint.sock` | Maintenance Unix socket |
| `/usr/local/bin/efinder-ctl` | CLI wrapper for the maint socket |
| `/usr/local/bin/efinder-update` | OTA update script (`--ref BRANCH` to track a branch; `webui Update` page wraps it). Images are git-provisioned by `install.sh` so OTA works on imaged devices. |
| `/usr/local/bin/efinder-bg-setup` | Show/set background mode + sizes via the maint socket |
| `/usr/local/bin/efinder-bg-test` | On-device background-mode A/B on saved/live frames; `--solve` adds live-solver match rates |
| `/usr/local/bin/ap.sh` | Switch wlan0 to access-point mode |
| `/usr/local/bin/station.sh` | Connect wlan0 to a station network |

---

## Running tests

See `tests/README.md` for the full test catalogue. Quick smoke-test on device:

```bash
cd /opt/efinder
sudo bash tests/diag_services.sh                  # check all processes alive
sudo python3 tests/diag_solve.py --live-shm       # one-shot solve with current image
sudo python3 tests/bench_pipeline_combos.py --live-shm  # pipeline timing
```

`EFINDER_LOGLEVEL=DEBUG sudo systemctl restart efinder` enables verbose logging.
