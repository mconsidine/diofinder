#!/usr/bin/env python3
"""
Pure-logic unit tests for the matches-driven auto-exposure / gain controller
decision (`comms_proc._auto_exposure_decision`). Runnable WITHOUT picamera2 or
star_detect — comms_proc only imports diofinder-internal + stdlib modules, but we
stub star_detect defensively in case a transitive import ever appears.

Run:
    python3 -m unittest tests.test_auto_exposure -v
or directly:
    python3 tests/test_auto_exposure.py
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "star_detect" not in sys.modules:
    _stub = types.ModuleType("star_detect")
    _stub.detect_stars = lambda *a, **k: []
    _stub.set_num_threads = lambda n: None
    sys.modules["star_detect"] = _stub

from diofinder.comms_proc import _auto_exposure_decision


def _decide(**overrides):
    """Call the decision with sensible mid-range defaults, overriding as needed."""
    kw = dict(
        solved=True, stars=20, matches=10, peak=120,
        cur_s=0.2, cur_g=4.0,
        target_stars=20, target_matches=10,
        min_s=0.05, max_s=0.5, min_g=1.0, max_g=16.0,
    )
    kw.update(overrides)
    return _auto_exposure_decision(**kw)


class AutoExposureDecisionTests(unittest.TestCase):
    # --- saturation overrides everything, gain shed first -------------------
    def test_saturation_drops_gain_first(self):
        action = _decide(peak=255, cur_g=4.0)
        self.assertIn("gain", action)
        self.assertLess(action["gain"], 4.0)

    def test_saturation_at_gain_floor_drops_exposure(self):
        action = _decide(peak=255, cur_g=1.0, cur_s=0.2)
        self.assertIn("exposure_s", action)
        self.assertLess(action["exposure_s"], 0.2)

    def test_saturation_wins_over_low_count(self):
        # Saturated AND starved -> still back off, never expose longer.
        action = _decide(peak=255, matches=0, cur_g=4.0)
        self.assertIn("gain", action)

    # --- starved: gain-priority ladder --------------------------------------
    def test_starved_raises_gain_first(self):
        # Gain is the primary knob: a starved frame climbs gain before exposure.
        action = _decide(solved=True, matches=3, target_matches=10,
                         cur_s=0.2, max_s=0.5, cur_g=4.0, max_g=16.0)
        self.assertIn("gain", action)
        self.assertGreater(action["gain"], 4.0)

    def test_starved_stretches_exposure_only_at_max_gain(self):
        # Gain maxed and still starved -> only now stretch exposure (gain can't
        # manufacture photons in a genuinely dark scene).
        action = _decide(solved=True, matches=3, target_matches=10,
                         cur_s=0.2, max_s=0.5, cur_g=16.0, max_g=16.0)
        self.assertIn("exposure_s", action)
        self.assertGreater(action["exposure_s"], 0.2)

    def test_starved_at_both_ceilings_is_noop(self):
        self.assertIsNone(_decide(solved=True, matches=3, target_matches=10,
                                  cur_s=0.5, max_s=0.5, cur_g=16.0, max_g=16.0))

    # --- over-served: shed cost, gain back down first -----------------------
    def test_overserved_drops_gain_first(self):
        action = _decide(solved=True, matches=20, target_matches=10,
                         cur_g=4.0, min_g=1.0)
        self.assertIn("gain", action)
        self.assertLess(action["gain"], 4.0)

    def test_overserved_at_gain_floor_shortens_exposure(self):
        action = _decide(solved=True, matches=20, target_matches=10,
                         cur_g=1.0, min_g=1.0, cur_s=0.2, min_s=0.05)
        self.assertIn("exposure_s", action)
        self.assertLess(action["exposure_s"], 0.2)

    # --- deadband + metric selection ----------------------------------------
    def test_deadband_is_noop(self):
        self.assertIsNone(_decide(solved=True, matches=10, target_matches=10))

    def test_unsolved_falls_back_to_star_count(self):
        # Not solving: matches==0 must NOT drive the loop; use the star proxy.
        # Plenty of stars -> treated as over-served, sheds cost (not exposes up).
        action = _decide(solved=False, matches=0, stars=40, target_stars=20,
                         cur_g=4.0)
        self.assertIsNotNone(action)
        self.assertIn("gain", action)  # over-served on the star proxy
        self.assertLess(action["gain"], 4.0)

    def test_unsolved_starved_raises_gain(self):
        # Lost-in-space + too few stars -> starved on the star proxy -> gain
        # first (not exposure).
        action = _decide(solved=False, matches=0, stars=4, target_stars=20,
                         cur_s=0.2, max_s=0.5, cur_g=4.0, max_g=16.0)
        self.assertIn("gain", action)
        self.assertGreater(action["gain"], 4.0)

    def test_subthreshold_exposure_move_is_noop(self):
        # cur_s already at the floor and over-served at gain floor -> the
        # would-be exposure step is clamped below the 5 ms deadband -> noop.
        self.assertIsNone(_decide(solved=True, matches=20, target_matches=10,
                                  cur_g=1.0, min_g=1.0, cur_s=0.05, min_s=0.05))

    # --- nominal exposure anchor (settled regime) ---------------------------
    def test_settled_walks_exposure_back_toward_nominal(self):
        # In-deadband but exposure stretched above nominal with gain headroom ->
        # shorten exposure toward nominal (gain restores brightness next cycle).
        action = _decide(solved=True, matches=10, target_matches=10,
                         cur_s=0.4, nominal_s=0.2, cur_g=4.0, max_g=16.0)
        self.assertIn("exposure_s", action)
        self.assertLess(action["exposure_s"], 0.4)
        self.assertGreaterEqual(action["exposure_s"], 0.2)

    def test_settled_at_nominal_is_noop(self):
        self.assertIsNone(_decide(solved=True, matches=10, target_matches=10,
                                  cur_s=0.2, nominal_s=0.2, cur_g=4.0))

    def test_settled_below_nominal_lengthens_exposure(self):
        action = _decide(solved=True, matches=10, target_matches=10,
                         cur_s=0.1, nominal_s=0.2, cur_g=4.0, min_g=1.0)
        self.assertIn("exposure_s", action)
        self.assertGreater(action["exposure_s"], 0.1)
        self.assertLessEqual(action["exposure_s"], 0.2)

    def test_settled_above_nominal_but_gain_maxed_is_noop(self):
        # Can't compensate with gain (already maxed) -> leave the long exposure
        # in place rather than starve the frame.
        self.assertIsNone(_decide(solved=True, matches=10, target_matches=10,
                                  cur_s=0.4, nominal_s=0.2, cur_g=16.0, max_g=16.0))


if __name__ == "__main__":
    unittest.main()
