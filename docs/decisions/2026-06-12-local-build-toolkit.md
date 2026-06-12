# 2026-06-12 — Local (no-CI) build toolkit and workflow linting

**Session:** funny-noether (`claude/funny-noether-lro1ig`)
**Session ID:** `session_01216HrQG6gvzvjiiAqZQUux` — https://claude.ai/code/session_01216HrQG6gvzvjiiAqZQUux
**Continues:** `session_018PPLNhiAv325425icnyf8g`
**Commits:** `ed2c064` (toolkit), `f7824e9` (check-workflows) on `claude/funny-noether-lro1ig`.

## Context / problem

Goal: recompile + re-vendor sycamore-extract and olive-solve wheels and build
the solver database locally, without spending GitHub Actions minutes. First
attempts used `act`; wheels built but never appeared on the host.

## Assessments

- **Root cause of the act problem:** by default act *copies* the repo into a
  container-private volume — `$GITHUB_WORKSPACE` and everything written under
  it are destroyed with the container. In-job verification (`ls`, `find`,
  `readlink -f`) shows container paths that vanish a second later. Remedies,
  in preference order: bind-mount a host dir via
  `--container-options "-v $HOME/build-cache:/build-cache"`; act's
  `--artifact-server-path` (makes upload-artifact@v4 work locally); `--bind`
  (writes into the real checkout; debugging only).
- Two concrete act-step bugs were diagnosed along the way: `mkdir -p
  build-cache/olive` (relative, container) vs `cp ... /build-cache/olive/`
  (absolute, host mount) → "Not a directory"; and relative-path verification
  lines after a correct absolute copy → step fails under `set -e` *after* the
  wheel was already saved.
- **Bigger assessment:** these CI jobs are plain shell; act adds ~10× overhead
  (fresh apt + rustup + `cargo install maturin` per run, no cargo cache) and
  an ephemeral-filesystem trap. Plain scripts are the right tool; act is only
  for debugging workflow YAML itself.

## Decisions

1. **`build/local/` toolkit** of shell equivalents that mirror the CI jobs'
   flags exactly:
   - `build-sycamore-wheel.sh [ref]` ≙ sycamore-extract `build.yml`
   - `build-olive-wheel.sh [ref]` ≙ `vendor-binaries.yml` build job
     (`SOLVER_ONLY=1` → `--no-default-features` on refs with olive-solve's
     new extractor feature gate)
   - `vendor-wheels.sh [--commit]` ≙ the stale-clear/copy/commit steps of both
     vendor workflows; stages by default, commits `[skip ci]` with `--commit`,
     **never pushes**
   - `build-database.sh` ≙ the database step of `release.yml` (esa/tetra3 in a
     cached venv, hip_main.dat from the mconsidine/astro_databases mirror,
     same `DB_MAX_FOV`/`DB_MIN_FOV` knobs, existing
     `build/generate_database.py`)
2. **Source-resolution semantics:** an explicit ref argument always builds
   from a pristine cached clone (`build/local/src/`, fetched + checked out);
   no ref builds the user's sibling checkout / `$SYCAMORE_SRC` / `$OLIVE_SRC`
   as-is. A build script never mutates a user checkout.
3. **Wheel tag `linux_aarch64`** (`maturin --compatibility linux`) for local
   builds instead of claiming manylinux from a plain cross-build. Pi pip
   accepts it; `install.sh` and the release preflight glob `*aarch64*.whl`,
   so CI (manylinux2014/2_35) and local wheels interoperate. Retires the old
   "rename manylinux_2_34 → linux_aarch64" workaround.
4. **`check-workflows.sh`** — actionlint 1.7.12 (auto-downloaded to
   `build/local/bin/`, gitignored) + shellcheck over `.github/workflows` of
   this repo and any sibling paths, as the zero-credit YAML check. Anything
   after `--` passes through to actionlint.

## Actions / verification (in-session)

- star_detect 0.11.1 (cp313) built from tag in 19 s; tetra3-py built from
  `olive-solve-noext` in 53 s (full 808 K; `SOLVER_ONLY=1` 704 K); both `.so`
  files verified aarch64 ELF.
- `vendor-wheels.sh` exercised against the live `vendor/wheels/` (correct
  stale-clearing, staging) and then restored — no locally-built wheels were
  left vendored.
- `check-workflows.sh` run across diofinder + sycamore-extract: clean except
  info-level shellcheck style notes (SC2012/SC2086/SC2015).
- `build-database.sh` not executed end-to-end (generation takes tens of
  minutes); it drives the identical CI-proven python script.

## Recommendations

- Merge `claude/funny-noether-lro1ig` → `olive`.
- Typical zero-credit loop:
  `build/local/build-sycamore-wheel.sh vX.Y.Z && SOLVER_ONLY=1
  build/local/build-olive-wheel.sh <ref> && build/local/vendor-wheels.sh
  --commit && git push origin olive`.
- In any act save-step, guard the mount:
  `mountpoint -q /build-cache || exit 1` — otherwise a missing `-v` flag
  silently writes into the doomed container filesystem.
- The on-device verification list from the 2026-06-10 handoff (bin=2 recall,
  3-core solver speedup, auto-exposure convergence, etc.) is still pending.
