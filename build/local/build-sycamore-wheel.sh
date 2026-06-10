#!/usr/bin/env bash
# Cross-build the sycamore-extract star_detect wheel for the Pi Zero 2W
# (aarch64, Cortex-A53), locally — the no-CI equivalent of sycamore-extract's
# build.yml release job.
#
# Usage:
#   build/local/build-sycamore-wheel.sh [git-ref]
#
# Env:
#   SYCAMORE_SRC  path to an existing sycamore-extract checkout
#                 (default: ../sycamore-extract sibling, else auto-clone)
#   PYVER         CPython version for the wheel (default 3.13 = Pi OS Bookworm)
#   OUT_DIR       where the wheel is copied (default build/local/out)
#
# The Cortex-A53 tuning comes from sycamore's committed .cargo/config.toml —
# no RUSTFLAGS needed (and none set, so the config file is honored).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

PYVER="${PYVER:-3.13}"
REF="${1:-}"

SRC=$(resolve_src sycamore-extract "${SYCAMORE_SRC:-}" \
      https://github.com/mconsidine/sycamore-extract "$REF")
echo "Source: $SRC ($(git -C "$SRC" describe --tags --always))"

require_cross_toolchain

# --compatibility linux tags the wheel linux_aarch64 directly. The CI release
# wheels are manylinux2014 (built inside maturin-action's docker image); a
# plain cross-build against Ubuntu's cross glibc can't honestly claim
# manylinux2014, and linux_aarch64 installs fine on the Pi. This replaces the
# old "rename manylinux_2_34 -> linux_aarch64" hack from sycamore's CLAUDE.md.
( cd "$SRC" && \
  CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc \
  maturin build --release \
      --target aarch64-unknown-linux-gnu \
      --interpreter "python${PYVER}" \
      --compatibility linux )

WHL=$(ls -t "$SRC"/target/wheels/star_detect-*-cp${PYVER//./}-*aarch64*.whl | head -1)
cp "$WHL" "$OUT_DIR/"
echo "Wheel: $OUT_DIR/$(basename "$WHL")"
