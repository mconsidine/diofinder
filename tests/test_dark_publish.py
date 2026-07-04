"""Dark-frame heartbeat throttle (v0.11.28): a time floor bounds the watchdog
epoch gap independent of exposure, replacing the every-Nth-frame throttle that
could cross the 30 s watchdog at long exposure + a dark scene.
"""
from diofinder.solver_proc import _dark_publish_due, _DARK_PUBLISH_FLOOR_S


def test_first_dark_frame_always_publishes():
    assert _dark_publish_due(0, now=100.0, last_publish_t=0.0) is True


def test_throttles_within_floor():
    # Just published at t=100; a frame 1 s later (< floor) is skipped.
    assert _dark_publish_due(3, now=101.0, last_publish_t=100.0) is False


def test_publishes_once_floor_elapsed():
    assert _dark_publish_due(3, now=100.0 + _DARK_PUBLISH_FLOOR_S,
                             last_publish_t=100.0) is True


def test_watchdog_safe_at_long_exposure():
    # The property that fixes the bug: simulate a dark stretch at a long
    # exposure and confirm the gap between publishes never approaches the
    # 30 s watchdog. At exposure E, publishing happens every ceil(floor/E)*E.
    watchdog_s = 30.0
    for exposure in (0.05, 0.2, 1.0, 3.0, 7.0, 10.0):
        last = 0.0
        max_gap = 0.0
        t = 0.0
        for streak in range(200):
            if _dark_publish_due(streak, t, last):
                max_gap = max(max_gap, t - last)
                last = t
            t += exposure
        # After the first publish, the gap is bounded by floor + one exposure.
        assert max_gap <= _DARK_PUBLISH_FLOOR_S + exposure + 1e-9
        assert max_gap < watchdog_s, f"exposure {exposure}: gap {max_gap}"
