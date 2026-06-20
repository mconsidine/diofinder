# Scripts and Tests Usage Guide

This guide covers every script in `tests/` and `scripts/` in the diofinder
repository. On an imaged device the test scripts live at
`/opt/efinder/tests/` and the operational scripts are installed to
`/usr/local/bin/`. Most test scripts require root (for POSIX shared-memory
access) and must be invoked with the efinder venv Python:
`sudo /opt/efinder/venv/bin/python3 tests/<script>.py`. The installed
scripts in `/usr/local/bin/` are plain executables on `PATH`. Three items in
`scripts/` are infrastructure-only or off-device tools: `install.sh` and
`firstboot.sh` are called by the image build / systemd and are never run by
hand; `calibrate_lens.py` runs on a developer laptop, not the Pi.

---

## Diagnostics (`tests/`)

### `diag_services.sh`

Comprehensive system health check. Verifies the `efinder` systemd service
state, pings the maintenance socket at `/run/efinder/maint.sock` for a live
`status` response (solved, stars, solve_ms, fov), checks that all three
`/dev/shm/efinder_frame_*` shared-memory buffers exist, verifies that the
solver database named in `/etc/efinder/efinder.conf` exists and loads
correctly, checks Python library imports (`numpy`, `tetra3`, `picamera2`,
`PIL`), lists the efinder process tree, prints the active configuration, and
tails the last 50 lines of the efinder systemd journal.

**When to use:** First stop when anything is broken; run immediately after
flashing or after a failed OTA update.

**Prerequisites:** `efinder.service` should be running for the full picture
(warnings are issued for anything that is not).

```bash
sudo bash /opt/efinder/tests/diag_services.sh
```

No flags; the config path can be overridden via the environment variable
`EFINDER_CONFIG` (default `/etc/efinder/efinder.conf`) and the socket path
via `EFINDER_MAINT_SOCKET`.

**Typical output:** A series of colour-coded PASS / WARN / FAIL / INFO lines
followed by the last 50 journal lines. All PASS lines on a healthy device.

---

### `diag_camera.py`

Camera exposure/gain sweep diagnostic. Captures one frame for every
combination of exposure and gain in the specified ranges, saves each as a
raw 8-bit greyscale PNG, then bundles everything into a timestamped ZIP
archive (deleting the individual PNGs). The ZIP always includes
`capture_info.txt` (sweep parameters, system info, live daemon status),
`camera_settings.txt` (tuning file, sensor properties, control ranges, all
applied controls), and a copy of `efinder.conf`.

**When to use:** Finding the right exposure and gain before a session;
evaluating read noise vs. sky background; assessing focus quality across
settings; capturing reference frames to transfer off the device.

**Prerequisites:** `picamera2`, `numpy`, `Pillow` (all in the efinder venv);
must run as root or a member of the `video` group on the Pi.

```bash
# Default sweep: exposures 0.05–0.30 s step 0.05, gains 15–40 step 5
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_camera.py

# Custom range with 2×2 software binning
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_camera.py \
    --exp-min 0.1 --exp-max 0.5 --exp-step 0.1 \
    --gain-min 10 --gain-max 30 --gain-step 5 \
    --binning

# Single exposure/gain pair matching solver defaults
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_camera.py \
    --exp-min 0.2 --exp-max 0.2 --gain-min 5 --gain-max 5

# Print camera properties and control ranges without capturing
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_camera.py \
    --info-only
```

| Flag | Default | Description |
|------|---------|-------------|
| `--exp-min` | `0.05` | Minimum exposure in seconds |
| `--exp-max` | `0.30` | Maximum exposure in seconds |
| `--exp-step` | `0.05` | Exposure step in seconds |
| `--gain-min` | `15.0` | Minimum analogue gain |
| `--gain-max` | `40.0` | Maximum analogue gain |
| `--gain-step` | `5.0` | Gain step |
| `--binning` | off | Apply 2×2 software binning (halves saved image size) |
| `--width` | from config or 960 | Frame width in pixels |
| `--height` | from config or 760 | Frame height in pixels |
| `--warmup` | `3` | Frames to discard after each settings change |
| `--output-dir` | where `test.png` lives | Save directory |
| `--tuning-file` | IMX477 scientific profile | libcamera tuning JSON path |
| `--info-only` | off | Print properties/controls and exit without capturing |

Output files are written to `--output-dir` (default `/var/lib/efinder/` when
`test.png` is present there). Transfer the archive with:
```bash
scp efinder@efinder.local:/var/lib/efinder/YYYYMMDDHHMMSSMMM.zip .
```

---

### `diag_bno055.py`

BNO055 IMU sensor diagnostic. Scans `/dev/i2c-1` for a BNO055, verifies the
chip ID, and prints chip identification, system status, self-test results,
calibration status (0–3 bars per axis), and a series of live samples covering
quaternion, Euler angles, raw/linear acceleration, gravity vector, gyroscope,
and magnetometer. Also detects and reports the BCM2835 I2C clock-stretching
bug with a fix suggestion.

