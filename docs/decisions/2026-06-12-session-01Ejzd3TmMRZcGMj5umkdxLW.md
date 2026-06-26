# Session Decision Record — diofinder

**Date**: 2026-06-12  
**Session**: `session_01Ejzd3TmMRZcGMj5umkdxLW`  
**Branch**: `claude/vigilant-brahmagupta-OYCJC`

---

## Context

Extended diofinder to support the four new sycamore bg_modes added this
session (column_percentile, row_column_percentile, block_percentile,
uniform_mean), added a new noise estimator selector, wired the IMX477
scientific tuning profile throughout the camera stack, and brought all
documentation and UI into sync with the expanded feature set.

---

## Changes Made

### 1. `diofinder/config.py` — three new config keys

```
camera_tuning_file:      str  = "/usr/share/libcamera/ipa/rpi/vc4/imx477_scientific.json"
detect_bg_block_size:    int  = 0
detect_uniform_filter_size: int = 0
detect_noise_mode:       str  = "mad"
```

**Decisions**:
- `camera_tuning_file` default points to the IMX477 scientific profile, which
  disables all ISP post-processing (AGC, AWB, noise reduction, sharpening,
  colour correction). Change `vc4` → `pisp` for Pi 5.
- `detect_bg_block_size = 0` means "use sycamore's internal default (32)".
- `detect_uniform_filter_size = 0` means "use sycamore's internal default (25)".
- `detect_noise_mode = "mad"` preserves existing behaviour; set to `"global_rms"`
  to replicate the tetra3/olive-solve pipeline.

---

### 2. `diofinder/bg_cache.py` — extended detection routing

**`CACHE_COMPATIBLE_MODES`** frozenset established:
```python
CACHE_COMPATIBLE_MODES = frozenset({"row_percentile", "line_median", "top_hat"})
```

**Decision**: The four new spatial-preprocessing modes (`column_percentile`,
`row_column_percentile`, `block_percentile`, `uniform_mean`) are explicitly
excluded from the cache-compatible set. They require a corrected full-image
array and cannot use the per-row cached offsets. Selecting any of them forces
the per-frame detection path.

`detect()` signature extended:
```python
def detect(self, image_u8, sigma, bg_mode, tophat_radius, max_axis_ratio,
           bg_block_size=0, uniform_filter_size=0, noise_mode="mad")
```

Per-frame routing passes the correct keyword for each mode:
- `block_percentile` → `bg_block_size`
- `uniform_mean` → `uniform_filter_size`
- both → `noise_mode` if not default

---

### 3. `diofinder/comms_proc.py` — extended maint socket API

`valid_modes` tuple expanded to all 7 modes:
```python
("row_percentile", "line_median", "top_hat",
 "column_percentile", "row_column_percentile",
 "block_percentile", "uniform_mean")
```

`solver_params_get` now returns: `detect_bg_block_size`, `detect_uniform_filter_size`,
`detect_noise_mode`.

`solver_params_set` validates and routes all three new keys:
- `detect_bg_block_size`: int, range [4, 256]
- `detect_uniform_filter_size`: int, range [3, 255]
- `detect_noise_mode`: one of `"mad"`, `"global_rms"`

---

### 4. `diofinder/solver_proc.py` — reads all new keys from `shared_cfg`

All three new keys read with fallback to `cfg` defaults and passed to
`bg_cache.detect()`.

---

### 5. `diofinder/camera_proc.py` — IMX477 scientific tuning + ISP controls

**Decision on tuning file fallback**: If the configured tuning file path
doesn't exist on the filesystem, log a `WARNING` and fall back to the default
libcamera tuning (pass no `tuning_file` argument to `Picamera2`). The
specified ISP controls (`NoiseReductionMode=0`, `Sharpness=0.0`,
`Saturation=0.0`) are applied in all cases and are sufficient to suppress
the most harmful ISP artefacts even without the scientific profile.

**Pattern applied**:
```python
tuning_file = getattr(cfg, "camera_tuning_file", "")
if tuning_file and not os.path.exists(tuning_file):
    log.warning("IMX477 scientific tuning file not found at %s — "
                "falling back to default tuning", tuning_file)
    tuning_file = ""
cam = Picamera2(tuning_file=tuning_file) if tuning_file else Picamera2()
```

ISP controls added to `create_still_configuration`:
```python
"NoiseReductionMode":  0,
"Sharpness":           0.0,
"Saturation":          0.0,
```

---

### 6. `webui/app.py` — bgtest_set route

Conditional parameter routing for each mode:
- `top_hat` → `tophat_radius`
- `block_percentile` → `bg_block_size`, `noise_mode`
- `uniform_mean` → `uniform_filter_size`, `noise_mode`

---

### 7. `webui/templates/bgtest.html` — full 7-mode UI

Dropdown now lists all 7 modes. Four conditional rows:
- `tophat-row` — visible for `top_hat`
- `block-row` — visible for `block_percentile`
- `uniform-row` — visible for `uniform_mean`
- `noise-row` — visible for `block_percentile` OR `uniform_mean`

Current Settings display now shows:
- `detect_bg_mode`
- `detect_tophat_radius`
- `detect_bg_block_size` (with "sycamore default 32" hint)
- `detect_uniform_filter_size` (with "sycamore default 25" hint)
- `detect_noise_mode`
- `detect_sigma`

---

### 8. `tests/diag_camera.py` — IMX477 tuning + ISP controls

Same tuning fallback pattern applied. `NoiseReductionMode`, `Sharpness`,
`Saturation` added to camera controls in the diagnostic sweep tool.

---

### 9. `CLAUDE.md` — documentation brought current

- Extraction section: all 7 bg_modes documented with sycamore version
  requirements; `noise_mode` selector documented; cache-incompatible modes
  explicitly called out.
- `shared_cfg` table: added `detect_bg_block_size`, `detect_uniform_filter_size`,
  `detect_noise_mode`.

---

## Assessment: what was audited and found complete

A comprehensive post-change audit was run. All functional wiring was found
correct. The gaps identified and fixed were documentation/UI only:

| File | Gap | Fixed |
|------|-----|-------|
| `CLAUDE.md` | 4 new modes not documented | ✅ |
| `CLAUDE.md` | 3 new shared_cfg keys missing from table | ✅ |
| `bgtest.html` | `detect_uniform_filter_size` not in current settings | ✅ |
| `bgtest.html` | `detect_noise_mode` not in current settings | ✅ |
| `bg_cache.py` | `CACHE_COMPATIBLE_MODES` comment unclear | ✅ |

---

## Recommendations

- Deploy a new sycamore 0.11.0 wheel to device before using `uniform_mean`
  or `global_rms` (these are v0.11.0 features; older wheels will raise
  `TypeError` on the unknown kwargs).
- `uniform_mean` + `global_rms` at `sigma=7` is the recommended starting
  point for matching olive-solve's extraction behaviour.
- `block_percentile` at `bg_block_size=32` is the recommended alternative
  when 2-D gradient removal is needed but cache compatibility is desired
  (note: block_percentile is still per-frame only).
- The scientific tuning file path uses `vc4`; change to `pisp` via
  `camera_tuning_file` in `diofinder.conf` if deploying on Pi 5 hardware.
- No capability probes exist in `bg_cache.py` for sycamore < 0.10.0 against
  `block_percentile`/`uniform_mean`/`column_percentile` — these will raise
  `TypeError` on old wheels. Add `HAS_BLOCK_PERCENTILE` / `HAS_UNIFORM_MEAN`
  probes if multi-version wheel compatibility is required.
