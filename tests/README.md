# Diagnostic and benchmark scripts

All scripts require the venv Python.  Most need root for SHM access:

```bash
sudo /opt/efinder/venv/bin/python3 tests/<script>.py [options]
```

The solver backend is **olive-solve** (Rust, in-process tetra3-py wheel).
No external daemon, gRPC, or server is involved.

---

## Quick reference

| Script | Purpose |
|---|---|
| `diag_services.sh` | System health check — first stop when anything is broken |
| `diag_camera.py` | Camera exposure/gain sweep, saves PNGs + ZIP |
| `diag_bno055.py` | BNO055 IMU sensor registers and live samples |
| `diag_detect.py` | Centroid extraction timing and sigma sweep |
| `diag_solve.py` | Full pipeline diagnostic: extract → solve (blind + hint) |
| `diag_background.py` | Background-mode A/B (row/line/top_hat…); `--solve` adds live-solver match rates per mode (memory-safe) |
| `solve_image.py` | Solve a single image: sycamore extraction + olive-solve |
| `bench_pipeline_combos.py` | Benchmark sycamore blind + hint solve paths with optional sweeps |
| `test_hint.py` | Attitude-hint effectiveness across a sequence of shifted images |

---

## `diag_services.sh` — system health check

```bash
sudo bash tests/diag_services.sh
```

Full system health check. Covers:
- `efinder` systemd service state
- Maintenance socket `/run/efinder/maint.sock` with live status query
  (solved, stars, solve_ms, fov)
- Shared memory frame buffers (`/dev/shm/efinder_frame_*`)
- Solver database (`solver_db` config key, defaults to
  `/var/lib/efinder/default_database.npz`), including a live load test
- Python library imports (`numpy`, `tetra3`, `picamera2`, `PIL`)
- Process list (efinder, solver_proc, camera_proc, comms_proc)
- Active configuration
- Last 50 lines of the efinder journal

Run this first when anything is broken.

---

## `diag_camera.py` — camera exposure/gain sweep

Captures one frame for every combination of exposure and gain in the
specified ranges and saves each as a PNG. Useful for finding the right
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

After all frames are captured they are bundled into a ZIP archive named
`YYYYMMDDHHMMSSMMM.zip` and the individual PNGs are deleted after a ZIP
integrity check. The archive is the only artifact left in the output
directory, making it easy to transfer off the device.

**The ZIP always contains two extra diagnostic files:**
- `capture_info.txt` — sweep parameters, frame pipeline explanation, hostname,
  Pi model, OS version, live daemon status (backend, test mode, FOV,
  star count, last solve time)
- `efinder.conf` — verbatim copy of `/etc/efinder/efinder.conf` at the time
  of capture

Captured PNGs are raw 8-bit grayscale Y-plane with no display stretch applied,
identical in content to what the solver receives during a live solve.

```bash
# Transfer the archive to a laptop:
scp efinder@efinder.local:/var/lib/efinder/YYYYMMDDHHMMSSMMM.zip .
```

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
   PASS/FAIL against expected values.
2. **System status** — operation mode, power mode, `SYS_STAT`, `SYS_ERR`,
   clock source.
3. **Self-test result** — MCU, gyro, magnetometer, accel (PASS/FAIL each).
4. **Calibration status** — system/gyro/accel/mag, each 0–3 with a bar display.
5. **Live samples** — quaternion + |q| sanity check, Euler, raw/linear accel,
   gravity, gyroscope, magnetometer.

The efinder daemon uses IMUPLUS (accel + gyro only, magnetometer disabled) to
avoid magnetic interference from telescope motors. When the IMU is available,
its quaternion is used to propagate the attitude hint between solves.

---

## `diag_detect.py` — centroid extraction diagnostic

Tests sycamore centroid extraction in isolation with timing and star count.

```bash
sudo .../diag_detect.py                          # live SHM
sudo .../diag_detect.py --image /path/to/img.png # use a saved frame
sudo .../diag_detect.py --sigma 7.0              # override sigma
sudo .../diag_detect.py --reps 10                # more timing repetitions
sudo .../diag_detect.py --sigma-sweep            # sweep sigma 3–12, show star count table
```

Stages:
1. Config + library imports
2. Database load
3. Frame source — live SHM / provided PNG / synthetic star field fallback
4. Extraction timing — N repetitions
5. Sigma sweep (if `--sigma-sweep`)

