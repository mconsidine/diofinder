# Build Configuration Decision Record

**Date:** 2026-06-12  
**Session:** amazing-ride-zwrob  
**Session ID:** session_01VPcZw7fvor24dqdJYnHfyP  
**Repository:** mconsidine/cedar-detect  
**Role in pipeline:** Source repository — binary built by `mconsidine/testrepo` CI

---

## Role

`cedar-detect` provides the `cedar-detect-server` gRPC binary used as the centroid extractor in the Pi Zero 2W electronic finder pipeline. It is not a Python wheel; it is a single statically-linked native binary.

The binary is built by `buildbinaries.yml` in `mconsidine/testrepo` and published to the `databases-latest` GitHub Release.

---

## Build Configuration Decisions

### Primary Target (aarch64)

| Property | Value |
|---|---|
| Rust target | `aarch64-unknown-linux-gnu` |
| Cross-compiler | `gcc-aarch64-linux-gnu` |
| RUSTFLAGS | `-C target-cpu=cortex-a53 -C codegen-units=1` |
| LTO | fat |
| Panic strategy | abort |
| Strip | `aarch64-linux-gnu-strip --strip-all` |

Rationale for cortex-a53: the Pi Zero 2W (RP3A0-AU) is a Cortex-A53 quad-core; the binary is not intended for any other Pi variant in the primary deployment scenario.

### Other Targets (manual-build.yml only)

| arch | Rust target | Toolchain |
|---|---|---|
| armv7 | armv7-unknown-linux-gnueabihf | gcc-arm-linux-gnueabihf, target-cpu=cortex-a7 |
| x86_64 | (native) | system gcc |
| x86 | i686-unknown-linux-gnu | gcc-multilib |

These are available via the manual trigger but not built in the regular pipeline.

### Build Dependencies

`protobuf-compiler` must be installed before building (required for gRPC stub generation). This is installed via `apt-get` inside the CI runner.

---

## Release Asset

The aarch64 binary is published to `databases-latest` as `cedar-detect-server` (no arch suffix). Binaries for other architectures, if built via `manual-build.yml`, receive a `-<arch>` suffix (e.g., `cedar-detect-server-x86_64`).

---

## SHA Caching

The testrepo pipeline tracks the HEAD SHA of the branch/tag specified by the `CEDARDETECT_REF` repository variable (default: `main`) in `vendor/.shas/cedar-detect`. If the SHA is unchanged from the previous build, the existing release binary is reused without rebuilding.

---

## Recommendations

- Keep the `CEDARDETECT_REF` variable set to a pinned release tag in production rather than `main` to get reproducible builds.
- If deploying to Pi 2/3 (Cortex-A7 / armv7), use `manual-build.yml` with `architecture=armv7`.
