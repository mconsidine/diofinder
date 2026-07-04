"""Unit tests for the IMU alpha-beta pointing filter (LX200 jitter fix).

Covers the pure alpha-beta math in diofinder.imu_math and the stateful
_imu_predict_smoothed wrapper in comms_proc (snap / cache / smooth / re-anchor).
No hardware required.
"""
import math
import statistics
import random
import time

import pytest

from diofinder import comms_proc
from diofinder.imu_math import alpha_beta_step, wrap180

A, B, DT = 0.25, 0.05, 0.05  # match comms_proc._IMU_AB_ALPHA / _BETA, 20 Hz


def test_wrap180():
    assert wrap180(10) == pytest.approx(10)
    assert wrap180(190) == pytest.approx(-170)
    assert wrap180(350) == pytest.approx(-10)
    assert wrap180(-350) == pytest.approx(10)
    assert wrap180(360) == pytest.approx(0)


def test_converges_when_stationary():
    st = (98.0, 18.0, 0.0, 0.0)
    for _ in range(400):
        st = alpha_beta_step(st, 100.0, 20.0, DT, A, B)
    assert st[0] == pytest.approx(100.0, abs=1e-3)
    assert st[1] == pytest.approx(20.0, abs=1e-3)


def test_reduces_noise_variance():
    rnd = random.Random(0)
    zs = [100.0 + rnd.gauss(0, 1.0) for _ in range(800)]
    st = (100.0, 20.0, 0.0, 0.0)
    out = []
    for z in zs:
        st = alpha_beta_step(st, z, 20.0, DT, A, B)
        out.append(st[0])
    warm, raw = out[200:], zs[200:]
    # Filtered series is materially smoother than the raw measurements, and
    # the mean is preserved (no bias).
    assert statistics.pstdev(warm) < statistics.pstdev(raw)
    assert statistics.mean(warm) == pytest.approx(100.0, abs=0.1)


def test_tracks_steady_ramp_without_lag_bias():
    # A constant-velocity slew (2 deg/s): the velocity term must cancel the
    # ramp so there is no steady-state position lag (the EMA-on-position
    # alternative would lag here).
    rate, last_z = 2.0, None
    st = (50.0, 20.0, 0.0, 0.0)
    for i in range(600):
        last_z = 50.0 + rate * (i * DT)
        st = alpha_beta_step(st, last_z, 20.0, DT, A, B)
    assert st[2] == pytest.approx(rate, abs=0.2)        # velocity learned
    assert abs(wrap180(st[0] - last_z)) < 0.3           # no lag bias


def test_ra_wraps_the_short_way():
    # Crossing the 0/360 boundary: the estimate must advance forward, not jump
    # ~358 deg backward.
    st = alpha_beta_step((359.0, 20.0, 0.0, 0.0), 1.0, 20.0, DT, A, B)
    moved = wrap180(st[0] - 359.0)
    assert moved > 0          # forward toward 1 deg
    assert moved < 1.0        # short-way correction, not a 358 deg jump


def test_smoothed_wrapper_snap_cache_smooth_reanchor(monkeypatch):
    comms_proc._imu_filt_state.clear()
    cfg = {"imu_t": 1.0, "imu_ref_t": 100.0, "_z": (10.0, 5.0)}
    monkeypatch.setattr(comms_proc, "_imu_predict", lambda c: c["_z"])

    # First sample snaps exactly to the raw prediction.
    assert comms_proc._imu_predict_smoothed(cfg) == (10.0, 5.0)

    # Same IMU sample (e.g. :GR# then :GD# in one poll) -> cached, no step.
    cfg["_z"] = (12.0, 6.0)
    assert comms_proc._imu_predict_smoothed(cfg) == (10.0, 5.0)

    # New sample -> output moves toward the measurement but is smoothed.
    cfg["imu_t"] = 1.05
    out = comms_proc._imu_predict_smoothed(cfg)
    assert 10.0 < out[0] < 12.0
    assert 5.0 < out[1] < 6.0

    # A fresh plate solve (imu_ref_t changes) re-anchors: snap to truth.
    cfg["imu_t"] = 1.10
    cfg["imu_ref_t"] = 200.0
    cfg["_z"] = (50.0, 25.0)
    assert comms_proc._imu_predict_smoothed(cfg) == (50.0, 25.0)

    # Raw prediction unavailable -> None and state cleared (re-snap next time).
    monkeypatch.setattr(comms_proc, "_imu_predict", lambda c: None)
    assert comms_proc._imu_predict_smoothed(cfg) is None
    assert comms_proc._imu_filt_state == {}


