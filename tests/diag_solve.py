#!/usr/bin/env python3
"""
eFinder olive-solve diagnostic.

Tests the olive-solve (tetra3-py) pipeline with per-call timing.
No cedar-detect, gRPC, or external server required.

Two solve paths are timed for each image:
  A) solve_from_image_fast (u8)  -- single Rust call, extract + solve
  B) get_centroids_from_image (f32) + solve_from_centroids -- split pipeline

Usage:
  # Loop over installed test images (default):
  sudo /opt/efinder/venv/bin/python3 tests/diag_solve.py

  # Specific image:
  sudo /opt/efinder/venv/bin/python3 tests/diag_solve.py --image /path/to/image.jpg

  # Live frame from running daemon:
  sudo /opt/efinder/venv/bin/python3 tests/diag_solve.py --live-shm

  # Override FOV:
  sudo /opt/efinder/venv/bin/python3 tests/diag_solve.py --fov 13.5 --fov-err 1.0
"""

import argparse
import pathlib
import sys
import time

sys.path.insert(0, '/opt/efinder')

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"
INFO = "\033[34mINFO\033[0m"

TEST_IMAGES_DIR = pathlib.Path('/opt/efinder/test-images')


def tag(label, msg): print(f"  [{label}] {msg}")
def sep(title):      print(f"\n{'='*62}\n{title}\n{'='*62}")