**When to use:** Verifying the IMU is wired correctly; confirming the daemon's
IMUPLUS mode assumption matches the sensor; debugging IMU calibration issues.

**Prerequisites:** `smbus2` must be installed (`/opt/efinder/venv/bin/pip
install smbus2`); must run as root.

```bash
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_bno055.py

# Match the efinder daemon operating mode (accel + gyro only, no magnetometer)
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_bno055.py \
    --mode imuplus

# Force alternate I2C address, more samples
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_bno055.py \
    --address 0x29 --samples 20 --interval 0.5
```

| Flag | Default | Description |
|------|---------|-------------|
| `--bus` | `1` | I2C bus number (`/dev/i2c-N`) |
| `--address` | auto-detect 0x28 then 0x29 | Force I2C address |
| `--samples` | `5` | Number of live data samples to print |
| `--interval` | `1.0` | Seconds between samples |
| `--mode` | `ndof` | Op mode: `ndof` (full 9-DOF), `imuplus` (accel+gyro, no mag), `current` (leave as-is) |
| `--no-mode-change` | off | Skip the mode-change step entirely |

Note: the efinder daemon uses IMUPLUS (no magnetometer) to avoid interference
from telescope motors. This script defaults to NDOF so all outputs are
exercised. Pass `--mode imuplus` to match the daemon exactly.

---

### `diag_detect.py`

Centroid extraction diagnostic. Tests sycamore `star_detect` extraction in
isolation: loads config, the solver database, and a frame (live SHM, PNG
file, or synthetic fallback), then runs extraction N times with timing, and
optionally sweeps sigma from 3 to 12 to show how the detection threshold
affects star count.

**When to use:** Verifying that sycamore is installed and working; finding
the right `detect_sigma` for your exposure and sky; confirming the star count
meets `min_centroids` before attempting a full pipeline test.

**Prerequisites:** `star_detect` and `tetra3` installed in the efinder venv;
root required for live SHM access.

```bash
# Live frame from the running daemon
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_detect.py

# Saved frame, custom sigma
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_detect.py \
    --image /var/lib/efinder/captures/frame.png --sigma 7.0

# Sigma sweep (shows star counts at sigma 3–12)
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_detect.py \
    --sigma-sweep
```

| Flag | Default | Description |
|------|---------|-------------|
| `--image` | — | PNG/JPG to use (falls back to live SHM then synthetic) |
| `--sigma` | from config | Detection sigma threshold |
| `--reps` | `6` | Extraction repetitions for timing |
| `--sigma-sweep` | off | Sweep sigma 3–12 and print star-count table |

**Typical output:** Per-rep timing lines (`[N] X.X ms  stars=Y`) followed by
avg/min/max summary. The sigma sweep adds a two-column table; entries that
meet `min_centroids` are annotated.

---

### `diag_solve.py`

Full pipeline diagnostic. Tests the complete sycamore extraction + olive-solve
(tetra3-py) pipeline with per-step timing. For each frame it runs two paths:
Path 1 (blind solve — what `solver_proc.py` runs on every frame) and Path 2
(hint solve — what it runs after the first successful solve). Reports
`ext_ms`, `slv_ms`, `total_ms`, RA/Dec/FOV/matches, and a summary table
across all tested images.

**When to use:** Full pipeline smoke-test; timing the blind vs. hint solve
speedup; confirming the database and FOV settings produce successful solves.

**Prerequisites:** `star_detect`, `tetra3`, `Pillow` in the efinder venv;
test images in `/opt/efinder/test-images/` for the default (no-flag) run;
root for SHM access.

```bash
# Default: loop over test images in /opt/efinder/test-images/
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_solve.py

# Live frame from running daemon
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_solve.py \
    --live-shm

# Single image with custom parameters
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_solve.py \
    --image /var/lib/efinder/captures/frame.png --sigma 7.0 --timeout 2000

# Retry at 3× timeout when the normal solve fails
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_solve.py \
    --image frame.png --extended-timeout
```

| Flag | Default | Description |
|------|---------|-------------|
| `--image` | — | Image file to solve (JPG/PNG) |
| `--live-shm` | off | Read frame from live daemon SHM |
| `--fov` | from config | FOV estimate in degrees |
| `--fov-err` | from config | FOV max error in degrees |
| `--timeout` | from config | Solve timeout in ms |
| `--sigma` | from config | Detection sigma threshold |
| `--reps` | `3` | Repetitions per path per image |
| `--extended-timeout` | off | Retry with 3× timeout when Path 1 produces no match |

---

### `diag_background.py`

