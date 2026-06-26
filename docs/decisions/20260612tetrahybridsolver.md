# 2026-06-12 — Tetra Hybrid Solver: Decisions, Assessments, Actions, Recommendations

**Session name:** assess-diofinder-improvements  
**Session ID:** `018v3BTHEGGxUVhCeyERTJD2`  
**Session URL:** https://claude.ai/code/session_018v3BTHEGGxUVhCeyERTJD2  
**Branch:** `combo`  
**Repo:** `mconsidine/eFinder_cli_new`

---

## Context

This session investigated why the `tetra` backend in the combo branch was
not solving frames, characterized the performance of both backends, and
implemented a series of fixes. All changes target the `combo` branch of
`mconsidine/eFinder_cli_new`.

---

## Assessment: Why `extract_centroids` was slow (250ms)

**Finding:** `tetra3rs.extract_centroids()` performs a full Rust-based image
processing pipeline (hot pixel removal, binning, thresholding, centroid
fitting) entirely in-process. On the Pi Zero 2W this takes ~250ms per frame.
cedar-detect performs the same work as a native C++ gRPC service (~13ms).

**Decision:** Adopt a **hybrid extraction approach** — use cedar-detect
(gRPC + SHM zero-copy) for centroid extraction in *both* backends, keeping
tetra3rs exclusively for the plate solve.

---

## Assessment: Coordinate format for `solve_from_centroids`

**Finding:** `tetra3rs.solve_from_centroids()` expects centroids in
**center-relative (x, y)** coordinates with (0,0) at the image center,
x+ right, y+ down — *not* absolute pixel coordinates.

cedar-detect returns absolute pixel coordinates: `centroid_position.x` =
column (0=left), `centroid_position.y` = row (0=top).

**Required conversion:**
```python
tetra_x = cedar.centroid_position.x - frame_width  / 2.0
tetra_y = cedar.centroid_position.y - frame_height / 2.0
```

**dtype requirement:** `np.float64` (not float32 — raises ValueError).

---

## Root Cause: tetra3rs returning "no solve" despite correct centroids

**Finding:** The `FovCalibrator` tightens `fov_max_error` from the configured
1.0° down to ~0.1° after cedar accumulates successful solves. The tetra3rs
database was generated at exactly **14.0° FOV** while the camera's actual
FOV is **13.497°**. With ±0.1° tolerance the search window is 13.4–13.6°,
missing all 14.0° database patterns. With ±1.0° the solve succeeds in ~495ms.

**Fix applied** (`diofinder/solver_proc.py`):
```python
# Before
fov_max_error_deg=calibrator.get_fov_max_error(),

# After
fov_max_error_deg=max(calibrator.get_fov_max_error(), cfg.fov_max_error_deg),
```

**Recommendation:** When regenerating the tetra3rs database, use the camera's
calibrated FOV (13.497°, not a round number) so the database FOV matches the
actual FOV and a tight calibrator window remains valid without the `max()`
workaround.

---

## Root Cause: Seeded solves still ~480ms despite having a quaternion hint

**Finding:** `hint_uncertainty_deg=5.0` was the default. A 5° uncertainty
cone is large enough that tetra3rs searches nearly the full pattern space,
providing almost no speedup over blind (~495ms blind vs ~483ms "seeded").

At 5fps the telescope moves far less than 0.1° between frames. With
`hint_uncertainty_deg=0.1` seeded solves drop to **~11ms**.

**Fix applied** (`diofinder/solver_proc.py`):
```python
hint_uncertainty_deg=0.1,
```

**Recommendation:** Make `hint_uncertainty_deg` a config parameter
(`tetra3rs_hint_uncertainty_deg`) so it can be tuned for mounts with faster
slew rates without requiring a code change.

---

## Assessment: db.solve() vs solve_from_centroids

**Finding:** The older `diofinder_cli_tetra3rs_mp` repo used `db.solve()` with
`ra_hint_deg`/`dec_hint_deg`/`search_radius_deg=5.0` and achieved ~6ms
seeded solves. This API **does not exist** in the current tetra3rs version —
only `solve_from_centroids` is available.

The ~6ms achieved previously was from a different tetra3rs API version, not
from a tighter hint radius. The current `solve_from_centroids` with
`hint_uncertainty_deg=0.1` achieves comparable performance (11ms).

---

## Assessment: solve_from_centroids argument names

