# AGENTS.md — AI-agent contributor guide for the diofinder project

This guide is **model-agnostic**: it is written so that any capable AI coding
agent (Claude, GPT/Codex, Gemini, or another framework) — or a human — can pick
up this project cold, understand how the pieces fit, follow the working
conventions, and execute the remaining backlog without access to any prior
conversation history.

Read this file first. Then read `CLAUDE.md` in this repo — despite the name it
is a plain-markdown deep technical guide to every subsystem (process
architecture, IPC tables, camera modes, detection pipeline, calibration,
auto-exposure, presets) and applies to any reader, not just Claude.
`sycamore-extract/CLAUDE.md` and `tetra3rs/CLAUDE.md` play the same role in
those repos.

---

## 1. What this project is

**diofinder** is a plate-solving electronic telescope finder that runs on a
Raspberry Pi Zero 2W (quad-core Cortex-A53, **512 MB RAM** — memory and IPC
budgets are real constraints) with an Arducam IMX477 camera and an optional
BNO055 IMU. It captures sky frames, extracts star centroids, plate-solves them
against a star catalog, and serves the pointing to planetarium apps
(SkySafari) over the LX200 TCP protocol, plus a self-hosted web UI.

The **active development branch of diofinder is `olive`**, not `main`.
All PRs target `olive`; releases are cut from `olive`.

### The repo ecosystem

| Repo | Role | Language | Ships as |
|---|---|---|---|
| `mconsidine/diofinder` | The application (this repo): daemon, web UI, install/update scripts, SD-card image builds. **Hub repo — start here.** | Python | GitHub release with a flashable `.img.xz` |
| `mconsidine/olive-solve` | The plate solver ("tetra3" Rust workspace + PyO3 binding). Installed Python package name: **`tetra3`**. | Rust | GitHub release with an aarch64 abi3 wheel |
| `mconsidine/sycamore-extract` | The star-centroid extractor. Repo name ≠ package name **on purpose**: the Python module is **`star_detect`**. | Rust (PyO3) | GitHub release with an aarch64 abi3 wheel |
| `mconsidine/astro_databases` | Prebuilt star databases (`diofinder_13deg.npz`, deep `_mag85` variant) + `star_names.csv` | data | GitHub release assets |
| `mconsidine/eFinder_cli` | AstroKeith's original project — the **reference baseline** the "Legacy" preset re-creates. Read-only inspiration; do not develop here. | Python | n/a |
| `mconsidine/tetra3rs` | A separate, more full-featured solver crate (SIP distortion calibration, WCS). Used off-device by `scripts/calibrate_lens.py`. Independent release discipline — see its CLAUDE.md (three version files must bump in lockstep). | Rust | crates.io `tetra3` + PyPI `tetra3rs` |
| `mconsidine/cedar-solve`, `mconsidine/cedar-detect` | Upstream reference code the solvers derive from. Read-only. | — | n/a |

