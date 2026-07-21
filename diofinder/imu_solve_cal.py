"""Unit C — accelerometer-tilt + gyro-scale calibration from plate-solve residuals.

Derives the *static* IMU calibrations the mounted BNO055 cannot self-produce —
the accelerometer tilt bias (it needs a 6-orientation tumble that is impossible
once bolted to the scope) and the gyro scale-factor — from the standing
disagreement between plate solves and IMU output, so the *standalone* IMU
(between solves, mid-slew, when solving fails) is trustworthy. See
docs/decisions/2026-07-21-imu-calibration-from-plate-solves.md.

Design (all conventions fixed here and unit-tested in tests/test_imu_solve_cal.py):

  * A constant accel bias tilts the IMU's sensed gravity/"up" axis. Expressed as
    the dimensionless tilt vector ``bias_tilt = b_a / g`` (radians), the observed
    tilt residual between the IMU's up and the true up (both in the BODY frame)
    is  ``r = up_true × up_imu = -skew(up_true) · bias_tilt``  — linear, and
    (critically) built from the UP vectors only, so the drifting heading/yaw
    (rotation about up) never enters. That dynamic term is left to re-anchoring.
  * Each observation constrains the 2 components of bias_tilt perpendicular to
    ``up_true``; stacking observations across DIFFERENT altitudes (tilt
    diversity) makes all 3 observable. The normal matrix ``N = Σ(I - u_i u_iᵀ)``
    is rank-deficient until the up vectors span 3-D — its smallest eigenvalue is
    the observability metric (the analogue of Unit A's axis-diversity gate).
  * ``up_true`` in the body frame comes from the plate solve + the Unit A
    extrinsic + the zenith direction (site + time); ``up_imu`` from the BNO055
    quaternion. Requires a good extrinsic (hard dependency A → C) and a valid
    site/time (refuses lat/long == 0/0, the factory-reset default).

Pure numpy, hardware-free. Frame conventions:
  * ``R(q)`` maps A→B for a quaternion mapping frame A→B: ``v_B = R(q) @ v_A``.
  * ``q_sky`` maps celestial→camera (boresight = row 0 of R, matching
    imu_math.quat_to_radec).
  * extrinsic ``R9`` (Unit A) maps body→camera: ``v_cam = R9 @ v_body``.
  * ``q_imu`` maps body→IMU-ref, whose +Z is up (opposite gravity).
"""
from __future__ import annotations

import math

import numpy as np

# Observability / acceptance gates (module constants; not per-device config).
MIN_OBS = 8            # minimum accepted tilt observations before a solve
MIN_EIG = 1.5          # smallest eigenvalue of N required (tilt diversity)
MIN_R2 = 0.5           # fit quality floor
MAX_TILT_RESID = 0.30  # rad (~17°): drop a wild residual (torn frame / bad solve)
GYRO_MIN_PAIRS = 6
GYRO_MIN_MAG = math.radians(1.0)   # ignore sub-degree slews for the scale ratio


def quat_to_matrix(q):
    """Rotation matrix R(q) (maps the quaternion's A frame to its B frame)."""
    w, x, y, z = q
    n = math.sqrt(w * w + x * x + y * y + z * z)
    if n < 1e-12:
        return np.eye(3)
    w, x, y, z = w / n, x / n, y / n, z / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w),     2 * (x * z + y * w)],
        [2 * (x * y + z * w),     1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w),     2 * (y * z + x * w),     1 - 2 * (x * x + y * y)],
    ])


def _unit_from_radec(ra_deg, dec_deg):
    ra, dec = math.radians(ra_deg), math.radians(dec_deg)
    cd = math.cos(dec)
    return np.array([cd * math.cos(ra), cd * math.sin(ra), math.sin(dec)])


def _julian_day(utc):
    """Julian Day from a timezone-aware (or naive-UTC) datetime."""
    y, m = utc.year, utc.month
    day = (utc.day + (utc.hour + (utc.minute + (utc.second
           + utc.microsecond / 1e6) / 60.0) / 60.0) / 24.0)
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return (math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1))
            + day + b - 1524.5)


def local_sidereal_time_deg(utc, longitude_deg):
    """Local (mean) sidereal time in degrees for a UTC datetime + east longitude."""
    jd = _julian_day(utc)
    d = jd - 2451545.0
    gmst = 280.46061837 + 360.98564736629 * d + 0.000387933 * (d / 36525.0) ** 2
    return (gmst + longitude_deg) % 360.0


def zenith_unit_j2000(latitude_deg, longitude_deg, utc, precess=None):
    """Zenith direction as a J2000 unit vector for a site + UTC time.

    The zenith is (RA = local sidereal time, Dec = latitude) in the
    equator-of-date (apparent) frame; ``precess`` (diofinder.precession
    .jnow_to_j2000) brings it to the J2000 frame the plate solver works in.
    """
    lst = local_sidereal_time_deg(utc, longitude_deg)
    ra_now, dec_now = lst, latitude_deg
    if precess is not None:
        ra_j2000, dec_j2000 = precess(ra_now, dec_now)
    else:
        ra_j2000, dec_j2000 = ra_now, dec_now
    return _unit_from_radec(ra_j2000, dec_j2000)


def up_true_body(q_sky, R9, zenith_cel):
    """True up (zenith) direction in the IMU body frame, from the plate solve.

    cel → camera (R(q_sky)) → body (R9ᵀ). ``R9`` is the row-major flat-9
    body→camera extrinsic (Unit A). Returns a unit 3-vector."""
    R_sky = quat_to_matrix(q_sky)
    R_ext = np.asarray(R9, dtype=float).reshape(3, 3)
    v_cam = R_sky @ np.asarray(zenith_cel, dtype=float)
    v_body = R_ext.T @ v_cam
    n = np.linalg.norm(v_body)
    return v_body / n if n > 1e-12 else v_body


