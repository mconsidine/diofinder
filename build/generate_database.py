#!/usr/bin/env python3
"""Generate a tetra3 star database from the Hipparcos catalogue.

Expects hip_main.dat to already be present in the tetra3 package directory
(downloaded by the CI step before this script runs).

Reads SOLVER_DB_PATH from the environment for the output path so that
tetra3's internal relative-path handling is bypassed entirely.

Optional env vars:
  DB_MAX_FOV   Maximum FOV the database will be used for, in degrees.
               Default: 14.0.  Set lower to shrink database size; set higher
               if you need to support a wider instrument.
  DB_MIN_FOV   Minimum FOV to optimise patterns for, in degrees.
               Default: unset (tetra3 picks automatically).  Set to e.g. 8.0
               to skip generating patterns irrelevant for small-FOV instruments,
               which reduces database size and speeds up generation.
"""
import os
import pathlib
import sys

import tetra3

pkg_dir = pathlib.Path(tetra3.__file__).parent
cat = pkg_dir / 'hip_main.dat'
if not cat.exists():
    sys.exit(f'ERROR: hip_main.dat not found at {cat}')
print(f'Star catalogue: {cat} ({cat.stat().st_size / 1e6:.1f} MB)')

save_as = os.environ.get('SOLVER_DB_PATH')
if not save_as:
    sys.exit('ERROR: SOLVER_DB_PATH environment variable is not set')
print(f'Saving database to: {save_as}.npz')

max_fov = float(os.environ.get('DB_MAX_FOV', '14.0'))
min_fov_str = os.environ.get('DB_MIN_FOV', '')
min_fov = float(min_fov_str) if min_fov_str else None

print(f'max_fov={max_fov}°' + (f'  min_fov={min_fov}°' if min_fov else '  min_fov=auto'))

t3 = tetra3.Tetra3(load_database=None)
kwargs = dict(max_fov=max_fov, save_as=save_as)
if min_fov is not None:
    kwargs['min_fov'] = min_fov
t3.generate_database(**kwargs)
print('Done.')
