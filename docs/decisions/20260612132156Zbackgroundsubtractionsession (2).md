# Decision Record — Background estimation & subtraction (assessment only)

| Field | Value |
|---|---|
| **Session name** | peaceful-gates-b2pv2 — "Background estimation & subtraction across the eFinder repos" |
| **Session id** | `01SA11ppqKuFYvhqyfm3c2vy` |
| **Timestamp (UTC)** | 2026-06-12T13:21:56Z |
| **Repo** | **cedar-detect** — role: ASSESSED READ-ONLY (no code changes made) |

> No changes were made to this repo. It was reviewed as part of a cross-repo
> assessment of background handling. The decisions/actions of the session
> concerned `diofinder` and `sycamore-extract`; see the records in those repos.

## Background-handling assessment (as found)
cedar-detect is a **detection engine** (Rust, Cedar/Tetra3 lineage, Steven
Rosenthal). Its background model is the most CPU-minimal of the engines reviewed:

- **No full-image background array.** Background is read off the *edges* of a
  small detection window:
  - 1-D pass (`gate_star_1d`, `src/algorithm.rs:260-320`): 7-pixel window
    `|lb lm l C r rm rb|`; background = `lb + rb`; detect if
    `2C − (lb+rb) ≥ 2σ·noise`.
  - 2-D pass (`gate_star_2d`, `:573-775`): concentric core/neighbor/margin/
    perimeter rectangles; background = mean of the 3-px perimeter; it
    **re-estimates local noise** from perimeter stddev and uses
    `max(global, local)` so bright clutter (moon, streetlight) self-suppresses.
- **Global noise** (`estimate_noise_from_image`, `:875`): samples **3 horizontal
  strips at the image midline** (deliberately, to absorb row-offset banding),
  picks the darkest, de-stars via histogram.
- **Explicit hot-pixel rejection** (`classify_pixel`, `:1155`) — the only engine
  reviewed with a designed-in hot-pixel test.
- Multi-resolution binning (1/2/4/8) with per-level noise re-estimation.

## Relevance to the session's decisions
cedar-detect is the **reference** for the deferred "master-dark + hot-pixel map"
layer: its `classify_pixel` neighbor-contribution test is the implementation to
port if hot-pixel false positives become a problem on the Pi finder. Its
horizontal-strip noise sampling is one of two strategies seen for sensor row
banding (the other being olive-solve's per-row LineMedian subtraction).

## Recommendation
No action required here. If the diofinder/sycamore line later needs explicit
hot-pixel handling, lift the `classify_pixel` approach from this repo.