Background-mode A/B diagnostic. Runs sycamore detection under all available
background modes (or a user-specified subset), then prints a comparison table:
star count, p50/p95 timing, centroid agreement against the `row_percentile`
baseline (`match`, `base_only`, `mode_only`, `dx_px`). With `--solve`, hands
each mode's centroids to the live daemon solver via the maintenance socket
(memory-safe — the solver reuses its resident database) and adds `solved` /
`Nmatch` columns.

**When to use:** Choosing between background modes for a specific sky
condition; verifying that `top_hat` gains faint stars on a gradient sky
(`--inject-gradient`); confirming that a mode actually produces successful
solves, not just high star counts.

**Prerequisites:** `star_detect` and `tetra3` in the efinder venv; root for
SHM access; `--solve` requires the daemon to be running.

```bash
# Live frame, all modes
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_background.py

# Saved frame with synthetic gradient to stress top_hat
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_background.py \
    --image /var/lib/efinder/captures/frame.png --inject-gradient 40

# Also solve each mode on the live daemon
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_background.py \
    --solve

# Test a specific subset of modes
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_background.py \
    --modes row_percentile,block_percentile,uniform_mean
```

| Flag | Default | Description |
|------|---------|-------------|
| `--image` | — | PNG/JPG to use (falls back to live SHM then synthetic) |
| `--sigma` | from config | Detection sigma threshold |
| `--reps` | `10` | Timing repetitions |
| `--tophat-radius` | from config | Override `detect_tophat_radius` |
| `--block-size` | from config | Override `detect_bg_block_size` (block_percentile tile, px) |
| `--uniform-size` | from config | Override `detect_uniform_filter_size` (uniform_mean window, px) |
| `--modes` | all supported | Comma-separated subset of modes to test |
| `--inject-gradient` | `0.0` | Add a synthetic ramp+vignette of this peak ADU before detection |
| `--solve` | off | Plate-solve each mode's centroids on the live daemon |

The `base_only` column is the most important: stars present in `row_percentile`
but absent in the mode under test indicate the mode is losing real stars.

---

### `solve_image.py`

Quick single-image solver. Loads a PNG or JPEG, runs sycamore extraction
followed by olive-solve (`solve_from_centroids`), and prints RA, Dec, Roll,
FOV, and match count. Useful for verifying database and FOV settings before a
session.

**When to use:** Quick sanity-check on a saved frame; confirming the database
covers the tested sky region; debugging failed solves interactively.

**Prerequisites:** `star_detect`, `tetra3`, `Pillow` in the efinder venv;
root not required unless accessing SHM directly.

```bash
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/solve_image.py \
    --image /var/lib/efinder/captures/frame.png

# Override database and FOV
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/solve_image.py \
    --image frame.png --db /var/lib/efinder/mydb.npz \
    --fov 13.5 --fov-err 1.0 --timeout 3000

# Timing over multiple reps
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/solve_image.py \
    --image frame.png --reps 5
```

| Flag | Default | Description |
|------|---------|-------------|
| `--image` | required | Path to star-field image (JPG/PNG) |
| `--db` | from config | Path to tetra3 `.npz` database |
| `--fov` | from config | FOV estimate in degrees |
| `--fov-err` | from config | FOV max error in degrees |
| `--timeout` | from config | Solve timeout in ms |
| `--sigma` | from config | Detection sigma threshold |
| `--reps` | `1` | Repetitions for timing |

On failure the script prints the status code and suggests corrective flags.

---

## Benchmarks (`tests/`)

### `bench_pipeline_combos.py`

