#!/usr/bin/env python3
"""
Pure-logic unit tests for IMU calibration persistence (imu_persist).

Covers the camera<->IMU extrinsic (Unit A) and the BNO055 profile (Unit B):
round-trip save/load/clear, the strict save gate, relative-rotation divergence
at known angles, malformed-input rejection, and the calib-status decoder.
diofinder.imu_persist is dependency-free (math/json/os/tempfile), so these run
without numpy / star_detect / picamera2 / hardware.

Run:
    python3 -m unittest tests.test_imu_persist -v
or directly:
    python3 tests/test_imu_persist.py
"""
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diofinder import imu_persist as ip

_IDENTITY = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]


def _rot_z(deg):
    """Row-major flat-9 rotation about +Z by ``deg`` degrees."""
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return [c, -s, 0.0, s, c, 0.0, 0.0, 0.0, 1.0]


class TestExtrinsic(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self.path)  # start absent

    def tearDown(self):
        try:
            os.remove(self.path)
        except OSError:
            pass

    def test_absent_load_returns_none(self):
        self.assertIsNone(ip.load_extrinsic(self.path))

    def test_round_trip(self):
        q = {"n": 9, "r2": 0.99, "axis_diversity": 0.5}
        ip.save_extrinsic(_rot_z(30), q, path=self.path)
        loaded = ip.load_extrinsic(self.path)
        self.assertIsNotNone(loaded)
        R9, quality, saved_at = loaded
        for a, b in zip(R9, _rot_z(30)):
            self.assertAlmostEqual(a, b, places=9)
        self.assertEqual(quality["n"], 9)
        self.assertGreater(saved_at, 0.0)

    def test_clear(self):
        ip.save_extrinsic(_IDENTITY, {}, path=self.path)
        self.assertTrue(ip.clear_extrinsic(self.path))
        self.assertFalse(ip.clear_extrinsic(self.path))  # already gone
        self.assertIsNone(ip.load_extrinsic(self.path))

    def test_malformed_matrix_rejected_on_save(self):
        with self.assertRaises(ValueError):
            ip.save_extrinsic([1.0, 2.0, 3.0], {}, path=self.path)
        with self.assertRaises(ValueError):
            ip.save_extrinsic([float("nan")] * 9, {}, path=self.path)

    def test_corrupt_file_loads_none(self):
        with open(self.path, "w") as f:
            f.write("{ not json")
        self.assertIsNone(ip.load_extrinsic(self.path))
        # Valid JSON but wrong shape.
        with open(self.path, "w") as f:
            f.write('{"R": [1, 2, 3]}')
        self.assertIsNone(ip.load_extrinsic(self.path))

    def test_atomic_write_no_partial_on_failure(self):
        # A non-serialisable quality object must abort the write and leave no
        # stray temp files or a truncated target.
        class Bad:
            pass
        with self.assertRaises(TypeError):
            ip.save_extrinsic(_IDENTITY, {"x": Bad()}, path=self.path)
        self.assertFalse(os.path.exists(self.path))
        d = os.path.dirname(self.path)
        self.assertEqual([f for f in os.listdir(d)
                          if f.startswith(".imu_extr_")], [])


class TestSaveGate(unittest.TestCase):
    def test_save_worthy(self):
        self.assertTrue(ip.save_worthy(
            {"n": 8, "r2": 0.97, "axis_diversity": 0.4}))
        self.assertFalse(ip.save_worthy(
            {"n": 4, "r2": 0.99, "axis_diversity": 0.9}))   # too few pairs
        self.assertFalse(ip.save_worthy(
            {"n": 20, "r2": 0.9, "axis_diversity": 0.9}))   # r2 too low
        self.assertFalse(ip.save_worthy(
            {"n": 20, "r2": 0.99, "axis_diversity": 0.2}))  # collinear-ish
        self.assertFalse(ip.save_worthy(None))
        self.assertFalse(ip.save_worthy({}))


class TestDivergence(unittest.TestCase):
    def test_relative_angle_known(self):
        self.assertAlmostEqual(ip.relative_angle_deg(_IDENTITY, _IDENTITY),
                               0.0, places=6)
        self.assertAlmostEqual(ip.relative_angle_deg(_rot_z(10), _IDENTITY),
                               10.0, places=4)
        self.assertAlmostEqual(ip.relative_angle_deg(_rot_z(40), _rot_z(10)),
                               30.0, places=4)

    def test_diverged(self):
        self.assertFalse(ip.extrinsic_diverged(_rot_z(0.3), _IDENTITY,
                                               tol_deg=0.5))
        self.assertTrue(ip.extrinsic_diverged(_rot_z(3.0), _IDENTITY,
                                              tol_deg=2.0))   # remount
        # An unusable matrix always reads as diverged (so a bad seed is dropped).
        self.assertTrue(ip.extrinsic_diverged(None, _IDENTITY))
        self.assertTrue(ip.extrinsic_diverged([1, 2, 3], _IDENTITY))


class TestBNO055Profile(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self.path)

    def tearDown(self):
        try:
            os.remove(self.path)
        except OSError:
            pass

    def test_decode_calib_status(self):
        # sys=3 gyro=3 accel=3 mag=0 -> 0b11_11_11_00 = 0xFC
        self.assertEqual(ip.decode_calib_status(0xFC),
                         {"sys": 3, "gyro": 3, "accel": 3, "mag": 0})
        # all zero
        self.assertEqual(ip.decode_calib_status(0x00),
                         {"sys": 0, "gyro": 0, "accel": 0, "mag": 0})
        # gyro=2 accel=1 -> 0b00_10_01_00 = 0x24
        self.assertEqual(ip.decode_calib_status(0x24),
                         {"sys": 0, "gyro": 2, "accel": 1, "mag": 0})

    def test_profile_round_trip(self):
        blob = list(range(22))
        ip.save_bno055_profile(blob, status={"gyro": 3, "accel": 3},
                               path=self.path)
        self.assertEqual(ip.load_bno055_profile(self.path), blob)

    def test_profile_wrong_length_rejected(self):
        with self.assertRaises(ValueError):
            ip.save_bno055_profile(list(range(10)), path=self.path)

    def test_profile_corrupt_loads_none(self):
        self.assertIsNone(ip.load_bno055_profile(self.path))  # absent
        with open(self.path, "w") as f:
            f.write('{"calib": [1, 2, 3]}')                    # wrong length
        self.assertIsNone(ip.load_bno055_profile(self.path))

    def test_profile_clear(self):
        ip.save_bno055_profile(list(range(22)), path=self.path)
        self.assertTrue(ip.clear_bno055_profile(self.path))
        self.assertFalse(ip.clear_bno055_profile(self.path))


if __name__ == "__main__":
    unittest.main()
