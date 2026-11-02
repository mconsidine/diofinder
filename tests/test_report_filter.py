"""Opt-in report smoother (imu_report_smoothing): new solves are blended into
the reported pointing instead of replacing it. Simulated slews, parked jitter,
a pan without IMU, and the mid-exposure timing helpers. No hardware."""
import math
import random

import pytest

from diofinder import report_filter as rf
from diofinder.imu_math import quat_mul, rotvec_to_quat


def sep(a, b):
    return rf.sep_deg(rf.radec_to_vec(*a), rf.radec_to_vec(*b))


def test_parked_jitter_is_averaged_and_converges_on_the_mean():
    rng = random.Random(1)
    sm = rf.ReportSmoother()
    truth = (150.0, 40.0)
    raw, out = [], []
    t, solve = 0.0, truth
    for k in range(200):                      # 4 polls/s, a solve every 3rd poll
        if k % 3 == 0:
            solve = (truth[0] + rng.gauss(0, 0.012) / math.cos(math.radians(40)),
                     truth[1] + rng.gauss(0, 0.012))
        raw.append(solve)
        out.append(sm.update(solve, "solve", solve, t))
        t += 0.25
    err_raw = [sep(p, truth) for p in raw[100:]]
    err_out = [sep(p, truth) for p in out[100:]]
    assert sum(err_out) / len(err_out) < 0.75 * sum(err_raw) / len(err_raw)
    # Steps between consecutive reports are smaller than the solve-to-solve hops.
    hop_raw = max(sep(a, b) for a, b in zip(raw[100:], raw[101:]))
    hop_out = max(sep(a, b) for a, b in zip(out[100:], out[101:]))
    assert hop_out < 0.6 * hop_raw


def test_no_stuck_offset_after_the_scope_stops():
    sm = rf.ReportSmoother()
    sm.update((10.0, 0.0), "imu", 1, 0.0)                 # moving on the IMU
    sm.update((10.3, 0.0), "imu", 1, 0.5)
    # Stops: the gate hands over to the solve, 0.04 deg from the report.
    t, last = 0.75, None
    for k in range(20):
        last = sm.update((10.34, 0.0), "solve", (10.34, 0.0), t)
        t += 0.25
    assert sep(last, (10.34, 0.0)) < 0.002                # settled ON the solve (5 s)


def test_slew_reanchor_steps_are_smaller_than_snapping():
    """2 deg/s slew; the IMU prediction runs 10% fast, so every new solve
    pulls it back ~0.066 deg. Shipped path: a 0.066 deg jump per solve.
    Smoothed: each jump is cut by the gain, lag stays bounded."""
    sm = rf.ReportSmoother(gain=0.3)
    rate, dt = 2.0, 0.25
    t, anchor_t, anchor_ra = 0.0, 0.0, 0.0
    worst_extra, worst_err, late_err = 0.0, 0.0, 0.0
    prev_out, prev_truth = None, None
    for k in range(60):
        truth = rate * t
        if k % 2 == 0 and k:                              # a solve every 0.5 s
            anchor_t, anchor_ra = t, truth
        pred = anchor_ra + 1.1 * rate * (t - anchor_t)    # 10% scale error
        out = sm.update((pred, 0.0), "imu", anchor_t, t)
        if prev_out is not None:
            step = (out[0] - prev_out[0]) - (truth - prev_truth)    # beyond real motion
            worst_extra = max(worst_extra, abs(step))
        worst_err = max(worst_err, abs(out[0] - truth))
        if k >= 40:
            late_err = max(late_err, abs(out[0] - truth))
        prev_out, prev_truth = out, truth
        t += dt
    snap_jump = 0.1 * rate * 0.5                          # what the shipped path does
    assert worst_extra < 0.6 * snap_jump + 0.05 * rate * dt
    assert worst_err < 0.15                               # bounded lag/bias
    # The learned drift cancels the IMU's scale error: the report ends up
    # closer to the truth than the shipped path ever is (0.1 deg before each solve).
    assert late_err < 0.02
    assert sm.status()["drift_deg_s"] == pytest.approx(0.1 * rate, rel=0.15)
    # The scope stops: the solve takes over and the drift is dropped.
    sm.update((truth, 0.0), "solve", (truth, 0.0), t)
    assert sm.status()["drift_deg_s"] == 0.0


def test_pan_without_imu_follows_each_solve():
    sm = rf.ReportSmoother()
    for k in range(10):                                   # 0.3 deg per solve: motion
        target = (20.0 + 0.3 * k, 5.0)
        out = sm.update(target, "solve", target, 0.5 * k)
        assert sep(out, target) < 1e-6
        assert sm.last_event in (None, "follow")