End-to-end pipeline benchmark. Times two solve paths on the same frame: Path 1
(blind: sycamore detection + `solve_from_centroids`) and Path 2 (hint: same
plus the previous solve's quaternion as attitude hint). Prints per-rep timing
and a summary table. Optional sweeps: `--hint-sweep` varies
`hint_uncertainty_deg` from 0.5° to 30° with both `strict_hint=False` and
`True`; `--sigma-sweep` sweeps sigma 3–12 and shows star count vs. threshold.

**When to use:** Measuring total pipeline latency; finding the optimal
`hint_uncertainty_deg` for a given slew speed; confirming performance after
a wheel update.

**Prerequisites:** `star_detect`, `tetra3` in the efinder venv; root for SHM
access (`--live-shm`).

```bash
# Live frame
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/bench_pipeline_combos.py \
    --live-shm

# Saved frame, 10 reps, hint sweep
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/bench_pipeline_combos.py \
    --image /var/lib/efinder/captures/frame.png --reps 10 --hint-sweep

# Sigma sweep
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/bench_pipeline_combos.py \
    --image frame.png --sigma-sweep
```

| Flag | Default | Description |
|------|---------|-------------|
| `--image` | — | Path to sky PNG/JPG |
| `--live-shm` | off | Read from live daemon SHM |
| `--reps` | `5` | Timed repetitions per path |
| `--timeout` | from config | Solve timeout in ms |
| `--sigma` | from config | Override `detect_sigma` |
| `--fov` | from config | Override FOV estimate in degrees |
| `--fov-err` | from config | Override FOV max error in degrees |
| `--hint-unc` | `5.0` | Hint uncertainty cone for Path 2 in degrees |
| `--hint-sweep` | off | Sweep `hint_uncertainty_deg` 0.5°–30° after main table |
| `--sigma-sweep` | off | Sweep sigma 3–12, show detection vs. solve trade-off |

---

### `bench_extractor_compare.py`

Extractor timing benchmark focused on isolation. Times sycamore
`detect_stars` extraction over N reps, then runs a single blind solve and a
single hint solve using the last centroid set, and prints an extraction speed /
star count / solve outcome summary table.

**When to use:** Benchmarking sycamore extraction speed in isolation;
confirming star counts and solve outcomes after changing sigma or binning; a
lighter alternative to `bench_pipeline_combos.py` when you only need one pass.

**Prerequisites:** Same as `bench_pipeline_combos.py`.

```bash
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/bench_extractor_compare.py \
    --image /var/lib/efinder/captures/frame.png

sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/bench_extractor_compare.py \
    --live-shm --reps 10 --sigma 7.0
```

| Flag | Default | Description |
|------|---------|-------------|
| `--image` | — | Path to sky PNG/JPG |
| `--live-shm` | off | Read from live daemon SHM |
| `--reps` | `5` | Timed repetitions per extractor |
| `--sigma` | from config | Override `detect_sigma` |
| `--timeout` | from config | Solve timeout in ms |
| `--fov` | from config | Override FOV estimate in degrees |
| `--fov-err` | from config | Override FOV max error in degrees |
| `--hint-unc` | `5.0` | Hint uncertainty cone for hint solve in degrees |

---

### `test_hint.py`

Attitude-hint effectiveness test across a sequence of images. The first image
is always a blind solve; subsequent images are solved twice (blind and hint)
so the speedup is directly visible. The hint chains forward image to image,
matching live daemon behaviour. Prints per-image timing and a summary table
with per-image blind/hint times, speedup, and angular separation from image 1.

**When to use:** Quantifying hint speedup on a real observing sequence;
choosing the right `--hint-unc` before a session; verifying hint effectiveness
after a solver or database update.

**Prerequisites:** `star_detect`, `tetra3` in the efinder venv; image files
must exist on disk (not live SHM).

```bash
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/test_hint.py \
    --images img1.png img2.png img3.png

# Wider hint cone for large slews
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/test_hint.py \
    --images *.png --hint-unc 15

# Explicit database and FOV
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/test_hint.py \
    --images img1.png img2.png \
    --db /var/lib/efinder/mydb.npz --fov 13.5 --timeout 2000
```

| Flag | Default | Description |
|------|---------|-------------|
| `--images` | required (one or more) | Image files in sequence order |
| `--db` | from config | tetra3 `.npz` database path |
| `--fov` | from config | FOV estimate in degrees |
| `--fov-err` | from config | FOV max error in degrees |
| `--timeout` | from config | Solve timeout in ms |
| `--sigma` | from config | Detection sigma threshold |
| `--hint-unc` | `5.0` | Hint uncertainty cone in degrees |

---

## Unit Tests (`tests/`)

### `test_seeing_hotpixel.py`

Pure-logic unit tests for the seeing presets and hot-pixel neighbor-median
repair. Does not require `star_detect` (sycamore) or `picamera2` — those
modules are stubbed before import. Contains three test classes:

- `SeeingPresetTests`: validates preset values, deep-DB fallback, drift
  detection, and effective-value resolution.
- `HotPixelTests`: validates hot-pixel detection, 8-neighbor repair for
  interior, edge, and adjacent-pixel cases, mask save/load round-trip, and
  shape-mismatch no-op.
- `MaintSeeingDispatchTests`: exercises `seeing_set`, `seeing_get`,
  `solver_params_set`, and `match_params_set` via the comms_proc maint
  dispatcher with a fake IPC context (no real solver/camera processes).

**When to use:** CI and after any changes to `efinder/seeing.py`,
`efinder/hot_pixel.py`, or the corresponding maint command handlers.

**Prerequisites:** numpy; no hardware, no sycamore, no picamera2.

```bash
# Preferred: via unittest module (runs from repo root, works in venv or system Python)
python3 -m unittest tests.test_seeing_hotpixel -v

# Or directly:
python3 /opt/efinder/tests/test_seeing_hotpixel.py
```

No command-line flags beyond the standard `unittest` options.

**Expected output:** All tests in `SeeingPresetTests`, `HotPixelTests`, and
`MaintSeeingDispatchTests` should pass (`OK`). The total count is printed at
the end.

---

### `replay_corpus.py`

Off-device regression-corpus replay harness.  Runs the diofinder detect+solve
pipeline (sycamore extraction + olive-solve) on a directory of labeled PNG
frames, sweeping over seeing presets and optionally over background modes, and
reports solve rate, star count, and timing aggregates.  Designed to run on a
developer laptop where the `star_detect` and `tetra3` wheels are installed —
**no Pi, no daemon, no shared memory required**.

**When to use:**
- After changing a seeing preset, extractor wheel, or solver database: run the
  corpus to confirm solve rate did not regress.
- Before a software release: compare Good vs. Bad preset solve rates on a
  representative set of sky conditions.
- Tuning `detect_sigma`, `detect_bg_mode`, or matching parameters: sweep
  `--bg-modes` to find which combination wins on your corpus.
- Building the corpus itself: transfer saved frames from the Pi with
  `scp efinder@efinder.local:/var/lib/efinder/captures/*.png tests/corpus/`.

**Prerequisites:** `star_detect` and `tetra3` wheels installed in your Python
environment; `numpy` and `Pillow`; a tetra3 `.npz` database.  The script
fails gracefully with a clear install/usage message if any of these are missing
(no raw tracebacks).  Running with `--help` or checking syntax with
`python3 -m py_compile` works without the wheels.

```bash
# Full run on a labeled corpus, both presets
python3 tests/replay_corpus.py \
    --corpus tests/corpus/ \
    --database /var/lib/efinder/default_database.npz

# Quick check: first 20 frames, good preset only
python3 tests/replay_corpus.py \
    --corpus tests/corpus/ \
    --database /var/lib/efinder/default_database.npz \
    --presets good \
    --limit 20

# Sweep background modes and write per-frame CSV
python3 tests/replay_corpus.py \
    --corpus tests/corpus/ \
    --database /var/lib/efinder/default_database.npz \
    --bg-modes row_percentile,block_percentile,uniform_mean \
    --csv /tmp/bg_sweep.csv

# Non-default FOV (e.g. after a lens change)
python3 tests/replay_corpus.py \
    --corpus tests/corpus/ \
    --database /var/lib/efinder/default_database.npz \
    --fov 10.0
```

| Flag | Default | Description |
|------|---------|-------------|
| `--corpus` | required | Directory of PNG frames (flat or labeled subdirs) |
| `--database` | `/var/lib/efinder/default_database.npz` | tetra3 `.npz` solver database |
| `--presets` | `good,bad` | Comma-separated list of seeing presets to run |
| `--bg-modes` | preset's value | Comma-separated bg modes to additionally sweep (overrides preset's `detect_bg_mode`) |
| `--csv` | — | Path for per-frame CSV output |
| `--fov` | from config (13.5°) | FOV estimate in degrees |
| `--limit` | `0` (no limit) | Cap frame count for a quick run |

