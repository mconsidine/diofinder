# Session Decision Record — eFinder_cli

**Date**: 2026-06-12  
**Session**: `session_01Ejzd3TmMRZcGMj5umkdxLW`  
**Branch**: `claude/vigilant-brahmagupta-OYCJC`

---

## Context

eFinder_cli uses a `Camera` class in `Solver/eFinder.py` that wraps
picamera2. This session applied consistent camera initialisation improvements
that were also applied to diofinder's `camera_proc.py`.

---

## Changes Made

### `Solver/eFinder.py` — IMX477 scientific tuning + ISP controls

**`Camera` class changes**:

1. **`TUNING_FILE` class constant** added pointing to the IMX477 scientific
   JSON profile at `/usr/share/libcamera/ipa/rpi/vc4/imx477_scientific.json`.

2. **Tuning file fallback in `__init__`** — same pattern as diofinder:
   ```python
   tuning = self.TUNING_FILE
   if tuning and not os.path.exists(tuning):
       print(f"WARNING: IMX477 scientific tuning file not found at {tuning} — "
             "falling back to default tuning")
       tuning = ""
   self._cam = Picamera2(tuning_file=tuning) if tuning else Picamera2()
   ```
   Uses `print()` for the warning (no logger in eFinder_cli, consistent with
   existing codebase style).

3. **ISP controls in `set()` method** — added to `set_controls()` call:
   ```python
   "NoiseReductionMode": 0,
   "Sharpness":          0.0,
   "Saturation":         0.0,
   ```

---

## Decision: tuning file fallback pattern

**Decision**: When the scientific profile is not found, log a warning and
proceed without it. The three explicit ISP controls (`NoiseReductionMode=0`,
`Sharpness=0.0`, `Saturation=0.0`) are sufficient to suppress the most
harmful artefacts even without the full scientific profile.

**Rationale**: A hard failure if the tuning file is missing would prevent the
finder from starting on systems without the complete libcamera IPA data,
which would be a worse outcome than running with default ISP tuning.

---

## Recommendations

- Change `vc4` → `pisp` in `TUNING_FILE` for Pi 5 hardware, or make it
  configurable via the existing config mechanism.
- The scientific profile suppresses AGC, AWB, noise reduction, sharpening,
  and colour correction — all of which corrupt star photometry. It should be
  present on any standard Pi OS Bookworm installation with the HQ camera.
