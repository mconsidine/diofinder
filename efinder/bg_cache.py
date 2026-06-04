"""Temporal background cache for the solver process.

This wires sycamore's "analytic threading" background model (decision #5 in
sycamore-extract/ARCHITECTURE.md) into diofinder's live solver loop. A worker
thread maintains a temporally median-stacked per-row background floor plus a
noise sigma; steady-state detection consumes it via
``star_detect.detect_stars_with_cache`` instead of re-estimating per frame.

Why bother (re-confirmed for this integration):
  * Temporal median-stacking N frames cuts the noise in the background/noise
    estimate by ~sqrt(N), which a single frame can never do.
  * A pixel bright across every stacked frame is a hot pixel; one that comes
    and goes is a star. The stack rejects hot pixels for free.
  * It is orthogonal to the per-frame ``top_hat`` spatial background: temporal
    handles noise/hot-pixels over time, top-hat handles 2-D gradients within a
    frame. They compose (steady cached detection can use ``tophat_radius>0``).

Routing (see ``BackgroundCache.detect``):
  * disabled                       -> per-frame ``detect_stars`` with bg_mode
  * enabled + STEADY               -> ``detect_stars_with_cache`` (temporal)
  * enabled + WARMING_UP/SLEWING   -> per-frame fallback
  * bg_mode == "top_hat"           -> top-hat spatial bg (cached path passes
                                      ``tophat_radius`` so the temporal noise is
                                      still used when STEADY)

The whole thing is gated by ``cfg.bg_cache_enabled`` so it can be turned off if
the per-frame submit/stack bookkeeping proves too costly on the Pi Zero 2W.
"""
from __future__ import annotations

import inspect
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, Tuple

import numpy as np
import star_detect

log = logging.getLogger("efinder.bg_cache")

# --- Capability probe ------------------------------------------------------
# Older sycamore wheels (< 0.9.0) lack the top_hat background mode and the
# tophat_radius argument. Detect what the installed wheel supports so we can
# degrade gracefully instead of raising TypeError in the hot path.
def _supports(fn_name: str, param: str) -> bool:
    fn = getattr(star_detect, fn_name, None)
    if fn is None:
        return False
    try:
        return param in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        # Builtins sometimes have no introspectable signature; assume present
        # for the cached path (it shipped in 0.5+) and absent for tophat.
        return param != "tophat_radius"


HAS_TOPHAT = _supports("detect_stars", "tophat_radius")
HAS_CACHE = hasattr(star_detect, "detect_stars_with_cache") and hasattr(
    star_detect, "compute_row_medians_py"
)
CACHE_HAS_TOPHAT = _supports("detect_stars_with_cache", "tophat_radius")


class CacheState(Enum):
    WARMING_UP = auto()  # not enough frames collected yet
    STEADY = auto()      # cache fresh; use detect_stars_with_cache
    SLEWING = auto()     # IMU says we moved; cache stale until rebuilt


@dataclass(frozen=True)
class BgModel:
    """Immutable snapshot of the cached background (atomic swap)."""
    row_offsets: np.ndarray   # uint8, shape (h // bin,)
    noise: float
    h: int
    w: int
    bin: int
    epoch: float
    n_frames: int
    pose_quat: Optional[Tuple[float, float, float, float]] = None


