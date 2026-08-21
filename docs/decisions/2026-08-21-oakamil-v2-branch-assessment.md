# oakamil/olive-solve `v2` branch — assessment for our fork

**Date:** 2026-08-21
**Question:** Assess oakamil's (upstream original author) `v2` branch of
`olive-solve` for possible use in our fork (`mconsidine/olive-solve`, `main` =
v0.1.7 — the branch diofinder consumes).

## TL;DR

- `v2` is oakamil's **active development line** (54 commits past the June-10
  merge-base `452602a`, through 2026-08-19) — much larger and newer than the
  `original` branch assessed on 2026-06-27.
- `v2` and our `main` **both rewrote `solver.rs` from the same merge-base in
  divergent directions** and `v2` **restructured the crate layout** (bindings
  moved to `tetra3/src/python.rs` + a new root fused-solver crate). A wholesale
  merge is not feasible.
- Decisively, **`v2` regresses the diofinder-critical set**: it has **no
  `attitude_hint` / `strict_hint` / `verify_attitude`** (0 occurrences anywhere),
  keeps the catalog/KdTree at **f64** (our `main` is f32), and is
  **single-threaded** (no rayon). `v2` was built on the `original` perf line, not
  on our `main`.
- **One standout, portable, net-new feature: horizon-based early rejection.**
  It is opt-in, orthogonal to f32/rayon/hint, ~90 lines, and directly usable by
  diofinder (which has observer lat/lon). Port that; skip the rest for diofinder.

## Topology

```
        452602a (merge-base, 2026-06-10)
        /        |         \
 our main(+41)  original   oakamil v2 (+54, through 2026-08-19)
 v0.1.7         (+perf)    built ON the original perf line, then +IMU/fusion,
 f32+rayon+hint            +horizon rejection, +extractor unify, +crate restructure
```

`v2` inherits the `original` perf work (early-rejection `valid_shape`, Cramer's
2×2, `ImmutableKdTree`) — which our `main` **also** already has (ported in
v0.1.7). So on the solver hot path the two branches now largely overlap in
*optimizations* but differ in *architecture* (f64/serial vs f32/rayon) and in
*API surface* (no hint/verify on v2).

## What v2 adds (by theme)

| Theme | Files | Notes |
|-------|-------|-------|
| **Fused IMU solver** | new root `src/lib.rs` (1272), `src/python.rs` (360); `olive-imu/` expanded to `bmi160`, **`bno055`**, `bno085`, `mpuxxxx`, `imu.rs` (1023) | `FusedSolver(database_path, imu_type)` — `imu_type` ∈ `bno085/bmi160/bno055/auto/none`; hardware-fused quaternion + accelerometer tracking, real-time MPU reads, IMU-thread lifecycle |
| **Horizon-based early rejection** | `tetra3/src/solver.rs` (+77), `tetra3/src/python.rs` (+15) | observer lat + LST → reject sub-horizon patterns; `min_boresight_altitude` gate |
| **Extractor unification** | deleted `fast_extractor_seq.rs` (-1043), reworked `fast_extractor.rs` | merges the parallel/sequential duplication (the maintainability rec from the efficiency assessment) |
| **Hot-loop perf** | `solver.rs` | "bitwise masks + hoisted math" |
| **Batch / virtual-crop solving** | `solver.rs`, bindings | target-pixel-center default, virtual-crop solutions, `get_matches_for_centroids` |
| **Crate restructure** | `tetra3-py/src/lib.rs` (−286), `tetra3/src/python.rs` (+404), new root crate | bindings relocated; new workspace member |

## Why a wholesale merge is off the table

Adopting `v2` as our `main` would **regress** all of:

1. **f32 catalog/KdTree** — `v2` is `ImmutableKdTree<f64,3>`; loses the 512 MB
   memory-bandwidth win our `main` has.
2. **rayon multi-core solve** — `v2` has no rayon (confirmed: no dep, no
   `par_chunks`/`find_map_first`); loses the 3-core speedup on the Pi.
3. **attitude_hint / strict_hint / verify_attitude** — absent on `v2`.
   diofinder's `tracking.py` (verify-only, tight-cone) and IMU-propagated hint
   solves **require** these; losing them breaks tracking.

…plus a large mechanical conflict from the crate restructure. Same verdict as
the `original` branch, only stronger.

## Port candidates (ranked)

### 1. Horizon-based early rejection — **HIGH value, port it**
Three opt-in `Option` fields on `SolveOptions` (`observer_latitude`,
`observer_lst`, `min_boresight_altitude`), default `None` = off:

