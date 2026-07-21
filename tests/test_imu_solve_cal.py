#!/usr/bin/env python3
"""
Pure-logic unit tests for Unit C — accel-tilt / gyro-scale calibration from
plate-solve residuals (diofinder.imu_solve_cal). No hardware; needs numpy.

Recovers an injected accelerometer tilt bias from synthetic observations,
proves the heading/yaw drift does NOT leak into the accel estimate, checks the
tilt-diversity observability gate, the Mode-1 correction round-trip, the
geometric transforms, and the gyro scale-factor.

Run:  python3 -m unittest tests.test_imu_solve_cal -v
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from diofinder import imu_solve_cal as sc


def _matrix_to_quat(R):
    """Shepperd's method: rotation matrix -> (w,x,y,z)."""
    t = np.trace(R)
    if t > 0:
        s = math.sqrt(t + 1.0) * 2
        w = 0.25 * s
        x = (R[2, 1] - R[1, 2]) / s
        y = (R[0, 2] - R[2, 0]) / s
        z = (R[1, 0] - R[0, 1]) / s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = math.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2
        w = (R[2, 1] - R[1, 2]) / s
        x = 0.25 * s
        y = (R[0, 1] + R[1, 0]) / s
        z = (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s = math.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2
        w = (R[0, 2] - R[2, 0]) / s
        x = (R[0, 1] + R[1, 0]) / s
        y = 0.25 * s
        z = (R[1, 2] + R[2, 1]) / s
    else:
        s = math.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2
        w = (R[1, 0] - R[0, 1]) / s
        x = (R[0, 2] + R[2, 0]) / s
        y = (R[1, 2] + R[2, 1]) / s
        z = 0.25 * s
    return (w, x, y, z)


def _quat_with_up(up, yaw=0.0):
    """Build a body->ref quaternion whose body-frame up (R^T e3) equals ``up``,
    with an arbitrary yaw about the ref +Z. i.e. R has third row == up."""
    up = np.asarray(up, dtype=float)
    up = up / np.linalg.norm(up)
    # Orthonormal complement (rows 0,1) to up (row 2).
    a = np.array([1.0, 0.0, 0.0])
    if abs(up @ a) > 0.9:
        a = np.array([0.0, 1.0, 0.0])
    e0 = a - (a @ up) * up
    e0 /= np.linalg.norm(e0)
    e1 = np.cross(up, e0)
    # Apply yaw: rotate e0,e1 in their plane.
    c, s = math.cos(yaw), math.sin(yaw)
    r0 = c * e0 + s * e1
    r1 = -s * e0 + c * e1
    R = np.vstack([r0, r1, up])          # third row == up
    if np.linalg.det(R) < 0:
        R[1] = -R[1]
    return _matrix_to_quat(R)


def _synth_up_imu(up_true, bias_tilt):
    """Physical model: up_imu = normalize(up_true - bias_tilt)."""
    v = np.asarray(up_true, float) - np.asarray(bias_tilt, float)
    return v / np.linalg.norm(v)


def _diverse_ups(n=40):
    """A spread of true up directions across the sky (tilt diversity)."""
    rng = np.random.default_rng(7)
    ups = []
    for _ in range(n):
        v = rng.normal(size=3)
        v[2] = abs(v[2]) + 0.2          # keep a general upward tilt spread
        ups.append(v / np.linalg.norm(v))
    return ups


class TestAccelBiasRecovery(unittest.TestCase):
    def test_recovers_injected_bias(self):
        bias = np.array([0.03, -0.02, 0.01])     # ~2° tilt bias
        est = sc.AccelTiltEstimator()
        for u in _diverse_ups(50):
            est.add(u, _synth_up_imu(u, bias))
        b, q = est.solve()
        self.assertIsNotNone(b, msg=q)
        np.testing.assert_allclose(b, bias, atol=2e-3)
        self.assertGreater(q["r2"], 0.99)

    def test_yaw_drift_does_not_leak(self):
        """Adding heading drift (rotation about up) to the IMU attitude must not
        change the tilt estimate — the estimator uses up vectors only."""
        bias = np.array([0.025, 0.0, -0.015])
        est = sc.AccelTiltEstimator()
        for u in _diverse_ups(50):
            u_imu = _synth_up_imu(u, bias)
            # Build a full quaternion for the IMU up WITH a large arbitrary yaw,
            # then re-extract up: yaw must wash out.
            q = _quat_with_up(u_imu, yaw=1.3)
            est.add(u, sc.up_imu_body(q))
        b, qual = est.solve()
        self.assertIsNotNone(b, msg=qual)
        np.testing.assert_allclose(b, bias, atol=3e-3)

    def test_under_diversity_refused(self):
        """All observations near the same up ⇒ bias under-observed ⇒ refuse."""
        bias = np.array([0.02, 0.0, 0.0])
        est = sc.AccelTiltEstimator()
        base = np.array([0.0, 0.0, 1.0])
        rng = np.random.default_rng(1)
        for _ in range(50):
            u = base + 0.01 * rng.normal(size=3)   # tiny spread only
            u /= np.linalg.norm(u)
            est.add(u, _synth_up_imu(u, bias))
        b, reason = est.solve()
        self.assertIsNone(b)
        self.assertIn("diversity", reason)

    def test_too_few_obs(self):
        est = sc.AccelTiltEstimator()
        est.add([0, 0, 1], [0, 0, 1])
        b, reason = est.solve()
        self.assertIsNone(b)
        self.assertIn("obs", reason)


class TestCorrection(unittest.TestCase):
    def test_correction_round_trip(self):
        """correct_quaternion(biased_q, bias) should recover the true up."""
        bias = np.array([0.04, -0.03, 0.02])
        for up_true in _diverse_ups(12):
            u_imu = _synth_up_imu(up_true, bias)
            q_imu = _quat_with_up(u_imu, yaw=0.7)
            q_corr = sc.correct_quaternion(q_imu, bias)
            up_corr = sc.up_imu_body(q_corr)
            ang = math.degrees(math.acos(
                max(-1.0, min(1.0, up_corr @ (up_true / np.linalg.norm(up_true))))))
            self.assertLess(ang, 0.2)   # corrected up within 0.2° of truth

    def test_zero_bias_is_identity(self):
        q = (0.9, 0.1, 0.2, 0.3)
        out = sc.correct_quaternion(q, [0.0, 0.0, 0.0])
        np.testing.assert_allclose(out, q)


class TestTransforms(unittest.TestCase):
    def test_up_true_body_identity(self):
        # body==camera==celestial ⇒ up_true_body == zenith.
        zen = sc._unit_from_radec(123.0, 45.0)
        I9 = list(np.eye(3).reshape(-1))
        u = sc.up_true_body((1.0, 0.0, 0.0, 0.0), I9, zen)
        np.testing.assert_allclose(u, zen, atol=1e-9)

    def test_up_imu_body_yaw_invariant(self):
        up = np.array([0.2, -0.3, 0.93])
        up /= np.linalg.norm(up)
        for yaw in (0.0, 0.5, 2.0, -1.7):
            q = _quat_with_up(up, yaw=yaw)
            np.testing.assert_allclose(sc.up_imu_body(q), up, atol=1e-9)

    def test_zenith_poles(self):
        import datetime
        utc = datetime.datetime(2026, 7, 21, 3, 0, 0)
        # Latitude 90 ⇒ zenith is celestial north pole [0,0,1] regardless of LST.
        z = sc.zenith_unit_j2000(90.0, 0.0, utc)
        np.testing.assert_allclose(z, [0, 0, 1], atol=1e-6)
        # Equator ⇒ zenith on the celestial equator (z-component ~0).
        z0 = sc.zenith_unit_j2000(0.0, -71.0, utc)
        self.assertAlmostEqual(z0[2], 0.0, places=6)

    def test_lst_range(self):
        import datetime
        utc = datetime.datetime(2026, 1, 1, 12, 0, 0)
        for lon in (-180, -71, 0, 100, 179):
            lst = sc.local_sidereal_time_deg(utc, lon)
            self.assertTrue(0.0 <= lst < 360.0)


class TestMode2Conversion(unittest.TestCase):
    def test_accel_offset_delta_lsb(self):
        # bias_tilt (rad) -> b_a = bias*g (m/s²) -> * lsb_per_ms2, sign-flipped.
        d = sc.accel_offset_delta_lsb([0.01, 0.0, -0.02],
                                      lsb_per_ms2=100.0, g=9.80665, sign=-1)
        # 0.01 rad * 9.80665 * 100 = 9.80665 -> round 10; sign -1 -> -10
        self.assertEqual(d, [-10, 0, 20])
        self.assertTrue(all(isinstance(v, int) for v in d))

    def test_zero_bias_zero_delta(self):
        self.assertEqual(
            sc.accel_offset_delta_lsb([0.0, 0.0, 0.0]), [0, 0, 0])

    def test_bad_shape_raises(self):
        with self.assertRaises(ValueError):
            sc.accel_offset_delta_lsb([0.0, 1.0])


class TestGyroScale(unittest.TestCase):
    def test_scale_recovery(self):
        rng = np.random.default_rng(3)
        true_scale = 1.05
        pairs = []
        for _ in range(20):
            sky = math.radians(rng.uniform(2, 20))
            pairs.append((sky * true_scale, sky))
        s, n = sc.gyro_scale(pairs)
        self.assertIsNotNone(s)
        self.assertAlmostEqual(s, true_scale, places=3)
        self.assertEqual(n, 20)

    def test_too_few_pairs(self):
        s, n = sc.gyro_scale([(0.1, 0.1)] * 3)
        self.assertIsNone(s)

    def test_ignores_tiny_slews(self):
        pairs = [(math.radians(0.2), math.radians(0.2))] * 20  # all sub-degree
        s, n = sc.gyro_scale(pairs)
        self.assertIsNone(s)


if __name__ == "__main__":
    unittest.main()