def _q_rot_x(deg):
    """Quaternion (w,x,y,z) for a rotation of `deg` about the x axis."""
    h = math.radians(deg) / 2.0
    return (math.cos(h), math.sin(h), 0.0, 0.0)


def _cfg_rotated(deg, imu_t=None, ref_t=None, gate_dps=None):
    """A shared_cfg dict that passes every _imu_predict precondition, with the
    live quaternion rotated `deg` from the reference captured at the last solve.
    Identity calibration (C maps rotvec component 0 -> RA, 1 -> Dec).
    imu_t / ref_t default to now; pass explicit monotonic times to drive the
    rotation-rate gate across successive calls."""
    now = time.monotonic()
    c = {
        "imu_available": True,
        "imu_calib_n": 3,
        "imu_calib_quality": 0.95,
        "imu_q": _q_rot_x(deg),
        "imu_t": now if imu_t is None else imu_t,
        "imu_ref_q": (1.0, 0.0, 0.0, 0.0),
        "imu_ref_ra_deg": 100.0,
        "imu_ref_dec_deg": 20.0,
        "imu_ref_roll_deg": 0.0,
        "imu_ref_t": now if ref_t is None else ref_t,
        "imu_calib_C": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0],
    }
    if gate_dps is not None:
        c["imu_rate_gate_dps"] = gate_dps
    return c


def _reset_rate_state():
    comms_proc._imu_rate_state.clear()


def test_imu_predict_gates_out_stationary_drift():
    # A parked scope accumulating slow gyro drift (0.05 deg over 1.6 s, i.e.
    # ~0.03 deg/s) stays below the 0.1 deg/s rate gate -> report the solved
    # position (None) on every poll.
    _reset_rate_state()
    t0 = time.monotonic() - 1.6
    ref = time.monotonic() - 1.7
    for i, (dt, deg) in enumerate([(0.0, 0.0), (0.8, 0.025), (1.6, 0.05)]):
        out = comms_proc._imu_predict(_cfg_rotated(deg, imu_t=t0 + dt, ref_t=ref))
        assert out is None, f"sample {i} engaged on drift"


def test_imu_predict_engages_on_slow_slew():
    # A slow pan (0.5 deg/s -- the case the old displacement gate froze) must
    # engage once a baseline of samples exists; RA advances by delta/cos(dec).
    _reset_rate_state()
    t0 = time.monotonic() - 1.6
    ref = time.monotonic() - 1.7
    assert comms_proc._imu_predict(_cfg_rotated(0.0, imu_t=t0, ref_t=ref)) is None
    out = None
    for dt, deg in [(0.8, 0.4), (1.6, 0.8)]:
        out = comms_proc._imu_predict(_cfg_rotated(deg, imu_t=t0 + dt, ref_t=ref))
    assert out is not None
    ra, dec = out
    assert ra == pytest.approx(100.0 + 0.8 / math.cos(math.radians(20.0)), abs=0.1)
    assert dec == pytest.approx(20.0, abs=0.05)


def test_imu_predict_holds_until_solve_reanchors():
    # Motion stops: the prediction must HOLD (not snap back by the whole slew)
    # until a fresh solve re-anchors the reference; then it disengages.
    # Timeline compressed to fit the 2 s IMU-freshness window.
    now = time.monotonic()
    t0 = now - 1.9
    ref = t0 - 0.05
    _reset_rate_state()
    comms_proc._imu_predict(_cfg_rotated(0.0, imu_t=t0, ref_t=ref))
    comms_proc._imu_predict(_cfg_rotated(0.4, imu_t=t0 + 0.4, ref_t=ref))
    assert comms_proc._imu_predict(
        _cfg_rotated(0.8, imu_t=t0 + 0.8, ref_t=ref)) is not None  # moving
    # Stopped (same quat) with rate measured as 0, no new solve yet -> HOLD.
    assert comms_proc._imu_predict(
        _cfg_rotated(0.8, imu_t=t0 + 1.6, ref_t=ref)) is not None
    # A solve lands after motion ended -> disengage to the (now equal) fix.
    assert comms_proc._imu_predict(
        _cfg_rotated(0.8, imu_t=t0 + 1.9, ref_t=now - 0.05)) is None


