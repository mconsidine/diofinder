#!/usr/bin/env python3
"""
eFinder extractor comparison benchmark.

Runs both the olive-solve and sycamore-extract centroid extractors on the
same sky frame and reports extraction speed, star count, and solve outcomes
side by side.  The plate-solver (tetra3 / olive-solve) is shared — only the
centroid extraction step differs.

Pipeline under test for each backend:

  extract(frame)  →  [star-count gate]  →  solve_from_centroids (blind)
                                         →  solve_from_centroids (+ hint)

"Olive"    extractor: tetra3.get_centroids_from_image_fast()
"Sycamore" extractor: star_detect.detect_stars()  (optional — skipped if
                      the star_detect wheel is not installed)

Coordinate note: sycamore returns (x=col, y=row); tetra3 expects (row, col),
so columns are swapped before passing centroids to solve_from_centroids.
This is the same swap performed by solver_proc.py in production.

Usage:
  # File on disk:
  sudo /opt/efinder/venv/bin/python3 tests/bench_extractor_compare.py \\
      --image /var/lib/efinder/captures/capture_20260101_123456.png

  # Live frame from running daemon:
  sudo /opt/efinder/venv/bin/python3 tests/bench_extractor_compare.py --live-shm

  # More reps, custom sigma and FOV:
  sudo /opt/efinder/venv/bin/python3 tests/bench_extractor_compare.py \\
      --image img.png --reps 10 --sigma 7.0

  # Override solver parameters:
  sudo /opt/efinder/venv/bin/python3 tests/bench_extractor_compare.py \\
      --image img.png --fov 14.0 --fov-err 1.5 --timeout 3000
"""

import argparse
import sys
import time

sys.path.insert(0, '/opt/efinder')

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"
INFO = "\033[34mINFO\033[0m"
BOLD = "\033[1m"
NC   = "\033[0m"


def tag(label, msg): print(f"  [{label}] {msg}")
def sep(title):      print(f"\n{BOLD}{'='*64}\n{title}\n{'='*64}{NC}")
def hr():            print(f"  {'-'*60}")


