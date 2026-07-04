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
10. **Describe tracking mode's solve path precisely**: it is verify-only ONLY
    when olive-solve ≥ 0.1.6 is installed (`verify_attitude`, capability-
    probed); on older wheels it is tight-hint solving with the pattern hash
    still paid. Do not present the Legacy preset as the recommended path —
    both are documented honesty constraints in CLAUDE.md.
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

*(Both tasks below shipped in v0.11.22 — kept as one-line records; see git
history for the full specs.)*

- **Task A (done, v0.11.22)**: webui frame reads now go through the solver's
  FrameSlots-bracketed `frame_get` maint command (`after_seq` chaining for
  strictly consecutive burst frames; bundles record `frames_synced`); the
  direct-SHM read survives only as a labeled daemon-down fallback.
- **Task B (done, v0.11.22)**: `diofinder/imu_frame.py` Kabsch-fits the
  IMU-body→camera rotation from solve-pair rotation vectors (quality-gated:
  pairs≥4, axis diversity, R²) and the solve hint conjugates the IMU delta
  through it (cone 1.2× when active, 2.5× fallback otherwise).
- **Done (v0.11.23), phase-2 pointing**: the LX200 prediction in comms uses
  the same fit — `imu_ref` grew a 6th element (the solved sky quaternion),
  and `_imu_predict` composes the exact quaternion prediction
  (`quat_to_radec`, boresight = row 0 of R(q)) when fit + sky_q are present;
  the C-matrix small-angle path (5° clamp, pole guard, calib gates) survives
  as the fallback.
- **Done (v0.11.23), watchdog first-publish deadline**: 300 s from comms
  start with no first publish → CRITICAL + exit (systemd restarts). The
  arm-after-first-publish rule still protects slow DB loads under that bound.
- **Done (v0.11.23), tracking fast paths**: with olive-solve ≥ 0.1.6 the
  tracking solve goes through `verify_attitude` (true verify-only — pattern
  hash skipped; NoMatch drops the lock and re-acquisition is always the full
  solver); with sycamore ≥ 0.14 ROI detection goes through the native batched
  `detect_stars_roi` (`tracking.roi_detect_native`). Both capability-probed
  with graceful fallback to the v0.11.22 behavior on older wheels.

### July 2026 audit — status after v0.11.24

Full findings with evidence, scenarios, and fix directions:
**`docs/audit-2026-07.md`** (IDs below refer to it). **v0.11.24 implemented
every P1 and P2 row and most of P3/P4** — the tables below are kept as the
record of what shipped; the *deferred* items (with reasons) are listed after
them and are the only open work.

**P1 — correctness / silent field failure (ALL DONE, v0.11.24):**

| ID | Task |
|----|------|
| W1 | Camera-stall liveness: solver counts consecutive same-`seq` timeout returns from `acquire_read_slot`; after ~30 s stop refreshing `epoch_monotonic` (or publish `frame_stale`) so the existing watchdog restarts the unit instead of serving frozen pointing as live. |
| W2 | Align path: make both `align_request_q`/`align_response_q` puts non-blocking (full queue → ":CM# busy" reply / drop superseded response), and add a `request_id` echoed through `AlignResult` so a late result from a previous sync can never be accepted — or persisted as boresight — for the current one. |
| W3/F1 | `set_db` double-fault: when the previous-DB reload also fails, `os._exit(1)` so systemd restarts with the configured DB (making the existing comment true); throttle the per-frame AttributeError warning. |
| F2 | FOV escape hatch: after N further FallbackGate fires, escalate the loose retry to a genuinely wide window (several degrees) so a lens change can recover; make the calibration reset (Config page + documented procedure) also restore `fov_deg`, not just `fov_calibrated`. |

**P2 — robustness (ALL DONE, v0.11.24, except W6 — see deferred):**

| ID | Task |
|----|------|
| W4 | Camera RPC timeout vs frame period: drain the camera cmd queue from a helper thread or scale `_call_camera` timeout with live exposure; bump `_call_solver` past the 5 s frame wait. |
| W5 | Treat a stale `imu_t` (>2 s) as IMU-lost in the solver's bg_cache feed (`note_imu_lost`) so a wedged BNO055 doesn't disable slew detection with a frozen quaternion. |
| F4 | Surface persistence failures: `seeing_set` / `auto_tune commit` / `auto_tune_apply_last` return `persisted:false` + error instead of silent `ok=True`; webui banner. |
| F5 | Honor `fallback_gate.note_failure()`'s return on the solver-raise path so an exception-class failure streak still triggers the loose retry. |
| F6 | `force_recalibrate` persist failure → propagate into the reset reply ("applied live, NOT persisted"). |
| F3 | Tracking flap observability + backoff: count tracking-solve failures (new counter in `tracking_status` + bundle), add relock backoff after a failed episode; consider excluding verify-only failures from the FallbackGate count. |
| F7 | `dark_capture`: `_ae_pause()` before the camera sets; widen the try/finally over the setup+settle phase. Also set `solver_busy_t` around the solver-side capture (W-L3) and chain `after_seq` for unique frames (F-L2). |
| W6 | systemd `WatchdogSec` + launcher `sd_notify` pings fed by maint-socket + FrameSlots self-checks — converts any comms/camera wedge into a restart. |

