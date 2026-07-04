"""IMU-body -> camera frame rotation fit + frame-corrected solve hint.

Pins the Task-B acceptance criteria: with a synthetic 90-degree-mounted IMU
and a 20-degree slew, the frame-corrected hint lands within 2 degrees of
truth (the raw body-frame hint is tens of degrees off); the fit refuses
collinear-axis histories and garbage pairs.
"""
import math
import time
import unittest

import numpy as np

from diofinder import imu_frame
from diofinder.imu_math import (quat_mul, quat_conjugate, rotvec_to_quat,
                                quat_to_rotvec, quat_delta_rotvec)
from diofinder import solver_proc


def _rotmat(axis, deg):
    axis = np.asarray(axis, float)
    axis = axis / np.linalg.norm(axis)
    a = math.radians(deg)
    K = np.array([[0, -axis[2], axis[1]],
                  [axis[2], 0, -axis[0]],
                  [-axis[1], axis[0], 0]])
    return np.eye(3) + math.sin(a) * K + (1 - math.cos(a)) * (K @ K)


def _pairs_for(R, r_imu_list):
    r_sky = [tuple(R @ np.asarray(r)) for r in r_imu_list]
    return r_imu_list, r_sky


DIVERSE = [(0.1, 0.0, 0.0), (0.0, 0.15, 0.02), (0.05, 0.05, 0.1),
           (-0.08, 0.1, 0.0), (0.0, -0.05, 0.12)]


class FitTests(unittest.TestCase):
    def test_recovers_known_mounting(self):
        for axis, deg in [((0, 0, 1), 90), ((1, 2, 3), 37), ((0, 1, 0), 180)]:
            R = _rotmat(axis, deg)
            a, b = _pairs_for(R, DIVERSE)
            R9, quality = imu_frame.fit_frame_rotation(a, b)
            self.assertIsNotNone(R9, quality)
            self.assertGreaterEqual(quality["r2"], 0.99)
            np.testing.assert_allclose(np.array(R9).reshape(3, 3), R,
                                       atol=1e-8)

    def test_noise_tolerated(self):
        rng = np.random.default_rng(3)
        R = _rotmat((1, 1, 0), 55)
        a = DIVERSE + [(0.12, -0.03, 0.05), (0.02, 0.09, -0.07)]
        b = [tuple(R @ np.asarray(r) + rng.normal(0, 0.002, 3)) for r in a]
        R9, quality = imu_frame.fit_frame_rotation(a, b)
        self.assertIsNotNone(R9, quality)
        self.assertGreaterEqual(quality["r2"], imu_frame.MIN_R2)

    def test_collinear_axes_refused(self):
        R = _rotmat((0, 0, 1), 90)
        a = [(0.1 * k, 0.0, 0.0) for k in (1, 2, 3, 4, 5)]   # all about +x
        a, b = _pairs_for(R, a)
        R9, reason = imu_frame.fit_frame_rotation(a, b)
        self.assertIsNone(R9)
        self.assertIn("diversity", reason)

    def test_magnitude_glitches_filtered(self):
        # Two pairs with wildly mismatched magnitudes (heading glitch) must
        # not drag the fit; with enough clean pairs it still succeeds.
        R = _rotmat((0, 0, 1), 90)
        a, b = _pairs_for(R, DIVERSE)
        a = list(a) + [(0.1, 0.0, 0.0), (0.0, 0.1, 0.0)]
        b = list(b) + [(1.5, 0.0, 0.0), (0.0, 0.001, 0.0)]   # 15x / 0.01x
        R9, quality = imu_frame.fit_frame_rotation(a, b)
        self.assertIsNotNone(R9, quality)
        self.assertEqual(quality["n"], len(DIVERSE))

    def test_garbage_refused(self):
        rng = np.random.default_rng(0)
        a = [tuple(v) for v in rng.normal(0, 0.1, (8, 3))]
        b = [tuple(v) for v in rng.normal(0, 0.1, (8, 3))]
        R9, reason = imu_frame.fit_frame_rotation(a, b)
        self.assertIsNone(R9)


def _geodesic_deg(q1, q2):
    d = quat_mul(quat_conjugate(q1), q2)
    return math.degrees(2.0 * math.acos(min(1.0, abs(d[0]))))


class HintTransformTests(unittest.TestCase):
    def _cfg(self, q_imu_now, R9=None):
        now = time.monotonic()
        cfg = {"imu_available": True, "imu_q": q_imu_now, "imu_t": now}
        if R9 is not None:
            cfg["imu_frame_R"] = R9
        return cfg

    def test_frame_corrected_hint_hits_truth(self):
        # 90-degree mounting about z; 20-degree slew about the camera x axis.
        R = _rotmat((0, 0, 1), 90)
        r_cam = (math.radians(20.0), 0.0, 0.0)
        r_imu = tuple(R.T @ np.asarray(r_cam))     # what the IMU measures
        q_imu_now = rotvec_to_quat(r_imu)          # ref was identity
        last_sky_q = rotvec_to_quat((0.05, -0.3, 0.7))   # arbitrary base
        truth = quat_mul(rotvec_to_quat(r_cam), last_sky_q)

        R9 = [float(v) for v in R.reshape(-1)]
        hint, unc = solver_proc._imu_propagate_hint(
            last_sky_q, (1.0, 0.0, 0.0, 0.0), self._cfg(q_imu_now, R9))
        self.assertLess(_geodesic_deg(hint, truth), 2.0)
        self.assertAlmostEqual(unc, max(2.0, 20.0 * 1.2), delta=0.5)

        # Without the fit the body-frame hint is far from truth and the cone
        # stays defensive.
        hint_raw, unc_raw = solver_proc._imu_propagate_hint(
            last_sky_q, (1.0, 0.0, 0.0, 0.0), self._cfg(q_imu_now))
        self.assertGreater(_geodesic_deg(hint_raw, truth), 10.0)
        self.assertAlmostEqual(unc_raw, max(2.0, 20.0 * 2.5), delta=0.5)

    def test_identity_mounting_unchanged(self):
        # With an identity fit the transform must be a no-op vs the raw path.
        r = (math.radians(10.0), 0.0, 0.0)
        q_imu_now = rotvec_to_quat(r)
        last_sky_q = (1.0, 0.0, 0.0, 0.0)
        eye9 = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        h1, _ = solver_proc._imu_propagate_hint(
            last_sky_q, (1.0, 0.0, 0.0, 0.0), self._cfg(q_imu_now, eye9))
        h2, _ = solver_proc._imu_propagate_hint(
            last_sky_q, (1.0, 0.0, 0.0, 0.0), self._cfg(q_imu_now))
        self.assertLess(_geodesic_deg(h1, h2), 0.01)

    def test_roundtrip_rotvec_quat(self):
        for r in [(0.3, -0.2, 0.5), (1e-14, 0, 0), (0, 0, math.pi * 0.9)]:
            q = rotvec_to_quat(r)
            back = quat_to_rotvec(q)
            np.testing.assert_allclose(back, r, atol=1e-9)


if __name__ == "__main__":
    unittest.main()
