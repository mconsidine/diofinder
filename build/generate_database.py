#!/usr/bin/env python3
"""Generate a tetra3 star database from the Hipparcos catalogue.

Expects hip_main.dat to already be present in the tetra3 package directory
(downloaded by the CI step before this script runs).

Reads SOLVER_DB_PATH from the environment for the output path so that
tetra3's internal relative-path handling is bypassed entirely.
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

t3 = tetra3.Tetra3(load_database=None)
t3.generate_database(
    max_fov=14.0,
    save_as=save_as,
)
print('Done.')
