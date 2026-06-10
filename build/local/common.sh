#!/usr/bin/env bash
# Common helpers for the local (no-CI) build scripts.
# Mirrors the toolchain setup of .github/workflows/{vendor-binaries,release}.yml
# and sycamore-extract's build.yml, so locally-built artifacts match CI output.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="${OUT_DIR:-$REPO_ROOT/build/local/out}"
SRC_CACHE="${SRC_CACHE:-$REPO_ROOT/build/local/src}"
mkdir -p "$OUT_DIR"

# resolve_src <name> <env-override> <git-url> <ref>
# With an explicit <ref>: always use a pristine cached clone under
# build/local/src, fetched and checked out at that ref (never mutates your
# own checkout). Without a ref: $2 if set -> sibling ../<name> -> cached
# clone at its current state (cloning default branch if absent).
resolve_src() {
    local name="$1" override="${2:-}" url="$3" ref="${4:-}"
    if [ -z "$ref" ]; then
        if [ -n "$override" ]; then
            echo "$override"
            return
        fi
        if [ -d "$REPO_ROOT/../$name/.git" ]; then
            echo "$(cd "$REPO_ROOT/../$name" && pwd)"
            return
        fi
    fi
    mkdir -p "$SRC_CACHE"
    if [ ! -d "$SRC_CACHE/$name/.git" ]; then
        git clone "$url" "$SRC_CACHE/$name" >&2
    fi
    if [ -n "$ref" ]; then
        git -C "$SRC_CACHE/$name" fetch --tags --force origin >&2
        git -C "$SRC_CACHE/$name" checkout --quiet "$ref" >&2
        # If the ref is a branch, make sure we're at its latest remote state
        git -C "$SRC_CACHE/$name" merge --ff-only "origin/$ref" >/dev/null 2>&1 || true
    fi
    echo "$SRC_CACHE/$name"
}

require_cross_toolchain() {
    command -v aarch64-linux-gnu-gcc >/dev/null || {
        echo "Missing cross compiler. Install with:" >&2
        echo "  sudo apt-get install -y gcc-aarch64-linux-gnu" >&2
        exit 1
    }
    command -v rustup >/dev/null || {
        echo "Missing rustup (https://rustup.rs)" >&2
        exit 1
    }
    rustup target list --installed | grep -q aarch64-unknown-linux-gnu \
        || rustup target add aarch64-unknown-linux-gnu
    command -v maturin >/dev/null || {
        echo "Missing maturin. Install with:  pip install 'maturin>=1.0,<2.0'" >&2
        exit 1
    }
}
