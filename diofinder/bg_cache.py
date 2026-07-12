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

from diofinder import bg_modes as _bg_modes

log = logging.getLogger("diofinder.bg_cache")

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
# sycamore >= 0.13: the full per-pixel cached model (the temporal median stack
# subtracted directly). The wheel exports an explicit flag because native
# functions don't support inspect.signature-based kwarg probing reliably.
HAS_BG_IMAGE = HAS_CACHE and bool(getattr(star_detect, "HAS_BG_IMAGE", False))

# Modes composable with the per-row cached model. block_percentile is
# additionally cache-compatible on sycamore>=0.12 via a block-median grid
# (handled separately in detect()). column_percentile,
# row_column_percentile, and uniform_mean need full-image spatial
# preprocessing and always force the per-frame path.
# Derived from the bg_modes registry (v0.11.48) — the single source of
# truth for per-mode facts; do not hand-edit a mode list here.
CACHE_COMPATIBLE_MODES = frozenset(
    name for name, d in _bg_modes.MODES.items() if d["cache_kind"] == "row")


class CacheState(Enum):
    WARMING_UP = auto()  # not enough frames collected yet
    STEADY = auto()      # cache fresh; use detect_stars_with_cache
    SLEWING = auto()     # IMU says we moved; cache stale until rebuilt


