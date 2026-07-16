"""Boresight-reticle geometry (webui app._draw_boresight_reticle).

The three rings are the classic Telrad pattern — DIAMETERS 0.5deg / 2deg / 4deg,
all angular (radius = half-diameter-arcsec / arcsec_per_pixel) — so they read as
true on-sky rulers regardless of the calibrated plate scale. These pin the
diameters, the plate-scale fallback, and the downsample scaling.
"""
import os
import sys

import pytest

# webui/app.py imports flask; skip cleanly in a bare environment (CI has it).
pytest.importorskip("flask")

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "webui"))

from app import _draw_boresight_reticle, _RETICLE_RING_DIAMETERS_DEG  # noqa: E402


class _FakeDraw:
    """Records ellipse/line calls instead of rasterising."""
    def __init__(self):
        self.ellipses = []
        self.lines = []

    def ellipse(self, box, **kw):
        self.ellipses.append(box)

    def line(self, seg, **kw):
        self.lines.append(seg)


def _ring_radii(fd):
    # box = [cx-r, cy-r, cx+r, cy+r] -> r = (x1-x0)/2
    return [round((b[2] - b[0]) / 2.0) for b in fd.ellipses]


def test_three_rings_at_telrad_diameters():
    fd = _FakeDraw()
    aps = 50.635  # device-calibrated plate scale (arcsec/px)
    _draw_boresight_reticle(fd, 480, 380, aps, ds=1)
    radii = _ring_radii(fd)
    assert len(radii) == 3
    # drawn diameter = 2*r*aps arcsec; within a pixel of rounding of the target.
    for r, target_deg in zip(radii, _RETICLE_RING_DIAMETERS_DEG):
        drawn_deg = 2 * r * aps / 3600.0
        assert abs(drawn_deg - target_deg) < 0.02, (r, target_deg, drawn_deg)


def test_targets_are_half_two_four_degrees():
    assert _RETICLE_RING_DIAMETERS_DEG == (0.5, 2.0, 4.0)


def test_crosshair_has_four_ticks():
    fd = _FakeDraw()
    _draw_boresight_reticle(fd, 480, 380, 50.635, ds=1)
    assert len(fd.lines) == 4


def test_fallback_when_scale_missing_or_invalid():
    for bad in (None, 0.0, -3.0, "x"):
        fd = _FakeDraw()
        _draw_boresight_reticle(fd, 480, 380, bad, ds=1)
        assert _ring_radii(fd) == [18, 71, 142]


def test_downsample_halves_radii():
    fd = _FakeDraw()
    _draw_boresight_reticle(fd, 240, 190, 50.635, ds=2)
    # full-res 18/71/142 -> //2
    assert _ring_radii(fd) == [9, 35, 71]
