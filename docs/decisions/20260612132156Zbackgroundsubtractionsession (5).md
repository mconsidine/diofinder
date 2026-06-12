# Decision Record — Background estimation & subtraction (assessment only)

| Field | Value |
|---|---|
| **Session name** | peaceful-gates-b2pv2 — "Background estimation & subtraction across the eFinder repos" |
| **Session id** | `01SA11ppqKuFYvhqyfm3c2vy` |
| **Timestamp (UTC)** | 2026-06-12T13:21:56Z |
| **Repo** | **eFinder_cli** — role: ASSESSED READ-ONLY (no code changes made) |

> No changes were made to this repo. It was reviewed as part of a cross-repo
> assessment of background handling. The decisions/actions of the session
> concerned `diofinder` and `sycamore-extract`; see the records in those repos.

## Background-handling assessment (as found)
eFinder_cli is a **consumer** (Python LX200 finder for the Pi). Like diofinder it
owns no background math, but it delegates to **ESA's stock Tetra3** rather than
sycamore.

- Delegation point: `Solver/eFinder.py:289` —
  `tetra3.get_centroids_from_image(np_image, downsample=2)`. No background
  parameters are exposed; the only knob is `downsample`.
- Capture: Y-plane only from YUV420, 960×760 uint8, auto-exposure/AWB disabled.
  Raw bytes go straight to Tetra3 — no dark/flat/hot-pixel/filtering.
- Its actual "background strategy" is **exposure control, not subtraction**:
  `getAutoExp()` (`:403-423`) brackets exposure to land 20–50 centroids with
  peak ≤ 250. The ×5 contrast enhancement in `saveImage` (`:342`) is debug-JPEG
  only and never reaches the solver.

## Relevance to the session's decisions
eFinder_cli and diofinder are siblings: thin Pi wrappers over a black-box engine,
each exposing one tuning lever (`downsample`/exposure vs `detect_sigma`). The
difference: eFinder_cli's engine (ESA Tetra3) is open and is the same algorithmic
family that `tetra3rs` and `olive-solve` reimplement in Rust. No part of this
session changed eFinder_cli; it is recorded here only because it was reviewed.

## Recommendation
No action required. If eFinder_cli ever needs the background features built for
diofinder, the path is the same: adopt the sycamore `star_detect` engine (with
its `bg_mode`/`top_hat` and temporal cache) in place of stock Tetra3.
