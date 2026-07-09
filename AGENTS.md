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
systemd units + sudoers drop-ins, restarts. Wheel refresh failures are
non-fatal but loudly reported — **always confirm the wheel summary** when an
update was meant to pick up solver/extractor fixes. Requires internet
(station mode, not AP mode). Conf migrations apply at the next service start.

**Self-update guard (v0.11.32)**: right after checkout, the script compares
itself (`$0`) against the freshly fetched `scripts/diofinder-update` and, if
they differ, hands off via `exec` to the fetched copy (guarded by
`DIOFINDER_UPDATE_REEXEC` against a loop) instead of finishing the run with
stale in-memory resync logic. Without this, a release that adds a new CLI
wrapper name or sudoers drop-in only reaches an already-deployed device on a
**second** `diofinder-update` run — the first run's OLD script git-syncs the
new files onto disk but doesn't yet know to install them anywhere (this bit
v0.11.31: `diofinder-factory-reset` shipped in the webui and the checkout,
but `command not found` on the box, because the OLD update script's wrapper
list didn't have its name yet).

---

## 6. Current state (as of v0.11.46)

Released through **v0.11.46** (latest). All 237 unit tests pass. The
operational backlog is empty; the remaining items below are deferred
optimizations/robustness items with stated gating reasons (see section 7).

Recent-history summary (details in each PR, #99–#103):
- v0.11.46: exact per-frame timing + per-frame RA/Dec in debug bundles.
  New `diofinder/frame_meta.py`: the camera now captures via a picamera2
  *request* (fallback to `capture_array` on old picamera2) and publishes
  each frame's libcamera metadata — `SensorTimestamp` (CLOCK_BOOTTIME ns at
  first-row readout start), ACTUAL `ExposureTime`, ACTUAL `AnalogueGain` —
  into a `shared_cfg["frame_meta"]` ring keyed by FrameSlots seq
  (`publish()` now returns the seq), plus the measured boottime→wall
  offset, so consumers derive `exposure_start = SensorTimestamp +
  wall_offset − ExposureTime` to ms accuracy. `frame_get` replies attach
  the matching `meta`; every published `latest_solution` now carries the
  `seq` of the frame it came from. `debug_collect` writes **`frames.json`**
  (filename → seq, `exposure_start_utc`, actual exposure/gain, the frame's
  OWN solve result matched by seq — the old `imu.json` `ref_*` fields were
  the PREVIOUS solve, up to one frame period stale — plus the IMU
  snapshot). New **`tests/bundle_solve.py`**: offline re-solve of every
  bundled frame (same effective-params hydration as `diag_solve.py
  --bundle`, plus the loose-window retry) emitting one JSON packet mapping
  each `frame_XX_raw.png` to solved RA/Dec/roll/FOV/matches merged with
  the capture metadata. Unit-tested in `tests/test_frame_meta.py` (+ seq
  additions in `tests/test_frame_get.py`).
- v0.11.45: fix the dark-frame Exposure field on the Advanced page
  rejecting round values (e.g. 0.9, its own default) and snapping to
  0.851 / 0.901. Cause: a browser `<input type="number">` validates
  against `min + n×step`, and the field had `min="0.001" step="0.05"` —
  0.001 is not a multiple of 0.05, so the entire step grid was offset by
  0.001 and no `.05`-aligned value (including the default 0.9) was
  reachable. Changed that field to `step="any"`, matching the sibling
  main-exposure number input (which was already `step="any"`) — the two
  had diverged. Audited **every** stepped number input across all
  templates programmatically: this was the only off-grid one; all others
  have `min` as an exact multiple of `step` (slider-paired fields all
  align min to the slider step), so their round/default values are
  reachable and were left unchanged.
