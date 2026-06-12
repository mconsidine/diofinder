# Session Decision Record — tetra3rs-mp viability assessment

**Date:** 2026-06-12  
**Session name:** claude/assess-tetra3rs-performance-F6f17  
**Session ID:** session_01U9c6UFBSawfSZMeMr8ZcJB  
**Repo:** mconsidine/efinder_cli_tetra3rs_mp  
**Branch:** claude/assess-tetra3rs-performance-F6f17  
**File assessed:** `Solver/eFinder_tetra3rs_mp.py` (1297 lines, version 6.6-tetra3rs-mp-tb8)

---

## Purpose

Assess whether `eFinder_tetra3rs_mp.py` is viable in terms of speed and efficiency as a
replacement for the cedar-based implementation in `efinder_cli_new`, and identify any
features present in the cedar version that are missing here.

---

## Assessment: Speed & Efficiency

### Verdict: Viable and well-engineered for Pi Zero 2W

The architecture eliminates the main performance bottleneck of the cedar implementation
(separate gRPC `cedar-detect` server process + IPC/serialization round-trip) by running
Rust centroid extraction and plate solving in-process via `tetra3rs`.

### Hot-path design (strengths)

| Feature | Detail |
|---------|--------|
| 3-process split with CPU pinning | camera→{0}, lx200→{1}, solver→{2,3}; keeps solver's numeric loop off IRQ/socket cores |
| Triple-buffered SHM frame handoff | `(latest_slot, frame_seq)` publish; solver never races camera, never re-solves same frame |
| Zero-IPC RA/Dec hot path | `:GR`/`:GD` reads `Value(c_double)` directly; no queue, no lock on reply path |
| TCP_NODELAY | Eliminates 40 ms Nagle coalescing on SkySafari polls |
| sleep(0.02)→0.001 | 20x reduction in per-command latency floor |
| Tracking mode via `attitude_hint` | Seeded solve (2 s timeout) vs blind (5 s); skips 4-star hash search after first lock |
| Background state writer (2 Hz) | JSON serialize+write offloaded to daemon thread; hot path does in-memory snapshot only |
| Background live JPEG writer | Pillow contrast/rotate/encode (40–80 ms on Pi Zero 2W) off the solve loop entirely |
| Size-1 drop-newest queue for live JPEG | Renderer always works on newest frame; no backlog |
| Peak via 5×5 window | 25 px read vs 730k-element full-frame `np.max()` |
| FOV self-calibration | Rolling 5–20 sample mean → `fov_max_error_deg` tightens from 1.0° to 0.3° once warm |
| Prewarm | tetra3rs scratch buffers allocated before first real frame |
| Restart-on-death supervisor | `proc_specs` dict allows clean reconstruction of any crashed child |

### Concerns / opportunities identified

1. **`time.sleep(0.05)` in camera loop** caps frame rate at ~20 Hz regardless of exposure.
   With Exposure=0.5 s harmless; with Exposure=0.1 s this is the binding limit. Should
   gate on actual capture completion rather than a fixed sleep.

2. **`_apply()` calls `picam2.stop()/start()` on every exposure/gain change** — 100–200 ms
   hiccup. Most libcamera controls can be set live without a restart; worth verifying for
   `ExposureTime` and `AnalogueGain`.

3. **`except Exception: pass` for `queue.Empty`** — functional but `queue.Empty` is the
   clean form for the get_nowait break pattern in the camera and solver command drains.

4. **`latest_slot` and `frame_seq` updated in separate locks** — a reader between the two
   lock releases can observe new-slot + old-seq. Documented as harmless via triple-buffer
   guarantee, but the "skipped frames" counter can be off-by-one. Could be fixed by
   combining into a single `(slot, seq)` Value or using a single lock covering both writes.

5. **`_pin_cpu('main')` pins supervisor to core 0 alongside camera** — main is idle so
   it's harmless; worth noting if background work is ever added to main.

6. **`set_start_method('fork')` is correct but requires vigilance** — no library that
   allocates GPU/DMA state should be imported at module top level before fork. Currently
   satisfied (picamera2 and tetra3rs imported inside child functions).

7. **`auto_exp` has no rate limit** — `for _ in range(20)` loop, each iteration doing a
   `_set_camera` + camera capture, can hold the solver command queue for up to ~40 s.
   The `lx200_result_q.get(timeout=60.0)` timeout accommodates this but blocks all other
   on-demand commands during the exposure search.

---

## Feature parity check status

### Goal
Verify that all features recently added to `mconsidine/efinder_cli_new` (cedar-based)
are present in this tetra3rs implementation.

### Outcome
**Incomplete — blocked by MCP access scope.**

The GitHub MCP server for this session was scoped to `mconsidine/efinder_cli_tetra3rs_mp`
only. `mconsidine/efinder_cli_new` could not be read. An attempt was made to expand scope
via web UI settings; the user was pursuing resolution in a parallel session at close.

### Known gap identified from README vs code inspection

| Command | Status | Note |
|---------|--------|------|
| `:GA#` (get altitude) | **MISSING** | Documented in README as "Get scope altitude (requires ADXL343 accelerometer)" but no `elif cmd == 'GA':` branch exists in `lx200_process`. Hardware (ADXL343 on I2C) is listed in README hardware requirements. |

All other commands in the README diagnostic table (`:PS#`, `:GV#`, `:GS#`, `:GK#`, `:Gt#`,
`:GO#`, `:SO#`, `:OF#`, `:GX#`, `:SX#`, `:TS#`, `:TO#`, `:IM#`) are implemented.

---

## Recommendations

### Immediate (before next field use)

1. **Implement `:GA#` accelerometer command** — add ADXL343 I2C read in `lx200_process`
   and a corresponding `get_altitude` handler. This is in the README and hardware BOM
   but absent from the code.

2. **Complete cedar feature parity check** — once MCP access to `efinder_cli_new` is
   available, diff the two implementations systematically and port any missing features.

### Short-term performance improvements

3. **Remove `sleep(0.05)` from camera loop** — replace with a capture-completion gate
   (e.g. block on `picam2.capture_array()` return, which already blocks for the exposure
   duration). This removes the artificial 20 Hz cap when using short exposures.

4. **Live set of camera controls** — test whether `picam2.set_controls(...)` can update
   `ExposureTime`/`AnalogueGain` without `stop()/start()`. If so, remove the restart
   from `_apply()` to eliminate the ~150 ms hiccup on every exposure change.

5. **Fix `latest_slot`/`frame_seq` dual-lock race** — wrap both updates in a single lock
   or use a combined `ctypes.c_uint64` packing slot in the low byte to make the publish
   atomic without two separate lock acquisitions.

6. **Rate-limit `auto_exp`** — add a per-step timeout or cap the binary search at 5–6
   iterations rather than 20 to keep the command queue responsive.

---

## Session actions taken

- Read and analysed `Solver/eFinder_tetra3rs_mp.py` in full
- Read `README.md` and cross-referenced documented LX200 commands against implementation
- Identified `:GA#` as the one documented command with no implementation
- Attempted to expand MCP repo scope to access `efinder_cli_new`; blocked by session
  configuration; user pursuing resolution separately
- No code changes were committed in this session

---

## Next session checklist

- [ ] Confirm `mconsidine/efinder_cli_new` is in MCP allowlist
- [ ] Diff cedar vs tetra3rs implementations — identify all feature gaps
- [ ] Implement `:GA#` ADXL343 altitude command
- [ ] Implement any additional features found in cedar diff
- [ ] Apply short-term perf fixes (items 3–6 above) if desired
