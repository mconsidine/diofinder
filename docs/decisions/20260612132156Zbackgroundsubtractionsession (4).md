# Decision Record — Background estimation & subtraction (assessment only)

| Field | Value |
|---|---|
| **Session name** | peaceful-gates-b2pv2 — "Background estimation & subtraction across the diofinder repos" |
| **Session id** | `01SA11ppqKuFYvhqyfm3c2vy` |
| **Timestamp (UTC)** | 2026-06-12T13:21:56Z |
| **Repo** | **tetra3rs** — role: ASSESSED READ-ONLY (no code changes made) |

> No changes were made to this repo. It was reviewed as part of a cross-repo
> assessment of background handling. The decisions/actions of the session
> concerned `diofinder` and `sycamore-extract`; see the records in those repos.

## Background-handling assessment (as found)
tetra3rs is a **lost-in-space solver** (Rust) whose `image`-feature
`centroid_extraction` module is the most textbook-astronomy background pipeline
of those reviewed. All in `src/centroid_extraction.rs`:

- **Stage 1 — local background** (`estimate_local_background`, `:454`): 64-px
  tiles, per-tile median via quickselect, **bilinear-interpolated** into a smooth
  background surface, subtracted with clamp-to-zero (~60% of extraction runtime;
  parallel under `parallel`, bit-identical).
- **Stage 2 — global noise** (`estimate_background`, `:570`): robust σ from the
  **lower half** of the residual distribution (stars only contaminate the upper
  half), sigma-clipped 5×/3σ.
- **Stage 3 — per-blob** (`:771`): 5-px annulus median before the
  intensity-weighted centroid + quadratic sub-pixel fit.
- Optional Gaussian **matched filter** feeds the detection mask only, never the
  photometry. Hot pixels handled implicitly via `min_pixels ≥ 3`.

## Relevance to the session's decisions
- tetra3rs's tiled-median+bilinear background is the **direct analogue** of the
  deferred sycamore "coarse mesh + RMS" layer (and matches olive-solve's
  `BlockMedian`). If that layer is pursued, the block-size and lower-half noise
  conventions here are the reference.
- The "matched filter affects the mask, not the photometry" design mirrors the
  principle adopted for the sycamore top-hat (detection on residual; centroids on
  the original image).

## Recommendation
No action required here. Reference for the mesh-background and lower-half noise
estimation if/when that sycamore layer is implemented.
