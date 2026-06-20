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

# sycamore >= 0.12 capabilities (all capability-probed so older wheels degrade
# gracefully — passing an unsupported kwarg raises TypeError in the hot path).
HAS_KERNEL_SIGMA = _supports("detect_stars", "kernel_sigma")
HAS_LOCAL_NOISE = _supports("detect_stars", "local_noise")
CACHE_HAS_KERNEL_SIGMA = _supports("detect_stars_with_cache", "kernel_sigma")
CACHE_HAS_LOCAL_NOISE = _supports("detect_stars_with_cache", "local_noise")
# block_percentile becomes cache-compatible only when the wheel can both build
# a block-median grid (compute_block_medians_py) and consume it via the cached
# path's block_offsets kwarg.
HAS_BLOCK_MEDIANS = hasattr(star_detect, "compute_block_medians_py")
CACHE_HAS_BLOCK_OFFSETS = _supports("detect_stars_with_cache", "block_offsets")
HAS_BLOCK_CACHE = HAS_BLOCK_MEDIANS and CACHE_HAS_BLOCK_OFFSETS


class CacheState(Enum):
    WARMING_UP = auto()  # not enough frames collected yet
    STEADY = auto()      # cache fresh; use detect_stars_with_cache
    SLEWING = auto()     # IMU says we moved; cache stale until rebuilt


@dataclass(frozen=True)
class BgModel:
    """Immutable snapshot of the cached background (atomic swap).

    Exactly one of row_offsets / block_offsets is populated depending on the
    background mode the model was built for:
      * row_offsets   — per-row floor (row_percentile / line_median / top_hat)
      * block_offsets — 2-D block-median grid (block_percentile, sycamore>=0.12)
    """
    noise: float
    h: int
    w: int
    bin: int
    epoch: float
    n_frames: int
    row_offsets: Optional[np.ndarray] = None     # uint8, (h_det,)
    block_offsets: Optional[np.ndarray] = None   # uint8 2-D grid
    block_size: int = 0
    pose_quat: Optional[Tuple[float, float, float, float]] = None


