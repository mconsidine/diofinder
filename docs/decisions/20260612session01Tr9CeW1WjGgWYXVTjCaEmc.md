# Session Decision Log

| Field | Value |
|---|---|
| **Date** | 2026-06-12 |
| **Session ID** | `01Tr9CeW1WjGgWYXVTjCaEmc` |
| **Session name** | admiring-franklin |
| **Session URL** | https://claude.ai/code/session_01Tr9CeW1WjGgWYXVTjCaEmc |
| **Branch** | `claude/admiring-franklin-VQXnN` |
| **Repo** | `mconsidine/efinder-combo` |

---

## Decisions and changes (chronological)

### 1. Config page redesign

**Decision**: Replace the flat key=value config dump with a structured, sectioned
view.

**Implementation**:
- Added `_CONFIG_SECTIONS` constant in `webui/app.py` — 9 sections
  (Camera, Optics/FOV, Observer Location, Star Detection, Plate Solving,
  Boresight, Communications, CPU Affinity, Diagnostics/Shutdown) with 35
  key/label/description tuples
- Added `_fmt_val()` helper for consistent value formatting
- Rewrote `/config` route: loads live config vs compiled defaults, marks
  keys with an **edited** badge wherever the file differs, queries daemon
  maint socket for runtime status (backend, test mode, IMU, FOV, boresight)
- Complete rewrite of `webui/templates/config.html`: card-per-section layout
- New CSS classes in `webui/static/style.css`: `.config-table`,
  `.config-badge`, `.config-modified`, etc.  Mobile-responsive.

---

### 2. CPU affinity correction

**Assessment**: The IMU thread is a daemon thread started inside the launcher
process (`efinder_main.py`).  Without pinning the launcher, the IMU thread
floated across all four cores — defeating the CPU isolation strategy.

**Decision**: Pin the launcher process to `cpu_comms` (CPU 1) immediately
after `load_config()`.

**Implementation** (`efinder/efinder_main.py`):
```python
cfg = load_config()
os.sched_setaffinity(0, {cfg.cpu_comms})
```

**Also fixed**: Stale defaults in `etc/efinder.conf.default`:
- `cpu_solver: 3` → `2`
- `detect_sigma: 8.0` → `9.0`
- `detect_use_binned: false` → `true`

**CPU layout (current)**:

| CPU | Role |
|---|---|
| 0 | Linux kernel, IRQs, sshd, NetworkManager |
| 1 | `efinder_main` launcher + IMU thread · `comms_proc` · `efinder-webui` |
| 2 | `solver_proc` + `cedar-detect-server` |
| 3 | `camera_proc` alone |

---

### 3. IMU dashboard "always calibrating" fix

**Root cause**: Dashboard IMU card used static Jinja2 text that was never
updated by the JS refresh loop.

**Fix**: Added `id="imu-status-text"` to the paragraph element and updated
the JS refresh loop to rewrite its text and style dynamically:
- Active: orange "Active — smoothing SkySafari updates"
- Inactive: muted "Detected — calibrating…"

**Also added**: explanatory hint text — "To activate: move scope ≥0.1°
between 3 successful solves."

---

### 4. IMU attitude-hint propagation (Option C)

**Assessment**: The BNO055 quaternion output is NOT directly used as the
tetra3rs `attitude_hint`.  Prior to this change, the hint was the raw
last-solve sky quaternion with a fixed 0.1° window — useful only when the
scope is stationary.

**Decision**: Implement IMU-propagated hint (Option C) as a proof of concept.

**Algorithm** (`efinder/solver_proc.py`, `_imu_propagate_hint()`):
```
q_delta = q_imu_now ⊗ conj(q_imu_at_last_solve)   # body rotation since last solve
q_hint  = q_delta ⊗ q_last_sky                      # apply to sky quaternion
uncertainty = max(2°, 1.5 × rotation_angle)         # scales with motion
```

**Hint labels in journal**: `(imu)` / `(seeded)` / `(blind)` per solve.

**Caveat**: Assumes IMU body axes ≈ camera axes.  A badly-mounted IMU
widens the search window but never blocks a solve (`strict_hint=False`).

---

### 5. Live-view arcsinh stretch

**Problem reported**: User saw 10–20 stars through eyepiece but background
was too bright in the live JPEG — linear percentile stretch white-pointed at
sky background level for sparse starfields.

**Assessment**: The stretch is display-only; cedar-detect and tetra3rs read
raw SHM bytes and are completely unaffected.

**Decision**: Replace linear stretch with arcsinh sky-subtracted stretch.

**Implementation** (`webui/app.py`, `frame_jpg()`):
```python
sky  = float(np.median(frame))
x    = np.clip(frame.astype(np.float32) - sky, 0.0, None)
beta = max(1.0, sky * 0.1)
xs   = np.arcsinh(x / beta)
scale = float(np.percentile(xs, 99.9))
if scale < 1e-6:
    scale = float(xs.max()) or 1.0
stretched = np.clip(xs / scale * 255.0, 0, 255).astype(np.uint8)
```

**Why arcsinh**: Linear in the faint-signal regime (preserves relative star
brightness), logarithmic for bright signals (prevents blowout), insensitive
to the sky background level.

---

### 6. `tests/diag_camera.py` — new diagnostic script

**Purpose**: Exposure/gain sweep to find optimal camera settings before or
during a session.

