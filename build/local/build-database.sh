#!/usr/bin/env bash
# Generate the tetra3 solver star database locally — the no-CI equivalent of
# the "Generate tetra3 star database" step in release.yml. Uses the same
# esa/tetra3 code, the same hip_main.dat mirror, and the same
# build/generate_database.py, so the output matches a CI-built database.
#
# Usage:
#   build/local/build-database.sh
#
# Env (same knobs as the workflow):
#   DB_MAX_FOV  max FOV in degrees (default 14.0)
#   DB_MIN_FOV  min FOV in degrees (default unset = auto)
#   OUT_DIR     output dir for solver_database.npz (default build/local/out)
#
# The venv and the ~50 MB catalogue download are cached under build/local/,
# so reruns only pay the generation time.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

VENV="$REPO_ROOT/build/local/.tetra3-venv"
CAT_CACHE="$REPO_ROOT/build/local/hip_main.dat"

if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
    # tetra3 is only on GitHub, not PyPI (same install as release.yml)
    "$VENV/bin/pip" install --quiet "git+https://github.com/esa/tetra3.git"
fi

TETRA3_DIR=$("$VENV/bin/python" -c 'import pathlib, tetra3; print(pathlib.Path(tetra3.__file__).parent)')
echo "tetra3 package dir: $TETRA3_DIR"

if [ ! -s "$CAT_CACHE" ]; then
    echo "Downloading hip_main.dat from mconsidine/astro_databases mirror..."
    curl -fsSL \
        https://raw.githubusercontent.com/mconsidine/astro_databases/main/hip_main.dat.gz \
        | gunzip > "$CAT_CACHE"
fi
cp "$CAT_CACHE" "$TETRA3_DIR/hip_main.dat"

SOLVER_DB_PATH="$OUT_DIR/solver_database" \
DB_MAX_FOV="${DB_MAX_FOV:-14.0}" \
DB_MIN_FOV="${DB_MIN_FOV:-}" \
    "$VENV/bin/python" "$REPO_ROOT/build/generate_database.py"

ls -lh "$OUT_DIR/solver_database.npz"
echo "Install on the Pi at /var/lib/diofinder/solver_database.npz"
