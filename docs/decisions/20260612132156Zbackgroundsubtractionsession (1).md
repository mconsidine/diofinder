# Decision Record — Background estimation & subtraction

| Field | Value |
|---|---|
| **Session name** | peaceful-gates-b2pv2 — "Background estimation & subtraction across the eFinder repos" |
| **Session id** | `01SA11ppqKuFYvhqyfm3c2vy` |
| **Timestamp (UTC)** | 2026-06-12T13:21:56Z |
| **Dev branch (this session, local only)** | `claude/peaceful-gates-b2pv2` |
| **Repo** | **sycamore-extract** — role: AFFECTED (analysis + patches delivered; NOT pushed) |

> NOTE: sycamore-extract was **out of scope** for this session's write access and
> was a public read-only clone, so nothing here was pushed. All artifacts were
> delivered to the operator as patches/files to apply manually. Companion record
> in `diofinder` (affected) and in `cedar-detect`, `olive-solve`, `tetra3rs`,
> `eFinder_cli` (assessed read-only).

---

## 1. Assessment of sycamore-extract (as found)
sycamore-extract (`star_detect`) is the **detection engine** behind diofinder.
It already had a more capable background story than diofinder's docs implied:
- Per-row background floor via `bg_mode`: `row_percentile` (default) and
  `line_median` (histogram median, parallel).
- A DC-canceling **matched filter** gate (Gaussian σ=1.5 kernel `[-50,-15,35,60,
  35,-15,-50]`, threshold `σ·noise·107`).
- Robust **MAD** noise estimation (9-patch median).
- A temporal "analytic-threading" cache (`detect_stars_with_cache` +
  `compute_row_medians_py`, `examples/bg_cache.py`).
Gaps: no 2-D spatial background (only per-row), no dark/hot-pixel map.

## 2. Decisions taken
- **D1.** Add an **opt-in white top-hat** (`bg_mode="top_hat"` + `tophat_radius`,
  default 12), off by default; never replacing the per-row modes. Implemented as
  separable min/max via a monotonic-deque sliding window — **O(1) amortized per
  pixel regardless of radius** (van Herk / Gil-Werman). Detection runs on the
  residual; centroids/brightness stay measured on the original image, so
  photometry is unchanged.
- **D2 (precedent honored).** ARCHITECTURE.md records that a previously-tried
  *local-mean* pre-subtraction made detection worse (100→52 stars) because a mean
  eats star flux. A morphological **opening** does not subtract star flux, so the
  top-hat is a genuinely different op — but it stays opt-in so it is settled
  empirically per rig via `tests/ab_background.py`.
- **D3.** The temporal cache is **NOT obsoleted** by the top-hat; keep it. They
  compose (cached path can pass `tophat_radius>0`).
- **D4 (versioning).** The cedar removal that landed on `main` (gate_mode
  removed) is a **breaking** change but `main` is still labeled `0.8.0`,
  colliding with the old gate_mode-bearing `0.8.0`. The top-hat + cedar-removed
  API MUST be released as a new version (**0.9.0**).

## 3. Actions completed (delivered as patches/files; NOT pushed)
- Implemented the top-hat in `src/lib.rs` (functions `extreme_1d`, `morph_h`,
  `morph_v`, `white_tophat`; `bg_mode="top_hat"` + `tophat_radius` on
  `detect_stars` and `detect_stars_with_cache`) **on the pre-cedar-removal main**.
  `cargo build --release`, `cargo test` (4 new unit tests), and `cargo clippy`
  all passed. Added `tests/ab_background.py` and ARCHITECTURE/CLAUDE/README/
  CHANGELOG prose. Delivered as `sycamore-extract-tophat.patch`.
- Diagnosed and fixed the **CI failure** on `main`: `.github/workflows/test.yml`
  still asserted `'gate_mode' in sig.parameters` after gate_mode was removed.
  Delivered `sycamore-ci-fix.patch` (against `cf9a01e`) and a robust idempotent
  `fix_ci.py` (works regardless of which test.yml variant is present).

## 4. Current state (verified 2026-06-12)
- `main @ cf9a01e`, version **`0.8.0`**: cedar removed, `gate_mode` removed,
  **no top-hat** (`top_hat`/`tophat_radius`/`white_tophat` = 0 occurrences),
  **CI red** (a manual "edit test.yml" commit removed the wrong assert line),
  version not bumped.
- `matched-filter-only` branch still exists and is strictly behind `main`
  (redundant; safe to delete).
- The top-hat lives only on the local `claude/peaceful-gates-b2pv2` branch (based
  on the OLD, gate_mode-bearing main) and in the delivered patch. **It is not on
  `main`.** The old patch will NOT apply to cedar-removed `main` (it carries
  gate_mode); the top-hat must be re-implemented against current `main` without
  any gate_mode plumbing.

## 5. Open items / next steps (do sycamore FIRST — diofinder depends on the wheel)
1. Apply the CI fix to `main` (`python3 fix_ci.py`) → green.
2. Re-implement the top-hat on cedar-removed `main` (drop gate_mode references);
   bump `Cargo.toml` + CHANGELOG to **`0.9.0`**; re-add `tests/ab_background.py`
   (top-hat aware). Keep fmt/clippy clean (the `main` pre-push gate).
3. Update `test.yml` to also `assert 'tophat_radius' in sig.parameters`.
4. **Tag & push `v0.9.0`** → `build.yml` builds the aarch64 wheel and publishes a
   GitHub Release with it attached (this is what diofinder's Vendor Sycamore
   workflow downloads).
5. Delete the redundant `matched-filter-only` branch.

## 6. Recommendation summary
The white top-hat is the right Pi-Zero-2W-appropriate next layer (O(1) morphology,
NEON-friendly, single op for vignette+glow+gradient). Keep it opt-in and validate
per rig. Land it as part of a real `0.9.0` release so the cedar-removed API stops
sharing a version number with the old one. Deferred layers (master-dark +
hot-pixel map; coarse mesh + RMS map) remain good future increments.
