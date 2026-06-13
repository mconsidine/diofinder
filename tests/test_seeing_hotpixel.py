#!/usr/bin/env python3
"""
Pure-logic unit tests for the seeing presets and hot-pixel neighbor-median
repair. Runnable WITHOUT sycamore (star_detect) or picamera2 — those modules
are stubbed in sys.modules before importing anything that touches them.

Run:
    python3 -m unittest tests.test_seeing_hotpixel -v
or directly:
    python3 tests/test_seeing_hotpixel.py
"""
import os
import sys
import types
import unittest

# Make the repo root importable when run directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Stub star_detect so importing efinder modules that reference it at import
# time (e.g. bg_cache) never fails in a sycamore-less environment. seeing.py
# and hot_pixel.py don't import it, but we stub defensively.
if "star_detect" not in sys.modules:
    _stub = types.ModuleType("star_detect")
    _stub.detect_stars = lambda *a, **k: []
    _stub.set_num_threads = lambda n: None
    sys.modules["star_detect"] = _stub

import numpy as np

from efinder import seeing
from efinder import hot_pixel


class _FakeCfg:
    """Minimal stand-in for the Config dataclass for preset tests."""
    def __init__(self, **kw):
        self.solver_db = "default_database"
        self.star_db_deep = ""
        self.detect_sigma = 5.0
        self.detect_kernel_sigma = 1.5
        self.detect_bg_mode = "row_percentile"
        self.detect_max_axis_ratio = 0.0
        self.min_centroids = 8
        self.match_radius = 0.01
        self.match_threshold = 1e-5
        self.solve_timeout_ms = 1500
        self.auto_exposure_target_stars = 20
        self.auto_exposure_max_s = 1.0
        for k, v in kw.items():
            setattr(self, k, v)


class SeeingPresetTests(unittest.TestCase):
    def test_modes_valid(self):
        self.assertTrue(seeing.is_valid_mode("good"))
        self.assertTrue(seeing.is_valid_mode("bad"))
        self.assertFalse(seeing.is_valid_mode("ugly"))

    def test_apply_good_preset_values(self):
        cfg = _FakeCfg()
        p = seeing.apply_preset("good", cfg)
        self.assertEqual(p["detect_sigma"], 5.0)
        self.assertEqual(p["detect_kernel_sigma"], 1.5)
        self.assertEqual(p["detect_bg_mode"], "row_percentile")
        self.assertEqual(p["detect_max_axis_ratio"], 3.0)
        self.assertEqual(p["min_centroids"], 8)
        self.assertEqual(p["solve_timeout_ms"], 1500)
        # star_db resolves to the standard db (no deep configured).
        self.assertEqual(p["star_db"], "default_database")

    def test_apply_bad_preset_values(self):
        cfg = _FakeCfg()
        p = seeing.apply_preset("bad", cfg)
        self.assertEqual(p["detect_sigma"], 4.0)
        self.assertEqual(p["detect_kernel_sigma"], 2.5)
        self.assertEqual(p["detect_bg_mode"], "block_percentile")
        self.assertEqual(p["detect_max_axis_ratio"], 5.0)
        self.assertEqual(p["min_centroids"], 5)
        self.assertEqual(p["solve_timeout_ms"], 3000)
        self.assertEqual(p["auto_exposure_target_stars"], 15)
        self.assertEqual(p["auto_exposure_max_s"], 1.0)

    def test_apply_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            seeing.apply_preset("nope", _FakeCfg())

    def test_deep_db_falls_back_when_missing(self):
        # Points at a nonexistent absolute path -> falls back to standard.
        cfg = _FakeCfg(star_db_deep="/nonexistent/deep_db.npz")
        p = seeing.apply_preset("bad", cfg)
        self.assertEqual(p["star_db"], "default_database")

    def test_deep_db_used_when_present(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".npz", delete=False) as f:
            deep_path = f.name
        try:
            cfg = _FakeCfg(star_db_deep=deep_path)
            p = seeing.apply_preset("bad", cfg)
            self.assertEqual(p["star_db"], deep_path)
        finally:
            os.remove(deep_path)

    def test_drift_detection(self):
        # Start from a cfg whose values exactly match the good preset so that,
        # with no overrides, there is zero drift.
        good = seeing.SEEING_PRESETS["good"]
        cfg = _FakeCfg(
            detect_sigma=good["detect_sigma"],
            detect_kernel_sigma=good["detect_kernel_sigma"],
            detect_bg_mode=good["detect_bg_mode"],
            detect_max_axis_ratio=good["detect_max_axis_ratio"],
            min_centroids=good["min_centroids"],
            match_radius=good["match_radius"],
            match_threshold=good["match_threshold"],
            solve_timeout_ms=good["solve_timeout_ms"],
            auto_exposure_target_stars=good["auto_exposure_target_stars"],
            auto_exposure_max_s=good["auto_exposure_max_s"],
        )
        self.assertEqual(seeing.drift_from_preset("good", cfg, {}), {})
        # Override detect_sigma in shared_cfg -> drift reported.
        shared = {"detect_sigma": 9.0}
        drift = seeing.drift_from_preset("good", cfg, shared)
        self.assertIn("detect_sigma", drift)
        self.assertEqual(drift["detect_sigma"]["preset"], 5.0)
        self.assertEqual(drift["detect_sigma"]["effective"], 9.0)

    def test_effective_prefers_shared_cfg(self):
        cfg = _FakeCfg()
        shared = {"detect_kernel_sigma": 3.3}
        eff = seeing.effective_values(cfg, shared)
        self.assertEqual(eff["detect_kernel_sigma"], 3.3)
        # A key absent from shared falls back to cfg.
        self.assertEqual(eff["detect_sigma"], 5.0)


