#!/usr/bin/env python3
"""
eFinder background-mode A/B diagnostic.

Compares the sycamore background-subtraction modes through the exact extraction
call the daemon uses (matched_filter gate), on a real frame:
  - row_percentile  (default, cheapest)
  - line_median     (robust to per-row offset / vignetting)
  - top_hat         (opt-in 2-D gradient removal; needs sycamore >= 0.9.0)

It reports per mode: star count, extraction timing, and centroid agreement vs
the row_percentile baseline (matched / base_only / mode_only / mean offset).
The base_only and mode_only columns are the interesting ones — they show which
stars each mode gains or loses relative to the default.

Decision guidance (see sycamore-extract/ARCHITECTURE.md):
  - On clean dark-sky frames, top_hat should NOT lose stars vs row_percentile.
    If it does, detect_tophat_radius is too small (eating stars) — raise it.
  - On gradient/glow frames (or with --inject-gradient), top_hat should GAIN
    faint stars. That's the payoff. If it neither gains nor is affordable on
    your sky, leave detect_bg_mode=row_percentile.

Usage:
  # Live frame from running daemon:
  sudo /opt/efinder/venv/bin/python3 tests/diag_background.py

  # Saved capture, custom sigma, stress the gradient case:
  sudo /opt/efinder/venv/bin/python3 tests/diag_background.py \\
      --image /var/lib/efinder/captures/frame.png --inject-gradient 40

  # Also solve each mode on the live daemon (memory-safe; daemon must be up):
  sudo /opt/efinder/venv/bin/python3 tests/diag_background.py --solve
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


def tag(label, msg): print(f"  [{label}] {msg}")
def sep(title):      print(f"\n{'='*70}\n{title}\n{'='*70}")


ap = argparse.ArgumentParser(
    description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument('--image', metavar='PATH', help='PNG/JPG to use as test frame')
ap.add_argument('--sigma', type=float, help='Override detect_sigma from config')
ap.add_argument('--reps', type=int, default=10, help='Timing repetitions')
ap.add_argument('--tophat-radius', type=int, default=None,
                help='Override detect_tophat_radius from config')
ap.add_argument('--inject-gradient', type=float, default=0.0, metavar='ADU',
                help='Add a synthetic ramp+vignette of this peak ADU before '
                     'detection (stress-tests top_hat).')
ap.add_argument('--solve', action='store_true',
                help="Also plate-solve each mode's centroids on the LIVE daemon "
                     "(requires the service running). Reuses the solver's "
                     "resident database — no second copy is loaded, so it is "
                     "memory-safe on the Pi. Shows whether a mode actually "
                     "SOLVES, not just how many stars it detects.")
args = ap.parse_args()


# ── Stage 0: Config & imports ────────────────────────────────────────────
sep('Stage 0: Config & library imports')
try:
    from efinder.config import load_config
    cfg = load_config()
    sigma = args.sigma if args.sigma is not None else cfg.detect_sigma
    min_c = cfg.min_centroids
    max_c = cfg.max_solve_stars
    W, H = cfg.frame_width, cfg.frame_height
    th_radius = args.tophat_radius if args.tophat_radius is not None \
        else cfg.detect_tophat_radius
    det_bin = cfg.detect_bin
    tag(PASS, cfg.summary())
except Exception as e:
    tag(WARN, f'Config unavailable ({e}); using defaults')
    sigma = args.sigma if args.sigma is not None else 7.0
    min_c, W, H, th_radius, det_bin = 8, 960, 760, 12, 1
    max_c = 50

tag(INFO, f'sigma={sigma:.1f}  frame={W}x{H}  bin={det_bin}  '
          f'tophat_radius={th_radius}  min_centroids={min_c}')

try:
    import numpy as np
    tag(PASS, f'numpy {np.__version__}')
except ImportError as e:
    tag(FAIL, str(e)); sys.exit(1)

try:
    import star_detect as _sd
    _sd.set_num_threads(2)
    HAS_TOPHAT = 'tophat_radius' in __import__('inspect').signature(
        _sd.detect_stars).parameters
    tag(PASS, f'sycamore star_detect (top_hat support: {HAS_TOPHAT})')
except ImportError as e:
    tag(FAIL, f'sycamore star_detect not installed: {e}'); sys.exit(1)

MODES = ['row_percentile', 'line_median']
if HAS_TOPHAT:
    MODES.append('top_hat')
else:
    tag(WARN, 'Installed wheel lacks top_hat (needs sycamore >= 0.9.0); '
              'comparing the per-row modes only.')


# ── Stage 1: Frame source ────────────────────────────────────────────
sep('Stage 1: Frame source')
frame = None
if args.image:
    try:
        from PIL import Image as PILImage
        img = PILImage.open(args.image).convert('L')
        frame = np.array(img, dtype=np.uint8)
        if frame.shape != (H, W):
            frame = np.array(img.resize((W, H)), dtype=np.uint8)
        tag(PASS, f'{pathlib.Path(args.image).name}: {W}x{H} peak={frame.max()}')
    except Exception as e:
        tag(FAIL, f'Cannot load image: {e}'); sys.exit(1)
else:
    try:
        from multiprocessing import shared_memory, resource_tracker as _rt
        from efinder.frame_slots import SHM_PREFIX, NUM_BUFFERS
        for i in range(NUM_BUFFERS):
            try:
                shm = shared_memory.SharedMemory(
                    name=f'{SHM_PREFIX}_{i}', create=False)
                try: _rt.unregister(shm._name, 'shared_memory')
                except Exception: pass
                frame = np.array(np.ndarray((H, W), dtype=np.uint8, buffer=shm.buf))
                shm.close()
                tag(PASS, f'Live SHM slot {i}: peak={frame.max()} '
                          f'mean={frame.mean():.1f}')
                break
            except Exception:
                continue
    except Exception:
        pass

if frame is None:
    tag(WARN, 'No frame source — generating synthetic star field')
    rng = np.random.default_rng(42)
    frame = np.zeros((H, W), dtype=np.uint8)
    for _ in range(60):
        cy, cx = int(rng.integers(15, H - 15)), int(rng.integers(15, W - 15))
        br = int(rng.integers(80, 230))
        ys, xs = np.ogrid[-6:7, -6:7]
        patch = (br * np.exp(-(ys**2 + xs**2) / 5.0)).astype(np.uint8)
        frame[cy-6:cy+7, cx-6:cx+7] = np.maximum(
            frame[cy-6:cy+7, cx-6:cx+7], patch)

if args.inject_gradient > 0:
    h, w = frame.shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    ramp = (xx / max(1, w-1) + yy / max(1, h-1)) / 2.0
    cx, cy = (w-1)/2.0, (h-1)/2.0
    r = np.sqrt((xx-cx)**2 + (yy-cy)**2)
    vign = 1.0 - 0.5 * (r / r.max())
    bg = args.inject_gradient * (0.6*ramp + 0.4*vign)
    frame = np.clip(frame.astype(np.float32) + bg, 0, 255).astype(np.uint8)
    tag(INFO, f'Injected gradient (peak {args.inject_gradient:.0f} ADU); '
              f'frame peak now {frame.max()}')
frame = np.ascontiguousarray(frame)


# ── Extraction + comparison ─────────────────────────────────────────────────
def extract(mode):
    kw = dict(sigma=sigma, bin=det_bin, centroid_full_res=True,
              bg_mode=mode)
    if mode == 'top_hat':
        kw['tophat_radius'] = th_radius
    times = []
    raw = []
    for _ in range(args.reps):
        t0 = time.monotonic()
        raw = _sd.detect_stars(frame, **kw)
        times.append((time.monotonic() - t0) * 1000)
    times.sort()
    pts = [(x, y) for (x, y, *_) in (raw or [])]
    return pts, times[len(times)//2], times[int(len(times)*0.95)]


def agreement(base, other, tol=1.5):
    if not base and not other:
        return 0, 0, 0, float('nan')
    B = np.array(base) if base else np.zeros((0, 2))
    O = np.array(other) if other else np.zeros((0, 2))
    used, offs = set(), []
    for bx, by in B:
        if len(O) == 0:
            break
        d2 = (O[:, 0]-bx)**2 + (O[:, 1]-by)**2
        for j in np.argsort(d2):
            j = int(j)
            if j in used:
                continue
            if d2[j] <= tol*tol:
                used.add(j); offs.append(float(np.sqrt(d2[j])))
            break
    matched = len(offs)
    return matched, len(B)-matched, len(O)-matched, (
        float(np.mean(offs)) if offs else float('nan'))


sep(f'Background mode A/B  sigma={sigma:.1f}  reps={args.reps}')
results = {m: extract(m) for m in MODES}
base_pts = results['row_percentile'][0]


# ── Optional: solve each mode on the live daemon (memory-safe) ──────────────
# Hands each mode's centroids to the running solver via the maint socket; the
# solver reuses its resident database, so no second copy is loaded. This shows
# which background mode actually SOLVES, which star-count alone cannot.
solve_results = {}
if args.solve:
    try:
        from efinder.maint import call as _maint_call
    except Exception as e:
        tag(FAIL, f'--solve needs efinder.maint ({e})'); _maint_call = None

    def solve_mode(pts):
        if _maint_call is None:
            return ('no-maint', False, 0)
        if len(pts) < min_c:
            return ('TooFew', False, 0)
        # star_detect points are (x, y); the solver expects (row, col) = (y, x).
        cents = [[float(y), float(x)] for (x, y) in pts[:max_c]]
        try:
            r = _maint_call('solve_centroids', {'centroids': cents}, timeout=25.0)
        except Exception as e:
            return (f'err:{type(e).__name__}', False, 0)
        if not r.ok:
            return (f'err:{r.error}', False, 0)
        res = r.result or {}
        return (res.get('status', '?'), bool(res.get('solved')),
                int(res.get('matches', 0) or 0))

    for m in MODES:
        solve_results[m] = solve_mode(results[m][0])

hdr = (f'  {"mode":>14}  {"stars":>5}  {"p50ms":>6}  {"p95ms":>6}  '
       f'{"match":>5}  {"base_only":>9}  {"mode_only":>9}  {"dx_px":>6}')
if args.solve:
    hdr += f'  {"solved":>6}  {"Nmatch":>6}'
print(hdr)
print('  ' + '-' * (len(hdr) - 2))
for m in MODES:
    pts, p50, p95 = results[m]
    mt, bo, mo, dx = agreement(base_pts, pts)
    dxs = 'n/a' if dx != dx else f'{dx:.3f}'
    flag = '' if len(pts) >= min_c else '  ← below min_centroids'
    line = (f'  {m:>14}  {len(pts):5d}  {p50:6.1f}  {p95:6.1f}  '
            f'{mt:5d}  {bo:9d}  {mo:9d}  {dxs:>6}')
    if args.solve:
        status, ok, nm = solve_results[m]
        line += f'  {("YES" if ok else "no"):>6}  {nm:6d}'
        if not ok and status not in ('NoMatch', 'TooFew'):
            flag += f'  ({status})'
    print(line + flag)

print(f'\n  agreement columns are vs the row_percentile baseline; '
      f'min_centroids={min_c}')
if args.solve:
    print('  solved/Nmatch are from the LIVE daemon solver (resident DB, no '
          'second copy loaded);\n  it briefly competes with live solving while '
          'this runs.')
if HAS_TOPHAT and args.inject_gradient > 0:
    th_n = len(results['top_hat'][0])
    rp_n = len(results['row_percentile'][0])
    if th_n > rp_n:
        tag(PASS, f'top_hat recovered {th_n-rp_n} more stars under the gradient')
    else:
        tag(INFO, 'top_hat did not gain stars here; try a larger gradient or '
                  'a different radius')
print()
