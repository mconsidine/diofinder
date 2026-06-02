#!/usr/bin/env python3
"""
Test attitude-hint effectiveness across a sequence of shifted star-field images.

The first image is solved blind (no hint).  Every subsequent image is solved
twice — once blind and once with the quaternion from the previous successful
solve — so you can directly compare solve time and success rate.

Backends
  olive    — get_centroids_from_image_fast (default)
  sycamore — detect_stars with matched-filter gate (requires star_detect wheel)

Usage:
  python3 tests/test_hint.py --images img1.png img2.png img3.png
  python3 tests/test_hint.py --images *.png --db /path/to/db.npz --hint-unc 20
  python3 tests/test_hint.py --images *.png --backend sycamore --sigma 7.0
"""

import argparse
import math
import pathlib
import sys
import time

sys.path.insert(0, '/opt/efinder')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_image(path: pathlib.Path):
    import numpy as np
    from PIL import Image
    return np.array(Image.open(path).convert('L'), dtype=np.uint8)


def _solve(t3, arr, *, fov, fov_err, timeout, sigma,
           extractor, hint_q=None, hint_unc=15.0):
    """Split pipeline: extract with *extractor*, then solve.

    *extractor* is a callable(arr, sigma) → (centroids_rowcol, n_stars).

    Returns (soln, n_stars, extract_ms, solve_ms).
    """
    t0 = time.monotonic()
    centroids, n_stars = extractor(arr, sigma)
    extract_ms = (time.monotonic() - t0) * 1000.0

    if n_stars == 0:
        return {'status': 'TooFew'}, 0, extract_ms, 0.0

    kwargs = dict(
        fov_estimate=fov,
        fov_max_error=fov_err,
        solve_timeout=timeout,
        return_matches=False,
    )
    if hint_q is not None:
        kwargs['attitude_hint'] = list(hint_q)
        kwargs['hint_uncertainty_deg'] = hint_unc
        kwargs['strict_hint'] = False

    t1 = _time.monotonic()
    soln = t3.solve_from_centroids(centroids, arr.shape, **kwargs)
    solve_ms = (_time.monotonic() - t1) * 1000.0

    return soln, n_stars, extract_ms, solve_ms


def _angular_sep_deg(ra1, dec1, ra2, dec2):
    r = math.pi / 180.0
    cos_sep = (
        math.sin(dec1 * r) * math.sin(dec2 * r)
        + math.cos(dec1 * r) * math.cos(dec2 * r) * math.cos((ra2 - ra1) * r)
    )
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_sep))))


