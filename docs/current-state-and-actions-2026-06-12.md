# Current State & Outstanding Actions

**Last verified: 2026-06-12 ~20:50 UTC** against live git state and the GitHub
releases/Actions APIs. Companion: `docs/technical-assessment.md` (rev 3, component
comparison). Earlier revisions of this document narrated the olive-solve release
problem and a longer outstanding list as they were diagnosed and worked; that
history is compressed into §4 — everything above it is the *present* state.

---

## 1. Deployed stack (all released, all merged)

| Component | Released | Notes |
|---|---|---|
| diofinder image | **v0.0.25** (green build) | from `olive`; seeing presets, hot-pixel mask, watchdog, auto-exposure ON, docs reconciled (`cccb6c8`) |
| star_detect (sycamore-extract) | **v0.12.0** | kernel_sigma, 2-D moment trail rejection, perimeter local noise, bin=4, block-grid cache — all live on-device |
| tetra3 (olive-solve) | **v0.1.2** (`tetra3-0.1.2-…aarch64.whl`) | built from reconciled `main` (former `noext`): parallel solver, extractor feature gate; the branch split is resolved |
| star database (astro_databases) | **v2026.06** | `cedar_solve_13deg.npz`, Gaia DR3+Hip G≤8.0, epoch 2026.0; deep `_mag85` assets pending the catalog (item 2 below) |
| CI runtimes | Node-24 action majors merged in all five repos | first post-bump runs still pending (item 5) |

Do not pin `SYCAMORE_TAG=v0.11.2` — that release's wheel assets were clobbered
during the 0.12.0 transition and it now serves no wheel.

## 2. Live to-do list (suggested order)

1. **On-device / on-sky verification batch** — the only real gate left. Flash
   v0.0.25, then one session: `tests/bench.py` p50 on test1–3 for **both** seeing
   presets (Bad exercises exactly the new non-default code paths); Good/Bad A/B on
   marginal frames; dark-frame hot-pixel capture; auto-exposure convergence;
   `bench_pipeline_combos.py --live-shm`; watchdog fire/restart; tune
   `efinder/seeing.py` preset values from the A/B data; run
   `scripts/calibrate_lens.py` on saved solved frames and set `distortion:`.
2. **Deep Gaia catalog (G≤9.0)** — in progress on the owner's Mac (runbook in the
   astro_databases README): download/merge, commit the `.bin` + `.dat.gz`, tag
   (e.g. `v2026.06.1`). CI then builds and attaches the `_mag85` deep databases.
3. **Deep-DB device plumbing** — not yet wired: the image build/`efinder-update`
   fetch only the standard `.npz`. Until the `_mag85.npz` is on the device **and**
   `star_db_deep` is set in `efinder.conf`, the Bad preset's database switch is a
   silent no-op. (Deferred by owner; Claude can wire it on request.)
4. **Branch cleanup** — `origin/hybrid` (30+ unmerged commits of the superseded
   cedar-detect-gRPC architecture; archive-tag first if the history should stay
   findable) and `origin/sycamore-only` (fully merged) still exist. Verified
   present at last check.
5. **Watch the first post-Node-24 workflow runs** in each repo. None have run
   yet (v0.0.25 was built before the bump merged). Artifact actions crossed
   multiple majors (v7/v8 in the four libraries, v5 in diofinder); if v7 upload
   semantics surprise, the fallback is a one-line downgrade to v5/v6.

## 3. Next-cycle improvements (deliberately deferred)

| Item | Why deferred |
|---|---|
| olive-solve f32 kd-tree / vector math (est. 20–40% verification speedup) | needs an on-device baseline from item 2.1 first |
| Calibrated-FOV DB regen (13.497° vs 10.5–14°) + retire the "13deg" label | batch into the next astro_databases release (natural fit: item 2.2's tag) |
| tetra3rs cibuildwheel→maturin migration | cibuildwheel v4 was deliberately *not* taken in the Node bump (changes wheel-repair defaults) — make it one deliberate migration |
| olive-solve gRPC `parallel` proto field | minor; server path unused by diofinder |
| docs/decisions naming convention (`YYYY-MM-DD-<session>-<repo>.md`) | housekeeping; exact duplicates already removed |

## 4. Resolved history (2026-06-12, compressed)

- **olive-solve v0.1.2 saga**: `release-wheels.yml` lived only on `olive-solve-noext`
  while `main` (the default branch) had none — so the Actions "Run workflow" button
  never appeared, and a v0.1.2 tag on a stale main commit built nothing. Fixed by
  merging noext↔main (PRs #7–#11), a workspace version bump, and — the last trap —
  bumping `tetra3-py/pyproject.toml`, which is what maturin actually names the
  wheel from (the first two release runs produced `tetra3-0.1.0-…whl` from
  correct code). Lesson recorded: version lives in Cargo.toml(s) **and**
  pyproject.toml; grep both before tagging.
- **sycamore 0.12.0**: was merged but untagged (and a 0.12.0 wheel briefly sat
  inside the v0.11.2 release); proper v0.12.0 release cut, mislabeled asset removed.
- Closed items from the original audit: image rebuild (v0.0.25), Node-24 bumps
  (all five repos, before the 2026-06-16 deadline), `tests/README.md` `--solve`
  documentation, docs/decisions byte-identical duplicate removal, README/TODO/
  CLAUDE.md/conf.default reconciliation against shipped code.

## 5. Superseded architectures (do not resurrect)

1. **`hybrid`** (cedar-detect gRPC + olive-solve) — superseded by `olive`
   (sycamore + olive-solve); all cedar-detect references were removed from the
   shipped line. Pending deletion (item 2.4).
2. **testrepo aggregator CI** — dismantled in favor of per-repo release workflows;
   decision records referencing testrepo describe a dead pipeline.
3. **cedar-solve / cedar-detect as runtime components** — reference
   implementations and database generators only.
