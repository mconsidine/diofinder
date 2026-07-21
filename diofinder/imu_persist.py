"""Persistence for the plate-solve-derived camera<->IMU extrinsic (`imu_frame_R`).

The IMU-body -> camera rotation is *physically fixed* (it changes only if the
IMU or camera is remounted), yet `solver_proc` re-learns it from scratch every
power cycle via the Kabsch fit in `imu_frame.py`, and it only becomes observable
after the scope has slewed in >= 2 non-collinear directions with a good solve at
each end. Persisting it across restarts makes the exact-quaternion LX200
prediction available from the FIRST solve after boot instead of after that first
multi-direction slew.

Design (mirrors seeing.py's saved-override discipline):
  * Small JSON at EXTRINSIC_PATH, written atomically (temp file + os.replace).
  * The stored value is a **seed**, never gospel: the live Kabsch fit keeps
    running and OVERWRITES the file when a good fit diverges from it (a remount
    self-heals with no user action — the same philosophy as the FOV
    drift-recommit).
  * A *stricter* save gate than the use gate (`save_worthy`) means only a
    well-observed mounting is ever written.

Pure Python (math/json/os/tempfile only) — hardware-free and unit-tested in
tests/test_imu_persist.py.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import time

EXTRINSIC_PATH = "/var/lib/diofinder/imu_extrinsic.json"

# Stricter than imu_frame's USE gate (MIN_PAIRS=4, MIN_R2=0.9,
# MIN_AXIS_DIVERSITY=0.25): only a well-observed mounting is persisted.
SAVE_MIN_PAIRS = 8
SAVE_MIN_R2 = 0.97
SAVE_MIN_AXIS_DIVERSITY = 0.4

# A good live fit this many degrees from the on-disk value triggers a rewrite.
# Small refinements below it don't churn the disk; a remount (angles well
# beyond this) always overwrites.
SAVE_UPDATE_TOL_DEG = 0.5
# Divergence beyond this between a persisted seed and a good live fit means the
# seed is stale (remount) and the live fit should win.
DIVERGE_TOL_DEG = 2.0


def _valid_R9(R9):
    if R9 is None:
        return False
    try:
        return len(R9) == 9 and all(
            isinstance(v, (int, float)) and math.isfinite(v) for v in R9)
    except TypeError:
        return False


def save_worthy(quality) -> bool:
    """True if a fit quality dict clears the (strict) save gate."""
    if not isinstance(quality, dict):
        return False
    try:
        return (int(quality.get("n", 0)) >= SAVE_MIN_PAIRS
                and float(quality.get("r2", 0.0)) >= SAVE_MIN_R2
                and float(quality.get("axis_diversity", 0.0))
                >= SAVE_MIN_AXIS_DIVERSITY)
    except (TypeError, ValueError):
        return False


def relative_angle_deg(A9, B9) -> float:
    """Angle (degrees) of the relative rotation A * B^T between two row-major
    3x3 rotation matrices given as flat 9-lists.

    For rotation matrices, trace(A B^T) = sum_m A[m] * B[m] (the flat dot
    product), and the rotation angle is acos((trace - 1) / 2).
    """
    trace = sum(a * b for a, b in zip(A9, B9))
    c = max(-1.0, min(1.0, (trace - 1.0) / 2.0))
    return math.degrees(math.acos(c))


def extrinsic_diverged(A9, B9, tol_deg: float = DIVERGE_TOL_DEG) -> bool:
    """True if the two extrinsics differ by more than ``tol_deg`` (or either is
    unusable)."""
    if not _valid_R9(A9) or not _valid_R9(B9):
        return True
    return relative_angle_deg(A9, B9) > tol_deg


def _atomic_write_json(path: str, data) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".imu_extr_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def save_extrinsic(R9, quality, *, path: str = EXTRINSIC_PATH,
                   now=None) -> None:
    """Persist the 9-float extrinsic + its quality dict. Raises ValueError on a
    malformed matrix (callers gate on ``save_worthy`` first)."""
    if not _valid_R9(R9):
        raise ValueError("R9 must be 9 finite numbers")
    _atomic_write_json(path, {
        "R": [float(v) for v in R9],
        "quality": quality if isinstance(quality, dict) else {},
        "saved_at": float(now if now is not None else time.time()),
    })


def load_extrinsic(path: str = EXTRINSIC_PATH):
    """Return ``(R9, quality, saved_at)`` or ``None`` if absent/unreadable.

    A malformed or non-rotation matrix returns None so a corrupt file can never
    seed a bad mounting (the live fit then relearns from scratch as before)."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    R9 = data.get("R")
    if not _valid_R9(R9):
        return None
    R9 = [float(v) for v in R9]
    quality = data.get("quality") if isinstance(data.get("quality"), dict) else {}
    try:
        saved_at = float(data.get("saved_at", 0.0))
    except (TypeError, ValueError):
        saved_at = 0.0
    return R9, quality, saved_at


def clear_extrinsic(path: str = EXTRINSIC_PATH) -> bool:
    """Delete the persisted extrinsic. Returns True if a file was removed."""
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


