"""Unit tests for the pointing_log diagnostic's pure logic. No hardware,
no daemon — synthetic record streams drive summarize() through the exact
signatures the two field experiments look for.
"""
import math

import pytest

from tests.pointing_log import (parse_ra, parse_dec, angsep_deg, summarize,
                                sample_once, run_log, FIELDS)


# ---- LX200 parsing -----------------------------------------------------------

def test_parse_ra_and_dec():
    assert parse_ra("19:22:52#") == pytest.approx((19 + 22/60 + 52/3600) * 15)
    assert parse_dec("+34*57:38#") == pytest.approx(34 + 57/60 + 38/3600)
    assert parse_dec("-05*30:00#") == pytest.approx(-(5 + 0.5))
    assert parse_ra("00:00:00#") == 0.0
    assert parse_ra(None) is None and parse_dec(None) is None
    assert parse_ra("garbage") is None and parse_dec("garbage") is None


def test_parse_dec_negative_below_one_degree():
    # Sign lives on the degrees field; -00*30:00 must not come back positive.
    assert parse_dec("-00*30:00#") == pytest.approx(-0.5)


def test_angsep():
    assert angsep_deg(0, 0, 0, 1) == pytest.approx(1.0)
    assert angsep_deg(0, 89, 180, 89) == pytest.approx(2.0, abs=1e-6)
    assert angsep_deg(None, 0, 0, 0) is None


# ---- record builders ---------------------------------------------------------

def _rec(t, ra, dec, solved=True, stars=40, matches=30, age=0.2,
         solve_ra=None, solve_dec=None):
    return {"t": t, "wall": "", "lx_ra": ra, "lx_dec": dec,
            "solve_ra": solve_ra if solve_ra is not None else ra,
            "solve_dec": solve_dec if solve_dec is not None else dec,
            "solved": solved, "stars": stars, "matches": matches,
            "age_s": age, "stale": False, "seq": None}


def test_empty_and_degenerate():
    s = summarize([])
    assert s["n"] == 0 and s["repeat_frac"] is None
    s1 = summarize([_rec(0.0, 100.0, 20.0)])
    assert s1["n"] == 1 and s1["repeat_frac"] is None   # needs >= 2 points


def test_stair_stepping_signature():
    # 4 Hz polls, position advancing once per ~0.35 s solve: two polls repeat,
    # then a hop. This is the "no interpolation" case.
    recs, dec = [], 20.0
    for i in range(40):
        if i % 3 == 0 and i:
            dec += 0.35                      # one solve-interval of motion
        recs.append(_rec(i * 0.25, 100.0, dec))
    s = summarize(recs)
    assert s["repeat_frac"] > 0.5            # most polls repeat
    assert s["step_median_deg"] == pytest.approx(0.35, abs=0.01)
    assert s["reversals"] == 0               # monotonic: no oscillation
    assert s["travel_deg"] == pytest.approx(0.35 * 13, abs=0.05)


def test_smooth_interpolated_motion():
    # The IMU filling in between solves: every poll advances a little.
    recs = [_rec(i * 0.25, 100.0, 20.0 + i * 0.0875) for i in range(40)]
    s = summarize(recs)
    assert s["repeat_frac"] == 0.0
    assert s["reversals"] == 0
    assert s["rate_dps"] == pytest.approx(0.35, abs=0.02)


def test_oscillation_signature_is_caught():
    # A steady slew with the reticle alternating back and forth ~1.6 deg —
    # the v0.11.63 IMU-hunting pattern seen in the real SkySafari log.
    recs, dec = [], 20.0
    for i in range(40):
        dec += 0.1
        wobble = 1.6 if i % 2 else 0.0
        recs.append(_rec(i * 0.25, 100.0, dec + wobble))
    s = summarize(recs)
    assert s["reversals"] > 5
    assert s["reversal_max_deg"] > 1.0


def test_reversals_ignore_quantisation_noise():
    # Sub-threshold jitter on a monotonic ramp must NOT be called oscillation.
    recs, dec = [], 20.0
    for i in range(40):
        dec += 0.1
        recs.append(_rec(i * 0.25, 100.0, dec + (0.004 if i % 2 else 0.0)))
    s = summarize(recs)
    assert s["reversals"] == 0


