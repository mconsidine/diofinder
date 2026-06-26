# diofinder TODO

Living document. Update as features land or get deferred.

## Major features

### Polar alignment helper
✅ DONE. Three-point algorithm: user rotates mount in RA only, diofinder
captures three plate-solved positions (with dwell detection), fits a
great circle through the points on the celestial sphere, derives the
apparent RA axis, and decomposes the offset from the true pole into
azimuth and altitude errors using the observer's latitude.

Code: `diofinder/polar.py` (math) and `diofinder/polar_run.py` (state machine,
lives in solver_proc). Maintenance commands: `diofinder-ctl polar start|status|
cancel|set-latitude`.

SkySafari sends latitude via `:St#` on connect (requires "Set Time &
Location" enabled in SkySafari settings). Value is cached in config so
it survives reconnects and powers the Polar page latitude field.

If alignment completes before latitude is available, the result is stored
pending decomposition; once latitude arrives (via `:St` or
`set-latitude`), az/alt errors are computed retroactively without
recapture.

Possible future improvements:
- Expose `PolarParams.target_points` as a config knob for > 3 capture
  points (noise reduction).
- Detect accidental dec movement between capture points and warn.
- Live residual prediction during iterative adjustment.

### Web UI
✅ DONE. Flask app at `webui/app.py`, runs on port 80 via
`diofinder-webui.service`. Uses maintenance socket for all data — no
state of its own. Pages:

- `/`        dashboard: live RA/Dec, pointing, focus score, calibration,
             boresight, test/live toggle, Good/Bad seeing toggle
- `/camera`  live frame + exposure/gain/sigma/kernel/trail/match sliders,
             auto-exposure + tuning toggles, dark-frame (hot-pixel) capture
- `/focus`   live focus assistant (Laplacian variance + zoomed crop)
- `/bgtest`  background-mode A/B (with optional live-solver match rates)
- `/polar`   step-by-step polar alignment workflow with live status
- `/wifi`    AP/station switching
- `/config`  read-only view of `/etc/diofinder/diofinder.conf` + seeing toggle
- `/logs`    live journalctl tail for diofinder.service
- `/update`  trigger diofinder-update in one click
- `/healthz` 200 OK ping endpoint

Web UI uses `threaded=True` on Flask's dev server so slow solver
operations don't block concurrent requests.

TTL cache on high-frequency maint ops (`calibration_status` 5 s,
`polar_status` 1 s) prevents queue pile-up on 2-second auto-refresh
with multiple browser tabs.

Future improvements:
- Editable config (defer; SSH + restart is fine)
- Solve history charts
- Image preview / overlay from saved frames (frame-save is now implemented —
  see "Frame save for diagnostics" below; centroid overlays still TODO)
- Replace Flask dev server with gunicorn (probably never; single-user)

### Maintenance socket (Unix socket IPC)
✅ DONE. Listens at `/run/diofinder/maint.sock`. One daemon thread per
connection so slow solver operations don't block concurrent callers.

Protocol: newline-delimited JSON. Client library: `diofinder/maint.py`.
Dispatch table: `diofinder/comms_proc.py::_handle_maint_command`.

Supported commands: ping, version, status, boresight (show/center/set),
calibration (status/reset), exposure (get/set/persist), gain (set/persist),
auto_exposure_set, tuning_set, polar (start/status/cancel/set-latitude),
solver_params_get/set, match_params_get/set, seeing_get/seeing_set,
dark_capture, hot_pixel_status, hot_pixel_clear, bg_cache_status,
solve_centroids, set_test_mode, raw JSON passthrough.

### LX200 protocol
✅ DONE. Implemented commands: `:GR#`, `:GD#`, `:CM#` (sync/boresight),
`:Sr#`/`:Sd#` (set target RA/Dec), `:St#` (set latitude), `:Sg#` (set
longitude), `:Gt#` (get latitude), `:Gg#` (get longitude), `:SG#`/`:SL#`/`:SC#`
(time/date sync), `:MS#`, `:Q#`, `:M*#`/`:R*#` (motion/rate, ignored),
`:GVP#`/`:GVN#` (product/firmware), `:GW#`, `:GT#`, plus assorted SkySafari
query stubs (`:Gr#`, `:GS#`, `:GL#`, `:GC#`, `:GG#`, `:GA#`, `:GZ#`). The
`:CM#` sync reply is `M31 EX GAL MAG 3.5 SZ178.0'#`.

`:Gt#` and `:Gg#` return stored lat/lon in LX200 DMS format so
SkySafari receives a proper response and does not time out or retry.
This is required for SkySafari to reliably send `:St#` on connect.

### Camera frame rate
✅ FIXED. `FrameDurationLimits` max was set to `exposure_s + 200ms`,
artificially capping the camera at 2.5 fps even at short exposures.
Now set to `1_000_000_000` µs (1000 s sentinel), letting the ISP
hardware determine the actual achievable rate.

### Maint socket broken pipes
✅ FIXED. Was single-threaded; slow solver operations (1.5 s) during
web UI auto-refresh caused connection pile-up and broken pipes. Fixed
with one daemon thread per connection + TTL cache for frequent reads
+ dark frame fast-path to eliminate wasted 1.5 s cycles on dark frames.

### Dark frame fast-path
✅ DONE. After camera publishes a frame, solver checks
`bufs[idx].max() < 20` before invoking olive-solve extraction.
If true, the frame is published as an empty solution and the slot is
released in ~0.1 ms. Eliminates the full detect+solve cycle when the
lens is capped or the sky is completely dark.

### CPU affinity
✅ DONE.
- CPU 0: kernel/IRQs/system services + comms_proc + diofinder-webui + IMU thread
  (all I/O bound; cpu_comms=0 in config.py)
- CPU 1: solver_proc auxiliary core (third rayon core for star extraction;
  cpu_solver_aux=1)
- CPU 2: solver_proc primary core (cpu_solver=2)
- CPU 3: camera_proc (ISP DMA + memcpy to SHM) + solver rayon secondary
  (cpu_camera=3)

The solver pins itself to {cpu_solver, cpu_camera, cpu_solver_aux} = CPUs 1+2+3
and runs sycamore with set_num_threads(3).

### Zram swap
✅ DONE. `install.sh` configures `zram-tools` with `PERCENT=50`
(~256 MB) and `ALGO=lz4`. Provides near-RAM-speed compressed swap
critical for the solver's large data structures on the Zero 2W's 512 MB.

### Station.sh interactive mode
✅ DONE. `station.sh` called with no arguments scans available SSIDs
with `nmcli dev wifi list`, sorts by signal strength, presents a
numbered menu, and prompts for selection and password.

### FOV self-calibration
✅ DONE. `calibration.py` accumulates 30 successful solves, commits the
median FOV and distortion to config when stddev < 0.05°, then uses a
tight 0.1° tolerance window for subsequent solves.

### Boresight calibration
✅ DONE. `:CM#` sync command records the pixel offset between the
plate-solved star position and the reported boresight. Offset is
persisted to `/etc/diofinder/diofinder.conf`.

### Release image filename stamping
✅ DONE. Release images named `diofinder-sycamore-YYYYMMDD-vX.Y.Z.img.xz` (tagged
builds) or `diofinder-sycamore-YYYYMMDD.img.xz` (manual workflow dispatch). The
build date is embedded at build time (see `.github/workflows/release.yml`).

---

## Tactical TODOs

### Dark frame and hot pixel calibration
**DONE (hot-pixel half):** `diofinder/hot_pixel.py` + `dark_capture` /
`hot_pixel_status` / `hot_pixel_clear` maint commands + Camera-page button.
The solver median-stacks a capped-lens dark capture, builds a mask
(`median + 5·1.4826·MAD`), saves `/var/lib/diofinder/hot_pixel_mask.npz`, and
repairs masked pixels (8-neighbor mean) before each detection. Loaded at
startup. This rejects hot pixels during slews when the temporal cache is off.

Still open (optional): full **dark-frame subtraction** in `camera_proc.py`
(subtract a per-pixel dark from every frame before publishing). The hot-pixel
repair above covers the dominant fake-star case for a finder.

### Auto-exposure
**Partly done:** the comms-side controller (`_auto_exposure_loop`) is live and
now **defaults ON** (`auto_exposure_enabled: true`); `auto_exposure_target_stars`
and `auto_exposure_max_s` are live-mutable via `shared_cfg` (read each cycle,
written by seeing presets). The notes below describe a richer hysteresis design
not yet adopted.

Solver already knows detected star count per frame. Adaptive exposure
algorithm:
- `n < target * 0.5`: increase exposure 1.5× (clamped to max_s)
- `n > target * 1.5`: decrease 0.7× (clamped to min_s)
- Hysteresis: N consecutive frames in over/under band before changing
- Rate-limit: max 1 change per 10 s to avoid oscillation

Needs a queue from solver to camera_proc. Camera currently receives
commands only from comms_proc; extend `CameraCmd` with
`CAMERA_OP_SET_EXPOSURE` from solver (already exists in worker_cmds.py
— solver just needs to be wired to send it).

### Frame save for diagnostics
**DONE:** `solver_proc._save_frame` writes `{utc-timestamp}_{status}.png` to
`failed_frames_dir`, enforces a 100 MB cap (oldest `*.png` deleted first), and
swallows IO errors so a full disk never kills the solver loop. Centroid
overlays are still not drawn (raw grayscale only).

### Watchdog for solver hang
**DONE:** `comms_proc._watchdog_loop` (daemon thread, `watchdog_enabled` /
`watchdog_timeout_s`) checks `latest_solution["epoch_monotonic"]`; on staleness
> timeout it logs CRITICAL and `os._exit(1)` so systemd restarts the unit. Arms
only after the first publication. (Implemented in comms, not the launcher, so
it sees the same Manager dict the solver writes.)

### Auto-solve history ring buffer
Expose the last 60 solve results (timing, star count, status) as a
Manager list so the web UI can graph solve history. Currently metrics
are journald-only.

### LX200 sync reply string
Currently hardcoded to `M31 EX GAL MOC 99#` (SkySafari accepts any
short string). Some LX200 dialects expect specific formats. Could craft
something like the solved RA/Dec for display in SkySafari.

### Captive portal for wrong Wi-Fi credentials
If `station.sh` is given bad credentials, the only recovery is USB
tether. Plan: detect failed join after N seconds, fall back to AP mode
automatically. `comitup` is the cleanest existing solution.
Target: v0.8.

### Label stars on the live frame overlay
The Camera page and Status page name only the **single** cataloged star nearest
the boresight ("Centered star", from `star_names.csv`). To answer "what's that
bright star over there?" without re-pointing, draw star **names on the live
frame overlay** in `/frame.jpg`: project the catalog (or the solved match list)
through the solved WCS onto the image and label the brightest few in-frame named
stars next to their pixel positions. Needs: the solver to expose matched
star sky-coords + names (or a small catalog query by RA/Dec/FOV), and
`frame_jpg()` to draw the labels (it already draws the boresight + FOV rings).
Heavier than the centered-star readout; do it after the higher-value items.

### Status page: build version, clock, and observer location
Surface on the Status/dashboard page (`/`):
- the **image version / git tag** the device is running (the `version` maint
  command already returns it — the Update page uses it; just also show it on the
  dashboard, e.g. a small header/footer badge),
- the **current time** (UTC and/or local), and
- the **observer latitude / longitude** (already in config / `status`; the
  Config page shows `runtime_lat`/`runtime_lon` — mirror onto Status).

All three are already available to the web UI; this is a presentation-only
change to `webui/templates/dashboard.html` (+ the `dashboard()` route passing
`version` and a timestamp). Low risk.

### Rebrand "diofinder" → "diofinder" for this variant (deferred)
Where `diofinder` names *this* fork (not AstroKeith's upstream), migrate to
`diofinder`. End state:
- SSH login `diofinder@diofinder.local` (hostname, user, mDNS),
- Wi-Fi AP SSID `diofinder-XXXX`,
- and the rest of the user-facing surface (web UI title, README).

Wide, careful rename touching the systemd units (`diofinder.service`,
`diofinder-webui.service`, …), install/image scripts, config paths
(`/etc/diofinder/`, `/var/lib/diofinder/`, `/opt/diofinder/`), the `diofinder-ctl` /
`diofinder-update` CLIs, the maint socket path, and docs — with a migration story
for already-imaged devices (or simply "new images only"). **Do this only after
the build/burn workflows are stable**, since it changes paths the workflows and
OTA update depend on. Leave references to AstroKeith's upstream `eFinder_cli`
unchanged.

### IMU→sky alignment: status clarity + persistence (deferred)
The IMU→sky transform is learned from motion (`solver_proc._imu_update_reference`):
it needs ≥3 successful solves at pointings 0.1°–15° apart (r² ≥ 0.85) before the
IMU goes "active"; a fixed camera shows **"calibrating" forever by construction**
(no motion → no pairs → no fit). This is expected, not a bug — but two
improvements would make it less confusing:

- **Status clarity:** surface the progress (`imu_calib_n`/3 pairs and the fit
  quality `imu_calib_quality`) on the Status / Camera page with a hint like
  "slew to a few sky positions to finish IMU calibration", so a stationary user
  understands why it never leaves "calibrating".
- **Persist the transform across reboots:** the IMU-to-camera mounting is
  physically fixed, so `imu_calib_C` is a constant once learned. Today it lives
  only in `shared_cfg` and is rebuilt from scratch every boot (forcing a
  re-slew). Save it to config and restore on boot, gated behind a "recalibrate"
  reset for when the camera/IMU is physically remounted.

---

## Known issues to watch

- **Manager dict overhead**: `latest_solution` and `shared_cfg` go
  through a socket to the manager process on every read. At LX200 poll
  frequency (a few Hz) this is fine. If the web UI adds more frequent
  endpoints, consider migrating to a `shared_memory` struct with a
  seqlock.

- **SHM cleanup on crash**: if the launcher dies between `create=True`
  and the `finally` unlink, stale SHM blocks remain in `/dev/shm`.
  Re-running clears them via the unlink-before-create dance.
  **DONE:** `diofinder.service` now has `ExecStartPre=-/bin/sh -c 'rm -f …'`
  lines removing stale `/dev/shm/diofinder_frame_*` and the maint socket
  (non-fatal `-` prefix) before each start.

- **Vendor wheel freshness**: the olive-solve and sycamore-extract wheels
  in `vendor/wheels/` are pre-built aarch64 binaries. If a new version is
  needed, run the corresponding GitHub Actions workflow to rebuild and
  commit the updated wheel before tagging a release.
