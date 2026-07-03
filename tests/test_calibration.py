"""FOV calibrator drift self-healing + the failure-driven fallback gate.

The v0.11.15 sensor-mode change shifted the true FOV 13.64 -> ~13.55 deg. The
old drift dead band — 3 x max(committed stddev, 0.05 convergence constant) =
0.15 deg — meant the calibrator stared at a full window of 13.55 measurements
(stddev 0.003) while keeping 13.64 committed, forever. These tests pin the
fixed behavior: the dead band scales with the MEASURED window stddev (floored
at fov_drift_stddev_floor) so a real miscentering recommits, while wobble
within the floor never does.
"""
import unittest
from unittest import mock

from diofinder import calibration
from diofinder.calibration import CalState, FovCalibrator, FallbackGate


class _FakeCfg:
    fov_deg = 13.64
    arcsec_per_pixel = 51.15
    fov_calibrated = True
    fov_calibrated_stddev = 0.05
    fov_calibrated_max_error_deg = 0.1
    fov_max_error_deg = 0.3
    distortion = 0.0
    frame_width = 960


def _mk(calibrated=True):
    cfg = _FakeCfg()
    cfg.fov_calibrated = calibrated
    shared = {}
    cal = FovCalibrator(cfg, shared)
    return cal, cfg, shared


def _feed(cal, fov, n, jitter=0.003):
    """Feed n solves at fov with a deterministic +/- jitter (stddev > 0)."""
    for i in range(n):
        cal.update_from_solve(fov + (jitter if i % 2 else -jitter), 0.0)


class DriftSelfHealTests(unittest.TestCase):
    def test_miscentered_commit_self_heals(self):
        # Committed 13.64, true measurements at 13.552 +/- 0.003: the old
        # 0.15 deg dead band ignored this forever; now the drift check must
        # fire at the first interval and the next window recommit ~13.55.
        cal, cfg, shared = _mk(calibrated=True)
        with mock.patch.object(calibration.cfg_mod, "save_keys") as sk:
            # Fill the window (30) + reach the drift-check interval (50);
            # the drift check fires (~9 sigma of the measured noise) and the
            # already-converged window recommits on the very next solve.
            _feed(cal, 13.552, cal.params.window_size
                  + cal.params.drift_check_interval + 1)
            self.assertEqual(cal.state, CalState.CALIBRATED)
            self.assertAlmostEqual(cal.committed_fov, 13.552, places=2)
            self.assertTrue(sk.called)
            self.assertAlmostEqual(
                sk.call_args_list[-1][0][0]["fov_deg"], 13.552, places=2)
        self.assertAlmostEqual(shared["fov_deg"], 13.552, places=2)

    def test_wobble_within_floor_does_not_recalibrate(self):
        # Measurements 0.012 deg off the committed value with near-zero
        # scatter: inside 3 x the 0.01 floor -> stay CALIBRATED (the floor is
        # what stops a hyper-tight window recommitting on every wobble).
        cal, cfg, shared = _mk(calibrated=True)
        with mock.patch.object(calibration.cfg_mod, "save_keys"):
            _feed(cal, 13.652, cal.params.window_size
                  + cal.params.drift_check_interval + 5, jitter=0.001)
            self.assertEqual(cal.state, CalState.CALIBRATED)
            self.assertAlmostEqual(cal.committed_fov, 13.64)

    def test_noisy_window_scales_dead_band_up(self):
        # Same 0.06 deg offset but a noisy window (stddev ~0.04): drift is
        # ~1.5 sigma of the measured noise -> no recalibration (offset is not
        # distinguishable from noise yet).
        cal, cfg, shared = _mk(calibrated=True)
        with mock.patch.object(calibration.cfg_mod, "save_keys"):
            _feed(cal, 13.70, cal.params.window_size
                  + cal.params.drift_check_interval + 5, jitter=0.04)
            self.assertEqual(cal.state, CalState.CALIBRATED)


class FallbackGateTests(unittest.TestCase):
    def test_no_retry_before_threshold(self):
        g = FallbackGate(fail_threshold=20, retry_every=10)
        for _ in range(19):
            self.assertFalse(g.note_failure())

    def test_retry_at_threshold_then_every_n(self):
        g = FallbackGate(fail_threshold=20, retry_every=10)
        fired = [i + 1 for i in range(45) if g.note_failure()]
        self.assertEqual(fired, [20, 30, 40])

    def test_success_resets(self):
        g = FallbackGate(fail_threshold=3, retry_every=10)
        g.note_failure(); g.note_failure()
        g.note_success()
        self.assertEqual(g.streak, 0)
        self.assertFalse(g.note_failure())
        self.assertFalse(g.note_failure())
        self.assertTrue(g.note_failure())      # 3rd after reset

    def test_zero_threshold_disables(self):
        g = FallbackGate(fail_threshold=0)
        for _ in range(100):
            self.assertFalse(g.note_failure())


if __name__ == "__main__":
    unittest.main()
