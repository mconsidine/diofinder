# Current State & Outstanding Actions — 2026-06-12

**Inputs:** all 36 records in `docs/decisions/` (deduplicated to ~22 distinct), verified
against live git state of diofinder, olive-solve, sycamore-extract, tetra3rs,
astro_databases, and the GitHub releases API. Companion document:
`docs/technical-assessment.md` (updated component comparison).

---

## 1. olive-solve: the v0.1.2 release problem — diagnosis and runbook

### Why "Run workflow" doesn't appear in Actions

`release-wheels.yml` exists **only on `olive-solve-noext`** — `main` has no
`.github/workflows/` at all. GitHub's Actions UI only offers the **Run workflow**
button for workflows that exist on the repository's **default branch** (`main`).
That is the entire reason the button is missing online.

### Why the v0.1.2 tag built nothing

The existing `v0.1.2` tag points at `765300a` — a docs commit on **main** — where
`release-wheels.yml` does not exist. Tag-push triggers run the workflow file *at the
tagged commit*, so nothing ran. Verified via the GitHub API: **no v0.1.2 release
object exists**; v0.1.1 ("Olive solve fork noext", built from noext) is still the
latest non-prerelease.

That last fact is good news: diofinder's `release.yml` resolves
`inputs.olive_solve_tag || vars.OLIVE_SOLVE_TAG || latest-non-prerelease`, so default
image builds are **currently still pulling the correct noext wheel (v0.1.1)**. But the
moment a v0.1.2 release is created from main, it would supersede v0.1.1 as "latest"
and default builds would silently lose the noext work (parallel solver, extractor
feature gate). Fix the tag before building anything.

### What's already prepared

Branch `claude/beautiful-ritchie-iqyu8w` on olive-solve (commit `8ff2f3b`, based
directly on `olive-solve-noext`) bumps tetra3 / tetra3-py / tetra3-server +
Cargo.lock to **0.1.2** — noext still said 0.1.0, so a release built from it would
have produced a wheel whose version contradicts the tag.

### Runbook (Option A — recommended, no change to main needed)

```bash
# 1. Merge the version bump into noext (PR or fast-forward):
#    claude/beautiful-ritchie-iqyu8w -> olive-solve-noext

# 2. Re-point the tag at the noext head and push it:
git fetch origin
git tag -f v0.1.2 origin/olive-solve-noext
git push origin :refs/tags/v0.1.2     # delete the stale tag
git push origin v0.1.2                # tag push triggers release-wheels.yml

# 3. Nothing else: v0.1.2 (noext) becomes latest non-prerelease;
#    diofinder default builds pick it up. Optionally pin
#    OLIVE_SOLVE_TAG=v0.1.2 in diofinder repo variables for determinism.
```

### Option B — make the UI button work

Either copy the workflow to main
(`git checkout main && git checkout olive-solve-noext -- .github/workflows/release-wheels.yml && commit/push`)
and then use **Run workflow** with branch `olive-solve-noext` and version `v0.1.2` — or,
cleaner and consistent with the pensive-allen decision that *noext is the branch of
record*: **change the repository default branch to `olive-solve-noext`** in GitHub
settings. That one click makes the Actions button appear *and* makes the repo's
landing view match reality.

### Longer term (unresolved contradiction C3 from the decision records)

pensive-allen declared noext the branch of record, yet main has since drifted
*forward* independently (its own `target-cpu` commit `43d8df0`, the v0.1.2 tag).
The branches now diverge in **both** directions. Pick one: (a) merge noext → main and
retire the split, or (b) make noext the default branch and treat main as the upstream-
tracking branch. Either ends the class of error that produced the dead v0.1.2 tag.

---

## 2. Master list of outstanding items (verified against git, by repo)

### olive-solve
| # | Item | Status / evidence |
|---|---|---|
| O1 | v0.1.2 release from noext | **OPEN — runbook above.** Version bump ready on `claude/beautiful-ritchie-iqyu8w` (`8ff2f3b`). |
| O2 | Reconcile noext ↔ main (or switch default branch) | **OPEN.** Diverged both directions; `release-wheels.yml`, parallel solver (`7229e18`), extractor feature (`75cede0`), prerelease guard (`ac57e4b`) are noext-only. |
| O3 | f32 kd-tree / f32 vector math (est. 20–40% verification speedup) | **OPEN — deliberately deferred** (vigilant-wright, funny-noether). Needs on-device solve validation. |
| O4 | gRPC `parallel` field in server proto | **OPEN** (minor; server got only a struct-literal fix on noext). |

