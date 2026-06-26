"""Unit tests for diofinder.frame_health.assess (no hardware needed)."""
import numpy as np

from diofinder import frame_health


def _frame(fill, h=200, w=320):
    return np.full((h, w), fill, dtype=np.uint8)


def test_healthy_frame_no_warn():
    # Background spread across a chunk of the range, a few bright stars.
    rng = np.random.default_rng(0)
    img = rng.integers(20, 60, size=(200, 320), dtype=np.uint8)
    img[50, 50] = 220
    img[120, 200] = 180
    h = frame_health.assess(img)
    assert not h["warn"], h
    assert not h["clipped_low"] and not h["compressed"] and not h["saturating"]


def test_clipped_and_compressed_like_real_frame():
    # Reproduce the measured pathology: ~70% at 0 DN, ~30% at 1 DN, peak 44.
    n = 760 * 960
    img = np.zeros(n, dtype=np.uint8)
    img[: int(0.30 * n)] = 1
    img[:50] = 2
    img[0] = 44  # brightest "star"
    img = img.reshape(760, 960)
    h = frame_health.assess(img)
    assert h["warn"]
    assert h["clipped_low"]      # 70% at zero
    assert h["compressed"]       # 99.9th pct way below 64
    assert not h["saturating"]
    assert h["peak_dn"] == 44
    assert "clipped" in h["msg"]


def test_saturating_frame():
    img = _frame(40)
    img.ravel()[: int(0.02 * img.size)] = 255   # 2% blown out
    h = frame_health.assess(img)
    assert h["saturating"] and h["warn"]


def test_compressed_not_clipped():
    # No zeros (background at a small pedestal) but everything in the bottom.
    rng = np.random.default_rng(1)
    img = rng.integers(3, 12, size=(200, 320), dtype=np.uint8)
    h = frame_health.assess(img)
    assert h["compressed"] and not h["clipped_low"] and h["warn"]


def test_empty_frame_safe():
    h = frame_health.assess(np.zeros((0, 0), dtype=np.uint8))
    assert not h["warn"]
