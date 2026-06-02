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
efinder_main.py (launcher, CPU 1)
  │
  ├── camera_proc   (CPU 3)          — captures frames → shared memory
  ├── solver_proc   (CPUs 2 + 3)     — extracts stars, plate-solves
  └── comms_proc    (CPU 1)          — LX200 TCP server + maintenance socket
        └── imu_thread (daemon)      — BNO055 quaternion reader at 20 Hz
```

CPU 0 is left to the kernel. CPU affinity is set with `os.sched_setaffinity`.

### Inter-process communication

| Channel | Type | Direction | Purpose |
|---------|------|-----------|---------|
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
| `solve_timeout_ms` | int | comms (via maint) | solver |
| `test_mode` | bool | comms (via maint) | camera |
| `extract_backend` | str | comms (via maint) | solver |
| `sycamore_gate_mode` | str | main (startup only) | solver |
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
`fov_calibrated_max_error_deg` (0.5°). This makes subsequent solves faster
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
- The webui process runs on CPU 1 alongside `comms_proc` and the IMU thread.

Templates live in `webui/templates/`. Static assets in `webui/static/`.
The Jinja2 environment has a `log10` filter registered for log-scale sliders.

---

## Extractor backends

| Backend | Module | Speed | Sensitivity notes |
|---------|--------|-------|-------------------|
| `olive` | `olive_solve.get_centroids_from_image_fast` | fastest | sigma is a hard threshold |
| `sycamore` | `star_detect.detect_stars` | ~2× slower | matched-filter gate is more conservative at equal sigma; use sigma 7–8 instead of 9 |

Switch at runtime from the Status page or:
```bash
echo '{"cmd":"set_extract_backend","args":{"backend":"sycamore"}}' | \
  socat - UNIX-CONNECT:/run/efinder/maint.sock
```

The sycamore wheel lives in `vendor/wheels/` and is installed by `release.yml`.
To update it, run the **Vendor Sycamore** GitHub Actions workflow with the
desired version tag, then merge the resulting commit before tagging a release.

---

## Config file

`/etc/efinder/efinder.conf` — key: value pairs, `#` for comments.

`efinder/config.py::load_config` reads the file, applies `EFINDER_<KEY>`
environment overrides, and returns a `Config` dataclass. Unknown keys are
logged and ignored. Missing file uses all defaults.

`config.save_keys(updates)` rewrites changed keys in place, preserving
comments and unrecognised lines. It is the only function that writes to the
config file at runtime.

---

## Key file locations (on device)

| Path | Contents |
|------|---------|
| `/opt/efinder/` | Installed Python package |
| `/etc/efinder/efinder.conf` | Runtime configuration |
| `/var/lib/efinder/` | Star databases (`.npz`), debug ZIPs, saved frames |
| `/var/lib/efinder/captures/` | PNG captures when `save_failed_frames=true` |
| `/run/efinder/maint.sock` | Maintenance Unix socket |
| `/usr/local/bin/efinder-ctl` | CLI wrapper for the maint socket |
| `/usr/local/bin/efinder-update` | OTA update script |
| `/usr/local/bin/ap.sh` | Switch wlan0 to access-point mode |
| `/usr/local/bin/station.sh` | Connect wlan0 to a station network |

---

## Running tests

See `tests/README.md` for the full test catalogue. Quick smoke-test on device:

```bash
cd /opt/efinder
sudo bash tests/diag_services.sh                  # check all processes alive
sudo python3 tests/diag_solve.py --live-shm       # one-shot solve with current image
sudo python3 tests/bench_pipeline_combos.py --live-shm  # olive path timing
```

`EFINDER_LOGLEVEL=DEBUG sudo systemctl restart efinder` enables verbose logging.
