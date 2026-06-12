# CI/CD Pipeline: Design Decisions and Build Architecture

**Date:** 2026-06-12  
**Session:** amazing-ride-zwrob  
**Session ID:** session_01VPcZw7fvor24dqdJYnHfyP  
**Repository:** mconsidine/testrepo  
**Branch:** claude/amazing-ride-zwrob → merged to main

---

## Purpose

This document records the design decisions, architectural choices, and rationale established during the session that produced the `buildbinaries.yml` and `manual-build.yml` GitHub Actions workflows and the `scripts/generate_databases.py` utility.

The pipeline produces deployable artifacts for a Raspberry Pi Zero 2W electronic finder scope, targeting Debian Trixie / Python 3.13 / aarch64 (Cortex-A53).

---

## Security Constraint (Non-Negotiable)

**All source repositories must be from `mconsidine/*` forks only.**  
The following upstream origins are explicitly prohibited in any CI step:

- `github.com/smroid/*`
- `github.com/ssmichael1/*`
- `github.com/oakamil/*`

This constraint is enforced by using only `mconsidine/cedar-detect`, `mconsidine/sycamore-extract`, `mconsidine/cedar-solve`, `mconsidine/tetra3rs`, and `mconsidine/olive-solve` as checkout sources. Repository variables (`CEDARDETECT_REF`, `SYCAMORE_REF`, `CEDARSOLVE_REF`, `TETRA3RS_REF`, `OLIVE_SOLVE_REF`) allow branch/tag targeting without modifying the workflow YAML.

---

## Pipeline Architecture (buildbinaries.yml)

### Stage 0 — Freshness Check (`check-freshness`)

**Decision:** Use `git ls-remote` SHA comparison against `vendor/.shas/` text files to skip unchanged packages.

**Rationale:** Avoids committing large binaries to git. Only tiny SHA/hash text files live in the repository; the actual binaries live in the `databases-latest` GitHub Release.

**Key details:**
- Each package has a corresponding `vendor/.shas/<key>` file storing the last-built source SHA.
- Databases use `sha256sum` of `scripts/generate_databases.py` (not a remote SHA), via `need_local_hash()`.
- When `FORCE=true` (workflow_dispatch input), all freshness checks are bypassed.
- If `git ls-remote` fails to resolve a SHA (network error, commit SHA used as ref), the package is rebuilt rather than silently skipped.

### Stage 1 — aarch64 Builds

**Target:** `aarch64-unknown-linux-gnu`, Cortex-A53, Debian Trixie, Python 3.13.

**RUSTFLAGS:** `-C target-cpu=cortex-a53 -C codegen-units=1`  
**LTO:** fat  
**Panic:** abort  

**Decision: cp313 only for tetra3rs aarch64.**  
Rationale: `tetra3rs` uses `setuptools-rust` which prevents native maturin cross-compilation; QEMU emulation is required for aarch64. Building three Python versions (cp311/cp312/cp313) via QEMU took ~3 hours. Cutting to cp313 only reduces this to ~1 hour while matching the Pi OS Bookworm / Python 3.13 deployment target.

**Decision: matched-filter sycamore wheel gets `matched-filter-` prefix in the release.**  
Rationale: Both the main and matched-filter branches of `sycamore-extract` produce a wheel named `star_detect-*.whl`. The prefix allows both to coexist in the `databases-latest` release as separate assets. The prefix is stripped by `install.sh` before `pip install`.

### Stage 2 — x86_64 Builds

Native builds on `ubuntu-latest` CI runners. Used only for the benchmark phase; not deployed to the Pi. Cedar-solve (pure Python) reuses its Stage 1 artifact.

### Stage 3 — Database Generation

**Files produced:** `cedar_solve_13deg.npz`, `tetra3rs_13deg.bin`

**FOV parameters:** min=10.5°, max=14° (camera sensor min/max)  
**Magnitude limit:** 8.0  
**Epoch:** 2026.0  

**Decision: `hip_main.dat` from `mconsidine/astro_databases`.**  
Source: `https://raw.githubusercontent.com/mconsidine/astro_databases/main/hip_main.dat.gz`  
The file is served gzip-compressed and decompressed on the fly (`curl … | gunzip`). This replaced unreliable CDS/VizieR mirrors that were causing `curl: (7) Failed to connect` errors in CI. The `actions/cache` step with key `hip-main-dat-immutable-v1` caches the decompressed file; the key is fixed because Hipparcos is a static catalog (bump the suffix only to invalidate a corrupt cache entry).

**Decision: Install cedar-solve with `--no-deps`.**  
Rationale: cedar-solve constraints `Pillow<9`, which has no Python 3.13 wheel. Only `tetra3.Tetra3.generate_database()` is needed for database generation; none of cedar-solve's optional imaging dependencies are required.

### Stage 4 — Benchmark

Runs four phases: sycamore-main × cedar-solve, sycamore-main × olive-solve, sycamore-matched × cedar-solve, sycamore-matched × olive-solve.

**Decision: Preserve cedar-detect pb2 stubs before namespace switch.**  
Both cedar-solve and olive-solve install into the `tetra3` Python namespace. When switching from cedar-solve to olive-solve mid-job, the `cedar_detect_pb2*.py` files installed by cedar-solve are overwritten. They are preserved to `lib/cedar_detect/` before the switch and restored afterward.

### Stage 5a — Release (`release-databases`)

**Decision: `databases-latest` pre-release as the persistent artifact store.**  
Rationale: GitHub Actions artifacts expire after 1–30 days. The release provides permanent storage without requiring Git LFS (which has bandwidth costs). `gh release delete … && gh release create` on every build ensures the release always reflects the latest successful run.