def _fmt_radec(soln):
    if soln.get('RA') is None:
        return f"NO MATCH  status={soln.get('status', '?')}"
    return (f"RA={soln['RA']:.4f}  Dec={soln['Dec']:.4f}  "
            f"Roll={soln.get('Roll', 0.0):.2f}  matches={soln.get('Matches', '?')}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument('--images', nargs='+', required=True,
                    help='Images in order; first is blind, rest are hint-vs-blind')
    ap.add_argument('--db',      help='tetra3 .npz database path (overrides config)')
    ap.add_argument('--fov',     type=float, help='FOV estimate in degrees')
    ap.add_argument('--fov-err', type=float, dest='fov_err',
                    help='FOV max error in degrees')
    ap.add_argument('--timeout', type=int,   help='Solve timeout in ms')
    ap.add_argument('--sigma',   type=float, help='Detection sigma threshold')
    ap.add_argument('--hint-unc', type=float, default=5.0, dest='hint_unc',
                    help='Hint uncertainty cone in degrees (default 5.0)')
    ap.add_argument('--backend', default='olive', choices=['olive', 'sycamore'],
                    help='Extraction backend (default: olive)')
    ap.add_argument('--gate-mode', default='matched_filter',
                    choices=['matched_filter', 'cedar'],
                    help='Sycamore gate algorithm (default: matched_filter)')
    args = ap.parse_args()

    # Load config when available; fall back to safe defaults
    try:
        from efinder.config import load_config
        cfg = load_config()
        db_path = pathlib.Path(
            cfg.solver_db if cfg.solver_db.startswith('/')
            else f'/var/lib/efinder/{cfg.solver_db}.npz'
        )
        fov     = args.fov     or cfg.fov_deg
        fov_err = args.fov_err or cfg.fov_max_error_deg
        timeout = args.timeout or cfg.solve_timeout_ms
        sigma   = args.sigma   or cfg.detect_sigma
        gate    = args.gate_mode or getattr(cfg, 'sycamore_gate_mode', 'matched_filter')
    except Exception:
        db_path = pathlib.Path('/var/lib/efinder/default_database.npz')
        fov     = args.fov     or 13.5
        fov_err = args.fov_err or 1.0
        timeout = args.timeout or 1500
        sigma   = args.sigma   or 9.0
        gate    = args.gate_mode

    if args.db:
        db_path = pathlib.Path(args.db)

    try:
        import tetra3
        import numpy as np
    except ImportError as e:
        sys.exit(f'ERROR: {e} — run from the efinder venv')

    # Set up extractor
    if args.backend == 'sycamore':
        try:
            import star_detect as _sd
            _sd.set_num_threads(2)
        except ImportError:
            sys.exit('ERROR: sycamore star_detect not installed — '
                     'run from the efinder venv or use --backend olive')

        def _extractor(arr, sig):
            raw  = _sd.detect_stars(arr, sigma=sig, bin=1, centroid_full_res=True,
                                    gate_mode=gate)
            n    = len(raw) if raw else 0
            cent = (np.array([[s[1], s[0]] for s in raw], dtype=np.float64)
                    if raw else None)
            return cent, n
    else:
        def _extractor(arr, sig):
            c = tetra3.Tetra3.__new__(tetra3.Tetra3)  # placeholder; t3 bound below
            raise RuntimeError('_extractor placeholder called before t3 was bound')

    if not db_path.exists():
        sys.exit(f'ERROR: database not found: {db_path}')

    images = [pathlib.Path(x) for x in args.images]
    for img in images:
        if not img.exists():
            sys.exit(f'ERROR: image not found: {img}')

    # Load DB
    print(f'Database : {db_path}')
    t0 = time.monotonic()
    t3 = tetra3.Tetra3(str(db_path))
    print(f'Loaded   : {(time.monotonic() - t0) * 1000:.0f} ms')

    # Bind t3 into the olive extractor now that t3 is available
    if args.backend == 'olive':
        def _extractor(arr, sig):
            c = t3.get_centroids_from_image_fast(arr, sigma=sig)
            n = len(c) if c is not None else 0
            return c, n

    print(f'Backend  : {args.backend}'
          + (f'  gate_mode={gate}' if args.backend == 'sycamore' else ''))
    print(f'FOV      : {fov:.2f} ± {fov_err:.2f} deg  '
          f'timeout={timeout} ms  sigma={sigma}  hint_unc={args.hint_unc:.1f} deg')
    print()

    solve_kw = dict(fov=fov, fov_err=fov_err, timeout=timeout, sigma=sigma,
                    extractor=_extractor)

    # ── Image 0: blind solve ─────────────────────────────────────────────────
    arr0 = _load_image(images[0])
    print(f'[1/{len(images)}] {images[0].name}  {arr0.shape[1]}×{arr0.shape[0]}  ── BLIND SOLVE')

    soln0, n0, ext0, slv0 = _solve(t3, arr0, **solve_kw)
    tot0 = ext0 + slv0
    print(f'  Stars   : {n0}')
    print(f'  Timing  : extract={ext0:.0f} ms  solve={slv0:.0f} ms  total={tot0:.0f} ms')
    print(f'  Result  : {_fmt_radec(soln0)}')

    hint_q = soln0.get('quaternion')
    if hint_q is not None:
        hint_q = tuple(hint_q)
        print(f'  Quat    : w={hint_q[0]:.5f} x={hint_q[1]:.5f} '
              f'y={hint_q[2]:.5f} z={hint_q[3]:.5f}')
    else:
        print('  WARNING : solver returned no quaternion — '
              'hint solves will run without hint')
    print()

    ref_ra  = soln0.get('RA')
    ref_dec = soln0.get('Dec')

    # ── Images 1+: blind vs hint ─────────────────────────────────────────────
    rows = []  # (name, n_stars, tot_blind, tot_hint, blind_ok, hint_ok)

    for i, img_path in enumerate(images[1:], start=2):
        arr = _load_image(img_path)
        print(f'[{i}/{len(images)}] {img_path.name}  {arr.shape[1]}×{arr.shape[0]}')
        print(f'  Hint from: '
              + (f'image [{i-1}] q=({hint_q[0]:.4f}, {hint_q[1]:.4f}, '
                 f'{hint_q[2]:.4f}, {hint_q[3]:.4f})'
                 if hint_q is not None else 'none (previous solve failed)'))

        # Blind
        soln_b, nb, ext_b, slv_b = _solve(t3, arr, **solve_kw)
        tot_b = ext_b + slv_b
        print(f'  BLIND   : stars={nb:3d}  ext={ext_b:5.0f} ms  '
              f'slv={slv_b:5.0f} ms  total={tot_b:5.0f} ms  → {_fmt_radec(soln_b)}')

        # Hint
        soln_h, nh, ext_h, slv_h = _solve(
            t3, arr, hint_q=hint_q, hint_unc=args.hint_unc, **solve_kw)
        tot_h = ext_h + slv_h
        print(f'  HINT    : stars={nh:3d}  ext={ext_h:5.0f} ms  '
              f'slv={slv_h:5.0f} ms  total={tot_h:5.0f} ms  → {_fmt_radec(soln_h)}')

        # Angular separation from image 0
        ra_h, dec_h = soln_h.get('RA'), soln_h.get('Dec')
        if ref_ra is not None and ra_h is not None:
            sep = _angular_sep_deg(ref_ra, ref_dec, ra_h, dec_h)
            print(f'  Sep from img1: {sep:.3f} deg')

        # Speedup
        if tot_b > 0 and tot_h > 0:
            speedup = tot_b / tot_h
            delta   = tot_b - tot_h
            sign    = '+' if delta >= 0 else ''
            print(f'  Speedup : {speedup:.2f}x  ({sign}{delta:.0f} ms)')

        rows.append((img_path.name, nb, tot_b, tot_h,
                     soln_b.get('RA') is not None,
                     soln_h.get('RA') is not None))

        # Advance hint for next image (chain)
        q_new = soln_h.get('quaternion')
        if q_new is not None:
            hint_q = tuple(q_new)
        print()

    # ── Summary ──────────────────────────────────────────────────────────────
    if not rows:
        return

    W = 26
    print('─' * 72)
    print(f'{"Image":<{W}}  {"Stars":>5}  {"Blind ms":>8}  '
          f'{"Hint ms":>7}  {"Speedup":>7}  B  H')
    print('─' * 72)
    for name, n, tb, th, ok_b, ok_h in rows:
        spd = f'{tb/th:.2f}x' if th > 0 else '   n/a'
        print(f'{name:<{W}}  {n:>5}  {tb:>8.0f}  {th:>7.0f}  '
              f'{spd:>7}  {"✓" if ok_b else "✗"}  {"✓" if ok_h else "✗"}')
    print('─' * 72)

    avg_b = sum(r[2] for r in rows) / len(rows)
    avg_h = sum(r[3] for r in rows) / len(rows)
    n_ok_b = sum(1 for r in rows if r[4])
    n_ok_h = sum(1 for r in rows if r[5])
    print(f'{"Average":<{W}}  {"":>5}  {avg_b:>8.0f}  {avg_h:>7.0f}  '
          f'{avg_b/avg_h:>6.2f}x  {n_ok_b}/{len(rows)}  {n_ok_h}/{len(rows)}')
    print()
    print(f'Hint saved {avg_b - avg_h:+.0f} ms on average per frame.')


if __name__ == '__main__':
    main()