**Output:** Per-frame progress lines followed by an aggregate table showing
frames, solved count, solve rate %, star count (p50/p90), extract time (p50/p90),
and solve time (p50/p90) for each preset×bg_mode combination.  If the corpus
contains multiple labels a second table breaks the results down by label.

**Corpus layout and labeling** is documented in `tests/corpus/README.md`.
In brief: frames in a named subdirectory inherit the subdirectory name as their
label; frames in the root use the `{ts}_{label}.png` filename convention from
the daemon's save-frame feature; unlabeled frames get label `"unlabeled"`.

**Preset fidelity:** Presets are loaded live from `efinder.seeing.SEEING_PRESETS`
(importing `efinder/seeing.py` from the repo root) so the harness stays in
sync with the daemon's actual preset table automatically.  A fallback hard-coded
copy is used only if the `efinder` package is not importable (dev-box without
a full install), with a warning.

**Capability probing:** The harness probes the installed `star_detect` wheel for
`kernel_sigma`, `local_noise`, `noise_mode`, `bg_block_size`, `uniform_filter_size`,
and `tophat_radius` exactly as `efinder/bg_cache.py` does, using
`inspect.signature`.  Unsupported kwargs are silently omitted, so the same
script works on sycamore 0.11 and 0.12 wheels.

---

## Regression Corpus Workflow

The corpus + replay harness is the measurement tool that makes extractor and
preset tuning data-driven rather than on-sky anecdote.

### Building the corpus

1. On the Pi, enable frame saving:

   ```bash
   # In /etc/efinder/efinder.conf
   save_failed_frames: true
   save_solved_frames: true
   ```

   Or toggle per-session via the web UI Camera page.

2. Observe: let the daemon run for a session or two across different conditions.
   Frames accumulate in `/var/lib/efinder/captures/`.

3. Copy frames off the Pi into labeled subdirectories:

   ```bash
   mkdir -p tests/corpus/clear_dark tests/corpus/moonlit

   # All solved frames from a clear-sky night
   scp efinder@efinder.local:'/var/lib/efinder/captures/*solved*' \
       tests/corpus/clear_dark/

   # Failed frames from a moonlit session
   scp efinder@efinder.local:'/var/lib/efinder/captures/*failed*' \
       tests/corpus/moonlit/
   ```