def test_imu_predict_sparse_polls_disengage_on_solve():
    # Regression: at LX200 poll intervals > 4x the rate baseline the sample
    # deque prunes to a single entry, the rate is unmeasurable (None), and the
    # old disengage branch (which required a rate) latched "engaged" forever —
    # a parked scope then walked with gyro drift instead of reporting the
    # solved position. A solve landing after the last observed motion must
    # disengage even with no measurable rate.
    _reset_rate_state()
    now = time.monotonic()
    t0 = now - 1.9
    ref_old = t0 - 0.05
    # Fast polls during a slew: engage.
    comms_proc._imu_predict(_cfg_rotated(0.0, imu_t=t0, ref_t=ref_old))
    comms_proc._imu_predict(_cfg_rotated(0.4, imu_t=t0 + 0.4, ref_t=ref_old))
    assert comms_proc._imu_predict(
        _cfg_rotated(0.8, imu_t=t0 + 0.8, ref_t=ref_old)) is not None
    # Simulate the poll gap: prune the deque to one stale-free sample so the
    # next call cannot measure a rate.
    comms_proc._imu_rate_state["samples"].clear()
    # A solve has since landed (fresh ref_t) -> must disengage (None = report
    # the solved position), not stay latched on the unmeasurable rate.
    assert comms_proc._imu_predict(
        _cfg_rotated(0.8, imu_t=t0 + 1.6, ref_t=now - 0.05)) is None


def test_imu_predict_rate_gate_is_tunable():
    # Same 0.5 deg/s pan, but a 10 deg/s gate treats it as stationary.
    _reset_rate_state()
    t0 = time.monotonic() - 1.6
    ref = time.monotonic() - 1.7
    for dt, deg in [(0.0, 0.0), (0.8, 0.4), (1.6, 0.8)]:
        out = comms_proc._imu_predict(
            _cfg_rotated(deg, imu_t=t0 + dt, ref_t=ref, gate_dps=10.0))
    assert out is None


def test_quat_to_radec_matches_rotation_matrix_row0():
    # Convention pin: boresight celestial vector is ROW 0 of R(q). Compare
    # against an independent numpy rotation-matrix construction.
    import numpy as np
    from diofinder.imu_math import quat_to_radec, rotvec_to_quat
    rnd = random.Random(7)
    for _ in range(20):
        r = tuple(rnd.uniform(-2.0, 2.0) for _ in range(3))
        w, x, y, z = rotvec_to_quat(r)
        R = np.array([
            [1 - 2*(y*y + z*z), 2*(x*y - z*w),     2*(x*z + y*w)],
            [2*(x*y + z*w),     1 - 2*(x*x + z*z), 2*(y*z - x*w)],
            [2*(x*z - y*w),     2*(y*z + x*w),     1 - 2*(x*x + y*y)],
        ])
        bx, by, bz = R[0]
        ra_exp = math.degrees(math.atan2(by, bx)) % 360.0
        dec_exp = math.degrees(math.asin(bz))
        ra, dec = quat_to_radec((w, x, y, z))
        assert ra == pytest.approx(ra_exp, abs=1e-9)
        assert dec == pytest.approx(dec_exp, abs=1e-9)


def test_imu_predict_frame_path_exact_large_slew():
    # v0.11.23 exact path: with the solver-published body->camera fit and the
    # solved sky quaternion in imu_ref, a 20-degree slew on a 90-degree
    # mounting predicts the true boresight exactly — no 5-degree clamp, no
    # C-matrix calibration keys needed at all.
    import numpy as np
    from diofinder.imu_math import (rotvec_to_quat, quat_to_rotvec, quat_mul,
                                    quat_to_radec)
    _reset_rate_state()
    ang = math.radians(90.0)
    R = np.array([[math.cos(ang), -math.sin(ang), 0.0],
                  [math.sin(ang),  math.cos(ang), 0.0],
                  [0.0, 0.0, 1.0]])                     # mounting about z
    q_sky_ref = rotvec_to_quat((0.2, -0.4, 0.3))        # arbitrary attitude
    ra_ref, dec_ref = quat_to_radec(q_sky_ref)
    d_cam = rotvec_to_quat((0.0, math.radians(20.0), 0.0))   # 20 deg slew
    truth = quat_to_radec(quat_mul(d_cam, q_sky_ref))
    r_imu = R.T @ np.asarray(quat_to_rotvec(d_cam))     # what the IMU sees

    now = time.monotonic()
    t0, ref_t = now - 1.6, now - 1.7

    def cfg(frac, imu_t):
        q_now = rotvec_to_quat(tuple(frac * v for v in r_imu))
        return {
            "imu_available": True,
            "imu_q": q_now,
            "imu_t": imu_t,
            "imu_ref": ((1.0, 0.0, 0.0, 0.0), ra_ref, dec_ref, 0.0, ref_t,
                        q_sky_ref),
            "imu_frame_R": [float(v) for v in R.reshape(-1)],
        }

    out = None
    for dt, frac in [(0.0, 0.0), (0.8, 0.5), (1.6, 1.0)]:
        out = comms_proc._imu_predict(cfg(frac, t0 + dt))
    assert out is not None
    assert out[0] == pytest.approx(truth[0], abs=1e-6)
    assert out[1] == pytest.approx(truth[1], abs=1e-6)

    # Sanity: the raw body-frame delta on this mounting lands far from truth,
    # so the frame correction is doing real work (not a no-op scenario).
    naive = quat_to_radec(quat_mul(rotvec_to_quat(tuple(r_imu)), q_sky_ref))
    err = math.hypot(wrap180(naive[0] - truth[0]), naive[1] - truth[1])
    assert err > 10.0


