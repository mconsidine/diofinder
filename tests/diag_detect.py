#!/usr/bin/env python3
"""
diofinder centroid-extraction diagnostic.

Tests sycamore star_detect extraction in isolation: timing, star count, and
sigma sensitivity.  No external server required.

Stages
  0. Config + library imports
  1. Database load
  2. Frame source  (live SHM | PNG file | synthetic fallback)
  3. Extraction timing  (N reps)
  4. Sigma sweep  (--sigma-sweep)

Usage:
  # Live frame from running daemon:
  sudo /opt/diofinder/venv/bin/python3 tests/diag_detect.py

  # Saved capture, custom sigma:
  sudo /opt/diofinder/venv/bin/python3 tests/diag_detect.py \\
      --image /path/to/frame.png --sigma 7.0

  # Sigma sweep:
  sudo /opt/diofinder/venv/bin/python3 tests/diag_detect.py --sigma-sweep
"""

import argparse
import pathlib
import sys
import time

sys.path.insert(0, '/opt/diofinder')

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"
INFO = "\033[34mINFO\033[0m"


def tag(label, msg): print(f"  [{label}] {msg}")
def sep(title):      print(f"\n{'='*62}\n{title}\n{'='*62}")


# ── Args ──────────────────────────────────────────────────────────────────────
ap = argparse.ArgumentParser(description=__doc__,
                              formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument('--image', metavar='PATH', help='PNG/JPG to use as test frame')
ap.add_argument('--sigma', type=float,     help='Override detect_sigma from config')
ap.add_argument('--reps',  type=int, default=6,
                help='Extraction repetitions (default 6)')
ap.add_argument('--sigma-sweep', action='store_true',
                help='Sweep sigma 3–12 and show star count table')
args = ap.parse_args()


# ── Stage 0: Config & imports ─────────────────────────────────────────────────
sep('Stage 0: Config & library imports')

try:
    from diofinder.config import load_config
    cfg     = load_config()
    db_raw  = cfg.solver_db
    db_path = pathlib.Path(db_raw if db_raw.startswith('/')
                           else f'/var/lib/diofinder/{db_raw}.npz')
    sigma   = args.sigma if args.sigma is not None else cfg.detect_sigma
    min_c   = cfg.min_centroids
    max_c   = cfg.max_solve_stars
    W, H    = cfg.frame_width, cfg.frame_height
    tag(PASS, cfg.summary())
except Exception as e:
    tag(WARN, f'Config unavailable ({e}); using defaults')
    db_path = pathlib.Path('/var/lib/diofinder/default_database.npz')
    sigma   = args.sigma if args.sigma is not None else 7.0
    min_c   = 8
    max_c   = 50
    W, H    = 960, 760

# This is an extraction-only diagnostic: fov_max_error_deg is a solve-stage
# parameter and does not apply here. max_solve_stars is the cap solver_proc
# applies to the detection list before solving, so we surface it for context.
tag(INFO, f'sigma={sigma:.1f}  frame={W}x{H}  min_centroids={min_c}  '
          f'max_solve_stars={max_c}')
tag(INFO, f'db={db_path}')

try:
    import numpy as np
    tag(PASS, f'numpy {np.__version__}')
except ImportError as e:
    tag(FAIL, str(e)); sys.exit(1)

try:
    import star_detect as _sd
    _sd.set_num_threads(2)
    tag(PASS, 'sycamore star_detect')
except ImportError as e:
    tag(FAIL, f'sycamore star_detect not installed: {e}')
    sys.exit(1)


# ── Stage 1: Database load ────────────────────────────────────────────────────
sep('Stage 1: Database load')

if not db_path.exists():
    tag(FAIL, f'Database not found: {db_path}')
    sys.exit(1)

try:
    import tetra3
    t0      = time.monotonic()
    t3      = tetra3.Tetra3(str(db_path))
    load_ms = (time.monotonic() - t0) * 1000
    tag(PASS, f'Loaded {db_path.name} in {load_ms:.0f} ms')
except ImportError as e:
    tag(FAIL, f'tetra3 not installed: {e}'); sys.exit(1)


# ── Stage 2: Frame source ─────────────────────────────────────────────────────
sep('Stage 2: Frame source')

frame = None

if args.image:
    try:
        from PIL import Image as PILImage
        img   = PILImage.open(args.image).convert('L')
        frame = np.array(img, dtype=np.uint8)
        if frame.shape != (H, W):
            tag(INFO, f'Resizing {frame.shape[1]}x{frame.shape[0]} → {W}x{H}')
            frame = np.array(img.resize((W, H)), dtype=np.uint8)
        tag(PASS, f'{pathlib.Path(args.image).name}: {W}x{H}  '
                  f'peak={frame.max()}  mean={frame.mean():.1f}  '
                  f'p95={int(np.percentile(frame, 95))}')
    except Exception as e:
        tag(FAIL, f'Cannot load image: {e}'); sys.exit(1)

else:
    try:
        from multiprocessing import shared_memory, resource_tracker as _rt
        from diofinder.frame_slots import SHM_PREFIX, NUM_BUFFERS
        for i in range(NUM_BUFFERS):
            try:
                shm = shared_memory.SharedMemory(name=f'{SHM_PREFIX}_{i}', create=False)
                try: _rt.unregister(shm._name, 'shared_memory')
                except Exception: pass
                frame = np.array(np.ndarray((H, W), dtype=np.uint8, buffer=shm.buf))
                shm.close()
                tag(PASS, f'Live SHM slot {i}: {W}x{H}  '
                          f'peak={frame.max()}  mean={frame.mean():.1f}')
                if frame.max() < 20:
                    tag(WARN, 'Peak < 20 — solver_proc would skip this frame. '
                              'Check exposure/gain.')
                break
            except Exception:
                continue
    except Exception:
        pass

if frame is None:
    tag(WARN, 'No live SHM found — generating synthetic star field')
    tag(INFO, '  (Timing and star counts are valid; blind plate solve will fail)')
    rng   = np.random.default_rng(42)
    frame = np.zeros((H, W), dtype=np.uint8)
    for _ in range(50):
        cy, cx = int(rng.integers(15, H - 15)), int(rng.integers(15, W - 15))
        br     = int(rng.integers(100, 230))
        ys, xs = np.ogrid[-6:7, -6:7]
        patch  = (br * np.exp(-(ys**2 + xs**2) / 5.0)).astype(np.uint8)
        y0, y1 = max(0, cy - 6), min(H, cy + 7)
        x0, x1 = max(0, cx - 6), min(W, cx + 7)
        frame[y0:y1, x0:x1] = np.maximum(
            frame[y0:y1, x0:x1], patch[:y1 - y0, :x1 - x0])
    tag(INFO, f'Synthetic: 50 Gaussian stars  peak={frame.max()}')


# ── Extraction helper ─────────────────────────────────────────────────────────

def _extract(f, sig):
    """Return (centroids_rowcol, n_stars, elapsed_ms) using sycamore detect_stars."""
    t0  = time.monotonic()
    raw = _sd.detect_stars(f, sigma=sig, bin=1, centroid_full_res=True)
    ms  = (time.monotonic() - t0) * 1000
    n   = len(raw) if raw else 0
    # sycamore returns (x=col, y=row); tetra3 expects (row, col)
    cent = (np.array([[s[1], s[0]] for s in raw], dtype=np.float64)
            if raw else None)
    return cent, n, ms


# ── Stage 3: Extraction timing ────────────────────────────────────────────────
sep(f'Stage 3: sycamore detect_stars (matched_filter)  sigma={sigma:.1f}  reps={args.reps}')
tag(INFO, 'Same call used by the daemon (solver_proc.py)')

times  = []
last_n = 0
for i in range(args.reps):
    _, n, ms = _extract(frame, sigma)
    times.append(ms)
    last_n = n
    lbl = PASS if n >= min_c else WARN
    cap = f'  (solver caps to {max_c})' if n > max_c else ''
    tag(lbl, f'[{i+1:2d}] {ms:6.1f} ms  stars={n:3d}{cap}')

if times:
    avg = sum(times) / len(times)
    print(f'\n  Timing: avg={avg:.1f} ms  min={min(times):.1f} ms  '
          f'max={max(times):.1f} ms  ({len(times)}/{args.reps} ok)')

if last_n < min_c:
    print()
    tag(WARN, f'Only {last_n} stars detected (need {min_c} to solve).')
    tag(WARN, f'  Try --sigma lower than {sigma:.1f}, longer exposure, '
              'or check image quality.')


# ── Stage 4: Sigma sweep ──────────────────────────────────────────────────────
if args.sigma_sweep:
    sep('Stage 4: Sigma sweep  (sigma 3 → 12)')
    print(f'  {"sigma":>6}  {"stars":>6}  {"ms":>7}')
    print(f'  ' + '-' * 24)
    for sig in [3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 12.0]:
        try:
            _, n, ms = _extract(frame, sig)
            note = ' ← min_centroids met' if n >= min_c else ''
            cur  = ' ← current' if abs(sig - sigma) < 0.05 else ''
            cap  = f' (capped to {max_c})' if n > max_c else ''
            print(f'  {sig:6.1f}  {n:6d}  {ms:7.1f}{note or cur}{cap}')
        except Exception as e:
            print(f'  {sig:6.1f}  ERROR: {e}')
    print(f'\n  min_centroids={min_c}  max_solve_stars={max_c}  '
          f'current sigma={sigma:.1f}')

print()
