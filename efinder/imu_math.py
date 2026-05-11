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


def quat_delta_rotvec(q_now, q_ref):
    """
    Rotation vector (in q_ref's coordinate frame) for the rotation that
    takes q_ref to q_now.  Used to measure how far the scope has moved
    since the last plate-solve reference.
    """
    return quat_to_rotvec(quat_mul(q_now, quat_conjugate(q_ref)))
