#!/usr/bin/env python3
"""
Solve a star-field image with sycamore extraction + olive-solve (tetra3-py).

Pipeline: sycamore detect_stars (matched_filter gate) → solve_from_centroids.

Usage (on device):
  sudo /opt/efinder/venv/bin/python3 tests/solve_image.py --image /path/to/image.jpg

  # Explicit database:
  python3 tests/solve_image.py --image image.jpg --db /path/to/database.npz

Defaults are read from /etc/efinder/efinder.conf when present.
"""

import argparse
import pathlib
import sys
import time

sys.path.insert(0, '/opt/efinder')


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--image', required=True, help='Path to star-field image (JPG/PNG)')
    p.add_argument('--db', help='Path to tetra3 .npz database (overrides config)')
    p.add_argument('--fov', type=float, help='FOV estimate in degrees (overrides config)')
    p.add_argument('--fov-err', type=float, help='FOV max error in degrees (overrides config)')
    p.add_argument('--timeout', type=int, help='Solve timeout in ms (overrides config)')
    p.add_argument('--sigma', type=float, help='Detection sigma threshold (overrides config)')
    p.add_argument('--reps', type=int, default=1, help='Repetitions for timing (default 1)')
    args = p.parse_args()

    # Load config if available; fall back to defaults
    try:
        from efinder.config import load_config
        cfg = load_config()
        db_path  = pathlib.Path(cfg.solver_db if cfg.solver_db.startswith('/') else
                                f'/var/lib/efinder/{cfg.solver_db}.npz')
        fov      = args.fov      or cfg.fov_deg
        fov_err  = args.fov_err  or cfg.fov_max_error_deg
        timeout  = args.timeout  or cfg.solve_timeout_ms
        sigma    = args.sigma    or cfg.detect_sigma
    except Exception:
        db_path  = pathlib.Path('/var/lib/efinder/default_database.npz')
        fov      = args.fov     or 13.5
        fov_err  = args.fov_err or 1.0
        timeout  = args.timeout or 1500
        sigma    = args.sigma   or 7.0

    if args.db:
        db_path = pathlib.Path(args.db)

    # Load image
    img_path = pathlib.Path(args.image)
    if not img_path.exists():
        sys.exit(f'ERROR: image not found at {img_path}')

    try:
        import numpy as np
        from PIL import Image
        arr = np.array(Image.open(img_path).convert('L'), dtype=np.uint8)
    except Exception as e:
        sys.exit(f'ERROR loading image: {e}')

    # Load sycamore extractor
    try:
        import star_detect as _sd
        _sd.set_num_threads(2)
    except ImportError:
        sys.exit('ERROR: sycamore star_detect not installed '
                 '(run from the efinder venv)')

    # Load tetra3 database
    print(f'Database : {db_path}')
    if not db_path.exists():
        sys.exit(f'ERROR: database not found at {db_path}')

    try:
        import tetra3
    except ImportError:
        sys.exit('ERROR: tetra3 not installed (run from the efinder venv)')

    t0 = time.monotonic()
    t3 = tetra3.Tetra3(str(db_path))
    print(f'Loaded   : {(time.monotonic() - t0)*1000:.0f} ms')

    print(f'Image    : {img_path.name}  {arr.shape[1]}x{arr.shape[0]}')
    print(f'FOV      : {fov:.2f} deg  +/-{fov_err:.2f} deg')
    print(f'Timeout  : {timeout} ms')
    print(f'Sigma    : {sigma}')
    print()

    solve_kwargs = dict(
        fov_estimate=fov,
        fov_max_error=fov_err,
        solve_timeout=timeout,
    )

    times = []
    result = None

    for i in range(args.reps):
        t0  = time.monotonic()
        raw = _sd.detect_stars(arr, sigma=sigma, bin=1, centroid_full_res=True)
        n   = len(raw) if raw else 0
        cent = (np.array([[s[1], s[0]] for s in raw], dtype=np.float64)
                if raw else None)
        if cent is None or n == 0:
            elapsed = (time.monotonic() - t0) * 1000
            times.append(elapsed)
            print(f'  [{i+1:2d}] {elapsed:6.0f} ms  TooFew (stars={n})')
            continue
        result = t3.solve_from_centroids(cent, arr.shape, **solve_kwargs)
        elapsed = (time.monotonic() - t0) * 1000
        times.append(elapsed)
        status = result.get('status', 'unknown') if result else 'None'
        print(f'  [{i+1:2d}] {elapsed:6.0f} ms  {status}  stars={n}')

    if args.reps > 1:
        print(f'\n  avg={sum(times)/len(times):.0f} ms  '
              f'min={min(times):.0f} ms  max={max(times):.0f} ms')

    print()
    if result and result.get('RA') is not None:
        print(f'SOLVED')
        print(f'  RA   : {result["RA"]:.6f} deg')
        print(f'  Dec  : {result["Dec"]:.6f} deg')
        if result.get('Roll') is not None:
            print(f'  Roll : {result["Roll"]:.6f} deg')
        if result.get('FOV') is not None:
            print(f'  FOV  : {result["FOV"]:.4f} deg')
        if result.get('Matches') is not None:
            print(f'  Matches: {result["Matches"]}')
    else:
        status = result.get('status', 'unknown') if result else 'unknown'
        print(f'NO MATCH  (status={status})')
        print(f'  Try: --fov <degrees>  --fov-err <degrees>  --timeout <ms>  --sigma <value>')


if __name__ == '__main__':
    main()
