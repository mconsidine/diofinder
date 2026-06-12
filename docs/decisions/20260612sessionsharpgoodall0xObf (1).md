# Session Decision Record — diofinder

**Date:** 2026-06-12  
**Session name:** sharp-goodall-0xObf  
**Session ID:** 28836a99-ec8b-4861-bfa7-2c4620cc8664  
**Branch:** `claude/sharp-goodall-0xObf` (development); pushed to `olive`

---

## Context

This session continued from a prior handoff (`bdc634d3-bgworkhandoff.md`).
The diofinder `olive` branch needed to:
1. Drop `gate_mode="matched_filter"` from all star-detection call sites
   (the 0.9.0 sycamore wheel removed that parameter).
2. Expose `detect_bg_mode` and `detect_tophat_radius` as live-mutable
   maintenance socket controls.
3. Add operator tooling for background A/B testing.
4. Vendor the new `star_detect-0.9.0` wheel.
5. Merge and fix the `claude/olive-bg-solve-rate` branch discovered mid-session.

---

## Assessment of Discovered Branch: `claude/olive-bg-solve-rate`

**Finding:** One commit (`441cd8a`) ahead of `olive` at session start.

**Contents:**
- `SOLVER_OP_SOLVE_CENTROIDS` op in `worker_cmds.py`
- `solve_centroids` maint command in `comms_proc.py` (routes centroids to live solver)
- `_handle_solver_cmd` handler in `solver_proc.py` (uses already-loaded DB — memory-safe)
- `tests/diag_background.py`: `--solve` flag reporting solved/Nmatch per bg mode
- `scripts/efinder-update`: `--ref BRANCH` support

**Bug found:** `extract()` in `diag_background.py` still passed
`gate_mode='matched_filter'` — would raise `TypeError` with the 0.9.0 wheel.

**Decision:** Merge all five files to `olive` with the `gate_mode` bug corrected.

**Rationale for merge:** The `--solve` column is the metric that matters for
`tophat_radius` tuning — star counts alone don't show whether a mode actually
plate-solves. The branch was evaluated, found correct aside from the one bug,
and merged rather than discarded.

---

## Decisions

### 1. Remove gate_mode from both bg_cache.py call sites immediately

**Decision:** Remove `gate_mode="matched_filter"` from `efinder/bg_cache.py`
before vendoring the 0.9.0 wheel.

**Rationale:** The 0.9.0 API does not accept `gate_mode`. Leaving it in place
would cause `TypeError` on every frame the moment the new wheel is installed —
a silent service-breaking regression. Removing it is safe because matched filter
is now the only gate and is hardcoded in the Rust extension.

### 2. Live-mutable bg controls via solver_params_get/set

**Decision:** Extend `solver_params_get` to return `detect_bg_mode` and
`detect_tophat_radius`, and extend `solver_params_set` to accept and validate them
(writing to `shared_cfg`, optionally persisting via `config.save_keys`).

**Rationale:** Allows on-device A/B tuning (`efinder-bg-setup set top_hat --radius 12`)
without a service restart. The maint socket is the established IPC channel for
live config mutations; no new channel was needed.

**Validation ranges:** `detect_bg_mode` ∈ {`row_percentile`, `line_median`, `top_hat`};
`detect_tophat_radius` ∈ [1, 100].

### 3. solve_centroids: reuse resident solver DB (no second copy)

**Decision:** `SOLVER_OP_SOLVE_CENTROIDS` in `solver_proc._handle_solver_cmd`
calls `solver_t3.solve_from_centroids` directly on the already-loaded tetra3
instance — it does not open a second database.

**Rationale:** The Pi Zero 2W has 512 MB RAM. A second copy of the star database
would exceed available memory. The trade-off is that `solve_centroids` briefly
competes with live frame-solving while the diagnostic is running — acceptable for
an operator tool used during characterisation, not in production.

### 4. Wheel vendored via GitHub Actions (no manual cross-compilation)

**Decision:** Triggered the `Vendor Sycamore` workflow in diofinder after the
sycamore-extract `v0.9.0` Release appeared, which downloaded the cp313 aarch64
wheel and committed it to `olive` via the workflow's automated commit.