def up_imu_body(q_imu):
    """IMU's estimated up (its reference +Z) expressed in the body frame."""
    R = quat_to_matrix(q_imu)
    return R.T @ np.array([0.0, 0.0, 1.0])


def _skew(u):
    return np.array([[0.0, -u[2], u[1]],
                     [u[2], 0.0, -u[0]],
                     [-u[1], u[0], 0.0]])


class AccelTiltEstimator:
    """Incremental least-squares for ``bias_tilt`` (= b_a/g, radians) from a
    stream of (up_true_body, up_imu_body) pairs. O(1) memory."""

    def __init__(self, min_obs=MIN_OBS, min_eig=MIN_EIG, min_r2=MIN_R2):
        self.min_obs = min_obs
        self.min_eig = min_eig
        self.min_r2 = min_r2
        self.reset()

    def reset(self):
        self._N = np.zeros((3, 3))     # Σ (I - u uᵀ)
        self._rhs = np.zeros(3)        # Σ (u × r)
        self._sumsq = 0.0              # Σ |r|²
        self.n = 0

    def add(self, u_true, u_imu):
        """Add one observation. Returns True if accepted."""
        u = np.asarray(u_true, dtype=float)
        nu = np.linalg.norm(u)
        if nu < 1e-9:
            return False
        u = u / nu
        r = np.cross(u, np.asarray(u_imu, dtype=float))   # tilt residual (rad)
        if np.linalg.norm(r) > MAX_TILT_RESID:
            return False                                  # outlier / torn frame
        self._N += np.eye(3) - np.outer(u, u)
        self._rhs += np.cross(u, r)
        self._sumsq += float(r @ r)
        self.n += 1
        return True

    def solve(self):
        """Return ``(bias_tilt(3,), quality)`` or ``(None, reason)``.

        quality: {n, r2, min_eig, tilt_deg} where tilt_deg is the magnitude of
        the estimated tilt bias in degrees."""
        if self.n < self.min_obs:
            return None, f"only {self.n} obs (< {self.min_obs})"
        eig = float(np.linalg.eigvalsh(self._N)[0])       # smallest eigenvalue
        if eig < self.min_eig:
            return None, f"tilt diversity {eig:.2f} < {self.min_eig}"
        try:
            bias = np.linalg.solve(self._N, self._rhs)
        except np.linalg.LinAlgError:
            return None, "singular normal matrix"
        # ss_res = bᵀ N b - 2 bᵀ rhs + sumsq  (N = ΣAᵀA, rhs = ΣAᵀr, A=-skew(u))
        ss_res = float(bias @ self._N @ bias - 2.0 * bias @ self._rhs + self._sumsq)
        r2 = max(0.0, 1.0 - ss_res / self._sumsq) if self._sumsq > 1e-12 else 0.0
        if r2 < self.min_r2:
            return None, f"fit r2 {r2:.3f} < {self.min_r2}"
        quality = {"n": int(self.n), "r2": round(r2, 4),
                   "min_eig": round(eig, 3),
                   "tilt_deg": round(math.degrees(float(np.linalg.norm(bias))), 3)}
        return [float(v) for v in bias], quality


def correct_quaternion(q_imu, bias_tilt):
    """Apply the tilt-bias correction to a live IMU quaternion (Mode 1).

    Inverts the bias model: the true up is ``normalize(up_imu + bias_tilt)``, so
    we rotate ``q_imu`` by the minimal body-frame rotation taking its up to the
    corrected up. Yaw is untouched (the correction is pure tilt). Returns a
    (w,x,y,z) tuple. A tiny or absent bias returns the input unchanged."""
    b = np.asarray(bias_tilt, dtype=float)
    if b.shape != (3,) or float(b @ b) < 1e-12:
        return tuple(float(v) for v in q_imu)
    u_imu = up_imu_body(q_imu)
    u_true = u_imu + b
    n = np.linalg.norm(u_true)
    if n < 1e-9:
        return tuple(float(v) for v in q_imu)
    u_true = u_true / n
    # q maps body→ref and up_body = R(q)ᵀ e3, so post-multiplying q by dq rotates
    # the reported body-up by −delta; use cross(u_true, u_imu) so that net
    # rotation carries u_imu onto u_true.
    delta = np.cross(u_true, u_imu)     # small body-frame rotation vector
    dn = np.linalg.norm(delta)
    if dn < 1e-12:
        return tuple(float(v) for v in q_imu)
    angle = math.asin(max(-1.0, min(1.0, dn)))
    axis = delta / dn
    s = math.sin(angle / 2.0)
    dq = (math.cos(angle / 2.0), axis[0] * s, axis[1] * s, axis[2] * s)
    # q maps body→ref; a body-frame rotation post-multiplies: q_corr = q_imu ⊗ dq.
    w1, x1, y1, z1 = q_imu
    w2, x2, y2, z2 = dq
    return (
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
    )


def gyro_scale(mag_pairs):
    """Robust gyro scale-factor from (|imu_delta|, |sky_delta|) rotation-angle
    pairs (radians): median of |imu|/|sky| over slews above GYRO_MIN_MAG.

    Returns ``(scale, n_used)`` or ``(None, 0)`` when under-observed. Ideal 1.0;
    a value ≠ 1 means the gyro over/under-reports rotation."""
    ratios = [im / sk for im, sk in mag_pairs
              if sk > GYRO_MIN_MAG and im > GYRO_MIN_MAG]
    if len(ratios) < GYRO_MIN_PAIRS:
        return None, 0
    return float(np.median(ratios)), len(ratios)
