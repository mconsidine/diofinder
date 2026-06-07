#!/usr/bin/env python3
"""
eFinder full-pipeline diagnostic.

Tests the complete sycamore extraction + olive-solve pipeline with per-step
timing.  No external server required.

Two paths are timed for each image:
  1  sycamore detect_stars  +  solve_from_centroids  (blind)
     Split pipeline — matches what solver_proc.py uses at runtime.
  2  sycamore detect_stars  +  solve_from_centroids  (+ hint)
     Same as path 1 but with the quaternion from path 1 as an attitude hint.

Usage:
  # Loop over installed test images:
  sudo /opt/efinder/venv/bin/python3 tests/diag_solve.py

  # Live frame from running daemon:
  sudo /opt/efinder/venv/bin/python3 tests/diag_solve.py --live-shm

  # Single image, custom sigma:
  sudo /opt/efinder/venv/bin/python3 tests/diag_solve.py \\
      --image /path/to/image.png --sigma 7.0

  # Extended timeout when normal fails:
  sudo /opt/efinder/venv/bin/python3 tests/diag_solve.py --extended-timeout
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
ap.add_argument('--image',    metavar='PATH', help='Image file to solve (JPG/PNG)')
ap.add_argument('--live-shm', action='store_true',
                help='Read frame from live efinder daemon SHM')
ap.add_argument('--fov',     type=float, help='FOV estimate in degrees')
ap.add_argument('--fov-err', type=float, help='FOV max error in degrees')
ap.add_argument('--timeout', type=int,   help='Solve timeout in ms')
ap.add_argument('--sigma',   type=float, help='Detection sigma threshold')
ap.add_argument('--reps',    type=int, default=3,
                help='Repetitions per path per image (default 3)')
ap.add_argument('--extended-timeout', action='store_true',
                help='Retry with 3× timeout when path 1 produces no match')
args = ap.parse_args()


# ── Stage 0: Config & imports ─────────────────────────────────────────────────
sep('Stage 0: Config & imports')

try:
    from efinder.config import load_config
    cfg     = load_config()
    db_path = pathlib.Path(cfg.solver_db if cfg.solver_db.startswith('/')
                           else f'/var/lib/efinder/{cfg.solver_db}.npz')
    fov     = args.fov     or cfg.fov_deg
    fov_err = args.fov_err or cfg.fov_max_error_deg
    timeout = args.timeout or cfg.solve_timeout_ms
    sigma   = args.sigma   or cfg.detect_sigma
    min_c   = cfg.min_centroids
    W, H    = cfg.frame_width, cfg.frame_height
    tag(PASS, cfg.summary())
except Exception as e:
    tag(WARN, f'Config unavailable ({e}); using defaults')
    db_path = pathlib.Path('/var/lib/efinder/default_database.npz')
    fov     = args.fov     or 13.5
    fov_err = args.fov_err or 1.0
    timeout = args.timeout or 1500
    sigma   = args.sigma   or 7.0
    min_c   = 8
    W, H    = 960, 760

tag(INFO, f'db={db_path}')
tag(INFO, f'FOV={fov:.2f}° ±{fov_err:.2f}°  timeout={timeout} ms  sigma={sigma}  min_c={min_c}')

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
    tag(PASS, 'tetra3 (olive-solve)')
except ImportError as e:
    tag(FAIL, f'tetra3 not installed: {e}'); sys.exit(1)

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
        tag(FAIL, 'No live SHM slots found — is the efinder daemon running?')
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
        tag(INFO, 'Use --image <path> or --live-shm, or install test images')
        sys.exit(0)
    for p in images:
        arr = np.array(PILImage.open(p).convert('L'), dtype=np.uint8)
        tag(INFO, f'{p.name}: {arr.shape[1]}x{arr.shape[0]}  peak={arr.max()}')
        frames.append((p.name, arr))


# ── Shared solve kwargs ────────────────────────────────────────────────────────
base_kw = dict(
    fov_estimate=fov,
    fov_max_error=fov_err,
    solve_timeout=timeout,
)


# ── Extraction helper ─────────────────────────────────────────────────────────

def _extract(arr_u8):
    """Extract centroids using sycamore detect_stars (matched_filter gate).

    Returns (centroids_rowcol, n_stars, extract_ms).
    """
    t0  = time.monotonic()
    raw = _sd.detect_stars(arr_u8, sigma=sigma, bin=1, centroid_full_res=True)
    ms  = (time.monotonic() - t0) * 1000
    n   = len(raw) if raw else 0
    # sycamore returns (x=col, y=row); tetra3 expects (row, col)
    cent = (np.array([[s[1], s[0]] for s in raw], dtype=np.float64)
            if raw else None)
    return cent, n, ms


summary = []

for label, arr_u8 in frames:
    tag(INFO, f'\nImage: {label}  {arr_u8.shape[1]}x{arr_u8.shape[0]}  '
              f'peak={arr_u8.max()}')

    # ── Path 1: sycamore extraction + blind solve ─────────────────────────────
    sep(f'Path 1: sycamore + solve_from_centroids (blind)  [{label}]  reps={args.reps}')
    tag(INFO, 'Split pipeline — matches what solver_proc.py uses at runtime')

    p1_ext, p1_slv, p1_wall = [], [], []
    p1_result = None
    p1_n      = 0
    last_q    = None

    for i in range(args.reps):
        centroids, n, ext_ms = _extract(arr_u8)
        p1_n = n

        if n < min_c:
            tag(WARN, f'[{i+1}] ext={ext_ms:.0f} ms  stars={n}  '
                      f'< min_centroids={min_c} — skipping solve')
            p1_ext.append(ext_ms)
            p1_slv.append(0.0)
            p1_wall.append(ext_ms)
            continue

        t1     = time.monotonic()
        soln   = t3.solve_from_centroids(centroids, arr_u8.shape, **base_kw)
        slv_ms = (time.monotonic() - t1) * 1000
        total  = ext_ms + slv_ms

        p1_ext.append(ext_ms); p1_slv.append(slv_ms); p1_wall.append(total)
        p1_result = soln

        ok = PASS if soln and soln.get('RA') is not None else FAIL
        tag(ok, f'[{i+1}] total={total:.0f} ms  '
                f'ext={ext_ms:.0f} ms  solve={slv_ms:.0f} ms  '
                f'stars={n}  {soln.get("status", "?") if soln else "None"}')

        q = soln.get('quaternion') if soln else None
        if q is not None:
            last_q = tuple(q)

    if p1_wall:
        print(f'\n  avg  total={sum(p1_wall)/len(p1_wall):.0f} ms  '
              f'ext={sum(p1_ext)/len(p1_ext):.0f} ms  '
              f'solve={sum(p1_slv)/len(p1_slv):.0f} ms')

    if p1_result and p1_result.get('RA') is not None:
        tag(PASS, f'RA={p1_result["RA"]:.4f}°  Dec={p1_result["Dec"]:.4f}°  '
                  f'FOV={p1_result.get("FOV", 0):.4f}°  '
                  f'matches={p1_result.get("Matches", "?")}')
    elif args.extended_timeout and p1_n >= min_c:
        tag(WARN, f'No match; retrying at {timeout*3} ms …')
        try:
            c2, _, _ = _extract(arr_u8)
            soln = t3.solve_from_centroids(c2, arr_u8.shape,
                                           **{**base_kw, 'solve_timeout': timeout * 3})
            if soln and soln.get('RA') is not None:
                tag(PASS, 'Solved at 3× timeout — consider raising solve_timeout_ms')
                last_q = tuple(soln['quaternion']) if soln.get('quaternion') else last_q
            else:
                tag(FAIL, 'Still no match at 3× timeout')
        except Exception as e:
            tag(FAIL, f'Extended retry raised: {e}')

    # ── Path 2: sycamore extraction + hint solve ──────────────────────────────
    sep(f'Path 2: sycamore + solve_from_centroids (+ hint)  [{label}]  reps={args.reps}')

    if last_q is None:
        tag(WARN, 'Path 1 produced no quaternion — hint path skipped')
        tag(WARN, '  (Need at least one successful path 1 solve to seed the hint)')
        p2_wall = []
        p2_result = None
    else:
        tag(INFO, f'Hint q=({last_q[0]:.4f}, {last_q[1]:.4f}, '
                  f'{last_q[2]:.4f}, {last_q[3]:.4f})  unc=5.0°')
        p2_ext, p2_slv, p2_wall = [], [], []
        p2_result = None
        for i in range(args.reps):
            centroids, n, ext_ms = _extract(arr_u8)

            if n < min_c:
                tag(WARN, f'[{i+1}] ext={ext_ms:.0f} ms  stars={n}  < min_centroids — skip')
                p2_wall.append(ext_ms)
                continue

            t1     = time.monotonic()
            soln   = t3.solve_from_centroids(
                centroids, arr_u8.shape,
                attitude_hint=list(last_q),
                hint_uncertainty_deg=5.0,
                strict_hint=False,
                **base_kw,
            )
            slv_ms = (time.monotonic() - t1) * 1000
            total  = ext_ms + slv_ms
            p2_ext.append(ext_ms); p2_slv.append(slv_ms); p2_wall.append(total)
            p2_result = soln
            ok = PASS if soln and soln.get('RA') is not None else FAIL
            tag(ok, f'[{i+1}] total={total:.0f} ms  '
                    f'ext={ext_ms:.0f} ms  solve={slv_ms:.0f} ms  '
                    f'stars={n}  {soln.get("status", "?") if soln else "None"}')

        if p2_wall and p2_ext:
            print(f'\n  avg  total={sum(p2_wall)/len(p2_wall):.0f} ms  '
                  f'ext={sum(p2_ext)/len(p2_ext):.0f} ms  '
                  f'solve={sum(p2_slv)/len(p2_slv):.0f} ms')

        # Compare path 1 vs path 2
        if p1_wall and p2_wall:
            avg_1 = sum(p1_wall) / len(p1_wall)
            avg_2 = sum(p2_wall) / len(p2_wall)
            diff  = avg_1 - avg_2
            print(f'\n  path1 vs path2: blind={avg_1:.0f} ms  hint={avg_2:.0f} ms  '
                  f'(hint {"saves" if diff >= 0 else "costs"} {abs(diff):.0f} ms)')

    p1_solved = p1_result is not None and p1_result.get('RA') is not None
    p2_solved = p2_result is not None and p2_result.get('RA') is not None
    p1_wall_avg = sum(p1_wall) / len(p1_wall) if p1_wall else 0
    p2_wall_avg = sum(p2_wall) / len(p2_wall) if p2_wall else 0

    summary.append((label, p1_n, p1_solved, p1_wall_avg, p2_solved, p2_wall_avg))


# ── Summary ───────────────────────────────────────────────────────────────────
sep('Summary')
print(f'  sigma={sigma}  FOV={fov:.2f}°  timeout={timeout} ms')
print(f'  {"Image":<28} {"Stars":>5}  '
      f'{"path1:wall":>10}  {"ok":<4}  '
      f'{"path2:wall":>10}  {"ok":<4}')
print('  ' + '-' * 68)
for lbl, n, p1_ok, p1w, p2_ok, p2w in summary:
    p1s = f'{p1w:>9.0f}ms'
    p2s = f'{p2w:>9.0f}ms' if p2w else f'{"—":>10}'
    print(f'  {lbl:<28} {n:>5}  '
          f'{p1s}  {"YES" if p1_ok else "no":<4}  '
          f'{p2s}  {"YES" if p2_ok else "no":<4}')
print()