- v0.11.44: gain sliders/number-inputs/step-buttons on Home, Focus, and
  Advanced were capped at 64, well above the IMX477's real analog-gain
  ceiling (`MAX_ANALOG_GAIN = 22.26` in `camera_proc.py`, the driver's
  documented register limit) — a request above the real cap was silently
  rejected by the backend (a confusing "out of range" error at the top of
  the slider, not a graceful clamp). All three pages' gain controls
  (slider `max`, number-input `max`, and the `stepGain` JS clamp) now cap
  at 22, matching the dark-capture gain field on the Advanced page, which
  was already correctly capped. Exposure's minimum (0.001 s) was NOT
  changed — unlike the gain ceiling, it isn't backed by a cited hardware
  register limit in this codebase, and confirming the sensor's true floor
  needs an on-device query of `picamera2`'s negotiated `ExposureTime`
  control range, which wasn't done.
- v0.11.43: auto-exposure now defaults **OFF** (`auto_exposure_enabled:
  false` in both the dataclass fallback and `diofinder.conf.default`) —
  exposure/gain stay where the user set them unless the controller is
  explicitly enabled (Camera-page toggle / `auto_exposure_set`; both
  persist). Devices with an existing conf keep their persisted value —
  toggle once in the webui (or factory-reset) to adopt the new default.
  Also added a **Downloads card to the Advanced page** (visible in novice
  AND expert mode, like the factory-reset card): the debug-bundle button
  (same `/debug/collect` flow as Home) plus direct download links for every
  saved `bg_ab_*.zip` burst archive (`/bgtest/run/download?name=...`).
- v0.11.42: fix the live view freezing until `diofinder-webui` is manually
  restarted (observed on v0.11.41 boot). Root cause: every
  `diofinder.service` (re)start unlinks and RECREATES
  `/dev/shm/diofinder_display` (`ExecStartPre` rm + `display_shm.create`),
  but the webui's long-lived `DisplayReader` kept its mapping to the OLD
  segment — `available` stayed True and `read()` kept returning the last
  frame ever written there, with a valid never-advancing seq, which
  `_live_frame` served as fresh forever. Any daemon restart under an open
  browser (watchdog restart, failed first start at boot while the camera
  stack comes up) froze the view. Fixes in `webui/app.py::_live_frame`:
  (1) the segment's /dev/shm inode is checked each poll — a change or the
  file disappearing drops the reader so the next poll re-attaches to the
  daemon's CURRENT segment; (2) a frame whose seq hasn't advanced in 15 s
  is served via `frame_get` instead (writer-stopped backstop);
  (3) an attach that finds no segment is no longer sticky — previously a
  webui that booted before the daemon recorded `hw` with an unavailable
  reader and never retried, permanently disabling the fast path
  (`After=diofinder.service` only orders process start; the launcher takes
  tens of seconds on the Zero 2W before creating segments). Also hardened
  `DisplayWriter` to resume its generation counter from the segment's
  current seq so a writer attaching to a non-fresh segment never re-issues
  an already-seen seq. Regression-tested in `tests/test_live_frame.py`
  (real segment, stubbed maint; the recreate-under-reader case is pinned).