def test_imu_predict_accepts_5_and_6_tuple_refs():
    # Backward/forward compat of the atomic imu_ref tuple: a 5-tuple (pre
    # v0.11.23 solver) and a 6-tuple with sky_q=None both take the legacy
    # C-matrix path and agree with the split-key result.
    now = time.monotonic()
    t0, ref_t = now - 1.6, now - 1.7
    base = ((1.0, 0.0, 0.0, 0.0), 100.0, 20.0, 0.0, ref_t)
    for ref in (base, base + (None,)):
        _reset_rate_state()
        out = None
        for dt, deg in [(0.0, 0.0), (0.8, 0.4), (1.6, 0.8)]:
            c = _cfg_rotated(deg, imu_t=t0 + dt, ref_t=ref_t)
            c["imu_ref"] = ref
            out = comms_proc._imu_predict(c)
        assert out is not None
        assert out[0] == pytest.approx(
            100.0 + 0.8 / math.cos(math.radians(20.0)), abs=0.1)
        assert out[1] == pytest.approx(20.0, abs=0.05)


def test_get_imu_qt_composite_and_fallback():
    from diofinder.imu_math import get_imu_qt
    # Composite key preferred (v0.11.24 writer).
    q, t = get_imu_qt({"imu": ((1.0, 0.0, 0.0, 0.0), 42.0)})
    assert q == (1.0, 0.0, 0.0, 0.0) and t == 42.0
    # Legacy split keys still readable (older writer).
    q, t = get_imu_qt({"imu_q": (0.0, 1.0, 0.0, 0.0), "imu_t": 7.0})
    assert q == (0.0, 1.0, 0.0, 0.0) and t == 7.0
    assert get_imu_qt({}) == (None, 0.0)


def test_imu_predict_accepts_composite_imu_key():
    # The rate-gated prediction works when the cfg carries the composite
    # "imu" key instead of split imu_q/imu_t.
    _reset_rate_state()
    now = time.monotonic()
    t0, ref_t = now - 1.6, now - 1.7
    out = None
    for dt, deg in [(0.0, 0.0), (0.8, 0.4), (1.6, 0.8)]:
        c = _cfg_rotated(deg, imu_t=t0 + dt, ref_t=ref_t)
        c["imu"] = (c.pop("imu_q"), c.pop("imu_t"))
        out = comms_proc._imu_predict(c)
    assert out is not None
    assert out[0] == pytest.approx(
        100.0 + 0.8 / math.cos(math.radians(20.0)), abs=0.1)


def test_smoothed_wrapper_resnaps_after_stale_gap(monkeypatch):
    comms_proc._imu_filt_state.clear()
    cfg = {"imu_t": 1.0, "imu_ref_t": 100.0, "_z": (10.0, 5.0)}
    monkeypatch.setattr(comms_proc, "_imu_predict", lambda c: c["_z"])
    assert comms_proc._imu_predict_smoothed(cfg) == (10.0, 5.0)
    # A >1 s jump in IMU time means the dead-reckoning lapsed; snap, don't smooth.
    cfg["imu_t"] = 3.0
    cfg["_z"] = (40.0, 15.0)
    assert comms_proc._imu_predict_smoothed(cfg) == (40.0, 15.0)
