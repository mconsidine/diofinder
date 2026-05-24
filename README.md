# eFinder — combo edition (Pi Zero 2W)

Plate-solving electronic finder for amateur telescopes. Runs on a Raspberry Pi
Zero 2W with an Arducam 12 MP IMX477. Reports live pointing to SkySafari (or
any LX200-speaking application) over Wi-Fi or USB tether, with sub-arcsecond
boresight registration and a built-in three-point polar alignment assistant.

**Solver backends (runtime-switchable):**
- **tetra3rs** (default) — Rust in-process solver, ~10–60 ms per solve when
  seeded with the previous position, ~300–800 ms blind. No external process.
- **cedar** — tetra3 Python solver, ~200–1200 ms. Slower but retains legacy
  compatibility.

Both backends use **cedar-detect** (Rust gRPC) for star centroid extraction.

---

## Table of contents

1. [What it does](#what-it-does)
2. [Hardware requirements](#hardware-requirements)
3. [Quick start — flashing the image](#quick-start--flashing-the-image)
4. [First boot and network access](#first-boot-and-network-access)
5. [Connecting from SkySafari](#connecting-from-skysafari)
6. [LX200 command reference](#lx200-command-reference)
7. [Web UI](#web-ui)
8. [Polar alignment](#polar-alignment)
9. [Focus assessment](#focus-assessment)
10. [IMU dead-reckoning (optional)](#imu-dead-reckoning-optional)
11. [Wi-Fi modes](#wi-fi-modes)
12. [Configuration reference](#configuration-reference)
13. [Maintenance CLI (`efinder-ctl`)](#maintenance-cli-efinder-ctl)
14. [Updating without a git repository](#updating-without-a-git-repository)
15. [Architecture](#architecture)
16. [Diagnostic guide (SSH / PuTTY)](#diagnostic-guide-ssh--putty)
17. [Benchmark and diagnostic scripts](#benchmark-and-diagnostic-scripts)
18. [Performance characteristics](#performance-characteristics)
19. [Building and development](#building-and-development)
20. [Known limitations and deferred work](#known-limitations-and-deferred-work)

---

## What it does

The eFinder turns a Raspberry Pi Zero 2W into a self-contained plate-solving
"sky compass" that mounts to your telescope tube. Point the scope at any part
of the sky. Within a second or two the eFinder has identified the star field,
calculated the telescope's precise RA/Dec, and is streaming that position to
SkySafari in real time via the LX200 protocol.

**Core capabilities:**

- **Continuous plate-solving** at ~1–2 s per frame (960×760). Each solve
  reports RA, Dec, field-of-view, and image orientation (position angle / roll).
- **Two solver backends**, switchable live from the dashboard: tetra3rs (Rust,
  default, fast seeded solves) and cedar (Python, legacy-compatible).
- **Boresight calibration** via SkySafari's Sync command: center a star, tap
  Sync, and the eFinder stores the pixel offset. Persisted across reboots.
- **FOV self-calibration**: after ~30 successful solves the eFinder commits
  the median field-of-view and uses a tighter (0.1°) window — improving speed
  and reliability.
- **Polar alignment assistant**: rotate the mount in RA only at three positions.
  The eFinder fits a great circle through the three plate-solve results, derives
  the true RA axis, and reports azimuth and altitude corrections.
- **IMU dead-reckoning** (optional, BNO055): smooths SkySafari position updates
  between solves to < 1 ms latency. Self-calibrating. Hot-pluggable.
- **Live web UI** on port 80: dashboard, camera controls, solver parameters,
  polar alignment, Wi-Fi switching, configuration viewer, live log tail.
- **Dark frame fast-path**: frames below 20 ADU peak are detected in ~0.1 ms
  and skipped entirely.
- **Dual network**: USB Ethernet gadget (`10.55.0.1`) and self-hosted Wi-Fi AP
  (`10.42.0.1`) simultaneously. Browser-based station-mode switching with
  automatic AP fallback.

---

## Hardware requirements

| Component | Notes |
|---|---|
| Raspberry Pi Zero 2W | Quad-core Cortex-A53 required; Pi Zero 1 is too slow |
| Arducam 12 MP IMX477 (HQ Camera) | 8-bit Y-plane capture via YUV420 |
| CSI-2 ribbon cable | 15-pin to 22-pin for the Zero's smaller connector |
| MicroSD card | 8 GB minimum; Class 10 / A1 recommended |
| Micro-USB power supply | 5V 2A; middle port is data+power |
| Micro-USB to USB-A cable | For USB tether |
| Mounting hardware | Dovetail or finder shoe |
| BNO055 IMU *(optional)* | I²C. Connect to GPIO 2 (SDA) / GPIO 3 (SCL). Hot-pluggable. |

---

## Quick start — flashing the image

1. Download `efinder-YYYYMMDD-vX.Y.Z.img.xz` from the
   [Releases](../../releases) page.
2. Flash with **Raspberry Pi Imager** (choose "Use custom image"), **balena
   Etcher**, or `dd`:
   ```bash
   xz -d efinder-YYYYMMDD-vX.Y.Z.img.xz
   sudo dd if=efinder-YYYYMMDD-vX.Y.Z.img bs=4M status=progress oflag=sync of=/dev/sdX
   ```
3. Insert and boot. First boot takes 30–90 s; the green LED steadies when the
   eFinder application has started.

### Install on an existing Pi OS Trixie Lite card

```bash
git clone https://github.com/mconsidine/efinder-combo
cd efinder-combo
sudo bash scripts/install.sh
```

`install.sh` is idempotent — safe to re-run after updates. It installs system
packages, Python dependencies, gRPC stubs, the cedar-detect binary, tetra3rs,
and all systemd unit files. Reboot after the first run.

---

## First boot and network access

`efinder-firstboot.service` configures two always-available interfaces on every
boot:

| Interface | IP address | How to reach it |
|---|---|---|
| **USB Ethernet gadget** | `10.55.0.1` | Connect the Pi's middle micro-USB port to your computer. No driver needed on macOS / Linux. |
| **Wi-Fi access point** | `10.42.0.1` | Join SSID `efinder-XXXX` (last 4 hex digits of the Wi-Fi MAC). |

**Default credentials:**

| Service | Username | Password |
|---|---|---|
| SSH | `efinder` | `12345678` |
| Wi-Fi AP | — | `12345678` |

**Quick connectivity check:**

```
http://efinder.local        Web UI
ssh efinder@efinder.local   SSH (password: 12345678)
ping efinder.local
```

mDNS hostname `efinder.local` works on macOS and Linux. Windows needs Bonjour.
Android `.local` resolution varies — use the IP address directly if needed.

---

## Connecting from SkySafari

### Setup (SkySafari 6 or later)

1. Open **Settings → Telescope → Scope Setup**.
2. **Telescope Brand** → Meade. **Mount Type** → LX-200 GPS.
3. **Connection** → WiFi (TCP).
4. Enter `efinder.local` or the IP address. **Port** → `4060`.
5. Enable **Set Time & Location** (sends your GPS position on connect —
   required for polar alignment decomposition).
6. Tap **Done**, then **Connect**.

### Syncing the boresight

1. Centre the target star in the eyepiece.
2. Tap the star in SkySafari.
3. Tap **Sync** (telescope icon → Sync).

The eFinder records the pixel offset and writes it to config. Survives restarts.

---

## LX200 command reference

| Command | Description | Response |
|---|---|---|
| `:GR#` | Get RA | `HH:MM:SS#` |
| `:GD#` | Get Dec | `±DD°MM:SS#` |
| `:CM#` | Sync / boresight calibration | `M31 EX GAL MOC 99#` |
| `:St dd*mm#` | Set observer latitude | `1` |
| `:Sg ddd*mm#` | Set observer longitude | `1` |
| `:Gt#` | Get latitude | `sDD*MM#` |
| `:Gg#` | Get longitude | `sDDD*MM#` |
| `:SL HH:MM:SS#` | Set local time | `1` |
| `:SC MM/DD/YY#` | Set date | `1Updating Planetary Data#` |
| `:MS#` | Move to target (ignored) | `0` |
| `:Q#` | Stop (ignored) | _(empty)_ |
| `:P#` | Toggle precision (no-op) | `HIGH PRECISION` |
| `:GVP#` | Product name | `eFinder#` |
| `:GVN#` | Firmware version | version string |

---

## Web UI

Open `http://efinder.local` from any browser on the same network.

### Dashboard (`/`)

Auto-refreshes every 1.5 s. Shows:

- **Pointing**: RA/Dec, star count, match count, solve time, peak pixel, FOV,
  roll. Status badge: SOLVED / TOO_FEW / NO_MATCH / TIMEOUT / DARK.
- **Solver backend toggle**: switch between **tetra3rs** and **cedar** live,
  without restarting the daemon.
- **Test / Live mode toggle**: switch to a static test image (if one is present
  at `/var/lib/efinder/test.png`) or back to the live camera.
- **Focus**: Laplacian variance score at the last committed frame.
- **Calibration**: FOV calibration state, committed FOV, statistics, and a
  **Recalibrate** button.
- **Boresight**: current pixel coordinates; **Reset to center** button.
- **IMU**: calibration progress and fit quality (shown when BNO055 is present).

### Camera (`/camera`)

Live camera view with controls that take effect immediately (no page reload):

- **Live frame**: JPEG from the current SHM buffer with a boresight crosshair.
  Refreshes every 2 s.
- **Exposure (seconds)**: log-scale slider + numeric box + `−`/`+` buttons
  (±0.05 s per click). Valid range: 0.001–10 s.
- **Gain (1–64)**: slider + numeric box + `−`/`+` buttons (±1 per click).
- **Detection sigma**: star extraction threshold. Slider + numeric box +
  `−`/`+` buttons (±1.0 per click). Default 9. Lower finds fainter stars;
  higher rejects noise. Valid range: 3–20.
- **Solve timeout (ms)**: maximum time per frame. Slider + numeric box.
  Default 1500 ms.
- **Binned star candidates**: checkbox. When checked (default), cedar-detect
  searches a 2×2 binned image for candidate regions before centroiding at full
  resolution. Roughly 20–40% faster extraction with negligible loss at this FOV.

All controls auto-apply on change. Use **Persist** checkbox + **Apply & save**
button to write values to `/etc/efinder/efinder.conf`.

### Polar alignment (`/polar`)

Step-by-step three-point polar alignment workflow. See [Polar alignment](#polar-alignment).

### Wi-Fi (`/wifi`)

Browser-based Wi-Fi management. See [Wi-Fi modes](#wi-fi-modes).

### Configuration (`/config`)

Read-only view of `/etc/efinder/efinder.conf`.

### Logs (`/logs`)

Live `journalctl` tail for `efinder.service` and `cedar-detect.service`.

### Update (`/update`)

One-click `efinder-update`. Fetches the latest tagged release, updates
dependencies, regenerates gRPC stubs, downloads the matching cedar-detect
binary, and restarts both services. Does not touch `efinder.conf`.

### Health endpoint (`/healthz`)

Returns HTTP 200 `{"status": "ok"}` if the daemon is reachable.

---

## Polar alignment

### Theory

Rotating a mount in RA traces a small circle on the celestial sphere centered
on the RA axis. Capturing three plate-solved positions gives three points on
that circle. Fitting a great circle yields the circle's center — where the RA
axis is actually pointing. The offset from the true pole, decomposed by
observer latitude, gives the azimuth and altitude corrections to apply.

### Procedure (web UI)

1. Ensure SkySafari is connected with **Set Time & Location** enabled (or set
   latitude via `efinder-ctl polar set-latitude`). Confirm latitude is shown on
   the Polar page.
2. Aim at an area with 20–30 visible stars. Click **Start Polar Alignment**.
3. Wait for capture point 1 (green checkmark).
4. Rotate 20–40° in RA only (do not touch declination).
5. Click **Capture point 2**. Wait.
6. Rotate another 20–40° in RA.
7. Click **Capture point 3**. The eFinder shows azimuth error (E/W) and
   altitude error (Up/Down).
8. Make adjustments; repeat until total error < 0.1° (visual) or < 0.05°
   (unguided imaging).

### Procedure (CLI)

```bash
efinder-ctl polar set-latitude 44.5   # if not sent by SkySafari
efinder-ctl polar start
# rotate RA, then repeat until 3 points captured:
efinder-ctl polar status
efinder-ctl polar cancel              # abort if needed
```

---

## Focus assessment

The dashboard shows a **focus score** (Laplacian variance) from the brightest
detected star. Higher = sharper. Typical values:

| Condition | Score |
|---|---|
| Severely out of focus | < 100 |
| Reasonable | 500–2000 |
| Well focused, good seeing | > 2000 |

Adjust focus until the score peaks.

---

## IMU dead-reckoning (optional)

A BNO055 on the I²C bus smooths SkySafari position updates between plate-solves
to < 1 ms latency. Self-calibrates passively from consecutive solve pairs.
Active once 3 pairs are collected and fit R² ≥ 0.85. Hot-pluggable.

### Wiring

| BNO055 | Pi Zero 2W |
|---|---|
| VIN | 3.3V (pin 1) |
| GND | GND (pin 6) |
| SDA | GPIO 2 (pin 3) |
| SCL | GPIO 3 (pin 5) |

Add the `efinder` user to the `i2c` group if not already done:

```bash
sudo usermod -aG i2c efinder
```

---

## Wi-Fi modes

### AP mode (default)

SSID `efinder-XXXX`, password `12345678`, gateway `10.42.0.1`.

### Switching to station mode

**Web UI:** Open `/wifi`, enter SSID and password, click **Connect**. A 90 s
countdown polls for success; two independent fallback layers restore AP mode
automatically if the connection fails.

**SSH:**
```bash
sudo /usr/local/bin/station.sh "MySSID" "MyPassword"
# or interactively (shows a numbered scan list):
sudo /usr/local/bin/station.sh
```

### Switching back to AP mode

**Web UI:** Open `/wifi` → **Switch to AP Mode**.

**SSH:** `sudo /usr/local/bin/ap.sh`

### Recovery if Wi-Fi is lost

Connect via USB Ethernet (`10.55.0.1`) and run `sudo /usr/local/bin/ap.sh`
via SSH or browser at `http://10.55.0.1/wifi`.

---

## Configuration reference

Config lives in `/etc/efinder/efinder.conf` (`key: value` format).

```bash
sudo nano /etc/efinder/efinder.conf
sudo systemctl restart efinder
```

Any key can be overridden by environment variable for one-off testing:

```bash
sudo EFINDER_EXPOSURE_S=0.5 systemctl restart efinder
```

### Full reference

| Key | Default | Description |
|---|---|---|
| `frame_width` | `960` | Capture width in pixels |
| `frame_height` | `760` | Capture height in pixels |
| `exposure_s` | `0.2` | Exposure time in seconds (0.001–10.0) |
| `gain` | `20.0` | Analogue gain (1.0–64.0) |
| `fov_deg` | `13.5` | Initial FOV estimate in degrees. Self-calibrates after 30 solves. |
| `detect_sigma` | `9.0` | Cedar-detect extraction threshold (σ above background). Default 9. |
| `detect_hot_pixels` | `true` | Cedar-detect hot-pixel removal. |
| `detect_use_binned` | `true` | Use 2×2 binned image for candidate search (faster; negligible quality loss at this FOV). |
| `solve_timeout_ms` | `1500` | Per-frame solver budget in ms. |
| `tetra3rs_db` | `/var/lib/efinder/efinder-tetra-database.bin` | tetra3rs Rust database file. |
| `tetra3_db` | `default_database` | tetra3 Python database name (used by cedar backend). |
| `cedar_detect_socket` | `localhost:50051` | gRPC address of cedar-detect-server. |
| `lx200_port` | `4060` | TCP port for the LX200 server. |
| `boresight_y` | `380` | Boresight Y in pixels. Set by `:CM#` sync. |
| `boresight_x` | `480` | Boresight X in pixels. Set by `:CM#` sync. |
| `latitude_deg` | _(empty)_ | Observer latitude. Auto-populated from SkySafari `:St` on connect. |
| `longitude_deg` | _(empty)_ | Observer longitude. Auto-populated from SkySafari `:Sg` on connect. |
| `fov_max_error_deg` | `1.0` | FOV tolerance window. Tightened to 0.1° after calibration. |
| `match_radius` | `0.01` | Tetra3rs match radius parameter. |
| `match_threshold` | `1e-5` | Tetra3rs match probability threshold. |
| `min_centroids` | `8` | Minimum centroids required to attempt a solve. |
| `hint_uncertainty_deg` | `0.1` | Angular radius of the attitude hint for seeded solves. |
| `cpu_camera` | `3` | CPU affinity for camera_proc. |
| `cpu_solver` | `2` | CPU affinity for solver_proc. |
| `cpu_comms` | `1` | CPU affinity for comms_proc. |
| `save_failed_frames` | `false` | When true, saves failed frames to `/var/lib/efinder/captures/`. |

### CPU affinity layout

| CPU | Role |
|---|---|
| 0 | Linux kernel, IRQs, sshd, NetworkManager — never pinned |
| 1 | `comms_proc` (LX200 + maint socket) + `efinder-webui` (Flask) |
| 2 | `solver_proc` + `cedar-detect-server` — pipeline pair; cedar-detect runs first then yields, solver consumes the centroids |
| 3 | `camera_proc` alone — ISP DMA + memcpy to SHM |

---

## Maintenance CLI (`efinder-ctl`)

Talks to the daemon over `/run/efinder/maint.sock`. Works while the solver
is running and does not require the web UI.

```bash
# Status
efinder-ctl ping
efinder-ctl status                        # RA, Dec, backend, stars, solve_ms

# Boresight
efinder-ctl boresight show
efinder-ctl boresight center
efinder-ctl boresight set 380 480         # Y X

# Calibration
efinder-ctl calibration status
efinder-ctl calibration reset

# Exposure / gain
efinder-ctl exposure get
efinder-ctl exposure set 0.3
efinder-ctl exposure set 0.3 --persist
efinder-ctl gain set 15.0 --persist

# Solver backend (live switch, no restart)
efinder-ctl raw '{"cmd":"set_backend","args":{"backend":"tetra"}}'
efinder-ctl raw '{"cmd":"set_backend","args":{"backend":"cedar"}}'

# Test / live mode
efinder-ctl raw '{"cmd":"set_test_mode","args":{"enabled":false}}'

# Polar alignment
efinder-ctl polar start
efinder-ctl polar status
efinder-ctl polar cancel
efinder-ctl polar set-latitude 44.5

# Raw JSON (any command)
efinder-ctl raw '{"cmd":"ping","args":{}}'
```

---

## Updating without a git repository

The device is installed via chroot image copy — there is no `.git` directory
on the device, so `git pull` will not work. Use `curl` to update individual
files from the GitHub repository.

### Update all application files

```bash
BASE="https://raw.githubusercontent.com/mconsidine/efinder-combo/main"

curl -fsSL "$BASE/efinder/efinder_main.py"     -o /opt/efinder/efinder/efinder_main.py
curl -fsSL "$BASE/efinder/config.py"           -o /opt/efinder/efinder/config.py
curl -fsSL "$BASE/efinder/solver_proc.py"      -o /opt/efinder/efinder/solver_proc.py
curl -fsSL "$BASE/efinder/comms_proc.py"       -o /opt/efinder/efinder/comms_proc.py
curl -fsSL "$BASE/efinder/camera_proc.py"      -o /opt/efinder/efinder/camera_proc.py
curl -fsSL "$BASE/webui/app.py"                -o /opt/efinder/webui/app.py
curl -fsSL "$BASE/webui/templates/camera.html" -o /opt/efinder/webui/templates/camera.html
curl -fsSL "$BASE/webui/static/style.css"      -o /opt/efinder/webui/static/style.css

sudo systemctl restart efinder efinder-webui
```

### Verify a file is current

Grep for a string that only exists in the new version. For example, after
updating `camera.html`:

```bash
grep -c "binned_chk" /opt/efinder/webui/templates/camera.html   # should print 1
grep -c "stepper-row" /opt/efinder/webui/static/style.css        # should print 1+
grep -c "detect_use_binned" /opt/efinder/efinder/comms_proc.py   # should print 1+
```

### Update a systemd service file

```bash
BASE="https://raw.githubusercontent.com/mconsidine/efinder-combo/main"
curl -fsSL "$BASE/systemd/cedar-detect.service" \
     -o /etc/systemd/system/cedar-detect.service
sudo systemctl daemon-reload
sudo systemctl restart cedar-detect
```

### Pin to a specific branch or commit

Replace `main` with the branch name or full commit SHA:

```bash
BASE="https://raw.githubusercontent.com/mconsidine/efinder-combo/<branch-or-sha>"
```

---

## Architecture

### Process topology

```
┌─────────── efinder (systemd) ─────────────────────────────────────────────────┐
│                                                                                │
│  efinder_main (launcher, exits after spawning workers)                         │
│    imu_thread (daemon thread, reads BNO055 at 20 Hz, writes shared_cfg)        │
│                                                                                │
│   ┌──────────────┐    ┌──────────────┐    ┌──────────────────────────────┐    │
│   │ comms_proc   │    │ camera_proc  │    │ solver_proc                  │    │
│   │  CPU 1       │    │  CPU 3       │    │  CPU 2                       │    │
│   │              │    │              │    │                              │    │
│   │ LX200 :4060  │    │ picamera2    │    │ cedar-detect (gRPC) →        │    │
│   │ maint.sock   │◄──►│ → SHM bufs  │◄───│   extract centroids          │    │
│   │ align queues │    │ FrameSlots   │    │ tetra3rs (Rust, in-process)→ │    │
│   └──────┬───────┘    └──────────────┘    │   solve_from_centroids()     │    │
│          │                                │ OR tetra3 Python →           │    │
│          │                                │   solve_from_centroids()     │    │
│          │                                └──────────────────────────────┘    │
└──────────┼────────────────────────────────────────────────────────────────────┘
           │ maint.sock
    ┌──────▼───────┐          ┌─────────────────────────────┐
    │ efinder-webui│          │ cedar-detect.service (Rust) │
    │ Flask :80    │          │  CPU 2  gRPC :50051          │
    │ CPU 1 (float)│          │  reads SHM directly          │
    └──────────────┘          └─────────────────────────────┘
```

### Frame pipeline (tetra3rs backend, default)

```
picamera2 ISP hardware
  │  capture_array("main")  [exposure_s + ~10 ms]
  ▼
numpy Y-plane slice 960×760 uint8
  │  np.copyto → FrameSlot buf[idx]  [~0.5 ms, lock-free triple buffer]
  ▼
FrameSlots.publish(idx)
  │  solver_proc: acquire_read_slot()
  ▼
peak pixel check: buf.max() < 20? → DARK fast-path (~0.1 ms), skip
  │  otherwise:
  ▼
cedar-detect gRPC ExtractCentroids(shmem_name=...) [30–80 ms]
  │  cedar-detect reads SHM directly — no 730 KB payload over gRPC
  ▼
star_candidates[]  (centroid x/y in image coordinates)
  │  convert to center-relative coords for tetra3rs
  ▼
tetra3rs SolverDatabase.solve_from_centroids(
    centroids, fov_estimate,
    attitude_hint=last_quaternion,   ← blind on first frame
    hint_uncertainty_deg=0.1,
    solve_timeout_s=1.5)  [~10–60 ms seeded, ~300–800 ms blind]
  ▼
SolveResult → RA, Dec, roll, fov_rad
  │  latest_solution.update(...)  [Manager dict]
  ▼
comms_proc: serves RA/Dec on next :GR# / :GD# poll
```

### Solver backends

| | cedar backend | tetra3rs backend (default) |
|---|---|---|
| Extraction | cedar-detect gRPC | cedar-detect gRPC |
| Solve | tetra3 Python | tetra3rs Rust (in-process) |
| First-frame (blind) | 200–1200 ms | 300–800 ms |
| Seeded solve | N/A (always blind) | 10–60 ms |
| Hint from | — | last quaternion, 0.1° window |
| Runtime switch | dashboard toggle or `set_backend` maint cmd | same |

### Shared state

| Object | Type | Writers | Readers |
|---|---|---|---|
| `FrameSlots` | `shared_memory` (3 × 730 KB) | `camera_proc` | `solver_proc`, `cedar-detect` |
| `latest_solution` | `Manager().dict()` | `solver_proc` | `comms_proc`, web UI |
| `shared_cfg` | `Manager().dict()` | `comms_proc` (maint cmds), `solver_proc` (IMU ref), `imu_thread` | `solver_proc`, `comms_proc`, web UI |
| `camera_cmd_q` | `multiprocessing.Queue` | `comms_proc` | `camera_proc` |
| `solver_cmd_q` | `multiprocessing.Queue` | `comms_proc` | `solver_proc` |

**Runtime-mutable keys in `shared_cfg`:**

| Key | Description |
|---|---|
| `solver_backend` | `"tetra"` or `"cedar"` — which solve path to use |
| `test_mode` | `True` = serve static test image; `False` = live camera |
| `detect_sigma` | Extraction threshold, overrides config at runtime |
| `detect_use_binned` | Binned candidate search, overrides config at runtime |
| `solve_timeout_ms` | Solver budget, overrides config at runtime |
| `boresight_y`, `boresight_x` | Current boresight pixel offset |
| `imu_available` | BNO055 detected and responding |
| `imu_q`, `imu_t` | Latest quaternion and its timestamp |
| `imu_ref_*` | Reference solve for dead-reckoning prediction |
| `imu_calib_C` | Fitted 2×3 IMU→camera transform matrix |
| `imu_calib_quality` | R² of the current fit |

### Systemd units

| Unit | Description |
|---|---|
| `efinder.service` | Main daemon (camera + solver + comms). `Restart=always`. |
| `cedar-detect.service` | Rust gRPC centroid server. `After=` efinder. `CPUAffinity=2`. |
| `efinder-webui.service` | Flask web UI on port 80. Survives an efinder restart. |
| `efinder-firstboot.service` | Network setup — idempotent, runs every boot. |
| `efinder-ensure-ap.service` | 60 s watchdog that restores AP mode if NetworkManager suppressed it. |

---

## Diagnostic guide (SSH / PuTTY)

This section covers diagnosing problems when the eFinder is not solving,
crashing, or behaving unexpectedly. Connect via SSH (or PuTTY on Windows) and
work through the stages below.

### 1. Connect to the device

```
Host:     efinder.local  (or 10.55.0.1 via USB, or 10.42.0.1 via Wi-Fi AP)
Port:     22
Username: efinder
Password: 12345678
```

### 2. Check service health

```bash
sudo bash /opt/efinder/tests/diag_services.sh
```

This comprehensive script checks:
- `efinder` and `cedar-detect` systemd service state
- Port 50051 (cedar-detect gRPC listener)
- Maintenance socket `/run/efinder/maint.sock` — queries current status
- Shared memory frame buffers `/dev/shm/efinder_frame_0/1/2`
- tetra3rs database file presence and size
- tetra3 Python database import
- Python library versions
- efinder / cedar process list
- Active configuration
- Last 40 lines of journal for each service

Interpret the output:
- `PASS` (green) = OK
- `WARN` (yellow) = worth investigating but not necessarily fatal
- `FAIL` (red) = likely cause of the problem

### 3. Inspect live logs

Watch both services in real time:

```bash
sudo journalctl -fu efinder &
sudo journalctl -fu cedar-detect
```

Or look at recent history (last 100 lines):

```bash
sudo journalctl -u efinder -n 100 --no-pager
sudo journalctl -u cedar-detect -n 100 --no-pager
```

Key things to look for:

| Log message | Meaning |
|---|---|
| `Starting in LIVE MODE` | Correct default startup |
| `Starting in TEST MODE` | Daemon is using a static image, not the camera |
| `solver_backend -> tetra` | tetra3rs is active |
| `dark frame` | Frame too dim; check exposure/gain |
| `TOO_FEW centroids` | Not enough stars detected; lower sigma or increase exposure |
| `NO_MATCH` | Stars detected but no solve; check database, FOV estimate |
| `TIMEOUT` | Solve exceeded budget; increase `solve_timeout_ms` or use tetra3rs |
| `camera init failed` | picamera2 error; camera cable or reboot |
| `cedar-detect` gRPC errors | cedar-detect service not running |

### 4. Check the maint socket directly

Query live daemon state without the web UI:

```bash
python3 -c "
import socket, json
s = socket.socket(socket.AF_UNIX)
s.connect('/run/efinder/maint.sock')
s.sendall(b'{\"cmd\":\"status\",\"args\":{}}\n')
import sys; buf = b''
while b'\n' not in buf: buf += s.recv(4096)
print(json.dumps(json.loads(buf.split(b'\n')[0]), indent=2))
"
```

Key fields in the response:

| Field | Expected |
|---|---|
| `solver_backend` | `"tetra"` (default) or `"cedar"` |
| `test_mode` | `false` for live operation |
| `solved` | `true` when a valid solution exists |
| `stars` | Number of detected centroids |
| `solve_ms` | Last solve duration in ms |

### 5. Test cedar-detect extraction in isolation

```bash
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_detect.py
```

Options:
```bash
# Use a saved PNG instead of the live camera:
sudo .../diag_detect.py --image /var/lib/efinder/test.png

# Override sigma:
sudo .../diag_detect.py --sigma 7.0

# Sweep sigma 3–12 to find the best setting:
sudo .../diag_detect.py --sigma-sweep

# Override binning flag:
sudo .../diag_detect.py --binned   # force on
sudo .../diag_detect.py           # uses config default
```

The script runs through:
- Stage 0: config + library imports
- Stage 1: cedar-detect gRPC connectivity
- Stage 2: frame source (live SHM / test PNG / synthetic fallback)
- Stage 3: ExtractCentroids timing (N repetitions, median/min/max)
- Stage 4 (if `--sigma-sweep`): star count vs sigma table

Healthy output looks like:
```
  [PASS] Warm-up: 45ms  stars=23  peak=187  noise=3.42
  [PASS] [ 1]  43.2ms  stars=23  peak=187  noise=3.42
```

If you see `FAIL` at Stage 1, cedar-detect is not running:
```bash
sudo systemctl start cedar-detect
sudo systemctl status cedar-detect
```

### 6. Test the full solve pipeline

```bash
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_solve.py
```

Options:
```bash
sudo .../diag_solve.py --backend tetra    # tetra3rs (default)
sudo .../diag_solve.py --backend cedar    # Python tetra3
sudo .../diag_solve.py --image /var/lib/efinder/test.png
sudo .../diag_solve.py --reps 10
```

This tests the full pipeline: frame → cedar-detect → solve → result. Reports:
- Centroid count and extraction time
- Solve status (SOLVED / NO_MATCH / TIMEOUT / TOO_FEW)
- RA / Dec / roll / FOV when solved
- Timing breakdown per stage

### 7. Check test mode

A common startup problem: the daemon boots in test mode because `test.png`
exists on disk. Check:

```bash
# Look for the startup log message:
sudo journalctl -u efinder -n 20 | grep -E "LIVE|TEST"

# Or query via maint socket:
python3 -c "
import socket,json
s=socket.socket(socket.AF_UNIX); s.connect('/run/efinder/maint.sock')
s.sendall(b'{\"cmd\":\"status\",\"args\":{}}\n')
buf=b''
while b'\n' not in buf: buf+=s.recv(4096)
r=json.loads(buf.split(b'\n')[0])
print('test_mode:', r['result']['test_mode'])
"
```

To force live mode without restarting:
```bash
python3 -c "
import socket
s=socket.socket(socket.AF_UNIX); s.connect('/run/efinder/maint.sock')
s.sendall(b'{\"cmd\":\"set_test_mode\",\"args\":{\"enabled\":false}}\n')
"
```

Or use the web UI dashboard toggle.

### 8. Check shared memory buffers

```bash
ls -la /dev/shm/efinder_frame_*
```

Three files (`efinder_frame_0`, `1`, `2`) each ~730 KB means the daemon is
running. If absent, efinder has crashed or not started.

```bash
sudo systemctl status efinder
sudo systemctl start efinder
```

### 9. Check the tetra3rs database

```bash
ls -lh /var/lib/efinder/efinder-tetra-database.bin
```

Should be several hundred MB. If absent or wrong size:
```bash
sudo /usr/local/bin/efinder-update   # re-downloads the database
```

### 10. Force a backend switch without a web browser

```bash
# Switch to cedar backend:
python3 -c "
import socket
s=socket.socket(socket.AF_UNIX); s.connect('/run/efinder/maint.sock')
s.sendall(b'{\"cmd\":\"set_backend\",\"args\":{\"backend\":\"cedar\"}}\n')
"

# Switch back to tetra3rs:
python3 -c "
import socket
s=socket.socket(socket.AF_UNIX); s.connect('/run/efinder/maint.sock')
s.sendall(b'{\"cmd\":\"set_backend\",\"args\":{\"backend\":\"tetra\"}}\n')
"
```

### 11. Restart individual services

```bash
sudo systemctl restart efinder          # restarts camera + solver + comms
sudo systemctl restart cedar-detect     # restarts gRPC centroid server only
sudo systemctl restart efinder-webui    # restarts Flask UI only (solver keeps running)
```

The web UI (`efinder-webui`) runs independently of the solver. Restarting the
web UI does not interrupt active plate-solving. Restarting `efinder` does.

### 12. Common problems and fixes

| Symptom | Likely cause | Fix |
|---|---|---|
| Web UI shows stale layout after update | Browser cache | Hard-refresh (`Ctrl+Shift+R`) or open in incognito |
| Always in test mode on startup | `test.png` found at startup; daemon used to auto-detect | Use web UI toggle or set_test_mode maint cmd |
| `TOO_FEW` on every frame | Low star count — exposure too short or sigma too high | Lower sigma (try 6–7) or increase exposure; use `diag_detect.py --sigma-sweep` |
| `NO_MATCH` with plenty of stars | FOV estimate wrong or database mismatch | Reset calibration (`efinder-ctl calibration reset`), verify tetra3rs database |
| Solve time > 1.5 s constantly | tetra3rs not finding a seeded hint (first-frame or after solve gap) | Normal on first frame after startup; if persistent, check `solve_timeout_ms` |
| Cedar-detect FAIL on `diag_services.sh` | Service not running | `sudo systemctl start cedar-detect` |
| `tetra3 Python DB: FAIL` on `diag_services.sh` | False negative from old diagnostic | Verify with `python3 -c "import tetra3; print(tetra3.__file__)"` — if it prints a path ending in `__init__.py`, tetra3 is fine |
| `EACCES` on SHM in diagnostic scripts | Root-created SHM not readable by efinder user | Scripts now chmod 0o644 automatically; re-run after updating scripts |
| Port 50051 not listening | Cedar-detect crashed | `sudo systemctl restart cedar-detect` and check its journal |

---

## Benchmark and diagnostic scripts

All scripts require the venv Python and (for SHM access) root privileges:

```bash
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/<script>.py
```

### `diag_services.sh` — system health check

```bash
sudo bash /opt/efinder/tests/diag_services.sh
```

Full system health check: services, ports, SHM, databases, Python imports,
process list, journals. The first thing to run when something is wrong.

### `diag_detect.py` — extraction diagnostic

```bash
sudo .../diag_detect.py [--image PNG] [--sigma N] [--reps N] [--sigma-sweep] [--binned]
```

Tests cedar-detect centroid extraction in isolation with detailed timing.
Use `--sigma-sweep` to find the optimal sigma for your sky conditions.

### `diag_solve.py` — full pipeline diagnostic

```bash
sudo .../diag_solve.py [--backend tetra|cedar] [--image PNG] [--reps N]
```

Tests the full frame → extract → solve pipeline for both backends with
per-stage timing. Shows solve status, star count, and RA/Dec/FOV when solved.

### `bench_pipeline_combos.py` — four-combination benchmark

```bash
sudo .../bench_pipeline_combos.py [--image PNG] [--reps N] \
    [--sigma-sweep] [--hint-sweep] [--binned]
```

Benchmarks all four pipeline combinations:

| Combo | Extraction | Solve |
|---|---|---|
| 1 | cedar-detect gRPC | tetra3 Python |
| 2 | tetra3rs native | tetra3rs Rust |
| 3 | tetra3rs native | tetra3 Python |
| 4 | cedar-detect gRPC | tetra3rs Rust ← matches daemon tetra backend |

`--hint-sweep`: varies `hint_uncertainty_deg` from 5.0° down to 0.02° for
both `strict_hint=False` and `True` — shows where speed vs reliability
trade off for seeded solves.

`--sigma-sweep`: compares cedar-detect vs tetra3rs-native star yield across
sigma 3–12 — useful for choosing the best sigma for your setup.

---

## Performance characteristics

### Solve pipeline timing (Pi Zero 2W, 960×760, clear sky, tetra3rs backend)

| Stage | Time |
|---|---|
| Camera capture (ISP hardware) | `exposure_s` + ~10 ms |
| Frame copy to SHM | ~0.5 ms |
| Cedar-detect extraction (gRPC, binned) | 25–60 ms |
| tetra3rs solve, seeded (0.1° hint) | 10–60 ms |
| tetra3rs solve, blind (first frame) | 300–800 ms |
| LX200 report latency | < 1 ms |
| IMU dead-reckoning (when active) | < 0.1 ms |
| **Typical end-to-end (after first frame)** | **~0.5–1.0 s with 0.2 s exposure** |

### RAM usage

| Process | Typical RSS |
|---|---|
| `camera_proc` | ~60 MB |
| `solver_proc` (tetra3rs loaded) | ~180 MB |
| `comms_proc` | ~25 MB |
| `efinder-webui` (Flask) | ~35 MB |
| `cedar-detect-server` | ~15 MB |
| **Total** | **~315 MB** |

With zram swap (~256 MB LZ4), the system operates comfortably within the
Zero 2W's 512 MB physical RAM. Verify zram is active:

```bash
swapon --show   # should show /dev/zram0
```

---

## Building and development

### Building the SD card image

```bash
sudo apt-get install -y qemu-user-static binfmt-support
sudo EFINDER_VERSION=dev bash build/build-image.sh
# Output: build/output/efinder.img
```

### Cross-compiling cedar-detect-server

```bash
git submodule update --init --recursive
rustup target add aarch64-unknown-linux-gnu
sudo apt-get install -y gcc-aarch64-linux-gnu protobuf-compiler

cd cedar-detect
CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc \
RUSTFLAGS="-C target-cpu=cortex-a53" \
cargo build --release --target aarch64-unknown-linux-gnu --bin cedar-detect-server

aarch64-linux-gnu-strip target/aarch64-unknown-linux-gnu/release/cedar-detect-server
scp target/.../cedar-detect-server efinder@efinder.local:/usr/local/bin/
ssh efinder@efinder.local "sudo systemctl restart cedar-detect"
```

### Running the web UI in development

```bash
pip install -r requirements.txt
EFINDER_MAINT_SOCK=/tmp/efinder-dev.sock python webui/app.py
```

### Static analysis

```bash
bash build/check-tree.sh
```

---

## Known limitations and deferred work

- **Dark frame / hot pixel calibration**: maintenance socket hooks exist;
  `camera_proc.py` capture logic not yet written.
- **Auto-exposure**: solver knows star count per frame; feedback loop to
  camera not wired up.
- **Frame save for diagnostics**: `save_failed_frames: true` in config but
  write logic in `solver_proc.py` not implemented.
- **Watchdog for solver hang**: systemd restarts on crash but not on hang.
  A heartbeat monitor on `latest_solution.epoch_monotonic` is planned.
- **No authentication**: LX200 server and web UI are open to any device on
  the same network. Do not expose to the public internet.
- **Single boresight offset**: one calibration for all eyepieces.
- **Polar alignment assumes pure RA motion**: accidental dec movement
  invalidates the result silently.