class BackgroundCache:
    def __init__(self, cfg):
        self.enabled = bool(cfg.bg_cache_enabled)
        self.bin = max(1, int(cfg.detect_bin))
        self.stack_size = max(2, int(cfg.bg_cache_stack))
        self.refresh_interval_s = float(cfg.bg_cache_refresh_s)
        self.slew_threshold_rad = math.radians(float(cfg.bg_cache_slew_deg))
        self.max_age_s = float(cfg.bg_cache_max_age_s)

        self._model: Optional[BgModel] = None
        self._frame_buf: "deque[np.ndarray]" = deque(maxlen=self.stack_size)
        self._frame_buf_lock = threading.Lock()
        self._needs_rebuild = threading.Event()
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._last_imu_quat: Optional[Tuple[float, float, float, float]] = None
        self._slewing = False

        if self.enabled and not HAS_CACHE:
            log.warning(
                "bg_cache_enabled but installed star_detect lacks the cached "
                "API; falling back to per-frame detection")
            self.enabled = False

    # ----- lifecycle -------------------------------------------------------
    def start(self):
        if not self.enabled or self._worker is not None:
            return
        self._worker = threading.Thread(
            target=self._worker_loop, name="bg-cache", daemon=True)
        self._worker.start()
        log.info(
            "bg-cache worker started (bin=%d stack=%d refresh=%.1fs "
            "tophat_cached=%s)",
            self.bin, self.stack_size, self.refresh_interval_s,
            CACHE_HAS_TOPHAT)

    def stop(self):
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=2)

    # ----- producer side ---------------------------------------------------
    def submit_frame(self, frame_u8: np.ndarray):
        """Called by the solver loop each frame. Copies, since the caller
        reuses its frame buffer in place."""
        if not self.enabled:
            return
        with self._frame_buf_lock:
            self._frame_buf.append(np.array(frame_u8, dtype=np.uint8, copy=True))

    def note_motion(self, quat):
        """Called with the latest IMU quaternion (w,x,y,z) or None."""
        if not self.enabled or quat is None:
            return
        self._last_imu_quat = tuple(quat)
        model = self._model
        if model is None or model.pose_quat is None:
            return
        ang = _angular_distance(self._last_imu_quat, model.pose_quat)
        if ang > self.slew_threshold_rad and not self._slewing:
            self._slewing = True
            self._needs_rebuild.set()
        elif ang <= self.slew_threshold_rad and self._slewing:
            self._slewing = False
            self._needs_rebuild.set()

    # ----- consumer side ---------------------------------------------------
    def state(self) -> CacheState:
        m = self._model
        if m is None:
            return CacheState.WARMING_UP
        if self._slewing:
            return CacheState.SLEWING
        if time.monotonic() - m.epoch > self.max_age_s:
            return CacheState.SLEWING
        return CacheState.STEADY

    def detect(self, image_u8, sigma, bg_mode, tophat_radius, max_axis_ratio):
        """Single detection entry point. Returns the raw star_detect list
        [(x, y, brightness, peak), ...]. Never raises for capability gaps —
        it degrades to the best supported mode."""
        want_tophat = (bg_mode == "top_hat")
        if want_tophat and not HAS_TOPHAT:
            # Old wheel: silently fall back to the robust per-row median.
            want_tophat = False
            bg_mode = "line_median"

        m = self._model
        steady = (
            self.enabled and self.state() is CacheState.STEADY and m is not None
            and m.h == image_u8.shape[0] and m.w == image_u8.shape[1]
            and m.bin == self.bin
        )

        if steady:
            kw = dict(
                sigma=sigma, bin=self.bin, max_axis_ratio=max_axis_ratio,
                gate_mode="matched_filter",
            )
            if want_tophat and CACHE_HAS_TOPHAT:
                kw["tophat_radius"] = int(tophat_radius)
            return star_detect.detect_stars_with_cache(
                image_u8, m.row_offsets, m.noise, **kw)

        # Per-frame path (cache disabled, warming up, slewing, or top-hat on an
        # old cached wheel).
        kw = dict(
            sigma=sigma, bin=self.bin, centroid_full_res=True,
            gate_mode="matched_filter", max_axis_ratio=max_axis_ratio,
        )
        if want_tophat:
            kw["bg_mode"] = "top_hat"
            kw["tophat_radius"] = int(tophat_radius)
        else:
            kw["bg_mode"] = bg_mode if bg_mode in (
                "row_percentile", "line_median") else "line_median"
        return star_detect.detect_stars(image_u8, **kw)

    # ----- worker ----------------------------------------------------------
    def _worker_loop(self):
        last_build = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            need = (
                self._needs_rebuild.is_set()
                or (self._model is None and self._frame_count() >= self.stack_size)
                or (now - last_build > self.refresh_interval_s
                    and self._frame_count() >= self.stack_size
                    and not self._slewing)
            )
            if not need or self._slewing:
                self._stop.wait(0.25)
                continue
            stack = self._snapshot_stack()
            if len(stack) < max(2, self.stack_size // 2):
                self._stop.wait(0.25)
                continue
            try:
                self._model = self._build_model(stack)  # atomic publish
                last_build = now
                self._needs_rebuild.clear()
            except Exception as e:
                log.warning("bg-cache build failed: %s", e)
                self._stop.wait(0.5)

    def _frame_count(self) -> int:
        with self._frame_buf_lock:
            return len(self._frame_buf)

    def _snapshot_stack(self) -> list:
        with self._frame_buf_lock:
            return list(self._frame_buf)

    def _build_model(self, frames: list) -> BgModel:
        h, w = frames[0].shape
        time_med = np.median(np.stack(frames, axis=0), axis=0).astype(np.uint8)
        if self.bin == 2:
            tm = time_med[: (h // 2) * 2, : (w // 2) * 2]
            time_med = tm.reshape(h // 2, 2, w // 2, 2).mean(
                axis=(1, 3)).astype(np.uint8)
        time_med = np.ascontiguousarray(time_med)
        h_det, w_det = time_med.shape
        row_offsets = star_detect.compute_row_medians_py(time_med)
        patch = time_med[h_det // 3: 2 * h_det // 3, w_det // 3: 2 * w_det // 3]
        flat = patch.astype(np.float32).ravel()
        mad = np.median(np.abs(flat - np.median(flat)))
        noise = max(0.5, 1.4826 * float(mad))
        return BgModel(
            row_offsets=row_offsets, noise=noise, h=h, w=w, bin=self.bin,
            epoch=time.monotonic(), n_frames=len(frames),
            pose_quat=self._last_imu_quat)


def _angular_distance(q1, q2) -> float:
    w1, x1, y1, z1 = q1
    w2, x2, y2, z2 = q2
    dot = abs(w1 * w2 + x1 * x2 + y1 * y2 + z1 * z2)
    return 2.0 * math.acos(min(1.0, dot))
