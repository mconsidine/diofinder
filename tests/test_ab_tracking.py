"""Pure-logic tests for the tracking A/B harness (tests/ab_tracking.py).

Covers _summarize, _sky_offset_arcmin, and _verdict — the socket-free decision
core. No daemon required.
"""
import math

import types

from tests.ab_tracking import _summarize, _sky_offset_arcmin, _verdict, run_ab


class _FakeMaint:
    """Simulates the daemon for run_ab: tracking_status / solver_params_set /
    solve_stats, returning FULL-tagged records while tracking is off and
    TRACKING-tagged records while it's on (locks immediately)."""

    def __init__(self, full_recs, track_recs, lock=True):
        self.tracking = False
        self.now = 1000.0
        self.lock = lock
        self.full_recs = full_recs
        self.track_recs = track_recs
        self.set_calls = []

    def __call__(self, cmd, args=None, timeout=10.0):
        self.now += 0.5
        if cmd == "tracking_status":
            state = "TRACKING" if (self.tracking and self.lock) else "FULL"
            return types.SimpleNamespace(
                ok=True, error=None,
                result={"enabled": self.tracking, "state": state})
        if cmd == "solver_params_set":
            if args and "tracking_enabled" in args:
                self.tracking = bool(args["tracking_enabled"])
                self.set_calls.append(self.tracking)
            return types.SimpleNamespace(ok=True, error=None, result=args or {})
        if cmd == "solve_stats":
            recs = self.track_recs if self.tracking else self.full_recs
            return types.SimpleNamespace(
                ok=True, error=None, result={"records": recs, "now": self.now})
        return types.SimpleNamespace(ok=False, error="unknown", result=None)


def _rec(epoch, tracked, solve_ms, matches, ra, dec, extract_ms=6.0):
    return (epoch, tracked, solve_ms, extract_ms, matches, ra, dec)


def test_summarize_empty():
    assert _summarize([]) == {"n": 0}


def test_summarize_medians_and_spread():
    recs = [_rec(i, False, 10.0 + i, 12, 100.0 + 0.001 * i, 20.0) for i in range(5)]
    s = _summarize(recs)
    assert s["n"] == 5
    assert s["solve_ms_p50"] == 12.0
    assert s["matches_p50"] == 12
    assert abs(s["ra_p50"] - 100.002) < 1e-6
    assert s["dec_spread"] == 0.0
    assert s["ra_spread"] < 0.01


def test_summarize_ra_wraps_across_zero():
    # Points straddling RA=0 must not blow the spread up to ~360.
    recs = [_rec(0, False, 10, 12, 359.9, 5.0), _rec(1, False, 10, 12, 0.1, 5.0)]
    s = _summarize(recs)
    assert s["ra_spread"] < 1.0


def test_sky_offset_same_field_small():
    full = _summarize([_rec(0, False, 10, 12, 100.0, 20.0)])
    track = _summarize([_rec(0, True, 5, 12, 100.02, 20.01)])
    off = _sky_offset_arcmin(full, track)
    # ~0.02 deg in RA*cos(dec) + 0.01 deg dec -> ~1.3'
    assert 0.5 < off < 3.0


def test_verdict_enable_when_faster_and_correct():
    full = _summarize([_rec(i, False, 12.0, 12, 100.0, 20.0) for i in range(20)])
    track = _summarize([_rec(i, True, 3.0, 12, 100.001, 20.0) for i in range(20)])
    v = _verdict(full, track, full_rate=3.0, track_rate=3.2)
    assert v["correctness_ok"] is True
    assert v["recommend"] == "enable"
    assert v["speedup_pct"] > 50


def test_verdict_keepoff_when_pointing_diverges():
    full = _summarize([_rec(i, False, 12.0, 12, 100.0, 20.0) for i in range(20)])
    # 2 degrees off -> way past the 18' gate.
    track = _summarize([_rec(i, True, 3.0, 12, 102.0, 20.0) for i in range(20)])
    v = _verdict(full, track, full_rate=3.0, track_rate=3.2)
    assert v["correctness_ok"] is False
    assert v["recommend"] == "keep-off"
    assert any("divergent" in r for r in v["reasons"])


