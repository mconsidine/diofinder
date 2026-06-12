# Session Decision Record — sycamore-extract

**Date:** 2026-06-12 13:50 UTC (session spanned 2026-06-04 → 2026-06-12)
**Session name:** vigilant-wright (assigned branch slug `claude/vigilant-wright-d9ndN`)
**Session ID:** `018PPLNhiAv325425icnyf8g`
**Session URL:** https://claude.ai/code/session_018PPLNhiAv325425icnyf8g
**Branch:** `main` · **Version shipped:** v0.11.1 (`7ee0b8e`)

---

## Assessment (full hot-path review at 0.11.0)

No correctness errors found. Verified-good and left alone: release profile
(opt-level 3, fat LTO, codegen-units 1, panic=abort, target-scoped
`target-cpu=cortex-a53`); van Herk O(1)/px morphology; SAT O(1)/px
uniform_mean; spatial bg applied AFTER 2×2 binning; NEON threshold prefilter;
banded parallel scan + union-find blobs + parallel centroiding; GIL released;
cached+top-hat path zeroes row floors correctly; binned→full-res centroid
mapping bias-free.

## Decisions

- All perf refactors must be **byte-identical** to the previous output,
  proven by an in-repo `cargo test` parity suite (5 tests added: van Herk vs
  naive, scratch-reuse cleanliness, tiled transpose vs naive + roundtrip,
  fused top-hat vs 4-transpose reference, histogram median vs sort).
- Top-hat stage-level parallelism is impossible (each of the 6 stages consumes
  the previous full image); per-stage data parallelism is complete. Expect
  ~2.2–2.6× on 3 threads (memory-bandwidth bound).

## Actions (v0.11.1, commit `7ee0b8e`)

1. van Herk scratch reuse — per-thread `MorphScratch` via `for_each_init`
   (was 3 Vec allocs per row per pass).
2. Tiled 32×32 `transpose` + top-hat transpose fusion
   (minH→T→minH→maxH→T→maxH: 2 transposes instead of 4); final subtraction
   parallelised. Expected top_hat ≈100 ms → ~40–60 ms at bin=2.
3. `sigma` clamped to ≥ 0.5 in both entry points (σ≈0 caused candidate
   explosion; diofinder exposes a 0–20 range).
4. `block_percentile` tile median via 256-bin counting (no alloc/sort, same
   element selected).
5. CLAUDE.md: thread-pool decision updated for diofinder's 3-dedicated-core
   deployment; stale duplicate pre-0.9.0 gate-mode decision removed.

(This session also wrote `tests/ab_background.py` early on; an identical copy
already existed on a parallel session's branch and was merged from there.)

## Recommendations / outstanding

- Re-vendor the wheel ≥ 0.11.1 into diofinder and update devices, then
  benchmark top_hat with `tests/bench.py` on the Pi.
- Remaining (small) headroom, not taken: NEON min/max in morph loops,
  row_column in-place fusion, NEON bin2x2; SAT u32 safe below ~16.8 MP
  (debug_assert would be prudent).
