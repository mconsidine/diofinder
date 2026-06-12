# Session Decision Record — olive-solve

**Date:** 2026-06-12 13:50 UTC
**Session name:** vigilant-wright (assigned branch slug `claude/vigilant-wright-d9ndN`)
**Session ID:** `018PPLNhiAv325425icnyf8g`
**Session URL:** https://claude.ai/code/session_018PPLNhiAv325425icnyf8g
**Branch:** `main` (reviewed at `cd3790e`)

---

## Assessment (performance review for the Pi Zero 2W)

Verified-good: workspace release profile (fat LTO, codegen-units 1,
panic=abort, strip); GIL released via `py.detach` in `solve_from_centroids`;
pre-allocated `Scratchpads` pools throughout the solve path; timeout via a
condvar watchdog thread + cooperative cancel flag (no per-iteration
syscalls); the solver Mutex serializing solves is load-bearing for
diofinder's memory-safe `solve_centroids` pattern. **No errors found.**

Findings, ranked:
1. **The solver core is 100% single-threaded** (zero rayon in `solver.rs`):
   diofinder reserves 3 cores, extraction uses them ~5 ms, then the blind
   solve (up to the 1500 ms timeout) runs on one core. Est. 2–2.5× from
   parallelizing; hint solves already fast.
2. `target-cpu=cortex-a53` was applied only via diofinder's vendor-workflow
   RUSTFLAGS — local/native builds silently lost it.
3. f64 throughout (incl. `KdTree<f64,3>`): 2× memory traffic vs f32 on A53;
   tetra3rs proves the f32-with-f64-SVD recipe. Est. 20–40% on verification.
4. ~4,300 LOC of extractor code compiles into the wheel unused by diofinder
   (sycamore extracts) — binary size only.

## Actions

- **Implemented & pushed (`43d8df0`):** `.cargo/config.toml` pinning
  `target-cpu=cortex-a53`, scoped to the aarch64 target (host build scripts
  unaffected). Verified: `cargo build --release -p tetra3` clean.

## NOT implemented — design preserved for a future session

**#1 Parallel pattern search (deterministic).** Verified preconditions:
`Scratchpads::new(p_size)` is per-instance state usable per-thread;
`verify_and_build_solution` mutates `image_centroids_undist` only on the
success path (after the probability-gate `?`), so per-thread clones are
semantically equivalent. Design: enumerate (l,k,j,i) combinations in exact
sequential order; process in chunks (~32); within a chunk evaluate patterns
in parallel via `par_iter().map_init(|| (Scratchpads::new(p_size),
image_centroids_undist.clone()), ...)`; select the **minimum-index** success
in the chunk so the result is identical to the serial loop's first match;
check the existing `abort` AtomicBool per pattern and at chunk boundaries
(Timeout/Cancelled statuses preserved). rayon's default pool sizes from
`available_parallelism`, which respects the solver process's 3-CPU affinity.

**#3 f32 kd-tree.** Single build site (`KdTree::new` + `.add(&star.vec, i)`)
and a single query site (`within::<SquaredEuclidean>` with center vector +
`max_dist_sq + 1e-8`). Convert tree to `KdTree<f32,3>` at DB load and the
query point/radius to f32 (inflate radius ~x1.0002 to only over-include);
downstream math stays f64 via `star_table_flat` index lookups — no DB format
change, negligible accuracy impact vs match_radius scale. The full f64→f32
vector-math conversion was deliberately deferred (needs on-device solve
validation).

## Recommendations

- Implement #1 then #3 on a dev branch; validate with the repo's
  validate_solver tests + diofinder live solves before vendoring a new wheel.
- Optionally feature-gate the unused extractors out of the tetra3-py wheel.
