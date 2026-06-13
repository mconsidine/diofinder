"""ROI "tracking mode" helper — pure, dependency-light, hardware-free.

After a run of confident full-frame solves the solver can switch star
*detection* from full-frame extraction to a set of small windows ("ROI")
placed around the previous frame's solved star positions. Extracting only the
windows that contained stars last frame saves the dominant full-frame
extraction cost (~6 ms full-frame -> target ~1.5 ms windowed on the Pi Zero
2 W; see sycamore-extract/ARCHITECTURE.md decision #4).

HONEST SCOPE / known limitation
-------------------------------
This module implements ROI-windowed **detection** only. The recovered
centroids are still handed to the ordinary ``solve_from_centroids`` (with a
tight attitude hint) — i.e. the solver still runs its 4-star geometric pattern
hashing. olive-solve's Python API exposes **no** pure "verify-only" entry
point (project the catalog through a known attitude, match, refine, skipping
pattern hashing), so a true verify-only fast path is **not** possible from here
and is left as future work requiring an olive-solve API addition. Do not read
this as verify-only solving — it is ROI detection plus tight-hint solving.

The module is deliberately free of any ``star_detect`` / ``picamera2`` /
``tetra3`` import so it loads and unit-tests in a wheel-less environment. The
per-window detector is **injected** as ``detect_fn`` by the caller (the solver
loop wraps ``bg_cache`` / ``star_detect`` with the live params); tests stub it.
"""
from __future__ import annotations

import math
from typing import Callable, List, Optional, Sequence, Tuple

import numpy as np

# A recovered star: (x, y, brightness, peak) in FULL-FRAME pixel coordinates,
# matching the sycamore detect_stars tuple convention (x=col, y=row).
Star = Tuple[float, float, float, float]


def _clamp_window(cx: float, cy: float, win: int, h: int, w: int):
    """Return (y0, y1, x0, x1) for a ``win``-square window centred at
    (cx, cy), clamped to the [0, w) x [0, h) image bounds.

    The window keeps its requested size where possible; near an edge it is
    shifted inward (not shrunk) so a star near the frame border still gets a
    full-size search box. If the image is smaller than ``win`` the box is the
    whole axis.
    """
    half = win // 2
    # Centre pixel indices.
    icx = int(round(cx))
    icy = int(round(cy))

    x0 = icx - half
    y0 = icy - half
    x1 = x0 + win
    y1 = y0 + win

    # Shift inward at the edges so the box stays full-size when it can.
    if x0 < 0:
        x0, x1 = 0, min(w, win)
    elif x1 > w:
        x1, x0 = w, max(0, w - win)
    if y0 < 0:
        y0, y1 = 0, min(h, win)
    elif y1 > h:
        y1, y0 = h, max(0, h - win)
    return y0, y1, x0, x1


def roi_detect(
    frame_u8: np.ndarray,
    predicted_xy: Sequence[Tuple[float, float]],
    window_px: int,
    detect_fn: Callable[[np.ndarray], Sequence[Star]],
    bin: int = 1,
    max_stars: Optional[int] = None,
    dedupe_dist_px: float = 3.0,
) -> Tuple[List[Star], int]:
    """Run ``detect_fn`` on a small window around each predicted star.

    Parameters
    ----------
    frame_u8 : np.ndarray
        Full-frame 2-D uint8 image (H, W).
    predicted_xy : sequence of (x, y)
        Predicted FULL-FRAME pixel positions (x=col, y=row) to search around —
        normally the previous successful frame's solved centroids.
    window_px : int
        Side length of each square search window in full-frame pixels.
    detect_fn : callable(window_u8) -> list[(x, y, brightness, peak)]
        Per-window detector. Coordinates it returns are window-local
        (origin = window top-left); ``roi_detect`` maps them back to full-frame.
        Injected so this module needs no star_detect import and tests can stub.
    bin : int
        Detection binning the caller's ``detect_fn`` applies. Only used to keep
        the dedupe distance sensible; window slicing is always full-resolution.
    max_stars : int or None
        If set, cap the number of returned stars (brightest first).
    dedupe_dist_px : float
        Two recoveries closer than this (full-frame px) are treated as the same
        star (overlapping windows can each catch the same neighbour). The
        brighter one is kept.

    Returns
    -------
    (stars, n_recovered_windows)
        ``stars`` — recovered full-frame (x, y, brightness, peak), brightest
        first, deduped. ``n_recovered_windows`` — how many windows yielded at
        least one detection (a coarse health signal for the state machine).
    """
    if frame_u8.ndim != 2:
        raise ValueError("frame_u8 must be a 2-D image")
    h, w = frame_u8.shape
    win = max(1, int(window_px))
    recovered: List[Star] = []
    n_windows_hit = 0

    for px, py in predicted_xy:
        y0, y1, x0, x1 = _clamp_window(float(px), float(py), win, h, w)
        if y1 <= y0 or x1 <= x0:
            continue
        window = frame_u8[y0:y1, x0:x1]
        try:
            dets = detect_fn(window)
        except Exception:
            # A single bad window must never sink the whole frame; skip it.
            continue
        if not dets:
            continue
        # Brightest detection in this window (sycamore returns brightest-first,
        # but never assume — pick by brightness explicitly).
        best = max(dets, key=lambda s: s[2])
        bx, by, brightness, peak = best[0], best[1], best[2], best[3]
        # Map window-local (x, y) back to full-frame.
        recovered.append((bx + x0, by + y0, brightness, peak))
        n_windows_hit += 1

    deduped = _dedupe(recovered, dedupe_dist_px)
    deduped.sort(key=lambda s: s[2], reverse=True)
    if max_stars is not None and max_stars >= 0:
        deduped = deduped[:max_stars]
    return deduped, n_windows_hit


def _dedupe(stars: List[Star], dist_px: float) -> List[Star]:
    """Drop duplicate recoveries of the same star from overlapping windows.

    Greedy: walk brightest-first, keep a star only if it is farther than
    ``dist_px`` from every star already kept. O(n^2) but ``n`` is the handful of
    tracked stars, so this is negligible.
    """
    if dist_px <= 0 or len(stars) < 2:
        return list(stars)
    order = sorted(stars, key=lambda s: s[2], reverse=True)
    kept: List[Star] = []
    d2 = dist_px * dist_px
    for s in order:
        sx, sy = s[0], s[1]
        dup = False
        for k in kept:
            dx = sx - k[0]
            dy = sy - k[1]
            if dx * dx + dy * dy <= d2:
                dup = True
                break
        if not dup:
            kept.append(s)
    return kept


def centroids_to_xy(centroids: Optional[np.ndarray]) -> List[Tuple[float, float]]:
    """Convert the solver's (row, col) = (y, x) float64 centroid array back to
    (x, y) prediction tuples for the next frame's ROI windows.

    The solver feeds ``solve_from_centroids`` an (N, 2) array of (row, col);
    this inverts that so ``roi_detect`` gets (x, y). Returns [] for None/empty.
    """
    if centroids is None:
        return []
    out: List[Tuple[float, float]] = []
    for c in centroids:
        # c == (row, col) == (y, x)
        out.append((float(c[1]), float(c[0])))
    return out
