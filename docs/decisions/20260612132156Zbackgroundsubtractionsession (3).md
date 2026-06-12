# Decision Record — Background estimation & subtraction (assessment only)

| Field | Value |
|---|---|
| **Session name** | peaceful-gates-b2pv2 — "Background estimation & subtraction across the eFinder repos" |
| **Session id** | `01SA11ppqKuFYvhqyfm3c2vy` |
| **Timestamp (UTC)** | 2026-06-12T13:21:56Z |
| **Repo** | **olive-solve** — role: ASSESSED READ-ONLY (no code changes made) |

> No changes were made to this repo. It was reviewed as part of a cross-repo
> assessment of background handling. The decisions/actions of the session
> concerned `diofinder` and `sycamore-extract`; see the records in those repos.

## Background-handling assessment (as found)
olive-solve is a **detection engine** (Rust, cedar-solve port) and offers the
richest background menu of the engines reviewed — it is effectively an
optimization playground for the same Pi target.

- `BgSubMode` (`tetra3/src/extractor.rs:72`): `LocalMean` (default, separable box
  blur), `LocalMedian` (2-D median filter, `filtsize` default 25), `GlobalMean`,
  `GlobalMedian`.
- `FastBgSubMode` (`tetra3/src/fast_extractor.rs:34`): integer pipeline adding
  **`BlockMedian`** (tiled median + bilinear interpolation — a 2-D mesh
  background) and **`LineMedian`** (per-row histogram median, purpose-built for
  horizontal banding/read-noise).
- σ estimation (`SigmaMode`): RMS or MAD×1.48, global or local; default
  `GlobalRootSquare`, `sigma` default 2.0.
- Hot pixels handled via **binary opening** (erosion+dilation) before CCL.

## Relevance to the session's decisions
- olive-solve's **`BlockMedian`** is the direct analogue of the deferred
  "coarse mesh + RMS" layer for sycamore, and is conceptually the same
  tiled-median+bilinear idea that `tetra3rs` also implements. If that layer is
  pursued, this is a reference.
- olive-solve's **`LineMedian`** mirrors what sycamore already does per-row; its
  existence confirmed the per-row approach as sound and named the banding case.
- The chosen path for sycamore was the morphological **white top-hat** instead of
  BlockMedian (O(1) per pixel, no grid/interpolation bookkeeping). BlockMedian
  remains a viable alternative if the top-hat underperforms in field A/B.

## Recommendation
No action required here. Treat `BlockMedian`/`LineMedian` as reference
implementations for any future sycamore mesh-background work.
