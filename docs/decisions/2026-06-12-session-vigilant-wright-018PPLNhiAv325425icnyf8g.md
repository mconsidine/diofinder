# Session Decision Record — diofinder

**Date:** 2026-06-12 13:50 UTC (session spanned 2026-06-04 → 2026-06-12)
**Session name:** vigilant-wright (assigned branch slug `claude/vigilant-wright-d9ndN`)
**Session ID:** `018PPLNhiAv325425icnyf8g`
**Session URL:** https://claude.ai/code/session_018PPLNhiAv325425icnyf8g
**Branch:** `olive` (final); early work on `claude/vigilant-wright-d9ndN` and
`claude/olive-bg-solve-rate` was folded into `olive` and those branches deleted.
**Goal:** robust star detection under light pollution / glare / skyglow, fast,
with A/B tooling so background-mode choices are evidence-based.

---

## Decisions

1. **`olive` is the single line of development**; short-lived dev branches are
   cut from current `olive`. Parallel Claude sessions must fetch+rebase before
   pushing (this session collided with concurrent sessions several times).
2. **Solve-rate, not star count, is the decision metric** for background-mode
   tuning. On-device solving for A/B reuses the live solver's resident
   database via a `solve_centroids` maint command (no second DB → memory-safe
   on 512 MB). A second in-process DB load was explicitly rejected (OOM risk).
3. **Images must be OTA-capable.** `/opt/diofinder` is git-provisioned at image
   build (`DIOFINDER_REPO_URL`/`DIOFINDER_GIT_REF` from CI context); copied-tree
   installs cannot self-update.
4. **Defaults:** `detect_sigma=5.0` (range 0–20), `gain=5.0`, `detect_bin=2`.
5. **CPU layout:** comms/webui/IMU/launcher on CPU 0 (shared with kernel,
   I/O-bound); solver on CPUs {1,2,3} (`cpu_solver_aux` key) with 3
   `star_detect` threads. Chosen over solver `{0,2,3}` to avoid IRQ contention
   without needing a benchmark.
6. **UI forms apply live AND persist** with a single "Apply & save" button
   (persist checkbox removed); sliders still live-apply without saving.

## Assessments

- Full repo audit found `auto_exposure_*` was vaporware (config keys + UI text,
  zero implementation) → implemented this session.
- Webui never actually pinned itself (docs claimed CPU 1; it floated) → fixed.
- Recurring `diofinder-update` "Local modifications" failures had two root
  causes: (a) git dubious-ownership when the check ran as root, (b)
  `firstboot.sh` committed 100644 but chmod-755'd in place at install →
  permanent mode-diff. Both fixed.
- `HAS_TOPHAT` UnboundLocalError (import-after-use in `solver_main`) was
  crash-looping every boot on olive → fixed (one-line reorder).
- Hot-path review: frame-copy chain is deliberate (camera never blocked by
  solve latency) — kept; `/frame.jpg` was the heaviest webui load.

## Actions (key commits, all on `olive`)

- `37f2e54` HAS_TOPHAT crash fix · `b34689f` firstboot.sh 100755
- `fd0d24b` OTA git-provisioning (install.sh graft, build-image.sh,
  release.yml, owner-safe diofinder-update with `--ref BRANCH`)
- `2d0f633` `diofinder-bg-test --solve` · `1482e55` `bg_cache_status` maint cmd
  + completed `solve_centroids` solver-side handler
- `8584bc3` Background preview (`/bg.jpg`) + Camera detection-view toggle
  (`/frame.jpg?sub=1`), 7-mode aware · `425c51c` capture→solve→zip A/B webui
- `54db072` Background nav link · `4eabcc0` Update-page branch/tag field
- `3498c55` diag_background: all 7 bg modes, `--modes/--block-size/--uniform-size`
- `89d4f8a` removed dead `gate_mode` from 6 test scripts; fixed diag_camera
  `Picamera2(tuning_file=)` → `load_tuning_file()`; added `--info-only` +
  `camera_settings.txt` dump
- `984e25c` sigma/gain defaults 5/5; single Apply&save; Config page shows all
  keys; conf.default carries all keys
- `8f67636` auto-exposure controller (comms thread, deadband + saturation
  backoff, unit-tested decision fn); sigma 0–20; `tuning_set`
  scientific↔standard toggle
- `5a365cf` speed pass: `detect_bin=2` default, CPU re-pin {1,2,3}+CPU0,
  half-res `/frame.jpg` + mtime-cached config + 1250 ms poll, bg_cache submit
  decimation (every 4th in STEADY), `[::2,::2]` peak gate, `performance`
  governor in firstboot.

## Recommendations / outstanding

- On-device verification pending (sandbox is x86): bin=2 solve-rate parity,
  3-core speedup (`bench_pipeline_combos.py --live-shm`), auto-exposure
  convergence, governor persistence.
- Devices with old conf files keep old `cpu_*`/`detect_bin` values — update
  `/etc/diofinder/diofinder.conf` or re-image.
- Not built: "view the temporal cache's stacked frame as an image" (cache
  stores per-row model only).
- BNO055 errno-104 re-probes: non-fatal, watch I²C wiring if persistent.