### Stage 5b — SHA Recording (`commit-vendor`)

**Decision: `if: ${{ !cancelled() }}`** (unconditional except cancellation).  
Rationale: Per-job conditional SHA recording happens inside the step script (checking `needs.<job>.result == 'success'`), so the outer `if` only needs to prevent recording after user cancellation. This ensures every successfully-built package gets its SHA recorded regardless of other failures.

**Decision: `[skip ci]` in commit message.**  
Prevents the SHA commit from re-triggering the full build pipeline.

---

## Mutually-Exclusive Job Pairs

Every package has a `<pkg>` build job and a `<pkg>-reuse` job. Exactly one runs per pair (build when source changed; reuse when unchanged). This creates a silent-skip propagation hazard: GitHub Actions skips downstream jobs when any dependency is in `skipped` state.

**Fix applied** to `build-databases`, `benchmark`, `release-databases`, and `commit-vendor`:
```yaml
if: ${{ !cancelled() && !contains(needs.*.result, 'failure') }}
```
This allows skipped dependencies while still blocking on actual failures.

---

## Database Parameter History

| Parameter | Original | Current |
|-----------|----------|---------|
| FOV min   | 12.5°    | 10.5°   |
| FOV max   | 14.5°    | 14.0°   |
| Epoch     | 2025.0   | 2026.0  |
| hip source | CDS/VizieR | mconsidine/astro_databases |

The file names (`cedar_solve_13deg.npz`, `tetra3rs_13deg.bin`) were not renamed to avoid breaking `install.sh` and downstream consumers; the "13deg" suffix is now a legacy label.

---

## Files Created / Modified

| File | Action | Description |
|------|--------|-------------|
| `.github/workflows/buildbinaries.yml` | Modified | Main CI pipeline (all stages) |
| `.github/workflows/manual-build.yml` | Created | Manually-triggered selective rebuild |
| `scripts/generate_databases.py` | Modified | FOV, epoch, hip source, gzip download |
| `vendor/.shas/cedar-detect` | Created | SHA cache seed |
| `vendor/.shas/sycamore` | Created | SHA cache seed |
| `vendor/.shas/sycamore-matched` | Created | SHA cache seed |
| `vendor/.shas/cedar-solve` | Created | SHA cache seed |
| `vendor/.shas/tetra3rs` | Created | SHA cache seed |
| `vendor/.shas/olive-solve` | Created | SHA cache seed |

---

## manual-build.yml — Design Decisions

**Trigger:** `workflow_dispatch` with five inputs:

| Input | Options | Notes |
|-------|---------|-------|
| `component` | cedar-detect, sycamore-main, sycamore-matched, cedar-solve, tetra3rs, olive-solve, db-npz, db-bin, db-all | |
| `architecture` | aarch64, armv7, x86_64, x86, all | Ignored for cedar-solve and db-* |
| `python-version` | cp313, cp312, cp311, many | Ignored for cedar-detect and cedar-solve |
| `publish` | boolean | `gh release upload --clobber` to `databases-latest` |
| `record-sha` | boolean | Commits updated `vendor/.shas/` entry |

**Decision: dynamic matrix via `setup` job.**  
A `setup` job resolves `inputs.architecture` to a JSON array (`["aarch64","x86_64"]` etc.) which downstream jobs consume via `fromJSON(needs.setup.outputs.archs)`. Matrix `include` entries inject arch-specific properties (rust_target, linker, rustflags, etc.).

**Decision: databases job downloads prerequisite wheels from `databases-latest`.**  
Rather than building cedar-solve and tetra3rs-x86 as inline prerequisites (complex dependency graph), the databases job downloads the existing wheels from the release. This assumes `databases-latest` already contains current wheel builds from a prior `buildbinaries.yml` run.

**Architecture cross-compilation targets:**

| arch input | Rust target | Cross toolchain | RUSTFLAGS target-cpu |
|---|---|---|---|
| aarch64 | aarch64-unknown-linux-gnu | gcc-aarch64-linux-gnu | cortex-a53 |
| armv7 | armv7-unknown-linux-gnueabihf | gcc-arm-linux-gnueabihf | cortex-a7 |
| x86_64 | (native) | — | — |
| x86 | i686-unknown-linux-gnu | gcc-multilib | — |

**tetra3rs uses cibuildwheel + QEMU** for arm targets (setuptools-rust prevents maturin cross-compile). aarch64/armv7 timeout is 240 minutes; other jobs 60 minutes.

---

## Recommendations

1. **Rename database files** in a future maintenance cycle to reflect the actual FOV range (e.g., `cedar_solve_10_14deg.npz`). Requires coordinating updates to `install.sh`, `buildbinaries.yml`, `manual-build.yml`, and `benchmark.py`.

2. **Add armv7 to the regular `buildbinaries.yml` pipeline** if the finder is ever deployed on a Pi 2 or Pi 3. The `manual-build.yml` already supports it.

3. **Bump `hip-main-dat-immutable-v1` cache key** if the `astro_databases` file is ever corrected or replaced.

4. **Consider adding cp311/cp312 back to the tetra3rs aarch64 build** (via a separate optional workflow) if users on older Python versions emerge. For now, the Pi OS Bookworm target (Python 3.13) makes cp313-only correct.

5. **Repository variables** (`CEDARDETECT_REF`, `SYCAMORE_REF`, etc.) allow targeting specific branches/tags without modifying workflow YAML. This is the preferred mechanism for pinning to release tags.