- **Pre-compute** the zenith unit vector once per solve, in the catalog
  (equatorial/ICRS) frame: `[cos(lat)cos(lst), cos(lat)sin(lst), sin(lat)]`.
  Mathematically sound — zenith RA = LST, zenith Dec = observer latitude.
- **Per-candidate gate** (in the hot combinatorics loop, *before* SVD/verify):
  reject any 4-star pattern with a star below the horizon (`star · zenith < 0`).
  A 4×(3-mul dot product) cull that runs before the expensive path.
- **Boresight gate** (post-solve): reject a solution whose boresight (rotation
  matrix column 2) altitude is below `min_boresight_altitude`.

**Why valuable:** at any given time/place ~half the celestial sphere is below
the horizon, so this prunes a large fraction of false-positive candidate
patterns instantly — a real **blind-solve speedup** and, independently, a
**false-positive rejection** win (a sub-horizon "match" is definitionally
spurious). It composes with `main`'s existing `valid_shape` pre-pass.

**Why portable:** additive, opt-in (`None` → byte-identical behavior), fully
orthogonal to f32/rayon/hint. ~90 lines. No conflict with the divergent
architecture.

**diofinder relevance & caveats:**
- diofinder already has observer lat/lon in config (`polar.py`,
  `imu_solve_cal`). It would compute **LST** from longitude + UTC (standard
  GMST formula) and pass `observer_latitude` + `observer_lst`.
- **Enable only when the clock is trusted** (NTP/RTC): a wrong UTC rotates the
  zenith and mis-rejects valid solves. Opt-in + a negative
  `min_boresight_altitude` margin bound the risk.
- **Epoch:** zenith is JNow (Earth rotation), the solve is J2000; precession
  (<~0.4° over decades) is negligible against a horizon-scale cut — keep a
  margin.
- **Value is concentrated on the BLIND path** (acquisition / lost-in-space /
  post-slew reacquire) and on robustness. In steady-state **hinted** tracking
  the IMU cone already constrains the search, so horizon rejection is largely
  redundant there — same shape as the early-rejection pre-pass.
- Validate on a real corpus (`replay_corpus.py` / `diag_solve.py --bundle`)
  with correct lat/LST: solve **rate** must not drop with it enabled.

### 2. Fused IMU solver + BNO055/MPU support — **LOW for diofinder, optional for the fork**
`v2`'s `olive-imu` now includes `bno055.rs` (diofinder's sensor) and a
`FusedSolver` root crate. But the conclusion from the 2026-06-27 olive-imu
revisit holds and strengthens: diofinder already has a **mature Python BNO055
stack** (`imu_proc` reader, `imu_frame` Kabsch fit, exact-quaternion LX200
prediction, calibration, kill switches). `FusedSolver` is a **different Rust
architecture with less gating** that **doesn't even expose `attitude_hint`** —
adopting it means a major rewrite and an API regression. **Skip for diofinder.**
The fork could carry it only to serve a different (Rust-native fused finder)
consumer; that is out of scope for diofinder's needs and a large maintenance
surface.

### 3. Extractor unification (deleted `fast_extractor_seq`) — **LOW; maintainability only**
Resolves the parallel/sequential duplication flagged in the efficiency
assessment, but diofinder extracts with **sycamore**, not olive's extractor
(only the non-default Legacy preset uses it). It conflicts with `main`'s
extractor. Skip for diofinder; the fork may adopt it independently if it intends
to keep maintaining the olive extractor.

### 4. Hot-loop perf (bitwise masks / hoisted math) — **LOW / incremental**
`main` already carries the `original` perf work plus rayon. These would conflict
with `main`'s structure and need benching to prove a win on top of
parallelism. Not worth the churn.

### 5. Batch solving / virtual crop / target-pixel-center — **niche**
Serves a batch/whole-image use case, not diofinder's single-frame live solve.

## Recommendation

1. **Do not merge `v2` wholesale** — it regresses f32 + rayon + hint/verify and
   carries a crate restructure that conflicts heavily with `main`.
2. **Port exactly one feature: horizon-based early rejection**, as opt-in
   `Option` fields onto `main`, validated on a real corpus with correct
   lat/LST. Then diofinder supplies `observer_latitude` + `observer_lst` (config
   lat/lon + UTC) to prune blind solves and reject sub-horizon false positives —
   gated behind a "clock trusted" check.
3. **Skip the rest for diofinder**; the fused-IMU/extractor/batch work is either
   redundant with diofinder's mature stacks or serves a different consumer, and
   would cost architecture regressions to take.
