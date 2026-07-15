# 2026-07-15 — epoch boundary shipped; mount-sync + field-networking specs

**Date:** 2026-07-15
**Session:** nice-volta (`claude/nice-volta-8iksyl`)
**Session URL:** https://claude.ai/code/session_01BqLSFGHV6GcjKKCBLji9H7
**Repo:** mconsidine/diofinder (branch `olive`)

This record exists so a fresh session can pick up where this one left off with no
loss of context. The durable task queue is **AGENTS.md §7**; the deep design
lives in **`docs/onstep-design.md`** and **`docs/networking.md`**. This is the
index + the "why" behind them.

## What shipped this session (released)

Each culminated in a release cut from `olive` (image workflow, `build_image:true`):

- **v0.11.49** — background-preview solver op (legibility step 3/3). Replaced the
  webui's reimplemented `_compute_background` with `bg_cache.preview_background()`
  + `SOLVER_OP_BG_PREVIEW`; the Background page can now render the solver's live
  cached `temporal_median` stack (which exists only in the solver process).
- **v0.11.50** — UI/diagnostics batch: live `imu_rate_gate_dps` setter (Camera
  page slider — the fix for parked-scope SkySafari jitter), debug-bundle burst
  fix (was returning ~5-6 of 12 frames), honest live-view message
  (`/api/liveview_health`), "flat-field preview" relabel. Also carries the
  shelved `tests/ab_plane_background.py` plane-fit experiment.
- **v0.11.52** — low-horizon resilience: **threaded LX200 server**
  (`_serve_lx200_client`, cap `_LX200_MAX_CLIENTS`=8, `_align_lock` around the
  shared `align_response_q`) so a blocking `:CM#` or half-open phone can't starve
  `:GR/:GD` polls (the broken-pipe storm); `pointing_age_s`/`pointing_stale`
  (`_POINTING_STALE_S`=10) so the web UI flags a held-but-old crosshair.
- **v0.11.51** — web UI info tooltips (`_macros.html` `tip()` macro, night-vision
  CSS, tap/hover/focus popover). Applied to Camera/Advanced, Home, Background,
  Config.
- **v0.11.53** — **report JNow to SkySafari (epoch-consistent boundary).** The
  headline correctness fix; see below.

## v0.11.53 — the epoch boundary (the load-bearing decision)

**Problem.** diofinder solves in the **catalog frame = Gaia DR3 + Hipparcos =
ICRS ≈ J2000**, and applied **no precession anywhere**. SkySafari's LX200 link
(confirmed set to "Use Current Epoch" = **JNow**) and OnStepX both work in JNow.
So the crosshair carried a real **~15-22′ (2026)** offset that only a local
`:CM#` align was hiding (the align quietly absorbed it into the boresight).

**How we determined the frame.** Reviewed `astro_databases` DB-build code: Gaia
DR3 + Hipparcos, proper-motion-propagated to epoch 2026.0. Position epoch
(proper motion) ≠ frame rotation (precession) — the frame stays ICRS/J2000
regardless of the PM propagation date. So the fix is precession at the I/O
boundary, not a catalog change.

**Why boresight doesn't compensate.** Boresight is a **constant 2-DOF pixel
offset**; precession is a **position-dependent frame rotation** (varies with
where you point). A single align cancels it at one sky position only — slew away
and the error returns. So the conversion must be global, not folded into
boresight.

**Decision.** New pure `diofinder/precession.py` (IAU 1976, Meeus ch. 21,
`math`-only — no numpy/astropy; nutation/aberration deliberately omitted as
negligible on a 13.6° finder). Convert **only at the comms boundary**:
- outbound `:GR/:GD` + status `report_ra_deg/dec_deg`: J2000→JNow (`_report_radec`,
  covers the IMU-predicted path too)
- inbound `:CM#` align target: JNow→J2000 (`_do_alignment`) so the align stays
  epoch-consistent and boresight settles to its true mechanical value.
Everything internal (solver, `imu_ref`, boresight, calibration) stays J2000.

**Python vs Rust:** Python. Cost is a once-per-report 3×3 rotation (a handful of
trig ops), not per-frame — nowhere near needing Rust.

**Kill switch:** `report_epoch: j2000` (config / `solver_params_set`, like
`imu_exact_predict`) reverts to the raw J2000 frame.

**Field note for the user:** on update, pointing shifts by the precession amount
— **re-align once**, and the boresight will now settle smaller/truer.

Pinned by `tests/test_precession.py` (round-trip <0.004″) + `tests/test_report_epoch.py`.

## What's designed but NOT built (next-session pickup)

Both are doc-only on `olive` — no release, no code beyond the docs.

- **`docs/onstep-design.md`** — outbound **mount-sync output**. The finder is
  currently an LX200 *server* only (no `socket.connect` in `diofinder/`). Spec:
  dialect-pluggable `MountLink`, `_onstep_loop` comms thread, manual-default +
  gated-auto push, **sync-only (no GoTo) v1**, OnStepX/LX200 as the v1 target.
  **Epoch prerequisite is already solved** — the sync reuses v0.11.53's
  `_report_radec` / `jnow_to_j2000` verbatim. This is the most-likely next
  feature; it was the reason v0.11.53 was cut when it was.
- **`docs/onstep-design.md` §11** — **SkyWatcher / multi-mount.** SkyWatcher
  speaks **SynScan, not LX200**; recommendation is an **ASCOM Alpaca** second
  `MountLink` dialect (HTTP/JSON, mount-agnostic). Build after the OnStep LX200
  dialect proves the seam.
- **`docs/networking.md`** — field-networking topologies. Today's boot behaviour
  (AP default at fixed `10.42.0.1`, `station.sh` to join, **boot-only**
  `diofinder-ensure-ap` fallback, `diofinder.local` mDNS, USB serial console)
  works. Key finding: the "keep the phone's internet **and** talk to the finder"
  goal is best served by **role reversal** (phone = hotspot, finder = station via
  `station.sh`) — works on iOS **and** Android; **Bluetooth PAN is a dead end
  because iOS doesn't support it.** Three deferred UX items, priority order:
  1. **mid-session AP-fallback watchdog** (highest value — closes the boot-only
     gap so a dropped station link brings the self-AP back mid-session)
  2. show the current station IP prominently (SkySafari wants a numeric IP)
  3. a "join my phone's hotspot" WiFi-page helper.

## Also this session (no code)

- Diagnosed IMU parked-jitter → `imu_rate_gate_dps` too low (shipped the live
  slider in v0.11.50). User confirmed `solve_timeout_ms=1500` helped the
  low-horizon broken-pipe cluster (solver saturation).
- Reviewed the `enhanced` branch of `eFinder_cli` for OnStep prior art (fed the
  design doc). The `tinySS` branch was a mis-recall by the user.

## Working-state at handoff

`olive` = v0.11.53 released, no unreleased code. The only post-v0.11.53 delta is
these docs (`docs/networking.md`, this record, AGENTS.md §6/§7 updates) — a
**doc-only PR, no release** per the established pattern.
