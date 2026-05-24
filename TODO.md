# eFinder TODO

Living document. Update as features land or get deferred.

## Major features

### Polar alignment helper
✅ DONE. Three-point algorithm: user rotates mount in RA only, eFinder
captures three plate-solved positions (with dwell detection), fits a
great circle through the points on the celestial sphere, derives the
apparent RA axis, and decomposes the offset from the true pole into
azimuth and altitude errors using the observer's latitude.

Code: `efinder/polar.py` (math) and `efinder/polar_run.py` (state machine,
lives in solver_proc). Maintenance commands: `efinder-ctl polar start|status|
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
`efinder-webui.service`. Uses maintenance socket for all data — no
state of its own. Pages:

- `/`        dashboard: live RA/Dec, exposure/gain controls, pointing,
             focus score, calibration, boresight
- `/polar`   step-by-step polar alignment workflow with live status
- `/config`  read-only view of `/etc/efinder/efinder.conf`
- `/logs`    live journalctl tail for efinder + cedar-detect
- `/update`  trigger efinder-update in one click
- `/healthz` 200 OK ping endpoint

Web UI uses `threaded=True` on Flask's dev server so slow solver
operations don't block concurrent requests.

TTL cache on high-frequency maint ops (`calibration_status` 5 s,
`polar_status` 1 s) prevents queue pile-up on 2-second auto-refresh
with multiple browser tabs.

Future improvements:
- Editable config (defer; SSH + restart is fine)
- Solve history charts
- Image preview from saved frames (blocked on frame-save implementation)
- Replace Flask dev server with gunicorn (probably never; single-user)

### Maintenance socket (Unix socket IPC)
✅ DONE. Listens at `/run/efinder/maint.sock`. One daemon thread per
connection so slow solver operations don't block concurrent callers.

Protocol: newline-delimited JSON. Client library: `efinder/maint.py`.
Dispatch table: `efinder/comms_proc.py::_handle_maint_command`.

Supported commands: ping, version, status, boresight (show/center/set),
calibration (status/reset), exposure (get/set/persist), gain (set/persist),
polar (start/status/cancel/set-latitude), raw JSON passthrough.

### LX200 protocol
✅ DONE. Implemented commands: `:GR#`, `:GD#`, `:CM#` (sync/boresight),
`:St#` (set latitude), `:Sg#` (set longitude), `:Gt#` (get latitude),
`:Gg#` (get longitude), `:SL#`, `:SC#`, `:MS#`, `:Q#`, `:P#`,
`:GVP#`, `:GVN#`.

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
`bufs[idx].max() < 20` before invoking cedar-detect or cedar-solve.
If true, the frame is published as an empty solution and the slot is
released in ~0.1 ms. Eliminates the full detect+solve cycle (~1.5 s)
when the lens is capped or the sky is completely dark.

### CPU affinity
✅ DONE.
- CPU 0: kernel/IRQs/system services
- CPU 1: comms_proc + efinder-webui (I/O bound)
- CPU 2: solver_proc + cedar-detect-server — pipeline pair; cedar-detect
  runs then yields, solver consumes the centroids. They interleave rather
  than compete so one core is sufficient. CPUAffinity=2 in cedar-detect
  systemd unit; cpu_solver=2 in config.py.
- CPU 3: camera_proc alone — ISP DMA + memcpy to SHM

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
persisted to `/etc/efinder/efinder.conf`.

### Release image filename stamping
✅ DONE. Release images named `efinder-YYYYMMDD-vX.Y.Z.img.xz` (tagged
builds) or `efinder-YYYYMMDD.img.xz` (manual workflow dispatch). The
build date is embedded at build time.

---

## Tactical TODOs

### Dark frame and hot pixel calibration
Infrastructure is in place (maintenance socket ready for new commands).
Still TODO in `camera_proc.py`:
- **Dark frame**: open shutter long, capture N frames, take median per
  pixel, store as a numpy array. Subtract from each subsequent frame
  before publishing to FrameSlots.
- **Hot pixel map**: capture a long exposure, threshold, store (y,x)
  list. Cedar-detect already detects hot pixels per-frame; baking it in
  makes per-frame work faster and masks in the camera before the solver.

New maintenance commands needed: `efinder-ctl darkframe capture`,
`efinder-ctl hotpixels capture`.

### Auto-exposure
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
`save_failed_frames: true` is wired in config but the write logic in
`solver_proc.py` is not implemented. When done:
- On `status=NO_MATCH` or `TOO_FEW`, write frame with centroid overlays
  to `/var/lib/efinder/captures/YYYYMMDD-HHMMSS.png`.
- Cap disk usage at 100 MB; rotate oldest first.

### Watchdog for solver hang
systemd restarts the service on crash but not on hang. Add a heartbeat
monitor: if `latest_solution.epoch_monotonic` hasn't updated for 60 s,
the launcher kills the solver to force a systemd restart.

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

---

## Known issues to watch

- **Manager dict overhead**: `latest_solution` and `shared_cfg` go
  through a socket to the manager process on every read. At LX200 poll
  frequency (a few Hz) this is fine. If the web UI adds more frequent
  endpoints, consider migrating to a `shared_memory` struct with a
  seqlock.

- **SHM cleanup on crash**: if the launcher dies between `create=True`
  and the `finally` unlink, stale SHM blocks remain in `/dev/shm`.
  Re-running clears them via the unlink-before-create dance. A
  `ExecStartPre` cleanup in `efinder.service` would be more robust.

- **grpcio build from source in chroot**: if no aarch64 wheel exists for
  the Python version on Trixie, pip builds grpcio from source. The
  chroot has `build-essential` so it works but adds 10–20 minutes to
  the image build.
