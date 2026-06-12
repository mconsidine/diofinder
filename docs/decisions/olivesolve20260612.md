# Build Configuration Decision Record

**Date:** 2026-06-12  
**Session:** amazing-ride-zwrob  
**Session ID:** session_01VPcZw7fvor24dqdJYnHfyP  
**Repository:** mconsidine/olive-solve  
**Role in pipeline:** Source repository — Python wheel built by `mconsidine/testrepo` CI

---

## Role

`olive-solve` provides the `tetra3` Python wheel (package name: `tetra3-py`) — a Rust-backed plate solver alternative to cedar-solve. It installs into the same `tetra3` namespace as cedar-solve.

In the benchmark, it is tested as the second solver in each phase (sycamore-main × olive-solve, sycamore-matched × olive-solve). On the Pi, it is available in `databases-latest` but is not installed by default by `install.sh` (cedar-solve is the default).

---

## Build Configuration Decisions

### Build Tool: maturin

olive-solve uses maturin as its build backend (unlike tetra3rs which uses setuptools-rust). This means native cross-compilation is possible without QEMU.

### Primary Target (aarch64)

| Property | Value |
|---|---|
| Build tool | maturin |
| Rust target | `aarch64-unknown-linux-gnu` |
| Cross-compiler | `gcc-aarch64-linux-gnu` |
| RUSTFLAGS | `-C target-cpu=cortex-a53 -C codegen-units=1` |
| LTO | fat |
| Panic strategy | abort |
| Working directory | `tetra3-py/` (subdirectory of the olive-solve checkout) |

### Other Targets (manual-build.yml)

| arch | Rust target | Toolchain |
|---|---|---|
| armv7 | armv7-unknown-linux-gnueabihf | gcc-arm-linux-gnueabihf, cortex-a7 |
| x86_64 | (native) | — |
| x86 | i686-unknown-linux-gnu | gcc-multilib |

Python versions selectable: cp311, cp312, cp313, or all three (`many`).

### Working Directory

The maturin build runs from `tetra3-py/` inside the olive-solve checkout (not the repo root). This is reflected in the `working-directory: tetra3-py` step in both `buildbinaries.yml` and `manual-build.yml`, and in wheel glob patterns (`tetra3-py/dist/tetra3-*.whl`).

---

## Namespace Conflict with cedar-solve

Both cedar-solve and olive-solve install as the `tetra3` Python package. They cannot coexist in the same Python environment. The CI benchmark handles this by pip-uninstalling the previous solver before installing the next one (phases 1→2 and 3→4 of the benchmark).

**Important:** the `cedar_detect_pb2*.py` stubs installed by cedar-solve are overwritten when olive-solve is installed. These are preserved to `lib/cedar_detect/` before the switch.

---

## Release Assets

- `tetra3-*cp313*aarch64*.whl` — cached in `databases-latest`, not installed by default on Pi
- x86_64 wheels are consumed as CI artifacts only (not published to the release)

---

## SHA Caching

Tracked in `vendor/.shas/olive-solve` against `OLIVE_SOLVE_REF` (default: `main`).

---

## Recommendations

- If olive-solve is ever made the default Pi solver (replacing cedar-solve), update `install.sh` accordingly and consider whether the pb2-stubs workaround in the benchmark is still needed.
- Pin `OLIVE_SOLVE_REF` to a release tag for production builds.
- The `tetra3-py/` working directory convention in the repo should be documented in olive-solve's own README/CLAUDE.md to prevent confusion when working in that repo directly.