**Finding:** The tetra3rs API uses `fov_estimate_deg` and `fov_max_error_deg`
(not `fov_estimate` / `fov_max_error`), and **requires** `image_width` and
`image_height` (or `image_shape`). Missing these causes TypeError or
ValueError with no solve result. The combo branch code was already correct
on these points.

---

## Performance Summary (static test image, Pi Zero 2W)

| Backend                              | Extract | Solve (blind) | Solve (seeded) | Total (seeded) |
|--------------------------------------|---------|---------------|----------------|----------------|
| Cedar (cedar-detect + tetra3 Python) | 13ms    | 26ms          | 26ms           | ~43ms          |
| Tetra hybrid (cedar-detect + tetra3rs) | 14ms  | ~495ms        | 11ms           | ~27ms          |

Tetra hybrid is **~37% faster than cedar** on seeded frames. The first-frame
blind solve takes ~495ms; all subsequent frames use the quaternion hint
and complete in ~27ms total.

---

## CI Build: tetra3rs wheel build time improvement

**Finding:** The `vendor-binaries.yml` workflow built tetra3rs wheels using
`ubuntu-latest` (x86_64) + QEMU emulation of aarch64. QEMU software
emulation was the sole cause of the ~3 hour build time.

`cedar-detect` avoided this by using a native cross-compiler
(`aarch64-linux-gnu-gcc`) rather than QEMU, completing in ~10 minutes.
The reason tetra3rs required QEMU was that Python extension wheels
(`.so` files) need to link against the target Python runtime, which
`cibuildwheel` solved via QEMU rather than cross-compilation.

**Fix applied** (`.github/workflows/vendor-binaries.yml`):

| Setting | Before | After |
|---|---|---|
| Runner | `ubuntu-latest` (x86_64) | `ubuntu-24.04-arm` (native aarch64) |
| QEMU step | present | removed |
| `CIBW_ARCHS_LINUX` | `aarch64` | `native` |
| Timeout | 210 minutes | 40 minutes |
| Expected build time | ~3 hours | ~15–20 minutes |

---

## Files Modified This Session

| File | Change |
|---|---|
| `diofinder/solver_proc.py` | Hybrid cedar extraction for tetra backend; `fov_max_error_deg` fix; `hint_uncertainty_deg` 5.0→0.1 |
| `.github/workflows/vendor-binaries.yml` | Native ARM64 runner for tetra3rs build |
| `tests/bench_tetra_hints.py` | New: hint_uncertainty_deg sweep benchmark |
| `tests/bench_cedar_vs_tetra.py` | New: side-by-side cedar vs tetra hybrid timing |
| `tests/README.md` | New: test script documentation and test image location notes |

---

## Commits This Session (combo branch)

| SHA | Message |
|---|---|
| `31a304f` | Fix tetra backend: use max(calibrator, config) fov_max_error_deg |
| `0ccb417` | Fix tetra hint_uncertainty_deg: 5.0 → 0.1 |
| `e5237ea` | tests: add solver benchmark scripts for cedar/tetra comparison |
| `9d5f872` | vendor-binaries: use native ARM64 runner for tetra3rs (drop QEMU) |

---

## Recommendations for Future Sessions

1. **Regenerate tetra3rs database at calibrated FOV** — use `fov_deg=13.497`
   rather than `14.0` so the calibrator's tight window remains valid for
   tetra3rs without the `max()` workaround.

2. **Make `hint_uncertainty_deg` configurable** — add to `diofinder.conf` as
   `tetra3rs_hint_uncertainty_deg: 0.1` so it can be adjusted for faster
   mounts without code changes.

3. **Test on live sky** — all testing was done with a static test image
   (NCP field, RA 93.3°, Dec 89.25°). Confirm seeded solve times on live
   sky where the field moves between frames.

4. **Consider `strict_hint=True` after convergence** — once the solver has
   several consecutive seeded solves, enabling strict mode would further
   reduce solve time and prevent false positives during high-speed slews.

5. **testrepo access** — the session-level MCP restriction prevented
   validating the ARM64 workflow change in an isolated test repo first.
   Consider adding `mconsidine/testrepo` to the allowed repos list in the
   Claude Code project configuration for future sessions.

6. **Verify ARM64 runner build** — trigger the `vendor-binaries.yml` workflow
   manually via workflow_dispatch on the combo branch to confirm the
   `ubuntu-24.04-arm` runner completes successfully and the resulting
   wheels are committed to `vendor/`.
