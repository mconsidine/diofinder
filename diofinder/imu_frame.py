"""IMU-body -> camera frame rotation estimation (Wahba/Kabsch fit).

The solve hint propagates the IMU's rotation delta onto the last solved sky
attitude. The BNO055 is mounted at an arbitrary fixed rotation to the optical
axis, so a raw body-frame delta points the hint in a mounting-dependent wrong
direction — measured on-sky: a 22.5 deg slew produced a hint 39.5 deg from
truth (1.76x the slew angle). If Q_cam = M Q_imu M^-1 for a fixed mounting M,
then the matched rotation VECTORS obey r_cam = R r_imu exactly, with R the
3x3 rotation of M. This module fits R from the (r_imu, r_sky) pairs the
solver already harvests between consecutive solves.

Quality gating is the load-bearing part: the fit engages only when it is
demonstrably good, otherwise the hint path keeps the shipped wide-cone
behavior (and olive-solve's blind fallback still backstops everything).
Gates:
  * >= MIN_PAIRS magnitude-consistent pairs (|r_sky|/|r_imu| within
    MAG_RATIO_RANGE — the same physical rotation must have the same angle;
    a heading glitch or torn sample fails this and is excluded),
  * axis diversity: the unitized r_imu set must span two directions
    (second singular value >= MIN_AXIS_DIVERSITY of the first) — alt-only
    slewing gives collinear axes and an unobservable R about that axis,
  * fit quality R^2 >= MIN_R2.

Pure numpy, hardware-free; unit-tested in tests/test_imu_frame.py.
"""
from __future__ import annotations

import numpy as np

MIN_PAIRS = 4
MIN_R2 = 0.9
MIN_AXIS_DIVERSITY = 0.25   # s2/s1 of the unitized r_imu directions
MAG_RATIO_RANGE = (0.7, 1.4)


def fit_frame_rotation(r_imu, r_sky):
    """Fit the rotation R with r_sky ~= R @ r_imu (Kabsch, det +1).

    ``r_imu`` / ``r_sky`` are matched sequences of 3-vectors (radians).
    Returns ``(R_flat9, quality)`` on success — ``R_flat9`` is the row-major
    list of 9 floats (JSON/Manager-friendly), ``quality`` a dict with
    ``n`` (pairs used), ``r2``, and ``axis_diversity`` — or ``(None, reason)``
    when any gate fails.
    """
    A = np.asarray(r_imu, dtype=float).reshape(-1, 3)
    B = np.asarray(r_sky, dtype=float).reshape(-1, 3)
    if A.shape != B.shape or A.shape[0] == 0:
        return None, "no pairs"

    # Magnitude consistency: same physical rotation -> same angle. Drop
    # glitched pairs instead of letting them drag the fit.
    na = np.linalg.norm(A, axis=1)
    nb = np.linalg.norm(B, axis=1)
    ok = (na > 1e-9) & (nb > 1e-9)
    ratio = np.where(ok, nb / np.maximum(na, 1e-12), 0.0)
    ok &= (ratio >= MAG_RATIO_RANGE[0]) & (ratio <= MAG_RATIO_RANGE[1])
    A, B = A[ok], B[ok]
    if A.shape[0] < MIN_PAIRS:
        return None, f"only {A.shape[0]} consistent pairs (< {MIN_PAIRS})"

    # Axis diversity of the (unitized) IMU rotation directions: collinear
    # axes make R unobservable about that axis.
    U_dirs = A / np.linalg.norm(A, axis=1, keepdims=True)
    sv = np.linalg.svd(U_dirs, compute_uv=False)
    diversity = float(sv[1] / sv[0]) if sv[0] > 0 else 0.0
    if diversity < MIN_AXIS_DIVERSITY:
        return None, f"axis diversity {diversity:.2f} < {MIN_AXIS_DIVERSITY}"

    # Kabsch: minimize sum ||R a_i - b_i||^2.
    H = A.T @ B
    U, _S, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    R = Vt.T @ np.diag([1.0, 1.0, d]) @ U.T

    resid = B - A @ R.T
    ss_res = float(np.sum(resid ** 2))
    ss_tot = float(np.sum(B ** 2))
    r2 = max(0.0, 1.0 - ss_res / ss_tot) if ss_tot > 1e-15 else 0.0
    if r2 < MIN_R2:
        return None, f"fit r2 {r2:.3f} < {MIN_R2}"

    quality = {"n": int(A.shape[0]), "r2": round(r2, 4),
               "axis_diversity": round(diversity, 3)}
    return [float(v) for v in R.reshape(-1)], quality


def apply_rotation(R_flat9, r):
    """R_flat9 (row-major 9 floats) applied to a 3-vector tuple. Pure Python
    (no numpy) so the per-frame hint path stays allocation-free."""
    rx, ry, rz = r
    return (R_flat9[0] * rx + R_flat9[1] * ry + R_flat9[2] * rz,
            R_flat9[3] * rx + R_flat9[4] * ry + R_flat9[5] * rz,
            R_flat9[6] * rx + R_flat9[7] * ry + R_flat9[8] * rz)
