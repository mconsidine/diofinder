"""Wheel-version reporting (diofinder/wheels.py) — must never raise, and must
return both labels even when a distribution is missing."""
import unittest

from diofinder.wheels import wheel_versions, wheel_versions_str, WHEEL_DISTS


class WheelVersionTests(unittest.TestCase):
    def test_returns_both_labels(self):
        v = wheel_versions()
        self.assertEqual(set(v.keys()), set(WHEEL_DISTS.keys()))
        for val in v.values():
            self.assertTrue(val is None or isinstance(val, str))

    def test_str_form_never_raises(self):
        s = wheel_versions_str()
        self.assertIn("olive-solve", s)
        self.assertIn("sycamore", s)


if __name__ == "__main__":
    unittest.main()
