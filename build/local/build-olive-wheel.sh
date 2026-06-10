#!/usr/bin/env bash
# Cross-build the olive-solve tetra3-py wheel for the Pi Zero 2W (aarch64,
# Cortex-A53), locally — the no-CI equivalent of the build-wheel job in
# .github/workflows/vendor-binaries.yml. Flags match that job exactly so the
# local wheel is equivalent to the CI-vendored one.
#
# tetra3-py uses PyO3 abi3 (py38+): one wheel works on every Pi Python, and
# no target Python interpreter is needed to build it.
#
# Usage:
#   build/local/build-olive-wheel.sh [git-ref]
#
# Env:
#   OLIVE_SRC    path to an existing olive-solve checkout
#                (default: ../olive-solve sibling, else auto-clone)
#   SOLVER_ONLY  set to 1 to build with --no-default-features (drops the
#                Extractor/FastExtractor code; requires a ref that has the
#                'extractor' cargo feature). diofinder extracts with sycamore,
#                so the solver-only wheel is sufficient and smaller.
#   OUT_DIR      where the wheel is copied (default build/local/out)
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

REF="${1:-}"

SRC=$(resolve_src olive-solve "${OLIVE_SRC:-}" \
      https://github.com/mconsidine/olive-solve "$REF")
echo "Source: $SRC ($(git -C "$SRC" describe --tags --always))"

require_cross_toolchain

EXTRA_ARGS=()
if [ "${SOLVER_ONLY:-0}" = "1" ]; then
    EXTRA_ARGS+=(--no-default-features)
    echo "Building solver-only wheel (--no-default-features)"
fi

# Same flags as vendor-binaries.yml:
#   target-cpu=cortex-a53   A53 scheduling (matches olive-solve's
#                           .cargo/config.toml on refs that have it; setting
#                           RUSTFLAGS here keeps older refs identical to CI)
#   codegen-units=1         maximum intra-crate optimisation
#   lto=thin                workspace sets lto="fat", overridden to avoid
#                           cross-linker plugin conflicts
( cd "$SRC/tetra3-py" && \
  CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc \
  RUSTFLAGS="-C target-cpu=cortex-a53 -C codegen-units=1" \
  CARGO_PROFILE_RELEASE_LTO=thin \
  maturin build --release \
      --target aarch64-unknown-linux-gnu \
      --compatibility linux \
      --out dist "${EXTRA_ARGS[@]}" )

WHL=$(ls -t "$SRC"/tetra3-py/dist/tetra3-*aarch64*.whl | head -1)
cp "$WHL" "$OUT_DIR/"
echo "Wheel: $OUT_DIR/$(basename "$WHL")"
