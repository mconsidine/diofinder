"""
Hot-pixel mask: dark-frame capture + fast neighbor-median repair.

A hot/warm pixel reads consistently high regardless of incident light, so it
shows up as a fake "star" — especially during slews, when the temporal
background cache is offline and per-frame detection has no √N hot-pixel
rejection. This module builds a static mask from a capped-lens dark capture
and repairs masked pixels in-place before each detection.

Design constraints (Pi Zero 2W hot path):
  * Repair must be pure vectorized numpy and cost < 1 ms for a few hundred
    masked pixels. We precompute, once at load, the flat indices of each
    masked pixel and of its 8 in-bounds neighbors; repair is then a single
    gather + nanmean + scatter.
  * The mask is saved as a small .npz (flat indices + shape + count), not a
    full-frame boolean image, so it loads fast and stays tiny.

The numeric helpers here take plain numpy arrays and have no dependency on
star_detect / picamera2, so they unit-test off-device.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import json

import numpy as np

log = logging.getLogger("diofinder.hot_pixel")

DEFAULT_MASK_PATH = "/var/lib/diofinder/hot_pixel_mask.npz"


# A real dark-capture mask is a few hundred pixels even at worst-case
# exposure/gain (observed: 260-435). A mask flagging a large fraction of the
# frame means the "dark" capture SAW THE SKY (lens not capped): the global
# threshold flags every star and the bright half of any gradient, and the
# repair step then smears >100k pixels of every live frame — star counts still
# look healthy but centroid geometry is corrupted and nothing plate-solves
# (observed live: a 195,356-pixel mask, 27% of the frame, zero solves after).
# A second failure mode produced the same signature from a PROPERLY capped
# capture: MAD collapse on a clean dark frame (see MIN_THRESH_DN below) flagged
# 47% of the frame. Cap = 0.5% of the frame (~3,600 px at 960x760): ~7x
# headroom over any legitimate mask, far below either poisoned kind.
MAX_MASK_FRACTION = 0.005


def implausibly_large(count: int, shape) -> bool:
    """True when a mask is too big to be a genuine dark-capture result.

    Absolute floor of 64 so the fractional rule can't misfire on the tiny
    frames used in tests / ROIs; any real frame is ~0.7 Mpx where the 0.5%
    rule (~3,600 px) dominates."""
    try:
        total = int(shape[0]) * int(shape[1])
    except (TypeError, IndexError, ValueError):
        return False
    return total > 0 and count > max(64, MAX_MASK_FRACTION * total)


# Minimum threshold offset above the median, in DN. A clean capped dark frame
# is so uniform that >50% of its (uint8, median-stacked) pixels share one value,
# so MAD collapses to 0 and a pure k*MAD threshold degenerates to "every pixel
# above the median" — observed live: a properly capped capture flagged 345,278
# pixels, 47.3% of the frame. Real hot pixels sit >=10 DN above the pedestal;
# quantization dither sits within 1-2 DN. 3 DN cleanly separates the two.
MIN_THRESH_DN = 3.0


def compute_hot_pixel_indices(stack: np.ndarray, k: float = 5.0) -> np.ndarray:
    """Return flat indices of hot pixels in a median-stacked dark frame.

    A pixel is hot if its stacked value exceeds
        median + max(k * (1.4826 * MAD), MIN_THRESH_DN)
    of the whole stacked frame. MAD is the median absolute deviation, so the
    threshold is robust to the hot pixels themselves; the MIN_THRESH_DN floor
    keeps the threshold sane when MAD quantizes to 0 on a very clean dark frame
    (without it, everything above the median gets flagged — half the frame).

    ``stack`` is a 2-D array (the per-pixel median over N dark frames).
    Returns a 1-D int64 array of flat indices into ``stack``.
    """
    flat = stack.astype(np.float32).ravel()
    med = float(np.median(flat))
    mad = float(np.median(np.abs(flat - med)))
    thresh = med + max(k * 1.4826 * mad, MIN_THRESH_DN)
    return np.where(flat > thresh)[0].astype(np.int64)


def build_neighbor_index(indices: np.ndarray, shape) -> np.ndarray:
    """Precompute the 8-neighbor flat-index table for masked pixels.

    Returns an (M, 8) int64 array; out-of-bounds neighbors are -1. Pairs with
    ``repair_frame`` for a precompute-once / apply-many repair.
    """
    h, w = int(shape[0]), int(shape[1])
    idx = np.asarray(indices, dtype=np.int64)
    rows = idx // w
    cols = idx % w
    offs = [(-1, -1), (-1, 0), (-1, 1),
            (0, -1), (0, 1),
            (1, -1), (1, 0), (1, 1)]
    neigh = np.full((idx.size, 8), -1, dtype=np.int64)
    for j, (dr, dc) in enumerate(offs):
        nr = rows + dr
        nc = cols + dc
        valid = (nr >= 0) & (nr < h) & (nc >= 0) & (nc < w)
        neigh[valid, j] = nr[valid] * w + nc[valid]
    return neigh


def repair_frame(frame: np.ndarray, indices: np.ndarray,
                 neighbors: np.ndarray) -> None:
    """Replace each masked pixel with the mean of its in-bounds 8 neighbors.

    In-place on ``frame`` (uint8, 2-D). ``indices`` and ``neighbors`` come from
    ``build_neighbor_index``. Vectorized: one gather + masked mean + scatter.
    Neighbor values are read BEFORE any writes (gathered into a temporary), so
    adjacent masked pixels don't contaminate each other within one call.
    """
    if indices.size == 0:
        return
    flat = frame.reshape(-1)
    # Gather neighbor values; -1 sentinels become NaN so they drop out of mean.
    nb = neighbors  # (M, 8)
    valid = nb >= 0
    safe = np.where(valid, nb, 0)
    vals = flat[safe].astype(np.float32)
    vals[~valid] = np.nan
    with np.errstate(invalid="ignore"):
        repaired = np.nanmean(vals, axis=1)
    # A pixel with zero valid neighbors (shouldn't happen for interior masks)
    # keeps its original value.
    good = ~np.isnan(repaired)
    out_idx = indices[good]
    flat[out_idx] = np.clip(np.rint(repaired[good]), 0, 255).astype(frame.dtype)


class HotPixelMask:
    """Loadable hot-pixel mask with precomputed neighbor table.

    Holds the masked flat indices, the frame shape, and the neighbor table.
    ``repair`` is the hot-path entry point (no-op when empty / shape mismatch).
    """

    def __init__(self, indices: Optional[np.ndarray] = None,
                 shape=None, mtime: float = 0.0, meta: Optional[dict] = None):
        self.indices = (np.asarray(indices, dtype=np.int64)
                        if indices is not None else np.zeros(0, dtype=np.int64))
        self.shape = tuple(shape) if shape is not None else None
        self.mtime = float(mtime)
        # Capture conditions (exposure_s, gain, sensor mode, created_at):
        # hot-pixel positions are sensor-mode dependent (the output frame is
        # always 960x760, so the shape guard passes across mode changes while
        # every index points at the wrong sky pixel) and coverage scales with
        # exposure/gain. Stored so a mismatch can be surfaced instead of
        # silently corrupting faint-star pixels (audit 2026-07 F-L1).
        self.meta = dict(meta) if meta else {}
        self.neighbors = (build_neighbor_index(self.indices, self.shape)
                          if self.shape is not None and self.indices.size
                          else np.zeros((0, 8), dtype=np.int64))

    @property
    def count(self) -> int:
        return int(self.indices.size)

    def repair(self, frame: np.ndarray) -> None:
        """In-place repair, guarded against shape mismatch / empty mask."""
        if self.indices.size == 0 or self.shape is None:
            return
        if frame.shape != self.shape:
            return
        repair_frame(frame, self.indices, self.neighbors)

    def save(self, path: str = DEFAULT_MASK_PATH) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        np.savez(path, indices=self.indices,
                 shape=np.array(self.shape, dtype=np.int64),
                 count=np.int64(self.indices.size),
                 meta=np.bytes_(json.dumps(self.meta).encode("utf-8")))
        self.mtime = os.path.getmtime(path)

    @classmethod
    def load(cls, path: str = DEFAULT_MASK_PATH) -> Optional["HotPixelMask"]:
        if not os.path.exists(path):
            return None
        try:
            data = np.load(path)
            indices = data["indices"].astype(np.int64)
            shape = tuple(int(v) for v in data["shape"])
            if implausibly_large(indices.size, shape):
                log.warning(
                    "Hot-pixel mask %s flags %d pixels (>%.1f%% of the frame) — "
                    "almost certainly captured with the lens uncapped; IGNORING "
                    "it. Recapture with the lens covered, or clear the mask.",
                    path, indices.size, 100.0 * MAX_MASK_FRACTION)
                return None
            meta = {}
            if "meta" in getattr(data, "files", []):
                try:
                    raw = data["meta"].item()
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    meta = json.loads(raw)
                except Exception:
                    meta = {}
            return cls(indices=indices, shape=shape,
                       mtime=os.path.getmtime(path), meta=meta)
        except Exception as e:
            log.warning("Could not load hot-pixel mask %s: %s", path, e)
            return None


def capture_dark_mask(read_frame, n_frames: int, shape,
                      interval_s: float = 0.3, k: float = 5.0,
                      meta: Optional[dict] = None) -> HotPixelMask:
    """Median-stack ``n_frames`` from ``read_frame()`` and build a mask.

    ``read_frame`` is a callable returning a fresh 2-D uint8 frame copy
    (None to skip); when it supports the solver reader's
    ``(with_seq=True, after_seq=N)`` protocol the capture chains sequence
    numbers so every stacked frame is UNIQUE — the old wall-clock-only
    sampling re-read the same frame at long exposures (16 reads at 0.9 s
    exposure = only ~5-6 distinct frames), silently weakening the mask's MAD
    statistics (audit 2026-07 F-L2). ``shape`` is the expected frame shape.
    The lens should be capped (this is a dark capture). Returns a
    HotPixelMask (not yet saved).
    """
    frames = []
    deadline_each = max(0.0, float(interval_s))
    last_seq = -1
    chain_seq = True
    for _ in range(max(1, int(n_frames))):
        fr = None
        if chain_seq:
            try:
                res = read_frame(with_seq=True, after_seq=last_seq)
                if res is not None:
                    fr, last_seq = res
            except TypeError:
                chain_seq = False    # plain no-arg reader (tests, older API)
        if not chain_seq:
            fr = read_frame()
        if fr is not None and fr.shape == tuple(shape):
            frames.append(np.asarray(fr, dtype=np.uint8))
        time.sleep(deadline_each)
    if not frames:
        raise RuntimeError("no frames captured for dark mask")
    stack = np.median(np.stack(frames, axis=0), axis=0)
    indices = compute_hot_pixel_indices(stack, k=k)
    return HotPixelMask(indices=indices, shape=tuple(shape), meta=meta)
