# Session decision record — seeing-robustness, release reconciliation, deep DB

**Date:** 2026-06-12 → 2026-06-13
**Session:** beautiful-ritchie-iqyu8w (`session_01NuuZyLLvCjvC1b2XouLesm`)
**Branch of record:** `claude/beautiful-ritchie-iqyu8w` → merged to `olive` (diofinder)
and `main` (the four library repos).
**Companion docs:** `docs/technical-assessment.md` (rev 3),
`docs/current-state-and-actions-2026-06-12.md` (live to-do),
`docs/scripts-and-tests-guide.md` (usage guide).

This record exists so a fresh session can pick up without replaying the
conversation. It captures (1) what shipped, (2) the bugs found and fixed,
(3) the remaining to-do items, and (4) seven new improvement ideas discussed
near the end, including the IMU-less analysis.

---

## 1. What this session started from and delivered

**Premise:** a full technical comparison of diofinder's pipeline
(sycamore-extract extraction + olive-solve solving) against the other repos
(cedar-detect, cedar-solve, tetra3rs, olive-solve, eFinder_cli, astro_databases),
asking where another extractor/solver could be *faster* or *more robust across
seeing conditions*. Conclusion: the shipped pair is already the speed optimum
for the Pi Zero 2W; the wins are seeing-robustness gaps closable in-house, not
component swaps. (Full reasoning in `docs/technical-assessment.md`.)

**Delivered and released:**

- **sycamore-extract v0.12.0** — runtime-tunable matched-filter `kernel_sigma`
  (1.0–4.0, default 1.5 reproduces the legacy kernel bit-for-bit); full 2-D
  moment (`m2_xy`) trail rejection via `max_axis_ratio`; perimeter-derived local
  noise inflation (`local_noise`, cedar-detect-inspired, independent impl);
  `bin=4`; block-grid temporal cache (`compute_block_medians_py` +
  `detect_stars_with_cache(block_offsets=...)`) so `block_percentile` composes
  with the temporal cache. Default path verified bit-identical to 0.11.2.
- **olive-solve v0.1.2** — the `noext` line (rayon-parallel deterministic solver,
  `extractor` feature gate, attitude-hint/strict_hint, watchdog/cancel) merged to
  `main`; wheel `tetra3-0.1.2-…aarch64.whl`.
- **astro_databases** — deep G≤8.5 variant (`--variant deep`); released as
  **v2026.06.1** with `cedar_solve_13deg_mag85.npz` + `tetra3rs_13deg_mag85.bin`
  after the user generated the G≤9.0 source catalog locally.
- **diofinder image v0.0.25** — seeing presets (`efinder/seeing.py`, Good/Bad
  toggle, `seeing_set`/`seeing_get`, `efinder-ctl seeing`, webui); every preset
  key individually live-tunable; previously-hardcoded `max_axis_ratio=inf` and
  config-only match params now live; `block_percentile` cache path; auto-exposure
  default ON; `save_failed_frames` implemented (100 MB cap); solver-hang
  watchdog; systemd `ExecStartPre` SHM cleanup; hot-pixel dark-capture mask
  (`efinder/hot_pixel.py`); off-device `scripts/calibrate_lens.py`.
- **Node-24 GitHub Actions bumps** across all five repos (deadline 2026-06-16).
- **Docs**: technical-assessment (3 revisions), current-state-and-actions,
  scripts-and-tests guide, decision-record dedupe (5 byte-identical removed).
- **Deep-DB device plumbing**: `efinder-db-update` now also fetches the
  `_mag85.npz` when present (SHA-verified); `star_db_deep` defaults to it.

### Seeing preset table (shipped; values are starting points pending on-sky A/B)

| Key | Good | Bad |
|---|---|---|
| `detect_sigma` | 5.0 | 4.0 |
| `detect_kernel_sigma` | 1.5 | 2.5 |
| `detect_bg_mode` | row_percentile | block_percentile |
| `detect_max_axis_ratio` | 3.0 | 5.0 |
| `min_centroids` | 8 | 5 |
| `match_radius` | 0.01 | 0.015 |
| `match_threshold` | 1e-5 | 1e-5 |
| `solve_timeout_ms` | 1500 | 3000 |
| `auto_exposure_target_stars` | 20 | 15 |
| `auto_exposure_max_s` | 0.5 | 1.0 |
| `star_db` | standard | deep (if `star_db_deep` file exists) |

---

## 2. Bugs found and fixed this session (lessons to keep)