class HotPixelTests(unittest.TestCase):
    def test_compute_indices_flags_hot_pixels(self):
        stack = np.full((10, 10), 20, dtype=np.uint8)
        stack[3, 4] = 200   # clearly hot
        stack[7, 8] = 255   # clearly hot
        idx = hot_pixel.compute_hot_pixel_indices(stack, k=5.0)
        flagged = set(idx.tolist())
        self.assertIn(3 * 10 + 4, flagged)
        self.assertIn(7 * 10 + 8, flagged)
        # A uniform-background pixel is not flagged.
        self.assertNotIn(0, flagged)

    def test_neighbor_median_repair_interior(self):
        frame = np.full((5, 5), 10, dtype=np.uint8)
        frame[2, 2] = 250   # hot pixel surrounded by 10s
        indices = np.array([2 * 5 + 2], dtype=np.int64)
        neigh = hot_pixel.build_neighbor_index(indices, frame.shape)
        hot_pixel.repair_frame(frame, indices, neigh)
        # All 8 neighbors are 10 -> repaired value is 10.
        self.assertEqual(int(frame[2, 2]), 10)

    def test_neighbor_median_repair_edge(self):
        # Corner pixel: only 3 in-bounds neighbors.
        frame = np.full((4, 4), 30, dtype=np.uint8)
        frame[0, 0] = 255
        indices = np.array([0], dtype=np.int64)
        neigh = hot_pixel.build_neighbor_index(indices, frame.shape)
        # The 3 in-bounds neighbors (0,1),(1,0),(1,1) are all 30.
        hot_pixel.repair_frame(frame, indices, neigh)
        self.assertEqual(int(frame[0, 0]), 30)

    def test_repair_does_not_cross_contaminate(self):
        # Two adjacent hot pixels; neighbors read before writes, so each is
        # repaired from the ORIGINAL surrounding values, not each other.
        frame = np.full((5, 5), 40, dtype=np.uint8)
        frame[2, 2] = 250
        frame[2, 3] = 250
        indices = np.array([2 * 5 + 2, 2 * 5 + 3], dtype=np.int64)
        neigh = hot_pixel.build_neighbor_index(indices, frame.shape)
        hot_pixel.repair_frame(frame, indices, neigh)
        # Each repaired pixel still sees one hot neighbor in its window (the
        # other masked pixel) but the bulk of neighbors are 40, so the mean is
        # pulled toward 40, well below the original 250.
        self.assertLess(int(frame[2, 2]), 150)
        self.assertLess(int(frame[2, 3]), 150)

    def test_repair_noop_on_empty_mask(self):
        frame = np.full((4, 4), 50, dtype=np.uint8)
        before = frame.copy()
        m = hot_pixel.HotPixelMask(indices=np.zeros(0, dtype=np.int64),
                                   shape=(4, 4))
        m.repair(frame)
        self.assertTrue(np.array_equal(frame, before))

    def test_mask_save_load_roundtrip(self):
        import tempfile
        stack = np.full((6, 6), 15, dtype=np.uint8)
        stack[1, 1] = 200
        idx = hot_pixel.compute_hot_pixel_indices(stack)
        m = hot_pixel.HotPixelMask(indices=idx, shape=(6, 6))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "mask.npz")
            m.save(path)
            loaded = hot_pixel.HotPixelMask.load(path)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.shape, (6, 6))
            self.assertEqual(loaded.count, m.count)
            self.assertTrue(np.array_equal(sorted(loaded.indices), sorted(idx)))

    def test_repair_shape_mismatch_is_noop(self):
        m = hot_pixel.HotPixelMask(indices=np.array([0], dtype=np.int64),
                                   shape=(5, 5))
        frame = np.full((4, 4), 60, dtype=np.uint8)
        before = frame.copy()
        m.repair(frame)  # shape mismatch -> no-op
        self.assertTrue(np.array_equal(frame, before))

    def test_capture_dark_mask(self):
        frames = [np.full((8, 8), 12, dtype=np.uint8) for _ in range(4)]
        for f in frames:
            f[5, 5] = 240
        it = iter(frames)

        def reader():
            return next(it, None)

        mask = hot_pixel.capture_dark_mask(reader, 4, (8, 8), interval_s=0.0)
        self.assertIn(5 * 8 + 5, set(mask.indices.tolist()))