def test_dropout_and_solve_stats():
    recs = []
    for i in range(20):
        solved = not (5 <= i < 13)          # 8 samples * 0.25 s = ~1.75 s gap
        recs.append(_rec(i * 0.25, 100.0, 20.0, solved=solved,
                         stars=40 if solved else 3,
                         matches=30 if solved else 0))
    s = summarize(recs)
    assert s["solved_frac"] == pytest.approx(0.6, abs=0.01)
    assert s["dropout_max_s"] == pytest.approx(1.75, abs=0.01)


def test_imu_involvement_detected_and_absent():
    # Solve-only: LX200 == status report -> imu_active_frac 0.
    same = [_rec(i * 0.25, 100.0, 20.0 + i * 0.01) for i in range(20)]
    assert summarize(same)["imu_active_frac"] == 0.0

    # IMU steering: LX200 diverges from the (frozen) solve position.
    steered = [_rec(i * 0.25, 100.0, 20.0 + i * 0.05,
                    solve_ra=100.0, solve_dec=20.0) for i in range(20)]
    s = summarize(steered)
    assert s["imu_active_frac"] > 0.5
    assert s["imu_offset_max_deg"] > 0.5


def test_summarize_tolerates_missing_lx_and_maint():
    # A run where the LX200 socket failed (all None) must not raise.
    recs = [{"t": i * 0.25, "wall": "", "lx_ra": None, "lx_dec": None,
             "solve_ra": None, "solve_dec": None, "solved": None,
             "stars": None, "matches": None, "age_s": None,
             "stale": None, "seq": None} for i in range(10)]
    s = summarize(recs)
    assert s["n"] == 10 and s["n_lx"] == 0
    assert s["repeat_frac"] is None and s["imu_active_frac"] is None


# ---- sampling layer (injected fakes) ----------------------------------------

class _FakeResp:
    def __init__(self, result):
        self.ok, self.result, self.error = True, result, ""


def test_sample_once_merges_both_sources():
    def lx(cmd):
        return {":GR#": "19:22:52#", ":GD#": "+34*57:38#"}[cmd]

    def maint(cmd, args=None, timeout=None):
        assert cmd == "status"
        return _FakeResp({"solution": {"report_ra_deg": 290.7,
                                       "report_dec_deg": 34.96,
                                       "solved": True, "stars": 42,
                                       "matches": 34, "seq": 5085},
                          "pointing_age_s": 0.3, "pointing_stale": False})

    r = sample_once(lx, maint, t0=0.0)
    assert set(FIELDS) <= set(r)
    assert r["lx_ra"] == pytest.approx(290.7167, abs=1e-3)
    assert r["lx_dec"] == pytest.approx(34.9606, abs=1e-3)
    assert r["solve_ra"] == 290.7 and r["solved"] is True
    assert r["matches"] == 34 and r["age_s"] == 0.3


def test_sample_once_survives_maint_failure():
    r = sample_once(lambda c: "19:22:52#" if c == ":GR#" else "+34*57:38#",
                    lambda *a, **k: (_ for _ in ()).throw(OSError("down")),
                    t0=0.0)
    assert r["lx_ra"] is not None      # LX200 side still recorded
    assert r["solved"] is None         # maint side simply absent


def test_run_log_respects_rate_and_maint_stride():
    calls = {"lx": 0, "maint": 0}

    def lx(cmd):
        calls["lx"] += 1
        return "19:22:52#" if cmd == ":GR#" else "+34*57:38#"

    def maint(cmd, args=None, timeout=None):
        calls["maint"] += 1
        return _FakeResp({"solution": {}, "pointing_age_s": 0.1})

    recs = run_log(0.5, hz=20.0, maint_hz=5.0, lx_query=lx, maint_call=maint)
    assert 5 <= len(recs) <= 14                 # ~10 samples in 0.5 s @20 Hz
    assert calls["lx"] == 2 * len(recs)         # :GR# + :GD# each sample
    # maint sampled every 4th (20/5) sample, so far fewer than lx samples
    assert 0 < calls["maint"] <= math.ceil(len(recs) / 4) + 1
    assert all(r["t"] >= 0 for r in recs)