# --- BNO055 accel/gyro calibration profile (Unit B) --------------------------
# The BNO055 has no flash: its calibration is lost on power-down. The 22-byte
# offset/radius blob (registers 0x55..0x6A) can be read once the chip reports a
# good calibration and written back at next boot (in CONFIG mode) to skip the
# warm-up. In IMUPLUS mode only the gyro/accel bytes matter (mag is disabled);
# the mag bytes in the blob are stored but unused.

BNO055_CALIB_PATH = "/var/lib/diofinder/bno055_calib.json"
BNO055_CALIB_LEN = 22


def decode_calib_status(byte):
    """Decode the BNO055 CALIB_STAT register (0x35) into its four 0-3 fields.

    Bit layout: [7:6]=sys [5:4]=gyro [3:2]=accel [1:0]=mag. Pure/unit-tested."""
    b = int(byte) & 0xFF
    return {
        "sys":   (b >> 6) & 0x03,
        "gyro":  (b >> 4) & 0x03,
        "accel": (b >> 2) & 0x03,
        "mag":   b & 0x03,
    }


def _valid_blob(blob):
    try:
        return (len(blob) == BNO055_CALIB_LEN
                and all(isinstance(v, int) and 0 <= v <= 255 for v in blob))
    except TypeError:
        return False


def save_bno055_profile(blob, *, status=None, path: str = BNO055_CALIB_PATH,
                        now=None) -> None:
    """Persist the 22-byte BNO055 calibration blob (list of ints 0-255)."""
    blob = [int(v) & 0xFF for v in blob]
    if not _valid_blob(blob):
        raise ValueError(f"blob must be {BNO055_CALIB_LEN} bytes")
    _atomic_write_json(path, {
        "calib": blob,
        "status": status if isinstance(status, dict) else {},
        "saved_at": float(now if now is not None else time.time()),
    })


def load_bno055_profile(path: str = BNO055_CALIB_PATH):
    """Return the 22-byte blob (list of ints) or None if absent/malformed."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    blob = data.get("calib")
    if not _valid_blob(blob):
        return None
    return [int(v) & 0xFF for v in blob]


def clear_bno055_profile(path: str = BNO055_CALIB_PATH) -> bool:
    """Delete the persisted BNO055 profile. True if a file was removed."""
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


# --- Unit C: plate-solve-derived accel-tilt / gyro-scale calibration ---------
# The static IMU calibration learned from solve-vs-IMU disagreement
# (imu_solve_cal). `bias_tilt` is the dimensionless accel tilt vector (b_a/g,
# radians); `gyro_scale` the rotation-magnitude scale factor. Seeded at boot so
# the Mode-1 software correction is live immediately; refined online; self-heals
# on divergence like the extrinsic.

SOLVE_CAL_PATH = "/var/lib/diofinder/imu_solve_cal.json"
# A new estimate this many radians from the stored one triggers a rewrite /
# self-heal (~0.29°).
SOLVE_CAL_UPDATE_TOL_RAD = 0.005


def _valid_vec3(v):
    try:
        return len(v) == 3 and all(
            isinstance(x, (int, float)) and math.isfinite(x) for x in v)
    except TypeError:
        return False


def save_solve_cal(bias_tilt, *, gyro_scale=None, quality=None,
                   path: str = SOLVE_CAL_PATH, now=None) -> None:
    """Persist the accel tilt bias (+ optional gyro scale + quality)."""
    if not _valid_vec3(bias_tilt):
        raise ValueError("bias_tilt must be 3 finite numbers")
    _atomic_write_json(path, {
        "bias_tilt": [float(v) for v in bias_tilt],
        "gyro_scale": (float(gyro_scale) if gyro_scale is not None else None),
        "quality": quality if isinstance(quality, dict) else {},
        "saved_at": float(now if now is not None else time.time()),
    })


def load_solve_cal(path: str = SOLVE_CAL_PATH):
    """Return ``(bias_tilt, gyro_scale, quality, saved_at)`` or None."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    b = data.get("bias_tilt")
    if not _valid_vec3(b):
        return None
    b = [float(v) for v in b]
    gs = data.get("gyro_scale")
    try:
        gs = float(gs) if gs is not None else None
    except (TypeError, ValueError):
        gs = None
    quality = data.get("quality") if isinstance(data.get("quality"), dict) else {}
    try:
        saved_at = float(data.get("saved_at", 0.0))
    except (TypeError, ValueError):
        saved_at = 0.0
    return b, gs, quality, saved_at


def solve_cal_diverged(b_new, b_old, tol_rad: float = SOLVE_CAL_UPDATE_TOL_RAD) -> bool:
    """True if two tilt-bias vectors differ by more than ``tol_rad`` (Euclidean),
    or either is unusable."""
    if not _valid_vec3(b_new) or not _valid_vec3(b_old):
        return True
    return math.sqrt(sum((a - c) ** 2 for a, c in zip(b_new, b_old))) > tol_rad


def clear_solve_cal(path: str = SOLVE_CAL_PATH) -> bool:
    """Delete the persisted solve-cal. True if a file was removed."""
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False
