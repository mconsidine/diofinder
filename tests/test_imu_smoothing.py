"""Unit tests for the IMU alpha-beta pointing filter (LX200 jitter fix).

Covers the pure alpha-beta math in efinder.imu_math and the stateful
_imu_predict_smoothed wrapper in comms_proc (snap / cache / smooth / re-anchor).
No hardware required.
"""
import statistics
import random

import pytest

from efinder import comms_proc
from efinder.imu_math import alpha_beta_step, wrap180

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


def test_smoothed_wrapper_resnaps_after_stale_gap(monkeypatch):
    comms_proc._imu_filt_state.clear()
    cfg = {"imu_t": 1.0, "imu_ref_t": 100.0, "_z": (10.0, 5.0)}
    monkeypatch.setattr(comms_proc, "_imu_predict", lambda c: c["_z"])
    assert comms_proc._imu_predict_smoothed(cfg) == (10.0, 5.0)
    # A >1 s jump in IMU time means the dead-reckoning lapsed; snap, don't smooth.
    cfg["imu_t"] = 3.0
    cfg["_z"] = (40.0, 15.0)
    assert comms_proc._imu_predict_smoothed(cfg) == (40.0, 15.0)
