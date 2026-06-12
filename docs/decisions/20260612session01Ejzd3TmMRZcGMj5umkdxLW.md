# Session Decision Record — sycamore-extract

**Date**: 2026-06-12  
**Session**: `session_01Ejzd3TmMRZcGMj5umkdxLW`  
**Version range covered**: 0.9.0 → 0.10.0 → 0.11.0

---

## Context

Session was initiated to extend the background subtraction options available
to the diofinder application. The `top_hat` mode (~100 ms) was too slow by
~10× for the Pi Zero 2W target. The goal was to add faster spatial-preprocessing
alternatives and to match the exact tetra3/olive-solve default pipeline.

---

## Changes Made

### 1. Parallelised `transpose()` (v0.10.0)

**Decision**: Parallelise the `transpose()` helper used by `morph_v`'s two
transpose calls around the vertical morphology pass.

**Rationale**: `morph_v` called `transpose()` twice, each pass being serial
O(N). On 960×760 frames this was a bottleneck for `top_hat`. Parallelising
with `par_chunks_mut(src_h)` (each output column written independently) is
safe because output columns don't overlap.

**Implementation**: `par_chunks_mut(src_h)` in `src/lib.rs`, each chunk
covers one column of the output matrix.

---

### 2. `column_percentile` and `row_column_percentile` modes (v0.10.0)

**Decision**: Add per-column percentile floor (`column_percentile`) and a
separable two-pass version (`row_column_percentile`: row pass then column
pass) as new `bg_mode` values.

**Rationale**: Removes column-wise vignetting and sky gradients that
`row_percentile` leaves behind. The separable approach is O(H + W) per-pixel
equivalent rather than O(H×W) for a full 2-D filter.

**Capability flag**: These modes are available in sycamore >= 0.10.0. The
diofinder `bg_cache.py` enforces the per-frame-only path for these modes
(they cannot compose with the temporal cache).

---

### 3. `block_percentile` bilinear tile median (v0.10.0)

**Decision**: Add `block_percentile` mode: per-tile median with bilinear
interpolation between tile centres, controlled by `bg_block_size` parameter
(default 0 → sycamore uses 32).

**Rationale**: Removes 2-D spatial background gradients cheaply. Same
algorithm as tetra3rs's `estimate_local_background`. Per-tile percentile
is robust to stars contaminating individual tiles. Bilinear interpolation
avoids block-boundary artefacts.

**API**: `detect_stars(..., bg_mode="block_percentile", bg_block_size=32)`

---

### 4. `uniform_mean` bg_mode + `global_rms` noise_mode (v0.11.0)

**Decision**: Add `bg_mode="uniform_mean"` (25×25 sliding-window mean via
summed-area table) and `noise_mode="global_rms"` (sqrt(mean(pixel²))).
Together these replicate the exact tetra3/olive-solve default extraction
pipeline (`BgSubMode::LocalMean` + `SigmaMode::GlobalRootSquare`).

**Rationale**: User wanted a mode that is directly comparable with the
olive-solve `FastExtractor` for A/B benchmarking. The SAT approach is O(N)
serial build + parallelised subtraction — fast enough for the Pi.

**Implementation details**:
- SAT built as `u32` (max value 255×960×760 ≈ 185M < u32::MAX, safe)
- SAT build loop is serial (data dependency between rows)
- Subtraction parallelised with `par_chunks_mut(width)` over rows
- Rectangle query: `sat[y1*(w+1)+x1] + sat[y0*(w+1)+x0] - sat[y0*(w+1)+x1] - sat[y1*(w+1)+x0]`
- `global_rms`: `sqrt(mean(pixel²))` over the raw (pre-subtraction) frame
- Default filter size: 25 (matching olive-solve's `filtsize=25`)

**API**: `detect_stars(..., bg_mode="uniform_mean", uniform_filter_size=25, noise_mode="global_rms")`

Aliases accepted: `"uniformmean"`, `"local_mean"`, `"box_filter"`.

---

### 5. CLAUDE.md updated

`bg_mode` API documentation updated for all seven modes. "Things deliberately
not done" section revised to reflect what has now been done.

---

## Cache Compatibility Note

`column_percentile`, `row_column_percentile`, `block_percentile`, and
`uniform_mean` are **not compatible** with the temporal background cache in
diofinder. They require a corrected full-image array as input, whereas the
cache provides per-row offsets only. The diofinder `bg_cache.py` gates these
to the per-frame path via `CACHE_COMPATIBLE_MODES`.

Only `row_percentile`, `line_median`, and `top_hat` compose with the cache.

---

## Recommendations

- Cross-build a `cp313` wheel and deploy to device before any field use of
  `uniform_mean` / `global_rms` (v0.11.0 feature).
- Run `tests/ab_background.py` on real sky frames for each new mode to
  establish star-count vs. sigma operating points before relying on them.
- `uniform_mean` + `global_rms` at `sigma=7–8` is the right starting point
  for direct olive-solve comparison.

---

## Version bump chain

| Version | Changes |
|---------|---------|
| 0.9.0   | Baseline (`top_hat`, `HAS_TOPHAT` probe, matched-filter gate) |
| 0.10.0  | Parallelised transpose; `column_percentile`, `row_column_percentile`, `block_percentile` |
| 0.11.0  | `uniform_mean`, `global_rms` |
