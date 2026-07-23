"""Unit tests for the BNO055 fusion-hunt suppression filter (P3).

The BNO055 in IMUPLUS mode can "hunt" between two nearby orientations while the
scope is physically stationary. Observed live (debug bundle
diofinder_debug_20260722212418) as the published quaternion toggling between two
fixed states ~0.8 deg apart while the plate solves stayed put — which drove the
LX200 pointing prediction and made SkySafari's reticle oscillate. `_hunt_filter`
attenuates that hunt while snapping cleanly through real motion.

No hardware required — the filter is pure on its inputs.
"""
import math

import pytest

from diofinder.imu_proc import (_hunt_filter, _nlerp, _quat_angle_deg,
                                _HUNT_SNAP_DEG)

# The two real BNO055 states from the debug bundle (stationary scope on Vega).
Q_A = (0.44572532078149707, -0.7530130282853349,
       0.45217276674258317, 0.17274289178758892)
Q_B = (0.439454025402522, -0.7556167825671142,
       0.453736281228104, 0.17334019890877256)


def _unit(q):
    n = math.sqrt(sum(c * c for c in q))
    return tuple(c / n for c in q)


def test_bundle_states_are_the_observed_hunt():
    # Sanity-pin the scenario: the two captured states are a sub-snap ~0.8 deg
    # apart, i.e. exactly the regime the filter must damp (not snap through).
    ang = _quat_angle_deg(Q_A, Q_B)
    assert 0.5 < ang < 1.0
    assert ang < _HUNT_SNAP_DEG


def test_first_sample_passes_through():
    st = {}
    assert _hunt_filter(st, Q_A) == Q_A
    assert st["q"] == Q_A


def test_real_motion_snaps_through_unfiltered():
    # A change larger than the snap threshold is a genuine slew: published
    # verbatim, no lag, so slews and the solve-hint Kabsch fit are unaffected.
    st = {"q": Q_A}
    h = math.radians(10.0) / 2.0            # 10 deg rotation about x
    q_far = _unit((Q_A[0] * math.cos(h), Q_A[1] + math.sin(h), Q_A[2], Q_A[3]))
    assert _quat_angle_deg(q_far, Q_A) > _HUNT_SNAP_DEG
    out = _hunt_filter(st, q_far)
    assert out == q_far
    assert st["q"] == q_far


def test_attenuates_two_state_hunt():
    # Feed the observed A<->B square wave; after warm-up the published output
    # oscillates with far smaller amplitude than the raw 0.8 deg hunt, and
    # never snaps (stays a smooth damped signal).
    st = {}
    outs = []
    for i in range(40):
        outs.append(_hunt_filter(st, Q_A if i % 2 == 0 else Q_B))
    warm = outs[20:]
    # Peak-to-peak swing between successive published orientations.
    p2p = max(_quat_angle_deg(a, b) for a, b in zip(warm, warm[1:]))
    raw = _quat_angle_deg(Q_A, Q_B)
    assert p2p < raw / 2.0                  # materially attenuated
    assert p2p > 0.0                        # not frozen solid
    # Every output is a unit quaternion bounded inside the A..B arc (never snaps
    # out to a third state).
    for q in warm:
        assert math.isclose(sum(c * c for c in q), 1.0, abs_tol=1e-9)
        assert _quat_angle_deg(q, Q_A) < raw
        assert _quat_angle_deg(q, Q_B) < raw


def test_hunt_amplitude_shrinks_vs_raw():
    # Quantify: the damped swing is at least ~3x smaller than the raw hunt.
    st = {}
    outs = [_hunt_filter(st, Q_A if i % 2 == 0 else Q_B) for i in range(40)]
    warm = outs[20:]
    p2p = max(_quat_angle_deg(a, b) for a, b in zip(warm, warm[1:]))
    assert p2p < _quat_angle_deg(Q_A, Q_B) / 3.0


def test_nlerp_endpoints_and_unit():
    assert _nlerp(Q_A, Q_B, 0.0) == pytest.approx(Q_A)
    assert _nlerp(Q_A, Q_B, 1.0) == pytest.approx(_unit(Q_B))
    mid = _nlerp(Q_A, Q_B, 0.5)
    assert math.isclose(sum(c * c for c in mid), 1.0, abs_tol=1e-12)
    # Midpoint is roughly equidistant from both endpoints.
    assert _quat_angle_deg(mid, Q_A) == pytest.approx(
        _quat_angle_deg(mid, Q_B), abs=0.02)


def test_nlerp_takes_short_arc_on_sign_flip():
    # A quaternion and its negation are the same orientation; nlerp must not
    # blend toward the antipode and collapse to ~zero norm.
    negB = tuple(-c for c in Q_B)
    out = _nlerp(Q_A, negB, 0.5)
    assert math.isclose(sum(c * c for c in out), 1.0, abs_tol=1e-9)
    assert _quat_angle_deg(out, Q_B) < _quat_angle_deg(Q_A, Q_B)


def test_disabled_semantics_via_clear():
    # When the caller disables the filter it clears the state; the next enabled
    # sample must then pass through (snap), not blend against a stale anchor.
    st = {"q": Q_A}
    st.clear()
    assert _hunt_filter(st, Q_B) == Q_B
