# olive-solve `original` branch — assessment for diofinder

**Date:** 2026-06-27
**Question:** Assess the changes on the `original` branch of `mconsidine/olive-solve`
and evaluate whether diofinder could benefit from any of them versus the branch
diofinder currently uses (`main`).

## TL;DR

- diofinder consumes olive-solve as its **plate solver**:
  `solver_proc.py` calls `tetra3.Tetra3.solve_from_centroids` (the Rust
  `tetra3/src/solver.rs` engine), so changes to `solver.rs` land directly in the
  live solve path. The release pipeline pulls the wheel from olive-solve's
  releases, which are cut from **`main`**.
- `original` is **not** a newer version of `main`. The two branches **diverged**
  from a common merge-base (`452602a`) and took **different, partially
  conflicting** optimization paths over the *same* hot code. `original` is
  3 perf commits past the merge-base; `main` is ~35 commits past it.
- **Adopting `original` wholesale is a regression**: it lacks the
  `attitude_hint` / `hint_uncertainty_deg` / `strict_hint` / blind-fallback API
  that `main` added and that diofinder's **tracking mode and IMU-hint solving
  actively depend on**. It also lacks the f32 catalog/KdTree memory work and the
  rayon multi-core parallelism `main` ships.
- **The realistic benefit is selectively *porting* a few of `original`'s
  algorithmic optimizations onto `main`.** The single highest-value candidate is
  the **early-rejection SVD pre-pass**. A handful of micro-opts are low-risk but
  low-reward given `main` already parallelizes. The memory-layout work
  (probe table / u32 packing) is largely **superseded** by `main`'s f32 choice.

## How the branches relate

```
                 452602a  (merge-base: "block median ... python fast path")
                /        \
   original (+3)          main (+~35)  ──► releases ──► diofinder wheel
   42145c4                ab29239 (v0.1.5)
```

`original`-only commits:

| SHA | Summary |
|-----|---------|
| `a1a479c` | "Comprehensive native optimization suite for Pi Zero 2W" |
| `d965fd5` | "Massive performance optimizations to pattern generation and matching" |
| `42145c4` | Docs/comments + compiler-warning fixes |

Both branches rewrote `tetra3/src/solver.rs` heavily (original: ~780 changed
lines; main: ~836), in the same functions (`verify_and_build_solution`, the hash
probe, the combinatorics loop). They do **not** cherry-pick cleanly onto each
other.

## What `main` already has (and `original` does NOT)

These are the strategically important wins, already in diofinder's solver:

1. **f32 resident catalog + KdTree** (`CatalogStar.vec: [f32;3]`, `KdTree<f32,3>`,
   f32 verification scratch). Halves the resident catalog/KdTree footprint and
   the dominant neighbor-query bandwidth on the 512 MB / memory-bandwidth-bound
   A53 — the documented Pi bottleneck. `original` kept everything **f64**.
2. **rayon multi-core candidate search** (`par_chunks(...).find_map_first(...)`,
   gated by `options.parallel`). Uses diofinder's three dedicated solver cores
   (CPUs 1–3). `original` is **single-threaded**.
3. **Attitude-hint / blind-fallback API** (`attitude_hint`, `hint_uncertainty_deg`,
   `strict_hint`, blind fallback when a hint matches nothing). diofinder's
   `tracking.py` (tight 2° cone, `strict_hint=True`) and IMU-propagated hint
   solves **require** this. Absent on `original`.
4. cortex-a53 `target-cpu` pinning (also present via the build config).

Because of (3), **diofinder cannot move to `original` without losing tracking
and hinted solves.** That alone rules out a branch switch.

## What `original` has that `main` does NOT — port candidates

Ranked by value-to-diofinder, with risk and portability notes. (All references
are to `tetra3/src/solver.rs`.)

### 1. Early-rejection SVD pre-pass — **highest value, port with validation**
Before calling the expensive `verify_and_build_solution` (KdTree query +
project + match) on a 4-star candidate, do a cheap 3×3 dot-product check: rotate
the 4 catalog pattern vectors by the candidate rotation and reject if any lands
> `(match_radius * fov * 2.0)` from its image vector. Kills geometric
false-positives before the heavy verify. This is **algorithmic, independent of
f32/rayon**, and is the most likely real speedup.
- **Risk:** the rejection threshold is heuristic (`* 2.0` fudge factor). Too
  tight → real solves rejected → lower solve *rate*. Must be validated on-sky /
  against the bundle corpus (`diag_solve.py --bundle`, `replay_corpus.py`)
  before shipping, watching solve rate, not just solve time.
- **Port effort:** moderate — must use `main`'s per-worker scratch (the parallel
  structure), not `original`'s shared `scratch`.

