"""
Pure-Python quaternion helpers for IMU dead-reckoning.

No numpy — safe to call from the LX200 hot path in comms_proc.
All quaternions are (w, x, y, z) unit quaternions.
"""
import math


def quat_conjugate(q):
    w, x, y, z = q
    return (w, -x, -y, -z)


def quat_mul(q1, q2):
    """Hamilton product of two unit quaternions."""
    w1, x1, y1, z1 = q1
    w2, x2, y2, z2 = q2
    return (
        w1*w2 - x1*x2 - y1*y2 - z1*z2,
        w1*x2 + x1*w2 + y1*z2 - z1*y2,
        w1*y2 - x1*z2 + y1*w2 + z1*x2,
        w1*z2 + x1*y2 - y1*x2 + z1*w2,
    )


def quat_to_rotvec(q):
    """
    Convert unit quaternion to rotation vector (axis × angle, radians).
    Uses the short-arc convention: angle ∈ [0, π].
    """
    w, x, y, z = q
    w = max(-1.0, min(1.0, w))          # numerical clamp
    sin_half = math.sqrt(max(0.0, x*x + y*y + z*z))
    if sin_half < 1e-9:
        return (2.0*x, 2.0*y, 2.0*z)   # linear approx for tiny rotation
    half_angle = math.atan2(sin_half, abs(w))
    if w < 0:
        half_angle = -half_angle        # keep short arc
    scale = 2.0 * half_angle / sin_half
    return (x * scale, y * scale, z * scale)


def rotvec_to_quat(r):
    """Inverse of quat_to_rotvec: rotation vector (radians) -> unit quaternion."""
    rx, ry, rz = r
    angle = math.sqrt(rx*rx + ry*ry + rz*rz)
    if angle < 1e-12:
        return (1.0, 0.5*rx, 0.5*ry, 0.5*rz)   # small-angle, matches quat_to_rotvec
    s = math.sin(angle / 2.0) / angle
    return (math.cos(angle / 2.0), rx*s, ry*s, rz*s)


def quat_delta_rotvec(q_now, q_ref):
    """
    Rotation vector (in q_ref's coordinate frame) for the rotation that
    takes q_ref to q_now.  Used to measure how far the scope has moved
    since the last plate-solve reference.
    """
    return quat_to_rotvec(quat_mul(q_now, quat_conjugate(q_ref)))


def quat_to_radec(q):
    """(RA, Dec) in degrees of the boresight for a solver attitude quaternion.

    Convention (verified against olive-solve solutions): q maps celestial ->
    camera with the camera +X axis as the boresight, so the boresight's
    celestial unit vector is ROW 0 of the rotation matrix of q.
    """
    w, x, y, z = q
    bx = 1.0 - 2.0 * (y * y + z * z)
    by = 2.0 * (x * y - z * w)
    bz = 2.0 * (x * z + y * w)
    ra = math.degrees(math.atan2(by, bx)) % 360.0
    dec = math.degrees(math.asin(max(-1.0, min(1.0, bz))))
    return ra, dec


def wrap180(deg):
    """Wrap an angle difference into [-180, 180)."""
    return (deg + 180.0) % 360.0 - 180.0


def alpha_beta_step(state, z_ra, z_dec, dt, alpha, beta):
    """One alpha-beta tracker step on a (RA, Dec) estimate, in degrees.

    This filters the IMU's *rate of change* rather than low-passing the
    position: it carries a velocity estimate, predicts forward by it, then
    corrects toward the new measurement. That kills per-sample sensor jitter
    when the scope is parked (velocity -> 0, output settles) while tracking a
    steady slew with no lag bias (the prediction term cancels the ramp).

    state: (ra, dec, vra, vdec) prior estimate -- deg, deg, deg/s, deg/s.
    z_ra, z_dec: new measurement (deg). dt: seconds since last step (> 0).
    alpha: position gain (0-1). beta: velocity gain (0-1).

    Returns the new (ra, dec, vra, vdec). RA stays on the circle (mod 360,
    residual taken the short way round); Dec is clamped to [-90, 90].
    """
    ra, dec, vra, vdec = state
    # Predict forward with the current velocity estimate.
    ra_pred = ra + vra * dt
    dec_pred = dec + vdec * dt
    # Residuals (RA measured the short way around the circle).
    res_ra = wrap180(z_ra - ra_pred)
    res_dec = z_dec - dec_pred
    # Correct position and velocity.
    ra_new = (ra_pred + alpha * res_ra) % 360.0
    dec_new = max(-90.0, min(90.0, dec_pred + alpha * res_dec))
    vra_new = vra + (beta / dt) * res_ra
    vdec_new = vdec + (beta / dt) * res_dec
    return ra_new, dec_new, vra_new, vdec_new