1. **olive-solve release wouldn't trigger** — `release-wheels.yml` lived only on
   `olive-solve-noext`, not the default branch `main`, so GitHub's "Run workflow"
   button never appeared and a tag on a stale `main` commit built nothing.
   Fixed by reconciling noext→main.
2. **Wheel named 0.1.0 despite a Cargo.toml bump** — maturin takes the wheel
   version from `tetra3-py/pyproject.toml`, *not* Cargo.toml. **Lesson: grep
   Cargo.toml(s) AND pyproject.toml before tagging any maturin/PyO3 release.**
3. **sycamore 0.12.0 wheel briefly sat inside the v0.11.2 release** — replaced;
   v0.11.2 now serves no wheel, so never pin `SYCAMORE_TAG=v0.11.2`.
4. **Circular "standard" database resolution** (observed on-device): `seeing_set`
   persists `solver_db` on every switch, and `resolve_star_db("standard")` fell
   back to the current `solver_db` — so after one Bad toggle, "standard" resolved
   to the deep path forever (Good preset stopped switching back). Fixed by
   snapshotting the outgoing standard db into a new `star_db_standard` config key
   the first time the preset leaves it (commit `f36e537`). **On-device repair
   for already-poisoned confs:** set `solver_db: default_database` and add
   `star_db_standard: default_database`, then `efinder-update --ref olive` to get
   the code that understands the new key. Verified working by the user.

---

## 3. Remaining to-do items (pre-existing / carried)

Authoritative live list is `docs/current-state-and-actions-2026-06-12.md`. As of
this record:

1. **On-device / on-sky verification batch** — the only real gate left. One
   clear-night session: `tests/bench.py` p50 on test1–3 for **both** presets
   (Bad exercises the new non-default code paths); Good/Bad A/B on marginal
   frames; deep-DB A/B (standard vs `_mag85` on marginal frames);
   `bench_pipeline_combos.py --live-shm`; auto-exposure convergence; watchdog
   fire/restart; dark-frame capture; run `calibrate_lens.py` on saved **solved**
   frames and set `distortion:`. Tune `efinder/seeing.py` from the A/B data.
2. **Watch first post-Node-24 workflow runs** in each repo (none have run since
   the bump; artifact actions crossed multiple majors — v7/v8 in the four libs,
   v5 in diofinder; one-line downgrade if v7 upload semantics surprise).
3. **Bake the deep DB into the image** (optional) — `build-image.sh` stages one
   database via chroot; adding a second touches `release.yml` + `build-image.sh`
   + `install.sh`. Today `efinder-db-update` covers it post-flash in one command.
4. **Next-cycle, deferred:** olive-solve f32 kd-tree/vector math (est. 20–40%
   verification speedup, needs an on-device baseline first); astro_databases
   regen at the calibrated FOV (13.497° vs 10.5–14°) + retire the "13deg" label;
   tetra3rs cibuildwheel→maturin migration (cibuildwheel v4 deliberately NOT
   taken in the Node bump — it changes wheel-repair defaults); olive-solve gRPC
   `parallel` proto field (minor); docs/decisions naming convention.
5. **Optional housekeeping:** delete the three merged session branches still on
   origin (`claude/funny-noether-…`, `claude/pensive-allen-…`,
   `claude/vigilant-brahmagupta-…`). The superseded `hybrid` and `sycamore-only`
   were already deleted.

---

## 4. Seven new improvement ideas discussed (NOT yet implemented)

Ordered by expected payoff. The recurring theme: the biggest wins are
architectural, not new algorithms.

1. **Tracking mode / ROI detection + verify-only solving.** Sycamore's
   ARCHITECTURE.md describes 48-px ROI windows (~1.5 ms vs ~6 ms full-frame) and
   event-driven detection; diofinder runs full-frame every frame. Pair with a
   verify-only solver mode (project catalog through propagated attitude, match,
   refine — works with 2–3 stars) so blind lost-in-space solving becomes the
   recovery path, not the steady state. **Highest-leverage item.**
2. **Auto-seeing.** A slow controller (like the auto-exposure thread) that
   watches solve rate, star count, and PSF FWHM (now available from gate_2d's
   2-D moments) and flips the Good/Bad preset automatically. Could be an "Auto"
   third position on the webui toggle.
3. **Saved-frame regression corpus.** Curate the now-saved failed/solved frames
   into a labeled golden set (good / moonlit / thin-cloud / defocused) + an
   off-device replay harness reporting match rates per preset/mode. Turns every
   future change into a measurable A/B. **Do this first — small, compounding;
   the captures dir is already filling with raw material.**
