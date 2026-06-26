#!/usr/bin/env bash
# Stage locally-built wheels into vendor/wheels/ for a local image build or
# on-device testing. Wheels are NOT committed to git (vendor/wheels/ is
# gitignored): CI and diofinder-update pull wheels from the source repos'
# GitHub releases, and a wheel staged here overrides that download.
#
# Usage:
#   build/local/vendor-wheels.sh [wheel ...]
#
# With no wheel arguments, stages every *.whl in OUT_DIR
# (default build/local/out).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

WHEELS=("$@")
if [ ${#WHEELS[@]} -eq 0 ]; then
    while IFS= read -r w; do WHEELS+=("$w"); done < <(ls "$OUT_DIR"/*.whl 2>/dev/null)
fi
[ ${#WHEELS[@]} -gt 0 ] || { echo "No wheels found (looked in $OUT_DIR)"; exit 1; }

mkdir -p "$REPO_ROOT/vendor/wheels"
for whl in "${WHEELS[@]}"; do
    base=$(basename "$whl")
    case "$base" in
        star_detect-*) stale='star_detect-*.whl' ;;
        tetra3-*)      stale='tetra3-*.whl tetra3rs-*.whl' ;;
        *) echo "Skipping unrecognized wheel: $base"; continue ;;
    esac
    # Clear stale variants so install scripts pick up exactly one wheel
    for pattern in $stale; do
        find "$REPO_ROOT/vendor/wheels" -name "$pattern" ! -name "$base" -delete
    done
    cp "$whl" "$REPO_ROOT/vendor/wheels/$base"
    echo "Staged: vendor/wheels/$base (local override; not committed)"
done
