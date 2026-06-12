# Technical Assessment — diofinder Pipeline vs. Alternative Extractors & Solvers

**Revision 2 — 2026-06-12.** Supersedes the session-delivered comparison of the same
date; updated for the current state of all repos (seeing presets merged to `olive`,
sycamore-extract 0.12.0 on main, olive-solve `noext` line, astro_databases deep
variant released). Operational follow-ups live in
`docs/current-state-and-actions-2026-06-12.md`.

"Better" throughout means **faster** or **more able to solve across a range of
seeing/sky conditions** (turbulence, transparency, moonlight/gradients, wind).

---

## 1. Verdict (unchanged in direction, updated in detail)

**The shipped `olive` pipeline — sycamore-extract (star_detect) extraction +
olive-solve (noext) solving — remains the best speed combination available across
the assessed repos for the Pi Zero 2W, and as of 0.12.0 the principal
seeing-robustness gaps in the extractor have been closed in-house rather than by
switching components.**

- **cedar-detect** is structurally immune to 2-D sky gradients (purely local
  thresholding) and has explicit hot-pixel classification, but is single-threaded
  scalar (NEON is an external plug-in), u8-only, gRPC-sidecar-only, and *less*
  tolerant of defocus past its `width/100` blob cap. On a 3-core A53 deployment it
  would be slower than sycamore. Its two best ideas — perimeter-derived local noise
  and persistent hot-pixel masking — are now implemented natively (sycamore 0.12
  `local_noise`; diofinder `hot_pixel.py` dark-capture mask).
- **tetra3rs** remains the precision instrument: SIP polynomial + multi-image
  distortion calibration, aberration correction, Gaia DR3 catalog. As a *live*
  solver it spends ~90% of an easy solve in WCS refinement a finder doesn't need and
  its postcard DB fully deserializes into RAM. Its role here: **off-device lens
  calibration** (`scripts/calibrate_lens.py`) and the second consumer of the
  astro_databases pipeline.
- **cedar-solve** is the database generator and algorithmic reference; olive-solve
  is its ~130× Rust port and reads its `.npz` databases directly. No runtime role.
- **eFinder_cli** (ESA tetra3, pure Python) is strictly dominated; retained as the
  protocol/UX reference.
- **Superseded in-house alternatives:** the `hybrid` branch (cedar-detect gRPC +
  olive-solve) and the testrepo aggregator CI are dead lines — see the actions doc.

---

## 2. Extractor state

Current default path: `BackgroundCache.detect()` → `detect_stars_with_cache`
(temporal 8-frame median model) with per-frame fallback during slew/warm-up.

| Capability | 0.11.2 (released) | 0.12.0 (main, **untagged**) |
|---|---|---|
| Matched-filter kernel | fixed σ=1.5 (compile-time) | `kernel_sigma` 1.0–4.0 runtime; σ=1.5 bit-identical to legacy |
| Trail rejection | separable var only (diagonal-blind); diofinder had it disabled | full 2×2 covariance (m2_xy) eigen-ratio; skipped when `max_axis_ratio=inf` |
| Local noise | one global figure per frame | per-blob perimeter noise inflation (`local_noise`, default on; cedar-detect-inspired) |
| Binning | 1/2 | 1/2/4 (defocus escape hatch) |
| Temporal cache background | per-row offsets only | row **or** 2-D block grid (`block_offsets` + `compute_block_medians_py`) — `block_percentile` is now cache-compatible |

Remaining extractor limitations (accepted): u8-only; matched filter's conservative
bias on correlated noise (mitigated by per-preset sigma); cached path needs IMU
`note_motion` wiring to invalidate (wired in diofinder).

**Gap:** 0.12.0 has no release tag, so deployed devices still run 0.11.2 and the new
parameters are capability-probed into inertness. Tagging v0.12.0 is the highest-value
single action for seeing robustness (S1 in the actions doc). On-Pi p50 targets
(≤6 ms per-frame, ≤4 ms cached at bin=2) must be re-verified with 0.12.0 — default
path is bit-identical by construction, so regressions are only possible behind
non-default parameters.

## 3. Solver state

olive-solve **noext** is the branch of record (pensive-allen decision) and the one
diofinder consumes (v0.1.1 release; v0.1.2-from-noext is the pending release — see
actions doc §1). Relative to upstream cedar-solve it adds: zero-alloc scratchpads,
deterministic rayon-parallel candidate search (3.61× on 4 threads, bit-identical to
serial; `parallel_parity.rs`, 738 fixtures), attitude-hint cone rejection +
`strict_hint`, watchdog/cancel, both DB probing schemes, Cortex-A53 codegen pin, and
an `extractor` cargo feature so the diofinder wheel drops ~4,300 LOC of unused
extractor (`--no-default-features`) — sycamore is the extractor.