4. Optionally keep a flat `tests/corpus/` for quick one-off tests; files named
   `{ts}_{label}.png` (the daemon's convention) auto-label themselves.

### Running a preset A/B

```bash
# Before changing a preset:
python3 tests/replay_corpus.py \
    --corpus tests/corpus/ \
    --database /var/lib/efinder/default_database.npz \
    --csv /tmp/before.csv

# After changing efinder/seeing.py:
python3 tests/replay_corpus.py \
    --corpus tests/corpus/ \
    --database /var/lib/efinder/default_database.npz \
    --csv /tmp/after.csv

# Compare solve rates manually or with any CSV tool.
```

### Running a background-mode sweep

```bash
python3 tests/replay_corpus.py \
    --corpus tests/corpus/ \
    --database /var/lib/efinder/default_database.npz \
    --bg-modes row_percentile,line_median,block_percentile,uniform_mean,top_hat \
    --presets good \
    --csv /tmp/bg_sweep.csv
```

The aggregate table shows solve rate and star counts per mode.  The mode with
the highest solve rate on your corpus — particularly on the `moonlit` or
`gradient` label — is a strong candidate for that condition's seeing preset.

### Checking for regressions after a wheel update

```bash
# After installing a new sycamore or olive-solve wheel:
python3 tests/replay_corpus.py \
    --corpus tests/corpus/ \
    --database /var/lib/efinder/default_database.npz
```

The script prints both wheel versions at the top of its output.  A solve rate
drop of more than a few percent is a regression worth investigating with
`diag_background.py` on a specific failing frame.

---

## Operational Scripts (`scripts/`, installed to `/usr/local/bin/`)

### `efinder-ctl`

CLI wrapper for the maintenance socket at `/run/efinder/maint.sock`. Provides
subcommands for inspecting and controlling a running daemon. Requires
membership in the `efinder` group (or root):
`sudo usermod -a -G efinder $USER` then re-login.

```bash
efinder-ctl status
efinder-ctl ping
efinder-ctl version

efinder-ctl boresight show
efinder-ctl boresight center
efinder-ctl boresight set 380 480    # Y X in pixels

efinder-ctl calibration status
efinder-ctl calibration reset        # prompts for confirmation
efinder-ctl calibration reset -y     # skip confirmation

efinder-ctl exposure get
efinder-ctl exposure set 0.3
efinder-ctl exposure set 0.3 --persist   # also write to efinder.conf

efinder-ctl gain set 20
efinder-ctl gain set 20 --persist

efinder-ctl seeing get
efinder-ctl seeing set good
efinder-ctl seeing set bad

efinder-ctl polar start
efinder-ctl polar status
efinder-ctl polar cancel
efinder-ctl polar set-latitude 45.0  # degrees

efinder-ctl raw '{"cmd":"status","args":{}}'
```

| Subcommand | Description |
|-----------|-------------|
| `status` | Current solution summary and config |
| `ping` | Check daemon is responsive |
| `version` | Show daemon version |
| `boresight show/center/set Y X` | Inspect or adjust the boresight pixel |
| `calibration status/reset [-y]` | FOV calibration state; reset discards committed values |
| `exposure get/set SECONDS [--persist]` | Read or change exposure |
| `gain set GAIN [--persist]` | Change analogue gain |
| `seeing get/set good\|bad` | Apply the Good/Bad seeing preset |
| `polar start/status/cancel/set-latitude` | Polar alignment workflow |
| `raw JSON` | Send any raw JSON request to the maint socket |

---

### `efinder-bg-setup`

Show or live-change the background compensation mode via the maintenance
socket. Changes take effect immediately without a restart. Use `--persist` to
also write the new value to `/etc/efinder/efinder.conf`.

```bash
# Show current background settings
efinder-bg-setup

# Enable top-hat with a custom radius
efinder-bg-setup set top_hat --radius 14

# Revert to default
efinder-bg-setup set row_percentile

# Persist the change
efinder-bg-setup set block_percentile --persist
```

| Subcommand / Flag | Default | Description |
|-------------------|---------|-------------|
| (no subcommand) | — | Show current `detect_bg_mode` and related config keys |
| `set MODE` | — | Change to the named mode (see mode list below) |
| `--radius N` | `12` | Top-hat structuring-element radius in pixels (only for `top_hat`) |
| `--persist` | off | Write the new value to `efinder.conf` |

Valid modes: `row_percentile`, `line_median`, `column_percentile`,
`row_column_percentile`, `block_percentile`, `uniform_mean`, `top_hat`.

---

### `efinder-bg-test`

On-device background-mode A/B test. Loads PNG frames from a directory (or
grabs one live frame via shared memory), runs detection under all or a
specified subset of background modes, and prints a comparison table of star
counts, timing, and centroid deviation from the `row_percentile` baseline.
With `--solve`, also plate-solves each mode's centroids on the live daemon
(memory-safe — the solver reuses its resident database, no second copy loaded).

```bash
# Test on saved frames in the default captures directory
sudo efinder-bg-test --dir /var/lib/efinder/captures

# Live frame
sudo efinder-bg-test --live

# Inject a vertical gradient (stress-tests top_hat vs row_percentile)
sudo efinder-bg-test --live --inject-gradient 40

# Also solve and compare solve outcomes
sudo efinder-bg-test --live --solve

# Test specific modes with non-default parameters
sudo efinder-bg-test --live \
    --modes row_percentile,block_percentile,uniform_mean \
    --sigma 8 --bin 2
```

| Flag | Default | Description |
|------|---------|-------------|
| `--dir PATH` | `/var/lib/efinder/captures` | Directory of PNG/JPG frames to test |
| `--live` | off | Grab one frame from live shared memory (mutually exclusive with `--dir`) |
| `--inject-gradient DN` | `0` | Add a vertical brightness ramp of this many DN to each frame |
| `--sigma` | `5.0` | Detection sigma |
| `--bin` | `2` | Binning: 1 or 2 |
| `--tophat-radius` | `12` | Structuring-element radius for `top_hat` |
| `--block-size` | `0` (sycamore default) | Tile side for `block_percentile` |
| `--uniform-size` | `0` (sycamore default) | Window side for `uniform_mean` |
| `--modes` | all | Comma-separated subset of modes to test |
| `--solve` | off | Also plate-solve each mode on the live daemon |
| `--max-stars` | `50` | Cap centroids sent to the solver |

---

### `efinder-update`

OTA update script. Pulls the latest application code from git, refreshes the
olive-solve (`tetra3`) and sycamore-extract (`star_detect`) wheels from their
GitHub releases, and restarts the service. Refuses to update if the working
tree has local modifications. A wheel placed manually in
`/opt/efinder/vendor/wheels/` overrides the download (local testing path).

```bash
sudo efinder-update                   # latest release tag
sudo efinder-update v0.8.1            # specific tag
sudo efinder-update --ref main        # track a branch (testing)
```

| Argument | Default | Description |
|----------|---------|-------------|
| (positional, optional) | `latest` | Specific tag to check out |
| `--ref BRANCH` | — | Track a branch or check out a tag/commit by ref |

**Prerequisites:** The `/opt/efinder/` directory must be a git repository
(images provisioned via `install.sh` satisfy this). Requires internet access
to reach GitHub. The webui Update page wraps this script.

---

### `efinder-db-update`

Solver database updater. Downloads `diofinder_13deg.npz` from the
`mconsidine/astro_databases` GitHub releases, verifies the SHA-256 against
the release manifest, installs it over the database named by `solver_db` in
`/etc/efinder/efinder.conf` (backing up the old file as `.bak`), and
restarts the service. If the release also carries
`diofinder_13deg_mag85.npz` (a deeper G≤8.5 database for the Bad-seeing
preset), that is downloaded and installed to
`/var/lib/efinder/diofinder_13deg_mag85.npz` as well.

```bash
sudo efinder-db-update               # latest release
sudo efinder-db-update v2026.06      # specific release tag
```

| Argument | Default | Description |
|----------|---------|-------------|
| (positional, optional) | `latest` | Specific release tag |

**Note on the deep database:** After installing, set `star_db_deep:
/var/lib/efinder/diofinder_13deg_mag85.npz` in `/etc/efinder/efinder.conf`
and apply the Bad preset (`efinder-ctl seeing set bad`) to enable it. The
script prints a reminder if `star_db_deep` is not yet configured.

---

### `ap.sh`

Switch the eFinder's Wi-Fi interface to access-point mode. Creates or updates
the `efinder-ap` NetworkManager profile and activates it. The Pi then
advertises a WPA2 AP at IP `10.42.0.1`; connect with
`ssh efinder@10.42.0.1` or `ssh efinder@efinder.local`.

```bash
sudo ap.sh                       # activate existing efinder-ap profile
sudo ap.sh "MySSID" "MyPassword" # change SSID/password and activate
```

Password must be 8+ characters. Prints the active SSID, password, and IP
after activation.

---

### `station.sh`

Switch the eFinder's Wi-Fi from AP mode to station (client) mode. Connects to
a named network or interactively scans and presents a menu. The USB tether
(`10.55.0.1`) remains up while Wi-Fi switches, so you can run this command
over USB and keep working after the switch.

```bash
sudo station.sh                            # interactive scan and menu
sudo station.sh "HomeNetwork" "password"   # non-interactive
```

On connection failure the script automatically returns to AP mode so the
device remains accessible.

---

## Off-Device Tools

### `scripts/calibrate_lens.py`

**Runs on a developer laptop, not the Pi.** Given a directory of solved-frame
PNGs (e.g. captures saved with `save_solved_frames: true` then copied off the
device), it extracts star centroids and calls `tetra3rs`'s `calibrate_camera`
to fit SIP distortion across all solvable frames. Prints the dominant radial-k
coefficient and the exact `distortion:` line to set in `efinder.conf`.

```bash
# On the dev box, not the Pi
pip install tetra3rs numpy pillow

python3 scripts/calibrate_lens.py \
    --images /path/to/solved-frame-pngs/ \
    --db /path/to/gaia_db.bin \
    --fov 13.6
```

| Flag | Default | Description |
|------|---------|-------------|
| `--images` | required | Directory of solved-frame PNGs (or a glob) |
| `--db` | required | tetra3rs solver database (`.bin`) |
| `--fov` | `13.6` | Nominal horizontal FOV in degrees |
| `--sigma` | `5.0` | Extraction sigma |
| `--max-images` | `20` | Cap the number of frames used |

Run this once after changing the lens or camera mode. Then copy the printed
value to the Pi:

```bash
# Example output:
#   distortion: 0.000123
# Apply on the Pi:
sudo sed -i 's/^distortion:.*/distortion: 0.000123/' /etc/efinder/efinder.conf
sudo systemctl restart efinder
```

---

### `scripts/install.sh` and `scripts/firstboot.sh`

These are image-build and boot-time infrastructure scripts. `install.sh` is
run during image build (chroot mode) or by a user on a freshly flashed card
(fresh mode); it sets up the Python venv, installs dependencies, and
provisions the git repository for OTA. `firstboot.sh` is called on every boot
by `efinder-firstboot.service`; it recreates the AP NetworkManager profile if
missing and performs hardware sanity checks. Neither script is intended to be
invoked by hand during normal operation.

### `scripts/efinder-gadget-connect`

Called by the `efinder-gadget.service` systemd unit at boot to configure the
USB CDC ACM serial gadget via configfs (Pi OS Trixie kernel). Makes the Pi
appear as `/dev/ttyACM0` (Linux/macOS) or a COM port (Windows) for serial
access. Not normally invoked by hand.

### `scripts/efinder-set-time`

Called by `comms_proc` when SkySafari sends a time-sync command over LX200.
Briefly disables `systemd-timesyncd`, calls `timedatectl set-time`, then
re-enables NTP. Not intended for direct use.

---

## Common Workflows

### Post-flash smoke test

After imaging a new card or after `efinder-update`, verify the system end to
end:

```bash
# 1. Check all processes and libraries are healthy
sudo bash /opt/efinder/tests/diag_services.sh

# 2. One-shot solve with the current camera frame
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_solve.py --live-shm

# 3. Pipeline timing
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/bench_pipeline_combos.py \
    --live-shm
```

### Seeing-preset A/B

Apply a preset and confirm the live parameters changed:

```bash
# Apply Good preset
efinder-ctl seeing set good

# Confirm current effective values and drift
efinder-ctl seeing get

# Apply Bad preset (uses deeper DB if configured, widens kernel, lowers sigma)
efinder-ctl seeing set bad

# Verify the solver still produces successful solves under the new settings
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_solve.py --live-shm
```

### Background-mode A/B

Compare background modes on a live or saved frame, including plate-solve
outcomes:

```bash
# Using the installed script (on saved captures)
sudo efinder-bg-test --dir /var/lib/efinder/captures --solve

# Using the test script (live frame, with injected gradient to stress top_hat)
sudo /opt/efinder/venv/bin/python3 /opt/efinder/tests/diag_background.py \
    --inject-gradient 40 --solve

# If a mode wins, switch to it live and persist
efinder-bg-setup set block_percentile --persist
```

### Hot-pixel dark capture

With the lens cap on and the service running (it uses the camera in test-mode
during capture):

```bash
# Trigger via efinder-ctl (routes through the maint socket)
efinder-ctl raw '{"cmd":"dark_capture","args":{"frames":20}}'

# Check the result
efinder-ctl raw '{"cmd":"hot_pixel_status","args":{}}'
```

The mask is saved to `/var/lib/efinder/hot_pixel_mask.npz` and loaded
automatically on the next restart. The Camera page in the web UI also exposes
a "Capture dark frame" button.

### Database update including the deep-magnitude database

```bash
# Fetch the latest standard + deep databases and restart
sudo efinder-update                # update code first
sudo efinder-db-update             # then update the databases

# If the deep database was downloaded, enable it for the Bad preset:
sudo sed -i 's|^#*star_db_deep:.*|star_db_deep: /var/lib/efinder/diofinder_13deg_mag85.npz|' \
    /etc/efinder/efinder.conf
sudo systemctl restart efinder
efinder-ctl seeing set bad         # apply the Bad preset, which resolves star_db_deep
```

### OTA software update

```bash
# Update to latest release
sudo efinder-update

# Update to a specific tag
sudo efinder-update v0.9.0

# Track the main branch (for testing pre-release code)
sudo efinder-update --ref main
```

The web UI's **Update** page wraps `efinder-update` and streams its output in
the browser.