### 2. Lazy / streaming verification with early break — **good value**
In `verify_and_build_solution`, `original` streams nearby catalog stars,
projects them one at a time, and **breaks at `2*num_extracted` kept stars**
instead of projecting *all* nearby stars then cropping. Avoids ~thousands of
redundant trig/projection ops in dense fields. `main` still projects the full
nearby set then crops.
- **Risk:** low-moderate; relies on catalog being brightness-sorted (it is) and
  on the within-radius set being index-sorted first.
- **Port effort:** moderate — `main`'s version is f32 and structured
  differently; re-express the streaming loop in f32.

### 3. Allocation-free verify (extra persistent scratchpads) — **modest, low risk**
`original` adds scratchpad fields (`sp_matched_stars`, `sp_matched_img_cents`,
`sp_distances`, …) so `verify_and_build_solution` does **no** per-call heap
allocation. `main` still does `Vec::with_capacity` / `vec![...]` there. Removes
allocator hits on every verify.
- **Caveat:** under `main`'s rayon path these must be per-worker, not shared.

### 4. Direct 2×2 Cramer's-rule distortion refine — **modest, low risk**
Replaces the nalgebra `SVD::pseudo_inverse` used for the 2-parameter
focal-length/distortion least-squares with closed-form normal-equations on a 2×2
(`ata_00..atb_1`). Faster; for a well-conditioned 2-DOF fit the result is
equivalent. `main` still calls `SVD::new(...).pseudo_inverse(1e-7)` here.
- **Risk:** low (normal equations are less stable than SVD in general, but fine
  for this tiny well-scaled system; keep the `det.abs() > 1e-12` guard).

### 5. Micro-ops: sorting networks, reciprocal multiply, unrolled edge compare — **low value on `main`**
Fixed 12-comparator sorting network for the 6 edges, multiply-by-reciprocal
instead of divide, unrolled 5-edge ratio comparison, loop-invariant hoisting of
hash-key diffs. All low-risk and portable, but the per-iteration cost they trim
is exactly what `main` already **spreads across 3 cores** via rayon, so the
marginal wall-clock win is smaller than on `original`'s single-threaded loop.
Bench before bothering.

### 6. Probe table + u32 pattern catalog — **largely superseded, low priority**
`original` packs the hash-probe lookup into a dense 4-byte `probe_table` and
converts `pattern_catalog_flat` from `usize`→`u32` for memory bandwidth. Sound
idea, but `main` already attacked memory bandwidth via the **f32 catalog**
(arguably the bigger lever). The u32 packing is orthogonal and *could* compose,
but adds real complexity to the probe/hash path and would need measurement to
justify. `main` keeps `pattern_catalog_flat: Vec<usize>`, so there's an easy
isolated win here (usize→u32 halves that array) if a bench shows it matters.

### 7. Watchdog-thread removal → inline timeout — **optional simplification**
`original` deletes the `Condvar`/`Mutex` watchdog thread and checks
`solve_timeout_ms` inline every 100 combinations. Removes a background thread +
lock. `main` (and the merge-base) keep the watchdog thread. Note diofinder
**also** has a comms-side watchdog (`comms_proc._watchdog_loop`, `os._exit(1)`),
so the in-solver thread is somewhat redundant. Low-risk simplification, but
purely incremental; not a reason to act on its own.

## Risks / caveats about `original` itself