@dataclass(frozen=True)
class BgModel:
    """Immutable snapshot of the cached background (atomic swap).

    Exactly one of row_offsets / block_offsets / bg_image is populated
    depending on the background mode the model was built for:
      * row_offsets   — per-row floor (row_percentile / line_median / top_hat)
      * block_offsets — 2-D block-median grid (block_percentile, sycamore>=0.12)
      * bg_image      — the binned temporal median itself, subtracted per-pixel
                        (temporal_median, sycamore>=0.13); removes gradients,
                        vignetting AND fixed-pattern structure in one pass
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
    bg_image: Optional[np.ndarray] = None        # uint8, (h_det, w_det)
    pose_quat: Optional[Tuple[float, float, float, float]] = None


class BackgroundCache:
    def __init__(self, cfg):
        self.enabled = bool(cfg.bg_cache_enabled)
        self.bin = max(1, int(cfg.detect_bin))
        # Full-res frame dims — the model's h/w are always full resolution even
        # when the stack holds pre-binned frames (P5).
        self._full_hw = (int(getattr(cfg, "frame_height", 760)),
                         int(getattr(cfg, "frame_width", 960)))
        # P5 (opt-in, default OFF): bin each frame to DETECTION resolution at
        # submit and median-stack there, instead of stacking full-res and
        # binning the median. ~2x less stack memory (uint16 binned sums) and
        # ~4x fewer median elements (the ~100-300 ms GIL-held rebuild). Stored
        # as uint16 SUMS so median(sums)/bin**2 reproduces the float
        # median(bin_mean(frames)) estimator EXACTLY (no u8 quantization of
        # the MAD). NOTE: that estimator is NOT algebraically identical to the
        # default bin_mean(median(frames)) — spatial-mean and temporal-median
        # do not commute — but offline quantification put the divergence at
        # ~1-2% of the noise scalar on real sky (up to ~5% on steep synthetic
        # structure), with identical u8 offsets and matching star counts at
        # the operating sigma. Hence opt-in + A/B before the default flips.
        self.stack_size = max(2, int(cfg.bg_cache_stack))
        self.refresh_interval_s = float(cfg.bg_cache_refresh_s)
        self.slew_threshold_rad = math.radians(float(cfg.bg_cache_slew_deg))
        self.max_age_s = float(cfg.bg_cache_max_age_s)

        # Active per-frame mode + block size, updated by detect() each call so
        # the worker knows which kind of model to build (row vs. block grid).
        self._active_bg_mode = str(cfg.detect_bg_mode)
        self._active_block_size = int(getattr(cfg, "detect_bg_block_size", 0))
        self._bin_at_submit = (bool(getattr(cfg, "bg_cache_bin_at_submit", False))
                               and self.bin > 1)

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
        # Rate-based slew detection: the previous pose observation and which
        # source feeds it. "_slewing" means *currently moving* (consecutive
        # observations differ > threshold), NOT "displaced from the model build
        # pose" — the latter latched SLEWING forever once you aimed the finder,
        # because the worker won't rebuild while slewing so the model pose never
        # caught up. IMU and solved quats live in different frames, so only one
        # source drives motion at a time (IMU when present, else the solver).
        self._motion_ref_quat: Optional[Tuple[float, float, float, float]] = None
        self._imu_feeding = False
        self._camera_epoch = None   # last seen camera_settings_epoch (adopt-first)

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
            if self._bin_at_submit:
                self._frame_buf.append(self._bin_sum_u16(frame_u8))
            else:
                self._frame_buf.append(
                    np.array(frame_u8, dtype=np.uint8, copy=True))

    def note_camera_settings(self, epoch):
        """Called with shared_cfg['camera_settings_epoch'] (bumped by
        camera_proc on every successful exposure/gain change).

        Frames captured at the old setting no longer share the new frames'
        background pedestal (sky background scales with gain x exposure), so a
        stack spanning the change produces a model that under- or
        over-subtracts by up to a full auto-exposure step (x1.5) for up to two
        stack periods — spurious detections after a step up, lost stars after
        a step down, which feeds the very auto-exposure hunting that caused
        the step. Flush the buffer and mark the model stale; detection falls
        back to per-frame (correct while brightness is unstable) until the
        stack refills at the new setting.

        The first observed epoch is adopted silently so a solver restart never
        invalidates a healthy state.
        """
        if not self.enabled or epoch is None:
            return
        if self._camera_epoch is None:
            self._camera_epoch = epoch
            return
        if epoch != self._camera_epoch:
            self._camera_epoch = epoch
            with self._frame_buf_lock:
                self._frame_buf.clear()
            self._invalidate_gen = getattr(self, "_invalidate_gen", 0) + 1
            self._needs_rebuild.set()

    def note_bin_at_submit(self, on):
        """Live A/B toggle for the P5 bin-at-submit path. On a change the
        frame buffer is flushed (it must not mix full-res u8 and pre-binned
        uint16 frames) and the model is marked stale — detection falls back
        to per-frame until the stack refills. O(1) when unchanged."""
        if not self.enabled:
            return
        want = bool(on) and self.bin > 1
        if want == self._bin_at_submit:
            return
        self._bin_at_submit = want
        with self._frame_buf_lock:
            self._frame_buf.clear()
        self._invalidate_gen = getattr(self, "_invalidate_gen", 0) + 1
        self._needs_rebuild.set()
        log.info("bg-cache bin_at_submit -> %s (buffer flushed)", want)

    def _bin_sum_u16(self, frame_u8):
        """2x2 (or bin x bin) block SUMS as uint16 at detection resolution.
        Exact: max sum = bin**2 * 255 (1020 at bin=2, 4080 at bin=4) < 65535."""
        b = self.bin
        h, w = frame_u8.shape
        f = frame_u8[: (h // b) * b, : (w // b) * b].astype(np.uint16)
        return f.reshape(h // b, b, w // b, b).sum(axis=(1, 3)).astype(np.uint16)

    def note_motion(self, quat):
        """Called with the latest IMU quaternion (w,x,y,z) or None."""
        if not self.enabled or quat is None:
            return
        self._imu_feeding = True
        self._imu_last_seen = time.monotonic()
        self._last_imu_quat = tuple(quat)
        self._update_slew(self._last_imu_quat)

    def note_imu_lost(self):
        """Called by the solver when shared_cfg says the IMU is unavailable.

        Without this, _imu_feeding latched True forever after the first
        note_motion, so an IMU that died mid-session (cable, driver) disabled
        BOTH solver-derived slew detection and the fail-streak invalidation
        for the rest of the run — a stale model served STEADY after every
        slew until the 60 s max-age expiry."""
        if self._imu_feeding:
            self._imu_feeding = False

    def _update_slew(self, new_quat) -> None:
        """Drive slew/rebuild state from a fresh pose observation.

        ``new_quat`` must come from a single, frame-consistent source (IMU xor
        the solver — never mixed). Two independent signals:

        * **currently moving** (``_slewing``) — the pose changed > threshold
          between *consecutive* observations. This gates the rebuild worker
          (don't rebuild mid-motion) and AUTO-CLEARS the moment motion stops, so
          a slew that ends at a new resting pose is no longer a permanent
          SLEWING latch.
        * **model stale** (``_needs_rebuild``) — the pose is displaced >
          threshold from where the live model was built (a new field). Marks the
          model for rebuild but does NOT block it; the worker refreshes at the
          new pose once motion stops.
        """
        ref = self._motion_ref_quat
        self._motion_ref_quat = new_quat
        if ref is not None:
            if _angular_distance(new_quat, ref) > self.slew_threshold_rad:
                self._slewing = True
                self._needs_rebuild.set()
            elif self._slewing:
                # Motion stopped since the last observation: clear the gate and
                # ask for a rebuild at the new resting pose.
                self._slewing = False
                self._needs_rebuild.set()
        model = self._model
        if model is not None and model.pose_quat is not None:
            if _angular_distance(new_quat, model.pose_quat) > self.slew_threshold_rad:
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
        if solved:
            self._solve_fail_run = 0
        if solved and quat is not None:
            self._last_solved_quat = tuple(quat)
            # Solver-derived motion only drives slew when there's no IMU: the
            # IMU is the higher-rate, authoritative source, and the two quats
            # live in different frames (mixing them produced garbage angles).
            if not self._imu_feeding:
                self._update_slew(self._last_solved_quat)
        elif not solved:
            self._solve_fail_run += 1
            # IMU-less ONLY: when the IMU is feeding, note_motion already detects
            # real slews, so a run of solve failures is NOT evidence of an
            # unsensed slew — on a stationary, faint-signal scene it just means
            # the sky is hard. Invalidating the model there would drop the
            # √N-noise-reduced cached background for *noisier* per-frame
            # detection, making faint solves harder still (observed: cache stuck
            # ~88% in fallback while failing on faint sky). The fail-streak net
            # is for the no-IMU case, matching this method's stated purpose.
            if (not self._imu_feeding
                    and self._fail_invalidate
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
            if bg_block_size and int(bg_block_size) != self._active_block_size:
                self._active_block_size = int(bg_block_size)
                if self.enabled and _model_kind(bg_mode) == "block":
                    # The served block grid was built at the old tile size.
                    self._needs_rebuild.set()
            if self.enabled and _model_kind(prev_mode) != _model_kind(bg_mode):
                self._needs_rebuild.set()

            is_block_cache = (bg_mode == "block_percentile" and HAS_BLOCK_CACHE
                              and self.enabled)
            is_image_cache = (bg_mode == "temporal_median" and HAS_BG_IMAGE
                              and self.enabled)

            m = self._model
            force_perframe = (bg_mode not in CACHE_COMPATIBLE_MODES
                              and not is_block_cache and not is_image_cache)
            # The cached model's noise is a temporal MAD; it cannot express a
            # different estimator. With noise_mode=global_rms the STEADY path
            # would silently use MAD while the fallback path used global RMS —
            # a detection-threshold discontinuity across cache states. Keep
            # the estimator consistent by staying per-frame.
            if noise_mode and noise_mode != "mad":
                force_perframe = True
            steady = (
                not force_perframe
                and self.enabled and self.state() is CacheState.STEADY and m is not None
                and m.h == image_u8.shape[0] and m.w == image_u8.shape[1]
                and m.bin == self.bin
            )
            # The steady cached path needs a model of the matching kind.
            if steady and is_image_cache and m.bg_image is None:
                steady = False
            if steady and is_block_cache and m.block_offsets is None:
                steady = False
            if (steady and not is_block_cache and not is_image_cache
                    and m.row_offsets is None):
                steady = False

            if steady:
                kw = dict(sigma=sigma, bin=self.bin, max_axis_ratio=max_axis_ratio)
                self._maybe_add_v12(kw, kernel_sigma, local_noise, cached=True)
                if is_image_cache:
                    kw["bg_image"] = m.bg_image
                    self._n_cached += 1
                    return star_detect.detect_stars_with_cache(
                        image_u8, noise=m.noise, **kw)
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
        elif bg_mode == "temporal_median":
            # No per-frame equivalent exists (the model IS temporal): during
            # warm-up / slew / camera-settings refill — or on a wheel without
            # bg_image support — degrade to the closest spatial mode.
            kw["bg_mode"] = ("block_percentile" if HAS_BLOCK_MEDIANS
                             else "line_median")
            if kw["bg_mode"] == "block_percentile" and bg_block_size:
                kw["bg_block_size"] = int(bg_block_size)
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
            "model_kind":  (("image" if m.bg_image is not None
                             else "block" if m.block_offsets is not None
                             else "row")
                            if m else None),
            "active_bg_mode": self._active_bg_mode,
            "bin_at_submit": self._bin_at_submit,   # P5 opt-in (A/B)
            "block_cache_supported": HAS_BLOCK_CACHE,
            "bg_image_supported": HAS_BG_IMAGE,
            "tophat_supported": HAS_TOPHAT,
        }

    def preview_background(self, image_u8, *, bg_mode=None, tophat_radius=12,
                           bg_block_size=0, uniform_filter_size=0,
                           noise_mode="mad"):
        """Reconstruct, at full frame resolution, the background the requested
        mode subtracts from ``image_u8`` — for the Background page's visual
        A/B. Read-only: never submits the frame or mutates cache state.

        Returns ``(bg_u8_fullres, info)``. ``info`` carries the requested mode,
        a human ``preview_source`` phrase, the model noise, and the live
        effective-path ``summary`` (so the page can label exactly what
        detection is using, independent of the mode being previewed).

        Spatial modes are recomputed per-frame from this frame (so the A/B
        compares modes without changing the live pipeline). ``temporal_median``
        has no per-frame form: its only faithful preview is the live cached
        stack (``_model.bg_image``), which lives only in this process — when no
        stack is built yet it degrades to per-frame ``block_percentile``, the
        same documented degradation ``detect()`` uses."""
        h, w = image_u8.shape
        req = str(bg_mode or self._active_bg_mode or "row_percentile")
        m = self._model
        st = self.stats()
        if req == "temporal_median":
            if m is not None and m.bg_image is not None and HAS_BG_IMAGE:
                bg = _upsample_binned(m.bg_image, h, w, m.bin)
                source = ("live temporal-median stack "
                          f"({m.n_frames} frames, "
                          f"{round(time.monotonic() - m.epoch, 1)}s old)")
            else:
                bg = _per_frame_background(
                    image_u8, "block_percentile", block_size=bg_block_size)
                source = ("no temporal-median stack built yet — showing "
                          "per-frame block_percentile (the live degradation)")
        else:
            bg = _per_frame_background(
                image_u8, req, tophat_radius=tophat_radius,
                block_size=bg_block_size, uniform_size=uniform_filter_size)
            source = f"per-frame {req}"
        bg_u8 = np.clip(np.rint(bg), 0, 255).astype(np.uint8)
        try:
            summary = resolve_effective(
                st, st.get("active_bg_mode"), noise_mode).get("summary")
        except Exception:
            summary = None
        return bg_u8, {
            "requested_mode": req,
            "preview_source": source,
            "noise": st.get("noise"),
            "model_kind": st.get("model_kind"),
            "model_age_s": st.get("model_age_s"),
            "summary": summary,
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
            gen_before = getattr(self, "_invalidate_gen", 0)
            try:
                model = self._build_model(stack)
                if getattr(self, "_invalidate_gen", 0) != gen_before:
                    # A camera-settings flush landed while np.median was
                    # running: this model was built entirely from OLD-pedestal
                    # frames. Publishing it (and clearing the rebuild flag)
                    # would serve a wrong background as STEADY — the exact
                    # mixed-pedestal failure the epoch flush exists to stop.
                    # Discard; the flag stays set and the next pass rebuilds
                    # from post-change frames.
                    log.info("bg-cache build discarded (camera settings "
                             "changed mid-build)")
                    continue
                # Clear the flag BEFORE the final gen re-check: with the
                # old order (publish, then clear) a flush landing between
                # them had its rebuild request wiped and its stale-model
                # discard skipped — an old-pedestal model then served STEADY
                # until max-age expiry (audit 2026-07 W-L1).
                self._needs_rebuild.clear()
                if getattr(self, "_invalidate_gen", 0) != gen_before:
                    self._needs_rebuild.set()
                    log.info("bg-cache build discarded (camera settings "
                             "changed in publish window)")
                    continue
                self._model = model  # atomic publish
                last_build = now
                self._n_builds += 1
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
        # Median-stack in float and KEEP float through binning and the noise
        # estimate. The previous uint8 casts (median -> u8, bin-mean -> u8)
        # quantized the MAD to whole DN, so `noise` could only take the values
        # {0.5, 1.48, 2.97, ...} — a 3x detection-threshold jump between
        # adjacent states, observed live as noise flipping 0.50 <-> 1.48 on
        # faint sky. Float MAD has sub-DN resolution; the estimator itself is
        # unchanged, so calibrated sigma operating points keep their meaning
        # (including the 0.5 floor).
        if frames[0].dtype == np.uint16:
            # P5 pre-binned path: frames are uint16 block SUMS at detection
            # resolution. median(sums)/bin**2 reproduces the float
            # median(bin_mean(frames)) estimator exactly, on 1/bin**2 the
            # elements with 1/2 the stack memory. This APPROXIMATES (does not
            # equal) the default bin_mean(median) below — see the __init__
            # note; A/B before flipping the default. Model h/w are full
            # resolution (from cfg), since the stored frames are binned.
            h, w = self._full_hw
            time_med_f = (np.median(np.stack(frames, axis=0), axis=0)
                          .astype(np.float32) / (self.bin * self.bin))
        else:
            # Default path: frames are full-resolution, so their own shape is
            # the model's h/w and drives the binning reshape.
            h, w = frames[0].shape
            time_med_f = np.median(np.stack(frames, axis=0),
                                   axis=0).astype(np.float32)
            if self.bin > 1:
                # Downsample to the DETECTION resolution for any bin (1/2/4).
                # This used to run only for bin == 2; at bin=4 the full-res
                # model then failed sycamore's height//bin validation on every
                # steady frame — a permanent extraction-exception loop.
                b = self.bin
                tm = time_med_f[: (h // b) * b, : (w // b) * b]
                time_med_f = tm.reshape(h // b, b, w // b, b).mean(axis=(1, 3))
        h_det, w_det = time_med_f.shape
        patch = time_med_f[h_det // 3: 2 * h_det // 3,
                           w_det // 3: 2 * w_det // 3].ravel()
        mad = np.median(np.abs(patch - np.median(patch)))
        noise = max(0.5, 1.4826 * float(mad))
        # u8 model image for the Rust offset builders (rounded, not truncated).
        time_med = np.ascontiguousarray(
            np.clip(np.rint(time_med_f), 0, 255).astype(np.uint8))

        common = dict(
            noise=noise, h=h, w=w, bin=self.bin,
            epoch=time.monotonic(), n_frames=len(frames),
            # Prefer the IMU pose (20 Hz) when present; fall back to the last
            # solved attitude so note_solve_result can detect pointing jumps
            # on IMU-less units.
            pose_quat=self._last_imu_quat or self._last_solved_quat)

        # Model kind follows the active mode: the full per-pixel median for
        # temporal_median (sycamore>=0.13), a block-median grid for
        # block_percentile (sycamore>=0.12), otherwise the per-row model.
        if self._active_bg_mode == "temporal_median" and HAS_BG_IMAGE:
            return BgModel(bg_image=time_med, **common)
        if self._active_bg_mode == "block_percentile" and HAS_BLOCK_CACHE:
            bs = int(self._active_block_size) or 32
            block_offsets = star_detect.compute_block_medians_py(
                time_med, block_size=bs)
            return BgModel(block_offsets=block_offsets, block_size=bs, **common)

        row_offsets = star_detect.compute_row_medians_py(time_med)
        return BgModel(row_offsets=row_offsets, **common)


# --- Background preview (webui /bg.jpg) ------------------------------------
# These reconstruct, at full frame resolution, the background a given mode
# subtracts — for the Background page's visual A/B and, uniquely, so the live
# temporal-median stack (which exists ONLY in this process's memory) can be
# rendered. This is the single home for background-preview math: the webui
# used to reimplement it (and had no temporal_median case, so it silently
# showed line_median). All pure numpy (scipy only for uniform_mean/top_hat)
# so they unit-test without a wheel or camera.

def _fit_1d(a: np.ndarray, n: int) -> np.ndarray:
    """Crop or edge-pad a 1-D array to length n."""
    if a.shape[0] == n:
        return a
    if a.shape[0] > n:
        return a[:n]
    return np.concatenate([a, np.full(n - a.shape[0], a[-1], a.dtype)])


def _fit_to(a: np.ndarray, h: int, w: int) -> np.ndarray:
    """Crop or edge-pad a 2-D array to (h, w) — the binned model may be a few
    pixels short of the full frame after the bin reshape truncation."""
    ah, aw = a.shape
    if ah > h:
        a = a[:h]
    elif ah < h:
        a = np.vstack([a, np.repeat(a[-1:], h - ah, axis=0)])
    if aw > w:
        a = a[:, :w]
    elif aw < w:
        a = np.hstack([a, np.repeat(a[:, -1:], w - aw, axis=1)])
    return a


def _upsample_binned(a_u8: np.ndarray, h: int, w: int, bin: int) -> np.ndarray:
    """Nearest-neighbour upsample a binned (h//bin, w//bin) model image to the
    full (h, w) frame — how the temporal-median stack maps back onto pixels."""
    b = max(1, int(bin))
    a = np.asarray(a_u8, dtype=np.float32)
    up = np.repeat(np.repeat(a, b, axis=0), b, axis=1)
    return _fit_to(up, h, w)


def _broadcast_rows(rows_u8: np.ndarray, h: int, w: int, bin: int) -> np.ndarray:
    """Expand a per-(binned-)row floor to the full (h, w) frame."""
    b = max(1, int(bin))
    r = _fit_1d(np.repeat(np.asarray(rows_u8, dtype=np.float32), b), h)
    return np.repeat(r[:, None], w, axis=1)


def _bilinear_to(grid_u8: np.ndarray, h: int, w: int) -> np.ndarray:
    """Bilinearly interpolate a block-median grid to the full (h, w) frame —
    the same reconstruction block_percentile applies to its tile medians."""
    grid = np.asarray(grid_u8, dtype=np.float32)
    gh, gw = grid.shape
    if gh == 1 and gw == 1:
        return np.full((h, w), float(grid[0, 0]), np.float32)
    ys = np.linspace(0, gh - 1, h)
    xs = np.linspace(0, gw - 1, w)
    y0 = np.floor(ys).astype(int)
    x0 = np.floor(xs).astype(int)
    y1 = np.minimum(y0 + 1, gh - 1)
    x1 = np.minimum(x0 + 1, gw - 1)
    wy = (ys - y0)[:, None]
    wx = (xs - x0)[None, :]
    top = grid[np.ix_(y0, x0)] * (1 - wx) + grid[np.ix_(y0, x1)] * wx
    bot = grid[np.ix_(y1, x0)] * (1 - wx) + grid[np.ix_(y1, x1)] * wx
    return top * (1 - wy) + bot * wy


def _per_frame_background(frame_u8: np.ndarray, mode: str, *,
                          tophat_radius: int = 12, block_size: int = 32,
                          uniform_size: int = 25) -> np.ndarray:
    """Reconstruct the per-frame background for a spatial mode from a single
    frame (float32, full resolution). Faithful for the percentile/median
    modes; uniform_mean/top_hat use scipy with a rectangular window and fall
    back to line_median if scipy is missing. Unknown modes -> line_median."""
    f = frame_u8.astype(np.float32)
    h, w = f.shape
    if mode == "row_percentile":
        return np.repeat(np.percentile(f, 25, axis=1)[:, None], w, axis=1)
    if mode == "column_percentile":
        return np.repeat(np.percentile(f, 25, axis=0)[None, :], h, axis=0)
    if mode == "row_column_percentile":
        rf = np.percentile(f, 25, axis=1)[:, None]
        cf = np.percentile(f, 25, axis=0)[None, :]
        g = float(np.percentile(f, 25))
        return np.clip(rf + cf - g, 0.0, None)
    if mode == "block_percentile":
        bs = max(4, int(block_size) or 32)
        bg = np.empty((h, w), np.float32)
        for y0 in range(0, h, bs):
            for x0 in range(0, w, bs):
                y1, x1 = min(h, y0 + bs), min(w, x0 + bs)
                bg[y0:y1, x0:x1] = np.percentile(f[y0:y1, x0:x1], 25)
        return bg
    if mode == "uniform_mean":
        try:
            from scipy import ndimage
            return ndimage.uniform_filter(
                f, size=max(3, int(uniform_size) or 25), mode="nearest")
        except Exception:
            pass
    if mode == "top_hat":
        try:
            from scipy import ndimage
            k = 2 * max(1, int(tophat_radius)) + 1
            return ndimage.grey_opening(f, size=(k, k))
        except Exception:
            pass
    # line_median and any unknown/scipy-missing fallback.
    return np.repeat(np.median(f, axis=1)[:, None], w, axis=1)


def _model_kind(bg_mode: str) -> str:
    """Which cached-model kind a given bg_mode wants: 'image' (temporal_median
    on a sycamore>=0.13 wheel), 'block' (block_percentile on a capable wheel)
    or 'row' (everything else that composes with the cache). Registry-driven
    (bg_modes.MODES) with the wheel-capability gate applied here."""
    kind = (_bg_modes.MODES.get(bg_mode) or {}).get("cache_kind")
    if kind == "image" and HAS_BG_IMAGE:
        return "image"
    if kind == "block" and HAS_BLOCK_CACHE:
        return "block"
    return "row"


def _angular_distance(q1, q2) -> float:
    w1, x1, y1, z1 = q1
    w2, x2, y2, z2 = q2
    dot = abs(w1 * w2 + x1 * x2 + y1 * y2 + z1 * z2)
    return 2.0 * math.acos(min(1.0, dot))


def resolve_effective(stats: dict, requested_mode: str,
                      noise_mode: str = "mad") -> dict:
    """Resolve what background subtraction is ACTUALLY doing right now.

    The answer to "is a cached background being used, and which kind?" was
    previously spread across this module's internals, wheel capabilities, and
    the live cache state — nowhere composed into one statement. This derives
    it from the SAME facts ``detect()`` decides with (a ``stats()`` snapshot
    plus the requested mode/noise_mode), so it cannot drift from the engine.

    Pure on its inputs (unit-tested without a wheel or camera). Returns::

        {requested_mode, effective_mode, path, reason, summary}

    ``path`` is one of ``cached-image`` / ``cached-block`` / ``cached-row`` /
    ``per-frame``; ``effective_mode`` differs from ``requested_mode`` only on
    a wheel-capability degradation (top_hat -> line_median on pre-0.9 wheels;
    temporal_median -> block_percentile whenever the image cache can't serve).
    ``reason`` explains a per-frame path in one phrase; ``summary`` is the
    single human sentence the web UI shows verbatim.
    """
    requested = str(requested_mode or stats.get("active_bg_mode")
                    or "row_percentile")
    effective = requested
    enabled = bool(stats.get("enabled"))
    state = str(stats.get("state") or "NONE")

    # Wheel-capability degradations (mirrors detect()'s want_tophat gate and
    # the temporal_median per-frame fallback).
    if requested == "top_hat" and not stats.get("tophat_supported", True):
        effective = "line_median"

    # Which cached path could serve this mode, per detect()'s candidacy
    # rules — registry-driven (bg_modes.MODES cache_kind + the wheel flags
    # the stats snapshot reports).
    kind = (_bg_modes.MODES.get(requested) or {}).get("cache_kind")
    if kind == "image" and stats.get("bg_image_supported"):
        want = ("cached-image", "image")
    elif kind == "block" and stats.get("block_cache_supported"):
        want = ("cached-block", "block")
    elif effective in CACHE_COMPATIBLE_MODES:
        want = ("cached-row", "row")
    else:
        want = None

    path, reason = "per-frame", None
    if want is None:
        reason = ("mode needs full-frame spatial preprocessing — never "
                  "cached" if kind is None
                  else "installed sycamore wheel lacks this cached path")
    elif not enabled:
        reason = "temporal cache disabled (bg_cache_enabled: false)"
    elif noise_mode and noise_mode != "mad":
        reason = (f"noise_mode={noise_mode} can't use the cached MAD model — "
                  "kept per-frame for a consistent threshold")
    elif state != "STEADY":
        reason = ("collecting the first frame stack" if state == "WARMING_UP"
                  else "moving / model stale — rebuilding" if state == "SLEWING"
                  else f"cache state {state}")
    elif not stats.get("has_model"):
        reason = "no model built yet"
    elif stats.get("model_kind") != want[1]:
        reason = (f"model kind '{stats.get('model_kind')}' != wanted "
                  f"'{want[1]}' — rebuilding for the new mode")
    else:
        path = want[0]

    # temporal_median has no per-frame form: off the image cache it runs as
    # per-frame block_percentile (documented degradation).
    if requested == "temporal_median" and path != "cached-image":
        effective = "block_percentile"

    served_c = int(stats.get("served_cached") or 0)
    served_f = int(stats.get("served_fallback") or 0)
    total = served_c + served_f
    age = stats.get("model_age_s")
    if path == "per-frame":
        head = (f"{requested} per-frame" +
                (f" (as {effective})" if effective != requested else "") +
                f" — {reason}")
    else:
        kind_lbl = {"cached-image": "full-image temporal cache",
                    "cached-block": "block-grid temporal cache",
                    "cached-row": "per-row temporal cache"}[path]
        head = (f"{requested} via {kind_lbl} ({state}" +
                (f", model {age:.0f}s old" if age is not None else "") + ")")
    if total:
        pct = f"{100.0 * served_c / total:.0f}%"
        summary = (f"{head}; served {pct} cached "
                   f"({served_c} cached / {served_f} per-frame)")
    else:
        summary = f"{head}; no detections served yet"

    return {"requested_mode": requested, "effective_mode": effective,
            "path": path, "reason": reason, "summary": summary}
