"""Unit tests for the centered-star naming selection (star_names.py).

Covers both selection modes: brightest_within (default display behaviour —
the notable star wins within the radius) and nearest (the original
pure-proximity behaviour, kept behind the expert toggle).
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diofinder.star_names import StarNames


def _catalog():
    # Three stars along +Dec from the pointing (ra=100, dec=20):
    #   A: 0.5 deg away, mag 6.0  (nearest, faint)
    #   B: 1.5 deg away, mag 1.0  (bright, within 2 deg)
    #   C: 5.0 deg away, mag 0.0  (brightest, outside 2 deg)
    return StarNames(
        ra_deg=[100.0, 100.0, 100.0],
        dec_deg=[20.5, 21.5, 25.0],
        mag=[6.0, 1.0, 0.0],
        names=["A", "B", "C"],
        desigs=["a", "b", "c"],
    )


def test_brightest_within_prefers_bright_over_near():
    star = _catalog().brightest_within(100.0, 20.0, 2.0)
    assert star["name"] == "B"          # mag 1.0 beats the nearer mag 6.0
    assert abs(star["sep_deg"] - 1.5) < 0.05
    assert star["mag"] == 1.0


def test_brightest_within_respects_radius():
    # Radius 6 deg admits C (mag 0.0) -> it wins despite being farthest.
    star = _catalog().brightest_within(100.0, 20.0, 6.0)
    assert star["name"] == "C"
    # Radius 0.2 deg admits nothing.
    assert _catalog().brightest_within(100.0, 20.0, 0.2) is None


def test_nearest_is_pure_proximity():
    # fov 13.64 -> radius ~8.9 deg spans all three; nearest wins on angle.
    star = _catalog().nearest(100.0, 20.0, 13.64)
    assert star["name"] == "A"
    assert abs(star["sep_deg"] - 0.5) < 0.05