**Critical naming trap:** the solver wheel installs as Python package `tetra3`
(from repo *olive-solve*), and the extractor installs as `star_detect` (from
repo *sycamore-extract*). Two different `tetra3`s exist in this ecosystem
(olive-solve's binding and tetra3rs); on the device, `import tetra3` is
**olive-solve**.

### How the pieces compose at runtime

```
camera_proc ──frames(SHM)──> solver_proc ──solutions──> comms_proc ──LX200──> SkySafari
                              │  star_detect (sycamore wheel): centroids       │
                              │  tetra3 (olive-solve wheel): plate solve       └─ maint socket ──> webui
                              └  bg_cache: temporal background model
```

Wheels are **not vendored in git**. The image build (`release.yml`) and the
on-device updater (`diofinder-update`) download them from the source repos'
GitHub releases. This is the project's most dangerous seam — see §5.

---

## 2. Working conventions (all agents)

1. **Branch → PR → squash-merge into `olive`.** Never push directly to
   `olive`. Reuse of a feature branch after its PR merged requires resetting
   it onto the new `olive` head (`git checkout -B <branch> origin/olive`) and
   force-pushing; never stack on already-merged history.
2. **Sync before branching**: `git fetch origin olive && git checkout -B
   <branch> origin/olive`. Never run `git reset --hard` with uncommitted work
   in the tree (this has destroyed in-progress edits twice in this project's
   history).
3. **Run the test suite before every commit**: `python3 -m pytest tests/ -q`
   from the repo root. It is hardware-free (picamera2 / star_detect / tetra3
   are stubbed where needed) and takes ~1 s. All tests must pass. New behavior
   needs a test; bug fixes need a regression test that pins the failure.
4. **Version bump discipline** (per release): `diofinder/config.py` →
   `version: str = "X.Y.Z"`. If you add config keys: also
   `etc/diofinder.conf.default` (with an explanatory comment) and the
   `CLAUDE.md` shared_cfg / config tables. If a *default changes for a
   correctness reason*, add a one-shot migration rule in
   `diofinder/conf_migrate.py` (and bump `CONF_VERSION`) so OTA-updated
   devices pick it up — see §5.
5. **Capability-probe, don't version-check.** Wheels of many vintages exist in
   the field. New wheel features must be probed
   (`hasattr(...)` / `getattr(module, "HAS_X", False)` / signature inspection)
   and degrade gracefully on older wheels. Grep for `HAS_TOPHAT`,
   `HAS_BG_IMAGE`, `SOLVER_HAS_STRICT_HINT` for the pattern.
6. **Mind the IPC budget.** `shared_cfg` and `latest_solution` are
   `multiprocessing.Manager` dict *proxies*: every `.get()` is a pickled
   unix-socket round-trip (~0.3–1 ms on the Pi). Hot paths take ONE
   `dict(shared_cfg)` snapshot per iteration and read locally; only writes go
   to the proxy. Do not reintroduce per-key reads in the solver frame loop or
   the LX200 poll path.
7. **Single-writer discipline** for shared state: each `shared_cfg` key has
   one writing process (table in CLAUDE.md). Multi-field state read by another
   process must be published as ONE atomic value (see `imu_ref`), not as
   several keys.
8. **Config writes** go through `config.save_keys` only (flock-serialized,
   atomic-replace). Never hand-write `/etc/diofinder/diofinder.conf`.
9. **Comments state constraints, not narration.** This codebase's comments
   record *why* something is load-bearing (often a live-debugged failure).
   Follow that idiom; don't strip them.
10. **Do not describe tracking mode as "verify-only"** and do not present the
    Legacy preset as the recommended path — both are documented honesty
    constraints in CLAUDE.md.
11. Keep model/vendor identifiers out of code, commits, and PRs.

---

## 3. Build, test, and diagnose

```bash
# Unit tests (no hardware, ~1 s)
python3 -m pytest tests/ -q

# Replay a user's debug bundle OFFLINE on any machine (x86 fine):
python3 tests/diag_solve.py --bundle /path/to/diofinder_debug_*.zip
#   --bin 2 to force the live detection binning.
# Requires: pip-installed star_detect + tetra3 wheels (build from
# sycamore-extract / olive-solve checkouts with `pip wheel .` in the binding
# dir) and the star database at /var/lib/diofinder/default_database.npz
# (download from astro_databases releases).

# Offline parameter sweep over a bundle/burst:
python3 tests/replay_corpus.py --corpus bundle.zip --sweep-sigma ...
```

**The debug bundle is the primary diagnostic artifact.** Users download it
from the web UI Home page; it contains a 12-frame raw burst,
`effective_params.json` (live knobs incl. wheel versions + hot-pixel state),
`diofinder.conf`, `status.json`, IMU samples, and the journal tail. The
offline replay reproduces the live pipeline exactly. When a device "won't
solve," get a bundle and replay it before theorizing.

**First questions for any no-solve report** (each of these was a real,
multi-hour incident — check them in order):

1. **Wheel versions** (in `effective_params.json` → `version.wheels`, the
   solver startup log, and the Home page footer). Stale wheels under new code
   have masqueraded as application regressions. olive-solve must be ≥ 0.1.3
   (blind-hint fallback); sycamore ≥ 0.13.
2. **Hot-pixel mask** (`hot_pixel` in the bundle): a poisoned mask (>0.5 % of
   the frame) corrupts every centroid while star *counts* look healthy.
   Guards exist since v0.11.17/18, but a device can carry an old mask.
   Fix: `diofinder-ctl raw '{"cmd":"hot_pixel_clear"}'`.
3. **FOV calibration** (`calibration` in `status.json`): is the committed FOV
   centered on the rolling-window median? Self-healing exists since v0.11.19
   (drift recommit + `FallbackGate` loose retry) but takes ~50 solves /
   20 failures to engage.
4. **Conf staleness**: the Update page's "Settings differing from shipped
   defaults" table; `conf_migrate.pending_migrations()`.
5. **Journal correlation**: solve failures cluster after which event? (mask
   capture, seeing toggle, slew, exposure change, restart.)

---

## 4. Failure-class catalog (hard-won; do not re-derive)

These were all diagnosed from live debug bundles in July 2026 and each now has
a guard in code. If you touch the related code, keep the guard and its test.

| Failure | Mechanism | Guard (version) |
|---|---|---|
| Zero solves, healthy star counts | Uncapped dark capture → 195k-px hot-pixel mask → repair corrupts centroid geometry | `implausibly_large` refuse-on-save + ignore-on-load (v0.11.17) |
| Dark capture unusable even when capped | Clean dark frame → MAD quantizes to 0 → threshold = median → 47 % of frame flagged | `MIN_THRESH_DN = 3` floor (v0.11.18) |
| Post-slew re-acquisition deadlock | IMU hint applied in body frame lands outside its own cone; pre-0.1.3 olive-solve has no blind fallback | 2.5× cone (v0.11.18), hint dropped after 5 fails + `FallbackGate` (v0.11.19/20); real fix = task B below |
| Solves stop after sensor-mode change | Committed FOV at edge of ±0.1° window; calibrator only learns from successes | Recenter (v0.11.17), measured-stddev drift dead band + loose-retry (v0.11.19) |
| "Code regression" that wasn't | OTA updated code but silently kept old wheels | wheel-version logging + update summary (v0.11.18), env pins (v0.11.21) |
| AE gain ping-pong 2.1↔3.2 | ×1.5 step straddles the star-count deadband | reversal damping + raise debounce + settle guard (v0.11.13/18/20) |
| Frozen-but-live pointing | Camera capture failure republished the stale buffer with fresh seq | publish-only-on-success + backoff (v0.11.20) |
| Settings saved ≠ settings in force | `%.6f` float persist (1e-7 → 0.0), unlocked cross-process conf RMW, half-applied seeing presets | `%.10g` + flock + atomic replace + DB-switch-first (v0.11.20) |
| Stale conf across OTA | `/etc` conf persists; old defaults (scientific tuning w/ DPC, old bg mode…) rot silently | one-shot `conf_migrate` + divergence report (v0.11.21) |

Device quirk worth knowing: field units often have **no RTC/NTP** — journal
timestamps can be weeks off. Correlate by event order, not wall-clock.

---

## 5. Release mechanics

### diofinder (image release)

Trigger the `Release image` workflow on branch `olive` with inputs
`{build_image: true, tag: "vX.Y.Z"}` (GitHub UI → Actions → Release image →
Run workflow, or the API equivalent). The workflow: creates the tag, downloads
the **latest** olive-solve + sycamore wheels and the astro_databases assets
(pin with repo variables `OLIVE_SOLVE_TAG` / `SYCAMORE_TAG` / `DB_TAG`),
builds the SD image (~30 min), and publishes a release with
`diofinder-sycamore-<date>-vX.Y.Z.img.xz` attached. The sycamore wheel is a
**hard requirement** — the build fails without it (do not weaken this; an
image without it restart-loops forever).

Before tagging: version bumped in `diofinder/config.py`, tests green,
conf.default + CLAUDE.md updated, migrations added for changed defaults.

### olive-solve / sycamore-extract (wheel releases)

- **sycamore-extract**: run the `build` workflow via **workflow_dispatch**
  with input `{"version": "vX.Y.Z"}` — it builds the aarch64 wheel AND
  publishes the release. (Tag-push does not work through this environment's
  git proxy; use the dispatch.)
- **olive-solve**: release workflow publishes the aarch64 abi3 wheel on tag.
  Behavior changes here affect every diofinder device on its next update —
  keep the Python API backward compatible and remember diofinder
  capability-probes rather than version-checks.
- **tetra3rs** (separate project): before tagging, bump `Cargo.toml`, root
  `pyproject.toml`, AND `python/Cargo.toml` in lockstep — see its CLAUDE.md
  for the failure mode when you don't.

### On-device update paths

`diofinder-update` (OTA): git-syncs the code, refreshes wheels from latest
releases (honors `OLIVE_SOLVE_TAG`/`SYCAMORE_TAG` env pins), prints a
per-wheel refresh summary and a conf divergence report, resyncs CLI wrappers +
systemd units, restarts. Wheel refresh failures are non-fatal but loudly
reported — **always confirm the wheel summary** when an update was meant to
pick up solver/extractor fixes. Requires internet (station mode, not AP mode).
Conf migrations apply at the next service start.

---

## 6. Current state (as of v0.11.21, 2026-07-03)

Released: **v0.11.21** (latest, recommended), v0.11.20, v0.11.17, v0.11.15
and earlier. `olive` == v0.11.21 with no unreleased work. All 160 unit tests
pass. The operational backlog is empty; the remaining items below are an
optimization and a diagnostics-fidelity improvement.

Recent-history summary (details in each PR, #99–#103):
- v0.11.17: hot-pixel mask sanity guard; FOV recenter 13.64→13.54.
- v0.11.18: dark-capture MAD floor; wheel-version visibility everywhere;
  AE reversal damping; hint cone 1.5×→2.5×.
- v0.11.19: FOV drift dead band from measured stddev; `FallbackGate`
  loose-window blind retry.
- v0.11.20: 15 audit fixes (camera stale-publish, solver IPC snapshot,
  bg-cache generation guard, atomic conf writes, `%.10g` floats, seeing_set
  atomicity, sycamore-required build, AE settle guard, atomic `imu_ref`, …).
- v0.11.21: one-shot conf migrations + divergence report; set_db hardening
  (watchdog busy flag, 60 s timeout, release-before-load); refcounted AE
  pause; noise-mode cache consistency.

---

## 7. Remaining backlog — ready to hand to any agent

Each task below is self-contained: context, design, files, and acceptance
criteria. Update this file (remove/annotate the task) in the same PR that
implements one.

### Task A (diagnostics) — FrameSlots-aware web-UI frame reads

**Problem.** Three webui code paths read camera frames from POSIX shared
memory directly and always take slot 0: `debug_collect` (~`webui/app.py:1609`),
`frame_jpg` (~`:1296`), `_bgrun_capture` (~`:1811`). Slot 0 is frequently the
camera's active write target, so captured frames can be **torn** (top half
exposure N, bottom half N−1) and bursts advance only when slot 0 happens to be
rewritten — breaking the "≥ bg_cache_stack consecutive frames" premise debug
bundles are built on. The webui runs in a **separate systemd unit** and cannot
see the daemon's `multiprocessing.Value` slot state, which is why it cheats.

**Design.** Do NOT plumb `FrameSlots` into comms (verified: `comms_main` does
not receive it, and the FrameSlots protocol has a single reader slot — the
solver). Instead reuse the solver's existing safe read path:
`_SolverState.read_frame` already returns a fresh, bracketed frame copy (it is
what `dark_capture` uses). Add a solver op `SOLVER_OP_FRAME_GET` in
`diofinder/solver_proc.py::_handle_solver_cmd` returning the frame (raw bytes
+ shape + seq), a comms maint command `frame_get` that forwards it (frame is
960×760 u8 ≈ 730 KB; the maint protocol ships JSON — base64 it, ~1 MB, fine
over the unix socket), and replace the three webui SHM readers with
`_safe_call("frame_get")`. Keep the direct-SHM path only as a fallback when
the daemon is down, labeling such frames `"unsynced"` in bundle metadata. For
the 12-frame burst, call `frame_get` in a loop and use the returned `seq` to
assert consecutiveness (skip/retry on gaps).

**Files.** `diofinder/solver_proc.py` (+op), `diofinder/worker_cmds.py`
(+constant), `diofinder/comms_proc.py` (+command), `webui/app.py` (three
readers), `CLAUDE.md` (maint command list), tests: solver-op unit test with a
stub `read_frame`; bundle metadata assertion.

**Acceptance.** Debug-bundle frames are bracketed reads (no torn frames by
construction); burst frames are strictly consecutive `seq`s; webui still
serves a frame when the daemon is stopped (fallback labeled).

### Task B (optimization) — IMU hint in the camera frame (full fix for the body-frame hint)

**Problem.** `solver_proc._imu_propagate_hint` composes the IMU's rotation
delta directly onto the last solved sky attitude, implicitly assuming the IMU
body frame ≡ camera frame. The mounting misalignment makes the hint direction
wrong by up to 2× the slew angle (measured on-sky: 39.5° hint error for a
22.5° slew, 1.76×). Mitigations shipped: cone widened to 2.5× the measured
angle (v0.11.18), hint dropped after 5 consecutive failures (v0.11.20),
olive-solve ≥ 0.1.3 falls back to a blind pass. Cost today: a wasted hinted
pass after large slews; the hint never actually *helps* re-acquisition.

**Design.** Estimate the fixed rotation between IMU-delta space and sky-delta
space from data already collected, then conjugate the delta through it:

1. `solver_proc._imu_update_reference` already harvests per-solve-pair motion:
   it stores `(r_imu[3], cam_r, cam_u)` in `shared_cfg["imu_calib_pairs"]` and
   fits the linearized 3×2 matrix `imu_calib_C` (used by LX200 pointing).
   Extend the stored pairs with the **full 3-D sky rotation vector**
   `r_sky[3]` (compute the delta quaternion between consecutive solved
   attitudes — `soln["quaternion"]` is available — and convert via
   `quat_delta_rotvec`, already in `diofinder/imu_math.py`).
2. New pure function in `diofinder/imu_math.py`:
   `fit_frame_rotation(pairs) -> (R 3x3, quality)` solving the Wahba/Kabsch
   problem over the rotation-vector pairs (SVD; enforce `det(R)=+1`).
   Quality gating is the load-bearing part: require ≥ 3 pairs, R² ≥ ~0.9, and
   **axis diversity** (the r_imu set must span ≥ 2 non-collinear directions —
   check the second singular value / condition number; alt-only slewing makes
   the fit degenerate about that axis). Rolling window (last ~20 pairs)
   handles slow BNO055 heading drift.
3. Solver publishes `shared_cfg["imu_frame_R"]` (9 floats) + quality after
   each refit. `_imu_propagate_hint`: when a good R exists, transform
   `r_delta_cam = R @ r_delta_imu`, rebuild the delta quaternion, compose;
   tighten the cone to `max(2°, 1.2× angle)`. When absent/degraded, current
   behavior (2.5× cone) unchanged.
4. Optional phase 2 (separate PR): use the same R for the comms LX200
   prediction, replacing the 2-D `imu_calib_C` linearization (better roll
   handling); keep `imu_calib_C` published for the webui quality display.

**Constraints.** Pure-numpy, hardware-free tests (synthetic mountings: 90°
rotations, arbitrary tilts → hint error collapses from ~1.76× to <0.1× slew;
degenerate-coverage refusal; drift re-learn). Keep IPC discipline: reads from
the frame snapshot, one batched publish. Never regress the fallback path.

**Files.** `diofinder/imu_math.py`, `diofinder/solver_proc.py`,
`tests/test_imu_smoothing.py` or a new `tests/test_frame_rotation.py`,
`CLAUDE.md` + `docs/imu.md` (flowchart mentions the transform).

**Acceptance.** With a synthetic 90°-mounted IMU and a 20° slew, the hinted
attitude is within 2° of truth (was ~35°); all existing IMU tests pass; the
transform disengages (falls back) on collinear-axis histories.

### Accepted-by-design (do NOT "fix" without a new reason)

- LX200 server handles one client connection at a time.
- A solver hung *before its first publish* never arms the watchdog
  (deliberate: slow first DB load).
- Tracking mode (`tracking.py`) is experimental, default-off; its dedupe
  distance not scaling with `bin` is known and harmless at bin=2.
- `detect_bin` is restart-only (the temporal cache is built at one binning).
- The Legacy preset is intentionally a faithful re-creation of the eFinder_cli
  baseline, including its weaknesses.

---

## 8. Handoff protocol

When finishing a work session (any agent):
1. All tests green; PR squash-merged into `olive`; local `olive` synced.
2. If defaults/keys changed: conf.default, `conf_migrate.py`, CLAUDE.md tables
   updated in the same PR.
3. If the work closes or adds a backlog item: edit §7 of this file in the same
   PR. This file is the durable task queue — session-local task trackers do
   not survive handoffs.
4. Cut a release only when asked; note in the PR whether `olive` carries
   unreleased work.
5. Record any new *diagnosed failure class* in §4 with its guard — that table
   is the project's institutional memory.