**Rationale:** Keeps the wheel provenance traceable (workflow run → Release tag →
wheel SHA) and eliminates the need for a cross-compilation toolchain on the dev box.

---

## Actions Taken

| File | Change |
|------|--------|
| `efinder/bg_cache.py` | Removed `gate_mode="matched_filter"` from cached-path and per-frame-path `kw` dicts. |
| `efinder/comms_proc.py` | Extended `solver_params_get/set` for `detect_bg_mode` and `detect_tophat_radius`. Added `solve_centroids` maint command. Imported `SOLVER_OP_SOLVE_CENTROIDS`. |
| `efinder/worker_cmds.py` | Added `SOLVER_OP_SOLVE_CENTROIDS = "solve_centroids"` constant. |
| `efinder/solver_proc.py` | Added `solve_centroids` branch in `_handle_solver_cmd`; receives `solver_t3`, `cfg`, `shared_cfg`. |
| `tests/diag_background.py` | Added `--solve` flag (calls `solve_centroids` maint command per mode). Fixed `gate_mode='matched_filter'` bug from `olive-bg-solve-rate` branch. |
| `scripts/efinder-bg-setup` | New CLI: show/set `detect_bg_mode` and `detect_tophat_radius` via maint socket. |
| `scripts/efinder-bg-test` | New A/B harness: detection counts, timing, centroid agreement across bg modes on saved or live frames. Supports `--inject-gradient`. |
| `scripts/efinder-update` | Added `--ref BRANCH` to fetch + checkout + fast-forward a branch instead of only release tags. |
| `webui/templates/bgtest.html` | New operator page: mode selector, tophat-radius field (JS show/hide on `top_hat`), persist checkbox. |
| `webui/app.py` | Added `/bgtest` (GET) and `/bgtest/set` (POST) routes. |
| `webui/templates/base.html` | Added "Background" nav link → `bgtest_page`. |
| `etc/efinder.conf.default` | Added `detect_bg_mode`, `detect_tophat_radius`, and all `bg_cache_*` keys with comments. |
| `CLAUDE.md` | Fixed stale `gate_mode="matched_filter"` reference in Extraction section. Updated shared_cfg table, maint command list, key file locations. |
| `vendor/wheels/` | Replaced `star_detect-0.8.0-…whl` with `star_detect-0.9.0-cp313-cp313-manylinux_2_17_aarch64.manylinux2014_aarch64.whl`. |

**Final `olive` HEAD at session close:** `fcfff5e`

---

## Recommendations / Open Items

### Immediate (on next Pi session)

1. **Update the device:**
   ```bash
   sudo efinder-update --ref olive
   ```
   This pulls the latest `olive` commits and installs the 0.9.0 wheel.

2. **Run the background A/B with solve-rate:**
   ```bash
   sudo python3 tests/diag_background.py --solve
   sudo python3 tests/diag_background.py --inject-gradient 40 --solve
   ```
   Use the `solved` / `Nmatch` columns to pick `detect_tophat_radius`. If
   `top_hat` at radius 12 loses stars vs `row_percentile` on a clean frame,
   raise the radius. If it gains stars and the solve rate improves under a
   gradient, enable it in config.

3. **Set and persist if top_hat is beneficial:**
   ```bash
   efinder-bg-setup set top_hat --radius 14 --persist
   sudo systemctl restart efinder
   ```

### Deferred / Future

- **Stale branches to delete:** `claude/vigilant-wright-d9ndN`, `hybrid`,
  `claude/olive-bg-solve-rate` (now merged) on the diofinder remote.
- **Future bg modes** (`column_percentile`, `block_percentile`, `uniform_mean`,
  `global_rms` noise mode): these are documented in `CLAUDE.md` as requiring
  sycamore >= 0.10.0/0.11.0. Do not enable in `solver_params_set` or `efinder-bg-setup`
  until a wheel that exports them is vendored.
- **tests/README.md:** `diag_background.py --solve` is not yet documented there
  (the in-script docstring is comprehensive, but the README catalogue predates
  the `--solve` flag).
