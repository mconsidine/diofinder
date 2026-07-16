# Review of the three efficiency assessments (diofinder / olive-solve / sycamore-extract)

**Date:** 2026-07-15
**Author:** review of a different LLM's three "efficiency assessment" documents,
evaluated against the actual code in this workspace.
**Scope:** judges the *recommendations for improvement* in each document — are they
sound, valuable, redundant, or wrong — for diofinder's real deployment.

## Headline

All three documents are **descriptively strong and accurate**. The olive-solve
one was spot-checked against current `main` and matches the code (it is *not*
branch-confused: `main` genuinely advanced to **v0.1.7** and now contains the
early-rejection pre-pass, Cramer's 2×2, ImmutableKdTree, and lazy verification —
commit `e0d5326`). The "areas not assessed" sections are honest.

**The recommendations are the weakest part of all three.** They skew toward
low-impact micro-optimizations and maintainability refactors mislabeled as
"efficiency," they were written in repo-isolation (so priorities don't match
diofinder's integrated pipeline), and one is likely backwards.

## The cross-repo picture the assessments don't connect (but no action pending)

olive-solve shipped **v0.1.6** (2026-07-04, `verify_attitude`, a true verify-only
entry point) and **v0.1.7** (2026-07-05, `e0d5326`: SVD pre-pass, reciprocal
multiply, Cramer's rule, ImmutableKdTree, lazy verification). This is the perf
work flagged in `2026-06-27-olive-solve-original-branch-assessment.md` — it has
since been ported from the `original` branch onto `main` upstream, so that doc's
"port the early-rejection pre-pass" recommendation is now **overtaken by events**.

**diofinder already runs v0.1.7.** It vendors no wheel (`vendor/wheels/` empty)
and pins no version — the release pipeline resolves to *latest* (there are no
`OLIVE_SOLVE_TAG`/repo variables set), and the code already capability-probes
`verify_attitude` (`solver_proc.py:1047`, `SOLVER_HAS_VERIFY`) and
`detect_stars_roi`. So the solver perf gains **and** true verify-only tracking
are already live — no wheel refresh or code change is pending.

The point for *this* review is only that each assessment, scoped to one repo,
doesn't note that diofinder's solver efficiency is governed by the (already
current) olive-solve wheel, not by anything in diofinder's own Python. Confirm
the running versions with `diofinder-ctl version` (the `wheels` key) if ever in
doubt, but nothing here is an open item.

## diofinder recommendations (§8)

| Rec | Verdict | Why |
|-----|---------|-----|
| 8.1 Split `comms_proc.py` (AE + maint dispatch) | Reasonable, low priority | Real 2,884-line monolith, but *maintainability*, not efficiency ("no runtime change"). AE decision is already a pure tested function, so extraction is cheap. |
| 8.2 IMU → raw SHM | Over-valued + factual error | Premise real (`shared_cfg` is a Manager dict), but claims IMU runs at **40 Hz — it's 20 Hz** (`imu_proc.py:_POLL_HZ=20`), one atomic tuple write per read since v0.11.24. I/O core is far from saturated; win is sub-ms. Not worth the torn-read/versioning complexity. |
| 8.3 Solver snapshot → targeted `.get()`s | **Likely backwards** | For a Manager dict, `dict(shared_cfg)` is **one** IPC round-trip; 10–15 individual `.get()`s are **10–15** round-trips. The team already went the other way — audit **P2** (`solver_proc.py:820`) trimmed payload *out* of `shared_cfg` to keep the single snapshot cheap. This rec fights that. **Reject.** |
| 8.4 Consolidate capability probes | Trivial, fine | Pure maintainability/diagnostics. |
| 8.5 In-memory capture-dir cap | Niche | Only runs when diagnostic frame-saving is on; adds a state-vs-disk desync risk. Marginal. |

## olive-solve recommendations (§9)

The valuable perf work already landed in v0.1.7, so the remainder is mostly
library hygiene with **low value to diofinder specifically**:

| Rec | Verdict for diofinder |
|-----|----------------------|
| 1. NEON downsampling/erosion (extractor) | Low — that's olive-solve's *extractor*, which diofinder runs only in the non-default Legacy preset; its real extractor is sycamore. Speculative "2–4×." |
| 2. Unify `fast_extractor` / `_seq` | Low — maintainability on extractor code diofinder mostly doesn't run. |
| 3. mmap DB loading | Low — their own text says hip_main is 5–15 MB; the solve touches the whole DB anyway. |
| 4. Incremental hash table | Speculative. |
| 5. **Unit tests** (`fast_binomial_cdf`, `geodesic_angle_deg`) | **Genuinely good** — real gap. |
| 6. gRPC concurrent bench | Irrelevant — diofinder loads the solver in-process, no gRPC. |
| 7–8. sorting network / `get_unchecked` | Correctly self-deprioritized. |

## sycamore-extract recommendations (§10)

| Rec | Verdict |
|-----|---------|
| 1. **Pre-allocate per-frame buffers (`DetectBuffers`)** | **Best rec in all three docs** — real ~0.3–0.5 ms of a ~6 ms budget (~5–8%), on the actual hot path. Worth doing. |
| 2. Cache the matched kernel | Correct but tiny (~2 µs); cheap, do opportunistically. |
| 3. NEON for the matched-filter dot product | **Misprioritized as "High"** — `gate_1d` runs on <1% of pixels (the byte prefilter culls ~99%, and *that* per-pixel scan already has NEON). Optimizes a near-nothing path. |
| 4. Parallelize `estimate_noise` | Minor — skipped entirely in the steady-state cached path. |
| 5. Combine copies in cached path | Minor (~0.1–0.2 ms). |
| 6. `bg_mode` string alloc | Trivial (~100 ns). |
| 7. Refactor `detect_stars_with_cache` | Reasonable maintainability. |
| 8–9. fixed-point interp / parallelize `bin2x2` | Low. |
| 10. **Python-level detection test in CI** | **Genuinely good** — CI only does `cargo test` + smoke import; real regression gap. |

## Cross-cutting critique

1. **"Efficiency" that's really maintainability.** Split comms_proc, unify
   extractors, refactor `detect_stars_with_cache`, consolidate capability probes
   — all explicitly "no runtime change." Fine work, wrong label.
2. **Repo-isolation distorts priority.** Acting on olive-solve's "High Priority"
   NEON-extractor list optimizes code diofinder runs only in Legacy mode; the
   gRPC rec is moot in-process. The one cross-repo action that matters (bump the
   wheel) is invisible to all three.
3. **One backwards rec (diofinder 8.3)** and **one factual slip (40 Hz vs
   20 Hz)** — minor alone, but they'd waste effort if taken at face value.
4. **The real bottleneck is under-served.** On the Pi the dominant costs are the
   per-frame image copy + extraction and the solve loop. Only sycamore #1
   (buffer reuse) targets that; the rest is nibbling.

## What to action, in order

Note: the olive-solve solver perf work + verify-only tracking are **already live**
(v0.1.7 wheel, latest-tracked) — nothing to do there.

1. **sycamore #1** — pre-allocated `DetectBuffers`. The best genuine perf rec
   (~0.3–0.5 ms of a ~6 ms budget). Validate detection parity on a real burst.
2. **sycamore #10 + olive-solve #5** — the two CI/unit-test gaps.
3. **diofinder 8.1** — split comms_proc, maintainability only, when convenient.
4. **Reject diofinder 8.3**; treat 8.2 as low priority; ignore the olive-solve
   extractor/gRPC recs for diofinder's purposes.
