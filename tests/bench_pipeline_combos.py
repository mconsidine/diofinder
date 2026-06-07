#!/usr/bin/env python3
"""
eFinder pipeline-combination benchmark.

Times both sycamore-extract + olive-solve paths on the same sky frame so you
can compare total latency, extractor speed, and solve reliability in one run.

─── What each path represents ────────────────────────────────────────────────

  Path 1  sycamore detect_stars  +  solve_from_centroids  (blind)
          The split pipeline the daemon uses on every frame before it has ever
          solved.  Extracting centroids separately lets the daemon gate on star
          count, apply the boresight offset, and cap the centroid list before
          handing off to the solver.  The blind solve searches the entire sky.

  Path 2  sycamore detect_stars  +  solve_from_centroids  (+ hint)
          Same split pipeline, but after the first successful solve the daemon
          passes the previous solution's quaternion as an attitude hint.  The
          solver restricts its search to a cone around that hint (default ±5°),
          which cuts solve time substantially on a tracking mount.  This is what
          the daemon uses for every subsequent frame once it has locked on.

─── Optional sweeps ──────────────────────────────────────────────────────────

  --hint-sweep   Vary hint_uncertainty_deg (0.5°–30°) to find the cone size
                 that gives the best blend of speed and reliability on your sky.

  --sigma-sweep  Sweep sigma 3–12 to show how the detection threshold trades
                 star count against extraction noise.  Helps pick the right
                 sigma value for your exposure and gain settings.

─── For a deeper extractor timing benchmark see ──────────────────────────────

  tests/bench_extractor_compare.py  — times sycamore extraction in isolation,
  reports extraction time, star count, and solve outcomes in a summary table.

Usage:
  # Daemon stopped, using a saved capture:
  sudo /opt/efinder/venv/bin/python3 tests/bench_pipeline_combos.py \\
      --image /var/lib/efinder/captures/capture_20260101_123456.png

  # Live frame from running daemon:
  sudo /opt/efinder/venv/bin/python3 tests/bench_pipeline_combos.py --live-shm

  # Extra reps, hint sweep:
  sudo /opt/efinder/venv/bin/python3 tests/bench_pipeline_combos.py \\
      --image img.png --reps 10 --hint-sweep

  # Sigma sweep:
  sudo /opt/efinder/venv/bin/python3 tests/bench_pipeline_combos.py \\
      --image img.png --sigma-sweep

  # Override solver parameters:
  sudo /opt/efinder/venv/bin/python3 tests/bench_pipeline_combos.py \\
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
                help='Timed repetitions per path (default 5)')
ap.add_argument('--timeout',  type=int,   help='Solve timeout in ms (overrides config)')
ap.add_argument('--sigma',    type=float, help='Override detect_sigma')
ap.add_argument('--fov',      type=float, help='Override FOV estimate in degrees')
ap.add_argument('--fov-err',  type=float, help='Override FOV max error in degrees')
ap.add_argument('--hint-unc', type=float, default=5.0,
                help='Hint uncertainty cone for path 2 (default 5.0°)')
ap.add_argument('--hint-sweep', action='store_true',
                help='Sweep hint_uncertainty_deg from 0.5° to 30° after main table')
ap.add_argument('--sigma-sweep', action='store_true',
                help='Sweep sigma 3–12 to show detection vs solve trade-off')
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
    import pathlib as _pl
    db_path = (cfg.solver_db if cfg.solver_db.startswith('/')
               else f'/var/lib/efinder/{cfg.solver_db}.npz')
    t3 = _t3.Tetra3(db_path)
    tag(PASS, f'tetra3 (olive-solve)  db={db_path}')
except Exception as e:
    tag(FAIL, f'tetra3 / database: {e}')
    sys.exit(1)

try:
    import star_detect as _sd
    _sd.set_num_threads(2)
    tag(PASS, 'sycamore star_detect')
except ImportError as e:
    tag(FAIL, f'sycamore star_detect not installed: {e}')
    sys.exit(1)


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
    tag(INFO, '  (Timing is valid; blind plate solve will fail on a synthetic field)')
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
    tag(WARN, f'Peak pixel={raw_frame.max()} < 20 — solver_proc would skip this frame. '
              'Check exposure/camera mode.')


# ── Shared helpers ─────────────────────────────────────────────────────────────
base_kw = dict(fov_estimate=fov_est, fov_max_error=fov_err, solve_timeout=timeout_ms,
               return_matches=False)


def _stats(vals):
    if not vals:
        return 'n/a'
    return (f'avg={sum(vals)/len(vals):.1f}  '
            f'min={min(vals):.1f}  max={max(vals):.1f} ms')


def _extract(frame, sig):
    t0  = time.monotonic()
    raw = _sd.detect_stars(frame, sigma=sig, bin=1, centroid_full_res=True)
    ms  = (time.monotonic() - t0) * 1000
    n   = len(raw) if raw else 0
    # (x=col, y=row) → (row, col) for tetra3
    cent = (np.array([[s[1], s[0]] for s in raw], dtype=np.float64)
            if raw else None)
    if cent is not None and len(cent) > max_c:
        cent = cent[:max_c]
    return cent, n, ms


def _solve_blind(centroids, frame_shape):
    t0   = time.monotonic()
    soln = t3.solve_from_centroids(centroids, frame_shape, **base_kw)
    ms   = (time.monotonic() - t0) * 1000
    return soln, ms


def _solve_hint(centroids, frame_shape, hint_q, hint_unc):
    t0   = time.monotonic()
    soln = t3.solve_from_centroids(
        centroids, frame_shape,
        attitude_hint=list(hint_q),
        hint_uncertainty_deg=hint_unc,
        strict_hint=False,
        **base_kw,
    )
    ms = (time.monotonic() - t0) * 1000
    return soln, ms


def _solved(soln):
    return soln is not None and soln.get('RA') is not None


# ── Warm-up ────────────────────────────────────────────────────────────────────
sep('Warm-up')
try:
    cent_wu, n_wu, ext_wu = _extract(raw_frame, sigma)
    tag(PASS, f'detect_stars: {ext_wu:.0f} ms  stars={n_wu}')
    if n_wu < min_c:
        tag(WARN, f'{n_wu} stars < min_centroids={min_c} — solves will likely fail. '
                  f'Try --sigma lower than {sigma:.1f}.')
    if n_wu > 0:
        soln_wu, slv_wu = _solve_blind(cent_wu, raw_frame.shape)
        tag(PASS if _solved(soln_wu) else WARN,
            f'solve_from_centroids: {slv_wu:.0f} ms  '
            f'status={soln_wu.get("status", "?") if soln_wu else "None"}')
        seed_q = tuple(soln_wu['quaternion']) if soln_wu and soln_wu.get('quaternion') else None
    else:
        seed_q = None
except Exception as e:
    tag(FAIL, f'Warm-up failed: {e}')
    sys.exit(1)

summary = {}

# ── Path 1: sycamore split pipeline, blind ────────────────────────────────────
sep(f'Path 1: sycamore split pipeline, blind   N={args.reps}')
tag(INFO, 'sycamore detect_stars + solve_from_centroids — blind')

p1_ext, p1_slv, p1_tot, p1_solved = [], [], [], 0
last_good_q = seed_q  # seed from warm-up if available

for i in range(args.reps):
    try:
        cent, n, ext_ms = _extract(raw_frame, sigma)
        p1_ext.append(ext_ms)

        if n < min_c:
            tag(WARN, f'[{i+1:2d}] ext={ext_ms:.0f} ms  '
                      f'stars={n} < min_centroids={min_c} — skipping solve')
            p1_tot.append(ext_ms); p1_slv.append(0)
            continue

        soln, slv_ms = _solve_blind(cent, raw_frame.shape)
        total = ext_ms + slv_ms
        p1_slv.append(slv_ms); p1_tot.append(total)

        ok = _solved(soln)
        if ok:
            p1_solved += 1
            if soln.get('quaternion'):
                last_good_q = tuple(soln['quaternion'])
        lbl = PASS if ok else FAIL
        tag(lbl, f'[{i+1:2d}] total={total:.0f} ms  '
                 f'ext={ext_ms:.0f} ms  slv={slv_ms:.0f} ms  '
                 f'stars={n}  {soln.get("status","?") if soln else "None"}')
        if ok and soln.get('RA'):
            tag(PASS, f'      RA={soln["RA"]:.4f}  Dec={soln["Dec"]:.4f}  '
                      f'FOV={soln.get("FOV",0):.4f}°  m={soln.get("Matches","?")}')
    except Exception as e:
        tag(FAIL, f'[{i+1:2d}] raised: {e}')

if p1_tot:
    avg_e = sum(p1_ext)/len(p1_ext)
    avg_s = sum(p1_slv)/len(p1_slv) if p1_slv else 0
    avg_t = sum(p1_tot)/len(p1_tot)
    print(f'\n  Solved: {p1_solved}/{len(p1_tot)}  '
          f'ext avg={avg_e:.1f} ms  slv avg={avg_s:.1f} ms  total avg={avg_t:.1f} ms')
summary[1] = {'solved': p1_solved, 'n': len(p1_tot),
              'avg_ext': sum(p1_ext)/len(p1_ext) if p1_ext else 0,
              'avg_slv': sum(p1_slv)/len(p1_slv) if p1_slv else 0,
              'avg_tot': sum(p1_tot)/len(p1_tot) if p1_tot else 0}

# ── Path 2: sycamore split pipeline + hint ────────────────────────────────────
sep(f'Path 2: sycamore split pipeline + hint ({args.hint_unc:.1f}°)   N={args.reps}')

if last_good_q is None:
    tag(WARN, 'No quaternion available (path 1 never solved) — path 2 skipped')
    summary[2] = None
else:
    tag(INFO, f'Hint q=({last_good_q[0]:.4f}, {last_good_q[1]:.4f}, '
              f'{last_good_q[2]:.4f}, {last_good_q[3]:.4f})  unc={args.hint_unc:.1f}°')
    p2_ext, p2_slv, p2_tot, p2_solved = [], [], [], 0

    for i in range(args.reps):
        try:
            cent, n, ext_ms = _extract(raw_frame, sigma)
            p2_ext.append(ext_ms)

            if n < min_c:
                tag(WARN, f'[{i+1:2d}] ext={ext_ms:.0f} ms  stars={n} — skipping')
                p2_tot.append(ext_ms); p2_slv.append(0)
                continue

            soln, slv_ms = _solve_hint(cent, raw_frame.shape, last_good_q, args.hint_unc)
            total = ext_ms + slv_ms
            p2_slv.append(slv_ms); p2_tot.append(total)

            ok = _solved(soln)
            if ok:
                p2_solved += 1
            lbl = PASS if ok else FAIL
            tag(lbl, f'[{i+1:2d}] total={total:.0f} ms  '
                     f'ext={ext_ms:.0f} ms  slv={slv_ms:.0f} ms  '
                     f'stars={n}  {soln.get("status","?") if soln else "None"}')
            if ok and soln.get('RA'):
                tag(PASS, f'      RA={soln["RA"]:.4f}  Dec={soln["Dec"]:.4f}  '
                           f'FOV={soln.get("FOV",0):.4f}°  m={soln.get("Matches","?")}')
        except Exception as e:
            tag(FAIL, f'[{i+1:2d}] raised: {e}')

    if p2_tot:
        avg_e = sum(p2_ext)/len(p2_ext)
        avg_s = sum(p2_slv)/len(p2_slv) if p2_slv else 0
        avg_t = sum(p2_tot)/len(p2_tot)
        print(f'\n  Solved: {p2_solved}/{len(p2_tot)}  '
              f'ext avg={avg_e:.1f} ms  slv avg={avg_s:.1f} ms  total avg={avg_t:.1f} ms')

        # Compare path 1 vs 2
        if summary[1]['avg_tot'] > 0:
            delta = summary[1]['avg_tot'] - avg_t
            print(f'  Hint vs blind: {avg_t:.1f} ms vs {summary[1]["avg_tot"]:.1f} ms  '
                  f'(hint {"saves" if delta >= 0 else "costs"} {abs(delta):.1f} ms avg)')

    summary[2] = {'solved': p2_solved, 'n': len(p2_tot),
                  'avg_ext': sum(p2_ext)/len(p2_ext) if p2_ext else 0,
                  'avg_slv': sum(p2_slv)/len(p2_slv) if p2_slv else 0,
                  'avg_tot': sum(p2_tot)/len(p2_tot) if p2_tot else 0}


# ── Summary table ──────────────────────────────────────────────────────────────
sep('Summary')
print(f'  {"#":<2}  {"Pipeline":<50}  {"avg_ext":>8}  {"avg_slv":>8}  '
      f'{"avg_tot":>8}  {"solved":>8}')
hr()
names = {
    1: 'sycamore  split pipeline, blind',
    2: f'sycamore  split pipeline + hint ({args.hint_unc:.1f}°)',
}
for n, label in names.items():
    s = summary.get(n)
    if s is None:
        print(f'  {n:<2}  {label:<50}  {"—":>8}  {"—":>8}  {"—":>8}  {"skipped":>8}')
        continue
    ext = f'{s.get("avg_ext",0):.1f}ms'
    slv = f'{s.get("avg_slv",0):.1f}ms'
    tot = f'{s.get("avg_tot",0):.1f}ms'
    rat = f'{s["solved"]}/{s["n"]}'
    print(f'  {n:<2}  {label:<50}  {ext:>8}  {slv:>8}  {tot:>8}  {rat:>8}')
hr()
print(f'  Frame: {w}x{h}  FOV: {fov_est:.3f}°±{fov_err:.3f}°  '
      f'timeout: {timeout_ms} ms  sigma: {sigma}  reps: {args.reps}')


# ── Hint-uncertainty sweep ─────────────────────────────────────────────────────
if args.hint_sweep:
    sep('Hint-uncertainty sweep')
    if last_good_q is None:
        tag(WARN, 'No seed quaternion — cannot run sweep (no successful blind solve)')
    else:
        HINT_REPS = max(3, args.reps)
        uncertainties = [0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 20.0, 30.0]

        # First get a fresh centroid set
        try:
            cent_h, n_h, _ = _extract(raw_frame, sigma)
        except Exception as e:
            tag(FAIL, f'Cannot extract centroids for hint sweep: {e}')
            cent_h, n_h = None, 0

        if n_h < min_c:
            tag(WARN, f'Only {n_h} stars — hint sweep results may be unreliable')

        for strict in (False, True):
            label = 'strict_hint=True ' if strict else 'strict_hint=False'
            print(f'\n  {label}  ({HINT_REPS} reps each)\n')
            print(f'  {"unc_deg":>8}  {"avg_slv_ms":>12}  {"solved":>8}')
            hr()
            for unc in uncertainties:
                times_h, ok_h = [], 0
                for _ in range(HINT_REPS):
                    try:
                        t0   = time.monotonic()
                        soln = t3.solve_from_centroids(
                            cent_h, raw_frame.shape,
                            attitude_hint=list(last_good_q),
                            hint_uncertainty_deg=unc,
                            strict_hint=strict,
                            **base_kw,
                        )
                        ms = (time.monotonic() - t0) * 1000
                        times_h.append(ms)
                        if _solved(soln):
                            ok_h += 1
                    except Exception:
                        pass
                avg_h  = f'{sum(times_h)/len(times_h):.1f}ms' if times_h else '—'
                sol_h  = f'{ok_h}/{HINT_REPS}'
                marker = ' ← default' if abs(unc - args.hint_unc) < 0.01 else ''
                print(f'  {unc:>8.1f}  {avg_h:>12}  {sol_h:>8}{marker}')


# ── Sigma sweep ────────────────────────────────────────────────────────────────
if args.sigma_sweep:
    sep('Sigma sweep  —  star count vs detection threshold')
    print(f'  {"sigma":>6}  {"stars":>6}  {"ext_ms":>8}  note')
    hr()
    for sig in [3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 12.0]:
        try:
            _, n_s, ms_s = _extract(raw_frame, sig)
            note  = ' ← min_centroids met' if n_s >= min_c else ''
            cur   = ' ← current' if abs(sig - sigma) < 0.05 else ''
            capped = f' (capped to {max_c})' if n_s > max_c else ''
            print(f'  {sig:6.1f}  {n_s:6d}  {ms_s:8.1f}{note or cur}{capped}')
        except Exception as e:
            print(f'  {sig:6.1f}  ERROR: {e}')
    print(f'\n  min_centroids={min_c}  max_solve_stars={max_c}  current sigma={sigma}')

print()