4. **Signal stacking under poor transparency.** The temporal cache stacks for
   *background*; nothing stacks for *signal*. On TOO_FEW frames, mean-stack 2–4
   aligned frames (~0.7 mag deeper, free) before detection. Software-only;
   engages only on already-failing frames.
5. **Retry-wider-kernel on failure.** Cheaper sibling of #4: on TOO_FEW,
   re-detect once with `kernel_sigma` bumped before declaring failure. One extra
   pass only on already-failed frames.
6. **Operational dark model.** Generalize the static hot-pixel mask: accumulate a
   per-pixel low-percentile across many pointings/sessions (sensor defects are
   pixel-fixed, sky drifts → defects separate statistically), persist it, refresh
   the mask automatically. Keeps the mask valid as the sensor warms/ages; manual
   `dark_capture` remains the instant bootstrap. (cedar-detect ideas.txt #2/#3.)
7. **Solve-history ring buffer + webui graph** (old TODO): solve rate / FWHM /
   star counts over the session — also the observability layer #2 wants.

**Explicitly not worth doing** (closed): 16-bit pixel path (finder accuracy
target doesn't need it); cedar-detect as a runtime sidecar (slower on 3 cores,
gRPC overhead, less defocus-tolerant); aberration correction (~20″ ≪ 51″/px
scale); richer in-solver distortion models (pre-undistorting centroids from the
off-device SIP fit is simpler and sufficient — see §5).

---

## 5. Two Q&A threads worth carrying forward

**Off-device `calibrate_lens.py` vs. live FOV self-calibration.** They fit
*different* distortion models. The live calibrator estimates a single radial `k`
(all olive-solve can consume), bootstrapped from solved frames. `calibrate_lens.py`
(tetra3rs, off-device) fits a full SIP polynomial (order ≤6, multi-frame,
sigma-clipped), capturing higher-order radial + tangential/decentering +
off-center distortion a single `k` can't. Its job: tell you *whether one `k` is
sufficient* (if SIP residuals are quadratic-dominated, you're done; if corner
residuals persist, edge stars leak outside `match_radius` and cost matches on
low-star frames) and give a clean reference `k`. The full SIP model is only
usable if a future pre-undistort step is added to `solver_proc` before handing
centroids to olive-solve — the natural upgrade if the fit shows `k` is inadequate.
For this, enable `save_solved_frames`, collect 10–20 PNGs across varied
pointings, scp to a dev box, run the script.

**IMU-less operation (the new ideas under no IMU).** The solver is itself a
1–2 Hz attitude sensor, and sidereal drift is ~0.3 px/s at 51″/px, so almost
everything survives without an IMU. Steady-state ROI tracking (#1) is *identical*
IMU-less (windows placed from last solve + sidereal rate). The IMU's unique
value is during fast manual slews: (a) it reports motion at 20 Hz, (b) it sees
through motion blur that stops solves entirely, (c) it dead-reckons an attitude
prior so the first solve after a slew is *seeded* (~10–100 ms) not *blind*
(~300–800 ms). Under ROI this contrast sharpens: verify-only needs an attitude
prior to start, so without an IMU you must climb back via a full-frame blind
solve after every slew before re-entering the cheap path. **Required fix for
IMU-less correctness:** `bg_cache` slew invalidation is currently IMU-driven
(`note_motion`); feed it from solver output too (invalidate on a large
solved-attitude jump or a run of failed solves) so the temporal cache doesn't
serve a stale background after an unsensed slew. This `note_motion`-from-solver
fallback makes IMU-less *correct* but not *equivalent*: it closes the stale-cache
hole, but cannot recover the IMU's latency/blur/seeded-recovery advantages.
**Net:** IMU-less loses fast slew recovery and smooth high-rate pose between
solves; steady-state detection and solving are unchanged. Add the
solver-derived `note_motion` to the to-do list if IMU-less units are a supported
configuration.

---

## 6. Quick-start for the next session

- **Branch:** cut a fresh `claude/*` branch from `olive` (diofinder) / `main`
  (libraries). The session branches above are merged; don't build on them.
- **Released stack:** image v0.0.25, star_detect v0.12.0, tetra3 v0.1.2, DB
  v2026.06.1. Do not pin `SYCAMORE_TAG=v0.11.2` (no wheel).
- **First action if implementing:** the regression corpus (#4.3) — it makes
  every other change measurable.
- **First action if observing:** the on-sky verification batch (§3.1) — it
  produces the data the seeing presets and kernel_sigma were tuned blind.
- **Open correctness item:** solver-derived `note_motion` fallback (§5) if
  IMU-less is supported.