# ── Args ──────────────────────────────────────────────────────────────────────
ap = argparse.ArgumentParser(description=__doc__,
                              formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument('--image',    metavar='PNG',
                help='Path to a sky PNG/JPG to use as the test frame')
ap.add_argument('--live-shm', action='store_true',
                help='Read from live efinder_frame_0 SHM (daemon must be running)')
ap.add_argument('--reps',     type=int, default=5,
                help='Timed repetitions per extractor (default 5)')
ap.add_argument('--sigma',    type=float, help='Override detect_sigma')
ap.add_argument('--timeout',  type=int,   help='Solve timeout in ms (overrides config)')
ap.add_argument('--fov',      type=float, help='Override FOV estimate in degrees')
ap.add_argument('--fov-err',  type=float, help='Override FOV max error in degrees')
ap.add_argument('--hint-unc', type=float, default=5.0,
                help='Hint uncertainty cone for hint solves (default 5.0°)')
args = ap.parse_args()


# ── Stage 0: Config & imports ─────────────────────────────────────────────────
sep('Stage 0: Config & library imports')

try:
    from efinder.config import load_config
    from efinder.calibration import FovCalibrator
    cfg        = load_config()
    shared_cfg = {}
    cal        = FovCalibrator(cfg, shared_cfg)
    fov_est    = args.fov     if args.fov     is not None else cal.get_fov_estimate()
    fov_err    = args.fov_err if args.fov_err is not None else cal.get_fov_max_error()
    timeout_ms = args.timeout if args.timeout is not None else cfg.solve_timeout_ms
    sigma      = args.sigma   if args.sigma   is not None else cfg.detect_sigma
    min_c      = cfg.min_centroids
    max_c      = cfg.max_solve_stars
    tag(PASS, f'Config: {cfg.summary()}')
    tag(INFO, f'FOV: {fov_est:.4f}° ± {fov_err:.4f}°  timeout: {timeout_ms} ms  '
              f'sigma: {sigma}  min_c: {min_c}  max_c: {max_c}')
except Exception as e:
    tag(FAIL, f'Config: {e}')
    sys.exit(1)

w, h = cfg.frame_width, cfg.frame_height

try:
    import numpy as np
    tag(PASS, f'numpy {np.__version__}')
except ImportError as e:
    tag(FAIL, str(e)); sys.exit(1)

try:
    import tetra3 as _t3
    db_path = (cfg.solver_db if cfg.solver_db.startswith('/')
               else f'/var/lib/efinder/{cfg.solver_db}.npz')
    t3 = _t3.Tetra3(db_path)
    tag(PASS, f'tetra3 (olive-solve)  db={db_path}')
except Exception as e:
    tag(FAIL, f'tetra3 / database: {e}')
    sys.exit(1)

sycamore_ok = False
try:
    import star_detect as _sd
    _sd.set_num_threads(2)
    sycamore_ok = True
    tag(PASS, 'sycamore star_detect — available')
except ImportError:
    tag(WARN, 'sycamore star_detect not installed — sycamore rows will be skipped')


# ── Stage 1: Frame source ─────────────────────────────────────────────────────
sep('Stage 1: Frame source')

raw_frame = None

if args.live_shm:
    try:
        from multiprocessing import shared_memory, resource_tracker as _rt
        from efinder.frame_slots import SHM_PREFIX, NUM_BUFFERS
        for i in range(NUM_BUFFERS):
            try:
                shm = shared_memory.SharedMemory(name=f'{SHM_PREFIX}_{i}', create=False)
                try: _rt.unregister(shm._name, 'shared_memory')
                except Exception: pass
                raw_frame = np.array(np.ndarray((h, w), dtype=np.uint8, buffer=shm.buf))
                shm.close()
                tag(PASS, f'Live SHM slot {i}: {w}x{h}  '
                          f'peak={raw_frame.max()}  mean={raw_frame.mean():.1f}')
                break
            except Exception:
                continue
        if raw_frame is None:
            tag(FAIL, 'No live SHM found — is efinder running?')
            sys.exit(1)
    except Exception as e:
        tag(FAIL, f'SHM attach failed: {e}'); sys.exit(1)

elif args.image:
    try:
        from PIL import Image as PILImage
        img = PILImage.open(args.image).convert('L')
        if img.size != (w, h):
            tag(INFO, f'Resizing {img.size} → ({w}, {h})')
            img = img.resize((w, h))
        raw_frame = np.array(img, dtype=np.uint8)
        tag(PASS, f'{args.image}  peak={raw_frame.max()}  mean={raw_frame.mean():.1f}  '
                  f'p95={int(np.percentile(raw_frame, 95))}')
    except Exception as e:
        tag(FAIL, f'Cannot load image: {e}'); sys.exit(1)

else:
    tag(WARN, 'No --image or --live-shm specified — generating synthetic star field')
    tag(INFO, '  (Extraction timing is valid; blind plate solve will fail)')
    raw_frame = np.zeros((h, w), dtype=np.uint8)
    rng = np.random.default_rng(42)
    for _ in range(50):
        cy, cx = int(rng.integers(15, h - 15)), int(rng.integers(15, w - 15))
        br = int(rng.integers(100, 230))
        ys, xs = np.ogrid[-6:7, -6:7]
        patch = (br * np.exp(-(ys**2 + xs**2) / 5.0)).astype(np.uint8)
        y0, y1 = max(0, cy - 6), min(h, cy + 7)
        x0, x1 = max(0, cx - 6), min(w, cx + 7)
        raw_frame[y0:y1, x0:x1] = np.maximum(
            raw_frame[y0:y1, x0:x1], patch[:y1 - y0, :x1 - x0])
    tag(INFO, f'Synthetic: peak={raw_frame.max()}')

if raw_frame.max() < 20:
    tag(WARN, f'Peak pixel={raw_frame.max()} — solver_proc would skip this frame. '
              'Check exposure/camera mode.')


# ── Helpers ───────────────────────────────────────────────────────────────────
base_kw = dict(fov_estimate=fov_est, fov_max_error=fov_err, solve_timeout=timeout_ms,
               return_matches=False)


def _solve_blind(centroids):
    t0   = time.monotonic()
    soln = t3.solve_from_centroids(centroids, raw_frame.shape, **base_kw)
    ms   = (time.monotonic() - t0) * 1000
    return soln, ms


def _solve_hint(centroids, hint_q):
    t0   = time.monotonic()
    soln = t3.solve_from_centroids(
        centroids, raw_frame.shape,
        attitude_hint=list(hint_q),
        hint_uncertainty_deg=args.hint_unc,
        strict_hint=False,
        **base_kw,
    )
    ms = (time.monotonic() - t0) * 1000
    return soln, ms


def _solved(soln):
    return soln is not None and soln.get('RA') is not None


def _cap(centroids, n_raw):
    if centroids is not None and len(centroids) > max_c:
        return centroids[:max_c], True
    return centroids, False


# ── Per-backend benchmark ─────────────────────────────────────────────────────
def run_backend(name, extract_fn):
    """
    Run one extractor backend through N extraction reps + one blind solve
    (using the last centroid set) + one hint solve (if blind succeeded).

    Returns a result dict for the summary table.
    """
    sep(f'{name} extractor   N={args.reps}')

    ext_times, star_counts = [], []
    last_centroids = None

    for i in range(args.reps):
        try:
            t0 = time.monotonic()
            centroids_raw, n_raw = extract_fn(raw_frame, sigma)
            ext_ms = (time.monotonic() - t0) * 1000

            centroids, capped = _cap(centroids_raw, n_raw)
            n_used = len(centroids) if centroids is not None else 0
            ext_times.append(ext_ms)
            star_counts.append(n_raw)
            last_centroids = centroids

            cap_note = f' (capped {n_raw}→{max_c})' if capped else ''
            tag(INFO, f'[{i+1:2d}] extract={ext_ms:6.1f} ms  stars={n_raw}{cap_note}')
        except Exception as e:
            tag(FAIL, f'[{i+1:2d}] extraction raised: {e}')

    if not ext_times:
        tag(FAIL, 'All extraction reps failed')
        return None

    avg_ext = sum(ext_times) / len(ext_times)
    avg_n   = sum(star_counts) / len(star_counts)
    print(f'\n  Extraction: avg={avg_ext:.1f} ms  min={min(ext_times):.1f}  '
          f'max={max(ext_times):.1f}  avg_stars={avg_n:.1f}')

    if last_centroids is None or len(last_centroids) < min_c:
        tag(WARN, f'  Too few stars ({len(last_centroids) if last_centroids is not None else 0} '
                  f'< min_centroids={min_c}) — skipping solves')
        return {'name': name, 'avg_ext': avg_ext, 'avg_n': avg_n,
                'blind_ms': None, 'blind_ok': False,
                'hint_ms':  None, 'hint_ok':  False,
                'ra': None, 'dec': None}

    # Blind solve
    print()
    tag(INFO, 'Blind solve (solve_from_centroids, no hint):')
    try:
        soln_b, blind_ms = _solve_blind(last_centroids)
        ok_b = _solved(soln_b)
        lbl  = PASS if ok_b else FAIL
        tag(lbl, f'  {blind_ms:.1f} ms  status={soln_b.get("status","?") if soln_b else "None"}')
        if ok_b:
            tag(PASS, f'  RA={soln_b["RA"]:.4f}°  Dec={soln_b["Dec"]:.4f}°  '
                      f'FOV={soln_b.get("FOV",0):.4f}°  matches={soln_b.get("Matches","?")}')
        hint_q = tuple(soln_b['quaternion']) if ok_b and soln_b.get('quaternion') else None
    except Exception as e:
        tag(FAIL, f'Blind solve raised: {e}')
        soln_b, blind_ms, ok_b, hint_q = None, 0.0, False, None

    # Hint solve
    hint_ms, ok_h = None, False
    if hint_q:
        print()
        tag(INFO, f'Hint solve (uncertainty={args.hint_unc:.1f}°):')
        try:
            soln_h, hint_ms = _solve_hint(last_centroids, hint_q)
            ok_h = _solved(soln_h)
            lbl  = PASS if ok_h else FAIL
            tag(lbl, f'  {hint_ms:.1f} ms  status={soln_h.get("status","?") if soln_h else "None"}')
            if ok_h:
                tag(PASS, f'  RA={soln_h["RA"]:.4f}°  Dec={soln_h["Dec"]:.4f}°  '
                           f'FOV={soln_h.get("FOV",0):.4f}°  matches={soln_h.get("Matches","?")}')
        except Exception as e:
            tag(FAIL, f'Hint solve raised: {e}')
    else:
        tag(WARN, '  Hint solve skipped — no quaternion from blind solve')

    return {
        'name':     name,
        'avg_ext':  avg_ext,
        'avg_n':    avg_n,
        'blind_ms': blind_ms,
        'blind_ok': ok_b,
        'hint_ms':  hint_ms,
        'hint_ok':  ok_h,
        'ra':       soln_b.get('RA')  if ok_b else None,
        'dec':      soln_b.get('Dec') if ok_b else None,
    }


# ── Extractor functions ───────────────────────────────────────────────────────
def _olive_extract(frame, sig):
    c = t3.get_centroids_from_image_fast(frame, sigma=sig)
    n = len(c) if c is not None else 0
    return c, n


def _sycamore_extract(frame, sig):
    raw = _sd.detect_stars(frame, sigma=sig, bin=1, centroid_full_res=True)
    n   = len(raw) if raw else 0
    # (x=col, y=row) → (row, col) for tetra3
    c   = (np.array([[s[1], s[0]] for s in raw], dtype=np.float32)
           if raw else None)
    return c, n


# ── Run backends ──────────────────────────────────────────────────────────────
results = []

# Warm-up pass (not timed)
sep('Warm-up (untimed)')
try:
    c_wu, n_wu = _olive_extract(raw_frame, sigma)
    tag(PASS, f'olive warm-up: {n_wu} stars')
    if sycamore_ok:
        c_sy, n_sy = _sycamore_extract(raw_frame, sigma)
        tag(PASS, f'sycamore warm-up: {n_sy} stars')
except Exception as e:
    tag(WARN, f'Warm-up error (non-fatal): {e}')

results.append(run_backend('Olive   (get_centroids_from_image_fast)', _olive_extract))

if sycamore_ok:
    results.append(run_backend('Sycamore (detect_stars)', _sycamore_extract))
else:
    tag(WARN, 'Sycamore skipped — star_detect wheel not installed')


# ── Summary table ─────────────────────────────────────────────────────────────
sep('Summary')
print(f'  {"Extractor":<42}  {"avg_ext":>8}  {"stars":>6}  '
      f'{"blind_ms":>9}  {"hint_ms":>8}  {"blind_ok":>8}  {"hint_ok":>8}')
hr()
for r in results:
    if r is None:
        continue
    ext  = f'{r["avg_ext"]:.1f} ms'
    n    = f'{r["avg_n"]:.1f}'
    bms  = f'{r["blind_ms"]:.1f} ms' if r["blind_ms"] is not None else '—'
    hms  = f'{r["hint_ms"]:.1f} ms'  if r["hint_ms"]  is not None else '—'
    bok  = 'YES' if r['blind_ok'] else 'NO'
    hok  = 'YES' if r['hint_ok']  else ('NO' if r['blind_ok'] else '—')
    print(f'  {r["name"]:<42}  {ext:>8}  {n:>6}  {bms:>9}  {hms:>8}  {bok:>8}  {hok:>8}')

hr()
print(f'  Frame: {w}x{h}  sigma: {sigma}  FOV: {fov_est:.3f}°±{fov_err:.3f}°  '
      f'timeout: {timeout_ms} ms  reps: {args.reps}  hint_unc: {args.hint_unc:.1f}°')

# Delta row (olive vs sycamore) if both ran
if len(results) == 2 and all(r is not None for r in results):
    ro, rs = results[0], results[1]
    delta_ext   = rs['avg_ext'] - ro['avg_ext']
    sign_ext    = '+' if delta_ext >= 0 else ''
    print(f'\n  Sycamore vs Olive extraction: {sign_ext}{delta_ext:.1f} ms '
          f'({sign_ext}{delta_ext/ro["avg_ext"]*100:.1f}%)')
    if ro['blind_ms'] and rs['blind_ms']:
        delta_b  = rs['blind_ms'] - ro['blind_ms']
        sign_b   = '+' if delta_b >= 0 else ''
        print(f'  Sycamore vs Olive blind solve: {sign_b}{delta_b:.1f} ms')
    if ro['hint_ms'] and rs['hint_ms']:
        delta_h  = rs['hint_ms'] - ro['hint_ms']
        sign_h   = '+' if delta_h >= 0 else ''
        print(f'  Sycamore vs Olive hint solve:  {sign_h}{delta_h:.1f} ms')

print()
