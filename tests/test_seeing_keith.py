"""Unit tests for the "Keith" seeing preset and the tetra3 extractor backend.

Pure-Python: no numpy / star_detect / tetra3 import required (seeing.py is
dependency-free), so this runs anywhere.
"""
import os
import tempfile
import unittest

import efinder.seeing as seeing
from efinder.config import Config


class TestKeithPreset(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def test_keith_is_a_valid_mode(self):
        self.assertIn("keith", seeing.VALID_MODES)
        self.assertTrue(seeing.is_valid_mode("keith"))

    def test_keith_selects_the_tetra3_backend(self):
        p = seeing.apply_preset("keith", self.cfg)
        self.assertEqual(p["extractor_backend"], "tetra3")

    def test_good_and_bad_reset_the_backend_to_sycamore(self):
        # Toggling away from Keith must restore the sycamore extractor, or the
        # tetra3 backend would "bleed" into the next preset.
        for mode in ("good", "bad"):
            self.assertEqual(
                seeing.apply_preset(mode, self.cfg)["extractor_backend"],
                "sycamore")

    def test_all_presets_share_one_key_set(self):
        # A preset toggle must fully re-tune the pipeline; that only holds if
        # every preset specifies the same keys (otherwise a key left out of one
        # preset keeps its prior value).
        keysets = [set(seeing.SEEING_PRESETS[m]) for m in seeing.VALID_MODES]
        for ks in keysets[1:]:
            self.assertEqual(ks, keysets[0])

    def test_keith_matches_astrokeith_baseline(self):
        p = seeing.apply_preset("keith", self.cfg)
        self.assertEqual(p["detect_sigma"], 2.0)
        self.assertEqual(p["detect_noise_mode"], "global_rms")
        self.assertEqual(p["detect_bg_mode"], "uniform_mean")
        self.assertEqual(p["min_centroids"], 15)
        self.assertEqual(p["solve_timeout_ms"], 5000)
        self.assertEqual(p["match_radius"], 0.01)
        self.assertEqual(p["match_threshold"], 1e-5)

    def test_no_drift_immediately_after_applying_keith(self):
        applied = seeing.apply_preset("keith", self.cfg)
        live = {k: v for k, v in applied.items() if k != "star_db"}
        drift = seeing.drift_from_preset("keith", self.cfg, live)
        self.assertEqual(drift, {})

    def test_override_roundtrip_on_keith(self):
        tmp = tempfile.mktemp(suffix=".json")
        try:
            seeing.save_override("keith", {"detect_sigma": 3.0}, path=tmp)
            self.assertIsNotNone(seeing.get_override("keith", tmp))
            _, used = seeing.merged_preset("keith", self.cfg,
                                           use_override=True, path=tmp)
            self.assertTrue(used)
            self.assertTrue(seeing.clear_override("keith", tmp))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


class TestExtractorBackendConfig(unittest.TestCase):
    def test_default_backend_is_sycamore(self):
        self.assertEqual(Config().extractor_backend, "sycamore")


if __name__ == "__main__":
    unittest.main()