- The three commits read as **AI-generated, unbenchmarked-in-repo** ("massive
  wave", "comprehensive suite"); the only test change (`validate_solver.rs`)
  just loops the same 738 samples 100× for timing, adding **no new correctness
  assertion**. The "< 0.30 ms" claim is not substantiated by a committed bench.
- They were apparently **not merged into `main`** — `main`'s author instead
  pursued f32 + rayon. Treat that as a signal that the maintainer chose a
  different strategy, not that these were endorsed.
- Aggressive changes carry **solve-rate** (not just solve-time) risk — the
  early-rejection threshold and the normal-equations refine especially. Any port
  must be validated for match rate, not only latency.

## Recommendation

1. **Do not switch diofinder's olive-solve dependency to `original`.** It would
   regress the hint/tracking API and drop the f32 + rayon wins.
2. **If solver latency on the Pi Zero 2W is a live pain point**, open a focused
   olive-solve PR onto `main` that ports **only** the early-rejection SVD
   pre-pass (#1), and measure it with `replay_corpus.py` / `diag_solve.py
   --bundle` on a real burst corpus, gating on **solve rate AND median
   solve_ms**. Add streaming verify (#2) and allocation-free verify (#3) if the
   pre-pass validates cleanly.
3. **Cheap, isolated wins** that don't depend on the above: `pattern_catalog_flat`
   `usize`→`u32` (#6, half the array) and Cramer's 2×2 refine (#4) — both small,
   both bench-gated.
4. **Skip** the watchdog-thread removal and the micro-op sorting networks unless
   a profile shows them mattering on top of rayon; they're not worth the churn.

Net: there is **selective, real value** in `original` — chiefly the
early-rejection pre-pass — but it must be **ported onto `main`**, re-expressed in
`main`'s f32 + per-worker-scratch parallel structure, and validated for solve
rate. There is **no value** in adopting the branch as-is.

---

## Revisit 2026-07-15 — the `original` branch grew (9 most-recent commits)

Since the original assessment, `mconsidine/olive-solve` `original` advanced past
the 3 perf commits. The 9 most-recent commits (HEAD `235a5d9`) are:

| Commit | Theme |
|--------|-------|
| `a1a479c` Comprehensive native optimization suite | perf (solver.rs) — assessed above |
| `d965fd5` Massive perf optimizations | perf (solver.rs) — assessed above |
| `42145c4` Docs: optimization comments + warnings | perf docs — assessed above |
| `b182ace` Docs: optimization comments + warnings | **duplicate** of `42145c4` (via the oakamil merge) |
| `b8ee8ee` Create astronomical IMU implementation | **new `olive-imu` crate** |
| `ebfb878` IMU improvements | olive-imu |
| `615c0a4` Merge `oakamil/olive-solve` main | brings oakamil's line in |
| `d476ea3` Improve IMU hardware sync & timing | olive-imu |
| `235a5d9` Include olive-imu in README | docs |

The **only new substance** vs. the assessment above is a standalone
**`olive-imu`** Rust crate (~1,900 LOC, author Omair Kamil/oakamil, absent from
`main`): I2C drivers for **BMI160 + BNO085**, real-time SVD camera↔IMU frame
alignment, continuous gyro-bias compensation, I2C timing synchronization, and a
100 Hz+ async (`tokio`) polling thread. The perf commits (and the duplicate docs
commit) are unchanged from what's assessed above.

### Verdict on `olive-imu` for diofinder: **skip**

- **Wrong sensor.** olive-imu drives BMI160 (raw 6-axis, no fusion) and BNO085.
  diofinder is committed to the **BNO055** (`imu_proc.py`), which fuses on-chip
  and returns a ready quaternion. olive-imu supports neither BNO055 specifically;
  its **continuous bias compensation** exists precisely because a raw IMU
  (BMI160) has no onboard fusion — moot when the chip already fuses in hardware.
- **Redundant with more mature, astronomy-specific equivalents.** olive-imu's
  "real-time SVD alignment" is the same math as diofinder's **Kabsch fit**
  (`imu_frame.py`), but diofinder's is quality-gated (≥4 magnitude-consistent
  pairs, axis diversity ≥0.25, R²≥0.9, refuses unobservable alt-only slews) and
  feeds a tear-proof `imu_ref` + exact-quaternion LX200 prediction with kill
  switches. diofinder's is field-hardened; olive-imu's is generic and new.
- **Wrong architecture/language.** Standalone Rust + `tokio` crate, **not** part
  of the `tetra3` wheel diofinder consumes, no Python bindings. Adopting it means
  PyO3 packaging + rewriting the comms-process IMU integration (shared_cfg
  publishing, calibration loop, LX200 path). High cost, negative net.

### One borrowable idea (low value): IMU timestamp back-dating

olive-imu's headline timing trick is back-dating timestamps to map an
I2C-jittered read to the true measurement instant. diofinder stamps samples with
a simple pre-read `time.monotonic()` and no back-dating (`imu_proc.py:185-189`),
so it has the same imperfection in principle. **But the payoff is small:**
diofinder polls at **20 Hz (50 ms granularity)**, which dominates the ~1 ms I2C
jitter, so back-dating buys little without also raising the poll rate (a
deliberate 20 Hz choice, BNO055 on CPU 0); and the main consumer — the Kabsch
fit over *inter-solve* intervals (seconds) — is insensitive to a 1 ms stamp
error. It would only marginally affect fast-slew LX200 prediction. Not worth
acting on unless the IMU rate is later raised for slew pointing.

### Net (revisit)

Nothing in the 9 commits changes the earlier conclusion. The perf commits remain
"port the early-rejection pre-pass only, if solver latency becomes a pain." The
new `olive-imu` crate is a **skip** for diofinder — wrong sensor, redundant with
a more mature in-house stack, architecturally expensive — with only IMU
timestamp back-dating as a minor, low-priority idea.
