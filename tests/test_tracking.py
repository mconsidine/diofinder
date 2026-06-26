#!/usr/bin/env python3
"""
Pure-logic unit tests for diofinder/tracking.py::roi_detect.

Hardware-free: tracking.py imports only numpy, and the per-window detector is
injected as a stub, so these tests run without sycamore (star_detect),
olive-solve (tetra3), or picamera2.

Run:
    python3 -m unittest tests.test_tracking -v
or directly:
    python3 tests/test_tracking.py
"""
import os
import sys
import unittest

# Make the repo root importable when run directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from diofinder import tracking


def _make_frame(h=200, w=200, stars=()):
    """Synthetic dark frame with bright 3x3 blobs at the given (x, y) centers."""
    frame = np.full((h, w), 5, dtype=np.uint8)
    for (sx, sy) in stars:
        ix, iy = int(round(sx)), int(round(sy))
        frame[max(0, iy - 1):iy + 2, max(0, ix - 1):ix + 2] = 250
    return frame


def _peak_detect_fn(window):
    """Stub detector: return the brightest pixel in the window as a single
    detection, in WINDOW-LOCAL (x, y, brightness, peak) coordinates. Returns []
    if the window has no clearly-bright pixel (peak below threshold), mimicking
    'no star in this window'."""
    peak = int(window.max())
    if peak < 100:
        return []
    # argmax over flattened -> (row, col) -> window-local (x=col, y=row).
    ry, rx = np.unravel_index(int(np.argmax(window)), window.shape)
    return [(float(rx), float(ry), float(peak), float(peak))]


class RoiDetectTests(unittest.TestCase):
    def test_recover_star_near_center(self):
        # Star at full-frame (100, 90); predict near it.
        frame = _make_frame(stars=[(100, 90)])
        stars, n_hit = tracking.roi_detect(
            frame, [(101, 89)], window_px=40, detect_fn=_peak_detect_fn)
        self.assertEqual(n_hit, 1)
        self.assertEqual(len(stars), 1)
        x, y, _, _ = stars[0]
        # Coordinate round-trip: recovered full-frame coords match the injection.
        self.assertAlmostEqual(x, 100, delta=1.0)
        self.assertAlmostEqual(y, 90, delta=1.0)

    def test_window_clamped_at_edge(self):
        # Star in the top-left corner; window must clamp and still find it.
        frame = _make_frame(stars=[(2, 3)])
        stars, n_hit = tracking.roi_detect(
            frame, [(2, 3)], window_px=40, detect_fn=_peak_detect_fn)
        self.assertEqual(n_hit, 1)
        x, y, _, _ = stars[0]
        self.assertAlmostEqual(x, 2, delta=1.0)
        self.assertAlmostEqual(y, 3, delta=1.0)

    def test_window_with_no_star_skipped(self):
        # Predict around an empty patch (no blob there) -> no recovery.
        frame = _make_frame(stars=[(150, 150)])
        stars, n_hit = tracking.roi_detect(
            frame, [(40, 40)], window_px=30, detect_fn=_peak_detect_fn)
        self.assertEqual(n_hit, 0)
        self.assertEqual(stars, [])

    def test_dedupe_overlapping_windows(self):
        # One real star; two overlapping predictions both centre near it. The
        # two windows each recover the same star -> dedupe to one.
        frame = _make_frame(stars=[(100, 100)])
        stars, n_hit = tracking.roi_detect(
            frame, [(99, 100), (101, 100)], window_px=40,
            detect_fn=_peak_detect_fn, dedupe_dist_px=5.0)
        # Both windows hit (the same star).
        self.assertEqual(n_hit, 2)
        # But after dedupe only one unique star remains.
        self.assertEqual(len(stars), 1)
        self.assertAlmostEqual(stars[0][0], 100, delta=1.0)
        self.assertAlmostEqual(stars[0][1], 100, delta=1.0)

    def test_coordinate_round_trip_multiple(self):
        injected = [(30, 40), (120, 60), (170, 150)]
        frame = _make_frame(stars=injected)
        preds = [(x + 1, y - 1) for (x, y) in injected]
        stars, n_hit = tracking.roi_detect(
            frame, preds, window_px=30, detect_fn=_peak_detect_fn)
        self.assertEqual(n_hit, 3)
        self.assertEqual(len(stars), 3)
        # Every injected star is recovered within a pixel.
        for (ix, iy) in injected:
            match = min(stars, key=lambda s: (s[0] - ix) ** 2 + (s[1] - iy) ** 2)
            self.assertAlmostEqual(match[0], ix, delta=1.0)
            self.assertAlmostEqual(match[1], iy, delta=1.0)

    def test_brightest_first_and_max_stars(self):
        # Three stars with distinct brightness via peak; cap to 2 brightest.
        frame = np.full((200, 200), 5, dtype=np.uint8)
        frame[50, 50] = 120     # dimmer
        frame[100, 100] = 200
        frame[150, 150] = 250   # brightest
        stars, _ = tracking.roi_detect(
            frame, [(50, 50), (100, 100), (150, 150)], window_px=20,
            detect_fn=_peak_detect_fn, max_stars=2)
        self.assertEqual(len(stars), 2)
        # Sorted brightest-first.
        self.assertGreaterEqual(stars[0][2], stars[1][2])
        # The dimmest (peak 120) was dropped by the cap.
        peaks = {int(s[2]) for s in stars}
        self.assertNotIn(120, peaks)

    def test_bad_window_detector_does_not_crash(self):
        # A detector that raises on one window must not sink the whole frame.
        frame = _make_frame(stars=[(100, 100), (50, 50)])
        calls = {"n": 0}

        def flaky(window):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return _peak_detect_fn(window)

        stars, n_hit = tracking.roi_detect(
            frame, [(100, 100), (50, 50)], window_px=30, detect_fn=flaky)
        # First window raised (skipped); second recovered.
        self.assertEqual(n_hit, 1)
        self.assertEqual(len(stars), 1)

    def test_centroids_to_xy_roundtrip(self):
        # Solver feeds (row, col) = (y, x); centroids_to_xy must invert it.
        cents = np.array([[90.0, 100.0], [40.0, 30.0]], dtype=np.float64)
        xy = tracking.centroids_to_xy(cents)
        self.assertEqual(xy, [(100.0, 90.0), (30.0, 40.0)])
        self.assertEqual(tracking.centroids_to_xy(None), [])

    def test_empty_predictions(self):
        frame = _make_frame()
        stars, n_hit = tracking.roi_detect(
            frame, [], window_px=40, detect_fn=_peak_detect_fn)
        self.assertEqual(stars, [])
        self.assertEqual(n_hit, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