class MaintSeeingDispatchTests(unittest.TestCase):
    """Exercise the comms_proc maint dispatch for the new commands with a fake
    IPC context (no real solver/camera processes)."""

    def setUp(self):
        # tetra3 isn't needed by comms_proc import, but stub defensively in
        # case a transitive import reaches for it.
        if "tetra3" not in sys.modules:
            t3 = types.ModuleType("tetra3")
            t3.Tetra3 = object
            sys.modules["tetra3"] = t3
        from efinder.maint import MaintRequest
        from efinder.config import Config
        import efinder.comms_proc as comms
        self.comms = comms
        self.MaintRequest = MaintRequest

        class _FakeReply:
            def __init__(self, ok=True, result=None, error=""):
                self.ok = ok
                self.result = result
                self.error = error

        # Fake _call_solver so seeing_set's set_db succeeds without a process.
        self._orig_call_solver = comms._call_solver
        comms._call_solver = lambda op, args, q, rq, timeout_s=5.0: \
            _FakeReply(ok=True, result={"db": args.get("db")})

        class _Ctx:
            def __init__(self, cfg):
                self.cfg = cfg
                self.shared_cfg = {}
                self.latest_solution = {}
                self.solver_cmd_q = None
                self.solver_cmd_reply_q = None
                self.camera_cmd_q = None
                self.camera_cmd_reply_q = None

        cfg = Config()
        # Avoid writing the real config file during the test.
        self._orig_save = comms.cfg_mod.save_keys
        comms.cfg_mod.save_keys = lambda updates, path=None: None
        self.ctx = _Ctx(cfg)

    def tearDown(self):
        self.comms._call_solver = self._orig_call_solver
        self.comms.cfg_mod.save_keys = self._orig_save

    def _call(self, cmd, args=None):
        req = self.MaintRequest(cmd=cmd, args=args or {})
        return self.comms._handle_maint_command(req, self.ctx)

    def test_seeing_set_writes_shared_cfg(self):
        r = self._call("seeing_set", {"mode": "bad"})
        self.assertTrue(r.ok, r.error)
        # Bad preset's live keys land in shared_cfg.
        self.assertEqual(self.ctx.shared_cfg["detect_bg_mode"], "block_percentile")
        self.assertEqual(self.ctx.shared_cfg["detect_kernel_sigma"], 2.5)
        self.assertEqual(self.ctx.shared_cfg["solve_timeout_ms"], 3000)
        self.assertEqual(self.ctx.shared_cfg["seeing_mode"], "bad")

    def test_seeing_set_rejects_bad_mode(self):
        r = self._call("seeing_set", {"mode": "terrible"})
        self.assertFalse(r.ok)

    def test_seeing_get_reports_mode_and_presets(self):
        self._call("seeing_set", {"mode": "good"})
        r = self._call("seeing_get")
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.result["mode"], "good")
        self.assertIn("good", r.result["presets"])
        self.assertIn("bad", r.result["presets"])

    def test_solver_params_set_new_keys(self):
        r = self._call("solver_params_set",
                       {"detect_kernel_sigma": 2.0, "detect_max_axis_ratio": 4.0,
                        "detect_local_noise": False, "min_centroids": 6})
        self.assertTrue(r.ok, r.error)
        self.assertEqual(self.ctx.shared_cfg["detect_kernel_sigma"], 2.0)
        self.assertEqual(self.ctx.shared_cfg["detect_max_axis_ratio"], 4.0)
        self.assertEqual(self.ctx.shared_cfg["detect_local_noise"], False)
        self.assertEqual(self.ctx.shared_cfg["min_centroids"], 6)

    def test_solver_params_set_rejects_out_of_range(self):
        r = self._call("solver_params_set", {"detect_kernel_sigma": 9.0})
        self.assertFalse(r.ok)
        r = self._call("solver_params_set", {"detect_max_axis_ratio": 0.5})
        self.assertFalse(r.ok)

    def test_solver_params_set_tracking_keys(self):
        r = self._call("solver_params_set",
                       {"tracking_enabled": True, "tracking_window_px": 64,
                        "tracking_min_recover": 7})
        self.assertTrue(r.ok, r.error)
        self.assertEqual(self.ctx.shared_cfg["tracking_enabled"], True)
        self.assertEqual(self.ctx.shared_cfg["tracking_window_px"], 64)
        self.assertEqual(self.ctx.shared_cfg["tracking_min_recover"], 7)

    def test_solver_params_set_rejects_bad_tracking(self):
        r = self._call("solver_params_set", {"tracking_window_px": 4})
        self.assertFalse(r.ok)
        r = self._call("solver_params_set", {"tracking_min_recover": 1})
        self.assertFalse(r.ok)

    def test_solver_params_get_includes_tracking(self):
        r = self._call("solver_params_get")
        self.assertTrue(r.ok, r.error)
        self.assertIn("tracking_enabled", r.result)
        self.assertIn("tracking_window_px", r.result)
        self.assertIn("tracking_min_recover", r.result)
        # Default off.
        self.assertFalse(r.result["tracking_enabled"])

    def test_match_params_set(self):
        r = self._call("match_params_set",
                       {"match_radius": 0.02, "match_threshold": 1e-6})
        self.assertTrue(r.ok, r.error)
        self.assertEqual(self.ctx.shared_cfg["match_radius"], 0.02)
        self.assertEqual(self.ctx.shared_cfg["match_threshold"], 1e-6)
        r = self._call("match_params_set", {"match_radius": 0.5})
        self.assertFalse(r.ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
