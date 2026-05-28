#!/usr/bin/env python3
"""
eFinder olive-solve full-pipeline diagnostic.

Tests the complete extraction + solve pipeline with per-step timing.
No cedar-detect, gRPC, or external server required.

Three paths are timed for each image:
  A  solve_from_image_fast (u8)           — single combined Rust call
  B  get_centroids_from_image_fast (u8)   — split pipeline (matches daemon)
     + solve_from_centroids
  C  same as B but with attitude hint     — seeded from path B result

Paths B and C reflect exactly what solver_proc.py does at runtime.
Path A is a useful timing reference but is no longer the live code path.

Usage:
  # Loop over installed test images (default):
  sudo /opt/efinder/venv/bin/python3 tests/diag_solve.py

  # Single image:
  sudo /opt/efinder/venv/bin/python3 tests/diag_solve.py --image /path/to/image.png

  # Live frame from running daemon:
  sudo /opt/efinder/venv/bin/python3 tests/diag_solve.py --live-shm

  # Override solver params:
  sudo /opt/efinder/venv/bin/python3 tests/diag_solve.py \\
      --fov 13.5 --fov-err 1.0 --timeout 2000 --sigma 7.0

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
ap.add_argument('--skip-a',  action='store_true',
                help='Skip path A (solve_from_image_fast) to save time')
ap.add_argument('--extended-timeout', action='store_true',
                help='Retry with 3× timeout when path A/B produce no match')
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
    sigma   = args.sigma   or 9.0
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
    if not hasattr(tetra3.Tetra3, 'solve_from_image_fast'):
        tag(FAIL, 'solve_from_image_fast missing — wrong tetra3 installed')
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

summary = []

for label, arr_u8 in frames:
    tag(INFO, f'\nImage: {label}  {arr_u8.shape[1]}x{arr_u8.shape[0]}  '
              f'peak={arr_u8.max()}')

    # ── Path A: solve_from_image_fast (single Rust call) ─────────────────────
    a_wall, a_solved = [], False
    a_result = None
    if not args.skip_a:
        sep(f'Path A: solve_from_image_fast  [{label}]  reps={args.reps}')
        tag(INFO, 'Single combined extraction + solve call (reference baseline)')
        for i in range(args.reps):
            t0   = time.monotonic()
            soln = t3.solve_from_image_fast(arr_u8, sigma=sigma, **base_kw)
            wall = (time.monotonic() - t0) * 1000
            ext  = soln.get('T_extract', 0.0) if soln else 0.0
            slv  = soln.get('T_solve',   0.0) if soln else 0.0
            a_wall.append(wall)
            a_result = soln
            ok = PASS if soln and soln.get('RA') is not None else FAIL
            tag(ok, f'[{i+1}] wall={wall:.0f} ms  '
                    f'ext={ext:.0f} ms  solve={slv:.0f} ms  '
                    f'{soln.get("status", "?") if soln else "None"}')
        if a_wall:
            avg = sum(a_wall) / len(a_wall)
            print(f'\n  avg wall={avg:.0f} ms')
        if a_result and a_result.get('RA') is not None:
            a_solved = True
            tag(PASS, f'RA={a_result["RA"]:.4f}°  Dec={a_result["Dec"]:.4f}°  '
                      f'FOV={a_result.get("FOV", 0):.4f}°  '
                      f'matches={a_result.get("Matches", "?")}')
        elif args.extended_timeout and not a_solved:
            tag(WARN, f'No match; retrying at {timeout*3} ms …')
            soln = t3.solve_from_image_fast(arr_u8, sigma=sigma,
                                             **{**base_kw, 'solve_timeout': timeout * 3})
            if soln and soln.get('RA') is not None:
                tag(PASS, f'Solved at 3× timeout — consider raising solve_timeout_ms')
            else:
                tag(FAIL, 'Still no match at 3× timeout')

    # ── Path B: split pipeline — matches daemon ───────────────────────────────
    sep(f'Path B: get_centroids_from_image_fast + solve_from_centroids  '
        f'[{label}]  reps={args.reps}')
    tag(INFO, 'This is the exact pipeline solver_proc.py uses at runtime')

    b_ext, b_slv, b_wall = [], [], []
    b_result = None
    b_n      = 0
    last_q   = None

    for i in range(args.reps):
        t0        = time.monotonic()
        centroids = t3.get_centroids_from_image_fast(arr_u8, sigma=sigma)
        ext_ms    = (time.monotonic() - t0) * 1000
        n         = len(centroids) if centroids is not None else 0

        if n < min_c:
            tag(WARN, f'[{i+1}] ext={ext_ms:.0f} ms  stars={n}  '
                      f'< min_centroids={min_c} — skipping solve')
            b_ext.append(ext_ms)
            b_slv.append(0.0)
            b_wall.append(ext_ms)
            b_n = n
            continue

        t1    = time.monotonic()
        soln  = t3.solve_from_centroids(centroids, arr_u8.shape, **base_kw)
        slv_ms = (time.monotonic() - t1) * 1000
        total  = ext_ms + slv_ms

        b_ext.append(ext_ms); b_slv.append(slv_ms); b_wall.append(total)
        b_result = soln
        b_n      = n

        ok = PASS if soln and soln.get('RA') is not None else FAIL
        tag(ok, f'[{i+1}] total={total:.0f} ms  '
                f'ext={ext_ms:.0f} ms  solve={slv_ms:.0f} ms  '
                f'stars={n}  {soln.get("status", "?") if soln else "None"}')

        q = soln.get('quaternion') if soln else None
        if q is not None:
            last_q = tuple(q)

    if b_wall:
        print(f'\n  avg  total={sum(b_wall)/len(b_wall):.0f} ms  '
              f'ext={sum(b_ext)/len(b_ext):.0f} ms  '
              f'solve={sum(b_slv)/len(b_slv):.0f} ms')

    if b_result and b_result.get('RA') is not None:
        tag(PASS, f'RA={b_result["RA"]:.4f}°  Dec={b_result["Dec"]:.4f}°  '
                  f'FOV={b_result.get("FOV", 0):.4f}°  '
                  f'matches={b_result.get("Matches", "?")}')
    elif args.extended_timeout and b_n >= min_c:
        tag(WARN, f'No match; retrying at {timeout*3} ms …')
        try:
            c2   = t3.get_centroids_from_image_fast(arr_u8, sigma=sigma)
            soln = t3.solve_from_centroids(c2, arr_u8.shape,
                                           **{**base_kw, 'solve_timeout': timeout * 3})
            if soln and soln.get('RA') is not None:
                tag(PASS, 'Solved at 3× timeout — consider raising solve_timeout_ms')
                last_q = tuple(soln['quaternion']) if soln.get('quaternion') else last_q
            else:
                tag(FAIL, 'Still no match at 3× timeout')
        except Exception as e:
            tag(FAIL, f'Extended retry raised: {e}')

    # ── Path C: split pipeline + attitude hint ────────────────────────────────
    sep(f'Path C: split pipeline + attitude hint  [{label}]  reps={args.reps}')

    if last_q is None:
        tag(WARN, 'Path B produced no quaternion — hint path skipped')
        tag(WARN, '  (Need at least one successful path B solve to seed the hint)')
        c_wall = []
        c_result = None
    else:
        tag(INFO, f'Hint q=({last_q[0]:.4f}, {last_q[1]:.4f}, '
                  f'{last_q[2]:.4f}, {last_q[3]:.4f})  unc=5.0°')
        c_ext, c_slv, c_wall = [], [], []
        c_result = None
        for i in range(args.reps):
            t0        = time.monotonic()
            centroids = t3.get_centroids_from_image_fast(arr_u8, sigma=sigma)
            ext_ms    = (time.monotonic() - t0) * 1000
            n         = len(centroids) if centroids is not None else 0

            if n < min_c:
                tag(WARN, f'[{i+1}] ext={ext_ms:.0f} ms  stars={n}  < min_centroids — skip')
                c_wall.append(ext_ms)
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
            c_ext.append(ext_ms); c_slv.append(slv_ms); c_wall.append(total)
            c_result = soln
            ok = PASS if soln and soln.get('RA') is not None else FAIL
            tag(ok, f'[{i+1}] total={total:.0f} ms  '
                    f'ext={ext_ms:.0f} ms  solve={slv_ms:.0f} ms  '
                    f'stars={n}  {soln.get("status", "?") if soln else "None"}')

        if c_wall and c_ext:
            print(f'\n  avg  total={sum(c_wall)/len(c_wall):.0f} ms  '
                  f'ext={sum(c_ext)/len(c_ext):.0f} ms  '
                  f'solve={sum(c_slv)/len(c_slv):.0f} ms')

        # Compare B vs C
        if b_wall and c_wall:
            avg_b = sum(b_wall) / len(b_wall)
            avg_c = sum(c_wall) / len(c_wall)
            diff  = avg_b - avg_c
            print(f'\n  B vs C: blind={avg_b:.0f} ms  hint={avg_c:.0f} ms  '
                  f'(hint {"saves" if diff >= 0 else "costs"} {abs(diff):.0f} ms)')

    b_solved = b_result is not None and b_result.get('RA') is not None
    c_solved = c_result is not None and c_result.get('RA') is not None
    a_wall_avg = sum(a_wall) / len(a_wall) if a_wall else 0
    b_wall_avg = sum(b_wall) / len(b_wall) if b_wall else 0
    c_wall_avg = sum(c_wall) / len(c_wall) if c_wall else 0

    summary.append((label, b_n,
                    a_solved, a_wall_avg,
                    b_solved, b_wall_avg,
                    c_solved, c_wall_avg))


# ── Summary ───────────────────────────────────────────────────────────────────
sep('Summary')
print(f'  {"Image":<28} {"Stars":>5}  '
      f'{"A:wall":>8}  {"A:ok":<5}  '
      f'{"B:wall":>8}  {"B:ok":<5}  '
      f'{"C:wall":>8}  {"C:ok":<5}')
print('  ' + '-' * 78)
for lbl, n, a_ok, aw, b_ok, bw, c_ok, cw in summary:
    def yn(ok, ms): return f'{"YES" if ok else "no"} {ms:>6.0f}ms' if ms else f'{"YES" if ok else "no"} {"—":>6}'
    a_skip = args.skip_a
    print(f'  {lbl:<28} {n:>5}  '
          f'{"—":>8}  {"skip":<5}  ' if a_skip else
          f'  {lbl:<28} {n:>5}  '
          f'{aw:>7.0f}ms  {"YES" if a_ok else "no":<5}  '
          f'{bw:>7.0f}ms  {"YES" if b_ok else "no":<5}  '
          f'{cw:>7.0f}ms  {"YES" if c_ok else "no":<5}')
print()