def test_verdict_keepoff_when_no_tracking_solves():
    full = _summarize([_rec(i, False, 12.0, 12, 100.0, 20.0) for i in range(20)])
    v = _verdict(full, _summarize([]), full_rate=3.0, track_rate=0.0)
    assert v["correctness_ok"] is False
    assert v["recommend"] == "keep-off"


def test_verdict_neutral_when_correct_but_no_gain():
    full = _summarize([_rec(i, False, 10.0, 12, 100.0, 20.0) for i in range(20)])
    track = _summarize([_rec(i, True, 9.8, 12, 100.001, 20.0) for i in range(20)])
    v = _verdict(full, track, full_rate=3.0, track_rate=3.0)
    assert v["correctness_ok"] is True
    assert v["recommend"] == "neutral"


def test_verdict_flags_unstable_spread():
    # Correct median but big RA scatter -> unstable, keep off.
    full = _summarize([_rec(i, False, 10.0, 12, 100.0, 20.0) for i in range(20)])
    scatter = [100.0 + (0.8 if i % 2 else -0.8) for i in range(20)]
    track = _summarize([_rec(i, True, 3.0, 12, scatter[i], 20.0) for i in range(20)])
    v = _verdict(full, track, full_rate=3.0, track_rate=3.2)
    assert v["correctness_ok"] is False
    assert any("spread" in r for r in v["reasons"])


def test_run_ab_structured_result_and_restore():
    full = [_rec(i, False, 12.0, 12, 100.0, 20.0) for i in range(15)]
    track = [_rec(i, True, 3.0, 12, 100.001, 20.0) for i in range(15)]
    fake = _FakeMaint(full, track)
    progress = []
    res = run_ab(0.0, 0.0, 1.0, maint_call=fake, progress=progress.append)
    assert res["ok"] is True
    assert res["full"]["n"] == 15 and res["track"]["n"] == 15
    assert res["verdict"]["correctness_ok"] is True
    assert res["verdict"]["recommend"] == "enable"
    # Tracking was toggled on for phase B, then RESTORED to the original (off).
    assert fake.set_calls[-1] is False
    assert res["restored_to"] is False
    assert progress  # progress callback fired


def test_run_ab_aborts_when_not_solving():
    fake = _FakeMaint(full_recs=[], track_recs=[])
    res = run_ab(0.0, 0.0, 1.0, maint_call=fake)
    assert res["ok"] is False
    assert "not solving" in res["aborted"].lower()
    assert fake.set_calls[-1] is False   # still restored


def test_run_ab_aborts_when_never_locks():
    full = [_rec(i, False, 12.0, 12, 100.0, 20.0) for i in range(10)]
    fake = _FakeMaint(full, track_recs=[], lock=False)   # never reports TRACKING
    res = run_ab(0.0, 0.0, 0.2, maint_call=fake)
    assert res["ok"] is False
    assert "never locked" in res["aborted"].lower()
    assert fake.set_calls[-1] is False


def test_run_ab_handles_maint_raising_midrun():
    # A daemon error partway through must yield ok=False with the error set,
    # and the finally must still attempt to restore tracking.
    class _Raising(_FakeMaint):
        def __call__(self, cmd, args=None, timeout=10.0):
            if cmd == "solve_stats":
                from diofinder.maint import MaintResponse
                return MaintResponse(ok=False, error="solver did not respond")
            return super().__call__(cmd, args, timeout)
    full = [_rec(i, False, 12.0, 12, 100.0, 20.0) for i in range(5)]
    fake = _Raising(full, [])
    res = run_ab(0.0, 0.0, 1.0, maint_call=fake)
    assert res["ok"] is False
    assert res["error"] and "solve_stats" in res["error"]
    assert fake.set_calls[-1] is False   # restored despite the error
