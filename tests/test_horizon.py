#!/usr/bin/env python3
"""
Pure-logic unit tests for diofinder/horizon.py — LST computation and the
opt-in / self-gating logic for the olive-solve horizon prune. No hardware,
no numpy, no star_detect.

Run:
    python3 -m unittest tests.test_horizon -v
or directly:
    python3 tests/test_horizon.py
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diofinder import horizon  # noqa: E402


class TestLST(unittest.TestCase):
    def test_gmst_at_j2000_epoch(self):
        # At 2000-01-01 12:00:00 UTC (JD 2451545.0) GMST = 280.46061837 deg.
        dt = datetime.datetime(2000, 1, 1, 12, 0, 0)
        lst = horizon.local_sidereal_time_deg(dt, 0.0)
        self.assertAlmostEqual(lst, 280.46061837, places=3)

    def test_longitude_shift_is_additive(self):
        dt = datetime.datetime(2026, 8, 21, 3, 30, 0)
        base = horizon.local_sidereal_time_deg(dt, 0.0)
        shifted = horizon.local_sidereal_time_deg(dt, 15.0)
        self.assertAlmostEqual((base + 15.0) % 360.0, shifted, places=9)

    def test_range_is_0_360(self):
        for hour in range(0, 24):
            for lon in (-179.0, -1.0, 0.0, 73.0, 179.0):
                dt = datetime.datetime(2026, 3, 1, hour, 0, 0)
                lst = horizon.local_sidereal_time_deg(dt, lon)
                self.assertTrue(0.0 <= lst < 360.0, f"{lst} at h={hour} lon={lon}")

    def test_sidereal_day_advance(self):
        # One solar day later, sidereal time advances ~360.9856 deg -> +0.9856.
        dt0 = datetime.datetime(2026, 8, 21, 0, 0, 0)
        dt1 = dt0 + datetime.timedelta(days=1)
        d = (horizon.local_sidereal_time_deg(dt1, 0.0)
             - horizon.local_sidereal_time_deg(dt0, 0.0)) % 360.0
        self.assertAlmostEqual(d, 0.98564736629, places=3)


class TestGating(unittest.TestCase):
    GOOD_CLOCK = datetime.datetime(2026, 8, 21, 3, 30, 0)

    def test_disabled_returns_empty(self):
        self.assertEqual(
            horizon.observer_solve_kwargs(False, 44.5, -73.0, self.GOOD_CLOCK), {})

    def test_unset_site_returns_empty(self):
        self.assertEqual(
            horizon.observer_solve_kwargs(True, 0.0, 0.0, self.GOOD_CLOCK), {})

    def test_bad_clock_returns_empty(self):
        stale = datetime.datetime(1970, 1, 1, 0, 0, 0)
        self.assertEqual(
            horizon.observer_solve_kwargs(True, 44.5, -73.0, stale), {})

    def test_all_gates_pass(self):
        kw = horizon.observer_solve_kwargs(True, 44.5, -73.0, self.GOOD_CLOCK)
        self.assertIn("observer_latitude", kw)
        self.assertIn("observer_lst", kw)
        self.assertEqual(kw["observer_latitude"], 44.5)
        self.assertTrue(0.0 <= kw["observer_lst"] < 360.0)
        # No boresight gate is passed (per-star prune only).
        self.assertNotIn("min_boresight_altitude", kw)

    def test_site_at_pole_is_considered_set(self):
        # Only exactly (0, 0) counts as unset; a real high-latitude site works.
        kw = horizon.observer_solve_kwargs(True, 89.0, 0.0, self.GOOD_CLOCK)
        self.assertIn("observer_lst", kw)

    def test_helpers(self):
        self.assertTrue(horizon.site_is_set(1.0, 0.0))
        self.assertFalse(horizon.site_is_set(0.0, 0.0))
        self.assertTrue(horizon.clock_is_plausible(self.GOOD_CLOCK))
        self.assertFalse(
            horizon.clock_is_plausible(datetime.datetime(2001, 1, 1)))


if __name__ == "__main__":
    unittest.main()
