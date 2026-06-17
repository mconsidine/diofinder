# eFinder — olive branch (Pi Zero 2W)

Plate-solving electronic finder for amateur telescopes. Runs on a Raspberry Pi
Zero 2W with an Arducam 12 MP IMX477. Reports live pointing to SkySafari (or
any LX200-speaking application) over Wi-Fi or USB tether, with sub-arcsecond
boresight registration and a built-in three-point polar alignment assistant.

**Solver: olive-solve** (Rust tetra3-py, fully in-process — no external daemon or gRPC server)

**Extraction:** sycamore `star_detect.detect_stars` with matched-filter gate + olive-solve (tetra3) solver

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
9. [Seeing presets](#seeing-presets)
10. [Dark-frame / hot-pixel capture](#dark-frame--hot-pixel-capture)
11. [Solver-hang watchdog](#solver-hang-watchdog)
12. [Focus assessment](#focus-assessment)
13. [IMU dead-reckoning (optional)](#imu-dead-reckoning-optional)
14. [Wi-Fi modes](#wi-fi-modes)
15. [Configuration reference](#configuration-reference)
16. [Maintenance CLI (`efinder-ctl`)](#maintenance-cli-efinder-ctl)
17. [Updating (OTA)](#updating-ota)
18. [Architecture](#architecture)
19. [Diagnostic guide (SSH / PuTTY)](#diagnostic-guide-ssh--putty)
    - [Can't solve even though stars are visible](#cant-solve-even-though-stars-are-visible)
20. [Benchmark and diagnostic scripts](#benchmark-and-diagnostic-scripts)
21. [Performance characteristics](#performance-characteristics)
22. [Building and development](#building-and-development)
23. [Known limitations and deferred work](#known-limitations-and-deferred-work)

---

## What it does

The eFinder turns a Raspberry Pi Zero 2W into a self-contained plate-solving
"sky compass" that mounts to your telescope tube. Point the scope at any part
of the sky. Within a second or two the eFinder has identified the star field,
calculated the telescope's precise RA/Dec, and is streaming that position to
SkySafari in real time via the LX200 protocol.

**Core capabilities:**

- **Continuous plate-solving** at ~1–2 s per frame (960×760). Each solve
  reports RA, Dec, field-of-view, and image orientation (roll).
- **In-process solver**: olive-solve's Rust tetra3-py wheel runs entirely inside
  `solver_proc` — no external service, no gRPC, no open ports.
- **sycamore matched-filter extraction + olive-solve (tetra3) solver**: fixed pipeline with
  matched-filter gate for robust star detection and in-process tetra3-py plate solving.
- **Boresight calibration** via SkySafari's Sync command: centre a star, tap
  Sync, and the eFinder stores the pixel offset. Persisted across reboots.
- **FOV self-calibration**: after ~30 successful solves the eFinder commits
  the median field-of-view and uses a tighter search window, improving speed
  and reliability.
- **Polar alignment assistant**: rotate the mount in RA only at three positions.
  The eFinder fits a great circle through the three plate-solve results, derives
  the true RA axis, and reports azimuth and altitude corrections.
- **IMU dead-reckoning** (optional, BNO055): smooths SkySafari position updates
  between solves to < 1 ms latency. Self-calibrating. Hot-pluggable.
- **Live web UI** on port 80: dashboard, camera controls, solver parameters,
  polar alignment, Wi-Fi switching, configuration viewer, live log tail, debug
  ZIP download.
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

1. Download `efinder-sycamore-YYYYMMDD-vX.Y.Z.img.xz` from the
   [Releases](../../releases) page.
2. Flash with **Raspberry Pi Imager** (choose "Use custom image"), **balena
   Etcher**, or `dd`:
   ```bash
   xz -d efinder-sycamore-YYYYMMDD-vX.Y.Z.img.xz
   sudo dd if=efinder-sycamore-YYYYMMDD-vX.Y.Z.img bs=4M status=progress oflag=sync of=/dev/sdX
   ```
3. Insert and boot. First boot takes 30–90 s; the green LED steadies when the
   eFinder application has started.

### Install on an existing Pi OS Trixie Lite card

```bash
git clone https://github.com/mconsidine/diofinder
cd diofinder
sudo bash scripts/install.sh
```

`install.sh` is idempotent — safe to re-run after updates. It installs system
packages, Python dependencies, the olive-solve tetra3-py wheel, and all
systemd unit files. Reboot after the first run.

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

> **USB serial device path differs by OS.** When tethered over USB, the Pi's
> serial (CDC-ACM) interface enumerates as **`/dev/ttyACM0`** on Linux but as
> **`/dev/tty.usbmodem*`** on macOS (e.g. `/dev/tty.usbmodem14201` — the digits
> vary by port/session). On a Mac, use the `tty.usbmodem*` path in any serial
> client (`screen /dev/tty.usbmodem14201 115200`, `minicom`, etc.); there is no
> `/dev/ttyACM0` on macOS. Run `ls /dev/tty.usbmodem*` to find the current name.

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
| `:CM#` | Sync / boresight calibration | `M31 EX GAL MAG 3.5 SZ178.0'#` |
| `:St dd*mm#` | Set observer latitude | `1` |
| `:Sg ddd*mm#` | Set observer longitude | `1` |
| `:Gt#` | Get latitude | `sDD*MM#` |
| `:Gg#` | Get longitude | `sDDD*MM#` |
| `:SL HH:MM:SS#` | Set local time | `1` |
| `:SC MM/DD/YY#` | Set date (syncs system clock) | `1Updating Planetary Data#` |
| `:MS#` | Move to target (ignored) | `0` |
| `:Q#` | Stop (ignored) | _(empty)_ |
| `:GVP#` | Product name | `eFinder <version>#` |
| `:GVN#` | Firmware version | `eFinder <version>#` |

---

## Web UI

Open `http://efinder.local` from any browser on the same network.

### Dashboard (`/`)

Auto-refreshes every 1.5 s. Shows:

- **Pointing**: RA/Dec, star count, match count, solve time, peak pixel, FOV,
  roll. Status badge: SOLVED / TOO_FEW / NO_MATCH / TIMEOUT / DARK.
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
  Refreshes every 2 s. The display uses an **arcsinh sky-subtracted stretch**:
  the sky median is subtracted, the residual is passed through `arcsinh(x/β)`
  (linear for faint signals, logarithmic for bright ones), and scaled to the
  99.9th percentile. This is cosmetic only — the solver reads raw bytes.
- **Exposure (seconds)**: log-scale slider + numeric box + `−`/`+` buttons
  (±0.05 s per click). Valid range: 0.001–10 s.
- **Gain (1–64)**: slider + numeric box + `−`/`+` buttons (±1 per click).
- **Detection sigma**: star extraction threshold. Slider + numeric box +
  `−`/`+` buttons. Default 5. Lower finds fainter stars;
  higher rejects noise. Valid range: 0–20. **This is the first thing to adjust
  if the solver is not finding enough stars** — see
  [Can't solve even though stars are visible](#cant-solve-even-though-stars-are-visible).
- **Matched-filter kernel sigma**, **trail rejection (max axis ratio)**, and
  **match radius/threshold** sliders are also exposed (sycamore ≥ 0.12 features).
- **Solve timeout (ms)**: maximum time per frame. Slider + numeric box.
  Default 1500 ms.

All controls auto-apply on change. Use **Persist** checkbox + **Apply & save**
button to write values to `/etc/efinder/efinder.conf`.

### Polar alignment (`/polar`)

Step-by-step three-point polar alignment workflow. See [Polar alignment](#polar-alignment).

### Focus (`/focus`)

Live focus assistant: real-time Laplacian variance score and a 4× zoomed crop
of the brightest star. Use the **Commit** button to record the peak score as a
dashboard reference.

### Wi-Fi (`/wifi`)

Browser-based Wi-Fi management. See [Wi-Fi modes](#wi-fi-modes).

### Configuration (`/config`)

Structured read-only view of `/etc/efinder/efinder.conf`, grouped into sections
(Camera, Optics/FOV, Star Detection, Plate Solving, Boresight, Observer Location,
Communications, CPU Affinity, Diagnostics, Shutdown). Each entry shows the
current value, a one-line description, and an **edited** badge with the default
value wherever the file differs from the compiled-in default. A Runtime Status
block at the top shows live solver backend, camera mode, IMU state, and FOV from
the daemon socket.

### Logs (`/logs`)

Live `journalctl` tail for `efinder.service`.

### Update (`/update`)

One-click `efinder-update`. Refreshes Python dependencies and the olive-solve
(`tetra3`) and sycamore (`star_detect`) wheels from their latest GitHub releases
(a wheel already in `vendor/wheels/` overrides the download), then restarts the
service. Does not touch `efinder.conf`.

### Health endpoint (`/healthz`)

Returns HTTP 200 `ok` if the daemon is reachable, else HTTP 503.

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

## Seeing presets

A single **Good / Bad** toggle re-tunes the whole detection + solve pipeline
for the night's conditions. It lives on the **Status** page and the **Config**
page, and on the CLI as `efinder-ctl seeing {get|set good|set bad}`.

| Key | Good | Bad | Rationale |
|---|---|---|---|
| `detect_sigma` | 5.0 | 4.0 | Lower threshold in bad seeing to keep faint-but-real stars. |
| `detect_kernel_sigma` | 1.5 | 2.5 | Wider matched-filter kernel matches a bloated/smeared PSF. |
| `detect_bg_mode` | `row_percentile` | `block_percentile` | 2-D block background handles uneven sky-glow / gradients. |
| `detect_max_axis_ratio` | 3.0 | 5.0 | Looser trail rejection: bad seeing elongates real stars. |
| `min_centroids` | 8 | 5 | Accept a sparser field rather than never solving. |
| `match_radius` | 0.01 | 0.015 | More catalog-match slack for distorted/blurred centroids. |
| `match_threshold` | 1e-5 | 1e-5 | False-positive ceiling unchanged. |
| `solve_timeout_ms` | 1500 | 3000 | A harder field deserves a longer solve budget. |
| `auto_exposure_target_stars` | 20 | 15 | Fewer stars expected; don't over-expose chasing them. |
| `auto_exposure_max_s` | 0.5 | 1.0 | Allow longer exposures to reach faint stars. |
| `star_db` | standard | deep | Use a deeper-magnitude catalog if one is configured. |

Applying a preset writes every key live (and persists it to `efinder.conf`).
You can still fine-tune any individual value afterward on the Camera page; the
Config page shows a **drift** note listing keys you have overridden since.

### Deeper database for the Bad preset

The Bad preset asks for `star_db="deep"`. This only takes effect if you set
`star_db_deep` in `efinder.conf` to a database that **exists on disk** —
otherwise the preset stays on the standard `solver_db`, so a missing catalog
never breaks solving. Build/download a deeper-magnitude tetra3 `.npz` (see the
`astro_databases` release pattern) and point at it:

```
star_db_deep: deep_database        # -> /var/lib/efinder/deep_database.npz
# or an absolute path:
# star_db_deep: /var/lib/efinder/gaia_mag9.npz
```

The switch is done in-process by the solver (no second database is held in
memory); it reloads `tetra3.Tetra3` on the live solver when the preset changes.

## Dark-frame / hot-pixel capture

Hot (always-bright) pixels masquerade as stars, especially during slews when
the temporal background cache is offline. Build a hot-pixel mask once per rig:

1. **Cap the lens.**
2. On the **Camera** page click **Capture dark frame** (or
   `efinder-ctl raw '{"cmd":"dark_capture","args":{"frames":16}}'`).

The solver median-stacks the dark frames, flags pixels above
`median + 5·(1.4826·MAD)`, saves the mask to
`/var/lib/efinder/hot_pixel_mask.npz`, and loads it immediately. From then on
each frame has its masked pixels replaced by the mean of their 8 neighbors
before detection (sub-millisecond, vectorized). The mask is reloaded on every
service start. Clear it from the Camera page or `hot_pixel_clear`.

## Solver-hang watchdog

A watchdog thread in `comms_proc` confirms the solver keeps publishing
solutions (it publishes on every frame, even dark ones). If nothing new
appears for `watchdog_timeout_s` (default 30 s) the solver is treated as hung:
the process exits with a CRITICAL log line and systemd restarts the unit.
Disable with `watchdog_enabled: false`. The watchdog only arms after the first
solution is published, so a slow first boot never trips it.

## Focus assessment

The focus page shows a **focus score** (Laplacian variance) from the brightest
detected star. Higher = sharper. Typical values:

| Condition | Score |
|---|---|
| Severely out of focus | < 100 |
| Reasonable | 500–2000 |
| Well focused, good seeing | > 2000 |

Adjust focus until the score peaks, then click **Commit** to save as the
reference on the dashboard.

---

## IMU dead-reckoning (optional)

A BNO055 on the I²C bus provides two complementary benefits:

1. **Attitude hint propagation** — after every successful solve the solver
   records the IMU quaternion alongside the sky quaternion. On the next frame
   it computes the IMU rotation delta and applies it to the sky quaternion,
   giving the solver an up-to-date attitude hint even when the scope has moved
   between frames. The search window scales with the measured motion (1.5× the
   rotation angle, minimum 2°) so slews of any size still produce a useful hint.
   Logs show `(imu)` / `(seeded)` / `(blind)` per solve so the hint source is
   visible in the journal.

2. **SkySafari smoothing** — self-calibrates passively from consecutive solve
   pairs (IMU rotation vector vs. sky displacement). Active once 3 pairs are
   collected and R² ≥ 0.85; smooths SkySafari `:GR#`/`:GD#` responses to
   < 1 ms latency between solves.

Hot-pluggable: if the sensor disappears the daemon degrades gracefully.
The I²C bus runs at 50 kHz to avoid the BCM2835 clock-stretching hardware
bug that causes corrupt reads at the standard 100 kHz rate.

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
| `sensor_full_width` | `4056` | Full IMX477 sensor width — forces libcamera full-array readout |
| `sensor_full_height` | `3040` | Full IMX477 sensor height — forces libcamera full-array readout |
| `exposure_s` | `0.2` | Exposure time in seconds (0.001–10.0) |
| `gain` | `5.0` | Analogue gain (1.0–64.0) |
| `auto_exposure_enabled` | `true` | Adaptively adjust exposure to reach `auto_exposure_target_stars`. Defaults ON. |
| `auto_exposure_target_stars` | `20` | Desired star count when auto-exposure is active. Live-mutable (seeing presets). |
| `auto_exposure_min_s` | `0.05` | Minimum exposure when auto-exposure is active |
| `auto_exposure_max_s` | `1.0` | Maximum exposure when auto-exposure is active. Live-mutable (seeing presets). |
| `fov_deg` | `13.5` | Initial FOV estimate in degrees. Self-calibrates after ~30 solves. |
| `arcsec_per_pixel` | `51.15` | Plate scale in arcsec/px (display only; solver uses fov_deg). |
| `distortion` | `0.0` | Barrel/pincushion coefficient. 0 = fit per-solve. |
| `detect_sigma` | `5.0` | Extraction threshold (σ above background). Lower finds fainter stars; raise to reject noise. |
| `detect_bin` | `2` | Detection binning: 2 = 2×2-binned (faster, centroids stay full-res), 1 = full-res. Restart to apply. |
| `detect_bg_mode` | `row_percentile` | Per-frame background mode (row_percentile, line_median, top_hat, block_percentile, column_percentile, row_column_percentile, uniform_mean). |
| `detect_kernel_sigma` | `1.5` | Matched-filter kernel width (px), 1.0–4.0. Widen for bloated PSFs. sycamore ≥ 0.12. |
| `detect_max_axis_ratio` | `0.0` | Trail/elongation rejection. 0 = off, else 1.5–10.0. Full 2-D moments (sycamore ≥ 0.12). |
| `detect_local_noise` | `true` | Per-window local noise in the matched filter. sycamore ≥ 0.12. |
| `detect_tophat_radius` | `12` | Structuring-element radius (px) for top_hat mode. |
| `solver_db` | `default_database` | tetra3 `.npz` database name (relative to `/var/lib/efinder/` or absolute path). |
| `star_db_deep` | _(empty)_ | Optional deeper-magnitude db for the "bad" seeing preset; used only if the file exists. |
| `seeing_mode` | `good` | Active Good/Bad seeing preset (see [Seeing presets](#seeing-presets)). |
| `min_centroids` | `8` | Minimum detected stars required to attempt a solve. |
| `max_solve_stars` | `50` | Cap on centroids passed to the solver (performance guard). |
| `solve_timeout_ms` | `1500` | Per-frame solver budget in ms. |
| `match_threshold` | `1e-5` | Max false-positive probability for accepting a match. |
| `match_radius` | `0.01` | Centroid-to-catalog match radius as fraction of FOV. |
| `fov_max_error_deg` | `1.0` | FOV tolerance window before calibration. |
| `fov_calibrated_max_error_deg` | `0.1` | FOV tolerance window after calibration. |
| `fov_calibrated_stddev` | `0.05` | Stddev threshold (°) for declaring FOV stable. |
| `bg_cache_enabled` | `true` | Temporal background cache (median-stack recent frames → per-row model). |
| `boresight_y` | `380` | Boresight Y in pixels. Set by `:CM#` sync. |
| `boresight_x` | `480` | Boresight X in pixels. Set by `:CM#` sync. |
| `lx200_port` | `4060` | TCP port for the LX200 server. |
| `lx200_client_timeout_s` | `30.0` | Disconnect idle LX200 clients. |
| `latitude_deg` | `0.0` | Observer latitude. Auto-populated from SkySafari `:St` on connect. |
| `longitude_deg` | `0.0` | Observer longitude. Auto-populated from SkySafari `:Sg` on connect. |
| `cpu_camera` | `3` | CPU affinity for camera_proc. Also a secondary core for olive-solve's rayon thread pool. |
| `cpu_solver` | `2` | Primary CPU affinity for solver_proc. |
| `cpu_solver_aux` | `1` | Auxiliary solver core (third rayon core for star extraction). |
| `cpu_comms` | `0` | CPU affinity for comms_proc and web UI (shares CPU 0 with the kernel). |
| `watchdog_enabled` | `true` | Restart the service if the solver stops publishing solutions. |
| `watchdog_timeout_s` | `30.0` | Staleness (s) before the solver is declared hung. |
| `save_failed_frames` | `false` | Save PNG for every failed solve to `failed_frames_dir`. |
| `save_solved_frames` | `false` | Save PNG for every successful solve to `failed_frames_dir`. |
| `failed_frames_dir` | `/var/lib/efinder/captures` | Directory for saved frame PNGs. |
| `log_solve_stats_every_n` | `50` | Print solve performance stats every N solves. |

### CPU affinity layout

| CPU | Role |
|---|---|
| 0 | Linux kernel, IRQs, sshd, NetworkManager · `comms_proc` (LX200 + maint socket) · `efinder-webui` (Flask) · IMU thread (20 Hz I²C) — all I/O-bound, share CPU 0 with the kernel |
| 1 | `solver_proc` auxiliary core (third rayon core for star extraction) |
| 2 | `solver_proc` primary — olive-solve tetra3-py in-process |
| 3 | `camera_proc` (ISP DMA + SHM copy) · `solver_proc` rayon secondary thread pool |

The solver pins itself to CPUs 1+2+3 (`{cpu_solver, cpu_camera, cpu_solver_aux}`)
and runs sycamore's thread pool with 3 threads; comms/webui share CPU 0 with the
kernel because both are I/O-bound.

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

# Polar alignment
efinder-ctl polar start
efinder-ctl polar status
efinder-ctl polar cancel
efinder-ctl polar set-latitude 44.5

# Seeing preset (Good / Bad one-tap tuning)
efinder-ctl seeing get
efinder-ctl seeing set good
efinder-ctl seeing set bad

# Solver parameters (no dedicated subcommand — use raw, or the Camera page)
efinder-ctl raw '{"cmd":"solver_params_get","args":{}}'
efinder-ctl raw '{"cmd":"solver_params_set","args":{"detect_sigma":4.0,"persist":true}}'

# Test / live mode
efinder-ctl raw '{"cmd":"set_test_mode","args":{"enabled":false}}'

# Raw JSON (any command)
efinder-ctl raw '{"cmd":"ping","args":{}}'
```

---

## Updating (OTA)

Release images are git-provisioned: `install.sh` makes `/opt/efinder` a real
git clone of this repo (with the build ref checked out), so over-the-air
updates work on imaged devices. `efinder-update` is the supported path — it
fetches the target ref, refreshes Python deps and the olive-solve / sycamore
wheels from their latest GitHub releases, records the version, and restarts
both services.

```bash
sudo /usr/local/bin/efinder-update            # latest tag
sudo /usr/local/bin/efinder-update v0.0.25    # a specific tag/commit
sudo /usr/local/bin/efinder-update --ref olive  # track a branch (git pull --ff-only)
```

The web UI **Update** page wraps the same script. To refresh only the star
database, use `efinder-db-update` (see below).

If `/opt/efinder` is not a git repository (e.g. a hand-copied install where the
git graft failed), re-image with the latest release rather than patching files
in place.

---

## Architecture

### Process topology

```
┌─────────── efinder (systemd) ─────────────────────────────────────────────────┐
│                                                                                │
│  efinder_main (launcher, exits after spawning workers)                         │
│    imu_thread (daemon thread, reads BNO055 at 20 Hz, writes shared_cfg)        │
│                                                                                │
│   ┌──────────────┐    ┌──────────────┐    ┌──────────────────────────────────┐ │
│   │ comms_proc   │    │ camera_proc  │    │ solver_proc                      │ │
│   │  CPU 0       │    │  CPU 3       │    │  CPUs 1 + 2 + 3 (rayon)         │ │
│   │              │    │              │    │                                  │ │
│   │ LX200 :4060  │    │ picamera2    │    │ sycamore star_detect             │ │
│   │ maint.sock   │◄──►│ → SHM bufs  │◄───│   + olive-solve (in-process)     │ │
│   │ align queues │    │ FrameSlots   │    │   solve_from_centroids           │ │
│   └──────┬───────┘    └──────────────┘    │                                  │ │
│          │                                └──────────────────────────────────┘ │
└──────────┼────────────────────────────────────────────────────────────────────┘
           │ maint.sock
    ┌──────▼───────┐
    │ efinder-webui│
    │ Flask :80    │
    │ CPU 0        │
    └──────────────┘
```

### Frame pipeline

```
picamera2 ISP hardware
  │  capture_array("main")  [exposure_s + ~10 ms]
  │  sensor={"output_size":(4056,3040)} → full array, 4× downscale to 960×760
  ▼
numpy Y-plane slice 960×760 uint8
  │  np.copyto → FrameSlot buf[idx]  [~0.5 ms, lock-free triple buffer]
  ▼
FrameSlots.publish(idx)
  │  solver_proc: acquire_read_slot()       ← raw bytes, no display stretch
  │  webui /camera frame_jpg():             ← display path (arcsinh stretch, cosmetic)
  ▼
peak pixel check: buf.max() < 20? → DARK fast-path (~0.1 ms), skip
  │  otherwise:
  ▼
star_detect.detect_stars(frame_u8, sigma)  [sycamore matched-filter, ~10–60 ms]
  ▼
star_candidates[]  (centroid x/y in image coordinates)
  │  convert to centre-relative float64 coords
  ▼
_imu_propagate_hint(last_sky_q, last_solve_imu_q, shared_cfg)
  │  q_delta = q_imu_now ⊗ conj(q_imu_at_last_solve)
  │  q_hint  = q_delta ⊗ q_last_sky
  │  uncertainty = max(2°, 1.5 × rotation_angle)
  ▼
solve_from_centroids(
    centroids, image_size, fov_estimate,
    attitude_hint=q_hint,
    hint_uncertainty_deg=uncertainty,
    solve_timeout_ms=1500)  [~10 ms seeded, ~300–800 ms blind]
  ▼
SolveResult → RA, Dec, roll, fov_rad
  │  latest_solution.update(...)  [Manager dict]
  ▼
comms_proc: serves RA/Dec on next :GR# / :GD# poll
```

### Extraction

Star extraction uses **sycamore** (`star_detect.detect_stars`) with a matched-filter gate.
Default sigma: 5. Speed: ~10–60 ms. Installed from `vendor/wheels/star_detect-*.whl`.

### Shared state

| Object | Type | Writers | Readers |
|---|---|---|---|
| `FrameSlots` | `shared_memory` (3 × 730 KB) | `camera_proc` | `solver_proc`, webui |
| `latest_solution` | `Manager().dict()` | `solver_proc` | `comms_proc`, web UI |
| `shared_cfg` | `Manager().dict()` | `comms_proc` (maint cmds), `solver_proc` (IMU ref), `imu_thread` | `solver_proc`, `comms_proc`, web UI |
| `camera_cmd_q` | `multiprocessing.Queue` | `comms_proc` | `camera_proc` |
| `solver_cmd_q` | `multiprocessing.Queue` | `comms_proc` | `solver_proc` |

**Runtime-mutable keys in `shared_cfg`:**

| Key | Description |
|---|---|
| `test_mode` | `True` = serve static test image; `False` = live camera |
| `detect_sigma` | Extraction threshold, overrides config at runtime |
| `detect_bg_mode`, `detect_kernel_sigma`, `detect_max_axis_ratio`, `detect_local_noise`, `detect_tophat_radius`, `detect_bg_block_size`, `detect_uniform_filter_size`, `detect_noise_mode`, `min_centroids` | Detection params, all live-mutable via `solver_params_set` / seeing presets |
| `match_radius`, `match_threshold` | Match params, live-mutable via `match_params_set` / seeing presets |
| `solve_timeout_ms` | Solver budget, overrides config at runtime |
| `seeing_mode` | Active Good/Bad preset |
| `auto_exposure_enabled`, `auto_exposure_target_stars`, `auto_exposure_max_s` | Auto-exposure controller settings (seeing presets) |
| `boresight_y`, `boresight_x` | Current boresight pixel offset |
| `imu_available` | BNO055 detected and responding |
| `imu_q`, `imu_t` | Latest quaternion and its monotonic timestamp |
| `imu_ref_q/ra/dec/roll/t` | Sky-solve reference for SkySafari dead-reckoning |
| `imu_calib_C` | Fitted 2×3 IMU→sky-displacement matrix |
| `imu_calib_n` | Number of calibration pairs collected |
| `imu_calib_quality` | R² of the current fit (≥ 0.85 to activate smoothing) |
| `fov_deg` | Current calibrated FOV (updated post-solve) |

### Systemd units

| Unit | Description |
|---|---|
| `efinder.service` | Main daemon (camera + solver + comms). `Restart=always`. |
| `efinder-webui.service` | Flask web UI on port 80. Survives an efinder restart. |
| `efinder-firstboot.service` | Network setup — idempotent, runs every boot. |
| `efinder-ensure-ap.service` | 60 s watchdog that restores AP mode if NetworkManager suppressed it. |
| `efinder-usb-gadget.service` | USB Ethernet gadget setup. |

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
- `efinder` systemd service state
- Maintenance socket `/run/efinder/maint.sock` — queries current status
- Shared memory frame buffers `/dev/shm/efinder_frame_0/1/2`
- Solver database file presence and a live load test
- Python library imports (`numpy`, `tetra3`, `picamera2`, `PIL`)
- Process list (efinder procs)
- Active configuration
- Last 50 lines of the efinder journal

Interpret the output:
- `PASS` (green) = OK
- `WARN` (yellow) = worth investigating but not necessarily fatal
- `FAIL` (red) = likely cause of the problem

### 3. Inspect live logs

```bash
sudo journalctl -fu efinder
```

Or look at recent history:

```bash
sudo journalctl -u efinder -n 100 --no-pager
```

Key things to look for:

| Log message | Meaning |
|---|---|
| `Starting in LIVE MODE` | Correct default startup |
| `Starting in TEST MODE` | Daemon is using a static image, not the camera |
| `dark frame` | Frame too dim; check exposure/gain |
| `TOO_FEW centroids` | Not enough stars detected; lower sigma or increase exposure |
| `NO_MATCH` | Stars detected but no solve; check database, FOV estimate |
| `TIMEOUT` | Solve exceeded budget; increase `solve_timeout_ms` |
| `camera init failed` | picamera2 error; check camera cable or reboot |
| `Sensor mode: output_size=(4056, 3040)` | Full-sensor readout confirmed (expected) |

### 4. Check the maint socket directly

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
| `solver_backend` | `"sycamore"` (fixed) |
| `test_mode` | `false` for live operation |
| `solved` | `true` when a valid solution exists |
| `stars` | Number of detected centroids |
| `solve_ms` | Last solve duration in ms |

### 5. Test extraction in isolation

```bash
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_detect.py
```

Options:
```bash
# Use a saved PNG instead of the live SHM:
sudo .../diag_detect.py --image /var/lib/efinder/test.png

# Override sigma:
sudo .../diag_detect.py --sigma 7.0

# Sweep sigma 3–12 to find the best setting:
sudo .../diag_detect.py --sigma-sweep
```

The script runs sycamore centroid extraction with detailed timing.
Use `--sigma-sweep` to find the optimal sigma for your sky conditions.

### 6. Test the full solve pipeline

```bash
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_solve.py --live-shm
```

Options:
```bash
sudo .../diag_solve.py --image /var/lib/efinder/test.png
sudo .../diag_solve.py --reps 5
sudo .../diag_solve.py --sigma 7.0 --timeout 2000
```

Tests blind + hint paths using sycamore extraction and olive-solve. Reports centroid count,
extraction time, solve status (SOLVED / NO_MATCH / TIMEOUT / TOO_FEW), RA/Dec/roll/FOV,
and timing breakdown per stage.

### 7. Check test mode

```bash
sudo journalctl -u efinder -n 20 | grep -E "LIVE|TEST"
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

### 9. Check the solver database

```bash
ls -lh /var/lib/efinder/default_database.npz
```

Should be present (typically a few hundred MB). If absent, fetch it from the
`astro_databases` release (SHA-256 verified):

```bash
sudo /usr/local/bin/efinder-db-update   # downloads cedar_solve_13deg.npz
```

### 10. Query or set runtime parameters without a web browser

```bash
# Get current solver parameters:
python3 -c "
import socket
s=socket.socket(socket.AF_UNIX); s.connect('/run/efinder/maint.sock')
s.sendall(b'{\"cmd\":\"solver_params_get\",\"args\":{}}\n')
print(s.recv(4096).decode())
"
```

### 11. Restart individual services

```bash
sudo systemctl restart efinder          # restarts camera + solver + comms
sudo systemctl restart efinder-webui    # restarts Flask UI only (solver keeps running)
```

The web UI (`efinder-webui`) runs independently of the solver. Restarting the
web UI does not interrupt active plate-solving.

### 12. Common problems and fixes

| Symptom | Likely cause | Fix |
|---|---|---|
| Live view looks washed out / all white | Sky background very bright (long exposure / high gain at twilight) | Reduce exposure or gain; the arcsinh stretch is display-only |
| Live view very dark / stars invisible | Heavy underexposure | Increase exposure or gain on the Camera page |
| Web UI shows stale layout after update | Browser cache | Hard-refresh (`Ctrl+Shift+R`) or open in incognito |
| Always in test mode on startup | `test.png` found at startup | Use web UI toggle or `set_test_mode` maint cmd |
| `TOO_FEW` on every frame | Low star count — exposure too short or sigma too high | Lower sigma (try 3–4) or increase exposure; use `diag_detect.py --sigma-sweep` |
| `NO_MATCH` with plenty of stars | FOV estimate wrong or database mismatch | Reset calibration (`efinder-ctl calibration reset`), verify database |
| Solve time > 1.5 s constantly | Blind solve on first frame or after a solve gap | Normal on first frame; if persistent, check `solve_timeout_ms` |
| `Sensor mode: output_size=(1332, 990)` in journal | `sensor=` hint not applied | Ensure camera_proc.py is up to date; restart efinder |

### 13. Can't solve even though stars are visible

This is the most common field problem. Work through these steps in order.

#### Step 1 — check what the solver is actually seeing

The stars you see through the eyepiece or in the arcsinh-stretched live view
are not necessarily the stars sycamore is extracting. Run the sigma sweep
to see the raw star count at each threshold:

```bash
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_detect.py --sigma-sweep
```

This prints a table like:

```
sigma=3   stars=47
sigma=5   stars=31     ← default; above the 8-star minimum
sigma=7   stars=18
sigma=9   stars=6      ← below the 8-star minimum → TOO_FEW
sigma=11  stars=2
```

If the default sigma=5 yields fewer than 8 stars, **lower sigma on the Camera
page**. Try 3–4. The change takes effect immediately with no restart. Use
**Persist → Apply & save** to keep it across reboots.

#### Step 2 — understand the frame pipeline

The IMX477 native sensor is 4056×3040. The eFinder configures libcamera with
`sensor={"output_size": (4056, 3040)}` to force a full-array readout; the ISP
then downscales 4× to 960×760. This gives an effective pixel pitch of 6.2 µm
and a plate scale of ~51.15 arcsec/px, yielding a ~13.6° horizontal FOV with
a 25 mm focal length lens.

Without this hint, libcamera silently selects the 1332×990 sub-mode (central
65% of sensor, 2×2 binned), producing ~8.8° FOV — which would cause 100%
`NO_MATCH` if `fov_deg` is set to 13.5°. Check the journal:

```bash
sudo journalctl -u efinder | grep "Sensor mode"
# Expected: Sensor mode: output_size=(4056, 3040)
```

#### Step 3 — verify the FOV estimate

If the solver reports ≥ 8 stars but you still get `NO_MATCH`, the FOV
estimate may be wrong. Check the Config page for `fov_deg` and compare it
to your actual optics. Then reset calibration so the solver uses the full
1° tolerance window:

```bash
efinder-ctl calibration reset
```

After a successful solve the eFinder self-calibrates the FOV and tightens the
window automatically.

#### Step 4 — capture frames for off-device analysis

If the problem is hard to diagnose live, collect a debug bundle from the web UI
(**Update** page → **Collect debug ZIP**) or capture frames directly:

```bash
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_camera.py \
    --exp-min 0.2 --exp-max 0.2 --gain-min 20 --gain-max 20
```

Copy to your laptop:
```bash
scp efinder@efinder.local:/var/lib/efinder/YYYYMMDDHHMMSSMMM.zip .
```

#### Quick-reference: sigma adjustment from the web UI

1. Open `http://efinder.local/camera`
2. Find the **Detection sigma** slider (default 5, range 0–20)
3. Drag left or click `−` to lower it — change takes effect on the next frame
4. Watch the dashboard for `stars` count to climb above 8
5. When solving reliably, tick **Persist** and click **Apply & save**

---

## Benchmark and diagnostic scripts

See `tests/README.md` for the full catalogue. All scripts require the venv
Python and (for SHM access) root privileges:

```bash
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/<script>.py
```

| Script | Purpose |
|---|---|
| `diag_services.sh` | System health check — first stop when anything is broken |
| `diag_camera.py` | Camera exposure/gain sweep, saves PNGs + ZIP |
| `diag_bno055.py` | BNO055 IMU sensor registers and live samples |
| `diag_detect.py` | Sycamore centroid extraction timing and sigma sweep |
| `diag_solve.py` | Full pipeline: sycamore extract → solve (blind + hint) with timing |
| `solve_image.py` | Quick single-image solve: sycamore extraction + olive-solve |
| `bench_pipeline_combos.py` | Benchmark sycamore blind + hint paths with optional sweeps |
| `bench_extractor_compare.py` | Sycamore extraction + solve timing benchmark |
| `test_hint.py` | Attitude-hint effectiveness across a sequence of shifted images |

---

## Performance characteristics

### Solve pipeline timing (Pi Zero 2W, 960×760, clear sky)

| Stage | Time |
|---|---|
| Camera capture (ISP hardware) | `exposure_s` + ~10 ms |
| Frame copy to SHM | ~0.5 ms |
| Sycamore extraction (`star_detect.detect_stars`) | ~10–60 ms |
| Solve, IMU-seeded (scope stationary/moving) | ~10–100 ms |
| Solve, blind (first frame / no IMU) | ~300–800 ms |
| LX200 report latency | < 1 ms |
| IMU dead-reckoning (when active) | < 0.1 ms |
| **Typical end-to-end (after first frame)** | **~0.2–0.5 s with 0.2 s exposure** |

### RAM usage

| Process | Typical RSS |
|---|---|
| `camera_proc` | ~60 MB |
| `solver_proc` (olive-solve loaded) | ~180 MB |
| `comms_proc` | ~25 MB |
| `efinder-webui` (Flask) | ~35 MB |
| **Total** | **~300 MB** |

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

### Wheel sourcing

The sycamore-extract (`star_detect-*.whl`) and olive-solve (`tetra3-*.whl`)
wheels are **not** committed to git. They are downloaded from the source
repos' GitHub releases:

- **Image builds**: `release.yml` fetches them into `vendor/wheels/` before
  `build-image.sh` runs. Pin versions with the `SYCAMORE_TAG` /
  `OLIVE_SOLVE_TAG` repository variables (unset = latest release).
- **Existing devices**: `efinder-update` refreshes both wheels from the
  latest releases during an OTA update.
- **Local override**: a wheel placed in `vendor/wheels/` (e.g. via
  `build/local/vendor-wheels.sh`) takes precedence over the download.

To publish new wheels, tag a release in `sycamore-extract` or `olive-solve` —
each repo's release workflow builds and attaches the aarch64 wheels.

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

- **Master-dark subtraction**: a hot-pixel mask (cap-the-lens `dark_capture`,
  8-neighbour repair — see [Dark-frame / hot-pixel capture](#dark-frame--hot-pixel-capture))
  is implemented and the dark-frame fast-path skips completely dark frames, but a
  full per-pixel master-dark subtraction in `camera_proc` is still deferred.
- **No authentication**: LX200 server and web UI are open to any device on
  the same network. Do not expose to the public internet.
- **Single boresight offset**: one calibration per session; swapping eyepieces
  requires a new `:CM#` sync.
- **Polar alignment assumes pure RA motion**: accidental Dec movement between
  the three capture points invalidates the result silently.
- **IMU hint frame mismatch**: the attitude hint propagation assumes the IMU
  body axes ≈ camera axes. A badly-rotated IMU mount will widen the effective
  search window but will not cause solve failures (`strict_hint=False`).
