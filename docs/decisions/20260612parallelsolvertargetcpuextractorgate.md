# 2026-06-12 — Parallel solver core, target-cpu config, extractor feature gate

**Session:** funny-noether (`claude/funny-noether-lro1ig`)
**Session ID:** `session_01216HrQG6gvzvjiiAqZQUux` — https://claude.ai/code/session_01216HrQG6gvzvjiiAqZQUux
**Continues:** `session_018PPLNhiAv325425icnyf8g` (whose 2026-06-10 handoff contained the olive-solve performance review this session implemented)
**Base commit:** `cd3790e` (main). Work requested as "#2, then #1, then #3" from the review's ranked findings.

## Decisions

1. **Committed `.cargo/config.toml`** with `target-cpu=cortex-a53`, scoped to
   `[target.aarch64-unknown-linux-gnu]` only (identical to sycamore-extract's,
   including the rationale comment). Previously the flag existed only in
   diofinder's vendor-workflow RUSTFLAGS, so local Pi builds silently lost it.
2. **Parallelized the pattern-candidate search** (the solver was 100%
   single-threaded while diofinder reserves 3 cores for it):
   - The 4-star l/k/j/i combination body is factored into `try_pattern_combo()`
     + an immutable `ComboContext`, shared by serial and parallel paths.
   - Parallel path materializes combinations in exact breadth-first serial
     order, chunks them, and scans with rayon `find_map_first` — the
     **leftmost verified solution wins**, so results are deterministic and
     identical to the serial search. Chunks left of a found match run to
     completion to preserve this; chunk size is capped (4–64) to bound tail
     latency.
   - Each worker carries private `Scratchpads` and undistorted-centroid buffer
     (the distortion branch of verification refines the latter).
   - All workers poll the same watchdog/cancel `AtomicBool`; timeout and
     `cancel_solve()` semantics unchanged.
   - `SolveOptions.parallel: bool` (default **true**), exposed as the
     `parallel` kwarg in Python; serial path (also used on 1-thread pools)
     retains the original zero-allocation behavior using the instance
     scratchpads.
3. **`extractor` cargo feature, default ON**, in both `tetra3` and `tetra3-py`:
   `--no-default-features` drops extractor.rs/fast_extractor*.rs (~4,300 LOC),
   the extraction methods of `Tetra3`, and the extraction Python methods.
   Solver API unaffected; gRPC server unaffected (uses default features).
4. `[profile.test] opt-level = 3` so the fixture-driven solver tests run in
   under a second.

## Assessments

- **Speedup:** full-enumeration NoMatch worst case, 4 x86 threads:
  55.1 ms → 15.3 ms per solve (**3.61×**). Expected ~2.2–2.7× on the Pi Zero
  2W's 3 solver cores (memory-bandwidth bound).
- **Parity:** new `tetra3/tests/parallel_parity.rs` proves serial and parallel
  paths bit-identical (every Solution field) across all 738 committed solver
  fixtures. `validate_solver` also passes with the parallel default.
- **Pre-existing issues found:** (a) `main` did not compile — the
  attitude-hint fields broke `server/src/lib.rs` and `validate_solver.rs`
  struct literals; fixed via `..def` / `..Default::default()`.
  (b) `test_extraction_u8_sanity` / `test_extraction_against_python_sanity`
  fail identically on unmodified main (fixture/crop issue, unrelated).
- **Wheel size** (aarch64 abi3, cross-built): 808 K full vs 704 K solver-only.

## Actions

- Three commits authored on `claude/funny-noether-lro1ig` (this container's
  clone). Direct push was blocked — the repo was outside the session's
  authorized scope — so the commits were delivered as `git format-patch`
  files + a bundle; **mconsidine applied them onto `olive-solve-noext`**
  (`0176bf7`, `7229e18`, `75cede0`, on top of the PR #3 merge `10bc2f6`).
- The applied branch was then verified in-session: both feature configs
  compile and all solver/parity tests pass on that base, and the aarch64
  cross-build succeeds (full and `--no-default-features`).

## Recommendations

- Merge `olive-solve-noext` → `main` to land PR #3 + this work together.
- On-Pi measurement:
  `cargo test -p tetra3 --release --test parallel_parity -- --ignored --nocapture`
- Future (from the same review, not yet done): f32 conversion of the kd-tree /
  vector math (est. 20–40% on verification, medium-large effort); a `parallel`
  field in the gRPC proto if server callers ever need to force serial.
- diofinder's vendor build can use the solver-only wheel
  (`SOLVER_ONLY=1` in its `build/local/build-olive-wheel.sh`) once this is on
  the vendored ref.
