# Diagnostic and benchmark scripts

All scripts require the venv Python. Most need root for SHM access:

```bash
sudo /opt/efinder/venv/bin/python3 tests/<script>.py [options]
```

---

## `diag_camera.py` — camera exposure/gain sweep

Captures one frame for every combination of exposure and gain in the
specified ranges and saves each as a PNG.  Useful for finding the right
exposure and gain before a session, evaluating read noise vs. sky background,
or assessing focus quality across settings.

```bash
# Default sweep: exposures 0.05–0.30 s (step 0.05), gains 15–40 (step 5)
sudo .../diag_camera.py

# Custom range with 2×2 software binning
sudo .../diag_camera.py --exp-min 0.1 --exp-max 0.5 --exp-step 0.1 \
                         --gain-min 10 --gain-max 30 --gain-step 5 \
                         --binning

# Single exposure/gain pair — matches solver defaults (exp=0.2s, gain=20)
sudo .../diag_camera.py --exp-min 0.2 --exp-max 0.2 \
                         --gain-min 20 --gain-max 20

# Save to a specific directory
sudo .../diag_camera.py --output-dir /tmp/frames
```

File naming convention: `YYYYMMDDHHMMSSMMM-EEE-GG[-2x2].png`
- `EEE` = exposure in milliseconds, zero-padded to 3 digits
- `GG`  = gain (integer)
- `-2x2` suffix present when `--binning` is specified

Example: `20260603190304010-050-20-2x2.png` — captured 2026-06-03 at
19:03:04.010, exposure 50 ms, gain 20, 2×2 binned.

After all frames are captured they are bundled into a ZIP archive named
`YYYYMMDDHHMMSSMMM.zip` (the sweep start timestamp) and the individual PNGs
are deleted after a ZIP integrity check.  The archive is the only artifact
left in the output directory, making it easy to transfer off the device.

**The ZIP always contains two extra diagnostic files:**
- `capture_info.txt` — sweep parameters, frame pipeline explanation, hostname,
  Pi model, OS version, live daemon status (solver backend, test mode, FOV,
  star count, last solve time)
- `efinder.conf` — verbatim copy of `/etc/efinder/efinder.conf` at the time
  of capture, so the exact settings that produced the frames are preserved

Files are saved to wherever `test.png` lives (`/var/lib/efinder` by default),
or the current directory if `test.png` is not found.  `--output-dir` overrides.

Per-frame log shows peak pixel and mean pixel value — useful for spotting
saturation or underexposure without opening every file.  The script prints the
`scp` command needed to copy the archive to your laptop when it finishes.

### What you are capturing

The IMX477 native sensor is 4056×3040.  When picamera2 is asked for 960×760
(the efinder default), it selects the 2×2 hardware-binned sensor mode
(2028×1520) and the ISP scales the result down to 960×760.  **The full sensor
area (full FOV) is always used — this is not a crop.**  Captured PNGs are raw
8-bit grayscale Y-plane with no display stretch applied, identical in content
to what the efinder solver receives.  A frame captured without `--binning` at
the default 960×760 is directly comparable to what cedar-detect and tetra3rs
see during a live solve.

**Note on the live-view stretch**: the web UI Camera page displays frames with
an arcsinh sky-subtracted stretch (sky median subtracted, then
`arcsinh(x / β)` scaled to the 99.9th percentile).  Frames captured by
`diag_camera.py` are raw, which is what you want for exposure/gain evaluation.

### Transferring the archive to a laptop

The script prints the exact command when it finishes.  In general:

```bash
# From your laptop (replace IP or use efinder.local):
scp efinder@efinder.local:/var/lib/efinder/YYYYMMDDHHMMSSMMM.zip .

# If you used --output-dir:
scp efinder@efinder.local:/tmp/frames/YYYYMMDDHHMMSSMMM.zip .
```

Password is `12345678` unless you changed it.  On Windows use WinSCP or
`pscp` (PuTTY tools).

---

## `diag_bno055.py` — BNO055 IMU sensor diagnostic

Tests for sensor presence and prints every data register the BNO055 exposes.
Requires only `smbus2` (no Adafruit library).

```bash
sudo .../diag_bno055.py                    # auto-detect address, NDOF mode, 5 samples
sudo .../diag_bno055.py --address 0x29     # force alternate I2C address
sudo .../diag_bno055.py --bus 3            # different I2C bus
sudo .../diag_bno055.py --samples 20       # more live samples
sudo .../diag_bno055.py --interval 0.5     # faster sampling
sudo .../diag_bno055.py --mode imuplus     # match the efinder daemon mode (no mag)
sudo .../diag_bno055.py --no-mode-change   # leave sensor in whatever mode it is
```

