#!/usr/bin/env python3
"""
Unit tests for the web-UI-driven clock sync (comms_proc._sync_clock_epoch /
_time_sync_response). SkyPortal cannot provide time over the Celestron AUX
protocol and the Pi has no RTC, so the web UI posts the browser's Date.now()
on every page load; these tests pin the drift threshold, range guards, and
the injected clock-setter path without touching the real system clock.

Runnable WITHOUT picamera2 or star_detect:
    python3 -m unittest tests.test_time_sync -v
"""
import datetime
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

from diofinder.comms_proc import (_sync_clock_epoch, _time_sync_response,
                                  _CLOCK_DRIFT_MIN_S)


class _Recorder:
    """Stand-in for _set_system_clock; records the UTC datetime it was asked
    to set instead of stepping the real clock."""

    def __init__(self, ok=True, detail="2026-07-18 04:00:00"):
        self.called_with = None
        self._ok = ok
        self._detail = detail

    def __call__(self, utc_dt):
        self.called_with = utc_dt
        return self._ok, self._detail


class TestSyncClockEpoch(unittest.TestCase):
    NOW = 1_752_811_200.0    # a fixed "server now" (2026-07-18T04:00:00Z)

    def test_small_drift_is_a_noop(self):
        rec = _Recorder()
        drift = _CLOCK_DRIFT_MIN_S - 1.0
        r = _sync_clock_epoch((self.NOW + drift) * 1000.0,
                              now_s=self.NOW, set_clock=rec)
        self.assertTrue(r["ok"])
        self.assertFalse(r["synced"])
        self.assertIsNone(rec.called_with)          # clock NOT stepped
        self.assertAlmostEqual(r["drift_s"], drift, places=3)

    def test_large_drift_steps_the_clock(self):
        rec = _Recorder()
        drift = 120.0
        r = _sync_clock_epoch((self.NOW + drift) * 1000.0,
                              now_s=self.NOW, set_clock=rec)
        self.assertTrue(r["ok"])
        self.assertTrue(r["synced"])
        self.assertIsNotNone(rec.called_with)
        # It stepped to the client's epoch, in UTC.
        self.assertEqual(rec.called_with.tzinfo, datetime.timezone.utc)
        want = datetime.datetime.fromtimestamp(self.NOW + drift,
                                               tz=datetime.timezone.utc)
        self.assertEqual(rec.called_with, want)

    def test_negative_drift_also_steps(self):
        rec = _Recorder()
        r = _sync_clock_epoch((self.NOW - 300.0) * 1000.0,
                              now_s=self.NOW, set_clock=rec)
        self.assertTrue(r["synced"])
        self.assertAlmostEqual(r["drift_s"], -300.0, places=3)

    def test_setter_failure_propagates(self):
        rec = _Recorder(ok=False, detail="rc=1: nope")
        r = _sync_clock_epoch((self.NOW + 999.0) * 1000.0,
                              now_s=self.NOW, set_clock=rec)
        self.assertFalse(r["ok"])
        self.assertFalse(r["synced"])
        self.assertIn("nope", r["error"])

    def test_bad_and_out_of_range_inputs(self):
        for bad in (None, "abc", float("nan")):
            r = _sync_clock_epoch(bad, now_s=self.NOW, set_clock=_Recorder())
            self.assertFalse(r["ok"], bad)
        # 1969 and year-2100 are rejected by the range guard.
        for bad_epoch_ms in (-1000.0, 5_000_000_000_000.0):
            r = _sync_clock_epoch(bad_epoch_ms, now_s=self.NOW,
                                  set_clock=_Recorder())
            self.assertFalse(r["ok"], bad_epoch_ms)


class TestTimeSyncResponse(unittest.TestCase):
    def test_missing_epoch_is_an_error(self):
        r = _time_sync_response({})
        self.assertFalse(r["ok"])
        self.assertIn("error", r)

    def test_wraps_success_into_result(self):
        # A far-future epoch would try to step the real clock, so exercise
        # only the small-drift no-op path (uses real time.time()) — it never
        # calls the setter.
        import time as _t
        r = _time_sync_response({"epoch_ms": _t.time() * 1000.0})
        self.assertTrue(r["ok"])
        self.assertIn("result", r)
        self.assertFalse(r["result"]["synced"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
