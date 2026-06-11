#!/usr/bin/env bash
# Re-vendor locally-built wheels into vendor/wheels/ — the no-CI equivalent of
# the "Remove stale ... / Commit and push" steps of vendor-sycamore.yml and
# vendor-binaries.yml. Stages the change; commits only with --commit; never
# pushes (review with `git show`, then push yourself).
#
# Usage:
#   build/local/vendor-wheels.sh [--commit] [wheel ...]
#
# With no wheel arguments, vendors every *.whl in OUT_DIR
# (default build/local/out).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

DO_COMMIT=0
WHEELS=()
for arg in "$@"; do
    case "$arg" in
        --commit) DO_COMMIT=1 ;;
        *) WHEELS+=("$arg") ;;
    esac
done
if [ ${#WHEELS[@]} -eq 0 ]; then
    while IFS= read -r w; do WHEELS+=("$w"); done < <(ls "$OUT_DIR"/*.whl 2>/dev/null)
fi
[ ${#WHEELS[@]} -gt 0 ] || { echo "No wheels found (looked in $OUT_DIR)"; exit 1; }

mkdir -p "$REPO_ROOT/vendor/wheels"
MSG_PARTS=()
for whl in "${WHEELS[@]}"; do
    base=$(basename "$whl")
    case "$base" in
        star_detect-*) stale='star_detect-*.whl' ;;
        tetra3-*)      stale='tetra3-*.whl tetra3rs-*.whl' ;;
        *) echo "Skipping unrecognized wheel: $base"; continue ;;
    esac
    # Same stale-clearing the vendor workflows do
    for pattern in $stale; do
        find "$REPO_ROOT/vendor/wheels" -name "$pattern" ! -name "$base" -delete
    done
    cp "$whl" "$REPO_ROOT/vendor/wheels/$base"
    echo "Vendored: vendor/wheels/$base"
    MSG_PARTS+=("$base")
done

git -C "$REPO_ROOT" add vendor/wheels/
if git -C "$REPO_ROOT" diff --staged --quiet; then
    echo "No vendor changes (wheels already up to date)"
elif [ "$DO_COMMIT" = "1" ]; then
    git -C "$REPO_ROOT" commit -m "vendor: update wheels (local build): ${MSG_PARTS[*]} [skip ci]"
    echo "Committed. Review with 'git show', then push."
else
    echo "Staged. Commit with: git commit -m 'vendor: ... [skip ci]'  (or rerun with --commit)"
fi
