# Decision Record: Sycamore-Only Pipeline

**Date:** 2026-06-12  
**Session:** claude/determined-cori-sYqQV → sycamore-only  
**Session ID:** 016awkUtyTwCrGAJTFND3uKT  
**Session URL:** https://claude.ai/code/session_016awkUtyTwCrGAJTFND3uKT  
**Branch:** sycamore-only (forked from claude/determined-cori-sYqQV)  
**Repo:** mconsidine/diofinder  

---

## Context

The diofinder system previously supported two extraction backends at runtime:
- **olive** — `get_centroids_from_image_fast` / `solve_from_image_fast` (Rust, combined extract+solve)
- **sycamore** — `star_detect.detect_stars` with selectable gate mode (matched_filter or cedar)

A web UI toggle, `extract_backend` config key, `sycamore_gate_mode` config key, and
`set_extract_backend` maintenance command allowed runtime switching between these.

---

## Decision

**Lock the entire system to sycamore (matched_filter gate) for extraction and
olive-solve (tetra3-py) for solving. Remove all backend-switching machinery.**

### Rationale

- Sycamore with `gate_mode="matched_filter"` is the preferred extractor; the
  olive extraction path was a legacy fallback.
- The cedar gate mode and cedar-detect gRPC server are not used in this deployment.
- Removing the toggle simplifies the codebase, eliminates dead code paths, and
  removes a class of operational ambiguity (wrong backend selected at runtime).
- The olive-solve Rust wheel (tetra3-py) remains — it is the solver, not the extractor.

---

## Technical Assessments

### Coordinate convention (critical)
Sycamore `detect_stars` returns centroids as `(x=col, y=row)`.  
tetra3 / olive-solve `solve_from_centroids` expects `(row, col)`.  
**Required swap:** `[[s[1], s[0]] for s in raw]`

### dtype requirement
The PyO3/Rust binding for `solve_from_centroids` rejects `float32`.  
**Required:** `dtype=np.float64` when constructing the centroids array.

### sycamore is now a hard dependency
Previously optional (with olive fallback). `solver_proc.py` now raises
`RuntimeError` at import time if `star_detect` is not installed. All test
scripts call `sys.exit(1)` if the import fails.

---

## Actions Taken

### Core Python — diofinder package

| File | Change |
|------|--------|
| `diofinder/config.py` | Removed `extract_backend` and `sycamore_gate_mode` fields; updated `detect_sigma` and `cpu_solver` comments; removed `backend=` from `summary()` |
| `diofinder/diofinder_main.py` | Removed `extract_backend` and `sycamore_gate_mode` from `shared_cfg` dict init; updated module docstring |
| `diofinder/solver_proc.py` | Made sycamore required (hard fail); removed `_backend`/`_gate` variables and all conditional branching; hardcoded `gate_mode="matched_filter"`, `dtype=np.float64`, coordinate swap |
| `diofinder/comms_proc.py` | Removed `set_extract_backend` handler; changed `solver_backend` status field to hardcoded `"sycamore"` |

### Web UI

| File | Change |
|------|--------|
| `webui/app.py` | Removed `/backend/set` route; hardcoded `solver_backend="sycamore"`; removed `extract_backend`/`sycamore_gate_mode` from config sections; removed `runtime_backend` from config page render |
| `webui/templates/dashboard.html` | Removed extractor toggle div (Olive/Sycamore buttons); removed JS backend toggle logic |
| `webui/templates/config.html` | Removed `{% if runtime_backend %}` solver backend display block |
| `webui/templates/camera.html` | Updated `detect_sigma` hint text; updated solver timeout description |
| `webui/templates/update.html` | Updated wheel description to sycamore |

### Test scripts

| File | Change |
|------|--------|
| `tests/diag_detect.py` | Removed `--backend`, `--gate-mode`, `--compare-full`; removed `_extract_olive()`; single sycamore `_extract()` function |
| `tests/diag_solve.py` | Removed `--backend`, `--gate-mode`, `--skip-a`; removed Path A (`solve_from_image_fast`); renamed B→1 (blind), C→2 (hint) |
| `tests/solve_image.py` | Removed `--backend`, `--gate-mode`; always sycamore → `solve_from_centroids` |
| `tests/test_hint.py` | Removed `--backend`, `--gate-mode`; `_solve()` uses sycamore directly |
| `tests/bench_pipeline_combos.py` | Removed olive paths 1–3; retained paths 4→1 (blind) and 5→2 (hint); removed `--gate-mode` |
| `tests/bench_extractor_compare.py` | Repurposed as sycamore-only extraction+solve benchmark; removed `_olive_extract()` and comparison section |