Note for historical accuracy: olive-solve `main` does **not** contain the parallel
solver or the feature gate; performance claims for the deployed system attach to
**noext** wheels only. Deferred solver work: f32 kd-tree/vector math (est. 20–40%
verification speedup, needs on-device validation), gRPC `parallel` field.

## 4. diofinder integration state

Implemented and merged to `olive` (PR #27 and successors):

- **Seeing presets + Good/Bad toggle** (`efinder/seeing.py`, `seeing_set`/`seeing_get`
  maint commands, dashboard/Config toggle, drift display, `efinder-ctl seeing`).
  Every preset key individually live-tunable (`solver_params_set`,
  `match_params_set`); presets are exactly equivalent to setting keys by hand.
- Trail rejection wired (`detect_max_axis_ratio`, 0=off; previously hardcoded inf).
- `block_percentile` temporal-cache path (activates on sycamore ≥0.12).
- Auto-exposure default ON, live target/max; failed-frame saving (100 MB cap);
  solver-hang watchdog (verified kill cascades launcher → systemd restart);
  systemd `ExecStartPre` SHM/socket cleanup; hot-pixel dark-capture mask applied
  pre-detection (works during slews, when the temporal cache is offline);
  `scripts/calibrate_lens.py` (off-device tetra3rs SIP fit).

### Preset table (shipped values — pending on-sky A/B)

| Parameter | Good | Bad | Why |
|---|---|---|---|
| `detect_sigma` | 5.0 | 4.0 | recover faint stars when PSFs bloat |
| `detect_kernel_sigma` | 1.5 | 2.5 | match kernel to PSF width (needs ≥0.12 wheel) |
| `detect_bg_mode` | row_percentile | block_percentile | 2-D moon/haze gradients; cache-compatible on ≥0.12 |
| `detect_max_axis_ratio` | 3.0 | 5.0 | trail cut; looser when seeing smears stars |
| `min_centroids` | 8 | 5 | binomial verification is the real FP guard |
| `match_radius` | 0.01 | 0.015 | larger centroid errors under turbulence |
| `match_threshold` | 1e-5 | 1e-5 | never loosen the FP guard |
| `solve_timeout_ms` | 1500 | 3000 | deeper enumeration with poorer centroids |
| auto-exposure target / max | 20 / 0.5 s | 15 / 1.0 s | longer integration for faint smeared stars |
| database | standard G≤8.0 | deep G≤8.5 if installed | catalog completeness for the *detected* set when every centroid counts |

The deep-DB entry is second-order and opt-in (inert unless `star_db_deep` points at
an existing file); its mechanism is verification completeness at low star counts,
not "detecting fainter" — see the actions doc and astro_databases README for the
remaining catalog-download step.

## 5. Database state

astro_databases `v2026.06`: Gaia DR3+Hipparcos merge (63,491 stars, G≤8.0,
epoch 2026.0, 10.5–14°), deterministic builds, `--variant deep` (G≤8.5) shipped;
deep assets await the one-time G≤9.0 catalog download. Next-cycle: regenerate at the
calibrated FOV (13.497°) and retire the "13deg" label.

## 6. Recommendation scorecard (from Revision 1)

| Recommendation | Status |
|---|---|
| 1. Tunable matched-filter kernel sigma | **Implemented** (sycamore 0.12, main) — unreleased |
| 2. Trail rejection on + m2_xy | **Implemented** (0.12 + diofinder presets) |
| 3. Perimeter local noise; persistent hot-pixel map | **Implemented** (0.12 `local_noise`; diofinder `hot_pixel.py`) |
| 4. 2-D-capable cached background | **Implemented** (0.12 block cache + bg_cache.py) |
| 5. Auto-exposure default on; lower min_centroids in Bad | **Implemented & merged** |
| 6. Deeper database variant | **Implemented & released** (catalog download pending) |
| 7. Off-device tetra3rs lens calibration | **Implemented** (`scripts/calibrate_lens.py`) — not yet run on real frames |
| 8. save_failed_frames write path | **Implemented & merged** |
| 9. Solver-hang watchdog | **Implemented & merged** |
| 10. bin=4 escape hatch | **Implemented** (0.12) — unreleased |
| 11. Live-mutable match params | **Implemented & merged** |
| 12. systemd SHM cleanup | **Implemented & merged** |

Everything not yet *operational* funnels through three bottlenecks: the olive-solve
v0.1.2-from-noext release, the sycamore v0.12.0 release, and one on-device
verification session. See the actions document.