`--sigma-sweep` is the quickest way to confirm whether `detect_sigma` is
appropriate for your setup. With sycamore's matched-filter gate, sigma 7–8 is
typical. The solver needs at least `min_centroids` stars (default 8) to attempt a solve.

---

## `diag_solve.py` — full pipeline diagnostic

Tests the complete extraction + solve pipeline with per-step timing.

```bash
sudo .../diag_solve.py                           # test images in /opt/efinder/test-images
sudo .../diag_solve.py --image /path/to/img.png  # single image
sudo .../diag_solve.py --live-shm                # live daemon frame
sudo .../diag_solve.py --reps 5
sudo .../diag_solve.py --sigma 7.0 --timeout 2000
sudo .../diag_solve.py --extended-timeout        # retry at 3× when normal fails
```

Two paths are timed per image:

| Path | Call | Description |
|------|------|-------------|
| blind | `detect_stars` + `solve_from_centroids` | **Daemon pipeline (blind)** |
| hint  | same + `attitude_hint` | **Daemon pipeline (seeded)** |

**The blind path is what `solver_proc.py` runs on every frame.**
The hint path is what it runs after the first successful solve (quaternion is
chained from the blind result).

Reports per-step timing (`ext_ms`, `slv_ms`, `total_ms`), solve status,
RA/Dec/roll/FOV/matches, and a summary table across all tested images.

---

## `solve_image.py` — quick single-image solve

Quick one-shot solver. Useful for checking that the database and FOV are
correct before a session.

```bash
sudo .../solve_image.py --image /path/to/image.png
sudo .../solve_image.py --image img.png --sigma 7.0
sudo .../solve_image.py --image img.png --db /var/lib/efinder/mydb.npz
sudo .../solve_image.py --image img.png --fov 13.5 --fov-err 1.0 --timeout 3000
sudo .../solve_image.py --image img.png --reps 5   # timing over multiple reps
```

Uses `detect_stars` + `solve_from_centroids` (sycamore extraction + olive-solve tetra3-py).

Prints RA, Dec, Roll, FOV, and match count on success; suggests corrective
flags on failure.

---

## `bench_pipeline_combos.py` — pipeline benchmark

Benchmarks all solve paths end-to-end with optional sweeps.

```bash
sudo .../bench_pipeline_combos.py --image img.png
sudo .../bench_pipeline_combos.py --live-shm
sudo .../bench_pipeline_combos.py --image img.png --reps 10
sudo .../bench_pipeline_combos.py --image img.png --hint-sweep
sudo .../bench_pipeline_combos.py --image img.png --sigma-sweep
```

| Path | Pipeline | Notes |
|------|----------|-------|
| 1 | `detect_stars` + `solve_from_centroids`, blind | Sycamore daemon pipeline |
| 2 | Same as 1 + `attitude_hint` | Sycamore daemon pipeline + hint |

**`--hint-sweep`** varies `hint_uncertainty_deg` from 0.5° to 30° with both
`strict_hint=False` and `True`. Use this to find the best cone size for your
typical slew speed. The default used by the daemon is 5°.

**`--sigma-sweep`** sweeps sigma 3–12, shows star count and extraction time
at each threshold. Also shows which values exceed `min_centroids` and which
hit `max_solve_stars` (the centroid cap).

---

## `test_hint.py` — attitude-hint effectiveness test

Solves a sequence of shifted star-field images and compares blind vs. hint
solve time. The first image is always a blind solve; subsequent images are
solved twice (blind + hint) so the speedup is directly visible.

```bash
sudo .../test_hint.py --images img1.png img2.png img3.png
sudo .../test_hint.py --images *.png --hint-unc 10
sudo .../test_hint.py --images *.png --sigma 7.0
sudo .../test_hint.py --images img1.png img2.png --db /path/to/db.npz \
                       --fov 13.5 --timeout 2000
```

Output per image:
- Detected star count
- Blind solve: `ext_ms`, `slv_ms`, `total_ms`, RA/Dec
- Hint solve: same fields, plus speedup vs. blind
- Angular separation from image 1

Summary table at the end: per-image blind vs. hint time, speedup, and
average time saved.

The hint chains forward: image 1 seeds image 2, image 2 (if solved) seeds
image 3, and so on — matching the live daemon behaviour.

The default `--hint-unc` is 5°, appropriate for small telescope shifts.
Widen it with e.g. `--hint-unc 15` if the images cover a larger slew.