# ── Args ──────────────────────────────────────────────────────────────────────
ap = argparse.ArgumentParser(description=__doc__,
                              formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument('--image', metavar='PATH', help='Image file to solve (JPG/PNG)')
ap.add_argument('--live-shm', action='store_true',
                help='Read from live efinder daemon SHM')
ap.add_argument('--fov',     type=float, help='FOV estimate in degrees')
ap.add_argument('--fov-err', type=float, help='FOV max error in degrees')
ap.add_argument('--timeout', type=int,   help='Solve timeout in ms')
ap.add_argument('--reps',    type=int,   default=3,
                help='Repetitions per solve path per image (default 3)')
args = ap.parse_args()

# ── Stage 0: Config & imports ─────────────────────────────────────────────────
sep('Stage 0: Config & imports')

try:
    from efinder.config import load_config
    cfg     = load_config()
    db_path = pathlib.Path(cfg.solver_db if cfg.solver_db.startswith('/')
                           else f'/var/lib/efinder/{cfg.solver_db}.npz')
    fov     = args.fov     if args.fov     else cfg.fov_deg
    fov_err = args.fov_err if args.fov_err else cfg.fov_max_error_deg
    timeout = args.timeout if args.timeout else cfg.solve_timeout_ms
    sigma   = cfg.detect_sigma
    W, H    = cfg.frame_width, cfg.frame_height
    tag(PASS, cfg.summary())
except Exception as e:
    tag(WARN, f'Config unavailable ({e}); using defaults')
    db_path = pathlib.Path('/var/lib/efinder/default_database.npz')
    fov     = args.fov     if args.fov     else 13.5
    fov_err = args.fov_err if args.fov_err else 1.0
    timeout = args.timeout if args.timeout else 1500
    sigma   = 9.0
    W, H    = 960, 760

tag(INFO, f'db={db_path}')
tag(INFO, f'FOV={fov:.2f}° ±{fov_err:.2f}°  timeout={timeout}ms  sigma={sigma}')

try:
    import numpy as np
    tag(PASS, f'numpy {np.__version__}')
except ImportError as e:
    tag(FAIL, str(e)); sys.exit(1)

try:
    from PIL import Image as PILImage
    tag(PASS, 'Pillow')
except ImportError as e:
    tag(FAIL, str(e)); sys.exit(1)

try:
    import tetra3
    if not hasattr(tetra3.Tetra3, 'solve_from_image_fast'):
        tag(FAIL, 'solve_from_image_fast missing -- wrong tetra3 installed')
        sys.exit(1)
    tag(PASS, 'tetra3 (olive-solve)')
except ImportError as e:
    tag(FAIL, f'tetra3 not installed: {e}'); sys.exit(1)

# ── Stage 1: Database load ────────────────────────────────────────────────────
sep('Stage 1: Database load')

if not db_path.exists():
    tag(FAIL, f'Database not found: {db_path}')
    sys.exit(1)

t0      = time.monotonic()
t3      = tetra3.Tetra3(str(db_path))
load_ms = (time.monotonic() - t0) * 1000
tag(PASS, f'Loaded {db_path.name} in {load_ms:.0f} ms')

# ── Stage 2: Frame source ─────────────────────────────────────────────────────
sep('Stage 2: Frame source')

frames = []  # list of (label, uint8 ndarray)

if args.live_shm:
    try:
        from multiprocessing import shared_memory, resource_tracker as _rt
        from efinder.frame_slots import SHM_PREFIX, NUM_BUFFERS
    except ImportError as e:
        tag(FAIL, f'Cannot import frame_slots: {e}'); sys.exit(1)

    found = False
    for i in range(NUM_BUFFERS):
        try:
            shm = shared_memory.SharedMemory(name=f'{SHM_PREFIX}_{i}', create=False)
            try: _rt.unregister(shm._name, 'shared_memory')
            except Exception: pass
            arr = np.array(np.ndarray((H, W), dtype=np.uint8, buffer=shm.buf))
            shm.close()
            tag(PASS, f'SHM slot {i}: {W}x{H}  peak={arr.max()}  mean={arr.mean():.1f}')
            frames.append((f'live_shm_{i}', arr))
            found = True
            break
        except Exception:
            continue
    if not found:
        tag(FAIL, 'No live SHM slots found -- is the efinder daemon running?')
        sys.exit(1)

elif args.image:
    p = pathlib.Path(args.image)
    if not p.exists():
        tag(FAIL, f'Not found: {p}'); sys.exit(1)
    arr = np.array(PILImage.open(p).convert('L'), dtype=np.uint8)
    tag(PASS, f'{p.name}: {arr.shape[1]}x{arr.shape[0]}  '
              f'peak={arr.max()}  mean={arr.mean():.1f}')
    frames.append((p.name, arr))

else:
    images = (sorted(TEST_IMAGES_DIR.glob('*.jpg')) +
              sorted(TEST_IMAGES_DIR.glob('*.png')))
    if not images:
        tag(WARN, f'No test images in {TEST_IMAGES_DIR}')
        tag(INFO, 'Use --image <path> or --live-shm, '
                  'or install test images first')
        sys.exit(0)
    for p in images:
        arr = np.array(PILImage.open(p).convert('L'), dtype=np.uint8)
        tag(INFO, f'{p.name}: {arr.shape[1]}x{arr.shape[0]}  peak={arr.max()}')
        frames.append((p.name, arr))

solve_kw = dict(
    fov_estimate=fov, fov_max_error=fov_err,
    solve_timeout=timeout, sigma=sigma,
)
solve_kw_no_sigma = {k: v for k, v in solve_kw.items() if k != 'sigma'}

summary = []

for label, arr_u8 in frames:
    arr_f32  = arr_u8.astype(np.float32)
    img_size = (float(arr_u8.shape[0]), float(arr_u8.shape[1]))

    # ── Path A: solve_from_image_fast (u8) ───────────────────────────────────
    sep(f'Path A: solve_from_image_fast  [{label}]  reps={args.reps}')
    a_wall, a_ext, a_slv = [], [], []
    a_result = None
    for i in range(args.reps):
        t0   = time.monotonic()
        soln = t3.solve_from_image_fast(arr_u8, **solve_kw)
        wall = (time.monotonic() - t0) * 1000
        ext  = soln.get('T_extract', 0.0)
        slv  = soln.get('T_solve',   0.0)
        a_wall.append(wall); a_ext.append(ext); a_slv.append(slv)
        a_result = soln
        ok = PASS if soln.get('RA') is not None else FAIL
        tag(ok, f'[{i+1}] wall={wall:.0f}ms  '
                f'ext={ext:.0f}ms  solve={slv:.0f}ms  '
                f'{soln.get("status", "?")}')
    if a_wall:
        print(f'\n  avg  wall={sum(a_wall)/len(a_wall):.0f}ms  '
              f'ext={sum(a_ext)/len(a_ext):.0f}ms  '
              f'solve={sum(a_slv)/len(a_slv):.0f}ms')
    if a_result and a_result.get('RA') is not None:
        tag(PASS, f'RA={a_result["RA"]:.4f}°  Dec={a_result["Dec"]:.4f}°  '
                  f'FOV={a_result.get("FOV", 0):.4f}°  '
                  f'matches={a_result.get("Matches", "?")}')

    # ── Path B: get_centroids_from_image (f32) + solve_from_centroids ─────────
    sep(f'Path B: get_centroids + solve_from_centroids  [{label}]  reps={args.reps}')
    b_wall, b_cent, b_slv = [], [], []
    b_result = None
    n_stars  = 0
    for i in range(args.reps):
        t0        = time.monotonic()
        centroids = t3.get_centroids_from_image(arr_f32, sigma=sigma)
        cent_ms   = (time.monotonic() - t0) * 1000

        t1   = time.monotonic()
        soln = t3.solve_from_centroids(centroids, img_size, **solve_kw_no_sigma)
        slv  = (time.monotonic() - t1) * 1000
        wall = cent_ms + slv

        b_wall.append(wall); b_cent.append(cent_ms); b_slv.append(slv)
        b_result = soln
        n_stars  = len(centroids) if hasattr(centroids, '__len__') else 0
        ok = PASS if soln.get('RA') is not None else FAIL
        tag(ok, f'[{i+1}] wall={wall:.0f}ms  '
                f'cent={cent_ms:.0f}ms  solve={slv:.0f}ms  '
                f'stars={n_stars}  {soln.get("status", "?")}')
    if b_wall:
        print(f'\n  avg  wall={sum(b_wall)/len(b_wall):.0f}ms  '
              f'cent={sum(b_cent)/len(b_cent):.0f}ms  '
              f'solve={sum(b_slv)/len(b_slv):.0f}ms')

    summary.append((
        label,
        a_result.get('RA') is not None if a_result else False,
        sum(a_wall)/len(a_wall) if a_wall else 0,
        sum(a_ext)/len(a_ext)   if a_ext   else 0,
        sum(a_slv)/len(a_slv)   if a_slv   else 0,
        sum(b_wall)/len(b_wall) if b_wall  else 0,
        sum(b_cent)/len(b_cent) if b_cent  else 0,
        sum(b_slv)/len(b_slv)   if b_slv   else 0,
        n_stars,
    ))

# ── Summary ───────────────────────────────────────────────────────────────────
sep('Summary')
print(f'  {"Image":<34} {"Solved":<7} '
      f'{"A:wall":>8} {"A:ext":>6} {"A:slv":>6}  '
      f'{"B:wall":>8} {"B:cent":>7} {"B:slv":>6} {"Stars":>6}')
print('  ' + '-' * 80)
for lbl, solved, aw, ae, asl, bw, bc, bsl, stars in summary:
    s = 'YES' if solved else 'no'
    print(f'  {lbl:<34} {s:<7} '
          f'{aw:>7.0f}ms {ae:>5.0f}ms {asl:>5.0f}ms  '
          f'{bw:>7.0f}ms {bc:>6.0f}ms {bsl:>5.0f}ms {stars:>6}')
print()