### Documentation

| File | Change |
|------|--------|
| `CLAUDE.md` | Removed `extract_backend`/`sycamore_gate_mode` from `shared_cfg` table; replaced "Extractor backends" section with single "Extraction" paragraph |
| `README.md` | Removed dual-backend description, backend toggle from Web UI features, `extract_backend`/`sycamore_gate_mode` from config/shared_cfg tables, backend switch from maint CLI examples, olive extraction timing from performance table; updated all test script descriptions |
| `tests/README.md` | Updated `diag_solve`, `solve_image`, `bench_pipeline_combos`, `bench_extractor_compare` rows; removed all `--backend`/`--gate-mode` examples; updated intro |

### Build / CI

| File | Change |
|------|--------|
| `build/check-tree.sh` | Removed `proto/` from required dirs; removed `proto/cedar_detect.proto` and `systemd/cedar-detect.service` from required files; removed sections 7 (proto validity) and 9 (cedar-detect submodule check) |
| `scripts/install.sh` | Updated header comment; removed cedar-detect.service note |
| `.github/workflows/vendor-binaries.yml` | Updated branch refs `olive`→`sycamore-only`; removed `cedar-detect-server` cleanup step; updated commit message |
| `.github/workflows/release.yml` | Updated branch refs `olive`→`sycamore-only`; replaced CDS hip_main.dat wget with `mconsidine/astro_databases` mirror via curl+gunzip; updated workflow name, trigger branch, artifact names |

---

## Cedar References Removed

All references to `cedar-detect`, `cedar-solve`, `cedar_detect.proto`, the
cedar-detect gRPC server, and the cedar gate mode were removed from code,
scripts, workflows, and documentation. Verified with a repo-wide grep after
completion — zero remaining references.

---

## hip_main.dat Download Fix

**Problem:** `wget` from `cdsarc.cds.unistra.fr` returns exit code 4
(network failure) on GitHub Actions runners. The CDS Hipparcos archive is
unreliable from CI.

**Fix applied to this branch:**
```yaml
curl -fsSL \
  https://raw.githubusercontent.com/mconsidine/astro_databases/main/hip_main.dat.gz \
  | gunzip > "${TETRA3_DIR}/hip_main.dat"
```

**Same fix also applied to:** `olive` branch (direct GitHub API commit,
same session).

**Assumption:** `hip_main.dat.gz` is at the root of the `main` branch of
`mconsidine/astro_databases`. Adjust the URL if the branch or path differs.

---

## Recommendations

1. **Verify sycamore wheel is present** in `vendor/wheels/` before triggering
   the Release workflow. The pre-flight check will fail fast if absent, but
   confirming beforehand avoids a wasted run.

2. **Run `bash build/check-tree.sh`** from the repo root after any future
   file restructuring to catch missing required files before pushing.

3. **sigma default is 7.0** for sycamore matched_filter. The previous "9 for
   olive" annotation has been removed everywhere. If field tests show
   under/over-detection, adjust `detect_sigma` in `diofinder.conf` or via the
   Config page — no code change needed.

4. **`mconsidine/testrepo`** — two additional workflow files in that repo
   contain the same CDS hip_main.dat wget pattern and need the same
   curl+gunzip fix. Those files are outside the tool access scope for this
   session; apply the fix manually (pattern documented in session chat).

5. **FOV calibration** — if switching hardware or lenses, reset with:
   ```bash
   sudo sed -i 's/^fov_calibrated:.*/fov_calibrated: false/' /etc/diofinder/diofinder.conf
   sudo systemctl restart diofinder
   ```

---

## Commits on sycamore-only (this session)

| SHA | Summary |
|-----|---------|
| `8f875d6` | Lock pipeline to sycamore (matched_filter) + olive-solve; remove all backend switching |
| `8146bb3` | Remove all cedar-detect references from build scripts and workflow |
| `a22eb1e` | Fix hip_main.dat download: use mconsidine/astro_databases mirror; update branch refs |