**Key design decisions**:
- Filename: `YYYYMMDDHHMMSSMMM-EEE-GG[-2x2].png` — embeds all parameters
- ZIP bundling: single archive named `YYYYMMDDHHMMSSMMM.zip` (sweep start
  timestamp); `ZipFile.testzip()` integrity check before deleting PNGs
- `ZIP_STORED` compression (PNG already compressed; deflate adds overhead)
- Output dir: wherever `test.png` lives (`/var/lib/efinder` default)
- Optional 2×2 software binning via `--binning`

**ZIP diagnostic contents** (added in this session):
- `capture_info.txt` — sweep parameters, hostname, Pi model, OS, frame
  pipeline explanation, live daemon status from maint socket
- `efinder.conf` — verbatim copy of `/etc/efinder/efinder.conf` at capture
  time

**Transfer command** (printed by script at end):
```bash
scp efinder@efinder.local:/var/lib/efinder/YYYYMMDDHHMMSSMMM.zip .
```

---

### 7. Frame pipeline clarification

**Question**: Is 960×760 a crop or a downscale of the native 4056×3040 sensor?

**Assessment**:
- picamera2 selects the 2×2 hardware-binned sensor mode (2028×1520) when
  asked for 960×760; the ISP then scales to 960×760
- **Full sensor area is always used — full FOV preserved, not a crop**
- Effective plate scale: ~50.8 arcsec/pixel (vs ~12"/px native)
- Stars remain point sources at this scale; downscale does not cause solve
  failures

**Recommendation for "10–20 stars visible but can't solve"**:
1. Run `diag_detect.py --sigma-sweep` — check actual extracted star counts
2. If < 8 stars at sigma=9, lower sigma on the Camera page (try 6–7)
3. If ≥ 8 stars but `NO_MATCH`: check FOV estimate, run
   `efinder-ctl calibration reset`
4. Capture frames with `diag_camera.py` for off-device analysis

---

### 8. Documentation updates (this session)

All changes committed to `claude/admiring-franklin-VQXnN`:

- **`README.md`**: Added §13 "Can't solve even though stars are visible"
  (sigma sweep, frame pipeline explanation, FOV reset, capture+scp workflow);
  expanded Camera page description; added arcsinh stretch to frame pipeline
  diagram; expanded `diag_camera.py` entry with ZIP contents and scp example;
  updated troubleshooting table
- **`tests/README.md`**: Fixed duplicate content (diag_bno055/services/detect/
  solve/bench sections appeared twice); expanded `diag_camera.py` section with
  ZIP extra files, frame pipeline note, scp transfer instructions; added
  arcsinh stretch note
- **`tests/diag_camera.py`**: Added `_build_info_txt()` and
  `_query_daemon_status()`; bundle `capture_info.txt` + `efinder.conf` in
  every ZIP; print scp command at end

---

### 9. GitHub Actions — workflow_dispatch "Failed to queue"

**Root cause 1**: `build_image` input defaulted to `false`; the only job's
`if` condition evaluated to `false` for every manual dispatch; GitHub refuses
to queue runs with zero runnable jobs.

**Fix**: Changed `build_image` default to `true`.

**Root cause 2**: The `if` condition used YAML block scalar (`|`) which embeds
literal newlines into the expression string — GitHub Actions expression parser
rejects multi-line expressions in job `if` fields.

**Fix**: Collapsed to a single-line expression with explicit boolean comparison:
```yaml
if: github.event_name == 'release' || (github.event_name == 'workflow_dispatch' && inputs.build_image == true)
```

**Root cause 3**: Both `release.yml` and `vendor-binaries.yml` had
`branches: [combo]` push triggers. The `combo` branch does not exist on
GitHub (only `main` and `claude/admiring-franklin-VQXnN`). Updated both to
`branches: [main]`.

---

### 10. GitHub Actions — `softprops/action-gh-release@v2` broken

**Symptom**: Release workflow failed with "An action could not be found at
URI .../softprops/action-gh-release/tar.gz/3bb12739..." — SHA no longer
accessible from GitHub's codeload CDN (tag force-pushed by maintainer).

**Decision**: Replace third-party action with `gh` CLI, which is pre-installed
on all GitHub-hosted runners and has no external dependency:

```yaml
- name: Attach to release
  env:
    GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
  run: |
    gh release upload "${{ github.event.release.tag_name }}" \
      "build/output/${{ steps.compress.outputs.imgname }}"
```

Applied to both `release.yml` and `vendor-binaries.yml`.

**Lesson**: Floating tags (`@v2`) in third-party actions are a reliability
risk.  Prefer pinning to a specific SHA or using first-party tooling.

---

## Open items / recommendations

- **Option C IMU hint**: proof-of-concept only; if SkySafari updates still
  feel jerky after large slews, consider Option B (calibrated C-matrix) or
  Option A (explicit mount calibration) for a more precise body→sky mapping
- **Auto-exposure**: star count per frame is already available; a feedback
  loop to hit a target star count has not been implemented
- **Sigma persistence**: default sigma=9 may be too aggressive for many
  setups; consider lowering the compiled default or adding a first-run wizard
- **softprops/action-gh-release** fully removed; no further action needed
- **Session archiving in Claude Code web**: currently one-way (no unarchive);
  GitHub issues [#41303](https://github.com/anthropics/claude-code/issues/41303)
  and [#50042](https://github.com/anthropics/claude-code/issues/50042) track
  the feature request

---

*Generated at 2026-06-12T14:09:52Z by Claude Code session admiring-franklin
(01Tr9CeW1WjGgWYXVTjCaEmc)*