Prints (in order):

1. **Chip identification** — chip ID, accel/mag/gyro sub-IDs, firmware revision.
   PASS/FAIL against expected values so a broken sensor or wiring fault is obvious.
2. **System status** — operation mode, power mode, `SYS_STAT`, `SYS_ERR`,
   clock source (internal RC vs external crystal).
3. **Self-test result** — MCU, gyro, magnetometer, accel (PASS/FAIL each).
4. **Calibration status** — system/gyro/accel/mag, each 0–3 with a bar display.
   Includes tips for improving calibration (still at rest → gyro, figure-8 → mag).
5. **Live samples** — per sample: temperature, quaternion + |q| sanity check,
   Euler (heading/roll/pitch), raw accel, linear accel (gravity removed), gravity
   vector, gyroscope, magnetometer + field strength with Earth-range check.

Default mode is NDOF (full 9-DOF fusion) so all outputs are exercised.
The efinder daemon uses IMUPLUS (accel + gyro only, magnetometer disabled) to
avoid magnetic interference from telescope motors.

---

## `diag_services.sh` — system health check

```bash
sudo bash tests/diag_services.sh
```

Full system health check. Covers:
- `efinder` and `cedar-detect` service state
- Port 50051 (cedar-detect gRPC listener)
- Maintenance socket `/run/efinder/maint.sock` with live status query
- Shared memory frame buffers
- tetra3rs and tetra3 Python database files
- Python library imports (grpc, numpy, tetra3, tetra3rs, picamera2, PIL)
- Process list
- Active configuration
- Last 40 lines of each service's journal

Run this first when anything is broken.

---

## `diag_detect.py` — cedar-detect extraction diagnostic

Tests cedar-detect centroid extraction in isolation.

```bash
sudo .../diag_detect.py                          # live SHM from running daemon
sudo .../diag_detect.py --image /path/to/img.png # use a saved frame
sudo .../diag_detect.py --sigma 7.0              # override sigma
sudo .../diag_detect.py --reps 10                # more timing repetitions
sudo .../diag_detect.py --sigma-sweep            # sweep sigma 3–12, show star count table
sudo .../diag_detect.py --binned                 # force binned candidate search on
```

Stages:
1. Config + library imports
2. Cedar-detect gRPC connectivity
3. Frame source — live SHM / provided PNG / synthetic star field fallback
4. ExtractCentroids timing (warm-up + N repetitions)
5. Sigma sweep (if `--sigma-sweep`)

Works with the daemon in test mode or live mode — reads from whatever SHM
buffer the daemon has populated, non-destructively.

---

## `diag_solve.py` — full pipeline diagnostic

Tests the complete frame → extract → solve pipeline.

```bash
sudo .../diag_solve.py                           # tetra3rs backend, live SHM
sudo .../diag_solve.py --backend cedar           # tetra3 Python backend
sudo .../diag_solve.py --image /path/to/img.png
sudo .../diag_solve.py --reps 5
```

Reports per-stage timing (extraction, solve), solve status (SOLVED /
NO_MATCH / TIMEOUT / TOO_FEW), and RA/Dec/roll/FOV when solved.

---

## `bench_pipeline_combos.py` — four-combination benchmark

Benchmarks all four extraction × solve combinations:

| Combo | Extraction | Solve |
|---|---|---|
| 1 | cedar-detect gRPC | tetra3 Python |
| 2 | tetra3rs native | tetra3rs Rust |
| 3 | tetra3rs native | tetra3 Python |
| 4 | cedar-detect gRPC | tetra3rs Rust ← matches the daemon's default |

```bash
sudo .../bench_pipeline_combos.py                    # all 4 combos, live SHM
sudo .../bench_pipeline_combos.py --image img.png    # use a saved frame
sudo .../bench_pipeline_combos.py --reps 5
sudo .../bench_pipeline_combos.py --sigma 7.0
sudo .../bench_pipeline_combos.py --binned

# Hint uncertainty sweep (seeded solve speed vs reliability):
sudo .../bench_pipeline_combos.py --hint-sweep

# Sigma sweep (cedar vs tetra3rs star yield across sigma 3–12):
sudo .../bench_pipeline_combos.py --sigma-sweep
```

`--hint-sweep` varies `hint_uncertainty_deg` across [5.0, 2.0, 1.0, 0.5,
0.2, 0.1, 0.05, 0.02] with both `strict_hint=False` and `True`. Use this
to choose the best hint window for your sky conditions and move frequency.

`--sigma-sweep` is the quickest way to confirm whether the default sigma=9
is appropriate for your setup or whether it should be tuned up or down.
