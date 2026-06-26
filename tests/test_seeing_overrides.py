#!/usr/bin/env python3
"""
Pure-logic unit tests for seeing override storage, merge, and lineage
classification (factory / tuned / custom). diofinder.seeing is dependency-free,
so these run without numpy / star_detect / picamera2.

Run:
    python3 -m unittest tests.test_seeing_overrides -v
or directly:
    python3 tests/test_seeing_overrides.py
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diofinder import seeing


class _FakeCfg:
    """Minimal Config stand-in matching the good preset by default."""
    def __init__(self, **kw):
        self.solver_db = "default_database"
        self.star_db_deep = ""
        good = seeing.SEEING_PRESETS["good"]
        for k, v in good.items():
            if k != "star_db":
                setattr(self, k, v)
        self.exposure_s = 0.2
        self.gain = 5.0
        for k, v in kw.items():
            setattr(self, k, v)


def _effective(cfg, shared=None):
    eff = seeing.effective_values(cfg, shared or {})
    eff["exposure_s"] = getattr(cfg, "exposure_s", None)
    eff["gain"] = getattr(cfg, "gain", None)
    return eff


class OverrideStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "ovr.json")

    def test_missing_file_is_empty(self):
        self.assertEqual(seeing.load_overrides(self.path), {})
        self.assertIsNone(seeing.get_override("good", self.path))

    def test_save_keeps_only_eligible_keys(self):
        entry = seeing.save_override(
            "good",
            {"detect_sigma": 6.0, "exposure_s": 0.3, "gain": 4.0,
             "bogus_key": 99},
            source="auto_tune", path=self.path, now=123.0)
        self.assertNotIn("bogus_key", entry["values"])
        self.assertEqual(entry["values"]["detect_sigma"], 6.0)
        self.assertEqual(entry["source"], "auto_tune")
        self.assertEqual(entry["saved_at"], 123.0)
        # Round-trips from disk.
        self.assertEqual(seeing.get_override("good", self.path)["values"],
                         entry["values"])

    def test_save_empty_raises(self):
        with self.assertRaises(ValueError):
            seeing.save_override("good", {"bogus": 1}, path=self.path)

    def test_save_bad_mode_raises(self):
        with self.assertRaises(ValueError):
            seeing.save_override("ugly", {"detect_sigma": 5.0}, path=self.path)

    def test_clear(self):
        seeing.save_override("good", {"detect_sigma": 6.0}, path=self.path)
        self.assertTrue(seeing.clear_override("good", self.path))
        self.assertIsNone(seeing.get_override("good", self.path))
        self.assertFalse(seeing.clear_override("good", self.path))

    def test_two_modes_independent(self):
        seeing.save_override("good", {"detect_sigma": 6.0}, path=self.path)
        seeing.save_override("bad", {"detect_sigma": 3.0}, path=self.path)
        self.assertEqual(seeing.get_override("good", self.path)["values"]["detect_sigma"], 6.0)
        self.assertEqual(seeing.get_override("bad", self.path)["values"]["detect_sigma"], 3.0)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "ovr.json")
        self.cfg = _FakeCfg()

    def test_use_override_false_returns_factory(self):
        seeing.save_override("good", {"detect_sigma": 6.0}, path=self.path)
        merged, applied = seeing.merged_preset(
            "good", self.cfg, use_override=False, path=self.path)
        self.assertFalse(applied)
        self.assertEqual(merged["detect_sigma"], 5.0)  # factory, not override

    def test_no_override_returns_factory(self):
        merged, applied = seeing.merged_preset(
            "good", self.cfg, use_override=True, path=self.path)
        self.assertFalse(applied)
        self.assertEqual(merged["detect_sigma"], 5.0)

    def test_override_overlays_factory(self):
        seeing.save_override(
            "good", {"detect_sigma": 6.0, "exposure_s": 0.35},
            path=self.path)
        merged, applied = seeing.merged_preset(
            "good", self.cfg, use_override=True, path=self.path)
        self.assertTrue(applied)
        self.assertEqual(merged["detect_sigma"], 6.0)        # from override
        self.assertEqual(merged["exposure_s"], 0.35)         # override-only key
        self.assertEqual(merged["min_centroids"], 8)         # untouched factory


class LineageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "ovr.json")

    def test_factory_when_matches_preset(self):
        cfg = _FakeCfg()
        out = seeing.classify_lineage("good", cfg, _effective(cfg), self.path)
        self.assertEqual(out["source"], "factory")

    def test_custom_when_drifted(self):
        cfg = _FakeCfg()
        out = seeing.classify_lineage(
            "good", cfg, _effective(cfg, {"detect_sigma": 9.0}), self.path)
        self.assertEqual(out["source"], "custom")
        self.assertIn("detect_sigma", out["drift"])

    def test_tuned_when_override_matches_effective(self):
        cfg = _FakeCfg()
        # Override sets sigma=6 and exposure=0.3; reflect that in effective.
        seeing.save_override(
            "good", {"detect_sigma": 6.0, "exposure_s": 0.3},
            source="auto_tune", path=self.path)
        cfg.exposure_s = 0.3
        out = seeing.classify_lineage(
            "good", cfg, _effective(cfg, {"detect_sigma": 6.0}), self.path)
        self.assertEqual(out["source"], "tuned")
        self.assertEqual(out["override"]["source"], "auto_tune")

    def test_custom_when_override_exists_but_not_matched(self):
        cfg = _FakeCfg()
        seeing.save_override("good", {"detect_sigma": 6.0}, path=self.path)
        # effective sigma is factory 5.0, not the override's 6.0 -> not tuned,
        # and factory matches -> factory (override present but not applied).
        out = seeing.classify_lineage("good", cfg, _effective(cfg), self.path)
        self.assertEqual(out["source"], "factory")
        self.assertIsNotNone(out["override"])  # metadata still surfaced


if __name__ == "__main__":
    unittest.main()
