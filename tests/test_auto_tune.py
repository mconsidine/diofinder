#!/usr/bin/env python3
"""
Pure-logic unit tests for the offline auto-tune candidate selection /
merit function (`comms_proc._auto_tune_select` / `_auto_tune_cost`). No
hardware, no solver — comms_proc imports only efinder-internal + stdlib
modules; star_detect is stubbed defensively.

Run:
    python3 -m unittest tests.test_auto_tune -v
or directly:
    python3 tests/test_auto_tune.py
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

from efinder.comms_proc import (
    _auto_tune_select, _auto_tune_cost, _AT_BG_COST,
    _auto_tune_valid_samples, _auto_tune_row, _AT_SIGNAL_FLOOR,
)


def _sample(peak=120, solved=True, matches=12, stars=20, solve_ms=300.0):
    return {"peak": peak, "solved": solved, "matches": matches,
            "stars": stars, "solve_ms": solve_ms}


def _row(bg="row_percentile", kernel=1.5, sigma=5.0,
         rate=1.0, matches=12.0, solve_ms=300.0):
    return {
        "bg_mode": bg, "kernel_sigma": kernel, "sigma": sigma,
        "match_rate": rate, "mean_matches": matches,
        "median_solve_ms": solve_ms,
    }


class AutoTuneSampleGatingTests(unittest.TestCase):
    CAND = {"bg_mode": "row_percentile", "kernel_sigma": 1.5, "sigma": 5.0}

    def test_drops_no_signal_samples(self):
        # Two good samples + two dark (no-signal) ones: the dark grabs are
        # artifacts and must not be averaged in.
        samples = [_sample(peak=120, solved=True, matches=12),
                   _sample(peak=5,  solved=False, matches=0),   # dark
                   _sample(peak=130, solved=True, matches=14),
                   _sample(peak=2,  solved=False, matches=0)]   # dark
        valid = _auto_tune_valid_samples(samples)
        self.assertEqual(len(valid), 2)
        row = _auto_tune_row(self.CAND, samples)
        self.assertEqual(row["n_frames"], 2)
        self.assertEqual(row["n_dropped"], 2)
        self.assertEqual(row["match_rate"], 1.0)          # not 0.5
        self.assertEqual(row["mean_matches"], 13.0)

    def test_signal_present_but_unsolved_counts_as_failure(self):
        # Good peak but didn't solve -> a REAL vote against the candidate (kept).
        samples = [_sample(peak=120, solved=True, matches=12),
                   _sample(peak=110, solved=False, matches=0)]  # real fail
        row = _auto_tune_row(self.CAND, samples)
        self.assertEqual(row["n_frames"], 2)
        self.assertEqual(row["n_dropped"], 0)
        self.assertEqual(row["match_rate"], 0.5)

    def test_all_no_signal_is_indeterminate(self):
        samples = [_sample(peak=3, solved=False, matches=0),
                   _sample(peak=8, solved=False, matches=0)]
        self.assertEqual(_auto_tune_valid_samples(samples), [])
        self.assertIsNone(_auto_tune_row(self.CAND, samples))

    def test_floor_boundary_inclusive(self):
        samples = [_sample(peak=_AT_SIGNAL_FLOOR, solved=True, matches=9)]
        self.assertEqual(len(_auto_tune_valid_samples(samples)), 1)


class AutoTuneSelectTests(unittest.TestCase):
    def test_empty_is_none(self):
        self.assertEqual(_auto_tune_select([], 10, 0.6), (None, False))

    def test_picks_cheapest_feasible(self):
        cheap = _row(bg="row_percentile", kernel=1.5, sigma=8.0, solve_ms=200)
        pricey = _row(bg="block_percentile", kernel=2.5, sigma=4.0, solve_ms=500)
        best, met = _auto_tune_select([pricey, cheap], target_matches=10,
                                      match_rate_floor=0.6)
        self.assertTrue(met)
        self.assertIs(best, cheap)

    def test_prefers_larger_sigma_smaller_kernel_when_otherwise_equal(self):
        a = _row(kernel=2.5, sigma=4.0)   # wider kernel, lower sigma -> costlier
        b = _row(kernel=1.5, sigma=8.0)   # tighter kernel, higher sigma -> cheaper
        self.assertLess(_auto_tune_cost(b), _auto_tune_cost(a))
        best, met = _auto_tune_select([a, b], 10, 0.6)
        self.assertIs(best, b)
        self.assertTrue(met)

    def test_cheaper_bg_breaks_tie(self):
        row_pct = _row(bg="row_percentile")
        block = _row(bg="block_percentile")
        self.assertLess(_auto_tune_cost(row_pct), _auto_tune_cost(block))

    def test_every_sweepable_bg_mode_is_scored(self):
        # Each background mode auto_tune can evaluate must have an explicit cost,
        # so a user-supplied bg_modes list is ranked deliberately rather than via
        # the .get() fallback. uniform_mean in particular was previously missing.
        sweepable = {"row_percentile", "line_median", "column_percentile",
                     "row_column_percentile", "block_percentile",
                     "uniform_mean", "top_hat"}
        self.assertTrue(sweepable.issubset(_AT_BG_COST.keys()),
                        msg=f"missing: {sweepable - set(_AT_BG_COST)}")

    def test_uniform_mean_scored_between_block_and_tophat(self):
        # uniform_mean (full-image SAT mean, cache-incompatible) should cost more
        # than the per-tile block model but less than the morphological top-hat.
        um = _row(bg="uniform_mean")
        block = _row(bg="block_percentile")
        top = _row(bg="top_hat")
        self.assertLess(_auto_tune_cost(block), _auto_tune_cost(um))
        self.assertLess(_auto_tune_cost(um), _auto_tune_cost(top))

    def test_match_rate_floor_excludes(self):
        # High matches but rate below the floor -> not feasible.
        flaky = _row(rate=0.4, matches=20.0)
        best, met = _auto_tune_select([flaky], target_matches=10,
                                      match_rate_floor=0.6)
        self.assertFalse(met)             # nothing feasible
        self.assertIs(best, flaky)        # best-effort fallback

    def test_mean_matches_need_excludes(self):
        # target 10 -> need 8; a row at 7 mean matches is infeasible.
        thin = _row(rate=1.0, matches=7.0)
        best, met = _auto_tune_select([thin], target_matches=10,
                                      match_rate_floor=0.6)
        self.assertFalse(met)

    def test_no_feasible_picks_max_mean_matches(self):
        low = _row(rate=0.3, matches=4.0)
        mid = _row(rate=0.3, matches=9.0)
        best, met = _auto_tune_select([low, mid], target_matches=10,
                                      match_rate_floor=0.6)
        self.assertFalse(met)
        self.assertIs(best, mid)

    def test_feasible_beats_cheaper_infeasible(self):
        # An infeasible row can have a lower raw cost, but feasibility wins.
        infeasible_cheap = _row(rate=0.2, kernel=1.5, sigma=8.0, solve_ms=100)
        feasible = _row(rate=1.0, kernel=2.5, sigma=4.0, solve_ms=800)
        best, met = _auto_tune_select([infeasible_cheap, feasible],
                                      target_matches=10, match_rate_floor=0.6)
        self.assertTrue(met)
        self.assertIs(best, feasible)


if __name__ == "__main__":
    unittest.main()
