# eFinder (Pi Zero 2W edition)

Plate-solving electronic finder for amateur telescopes. Runs on a Raspberry Pi
Zero 2W with an Arducam 12 MP IMX477. Reports live pointing to SkySafari (or
any LX200-speaking application) over Wi-Fi or USB tether, with sub-arcsecond
boresight registration and a built-in three-point polar alignment assistant.

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
14. [Updating in the field](#updating-in-the-field)
15. [Performance characteristics](#performance-characteristics)
16. [Architecture](#architecture)
17. [Building and development](#building-and-development)
18. [Known limitations and deferred work](#known-limitations-and-deferred-work)

---

## What it does

The eFinder turns a Raspberry Pi Zero 2W into a self-contained plate-solving
"sky compass" that mounts to your telescope tube. Point the scope at any part
of the sky. Within a second or two the eFinder has identified the star field,
calculated the telescope's precise RA/Dec, and is streaming that position to
SkySafari in real time via the LX200 protocol.

**Core capabilities:**

- **Continuous plate-solving** at ~1–2 s per frame (960×760, Cortex-A53
  optimized). Each solve reports RA, Dec, field-of-view, and image orientation
  (position angle / roll).
- **Boresight calibration** via SkySafari's Sync command: center a star,
  tap Sync, and the eFinder stores the pixel offset between the star's
  centroid and the frame center. The offset is persisted across reboots.
- **FOV self-calibration**: after ~30 successful solves the eFinder
  accumulates the reported field-of-view, commits the median to config, and
  uses a tighter (0.1°) tolerance window for subsequent solves — improving
  both speed and reliability.
- **Polar alignment assistant**: rotate the mount in RA only at three
  positions. The eFinder fits a great circle through the three plate-solve
  results, derives the true RA axis, and reports azimuth and altitude
  corrections to align it with the celestial pole. Requires observer latitude,
  which SkySafari supplies automatically.
- **IMU dead-reckoning** (optional, requires BNO055): a BNO055 IMU on I²C
  self-calibrates passively from consecutive plate-solve pairs and smooths
  SkySafari position updates between solves to < 1 ms latency. Hot-pluggable
  — no reboot needed.
- **Live web UI** on port 80: dashboard, camera settings, polar alignment
  workflow, Wi-Fi switching, configuration viewer, live log tail, and one-click
  in-place updater.
- **Maintenance CLI** (`efinder-ctl`) for full control over boresight,
  calibration, exposure, gain, and polar alignment without opening a browser.
- **Dual network presence**: USB Ethernet gadget (plug-and-play, no drivers on
  Mac/Linux) and self-hosted Wi-Fi AP simultaneously. Optionally joins your home
  Wi-Fi instead; browser-based switching with automatic AP fallback.
- **Dark frame fast-path**: frames below 20 ADU peak (lens cap on, completely
  dark sky) are detected in ~0.1 ms and skipped entirely — the solver never
  wastes time on empty frames.

---

## Hardware requirements

| Component | Notes |
|---|---|
| Raspberry Pi Zero 2W | The quad-core Cortex-A53 is essential; Pi Zero 1 is too slow |
| Arducam 12 MP IMX477 (HQ Camera) | 8-bit Y-plane capture via YUV420; color sensor used in mono mode |
| CSI-2 ribbon cable | 15-pin to 22-pin for the Zero's smaller connector |
| MicroSD card | 8 GB minimum; Class 10 / A1 recommended |
| Micro-USB power supply | 5V 2A; the Zero's middle port is data+power |
| Micro-USB to USB-A cable | For USB tether to laptop/tablet |
| Mounting hardware | Dovetail or finder shoe to attach to the tube |
| BNO055 IMU *(optional)* | I²C inertial sensor for dead-reckoning between plate-solves. Connect to GPIO 2 (SDA) and GPIO 3 (SCL). Hot-pluggable — detected automatically, no reboot. |

The IMX477 was chosen for its large 7.9 mm diagonal sensor (wide field),
12 MP resolution (more stars per frame), and excellent low-light performance.
The camera runs in a 960×760 cropped mode by default for faster solves; the
full sensor can be used by changing `frame_width`/`frame_height` in config at
the cost of ~3× longer solve times.

---

## Quick start — flashing the image

1. Download `efinder-YYYYMMDD-vX.Y.Z.img.xz` (or `efinder-YYYYMMDD.img.xz`
   for a manual build) from the [Releases](../../releases) page.
2. Flash to a microSD card:
   - **Raspberry Pi Imager**: choose "Use custom image," select the `.img.xz`
     file, choose your card, and click Write. No customization needed.
   - **balena Etcher**: drag the `.img.xz` onto Etcher, select your card, Flash.
   - **dd** (Linux/macOS):
     ```bash
     xz -d efinder-YYYYMMDD-vX.Y.Z.img.xz
     sudo dd if=efinder-YYYYMMDD-vX.Y.Z.img bs=4M status=progress oflag=sync of=/dev/sdX
     ```
3. Insert the card and boot. **Do not edit any files on the card before first
   boot** — the first-boot service configures the network on first power-on.
4. First boot takes 30–90 seconds. The green LED on the Pi will stop blinking
   steadily when the eFinder application has started.

### Install on an existing Pi OS Trixie Lite card

If you already have a Pi OS Bookworm/Trixie Lite card configured the way you
want:

```bash
git clone https://github.com/mconsidine/eFinder_cli_new
cd eFinder_cli_new
sudo bash scripts/install.sh
```

`install.sh` is idempotent — safe to re-run after updates. It installs system
packages (including `zram-tools` for compressed swap), Python dependencies,
gRPC stubs, the cedar-detect binary, and all systemd unit files. After the
first run, reboot to let the first-boot service configure the network.

---

## First boot and network access

On first boot, `efinder-firstboot.service` runs once and sets up two network
interfaces that are always available:

| Interface | IP address | How to reach it |
|---|---|---|
| **USB Ethernet gadget** | `10.55.0.1` | Connect the Pi's middle micro-USB (data) port to your computer with a USB cable. No driver needed on macOS and Linux; Windows may need RNDIS drivers. |
| **Wi-Fi access point** | `10.42.0.1` | Join the SSID `efinder-XXXX` (last 4 hex digits of the Pi's Wi-Fi MAC address). |

Both interfaces support SSH and the web UI simultaneously.

**Default credentials:**

| Service | Username | Password |
|---|---|---|
| SSH | `efinder` | `12345678` |
| Wi-Fi AP | — | `12345678` |

These are intentionally simple for a device that operates only on private
networks. Do not expose the eFinder to the public internet.

**mDNS hostname:** `efinder.local` — works on macOS and Linux without
configuration. Windows requires Bonjour (included with iTunes or many printer
drivers). Android `.local` resolution varies by app; use the IP address
directly if `.local` doesn't resolve.

**Quick connectivity check:**

```
http://efinder.local        Web UI
ssh efinder@efinder.local   SSH (or use the IP)
ping efinder.local
```

---

## Connecting from SkySafari

SkySafari connects to the eFinder as a Meade LX200-compatible mount.

### Setup (SkySafari 6 or later)

1. Open **Settings → Telescope → Scope Setup**.
2. Set **Telescope Brand** → Meade and **Mount Type** → LX-200 GPS (or
   Meade LX-200 Classic if GPS is not an option).
3. Set **Connection** → WiFi (TCP).
4. Enter the IP address or `efinder.local`.
5. Set **Port** → `4060`.
6. Tap **Done**, then tap **Connect**.

SkySafari will immediately begin polling for RA/Dec and displaying it on the
sky chart. The eFinder does not need to be solving at the moment of connect;
it will start reporting as soon as the first plate solve completes.

### Sending time and location to the eFinder

SkySafari can (and should) send your current date/time and GPS location to the
eFinder each time it connects. This is required for the polar alignment
assistant to decompose errors into azimuth and altitude components.

In **Settings → Telescope → Scope Setup**, enable:
- **Set Time & Location** — sends the current date/time and your GPS position
  via the `:SL`, `:SC`, `:St`, and `:Sg` LX200 commands on connect.

Once this is enabled, the **Polar** page of the web UI will show your latitude
automatically as soon as SkySafari connects.

### Syncing the boresight

To register the eFinder's boresight to a known star:

1. Centre the star precisely in your eyepiece (or in whatever optical path
   the eFinder parallels).
2. Tap the same star in SkySafari.
3. Tap **Sync** (the telescope icon → Sync).

The eFinder records the offset in pixel coordinates and applies it to all
subsequent pointing reports. The offset is written to
`/etc/efinder/efinder.conf` and survives restarts.

---

## LX200 command reference

The eFinder implements the subset of LX200 commands that SkySafari uses.
All commands use the standard `#`-terminated LX200 wire format.

| Command | Description | Response |
|---|---|---|
| `:GR#` | Get telescope RA | `HH:MM:SS#` |
| `:GD#` | Get telescope Dec | `±DD°MM:SS#` |
| `:CM#` | Sync to target (boresight calibration) | `M31 EX GAL MOC 99#` |
| `:St dd*mm#` | Set observer latitude | `1` (acknowledged) |
| `:Sg ddd*mm#` | Set observer longitude | `1` (acknowledged) |
| `:Gt#` | Get observer latitude | `sDD*MM#` |
| `:Gg#` | Get observer longitude | `sDDD*MM#` |
| `:SL HH:MM:SS#` | Set local time | `1` |
| `:SC MM/DD/YY#` | Set date | `1Updating Planetary Data#` |
| `:MS#` | Move to target (ignored) | `0` |
| `:Q#` | Stop movement (ignored) | _(empty)_ |
| `:P#` | Toggle precision (no-op) | `HIGH PRECISION` |
| `:GVP#` | Get product name | `eFinder#` |
| `:GVN#` | Get firmware version | version string |

The `:St` command (latitude) is used by the polar alignment assistant. If
SkySafari is configured with **Set Time & Location** enabled, it sends `:St`
immediately on connect, and the Polar page will populate the latitude field
without any manual action. The value is cached in config so it survives
reconnects.

---

## Web UI

Open `http://efinder.local` (or the Pi's IP address) from any browser on the
same network. The web UI uses the same maintenance socket as `efinder-ctl` and
holds no state of its own.

### Dashboard (`/`)

The dashboard auto-refreshes every 1.5 seconds and shows:

- **Pointing**: current RA/Dec, star count, match count, solve time, peak pixel
  value, FOV, and image roll (position angle of celestial north relative to
  image up). Auto-refreshed from `/api/status`.
- **Focus**: Laplacian variance score at the last focus commit, with a link to
  the Focus page for active focusing.
- **Calibration**: FOV calibration state, committed FOV, rolling window
  fill level, median, and standard deviation. Includes a **Recalibrate** button.
- **Boresight**: current boresight pixel coordinates. **Reset to center** button
  resets to the frame center without a new sync.
- **IMU**: if a BNO055 is detected, shows calibration progress (pairs
  collected / 3 needed), R² fit quality, and state (calibrating / active).
  Activates automatically once 3 pairs are collected and R² ≥ 0.85.

### Camera (`/camera`)

Live camera view with configurable settings:

- **Live frame**: JPEG from the current SHM buffer with a boresight crosshair
  overlaid. Refreshes every 2 seconds.
- **Exposure / Gain**: sliders for exposure time (0.001–10 s) and analogue
  gain (1–64). Changes take effect on the next frame; use **Persist** to write
  to config.
- **Solver parameters**: sliders for `detect_sigma` (star extraction threshold,
  3–20) and `solve_timeout_ms` (per-frame solver budget, 500–5000 ms). Changes
  take effect immediately; use **Persist** to write to config.

### Polar alignment (`/polar`)

Step-by-step workflow for three-point polar alignment. See the
[Polar alignment](#polar-alignment) section for the full procedure.

### Wi-Fi (`/wifi`)

Browser-based Wi-Fi management:

- **Status card**: shows current mode (AP / station), SSID, and IP address.
  Includes a **Switch to AP Mode** button when in station mode.
- **Connect form**: enter an SSID (with autocomplete from a live network scan)
  and password, then click **Connect**. A connecting page counts down 90 s,
  polls `/api/wifi/status` every 3 s, and reports success, fast fail (AP
  fallback fired), or timeout. On timeout the JS automatically POSTs to
  `/wifi/ap` to restore the AP — a second independent fallback layer alongside
  the one built into `station.sh` itself.
- **Rescan button**: triggers a fresh NM Wi-Fi scan and repopulates the SSID
  list without a page reload.

If the Pi cannot reach the station network and you are not connected via USB
Ethernet, connect via USB at `10.55.0.1` and run `sudo ap.sh` to restore AP mode.

### Configuration (`/config`)

Read-only view of `/etc/efinder/efinder.conf`. To make changes, edit the file
via SSH and restart the service (`sudo systemctl restart efinder`).

### Logs (`/logs`)

Live tail of `journalctl` output for both `efinder.service` and
`cedar-detect.service`. Useful for diagnosing solve failures or camera errors
in the field.

### Update (`/update`)

Runs `efinder-update` in one click. Fetches the latest tagged release,
updates Python dependencies, regenerates gRPC stubs, downloads the matching
`cedar-detect-server` binary, and restarts both services. The update is
non-destructive — it never touches `/etc/efinder/efinder.conf`.

### Health endpoint (`/healthz`)

Returns HTTP 200 with `{"status": "ok"}` if the eFinder daemon is reachable.
Suitable for external monitoring or uptime checkers.

---

## Polar alignment

The eFinder includes a three-point polar alignment assistant that computes the
azimuth and altitude error of your mount's RA axis relative to the true
celestial pole.

### Theory

Rotating a mount in RA traces a small circle on the celestial sphere centered
on the RA axis. If the RA axis is perfectly polar-aligned, that circle is
centered on the pole. If not, the center is offset. Capturing three
plate-solved positions at three different RA angles gives three points on that
circle; fitting a great circle through them yields the circle's center, which
is where the RA axis is actually pointing. The angular distance from the true
pole, decomposed into azimuth and altitude components using the observer's
latitude, gives the corrections to apply to the mount's altitude and azimuth
adjusters.

### Requirements

- Observer latitude (sent automatically by SkySafari if **Set Time & Location**
  is enabled, or set manually with `efinder-ctl polar set-latitude`).
- At least 8 detected star centroids per capture point (otherwise the solver
  rejects the frame as unreliable). Increase exposure if too few stars are
  detected.
- The mount must be rotated **only in RA** between capture points — no
  declination movement. The algorithm does not detect dec drift and will give
  incorrect results if the scope is bumped in dec.

### Procedure

**Via the web UI (`/polar` page):**

1. Ensure SkySafari is connected with **Set Time & Location** enabled, or set
   latitude manually. Confirm the latitude field on the Polar page shows a
   non-zero value.
2. Aim the scope at an area of sky with at least 20–30 visible stars (avoid
   the horizon and areas with heavy light pollution).
3. Click **Start Polar Alignment**.
4. Wait for the first capture point to be collected (the eFinder takes several
   successful solves and averages them). A green checkmark appears when done.
5. Rotate the mount **20–40°** in RA (without touching declination).
6. Click **Capture point 2**. Wait for the checkmark.
7. Rotate another 20–40° in RA.
8. Click **Capture point 3**. Wait for the checkmark.
9. The eFinder computes the RA axis offset and displays:
   - **Azimuth error**: degrees East/West. Adjust the mount's azimuth
     (horizontal) knob.
   - **Altitude error**: degrees Up/Down. Adjust the mount's altitude
     (vertical) knob.
   - **Total error**: combined angular distance from the pole.
10. Make the adjustments and repeat from step 2 until the total error is
    below your target (typically < 0.1° for visual use, < 0.05° for
    unguided imaging up to a few minutes per frame).

**Via `efinder-ctl`:**

```bash
efinder-ctl polar set-latitude 44.5   # if SkySafari hasn't sent it
efinder-ctl polar start
# rotate in RA, then:
efinder-ctl polar status              # check progress, repeat until 3 points captured
efinder-ctl polar status              # result shows az/alt corrections
efinder-ctl polar cancel              # abort if needed
```

### Notes

- If latitude is not available when the three captures complete, the result is
  stored as "pending decomposition." As soon as latitude arrives (via SkySafari
  connecting or `efinder-ctl polar set-latitude`), the decomposition runs
  retroactively without requiring a new capture.
- The latitude from SkySafari is persisted to config after first receipt, so
  users at a permanent observatory only need to set it once.
- Polar alignment can be performed in twilight or on a partially cloudy night
  as long as enough stars are visible. It does not require a clear view of the
  pole itself.

---

## Focus assessment

The dashboard shows a **focus score** (Laplacian variance) computed from the
brightest detected star in each solved frame. The score is the sum of squared
Laplacian values over a 60×60 pixel patch centered on the star's centroid.

A higher score means sharper focus. The absolute value depends on seeing,
star brightness, and exposure, so use it as a relative guide: adjust focus
until the score peaks, then stop.

Typical values:
- Severely out of focus: < 100
- Reasonably focused: 500–2000
- Well focused in good seeing: > 2000

The score is only computed when the solver has detected at least one centroid,
so it will not appear on the dashboard when pointing at an empty sky region or
while the dark frame fast-path is active (lens cap on).

---

## IMU dead-reckoning (optional)

If a BNO055 inertial measurement unit (IMU) is wired to the Pi's I²C bus
(GPIO 2 = SDA, GPIO 3 = SCL), the eFinder uses it to smooth SkySafari
position updates between plate-solves.

### How it works

Without the IMU, SkySafari polls `:GR#`/`:GD#` every ~0.5–1 s and gets the
last plate-solved position, which jumps discretely each time a new solve
completes. With the IMU active, the same polls return a continuously updated
dead-reckoning estimate: the last known position plus the rotation the IMU has
measured since that solve. The result is smooth, < 1 ms response even while
the solver is busy.

### Self-calibration

No manual calibration ritual is required. The IMU transform is learned
passively from consecutive plate-solve pairs:

1. After each successful solve, the solver computes the angular displacement
   both in sky coordinates (from the two RA/Dec solutions) and in camera
   coordinates (by rotating the sky delta by the image roll angle, so the
   result is independent of where the scope is pointing).
2. The sky displacement and the simultaneous IMU rotation vector are stored
   as a training pair in a 20-entry ring buffer.
3. Once 3 pairs are available, a 2×3 least-squares matrix (C) is fitted that
   maps IMU rotation vectors to (right, up) displacements in the camera frame.
4. The IMU becomes "active" when at least 3 pairs are collected and the fit
   R² ≥ 0.85. Status is shown on the dashboard IMU card.

The ring buffer is rolling, so C continues to refine as more pairs arrive and
old pairs age out. Because training pairs are expressed in the camera frame
(using the image roll), C captures the fixed mechanical mounting relationship
between the IMU and the camera — valid across the whole sky, not just near
the calibration region.

### Roll (position angle)

Each plate-solve also reports **roll** — the rotation of celestial north
relative to the image's "up" direction (toward y=0), counter-clockwise
positive. Roll is shown on the dashboard Pointing card.

Roll serves two purposes:

1. **IMU calibration normalization** (described above): rotating the sky delta
   by −roll before fitting removes the sky-position dependence from C, making
   the calibration globally valid.
2. **Dead-reckoning prediction**: when predicting, the camera-frame delta
   from C is rotated back by +roll at the reference solve to recover the
   correct sky-frame RA/Dec offset.

### Hardware setup

Wire the BNO055 to the Pi Zero 2W's I²C-1 bus:

| BNO055 pin | Pi Zero 2W pin |
|---|---|
| VIN | 3.3 V (pin 1) |
| GND | GND (pin 6) |
| SDA | GPIO 2 (pin 3) |
| SCL | GPIO 3 (pin 5) |

The I²C bus is enabled by default on Pi OS. The eFinder uses `smbus2`
(installed by `install.sh`), not the Adafruit CircuitPython library. The
`efinder` user must be in the `i2c` group:

```bash
sudo usermod -aG i2c efinder
```

The chip is detected automatically within 3 s of being powered on (hot-plug).
No configuration or reboot is needed. If the chip is removed, the eFinder
reverts to direct plate-solve reporting with no interruption to normal
operation.

---

## Wi-Fi modes

The eFinder starts in **access point mode** (AP mode) out of the box. It also
has a **station mode** where it joins an existing Wi-Fi network.

### AP mode (default)

The Pi hosts its own network on `10.42.0.1`. SSID: `efinder-XXXX` (last 4 hex
digits of the Wi-Fi MAC). Password: `12345678`. SkySafari connects directly
to this network.

### Switching to station mode

**Via the web UI (recommended):**

Open `http://efinder.local/wifi`, enter the target SSID and password in the
Connect form, and click **Connect**. The connecting page counts down 90 s while
polling for success. Two independent fallback layers restore AP mode
automatically if the connection fails — no manual intervention needed.

**Via SSH / command line:**

```bash
sudo /usr/local/bin/station.sh "MySSID" "MyPassword"
```

Or run interactively (scans for available SSIDs):

```bash
sudo /usr/local/bin/station.sh
```

After connecting in station mode, the Pi's IP address on the home network is
assigned by DHCP. Use `efinder.local` to find it without knowing the IP. The
USB Ethernet gadget remains on `10.55.0.1` regardless of Wi-Fi mode.

### Switching back to AP mode

**Via the web UI:** Open `/wifi` and click **Switch to AP Mode**.

**Via command line:**

```bash
sudo /usr/local/bin/ap.sh
```

### Recovering from bad Wi-Fi credentials

If the Pi cannot join the target network (wrong password, out of range), both
`station.sh` and the web UI's JS watchdog independently attempt to restore AP
mode. If Wi-Fi is fully lost:

1. Connect via USB Ethernet (`10.55.0.1`) and open `http://10.55.0.1/wifi`, or
   run `sudo /usr/local/bin/ap.sh` via SSH.
2. SSH in via `efinder.local` over USB, run `sudo nmcli con delete "wrong-ssid"`,
   then retry.
3. As a last resort, re-flash the SD card.

---

## Configuration reference

Configuration lives in `/etc/efinder/efinder.conf`. The file uses
`key: value` syntax; lines beginning with `#` are comments.

Edit the file via SSH and restart the eFinder to apply changes:

```bash
sudo nano /etc/efinder/efinder.conf
sudo systemctl restart efinder
```

Any key can also be overridden via environment variable at startup (useful for
one-off tests without touching the file):

```bash
sudo EFINDER_EXPOSURE_S=0.5 systemctl restart efinder
```

### Full configuration reference

| Key | Default | Description |
|---|---|---|
| `frame_width` | `960` | Camera capture width in pixels. Reducing improves solve speed; increasing gives more stars but slower solves. |
| `frame_height` | `760` | Camera capture height in pixels. |
| `exposure_s` | `0.2` | Starting exposure time in seconds. Adjustable live via the web UI or `efinder-ctl`. Valid range: 0.001–10.0 s. |
| `gain` | `20.0` | Analogue gain. Valid range: 1.0–64.0. |
| `fov_deg` | `13.5` | Initial field-of-view estimate in degrees (diagonal). Self-calibrates after 30 solves. |
| `detect_sigma` | `8.0` | Cedar-detect star extraction threshold in sigma above background. Lower values find more (fainter) stars but increase noise centroids. |
| `solve_timeout_ms` | `1500` | Maximum time cedar-solve is allowed per frame in milliseconds. Frames that exceed this are reported as TIMEOUT. |
| `lx200_port` | `4060` | TCP port the LX200 server listens on. |
| `boresight_y` | `380` | Boresight Y coordinate in pixels. Set by `:CM#` sync command. |
| `boresight_x` | `480` | Boresight X coordinate in pixels. Set by `:CM#` sync command. |
| `latitude_deg` | _(empty)_ | Observer latitude in decimal degrees. Populated automatically from SkySafari's `:St` command on first connect. |
| `longitude_deg` | _(empty)_ | Observer longitude in decimal degrees. Populated automatically from SkySafari's `:Sg` command on first connect. |
| `save_failed_frames` | `false` | Reserved for future use. When implemented, saves frames that fail to solve to `/var/lib/efinder/captures/`. |
| `cedar_detect_socket` | `localhost:50051` | gRPC address of the cedar-detect-server. Do not change unless running cedar-detect on a different host. |
| `cpu_camera` | `3` | CPU affinity for the camera worker process (0–3). |
| `cpu_solver` | `3` | CPU affinity for the solver worker process (0–3). |
| `cpu_comms` | `1` | CPU affinity for the comms worker process (0–3). |

### CPU affinity layout

The four cores of the Zero 2W are assigned as follows:

| CPU | Role |
|---|---|
| 0 | Linux kernel, IRQs, sshd, journald, NetworkManager — left alone |
| 1 | `comms_proc` — LX200 TCP server, maintenance socket, web UI backend |
| 2 | `cedar-detect-server` — Rust gRPC star centroid extractor |
| 3 | `solver_proc` + `camera_proc` — plate solver and camera capture loop |

The solver and camera share CPU 3 because the camera's picamera2 loop is
mostly dormant between frame captures (it blocks on the ISP hardware), while
the solver is CPU-bound during solves. In practice they time-slice naturally.

### Zram compressed swap

The installer configures `zram-tools` to provide ~256 MB of compressed swap
using the LZ4 algorithm at near-RAM speeds. This is important on the Zero 2W's
512 MB RAM when large solver data structures are in use. You can verify it is
active:

```bash
swapon --show
```

Should show a `/dev/zram0` device.

---

## Maintenance CLI (`efinder-ctl`)

`efinder-ctl` talks to the eFinder daemon over a Unix socket at
`/run/efinder/maint.sock`. It works whether or not the web UI is running and
is safe to use while the solver is actively running.

```bash
# Status and health
efinder-ctl ping                          # check daemon is alive
efinder-ctl version                       # daemon version string
efinder-ctl status                        # current solution: RA, Dec, status, stars

# Boresight
efinder-ctl boresight show                # current pixel offset (Y X)
efinder-ctl boresight center              # reset to frame center
efinder-ctl boresight set 380 480         # set to specific pixel coords (Y X)

# Calibration
efinder-ctl calibration status            # FOV calibration state and value
efinder-ctl calibration reset             # discard calibration, return to defaults

# Exposure and gain
efinder-ctl exposure get                  # current exposure in seconds
efinder-ctl exposure set 0.3              # change exposure (this session only)
efinder-ctl exposure set 0.3 --persist    # change and write to /etc/efinder/efinder.conf
efinder-ctl gain set 15.0                 # change gain (this session only)
efinder-ctl gain set 15.0 --persist       # change and write to config

# Polar alignment
efinder-ctl polar start                   # begin a polar alignment session
efinder-ctl polar status                  # capture progress and result
efinder-ctl polar cancel                  # abort the current session
efinder-ctl polar set-latitude 44.5       # set observer latitude manually

# Advanced
efinder-ctl raw '{"cmd":"ping","args":{}}'  # send a raw JSON command
```

The maintenance socket uses newline-delimited JSON. The protocol is documented
in `efinder/maint.py` if you want to integrate a custom client.

---

## Updating in the field

### Via the web UI

Open `http://efinder.local/update` and click **Update now**. The update runs
`efinder-update` which:

1. Checks the current git working tree is clean (refuses if there are local
   modifications).
2. Fetches the latest tagged release from GitHub.
3. Pulls the new application code.
4. Updates Python dependencies.
5. Regenerates gRPC protobuf stubs.
6. Downloads the matching `cedar-detect-server` binary from the release assets.
7. Restarts `efinder.service` and `cedar-detect.service`.

### Via SSH

```bash
sudo /usr/local/bin/efinder-update              # latest tagged release
sudo /usr/local/bin/efinder-update v0.7.1       # specific version
```

### What the update does not touch

- `/etc/efinder/efinder.conf` — your configuration is preserved.
- Boresight calibration data stored in config.
- The OS, kernel, or any system packages — `efinder-update` only updates the
  eFinder application layer.

---

## Performance characteristics

### Solve pipeline timing (Pi Zero 2W, 960×760, typical clear sky)

| Stage | Time |
|---|---|
| Camera capture (ISP hardware) | exposure_s + ~10 ms overhead |
| Frame copy to shared memory | ~0.5 ms (lock-free memcpy) |
| cedar-detect centroid extraction (gRPC, SHM zero-copy) | 30–80 ms |
| cedar-solve plate matching | 200–800 ms (depends on star count and FOV calibration state) |
| LX200 report latency after solve | < 1 ms |
| IMU dead-reckoning prediction (when active) | < 0.1 ms |
| **Total end-to-end (typical)** | **~1–2 s with 0.2 s exposure** |

### Camera frame rate

The camera operates at the hardware-limited frame rate for the configured
exposure. With a 0.2 s exposure, the Zero 2W ISP delivers approximately
5 fps. The solver processes one frame per cycle; excess frames are silently
dropped (always the freshest frame is used thanks to the triple-buffer design).

With the lens cap on or in a completely dark environment, the dark frame
fast-path triggers in ~0.1 ms and the frame is skipped without any gRPC or
solver invocation.

### RAM usage

| Process | Typical RSS |
|---|---|
| `efinder_main` (launcher) | ~30 MB |
| `camera_proc` | ~60 MB (picamera2 + libcamera) |
| `solver_proc` (tetra3 loaded) | ~160 MB |
| `comms_proc` | ~25 MB |
| `efinder-webui` (Flask) | ~35 MB |
| `cedar-detect-server` | ~15 MB |
| **Total** | **~325 MB** |

**IMU overhead:** The `imu_thread` daemon reads 8 bytes over I²C at 20 Hz,
consuming < 0.4% of total system CPU and < 1 MB additional RSS. When the IMU
is absent the thread polls once every 3 s — effectively zero overhead.

With zram swap providing ~256 MB of additional (compressed) swap, the system
operates comfortably within the Zero 2W's 512 MB physical RAM.

### Solve reliability

Typical success rates depend on sky conditions and the calibration state of
the FOV:

| Condition | Expected solve rate |
|---|---|
| Good sky, calibrated FOV | > 95% |
| Good sky, uncalibrated FOV | 80–90% |
| Hazy sky (< 20 centroids) | 50–70% |
| Horizon (high airmass) | 40–60% |
| Lens cap on / dark | 0% (fast-path, ~0.1 ms per frame) |

---

## Architecture

### Process topology

```
┌─────────── efinder_main (CPU 0, then yields) ────────────────────────────────┐
│  Launches workers, manages shared state, handles SIGTERM/SIGCHLD              │
│  imu_thread (daemon, CPU any) — probes I²C every 3 s, reads at 20 Hz         │
└──────────┬──────────────┬─────────────────┬───────────────────────────────────┘
           │              │                 │
    ┌──────▼──────┐ ┌─────▼──────┐  ┌──────▼──────┐
    │ comms_proc  │ │camera_proc │  │ solver_proc  │
    │   CPU 1     │ │   CPU 3    │  │   CPU 3      │
    │             │ │            │  │              │
    │ LX200 TCP   │ │ picamera2  │  │ cedar-solve  │
    │ :4060       │ │ → SHM      │  │ ← SHM        │
    │ maint.sock  │ │ FrameSlots │  │ → cedar-det. │
    └──────┬──────┘ └────────────┘  └──────┬───────┘
           │                               │ gRPC
    ┌──────▼──────┐                 ┌──────▼───────┐
    │efinder-webui│                 │cedar-detect  │
    │ Flask :80   │                 │  CPU 2       │
    │ maint.sock  │                 │  Rust gRPC   │
    └─────────────┘                 └──────────────┘
```

The `imu_thread` runs as a Python daemon thread inside the `efinder_main`
process. It polls I²C addresses 0x28 and 0x29 every 3 s looking for a BNO055
chip. When found it initialises the chip in IMUPLUS mode (accelerometer +
gyroscope only; no magnetometer, immune to telescope motors and metal) and reads
quaternions at 20 Hz. IMU data is written to `shared_cfg` so both `solver_proc`
and `comms_proc` can read it without IPC overhead. If the chip is removed, the
thread falls back to the 3 s polling loop.

### Frame pipeline

```
picamera2 (ISP hardware)
  │
  │  capture_array("main")  [exposure_s + ~10ms]
  ▼
numpy Y-plane slice (960×760 uint8)
  │
  │  np.copyto to FrameSlot buf[idx]  [~0.5ms, lock-free]
  ▼
FrameSlots.publish(idx)
  │
  │  solver_proc: FrameSlots.acquire_read_slot()
  ▼
cedar-detect gRPC: DetectStars(shmem_name=..., ...)  [30–80ms]
  │  (cedar-detect reads SHM directly — no 730KB payload over gRPC)
  ▼
centroids []
  │
  │  cedar-solve.solve_from_centroids(centroids, fov_estimate, ...)  [200–800ms]
  ▼
SolveResult(RA, Dec, roll, fov_rad, distortion)
  │
  │  latest_solution.update(...)  [Manager dict, IPC]
  ▼
comms_proc: RA/Dec available for next :GR# / :GD# poll
```

### Shared state

| Object | Type | Readers | Writers |
|---|---|---|---|
| `FrameSlots` | `shared_memory` (3 × 730 KB) | `solver_proc`, `cedar-detect` | `camera_proc` |
| `latest_solution` | `Manager().dict()` | `comms_proc`, web UI | `solver_proc` |
| `shared_cfg` | `Manager().dict()` | `comms_proc`, `solver_proc`, web UI | `comms_proc` (maint cmd), `solver_proc` (boresight/IMU ref), `imu_thread` (IMU data) |
| `camera_cmd_q` | `multiprocessing.Queue` | `camera_proc` | `comms_proc` |
| `solver_cmd_q` | `multiprocessing.Queue` | `solver_proc` | `comms_proc` |

**IMU-related keys in `shared_cfg`:**

| Key | Written by | Description |
|---|---|---|
| `imu_available` | `imu_thread` | `True` when BNO055 is detected and responding |
| `imu_q` | `imu_thread` | Current quaternion as `(w, x, y, z)` tuple |
| `imu_t` | `imu_thread` | `time.monotonic()` of last quaternion read |
| `imu_ref_q` | `solver_proc` | Quaternion at the last plate-solve |
| `imu_ref_ra_deg` | `solver_proc` | RA at the last plate-solve |
| `imu_ref_dec_deg` | `solver_proc` | Dec at the last plate-solve |
| `imu_ref_roll_deg` | `solver_proc` | Roll (position angle) at the last plate-solve |
| `imu_ref_t` | `solver_proc` | Monotonic time of the last plate-solve |
| `imu_calib_pairs` | `solver_proc` | Ring buffer of (IMU rot-vec, camera-frame sky-delta) training pairs (≤ 20) |
| `imu_calib_C` | `solver_proc` | Fitted 2×3 IMU→camera transform matrix (row-major list of 6 floats) |
| `imu_calib_quality` | `solver_proc` | R² of the current calibration fit |
| `imu_calib_n` | `solver_proc` | Number of calibration pairs collected so far |

### Maintenance socket IPC

The maintenance socket at `/run/efinder/maint.sock` handles one JSON command
per connection. The server spawns a daemon thread per connection so slow
solver operations (e.g., a 1.5 s solve cycle) do not block web UI refreshes
or `efinder-ctl` commands from other concurrent callers.

High-frequency read-only operations (`calibration_status`, `polar_status`) use
a short TTL cache (5 s and 1 s respectively) to avoid solver queue pile-up
when the web UI auto-refreshes every 2 seconds from multiple browser tabs.

### Systemd units

| Unit | Description |
|---|---|
| `efinder.service` | Main eFinder application (camera + solver + comms). Restart policy: `always`. |
| `cedar-detect.service` | Rust gRPC centroid server. Started before `efinder.service` via `Wants=`/`After=`. |
| `efinder-webui.service` | Flask web UI on port 80. Independent of `efinder.service`; survives a solver restart. |
| `efinder-firstboot.service` | Network setup — runs on every boot (all operations are idempotent). Configures the USB gadget interface and ensures the AP profile exists. |
| `efinder-ensure-ap.service` | 60-second polling watchdog that restores the AP profile if NetworkManager has suppressed it (e.g. after a failed station-mode attempt). |

---

## Building and development

### Building the SD card image locally

```bash
# One-time: install QEMU user-mode emulation support
sudo apt-get install -y qemu-user-static binfmt-support

# Build (takes 30–90 minutes)
sudo EFINDER_VERSION=dev bash build/build-image.sh

# Output: build/output/efinder.img
xz -T0 -9 build/output/efinder.img
```

The build script:
1. Downloads the official Raspberry Pi OS Trixie Lite arm64 image.
2. Expands the root partition by 2 GB.
3. Runs `install.sh` inside a QEMU chroot (full aarch64 user-mode emulation).
4. Configures USB gadget mode (`dwc2` overlay + `libcomposite`) for
   plug-and-play USB Ethernet.
5. Sets up the first-boot service.
6. Compresses and outputs the image.

### Building cedar-detect-server (cross-compilation)

Normally you do not need to do this — CI builds it automatically and attaches
it to every release. If you need to build locally:

```bash
# Populate the submodule (first time only)
git submodule update --init --recursive

# Install the Rust cross-compilation target and linker
rustup target add aarch64-unknown-linux-gnu
sudo apt-get install -y gcc-aarch64-linux-gnu protobuf-compiler

# Build
cd cedar-detect
CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc \
RUSTFLAGS="-C target-cpu=cortex-a53" \
cargo build --release --target aarch64-unknown-linux-gnu --bin cedar-detect-server

# Strip and deploy
aarch64-linux-gnu-strip target/aarch64-unknown-linux-gnu/release/cedar-detect-server
scp target/aarch64-unknown-linux-gnu/release/cedar-detect-server \
    efinder@efinder.local:/usr/local/bin/
ssh efinder@efinder.local "sudo systemctl restart cedar-detect"
```

The `-C target-cpu=cortex-a53` flag targets the Zero 2W's specific core,
enabling NEON and other Cortex-A53 extensions for a ~5–10% throughput
improvement over generic aarch64.

### CI pipeline

`release.yml` runs on every push to `main` (for paths that affect the build)
and on tagged releases:

1. **cedar-detect job** (always): cross-compiles `cedar-detect-server` for
   `aarch64-unknown-linux-gnu` with Cortex-A53 tuning. Uploads as a workflow
   artifact (retained 30 days) and attaches to the GitHub release on tagged
   builds.

2. **image job** (tagged releases and manual dispatch with `build_image=true`):
   downloads the cedar-detect artifact from job 1, runs the full image build
   in a QEMU chroot, names the output `efinder-YYYYMMDD-vX.Y.Z.img.xz` (or
   `efinder-YYYYMMDD.img.xz` for manual builds), and attaches it to the
   release.

`build-cedar-detect.yml` provides a standalone cedar-detect build on every
push to `main` so development artifacts are always available without triggering
a full image build.

### Running in development (without a Pi)

The comms process and web UI can run on any Linux host for development and
testing:

```bash
# Install Python dependencies
pip install -r requirements.txt

# Generate gRPC stubs
python -m grpc_tools.protoc -I proto \
  --python_out=. --grpc_python_out=. \
  proto/cedar_detect.proto

# Run the web UI only (no solver or camera needed)
EFINDER_MAINT_SOCK=/tmp/efinder-dev.sock python webui/app.py
```

The camera and solver processes require picamera2 and cedar-detect-server
respectively, which are Pi-specific. For development, mock the maintenance
socket or test with the web UI against a running Pi over SSH tunnel.

### Static analysis

Before pushing, run the tree checker:

```bash
bash build/check-tree.sh
```

This verifies that required files exist, imports are consistent, and the
systemd unit syntax is valid.

---

## Known limitations and deferred work

### Not yet implemented

- **Dark frame and hot pixel calibration**: The maintenance socket has
  placeholder hooks for `darkframe capture` and `hotpixels capture`. The
  capture logic in `camera_proc.py` (take N dark frames, compute median,
  subtract) and hot-pixel masking have not been written yet.

- **Auto-exposure**: Infrastructure exists (the solver knows the detected star
  count per frame), but the feedback loop from solver to camera has not been
  wired up. The algorithm would increase exposure when too few stars are
  detected and decrease when too many saturated centroids appear.

- **Frame save for diagnostics**: `save_failed_frames: true` is in config but
  the write logic in `solver_proc.py` is not implemented. When done, failed
  frames will be written to `/var/lib/efinder/captures/YYYYMMDD-HHMMSS.png`
  with centroid overlays.

- **Captive portal for wrong Wi-Fi credentials**: If `station.sh` is given bad
  credentials and the Pi can't join the network, the only recovery is USB
  tether. A `comitup`-based captive portal is planned for a future release.

- **Watchdog for solver hang**: systemd restarts the service if it crashes, but
  not if it hangs without crashing. A heartbeat monitor on
  `latest_solution.epoch_monotonic` is the intended fix.

### Limitations to be aware of

- **No authentication**: The LX200 server and web UI are open to any device on
  the same network. Do not expose the eFinder to the public internet or
  untrusted networks.

- **Single boresight offset**: The eFinder stores one boresight calibration. If
  you use multiple eyepieces with different exit pupils, you need to re-sync
  when swapping.

- **Polar alignment requires pure RA motion**: The three-point algorithm assumes
  the mount is rotated only in RA between capture points. Accidental declination
  movement (bumping the scope) invalidates the result silently.

- **Recovery from bad Wi-Fi**: Wrong credentials during `station.sh` require
  USB tether recovery (see [Wi-Fi modes](#wi-fi-modes)).

- **`detect_sigma` and `solve_timeout_ms` tuning**: Default values are based on
  limited field testing with specific hardware. Once you have a few sessions of
  data, tune these from the median solve time and miss rate shown on the
  dashboard.