class BackgroundCache:
    def __init__(self, cfg):
        self.enabled = bool(cfg.bg_cache_enabled)
        self.bin = max(1, int(cfg.detect_bin))
        self.stack_size = max(2, int(cfg.bg_cache_stack))
        self.refresh_interval_s = float(cfg.bg_cache_refresh_s)
        self.slew_threshold_rad = math.radians(float(cfg.bg_cache_slew_deg))
        self.max_age_s = float(cfg.bg_cache_max_age_s)

        # Active per-frame mode + block size, updated by detect() each call so
        # the worker knows which kind of model to build (row vs. block grid).
        self._active_bg_mode = str(cfg.detect_bg_mode)
        self._active_block_size = int(getattr(cfg, "detect_bg_block_size", 0))

        self._model: Optional[BgModel] = None
        self._frame_buf: "deque[np.ndarray]" = deque(maxlen=self.stack_size)
        self._frame_buf_lock = threading.Lock()
        self._needs_rebuild = threading.Event()
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._last_imu_quat: Optional[Tuple[float, float, float, float]] = None
        # Solver-derived motion fallback (works without an IMU): the last
        # solved attitude and a run-length of consecutive solve failures.
        self._last_solved_quat: Optional[Tuple[float, float, float, float]] = None
        self._solve_fail_run = 0
        self._fail_invalidate = max(0, int(getattr(cfg, "bg_cache_fail_invalidate", 3)))
        self._slewing = False

        # Lightweight counters for the bg_cache_status diagnostic.
        self._n_builds = 0      # temporal models built by the worker
        self._n_cached = 0      # detections served from the cached model
        self._n_fallback = 0    # detections served by per-frame fallback

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
        reuses its frame buffer in place.

        In steady state the worker only rebuilds every refresh_interval_s, so
        copying every frame (~0.7 MB each) is wasted bandwidth; accept every
        4th frame then. While there is no model yet (warm-up) or a rebuild is
        pending, accept every frame so the stack fills quickly."""
        if not self.enabled:
            return
        self._submit_seq = getattr(self, "_submit_seq", 0) + 1
        steady = self._model is not None and not self._needs_rebuild.is_set()
        if steady and self._submit_seq % 4 != 0:
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

    def note_solve_result(self, quat, solved: bool):
        """Solver-derived cache invalidation — the IMU-less safety net.

        The solver is itself a ~1-2 Hz attitude sensor. Two signals say the
        cached background is stale even when no IMU reported the motion:

        * a solved attitude that jumped more than the slew threshold from the
          pose the model was built at (an unsensed slew the solver caught), and
        * a run of consecutive solve failures while a model is live — detection
          against a stale post-slew background is exactly what produces them.

        Complements note_motion (harmless when an IMU is also driving slew
        detection — the two agree). `quat` is the solved (w,x,y,z) on success.
        """
        if not self.enabled:
            return
        if solved and quat is not None:
            self._last_solved_quat = tuple(quat)
            self._solve_fail_run = 0
            model = self._model
            if model is not None and model.pose_quat is not None:
                ang = _angular_distance(self._last_solved_quat, model.pose_quat)
                if ang > self.slew_threshold_rad and not self._slewing:
                    self._slewing = True
                    self._needs_rebuild.set()
                elif ang <= self.slew_threshold_rad and self._slewing:
                    self._slewing = False
                    self._needs_rebuild.set()
        elif not solved:
            self._solve_fail_run += 1
            if (self._fail_invalidate
                    and self._solve_fail_run >= self._fail_invalidate
                    and self._model is not None
                    and not self._needs_rebuild.is_set()):
                # Stale-background suspicion: drop to per-frame detection and
                # rebuild from recent frames. Reset the run so we re-arm rather
                # than thrash every subsequent failing frame.
                self._needs_rebuild.set()
                self._solve_fail_run = 0

    # ----- consumer side ---------------------------------------------------
    def state(self) -> CacheState:
        m = self._model
        if m is None:
            return CacheState.WARMING_UP
        if self._slewing:
            return CacheState.SLEWING
        # A pending rebuild means the live model is stale: an unsensed slew the
        # solver caught via its fail-streak (the IMU-less safety net), a solved
        # pose jump, a bg-mode switch, or a frame-size change all set this flag.
        # Until the worker publishes the fresh model and clears it, fall back to
        # per-frame detection. Without this the consumer kept serving the STALE
        # model during the async rebuild window — after a slew with no IMU, that
        # stale per-row background suppressed the new field's stars and the
        # solver could not re-acquire until the rebuild eventually landed.
        if self._needs_rebuild.is_set():
            return CacheState.SLEWING
        if time.monotonic() - m.epoch > self.max_age_s:
            return CacheState.SLEWING
        return CacheState.STEADY

    def detect(self, image_u8, sigma, bg_mode, tophat_radius, max_axis_ratio,
               bg_block_size=0, uniform_filter_size=0, noise_mode="mad",
               kernel_sigma=None, local_noise=None, force_per_frame=False):
        """Single detection entry point. Returns the raw star_detect list
        [(x, y, brightness, peak), ...]. Never raises for capability gaps —
        it degrades to the best supported mode.

        kernel_sigma / local_noise are sycamore>=0.12 knobs; they are passed
        only when the installed wheel supports them (capability-probed).

        force_per_frame=True bypasses the temporal cache entirely (always
        per-frame) AND leaves the cache's model-kind tracking untouched. Used by
        the offline auto-tune sweep to evaluate a candidate bg_mode cleanly
        without triggering a live model rebuild for the wrong mode."""
        # Modes composable with the per-row cached model. block_percentile is
        # additionally cache-compatible on sycamore>=0.12 via a block-median
        # grid (handled separately below). column_percentile,
        # row_column_percentile, and uniform_mean need full-image spatial
        # preprocessing and always force the per-frame path.
        CACHE_COMPATIBLE_MODES = frozenset(
            {"row_percentile", "line_median", "top_hat"})
        want_tophat = (bg_mode == "top_hat")
        if want_tophat and not HAS_TOPHAT:
            # Old wheel: silently fall back to the robust per-row median.
            want_tophat = False
            bg_mode = "line_median"

        steady = False
        if not force_per_frame:
            # Tell the worker which model shape to build for the current mode.
            # If the mode's model-kind changed (e.g. a seeing preset switched
            # row_percentile -> block_percentile), trigger a rebuild so the next
            # steady detection uses a matching model.
            prev_mode = self._active_bg_mode
            self._active_bg_mode = bg_mode
            if bg_block_size:
                self._active_block_size = int(bg_block_size)
            if self.enabled and _model_kind(prev_mode) != _model_kind(bg_mode):
                self._needs_rebuild.set()

            is_block_cache = (bg_mode == "block_percentile" and HAS_BLOCK_CACHE
                              and self.enabled)

            m = self._model
            force_perframe = (bg_mode not in CACHE_COMPATIBLE_MODES
                              and not is_block_cache)
            steady = (
                not force_perframe
                and self.enabled and self.state() is CacheState.STEADY and m is not None
                and m.h == image_u8.shape[0] and m.w == image_u8.shape[1]
                and m.bin == self.bin
            )
            # The steady cached path needs a model of the matching kind.
            if steady and is_block_cache and m.block_offsets is None:
                steady = False
            if steady and not is_block_cache and m.row_offsets is None:
                steady = False

            if steady:
                kw = dict(sigma=sigma, bin=self.bin, max_axis_ratio=max_axis_ratio)
                self._maybe_add_v12(kw, kernel_sigma, local_noise, cached=True)
                if is_block_cache:
                    kw["block_offsets"] = m.block_offsets
                    if m.block_size:
                        kw["block_size"] = int(m.block_size)
                    self._n_cached += 1
                    return star_detect.detect_stars_with_cache(
                        image_u8, noise=m.noise, **kw)
                if want_tophat and CACHE_HAS_TOPHAT:
                    kw["tophat_radius"] = int(tophat_radius)
                self._n_cached += 1
                return star_detect.detect_stars_with_cache(
                    image_u8, m.row_offsets, m.noise, **kw)

        # Per-frame path (cache disabled, warming up, slewing, spatial mode
        # incompatible with cache, or top-hat on an old cached wheel).
        kw = dict(
            sigma=sigma, bin=self.bin, centroid_full_res=True,
            max_axis_ratio=max_axis_ratio,
        )
        self._maybe_add_v12(kw, kernel_sigma, local_noise, cached=False)
        if bg_mode in CACHE_COMPATIBLE_MODES:
            if want_tophat:
                kw["bg_mode"] = "top_hat"
                kw["tophat_radius"] = int(tophat_radius)
            else:
                kw["bg_mode"] = bg_mode
        else:
            kw["bg_mode"] = bg_mode
            if bg_mode == "block_percentile" and bg_block_size:
                kw["bg_block_size"] = int(bg_block_size)
            if bg_mode == "uniform_mean" and uniform_filter_size:
                kw["uniform_filter_size"] = int(uniform_filter_size)
        if noise_mode and noise_mode != "mad":
            kw["noise_mode"] = noise_mode
        self._n_fallback += 1
        return star_detect.detect_stars(image_u8, **kw)

    @staticmethod
    def _maybe_add_v12(kw, kernel_sigma, local_noise, *, cached):
        """Add sycamore>=0.12 kwargs only when the installed wheel supports
        them on the chosen call path. No-op on older wheels."""
        has_ks = CACHE_HAS_KERNEL_SIGMA if cached else HAS_KERNEL_SIGMA
        has_ln = CACHE_HAS_LOCAL_NOISE if cached else HAS_LOCAL_NOISE
        if kernel_sigma is not None and has_ks:
            kw["kernel_sigma"] = float(kernel_sigma)
        if local_noise is not None and has_ln:
            kw["local_noise"] = bool(local_noise)

    def stats(self) -> dict:
        """Live snapshot for the bg_cache_status diagnostic."""
        m = self._model
        return {
            "enabled":     self.enabled,
            "state":       self.state().name,
            "has_model":   m is not None,
            "n_frames":    (m.n_frames if m else 0),
            "noise":       (round(m.noise, 3) if m else None),
            "model_age_s": (round(time.monotonic() - m.epoch, 1) if m else None),
            "bin":         self.bin,
            "stack_size":  self.stack_size,
            "refresh_s":   self.refresh_interval_s,
            "builds":      self._n_builds,
            "served_cached":   self._n_cached,
            "served_fallback": self._n_fallback,
            "frames_buffered": self._frame_count(),
            "slewing":     self._slewing,
            "model_kind":  (("block" if m.block_offsets is not None else "row")
                            if m else None),
            "active_bg_mode": self._active_bg_mode,
            "block_cache_supported": HAS_BLOCK_CACHE,
        }

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
                self._n_builds += 1
                self._needs_rebuild.clear()
                log.info("bg-cache model rebuilt (#%d): %d frames, noise=%.2f",
                         self._n_builds, self._model.n_frames, self._model.noise)
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
        patch = time_med[h_det // 3: 2 * h_det // 3, w_det // 3: 2 * w_det // 3]
        flat = patch.astype(np.float32).ravel()
        mad = np.median(np.abs(flat - np.median(flat)))
        noise = max(0.5, 1.4826 * float(mad))

        common = dict(
            noise=noise, h=h, w=w, bin=self.bin,
            epoch=time.monotonic(), n_frames=len(frames),
            # Prefer the IMU pose (20 Hz) when present; fall back to the last
            # solved attitude so note_solve_result can detect pointing jumps
            # on IMU-less units.
            pose_quat=self._last_imu_quat or self._last_solved_quat)

        # Build a block-median grid when block_percentile is active and the
        # wheel supports the cached block path; otherwise the per-row model.
        if self._active_bg_mode == "block_percentile" and HAS_BLOCK_CACHE:
            bs = int(self._active_block_size) or 32
            block_offsets = star_detect.compute_block_medians_py(
                time_med, block_size=bs)
            return BgModel(block_offsets=block_offsets, block_size=bs, **common)

        row_offsets = star_detect.compute_row_medians_py(time_med)
        return BgModel(row_offsets=row_offsets, **common)


def _model_kind(bg_mode: str) -> str:
    """Which cached-model kind a given bg_mode wants: 'block' (block_percentile
    on a capable wheel) or 'row' (everything else that composes with the
    cache)."""
    if bg_mode == "block_percentile" and HAS_BLOCK_CACHE:
        return "block"
    return "row"


def _angular_distance(q1, q2) -> float:
    w1, x1, y1, z1 = q1
    w2, x2, y2, z2 = q2
    dot = abs(w1 * w2 + x1 * x2 + y1 * y2 + z1 * z2)
    return 2.0 * math.acos(min(1.0, dot))