def test_big_disagreement_snaps_and_long_gap_resets():
    sm = rf.ReportSmoother()
    sm.update((0.0, 0.0), "imu", 1, 0.0)
    out = sm.update((3.0, 0.0), "imu", 2, 0.25)           # 3 deg off: snap
    assert sep(out, (3.0, 0.0)) < 1e-9 and sm.last_event == "snap"
    sm.update((3.2, 0.0), "solve", "a", 0.5)              # switch: continuity
    out = sm.update((50.0, 10.0), "solve", "b", 100.0)    # long silence: start over
    assert sep(out, (50.0, 10.0)) < 1e-9


def test_same_poll_twice_is_stable():
    sm = rf.ReportSmoother()
    sm.update((1.0, 1.0), "imu", 1, 0.0)
    a = sm.update((1.02, 1.0), "imu", 2, 0.25)
    b = sm.update((1.02, 1.0), "imu", 2, 0.25)            # :GD right after :GR
    assert a == b


def test_works_at_the_pole():
    sm = rf.ReportSmoother()
    sm.update((0.0, 89.99), "imu", 1, 0.0)
    out = sm.update((180.0, 89.98), "imu", 2, 0.25)       # 0.03 deg across the pole
    assert out[1] > 89.97


def test_interp_quat_and_mid_exposure_time():
    q0 = (1.0, 0.0, 0.0, 0.0)
    q1 = quat_mul(rotvec_to_quat((0.0, 0.0, math.radians(1.0))), q0)
    h = [(10.0, q0), (10.1, q1)]
    qm = rf.interp_quat(h, 10.05)
    ang = 2 * math.degrees(math.acos(min(1.0, abs(sum(a * b for a, b in zip(qm, q0))))))
    assert ang == pytest.approx(0.5, abs=1e-3)
    assert rf.interp_quat(h, 9.0) is None and rf.interp_quat(h, 11.0) is None
    assert rf.interp_quat(h, 10.12) == q1                 # just past the end: last
    assert rf.interp_quat([], 10.0) is None
    meta = {"sensor_timestamp_ns": 12_500_000_000, "exposure_us": 400_000}
    assert rf.mid_exposure_monotonic(meta, 0.5) == pytest.approx(12.5 - 0.2 - 0.5)
    assert rf.mid_exposure_monotonic({}, 0.0) is None


def test_comms_switch_routes_through_the_smoother(monkeypatch):
    from diofinder import comms_proc as cp
    monkeypatch.setattr(cp, "_report_smoother", rf.ReportSmoother())
    sol = {"ra_deg": 100.0, "dec_deg": 20.0}
    off = {"report_epoch": "j2000", "imu_available": False}
    on = dict(off, imu_report_smoothing=True)
    assert cp._report_radec(off, sol) == (100.0, 20.0)
    assert cp._report_radec(on, sol) == pytest.approx((100.0, 20.0))
    # A 0.02 deg solve-to-solve wobble: shipped path jumps, smoother averages.
    sol2 = {"ra_deg": 100.0, "dec_deg": 20.02}
    assert cp._report_radec(off, sol2) == (100.0, 20.02)
    ra, dec = cp._report_radec(on, sol2)
    assert 20.0 < dec < 20.02 and dec == pytest.approx(20.0 + 0.3 * 0.02, abs=2e-3)


def test_solver_takes_the_attitude_at_mid_exposure(monkeypatch):
    from diofinder import frame_meta as fm
    from diofinder import solver_proc as sp
    monkeypatch.setattr(sp.time, "clock_gettime", lambda clk: 100.0)
    monkeypatch.setattr(sp.time, "monotonic", lambda: 100.0)     # boottime == monotonic
    q0 = (1.0, 0.0, 0.0, 0.0)
    q1 = quat_mul(rotvec_to_quat((0.0, 0.0, math.radians(2.0))), q0)
    hist = ((99.0, q0), (99.5, q1))
    # Frame 7: readout started at 99.6 s after a 0.4 s exposure -> mid 99.4.
    ring = fm.push((), (7, int(99.6e9), 400_000, 4.0, 0))
    src = {"imu_report_smoothing": True, "imu": (q1, 99.5, hist), "frame_meta": ring}
    q = sp._mid_exposure_imu_q(src, 7)
    ang = 2 * math.degrees(math.acos(min(1.0, abs(sum(a * b for a, b in zip(q, q0))))))
    assert ang == pytest.approx(1.6, abs=0.01)                   # 80% of the way
    assert sp._mid_exposure_imu_q(dict(src, imu_report_smoothing=False), 7) is None
    assert sp._mid_exposure_imu_q(dict(src, imu=(q1, 99.5)), 7) is None   # no history
    assert sp._mid_exposure_imu_q(src, 8) is None                         # no metadata
