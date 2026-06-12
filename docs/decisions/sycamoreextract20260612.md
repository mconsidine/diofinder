# Build Configuration Decision Record

**Date:** 2026-06-12  
**Session:** amazing-ride-zwrob  
**Session ID:** session_01VPcZw7fvor24dqdJYnHfyP  
**Repository:** mconsidine/sycamore-extract  
**Role in pipeline:** Source repository — Python wheel built by `mconsidine/testrepo` CI

---

## Role

`sycamore-extract` provides the `star_detect` PyO3 extension wheel — the centroid extractor for 8-bit grayscale frames. Two variants are built from different branches:

| Variant | Branch | Release asset name |
|---|---|---|
| main (cedar gate) | `main` (or `SYCAMORE_REF`) | `star_detect-*aarch64*.whl` |
| matched-filter | `matched-filter-only` | `matched-filter-star_detect-*aarch64*.whl` |

The `matched-filter-` prefix is applied at publish time so both wheels can coexist in the `databases-latest` release. `install.sh` strips the prefix before `pip install`.

---

## Build Configuration Decisions

### Primary Target (aarch64)

| Property | Value |
|---|---|
| Build tool | maturin |
| Rust target | `aarch64-unknown-linux-gnu` |
| Cross-compiler | `gcc-aarch64-linux-gnu` |
| RUSTFLAGS | `-C target-cpu=cortex-a53 -C codegen-units=1` |
| LTO | fat |
| Panic strategy | abort |
| Python version | 3.13 (regular pipeline); configurable in manual-build.yml |

### Cross-Compilation Approach

maturin supports native cross-compilation (`--target aarch64-unknown-linux-gnu -i python3.13`). The host Python (3.13 on the CI runner) drives the build; no QEMU is required. This is significantly faster than cibuildwheel + QEMU.

### Other Targets (manual-build.yml)

| arch | Rust target | Toolchain |
|---|---|---|
| armv7 | armv7-unknown-linux-gnueabihf | gcc-arm-linux-gnueabihf, cortex-a7 |
| x86_64 | (native) | — |
| x86 | i686-unknown-linux-gnu | gcc-multilib |

Python versions selectable: cp311, cp312, cp313, or all three (`many`). maturin is invoked with multiple `-i` flags for multi-version builds.

---

## x86_64 Build (for CI benchmark)

A native x86_64 wheel is also built in Stage 2 of the regular pipeline, used only for the in-CI benchmark (not deployed to the Pi).

---

## SHA Caching

Two separate SHA cache entries:
- `vendor/.shas/sycamore` — tracks `SYCAMORE_REF` branch HEAD
- `vendor/.shas/sycamore-matched` — tracks `matched-filter-only` branch HEAD

Each has its own build/reuse job pair. The matched-filter variant is always tracked against the `matched-filter-only` branch, regardless of `SYCAMORE_REF`.

---

## Empirical Note (from project CLAUDE.md)

At the same nominal sigma, `gate_mode="matched_filter"` detects roughly the same star count as `gate_mode="cedar"` at sigma=9-10. The matched-filter gate is more conservative on real frames due to correlated noise. Neither is definitively better for the finder — the pipeline maintains both to allow empirical comparison via the benchmark phase.

---

## Recommendations

- Pin `SYCAMORE_REF` to a release tag for production builds.
- The `many` python-version option in `manual-build.yml` produces wheels for cp311 + cp312 + cp313 in one maturin invocation; use this if wheels for older Python versions are needed.