- v0.11.41: fix `diofinder-update` failing to actually install a freshly
  *downloaded* wheel (surfaced trying to pick up v0.11.40's sycamore-extract
  v0.14.1 bump): `WHEEL_TMP=$(mktemp -d)` defaults to mode 0700 owned by
  root (the script runs via `sudo`), but the wheel is installed by `sudo -u
  diofinder pip install ... "$whl"` — the unprivileged `diofinder` user
  can't even traverse into a 0700 root-owned directory, so every download
  (as opposed to a `vendor/wheels/` override or an already-current skip)
  failed with `Permission denied` on the just-downloaded `.whl`, silently
  keeping the old wheel installed. This is why v0.11.40 alone wasn't enough
  to get sycamore-extract v0.14.1 onto a device — the download itself
  worked (confirmed by GitHub's `releases/latest`), only the local install
  step failed. Fix: `chmod 755 "$WHEEL_TMP"` right after creating it.
- v0.11.40: wheel-refresh-only release, picking up sycamore-extract v0.14.1
  (no diofinder code change). That upstream release removes a vestigial
  per-row floor scan for `bg_mode="top_hat"` and the `bg_image` temporal
  cache path — both already fully flatten the detection image to a
  near-zero background before the matched-filter gate runs, so the trailing
  row-percentile floor was re-measuring ~0 rather than doing real work.
  Verified upstream via multi-seed synthetic A/B (including adversarial
  per-row-bias / fast row-oscillation scenes) to be byte-identical
  before/after for those two paths; `row_column_percentile`,
  `block_percentile`, `uniform_mean`, and the `block_offsets` cached path
  were audited and deliberately left unchanged (uniform_mean in particular
  showed a real false-negative regression under the same adversarial test
  when the floor was zeroed). No diofinder-side behavior change beyond
  picking up the new wheel; `SYCAMORE_TAG` is unset, so this and future
  image builds / OTA updates always resolve "latest" automatically.
- v0.11.39: fix `/update` and `/factory_reset` **always** falling back to
  a sandboxed subprocess instead of using their `systemd-run` escape hatch —
  a deterministic bug (not a rare "stale unit" collision as first suspected),
  root-caused via the sudoers drop-ins: `sudo systemd-run ... /usr/local/bin/
  diofinder-update` was authorized on the command sudo is actually asked to
  run (`systemd-run`), not on `diofinder-update` appearing later as one of
  systemd-run's own arguments, and the sudoers grant only ever covered the
  bare script path. So `sudo systemd-run ...` silently failed non-interactive
  auth on *every* webui-triggered update/factory-reset, always falling back
  to a plain `Popen` that inherits `diofinder-webui.service`'s
  `ProtectSystem=full`/`ProtectHome=true` sandbox — hence the recurring
  `mktemp: ... Read-only file system` (CLI wrapper resync) and `unable to
  access '/home/diofinder/.config/git/...'` (git) warnings on every update.
  Fix: two new root-owned wrapper scripts, `diofinder-update-launcher` and
  `diofinder-factory-reset-launcher`, each with its own scoped sudoers grant
  and each doing nothing but the `systemd-run --collect --unit=... /usr/
  local/bin/diofinder-{update,factory-reset} "$@"` call — since *that* is the
  exact command sudo authorizes, the escape hatch actually fires now. Existing
  devices need one direct-SSH `sudo /usr/local/bin/diofinder-update --ref
  olive` to bootstrap the new launcher scripts onto `/usr/local/bin` (a
  webui-triggered update can't write there until it has them); after that,
  webui-triggered updates self-heal.
- v0.11.38: webui nits — nav label/URL mismatches ("Settings"->"Utilities",
  `/camera`->`/advanced` to match its existing "Advanced" label); a
  `seeing.display_presets(cfg)` helper so the Utilities/Config "Current
  settings" table resolves `star_db`'s preset token ("standard"/"deep") to
  the same concrete db filename the "in use" column already shows (every
  other row compared like-for-like; star_db alone didn't); Home page
  exposure/gain/detection-sigma controls converted from plain number inputs
  + full-page-reload forms to the slider + step-button + direct-entry
  pattern already used on the Focus page (live-apply via `/api/camera/set`),
  with sigma's range narrowed to 1-16 on this control specifically (daemon
  still accepts 0-20 elsewhere).
- v0.11.37: retag-only; a corrective rebuild of v0.11.36's image after
  discovering `OLIVE_SOLVE_TAG`/`SYCAMORE_TAG` repo variables had been
  pinning every image build to stale olive-solve v0.1.2 / sycamore v0.12.0
  wheels (both variables have since been deleted; new builds now correctly
  resolve "latest"). No diofinder code change from v0.11.36.
- v0.11.36: version-only release to pick up **olive-solve v0.1.7** (no
  diofinder code changes). The new wheel ports five solver micro-optimizations
  from upstream `oakamil/olive-solve` — an early-rejection SVD pre-pass and
  ImmutableKdTree switch in `try_pattern_combo`/`verify_and_build_solution`,
  reciprocal-multiply and Cramer's-rule refinements, and a lazy-verification
  early-break — all algebraically/output-equivalent, so no behavioral change
  is expected on-device; see `mconsidine/olive-solve` AGENTS.md §5 for the
  full writeup. `OLIVE_SOLVE_TAG` is unset (tracks latest), so `diofinder-update`
  and new image builds pick it up automatically regardless of this release,
  but cutting a diofinder release keeps the shipped wheel version visible in
  the Home/Update pages and debug bundles per the v0.11.18 wheel-diagnostics
  policy.
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

### v0.11.28 — regression fix + kill switch (post-0.11.21 risk review)

Two fixes from a review of what changed between v0.11.21 (last field-confirmed
good) and v0.11.27:

- **Dark-throttle regression (introduced v0.11.24)**: the every-Nth-frame dark
  heartbeat throttle multiplied the watchdog-epoch gap by the frame period, so
  a long manual exposure (> 6 s) + a dark scene crossed the 30 s watchdog and
  spuriously restarted the unit. Replaced with a 3 s **time** floor
  (`_dark_publish_due`, pure/unit-tested) — bounded gap at any exposure, IPC
  win preserved at short exposure.
- **Phase-2 pointing kill switch**: `imu_exact_predict` (default true,
  live-mutable via `solver_params_set`, seeded from conf) forces the legacy
  C-matrix pointing path if the v0.11.23 exact quaternion prediction looks
  wrong on-sky — a field-reversible fallback to pre-v0.11.23 behavior with no
  downgrade.

### v0.11.35 — Home page "Detection" controls redirected away from Home

User-reported: changing an entry under the Home page's "Detection" card
(star detector, detection sensitivity, sky background) left the user on the
Camera or Background page instead of reloading Home. Root cause: three route
handlers (`bgtest_set`, `solver_params_set`, `seeing_set`) each had their own
ad-hoc, incomplete `next`-redirect whitelist, none of which included
`home_page` — despite `home.html`'s Detection card forms passing
`next=home_page`. `bgtest_set` fell back to `bgtest_page` (Background);
`solver_params_set` fell back to `camera_page` (Camera). `seeing_set` had the
same gap but was masked by an accidental double-redirect through the
deprecated `dashboard` shim (`dashboard` → `home_page`), so it happened to
still work — fragile, not a deliberate whitelist.

The file already has a shared, correct helper for exactly this
(`_redirect_next(default)` + `_NEXT_ENDPOINTS`, used consistently by
`testmode_set` and others) — these three handlers just weren't using it.

**Fix**: all three now call `_redirect_next(default)` instead of their own
inline whitelist checks. Added `bgtest_page` to `_NEXT_ENDPOINTS` (previously
only reachable via the "no `next` given" fallback, never explicitly
whitelisted). Swept every other `request.form.get("next")` / redirect call
site in `webui/app.py` for the same class of bug: `seeing_override_save`/
`seeing_override_clear` (config_page-only, whitelist already correct) and
`hotpixel_capture`/`hotpixel_clear` (narrower two-way switch matching their
actual template usage exactly) were checked and found already consistent —
left unchanged.

### v0.11.34 — diag_solve.py --bundle silently ignored --fov/--sigma/--fov-err/--timeout

Found while confirming CLI-arg handling: `tests/diag_solve.py --bundle X.zip
--fov 13.5 --sigma 10` displayed `FOV=13.64° ... sigma=4.0` — the bundle's
own recorded `effective_params.json` values, not the CLI args. Root cause:
the bundle-hydration block (lines ~165-186) unconditionally overwrites
`sigma`/`fov`/`fov_err`/`timeout` from the bundle *after* they were set from
CLI args, and the "explicit CLI overrides win over config / bundle" section
right after it only actually re-applied `--bin`/`--backend` — the comment's
claim was false for the other four flags. Reproduced empirically (zipped a
real debug bundle from a prior session, ran the script twice with/without
the fix) rather than just reading the code.

**Fix**: `--fov`/`--fov-err`/`--sigma`/`--timeout` are now re-applied after
the bundle block too, guarded with `is not None` (not truthiness — 0 is a
value a caller could legitimately pass and a falsy check would silently
discard it). Verified both directions: CLI values now win when passed,
and the bundle's own values are still used when they aren't.

User-reported live, immediately after the v0.11.32 fix landed: the Factory
reset button now got as far as actually running the script (proving the
v0.11.32 self-update/resync fixes worked), but failed with
`ModuleNotFoundError: No module named 'diofinder'` from the embedded
`"$VENV_PY" -c "from diofinder.conf_migrate import factory_reset; ..."`
call. Root cause: the `diofinder` package is never `pip install`-ed into the
venv — every existing entry point relies on either `-m module` execution
from `WorkingDirectory=$DIOFINDER_DIR` (the systemd units) or a cwd/
PYTHONPATH that already includes it (`diofinder-update` does `cd
"$DIOFINDER_DIR"` near the top and never leaves it). `diofinder-factory-reset`
was the one new script that invoked a bare `python -c` with neither — and
since it's launched detached via `systemd-run` from the webui, its cwd has
no reason to already be `$DIOFINDER_DIR`. Fixed by setting
`PYTHONPATH="$DIOFINDER_DIR"` for that one invocation. Audited every other
`venv/bin/python -c`/`-m` call in `install.sh`/`firstboot.sh`/
`diofinder-update` for the same landmine — all the others either import
pip-installed third-party wheels (`tetra3`, `tetra3rs`, `star_detect`, no
cwd dependency) or already run after an established `cd`/`-m` context, so
this was an isolated bug scoped to the one new script.

### v0.11.32 — diofinder-update self-update guard (post-v0.11.31 field report)

User-reported live: clicking the new v0.11.31 "Factory reset" button gave
`sudo: /usr/local/bin/diofinder-factory-reset: command not found`, despite
the webui clearly running v0.11.31 code (the card/route only exist there).
Root cause: the device's ALREADY-INSTALLED (pre-v0.11.31) `diofinder-update`
performed the OTA — it git-synced the v0.11.31 checkout onto disk (new
script, new sudoers file, all present in `/opt/diofinder`), but the CLI
wrapper/sudoers *resync logic that would install them* is itself part of the
diofinder-update script, and the copy that was RUNNING was still the OLD
one, whose wrapper list didn't know `diofinder-factory-reset` existed yet
(and which had no sudoers-resync step at all, pre-v0.11.31). Any release
that adds a new wrapper name or sudoers drop-in has this exact gap.

**Fix**: `diofinder-update` now re-execs itself (`exec bash
$DIOFINDER_DIR/scripts/diofinder-update`, guarded by
`DIOFINDER_UPDATE_REEXEC` against a loop) immediately after checkout if the
freshly fetched script differs from the one currently running — so a single
`diofinder-update` invocation always finishes using up-to-date resync logic,
never a second run required. Immediate fix for anyone already stuck on
v0.11.31: run `sudo diofinder-update` once more (the first run already
resynced `diofinder-update` itself, so the second run uses the new logic and
finishes installing what was missing), or manually:
```bash
sudo install -m 755 /opt/diofinder/scripts/diofinder-factory-reset /usr/local/bin/diofinder-factory-reset
sudo install -m 440 /opt/diofinder/etc/sudoers.d/diofinder-factory-reset /etc/sudoers.d/diofinder-factory-reset
```

### v0.11.31 — webui "Factory reset" control

User request: a one-click way to restore all settings to fresh-image values
from the webui, available in both novice and expert mode.

- **`diofinder/conf_migrate.py::factory_reset()`**: overwrites the live conf
  with the shipped `diofinder.conf.default` **verbatim** (a full-file
  replace, not a per-key merge like `save_keys`), using the same
  `.lock`-sidecar + temp-file + `os.replace` discipline. Unit-tested in
  `tests/test_conf_migrate.py` (verbatim overwrite, works when the live conf
  is missing, raises when the default can't be found, leaves no `.tmp`
  behind).
- **`scripts/diofinder-factory-reset`** (new, root-only via a scoped sudoers
  drop-in mirroring `station.sh *`): calls `factory_reset()`, re-chowns the
  conf + its `.lock` to `diofinder:diofinder` (it ran as root — the same
  directory-ownership bug class as v0.11.30, just scoped to two files),
  optionally deletes `seeing_overrides.json` / `hot_pixel_mask.npz`
  (`--clear-overrides` / `--clear-hot-pixel-mask`), then restarts
  `diofinder.service` and `diofinder-webui.service` so restart-only keys
  (detect_bin, sensor mode, tuning file) actually take effect.
- **webui**: `/factory_reset` (Config page → "Factory reset" card, a plain
  `.card` — deliberately **not** `expert-only` — with two default-checked
  clear-artifact checkboxes and a JS confirm) fires the script detached via
  `systemd-run`, exactly like `/update` does, since the script restarts the
  very webui servicing the request. `/api/factory_reset/log` polls a log
  file for the running page, mirroring the update-log pattern.
- **`diofinder-update`** gained two general fixes needed to actually ship
  this to already-deployed devices: the CLI-wrapper resync loop now includes
  `diofinder-factory-reset`, and a new sudoers-drop-in resync step (with a
  `visudo -cf` syntax gate — a malformed drop-in breaks ALL sudo on the box)
  was added, since OTA previously never resynced `/etc/sudoers.d/*` at all.

### v0.11.30 — /etc/diofinder directory-ownership fix

Reported live: every webui "Save" (and `:St`/`:Sg`, alignment persist, auto
calibration commit — any path through `config.save_keys`) failed with
`PermissionError: [Errno 13] Permission denied:
'/etc/diofinder/diofinder.conf.lock'`.

Root cause, in `scripts/install.sh` and `scripts/firstboot.sh`: both did
`mkdir -p /etc/diofinder` as root with no `chown` on the directory itself —
only the `diofinder.conf` file inside it got `-o/-g $DIOFINDER_USER`. Since
v0.11.20, `save_keys()` writes via a same-directory temp file + `os.replace`
and serializes with a sibling `.lock` file (both **new** files needing
directory-level write permission, not just file-level) — so on a
root:root 755 directory, the unprivileged `diofinder` user (which owns and
runs both `diofinder.service` and `diofinder-webui.service`) could edit the
existing conf file directly but could never create the `.tmp`/`.lock`
siblings next to it. This has silently broken every settings-persist path on
every device provisioned by the installer since v0.11.20.

**Fix**: `install.sh` now `chown`s `/etc/diofinder` itself (unconditionally,
not just on first install, so upgrading an already-broken device also
repairs it); `firstboot.sh` (runs every boot, already idempotent by design)
also self-heals it, so an already-deployed device fixes itself after a
`diofinder-update` + reboot — no reimage required.

**Immediate workaround for an affected device** (before the next update):
```bash
sudo chown diofinder:diofinder /etc/diofinder
```

### v0.11.29 — auto-exposure peak-floor deadlock fix

Traced from real device logs (a debug bundle spanning three re-acquisition
episodes, 146/45/198 consecutive failed solves) while investigating whether
efficiency work should target the solver's hint/blind-fallback path or the
auto-exposure controller. The 146-fail episode (~4.5 min) showed `peak`
pinned at 20-22 — deep under `auto_exposure_peak_floor` (70) — while the
lost-in-space star-count fallback reported 183-353 "stars" (0 matches, ever):
at that peak, a `sigma * noise` threshold against a MAD-floored noise
estimate (0.50 DN) sits only ~2.5 DN above background, trivial for
quantization/read noise near black to trip. `_auto_exposure_decision`
(comms_proc.py) treated the inflated count as "over-served" and the
low-contrast guard correctly suppressed the reduction that branch would
otherwise apply — but nothing forced a *raise*, since the raise branch
(step 3, "starved") is only reached when the metric reads low, which it
never did. Net effect: a genuinely starved, unsolved frame could get stuck
holding forever. (The 45-fail/198-fail episodes in the same bundle were a
different, already-fixed issue — pre-v0.11.18 gain-hunting oscillation;
stale data, not a live concern.)

**Fix**: `_auto_exposure_decision` now forces the starved/raise branch
whenever `not solved and low_contrast`, regardless of what the star-count
metric reads — peak, not the noise-corrupted count, drives the decision at
that operating point. Four new cases in `tests/test_auto_exposure.py`
reproduce the exact deadlock (forced gain raise, forced exposure stretch at
max gain, at-ceiling no-op, and a sanity check that the *solved* low-contrast
case is unaffected).

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
  graduates from experimental/default-off. **Graduation gate exists** and is
  now clickable in the web UI (v0.11.27): the Camera page → *Experimental A/B*
  card runs `ab_tracking.run_ab` in a webui background thread and also exposes
  the two live default-off toggles (tracking mode, P5 bin-at-submit) that were
  previously CLI-only. `tests/ab_tracking.py` (`diofinder-ctl ab-tracking`)
  remains the CLI equivalent. Run it clear-sky before flipping the default.
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
- **olive-solve's hint-fallback re-enumeration** (measured 4.8×-120× cost per
  hint-rejected attempt vs. blind, `olive-solve/tetra3/src/solver.rs` two-pass
  loop ~1973-2116): considered and rejected. diofinder drops the attitude hint
  entirely once `fail_streak >= 5` (`solver_proc.py` ~line 1453), so the
  redundant re-enumeration can only occur on frames 1-5 of a reacquisition
  episode — never later, never in steady state. At those absolute magnitudes
  (microseconds-to-single-digit-milliseconds per attempt) the total waste is
  tens of milliseconds at most per episode, against episodes that in real
  device logs ran for minutes and were dominated by the auto-exposure
  peak-floor deadlock (fixed v0.11.29). The engineering risk of the fix
  (shared mutable undistorted-centroid state refined across passes, the
  parallel-search determinism guarantee) is disproportionate to that payoff.
  Revisit only if a future measurement shows the bound above no longer holds
  (e.g. the fail-streak threshold changes, or hinted attempts get
  meaningfully more expensive).
- **Deep-sky-object (Messier/NGC/etc.) catalog.** diofinder is a *pointing*
  finder, not a planning tool; its actual client (SkySafari, via LX200) already
  owns the full DSO catalog and displays it once diofinder reports where the
  scope is pointed. Duplicating that catalog here (beyond the existing
  brightest-star `star_names.csv` "centered star" label) would be redundant
  with the client's job for no user-facing gain.
- **HTTPS/TLS for the webui.** It's a local AP/home-network tool with no
  public exposure; self-signed-cert warnings and the mDNS/cert-hostname
  mismatch aren't worth the complexity here. Revisit only if the device is
  ever exposed beyond a trusted local network.
- **ASCOM/INDI protocol support alongside LX200.** LX200 already covers the
  real client (SkySafari); a second pointing-protocol surface is ongoing
  maintenance burden (another server, another set of quirks) for no
  additional user-facing capability.
- **Auto-focus motor control.** There is no focuser hardware interface
  anywhere in this project — `/focus` is a manual-focus *assistant* (Laplacian
  variance + zoomed crop), not a driver. Motorized autofocus is a new
  hardware project (stepper-driven focuser + driver), not a software feature
  of a finder scope; out of scope unless that hardware exists.
- **Full per-pixel dark-frame subtraction** in `camera_proc.py` (subtract a
  captured dark from every frame before publish). `TODO.md`'s own assessment
  already concluded the existing hot-pixel repair (`hot_pixel.py`, 8-neighbor
  mean fill from a capped-lens dark capture) covers the dominant fake-star
  case for a finder; the added per-frame cost isn't justified by the marginal
  accuracy gain full subtraction would add.

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