**P3 — performance (DONE in v0.11.24 except P4/P5 — see deferred):**

| ID | Task |
|----|------|
| P1+P2+P8 | Solver-side IPC diet: stop writing the 5 legacy `imu_ref_*` split keys per solve (readers move to the atomic tuple), batch calib keys into one composite, keep `imu_calib_pairs` solver-local, pass the frame snapshot into `get_fov_max_error`. ~3–12 ms back per solved frame. |
| P3+P6 | Poll-path RPC collapse: ~100 ms-TTL snapshot cache shared by :GR/:GD; `status` handler uses one `dict(shared_cfg)` snap instead of ~15 gets. |
| P4 | Live-view transport: binary framing on the maint socket (length-prefixed raw payload) instead of base64-in-JSON; longer term a dedicated display SHM segment. |
| P5 | bg_cache: bin frames at submit time and median-stack at detection resolution (~4× less copy/stack/rebuild; re-verify MAD noise level against calibrated sigmas first). |
| P7/P10/P11 | Composite `imu=(q,t)` key (40→20 RPC/s, atomic pair); TTL `test_mode` read; throttle dark-frame publishes to every 5th. (The camera request-API capture half of P10 is deferred — see below.) |

**P4 — small robustness / observability (ALL DONE, v0.11.24):**

| ID | Task |
|----|------|
| F-L4/F-M4 | Add the ~10 missing keys to conf.default; wire `auto_exposure_peak_floor`/`nominal_s` into a params_set (or re-document as config-only). |
| F-L6 | FallbackGate counters in `calibration_status`; `tracking_status` into the debug bundle. |
| F-L1 | Hot-pixel mask capture metadata (exposure/gain/sensor mode) + mismatch warning. |
| F-L3 | `conf_migrate` numeric matching via `_norm()` (a `%.10g`-persisted `1.0` currently escapes migration). |
| F-L5/F-L9 | Throttle the two per-frame solver exception warnings; log active `DIOFINDER_*` env overrides at startup and surface them in `version`. |
| W-L1/W-L2/W-L5/W-L6 | bg_cache gen re-check after `_needs_rebuild.clear()`; bounded `_call_solver` put; receive-buffer caps on both server sockets; per-command maint client timeouts. |
| F-L7/F-L8 | Mutual exclusion between auto_tune sweep and dark_capture; lineage classification tolerant of AE-moved exposure/gain. |

### Deferred from the July 2026 audit (open, with reasons)

- **P4 — DONE (v0.11.25): display SHM segment.** `diofinder/display_shm.py`
  + a launcher-allocated `diofinder_display` segment; the solver seqlock-
  writes it demand-gated on `display_wanted_until` (comms `display_start`
  keepalive); the web UI reads it directly (`_live_frame`), falling back to
  `frame_get`. No maint round-trip / base64 for the live view, and zero cost
  when no browser is open. On-device before/after timing on `/frame.jpg` is
  still worth capturing but not required.
- **P5 — DONE (v0.11.26) opt-in; A/B before default.** `bg_cache_bin_at_submit`
  (default OFF, live-mutable via `solver_params_set`): bins each frame to the
  detection resolution at submit (uint16 block SUMS — exact, ~2× less stack
  memory, ~4× fewer median elements) and median-stacks there. It IS a
  different estimator (spatial-mean and temporal-median don't commute); the
  offline quantification (`scratchpad/p5_quant*.py`, method preserved in
  `tests/test_bg_cache_bin_at_submit.py`) put the noise divergence at ~1–2%
  on real dark sky and injected light pollution, with identical u8 offsets and
  matching star counts at the operating sigma. Validate on a clear night with
  the background A/B + `solve_stats` (compare `bg_cache_status.bin_at_submit`
  on/off), then flip the default in a follow-up.
- **P9 (TRACKING fixed-cost trims)**: revisit only if tracking mode
  graduates from experimental/default-off. **Graduation gate now exists**:
  `tests/ab_tracking.py` (`diofinder-ctl ab-tracking`) does the on-sky FULL
  vs TRACKING A/B (rate, latency, pointing-agreement) — run it clear-sky
  before flipping the default.
- **P10 (camera request-API capture)**: picamera2 API variance across
  versions; needs on-device validation. The TTL `test_mode` half shipped.
- **W6 (systemd `WatchdogSec` + `sd_notify`)**: `Type=notify` misconfigured
  can fail the unit at startup — needs on-device testing; W1's camera-stall
  detection already covers the biggest gap it targeted (comms-thread wedges
  are also now far less likely after W2's non-blocking align puts).
- **F2 (partial)**: the escalated full-range blind retry shipped; making
  the Config-page reset also restore `fov_deg` is still open (the docs now
  tell lens-changers to set `fov_deg` manually).

### Accepted-by-design (do NOT "fix" without a new reason)

- LX200 server handles one client connection at a time.
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
