# Decision Record — Background estimation & subtraction

| Field | Value |
|---|---|
| **Session name** | peaceful-gates-b2pv2 — "Background estimation & subtraction across the eFinder repos" |
| **Session id** | `01SA11ppqKuFYvhqyfm3c2vy` |
| **Timestamp (UTC)** | 2026-06-12T13:21:56Z |
| **Dev branch** | `claude/peaceful-gates-b2pv2` |
| **Repo** | **diofinder** — role: AFFECTED (code merged + decisions + pending actions) |

> Companion records were written into the same path in `sycamore-extract`
> (affected) and in `cedar-detect`, `olive-solve`, `tetra3rs`, `eFinder_cli`
> (assessed read-only). This file is self-contained for diofinder.

---

## 1. What this session set out to do
1. A comprehensive cross-repo assessment of how each repo handles background
   noise / sky-glow / haze / gradient / hot-pixel handling before star detection.
2. A recommendation for a background-subtraction approach suited to the Pi Zero
   2 W, grounded in literature.
3. Implement the chosen approach (a **white top-hat**) plus selectable background
   modes, with on/off toggles, A/B test tooling, and a verdict on the temporal
   "analytic-threading" cache.

## 2. Assessment of diofinder (as found)
- diofinder is a **consumer**: it owns no background math; it delegates star
  extraction to the **sycamore** `star_detect` engine and plate-solving to
  olive-solve.
- Before this session, `solver_proc.py` called `star_detect.detect_stars(...)`
  per frame with a single tunable, `detect_sigma` (default 7). No dark frames,
  flats, hot-pixel maps, gradient removal, or temporal model. Display-only
  median+arcsinh stretch in the web UI (cosmetic; never reaches the solver).

## 3. Decisions taken
- **D1.** Adopt a layered plan; implement the **white top-hat** first (opt-in,
  off by default) because it is self-contained and folds vignetting + sky-glow +
  gradient removal into one O(1)-per-pixel morphological op (van Herk /
  Gil-Werman). Defer the dark-frame/hot-pixel layer and the coarse-mesh+RMS
  layer to later increments.
- **D2.** Make every background option toggleable via config and live via
  `shared_cfg`.
- **D3.** Keep and **fully enable** the temporal "analytic-threading" cache (it
  is orthogonal to the top-hat: temporal buys √N noise reduction + free
  hot-pixel rejection; top-hat buys per-frame spatial flattening). Gate it with
  `bg_cache_enabled` so it can be switched off if too costly.
- **D4 (reconciliation).** Two parallel implementations of "configurable
  background + temporal cache" existed in diofinder: the one merged to `olive`
  (this session's, PR #18) and `claude/vigilant-wright-d9ndN` (another session,
  pending). Decision: **keep the merged version as the base**, harvest the new
  operator tooling from vigilant-wright (maint-socket commands, the
  `efinder-bg-setup`/`efinder-bg-test` scripts, the `webui/templates/bgtest.html`
  page), then **discard** the vigilant-wright branch.
- **D5.** **Land the top-hat properly** (not drop it): once sycamore ships a
  cedar-removed `0.9.0` with `tophat_radius`, vendor it and make diofinder
  consistent.

## 4. Actions completed (merged to `olive` via PR #18, commit `12b27c8`)
- Added `efinder/bg_cache.py` (`BackgroundCache`): routes detection through
  per-frame `row_percentile`/`line_median`/`top_hat` and/or the temporal cache;
  includes a `HAS_TOPHAT` capability probe that degrades to `line_median` on
  older sycamore wheels.
- `solver_proc.py`: submits frames + IMU motion to the cache; routes extraction
  through `bg_cache.detect(...)`; stops the worker on shutdown.
- `config.py`: new keys `detect_bin`, `detect_bg_mode`, `detect_tophat_radius`,
  `bg_cache_enabled`, `bg_cache_stack`, `bg_cache_refresh_s`,
  `bg_cache_slew_deg`, `bg_cache_max_age_s`.
- `tests/diag_background.py`: on-device A/B of the background modes
  (`--inject-gradient` to stress the glow case).
- Docs: CLAUDE.md extraction section + `shared_cfg` keys table.
- Validation in-session: py_compile, config-load test, and a stubbed-`star_detect`
  routing test covering all branches incl. graceful degrade on an old wheel.

## 5. Current state (verified 2026-06-12 against remotes)
- Default branch **`olive` @ `59b6fa7`** has the integration merged.
- It **runs today** because the vendored wheel
  `vendor/wheels/star_detect-0.8.0-...aarch64.whl` is the **OLD** sycamore 0.8.0
  (verified from its compiled `__text_signature__`: it has `gate_mode` and
  `bg_mode`, but **no `tophat_radius`**). So `gate_mode=` calls succeed and
  `top_hat` requests gracefully degrade to `line_median`.

## 6. Open items / next steps (in order)
1. **(sycamore first)** Ship sycamore `main` as **`v0.9.0`** with the top-hat and
   cedar removed; tag `v0.9.0` so `build.yml` publishes the wheel as a Release.
2. **Drop `gate_mode="matched_filter"` from `efinder/bg_cache.py`** (both call
   sites, ≈ lines 186/197). **Must land on `olive` before/with the new wheel** —
   the cedar-removed wheel has no `gate_mode`; passing it raises `TypeError` on
   every frame.
3. **Run the "Vendor Sycamore (star_detect)" workflow** (Actions → workflow_dispatch,
   `version=0.9.0`, `python=cp313`). It downloads the Release wheel, removes the
   stale 0.8.0 wheel, and commits straight to `olive`. `install.sh` picks the
   wheel up by glob (no filename pin to edit).
4. Fold in the vigilant-wright tooling (D4); extend the maint-socket validation
   to accept `top_hat` + `detect_tophat_radius`; then delete
   `claude/vigilant-wright-d9ndN` and the stale `hybrid` branch.
5. (Optional) Rebuild the SD image via `release.yml` if you distribute images.
6. Keep `sycamore-only` — it backs the tetra3-py vendor + image-release plumbing;
   not a stray branch.

## 7. Landmines recorded
- **Version collision:** the cedar-removed sycamore is still labeled `0.8.0`,
  colliding with the old gate_mode-bearing `0.8.0`. The new engine MUST be a new
  version (0.9.0) or vendored wheels are indistinguishable.
- **gate_mode TypeError** as in step 2 above.
- **Top-hat radius must exceed the largest star radius** or the opening eats
  stars (watch the `base_only` column in the A/B harness).

## 8. Recommendation summary
Finish the three-step ordering (release sycamore 0.9.0 → land gate_mode removal on
`olive` → run Vendor Sycamore). After that, top-hat is functional and the two
repos are consistent. The deferred dark-frame/hot-pixel and coarse-mesh+RMS
layers remain available as future increments but are not required for a clean,
working state.