### sycamore-extract
| # | Item | Status / evidence |
|---|---|---|
| S1 | **Tag/release v0.12.0** | **OPEN — high priority.** The 0.12.0 seeing-robustness work (kernel_sigma, 2-D moments, perimeter local noise, bin=4, block cache) is merged to main (`86bd736`) but the highest tag is v0.11.2. Until released, diofinder's Bad-seeing preset runs with kernel_sigma / local_noise / block-cache **inert** (capability probing degrades them silently). |
| S2 | On-Pi bench (`tests/bench.py` on test1–3, p50 targets) for 0.12.0 | **OPEN** — all verification so far was x86 (bit-identity vs 0.11.2 on synthetic frames). |

### diofinder
| # | Item | Status / evidence |
|---|---|---|
| D1 | On-device verification batch | **OPEN.** Accumulated across sessions: bin=2 solve-rate parity, 3-core solver speedup (`bench_pipeline_combos.py --live-shm`), auto-exposure convergence, governor persistence, top_hat timing, seeing-preset A/B (Good vs Bad on marginal frames), hot-pixel dark-capture workflow, watchdog fire/restart. |
| D2 | Rebuild image after O1+S1 land | **OPEN.** Current image v0.0.24 carries star_detect 0.11.2 + tetra3 0.1.0. After olive-solve v0.1.2(noext) and sycamore v0.12.0 exist, cut v0.0.25 (pins optional: `OLIVE_SOLVE_TAG`, `SYCAMORE_TAG`). |
| D3 | Delete/archive the `hybrid` branch | **OPEN.** 30+ unmerged commits of the superseded cedar-detect-gRPC architecture; contradicts the shipped `olive` line (C1). Tag it `archive/hybrid` if you want the history findable, then delete. `sycamore-only` is fully merged — safe to delete. |
| D4 | GitHub Actions Node 24 bump | **OPEN — deadline 2026-06-16 (4 days).** Runners drop Node 20; bump checkout/setup-python/upload-artifact majors across workflows (also applies to the other repos' workflows). |
| D5 | `tests/README.md`: document `diag_background.py --solve` | **OPEN** (sharp-goodall deferred item). |
| D6 | docs/decisions housekeeping | **SUGGESTED.** 36 files → ~22 distinct; 14 are exact duplicates (" (1).md" copies and unprefixed twins). A dedupe pass + a naming convention (`YYYY-MM-DD-<session>-<repo>.md`) would keep this folder auditable. Not done here — they're your curated records. |

### astro_databases / tetra3rs
| # | Item | Status / evidence |
|---|---|---|
| A1 | Deep G≤8.5 variant | **DONE & RELEASED** — in `v2026.06` (tag == main HEAD). Remaining sub-item: the deep build still needs the G≤9.0 source catalog generated once (network step documented in its README); until then CI skips the deep assets gracefully. |
| A2 | Regenerate DBs at calibrated FOV (13.497° vs 10.5–14°) | **OPEN** (tetra-hybrid session rec). Worth folding into the next DB release; tightens pattern density around the real FOV. |
| A3 | Rename "13deg" label to match true FOV range | **OPEN, cosmetic** (touches install scripts + workflows; batch with A2). |
| T1 | tetra3rs pi-wheels off cibuildwheel/QEMU → maturin | **OPEN, deferred.** PyPI-owner guard, prerelease + Cortex-A53 tuning are in place. |

### Out-of-scope repos (for completeness)
eFinder_cli_new / efinder-combo / efinder_cli_tetra3rs_mp items (`:GA#` altitude
command, `hint_uncertainty_deg` config key, sleep(0.05) removal, live-sky hybrid
test) are recorded in the decision docs but those repos are outside this session's
scope — unverifiable here.

---

## 3. Superseded architectures (so nobody resurrects them by accident)

1. **`hybrid` (cedar-detect gRPC + olive-solve)** — superseded by `olive`
   (sycamore + olive-solve). Two sessions explicitly removed all cedar-detect
   references from the shipped line.
2. **testrepo aggregator CI** (`buildbinaries.yml`, `databases-latest` store) —
   dismantled by the pensive-allen per-repo release workflows. The per-repo
   build-config decision records that reference testrepo describe a dead pipeline.
3. **cedar-solve / cedar-detect as runtime components** — both are now reference
   implementations / database generators only.

---

## 4. Priority order

1. **O1** — olive-solve v0.1.2 from noext (runbook §1; everything else about wheel
   consumption is already correct and waiting).
2. **S1** — tag sycamore-extract v0.12.0 (activates the Bad-seeing preset's new
   detector features fleet-wide via OTA).
3. **D4** — Node 24 workflow bumps before 2026-06-16, all repos.
4. **D2** — rebuild the diofinder image (v0.0.25) once 1–2 are done.
5. **D1 + S2** — the on-device verification batch (one observing session covers most of it).
6. **O2, D3, D6** — branch/process reconciliation and housekeeping.
7. **A2/A3, O3, O4, T1** — next-cycle improvements.
