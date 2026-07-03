"""One-shot conf migrations + shipped-default divergence (conf_migrate.py)."""
import os
import tempfile
import unittest

from diofinder import conf_migrate


def _write(d, text, name="d.conf"):
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write(text)
    return p


class MigrateTests(unittest.TestCase):
    def test_old_defaults_migrate_and_stamp(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "detect_bg_mode: row_percentile\n"
                          "fov_max_error_deg: 1.0\n"
                          "camera_tuning_file: /usr/share/libcamera/ipa/rpi/vc4/imx477_scientific.json\n")
            applied = conf_migrate.migrate(p)
            self.assertEqual(applied["detect_bg_mode"], "block_percentile")
            self.assertEqual(applied["fov_max_error_deg"], "0.3")
            self.assertIn("imx477_finder", applied["camera_tuning_file"])
            # star_db_deep was absent -> added.
            self.assertIn("star_db_deep", applied)
            txt = open(p).read()
            self.assertIn("conf_version: 1", txt)
            self.assertIn("block_percentile", txt)
            # Second call: stamped, nothing to do.
            self.assertEqual(conf_migrate.migrate(p), {})

    def test_user_edits_are_never_overridden(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "detect_bg_mode: top_hat\n"       # user choice
                          "fov_max_error_deg: 0.5\n"        # user choice
                          "fov_deg: 12.9\n"                 # different lens!
                          "star_db_deep: /data/mydeep.npz\n")
            applied = conf_migrate.migrate(p)
            self.assertNotIn("detect_bg_mode", applied)
            self.assertNotIn("fov_max_error_deg", applied)
            self.assertNotIn("fov_deg", applied)
            self.assertNotIn("star_db_deep", applied)
            txt = open(p).read()
            self.assertIn("top_hat", txt)
            self.assertIn("12.9", txt)
            self.assertIn("/data/mydeep.npz", txt)

    def test_fov_recenter_pair(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "fov_deg: 13.64\narcsec_per_pixel: 51.15\n")
            applied = conf_migrate.migrate(p)
            self.assertEqual(applied["fov_deg"], "13.54")
            self.assertEqual(applied["arcsec_per_pixel"], "50.78")

    def test_already_stamped_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "conf_version: 1\ndetect_bg_mode: row_percentile\n")
            self.assertEqual(conf_migrate.migrate(p), {})
            self.assertIn("row_percentile", open(p).read())


class DiffTests(unittest.TestCase):
    def test_diff_reports_changes_and_absentees(self):
        with tempfile.TemporaryDirectory() as d:
            dflt = _write(d, "a: 1\nb: 2.0\nc: x\n", "default.conf")
            conf = _write(d, "a: 5\nb: 2.000000\nmy_extra: y\n", "live.conf")
            diffs = conf_migrate.diff_from_default(conf, dflt)
            keys = {k: (cur, dv) for k, cur, dv in diffs}
            self.assertEqual(keys["a"], ("5", "1"))
            self.assertNotIn("b", keys)              # numerically equal
            self.assertEqual(keys["c"][0], "<absent>")
            self.assertEqual(keys["my_extra"][1], "<not in default>")


if __name__ == "__main__":
    unittest.main()
