"""Frame exposure/contrast health check (cheap histogram diagnostics).

A pure-numpy assessment of whether an 8-bit detection frame is *clipped low*
(background pinned at 0 DN), *compressed* (using only the bottom of the 0-255
range), or *saturating* — conditions that throttle faint-star detection
regardless of the extractor. Used by ``solver_proc`` to print a throttled
warning so an exposure/gain/black-level problem is caught on-device.

This does NOT try to fix anything (no stretch, no rescale): the right fixes are
exposure/gain (auto-exposure) and the libcamera black level. It only *flags* the
condition. Pure numpy, no hardware import — unit-tested in
``tests/test_frame_health.py``.

The motivating real case: a frame with 70 % of pixels at 0 DN, 30 % at 1 DN,
and a peak of 44/255 — the whole image crushed into the bottom ~6 % of the range
with the background clipped to zero, so faint stars sit in the 0<->1 DN
quantization mud and are lost before the detector ever sees them.
"""
from __future__ import annotations

import numpy as np


def assess(
    frame_u8: np.ndarray,
    *,
    clip_low_frac: float = 0.60,
    compress_peak_dn: int = 64,
    sat_frac: float = 5e-4,
) -> dict:
    """Return histogram-health metrics for an 8-bit frame.

    Thresholds (all tunable):
      * clip_low_frac    — warn if more than this fraction of pixels are exactly
                           0 DN (background subtracted/clipped to the floor). A
                           healthy frame with a proper black-level pedestal has
                           very few exact zeros.
      * compress_peak_dn — warn if the *brightest* pixel in the whole frame is
                           below this, i.e. nothing reaches up the range
                           (under-exposed / under-gained / crushed). Using the
                           max (not a percentile) is deliberate: a sparse star
                           field is mostly low-DN background, so a percentile
                           would false-positive on every normal frame; only a
                           genuinely compressed frame has *no* bright pixel. A
                           lone hot pixel makes this conservative (won't flag),
                           which is the safe direction.
      * sat_frac         — warn if more than this fraction is at/above 254 DN.

    Returns a dict with ``warn`` plus the individual flags and the raw metrics,
    and a human ``msg`` summarising whichever conditions tripped.
    """
    a = np.ascontiguousarray(frame_u8).ravel()
    n = int(a.size)
    if n == 0:
        return {"warn": False, "clipped_low": False, "compressed": False,
                "saturating": False, "frac_zero": 0.0, "frac_sat": 0.0,
                "p999_dn": 0, "peak_dn": 0, "range_frac": 0.0, "msg": ""}

    hist = np.bincount(a, minlength=256).astype(np.int64)
    frac_zero = float(hist[0]) / n
    frac_sat = float(hist[254:].sum()) / n
    cum = np.cumsum(hist)
    # 99.9th-percentile DN, reported for context (background level in a field).
    p999 = int(np.searchsorted(cum, 0.999 * n))
    nz = np.flatnonzero(hist)
    peak_dn = int(nz[-1]) if nz.size else 0
    range_frac = peak_dn / 255.0

    clipped_low = frac_zero > clip_low_frac
    compressed = peak_dn < compress_peak_dn
    saturating = frac_sat > sat_frac
    warn = bool(clipped_low or compressed or saturating)

    parts = []
    if clipped_low:
        parts.append(f"{frac_zero * 100:.0f}% of pixels at 0 DN (background clipped)")
    if compressed:
        parts.append(
            f"peak only {peak_dn}/255 ({range_frac * 100:.0f}% of range used)")
    if saturating:
        parts.append(f"{frac_sat * 100:.2f}% of pixels >=254 DN (saturating)")

    return {
        "warn": warn,
        "clipped_low": clipped_low,
        "compressed": compressed,
        "saturating": saturating,
        "frac_zero": frac_zero,
        "frac_sat": frac_sat,
        "p999_dn": p999,
        "peak_dn": peak_dn,
        "range_frac": range_frac,
        "msg": "; ".join(parts),
    }
