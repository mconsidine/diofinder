#!/usr/bin/env python3
"""
Plane-fit background A/B experiment (pure-Python; no wheel rebuild).

Thought-experiment premise: the goal is not to *eliminate* the sky background,
only to knock it down enough that the matched filter can find stars. The
sycamore gate is already mean-zero (it cancels DC), so background compensation
only has to (a) flatten structure at scales larger than the kernel and (b)
leave a representative noise floor. A low-order polynomial (plane / quadratic)
can't represent a complex light-pollution dome — which is why the
"eliminate it" mindset skips it — but it removes the single biggest offender,
a frame-spanning gradient, for almost nothing: a sigma-clipped fit of 3 or 6
coefficients on a decimated grid.

This harness measures whether that cheap-and-incomplete approach is *good
enough*: it pits plane1 (linear) and plane2 (quadratic) against the incumbent
gradient removers (block_percentile, uniform_mean, top_hat) and the
no-gradient-removal floor (line_median), on the same frame, through the exact
detect_stars call the daemon uses. With --solve it also plate-solves each on
the live daemon's resident DB (memory-safe).

The plane modes are done ENTIRELY in numpy here (fit -> subtract -> clip ->
detect with a cheap residual mode), so nothing native changes: pull this one
file onto a running device and run it. If plane* solves where line_median
fails, at a fraction of block/top_hat's cost, it's worth promoting to a real
bg_mode later.

Usage:
  # Live frame from the running daemon, stress a synthetic gradient, solve:
  sudo /opt/diofinder/venv/bin/python3 tests/ab_plane_background.py \\
      --inject-gradient 50 --solve

  # A saved capture / bundle frame:
  sudo /opt/diofinder/venv/bin/python3 tests/ab_plane_background.py \\
      --image /var/lib/diofinder/captures/frame.png --inject-gradient 40 --solve
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
def sep(title):      print(f"\n{'='*74}\n{title}\n{'='*74}")


# ── Plane / polynomial background (the thing under test) ─────────────────────
def fit_poly_background(frame_f, order=1, iters=3, clip=2.5, decimate=8):
    """Sigma-clipped least-squares polynomial background, evaluated full-res.

    order 1 -> plane   (1, x, y)
    order 2 -> quadratic (1, x, y, x^2, xy, y^2)

    Fit on a `decimate`x decimated grid (the fit is smooth, so this is free)
    with a ONE-SIDED clip: stars are positive outliers, so each iteration
    rejects pixels far ABOVE the current fit and refits to what's left — the
    plane tracks the sky floor, not the stars. Coordinates are normalized to
    [-1, 1] for conditioning.

    The full-res background is evaluated by BROADCASTING the polynomial over a
    (1, W) x-vector and (H, 1) y-vector — never materializing an (H*W, k)
    design matrix — so the only H*W-sized work is a few fused adds/mults.
    Returns a float32 array broadcasting to (H, W).
    """
    import numpy as np
    h, w = frame_f.shape
    xs = np.arange(0, w, decimate)
    ys = np.arange(0, h, decimate)
    samp = frame_f[::decimate, ::decimate].astype(np.float64)
    nxg, nyg = np.meshgrid(xs / max(1, w - 1) * 2.0 - 1.0,
                           ys / max(1, h - 1) * 2.0 - 1.0)
    sx, sy, sv = nxg.ravel(), nyg.ravel(), samp.ravel()

    def basis(X, Y):
        cols = [np.ones_like(X), X, Y]
        if order >= 2:
            cols += [X * X, X * Y, Y * Y]
        return np.stack(cols, axis=1)

    A = basis(sx, sy)
    mask = np.ones(sv.shape[0], bool)
    coef = np.linalg.lstsq(A, sv, rcond=None)[0]
    for _ in range(iters):
        coef = np.linalg.lstsq(A[mask], sv[mask], rcond=None)[0]
        resid = sv - A @ coef
        s = float(np.std(resid[mask]))
        if s <= 0.0:
            break
        # Keep the floor: reject strong POSITIVE residuals (stars); allow
        # negatives so a dark corner still anchors the fit.
        new_mask = resid < clip * s
        if new_mask.sum() < A.shape[1] * 4:
            break
        mask = new_mask

    FX = (np.arange(w) / max(1, w - 1) * 2.0 - 1.0)[None, :].astype(np.float32)
    FY = (np.arange(h) / max(1, h - 1) * 2.0 - 1.0)[:, None].astype(np.float32)
    bg = coef[0] + coef[1] * FX + coef[2] * FY
    if order >= 2:
        bg = bg + coef[3] * (FX * FX) + coef[4] * (FX * FY) + coef[5] * (FY * FY)
    return np.asarray(np.broadcast_to(bg, (h, w)), dtype=np.float32)


ap = argparse.ArgumentParser(
    description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument('--image', metavar='PATH', help='PNG/JPG to use as test frame')
ap.add_argument('--sigma', type=float, help='Override detect_sigma from config')
ap.add_argument('--reps', type=int, default=7, help='Timing repetitions')
ap.add_argument('--decimate', type=int, default=8,
                help='Plane-fit sample stride (larger = cheaper, still smooth)')
ap.add_argument('--clip', type=float, default=2.5,
                help='Plane-fit one-sided sigma clip (reject stars above this)')
ap.add_argument('--inject-gradient', type=float, default=0.0, metavar='ADU',
                help='Add a synthetic ramp+vignette of this peak ADU first')
ap.add_argument('--solve', action='store_true',
                help="Also plate-solve each mode's centroids on the LIVE daemon "
                     "(resident DB, memory-safe).")
args = ap.parse_args()


# ── Stage 0: Config & imports ────────────────────────────────────────────
sep('Stage 0: config & libraries')
try:
    from diofinder.config import load_config
    cfg = load_config()
    sigma = args.sigma if args.sigma is not None else cfg.detect_sigma
    min_c, max_c = cfg.min_centroids, cfg.max_solve_stars
    W, H = cfg.frame_width, cfg.frame_height
    th_radius = cfg.detect_tophat_radius
    block_size = getattr(cfg, 'detect_bg_block_size', 0)
    uniform_size = getattr(cfg, 'detect_uniform_filter_size', 0)
    noise_mode = getattr(cfg, 'detect_noise_mode', 'mad')
    det_bin = cfg.detect_bin
    tag(PASS, cfg.summary())
except Exception as e:
    tag(WARN, f'config unavailable ({e}); using defaults')
    sigma = args.sigma if args.sigma is not None else 9.0
    min_c, max_c, W, H, th_radius, det_bin = 8, 50, 960, 760, 12, 1
    block_size = uniform_size = 0
    noise_mode = 'mad'

import numpy as np
try:
    import star_detect as _sd
    _sd.set_num_threads(3)
    HAS_TOPHAT = 'tophat_radius' in __import__('inspect').signature(
        _sd.detect_stars).parameters
except ImportError as e:
    tag(FAIL, f'sycamore star_detect not installed: {e}'); sys.exit(1)

tag(INFO, f'sigma={sigma:.1f}  frame={W}x{H}  bin={det_bin}  '
          f'min_centroids={min_c}  plane decimate={args.decimate} clip={args.clip}')

# Incumbent gradient removers + the no-2D-removal floor, then the plane modes.
NATIVE = ['line_median', 'block_percentile', 'uniform_mean']
if HAS_TOPHAT:
    NATIVE.append('top_hat')
PLANES = ['plane1', 'plane2']
MODES = NATIVE + PLANES


# ── Stage 1: frame source ────────────────────────────────────────────────
sep('Stage 1: frame source')
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
        tag(FAIL, f'cannot load image: {e}'); sys.exit(1)
else:
    try:
        from multiprocessing import shared_memory, resource_tracker as _rt
        from diofinder.frame_slots import SHM_PREFIX, NUM_BUFFERS
        for i in range(NUM_BUFFERS):
            try:
                shm = shared_memory.SharedMemory(
                    name=f'{SHM_PREFIX}_{i}', create=False)
                try: _rt.unregister(shm._name, 'shared_memory')
                except Exception: pass
                frame = np.array(np.ndarray((H, W), dtype=np.uint8, buffer=shm.buf))
                shm.close()
                tag(PASS, f'live SHM slot {i}: peak={frame.max()} mean={frame.mean():.1f}')
                break
            except Exception:
                continue
    except Exception:
        pass

if frame is None:
    tag(WARN, 'no frame source — generating synthetic star field')
    rng = np.random.default_rng(42)
    frame = np.zeros((H, W), dtype=np.uint8)
    for _ in range(80):
        cy, cx = int(rng.integers(15, H - 15)), int(rng.integers(15, W - 15))
        br = int(rng.integers(70, 230))
        ys, xs = np.ogrid[-6:7, -6:7]
        patch = (br * np.exp(-(ys**2 + xs**2) / 5.0)).astype(np.uint8)
        frame[cy-6:cy+7, cx-6:cx+7] = np.maximum(frame[cy-6:cy+7, cx-6:cx+7], patch)

if args.inject_gradient > 0:
    h, w = frame.shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    ramp = (xx / max(1, w-1) + yy / max(1, h-1)) / 2.0
    cx, cy = (w-1)/2.0, (h-1)/2.0
    r = np.sqrt((xx-cx)**2 + (yy-cy)**2)
    vign = 1.0 - 0.5 * (r / r.max())
    bg = args.inject_gradient * (0.6*ramp + 0.4*vign)
    frame = np.clip(frame.astype(np.float32) + bg, 0, 255).astype(np.uint8)
    tag(INFO, f'injected gradient (peak {args.inject_gradient:.0f} ADU); '
              f'frame peak now {frame.max()}')
frame = np.ascontiguousarray(frame)


# ── Extraction ───────────────────────────────────────────────────────────
def _detect_native(mode):
    kw = dict(sigma=sigma, bin=det_bin, centroid_full_res=True, bg_mode=mode)
    if mode == 'top_hat':
        kw['tophat_radius'] = th_radius
    elif mode == 'block_percentile' and block_size:
        kw['bg_block_size'] = block_size
    elif mode == 'uniform_mean' and uniform_size:
        kw['uniform_filter_size'] = uniform_size
    if mode in ('uniform_mean', 'block_percentile') and noise_mode != 'mad':
        kw['noise_mode'] = noise_mode
    return _sd.detect_stars(frame, **kw)


def _detect_plane(order):
    # Fit + subtract the plane in numpy, clip to u8, then let sycamore do only
    # the cheap residual floor (line_median) + the matched filter. The whole
    # cost — fit included — is what's timed.
    bg = fit_poly_background(frame.astype(np.float32), order=order,
                             clip=args.clip, decimate=args.decimate)
    resid = np.clip(frame.astype(np.float32) - bg, 0, 255).astype(np.uint8)
    return _sd.detect_stars(np.ascontiguousarray(resid), sigma=sigma,
                            bin=det_bin, centroid_full_res=True,
                            bg_mode='line_median')


def extract(mode):
    times, raw = [], []
    for _ in range(args.reps):
        t0 = time.monotonic()
        if mode == 'plane1':
            raw = _detect_plane(1)
        elif mode == 'plane2':
            raw = _detect_plane(2)
        else:
            raw = _detect_native(mode)
        times.append((time.monotonic() - t0) * 1000)
    times.sort()
    pts = [(x, y) for (x, y, *_) in (raw or [])]
    return pts, times[len(times)//2], times[int(len(times)*0.95)]


sep(f'Plane-fit vs incumbents   sigma={sigma:.1f}  reps={args.reps}')
results = {}
for m in list(MODES):
    try:
        results[m] = extract(m)
    except Exception as e:
        tag(WARN, f'mode {m!r} failed — skipping ({e})')
        MODES.remove(m)


# ── Optional: solve each on the live daemon (memory-safe) ─────────────────
solve_results = {}
if args.solve:
    try:
        from diofinder.maint import call as _maint_call
    except Exception as e:
        tag(FAIL, f'--solve needs diofinder.maint ({e})'); _maint_call = None

    def solve_mode(pts):
        if _maint_call is None:
            return ('no-maint', False, 0)
        if len(pts) < min_c:
            return ('TooFew', False, 0)
        cents = [[float(y), float(x)] for (x, y) in pts[:max_c]]  # (x,y)->(row,col)
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


# ── Report ────────────────────────────────────────────────────────────────
hdr = f'  {"mode":>16}  {"stars":>5}  {"p50ms":>6}  {"p95ms":>6}'
if args.solve:
    hdr += f'  {"solved":>6}  {"Nmatch":>6}  {"status":>8}'
print(hdr)
print('  ' + '-' * (len(hdr) - 2))
for m in MODES:
    pts, p50, p95 = results[m]
    kind = '  (plane)' if m.startswith('plane') else ''
    line = f'  {m:>16}  {len(pts):5d}  {p50:6.1f}  {p95:6.1f}'
    if args.solve:
        status, ok, nm = solve_results[m]
        line += f'  {("YES" if ok else "no"):>6}  {nm:6d}  {status:>8}'
    flag = '' if len(pts) >= min_c else '  ← below min_centroids'
    print(line + flag + kind)

print()
tag(INFO, 'plane1 = linear (a+bx+cy); plane2 = quadratic (6 coeffs). Both '
          'fit+subtract in numpy, then detect with line_median.')
if args.solve:
    tag(INFO, 'solved/Nmatch/status are from the LIVE daemon (resident DB); it '
              'briefly competes with live solving while this runs.')
# Verdict hint under a gradient: did the cheap plane climb back to the
# incumbents from the line_median floor?
if 'line_median' in results and args.inject_gradient > 0:
    floor = len(results['line_median'][0])
    best_incumbent = max((len(results[m][0]) for m in ('block_percentile',
                          'top_hat', 'uniform_mean') if m in results), default=0)
    for pm in ('plane2', 'plane1'):
        if pm in results:
            pn = len(results[pm][0])
            if pn >= floor + max(3, int(0.15 * max(1, best_incumbent - floor))):
                tag(PASS, f'{pm} recovered {pn - floor} stars over the '
                          f'line_median floor (incumbent best {best_incumbent}) '
                          f'at p50 {results[pm][1]:.1f} ms (numpy cost; a native '
                          'fit would be far cheaper)')
            else:
                tag(INFO, f'{pm} gained little over the floor here '
                          f'({pn} vs {floor}); try a larger --inject-gradient '
                          'or order 2')
print()
