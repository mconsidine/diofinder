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
            self.assertIn(f"conf_version: {conf_migrate.CONF_VERSION}", txt)
            self.assertIn("block_percentile", txt)
            # Second call: stamped, nothing to do.
            self.assertEqual(conf_migrate.migrate(p), {})

    def test_g10_persisted_old_default_still_matches(self):
        # save_keys writes floats as %.10g, so a user-persisted "1.0" lands
        # in the conf as "1" — the exact-string rules missed it and the
        # migration silently skipped (audit 2026-07 F-L3).
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "fov_max_error_deg: 1\n")
            fires = conf_migrate.pending_migrations(p)
            keys = [k for k, _c, _n, _r in fires]
            self.assertIn("fov_max_error_deg", keys)
            applied = conf_migrate.migrate(p)
            self.assertEqual(applied["fov_max_error_deg"], "0.3")

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
            p = _write(d, f"conf_version: {conf_migrate.CONF_VERSION}\n"
                          "detect_bg_mode: row_percentile\nimu_poll_hz: 20\n")
            self.assertEqual(conf_migrate.migrate(p), {})
            self.assertIn("row_percentile", open(p).read())

    def test_v1_device_gets_only_the_v2_rules(self):
        # Migrated to v1 earlier, then the user chose row_percentile on
        # purpose: the v2 run moves the IMU rate and leaves that choice alone.
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "conf_version: 1\ndetect_bg_mode: row_percentile\n"
                          "imu_poll_hz: 20\n")
            self.assertEqual(conf_migrate.migrate(p), {"imu_poll_hz": "10"})
            txt = open(p).read()
            self.assertIn("row_percentile", txt)
            self.assertIn("imu_poll_hz: 10", txt)
            self.assertIn("conf_version: 2", txt)

    def test_user_chosen_imu_rate_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "conf_version: 1\nimu_poll_hz: 15\n")
            self.assertEqual(conf_migrate.migrate(p), {})
            self.assertIn("imu_poll_hz: 15", open(p).read())


class DiffTests(unittest.TestCase):
    def test_diff_reports_changes_and_absentees(self):
        with tempfile.TemporaryDirectory() as d:
            dflt = _write(d, "a: 1\nb: 2.0\nc: x\n", "default.conf")
            conf = _write(d, "a: 5\nb: 2.000000\nmy_extra: y\n", "live.conf")
            diffs = conf_migrate.diff_from_default(conf, dflt)
            keys = {k: (cur, dv) for k, cur, dv in diffs}
            self.assertEqual(keys["a"], ("5", "1"))
            self.assertNotIn("b", keys)              # numerically equal
            self.assertEqual(keys["c"][0], "<not set>")   # not a Config field
            self.assertEqual(keys["my_extra"][1], "<not in default>")

    def test_a_missing_key_is_listed_only_when_its_builtin_differs(self):
        with tempfile.TemporaryDirectory() as d:
            # imu_poll_hz 10 and exposure_s 0.2 are the built-in defaults too;
            # gain 7 is not (built-in 5), so a device without it runs 5.
            dflt = _write(d, "imu_poll_hz: 10\nexposure_s: 0.200000\n"
                             "watchdog_enabled: true\ngain: 7\n", "default.conf")
            conf = _write(d, "latitude_deg: 1\n", "live.conf")
            keys = {k: (cur, dv) for k, cur, dv in
                    conf_migrate.diff_from_default(conf, dflt)}
            for k in ("imu_poll_hz", "exposure_s", "watchdog_enabled"):
                self.assertNotIn(k, keys)
            self.assertEqual(keys["gain"], ("<not set: using 5.0>", "7"))

    def test_shipped_default_and_builtin_defaults_agree(self):
        # Every key the shipped conf sets means the same as the built-in
        # default, except calibration bookkeeping a fresh image ships
        # pre-stamped. A conf that lacks a key then runs what a fresh image
        # runs (the camera tuning file and the deep db used to differ).
        import dataclasses
        from diofinder import config as cfg_mod
        here = os.path.dirname(os.path.abspath(__file__))
        dflt = conf_migrate._parse_conf(os.path.join(here, "..", "etc",
                                                     "diofinder.conf.default"))
        base = cfg_mod.Config()
        fields = {f.name for f in dataclasses.fields(cfg_mod.Config)}
        differ = sorted(k for k, v in dflt.items() if k in fields
                        and not conf_migrate._same_value(v, getattr(base, k)))
        self.assertEqual(differ, ["conf_version", "fov_calibrated"])


class FactoryResetTests(unittest.TestCase):
    def test_overwrites_live_conf_with_default_verbatim(self):
        with tempfile.TemporaryDirectory() as d:
            dflt = _write(d, "a: 1\nb: 2\n# a comment\n", "default.conf")
            live = _write(d, "a: 999\nc: user_added\n", "live.conf")
            used = conf_migrate.factory_reset(live, dflt)
            self.assertEqual(used, dflt)
            self.assertEqual(open(live).read(), open(dflt).read())

    def test_works_when_live_conf_is_missing(self):
        # A true factory reset should recreate the conf, not require one to
        # already exist (unlike save_keys, which is a merge-into-existing op).
        with tempfile.TemporaryDirectory() as d:
            dflt = _write(d, "a: 1\n", "default.conf")
            live = os.path.join(d, "live.conf")
            self.assertFalse(os.path.exists(live))
            conf_migrate.factory_reset(live, dflt)
            self.assertEqual(open(live).read(), "a: 1\n")

    def test_raises_when_default_missing(self):
        with tempfile.TemporaryDirectory() as d:
            live = _write(d, "a: 1\n", "live.conf")
            with self.assertRaises(FileNotFoundError):
                conf_migrate.factory_reset(live, os.path.join(d, "nope.conf"))

    def test_leaves_no_tmp_file_behind(self):
        with tempfile.TemporaryDirectory() as d:
            dflt = _write(d, "a: 1\n", "default.conf")
            live = _write(d, "a: 2\n", "live.conf")
            conf_migrate.factory_reset(live, dflt)
            self.assertFalse(os.path.exists(live + ".tmp"))


if __name__ == "__main__":
    unittest.main()
