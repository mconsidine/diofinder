"""
Comms worker process.

Pinned to its dedicated CPU. Two server endpoints:

  1. LX200 TCP server on cfg.lx200_port (default 4060) -- talks to
     SkySafari and any other LX200 client. Handles :GR/:GD pointing
     queries and the :Sr/:Sd/:CM# boresight alignment workflow.

  2. Maintenance Unix socket at /run/diofinder/maint.sock -- accepts
     newline-delimited JSON requests for inspection, calibration,
     boresight management, exposure tuning, and mode switching.
     Used by diofinder-ctl and the web UI.

Available maintenance commands:
  set_test_mode {"enabled": true | false}
  status, boresight_show/set/center, calibration_status/reset,
  polar_start/status/cancel/set_latitude, exposure_get/set, gain_set,
  auto_exposure_set, auto_tune/auto_tune_status/auto_tune_cancel,
  tuning_set, solver_params_get/set, match_params_get/set,
  seeing_get/set, seeing_override_save/clear, solve_centroids,
  bg_cache_status, tracking_status, dark_capture, hot_pixel_status,
  hot_pixel_clear
"""

import datetime
import itertools
from collections import deque
import json
import logging
import math
import os
import socket
import statistics
import subprocess
import threading
import time
from queue import Empty

from diofinder import config as cfg_mod
from diofinder.align import AlignRequest, AlignResult, CommsAlignState
from diofinder.imu_math import quat_delta_rotvec, alpha_beta_step
from diofinder.maint import MaintRequest, MaintResponse, SOCKET_PATH
from diofinder.worker_cmds import (
    SolverCmd, CameraCmd,
    SOLVER_OP_CALIBRATION_STATUS, SOLVER_OP_CALIBRATION_RESET,
    SOLVER_OP_POLAR_START, SOLVER_OP_POLAR_STATUS,
    SOLVER_OP_POLAR_CANCEL, SOLVER_OP_POLAR_SET_LATITUDE,
    SOLVER_OP_SOLVE_CENTROIDS, SOLVER_OP_BG_CACHE_STATUS,
    SOLVER_OP_SET_DB, SOLVER_OP_DARK_CAPTURE,
    SOLVER_OP_HOT_PIXEL_STATUS, SOLVER_OP_HOT_PIXEL_CLEAR,
    SOLVER_OP_TRACKING_STATUS,
    SOLVER_OP_AUTO_TUNE_EVAL,
    CAMERA_OP_GET_EXPOSURE, CAMERA_OP_SET_EXPOSURE, CAMERA_OP_SET_GAIN,
)
from diofinder import seeing as seeing_mod

log = logging.getLogger("diofinder.comms")

_request_id_seq = itertools.count(1)

# Release tag recorded by diofinder-update (and install.sh) after each OTA.
# Format: "<tag> <iso8601-timestamp>" e.g. "v0.0.28 2026-06-15T11:40:00Z".
_RELEASE_FILE = "/var/lib/diofinder/version"


def _release_info():
    """Return (tag, released_at) from the OTA-written release file.

    Returns (None, None) when the file is absent or unreadable (e.g. a manual
    install or a freshly imaged device that predates the file), so callers can
    fall back to the in-code config version.
    """
    try:
        with open(_RELEASE_FILE) as f:
            line = f.readline().strip()
    except (OSError, ValueError):
        return None, None
    if not line:
        return None, None
    parts = line.split(None, 1)
    tag = parts[0] or None
    released_at = parts[1].strip() if len(parts) > 1 else None
    return tag, released_at


def _git_describe():
    """Best-effort ``git describe`` of the installed checkout, or None.

    The image is a git checkout (install.sh provisions it so OTA works), so git
    is the authoritative source of the running tag/commit even on a freshly
    burned device that predates any OTA-written release file. Returns the exact
    tag when built from one, else ``<tag>-<n>-g<sha>`` / a bare short sha.
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        out = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", repo,
             "describe", "--tags", "--always", "--dirty"],
            capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return None

_solver_call_lock = threading.Lock()
_camera_call_lock = threading.Lock()

_solver_cache: dict = {}
_solver_cache_lock = threading.Lock()


def _cached_call_solver(op, solver_cmd_q, solver_cmd_reply_q, ttl_s: float):
    """Call solver RPC, returning a cached reply if one exists and is younger than ttl_s."""
    now = time.monotonic()
    with _solver_cache_lock:
        entry = _solver_cache.get(op)
        if entry and now - entry[0] < ttl_s:
            return entry[1]
    reply = _call_solver(op, {}, solver_cmd_q, solver_cmd_reply_q)
    if reply is not None and reply.ok:
        with _solver_cache_lock:
            _solver_cache[op] = (time.monotonic(), reply)
    return reply


def _invalidate_solver_cache(op=None):
    """Drop cached solver replies for op, or all entries when op is None."""
    with _solver_cache_lock:
        if op is None:
            _solver_cache.clear()
        else:
            _solver_cache.pop(op, None)


def _pin_to_cpu(cpu: int) -> None:
    """Set the calling process's CPU affinity to {cpu}."""
    try:
        os.sched_setaffinity(0, {cpu})
        log.info("Pinned to CPU %d", cpu)
    except Exception as e:
        log.warning("Could not pin to CPU %d: %s", cpu, e)


def _format_ra(ra_hours: float) -> str:
    """Return LX200-protocol RA string HH:MM:SS# from fractional hours."""
    ra_hours = ra_hours % 24.0
    h = int(ra_hours); m_full = (ra_hours - h) * 60.0
    m = int(m_full); s = int(round((m_full - m) * 60.0))
    if s == 60: s = 0; m += 1
    if m == 60: m = 0; h = (h + 1) % 24
    return f"{h:02d}:{m:02d}:{s:02d}#"


def _format_dec(dec_deg: float) -> str:
    """Return LX200-protocol Dec string ±DD*MM:SS# from decimal degrees."""
    sign = "+" if dec_deg >= 0 else "-"
    a = abs(dec_deg); d = int(a)
    m_full = (a - d) * 60.0; m = int(m_full)
    s = int(round((m_full - m) * 60.0))
    if s == 60: s = 0; m += 1
    if m == 60: m = 0; d += 1
    return f"{sign}{d:02d}*{m:02d}:{s:02d}#"


def _wait_for_reply(reply_q, request_id, timeout_s=5.0):
    """Block up to timeout_s for the reply matching request_id; returns None on timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        remaining = max(0.05, deadline - time.monotonic())
        try:
            reply = reply_q.get(timeout=remaining)
        except Empty:
            continue
        if reply.request_id == request_id:
            return reply
        log.debug("Discarding stale reply id=%s (waiting for %s)",
                  reply.request_id, request_id)
    return None


def _call_solver(op, args, solver_cmd_q, solver_cmd_reply_q, timeout_s=5.0):
    """Send op/args to solver_proc and return the SolverCmdReply, or None on timeout."""
    rid = next(_request_id_seq)
    with _solver_call_lock:
        solver_cmd_q.put(SolverCmd(op=op, args=args or {}, request_id=rid))
        return _wait_for_reply(solver_cmd_reply_q, rid, timeout_s=timeout_s)


def _call_camera(op, args, camera_cmd_q, camera_cmd_reply_q, timeout_s=2.0):
    """Send op/args to camera_proc and return the CameraCmdReply, or None on timeout."""
    rid = next(_request_id_seq)
    with _camera_call_lock:
        camera_cmd_q.put(CameraCmd(op=op, args=args or {}, request_id=rid))
        return _wait_for_reply(camera_cmd_reply_q, rid, timeout_s=timeout_s)


# Auto-exposure / gain controller tunables.
_AE_PEAK_SATURATION = 250     # 8-bit peak at/above which a frame is treated as clipped
_AE_EXP_UP = 1.3              # exposure multiplier when starved of signal
_AE_EXP_DOWN = 0.8            # exposure multiplier when over-served
_AE_GAIN_STEP = 1.5           # gain multiplier per ladder step
_AE_MIN_EXP_DELTA_S = 0.005   # ignore sub-5 ms exposure moves (clamped / noise)
_AE_STARVED_FRAC = 0.8        # metric below this fraction of target -> need more signal
_AE_SPARE_FRAC = 1.5          # metric above this fraction of target -> shed cost
_AE_PEAK_FLOOR = 70           # 8-bit peak below which the frame is near the
                              # detection cliff: never shed signal here even when
                              # match-rich (empirically solves span peak 36-247,
                              # but reducing past ~peak 35 starves detection)
_AE_RAISE_DEBOUNCE = 2        # consecutive raise intents required before AE
                              # actually raises brightness — so a single transient
                              # dark frame (passing cloud / wind smear) right after
                              # a good solving run doesn't bounce gain/exposure up


def _auto_exposure_decision(*, solved, stars, matches, peak,
                            cur_s, cur_g, target_stars, target_matches,
                            min_s, max_s, min_g, max_g, nominal_s=None,
                            peak_floor=_AE_PEAK_FLOOR):
    """Pure decision step for the auto-exposure / gain controller.

    Returns a dict describing the new camera state — at most one of
    ``{"exposure_s": float}`` or ``{"gain": float}`` per call — or ``None`` to
    leave the camera alone.

    Cost model: on a finder, **exposure is the expensive axis** — it sets the
    frame cadence, the pointing-feedback latency, and the star trailing on a
    moving mount — while **gain is a latency-free brightness trim** (and moderate
    analog gain even lifts signal above the sensor's read noise at low light).
    So gain is the primary knob and exposure is anchored near ``nominal_s``:

      * starved      -> raise GAIN first; stretch exposure only once gain is at
                        ``max_g`` (gain can't manufacture photons, so a genuinely
                        dark scene still needs integration time).
      * over-served  -> drop GAIN first; shorten exposure only at the gain floor.
      * saturated    -> back off, gain first (it costs only noise).
      * low-contrast -> a frame whose ``peak`` is at/below ``peak_floor`` sits
        near the detection cliff, where the abundance of matches is untrustworthy
        (low contrast both inflates spurious matches and is one fluctuation away
        from dropping below ``min_centroids``). Never shed signal here — hold
        instead of reducing. This is the asymmetric partner of the saturation
        guard and prevents the over-reduction failure observed on faint sky
        (peak walked 84 -> 45 -> 29 until detection starved).
      * settled but exposure has drifted off ``nominal_s`` with gain headroom to
        compensate -> nudge exposure one step back toward nominal, letting the
        gain ladder restore brightness next cycle, so a temporary stretch never
        becomes permanent latency.

    Wide deadband (``_AE_STARVED_FRAC`` .. ``_AE_SPARE_FRAC`` of target) so the
    loop settles instead of oscillating. The real currency is matched stars
    while solving; raw detected-star count is only a fallback when lost /
    slewing (``matches == 0`` then carries no exposure information).
    """
    if nominal_s is None:
        nominal_s = cur_s

    # 1. Saturation overrides everything: a clipped frame yields poor centroids
    #    regardless of count. Shed the cheapest-to-restore signal first (gain).
    if peak >= _AE_PEAK_SATURATION:
        if cur_g > min_g:
            new_g = max(min_g, cur_g / _AE_GAIN_STEP)
            if new_g != cur_g:
                return {"gain": round(new_g, 2)}
        new_s = max(min_s, cur_s * _AE_EXP_DOWN)
        if cur_s - new_s >= _AE_MIN_EXP_DELTA_S:
            return {"exposure_s": round(new_s, 4)}
        return None

    # Low-contrast guard: a non-dark frame whose peak is below the floor is near
    # the detection cliff. ``peak == 0`` carries no information (dark frame mid
    # slew / exposure change), so it does not trip the guard.
    low_contrast = 0 < peak < peak_floor

    # 2. Choose the metric + target. Matches drive the loop when we are solving;
    #    otherwise fall back to detected-star count.
    if solved and target_matches > 0:
        metric, target = matches, target_matches
    else:
        metric, target = stars, target_stars

    # 3. Starved — gain first; stretch exposure only when gain is maxed out.
    if metric < _AE_STARVED_FRAC * target:
        if cur_g < max_g:
            new_g = min(max_g, cur_g * _AE_GAIN_STEP)
            if new_g != cur_g:
                return {"gain": round(new_g, 2)}
        if cur_s < max_s:
            new_s = min(max_s, cur_s * _AE_EXP_UP)
            if new_s - cur_s >= _AE_MIN_EXP_DELTA_S:
                return {"exposure_s": round(new_s, 4)}
        return None                       # at the ceiling on both axes

    # 4. Over-served — gain back down first (noise), shorten exposure only at
    #    the gain floor. But never shed signal at low contrast: an abundance of
    #    matches on a dim frame is one fluctuation from starving detection, so
    #    hold the operating point instead of walking off the cliff.
    if metric > _AE_SPARE_FRAC * target:
        if low_contrast:
            return None
        if cur_g > min_g:
            new_g = max(min_g, cur_g / _AE_GAIN_STEP)
            if new_g != cur_g:
                return {"gain": round(new_g, 2)}
        new_s = max(min_s, cur_s * _AE_EXP_DOWN)
        if cur_s - new_s >= _AE_MIN_EXP_DELTA_S:
            return {"exposure_s": round(new_s, 4)}
        return None

    # 5. Settled — walk exposure back toward nominal when gain can compensate,
    #    so a starved episode that stretched exposure doesn't leave us with
    #    permanent latency. The gain ladder restores brightness next cycle.
    #    Suppressed at low contrast: shortening exposure there would starve
    #    detection before the gain ladder gets a chance to recover.
    if (cur_s > nominal_s + _AE_MIN_EXP_DELTA_S and cur_g < max_g
            and not low_contrast):
        new_s = max(nominal_s, cur_s * _AE_EXP_DOWN)
        if cur_s - new_s >= _AE_MIN_EXP_DELTA_S:
            return {"exposure_s": round(new_s, 4)}
    if cur_s < nominal_s - _AE_MIN_EXP_DELTA_S and cur_g > min_g:
        new_s = min(nominal_s, cur_s * _AE_EXP_UP)
        if new_s - cur_s >= _AE_MIN_EXP_DELTA_S:
            return {"exposure_s": round(new_s, 4)}
    return None


def _ae_apply_raise_debounce(action, cur_s, cur_g, raise_streak):
    """Debounce brightness *raises* so AE doesn't chase a single transient dark
    frame (passing cloud / wind smear) right after a good solving run.

    A raise (gain-up or exposure-up) only takes effect once it has been the
    intended action for ``_AE_RAISE_DEBOUNCE`` consecutive cycles; until then it
    is held. Any non-raise action (reduce / hold) resets the streak. Reductions
    and the saturation backoff are NOT debounced — only the starved→raise path,
    which is the one that oscillates on transient dips. Once confirmed (streak
    past the threshold) it keeps raising every cycle, so a genuine cold-start
    ramp is delayed by at most one cycle, not slowed throughout.

    Returns ``(effective_action, new_raise_streak)``.
    """
    is_raise = bool(action) and (
        action.get("gain", cur_g) > cur_g
        or action.get("exposure_s", cur_s) > cur_s)
    if not is_raise:
        return action, 0
    raise_streak += 1
    if raise_streak < _AE_RAISE_DEBOUNCE:
        return None, raise_streak          # hold; wait for a confirming frame
    return action, raise_streak


def _auto_exposure_loop(ctx, interval_s=5.0):
    """Background controller: adjust exposure toward the target star count.

    Enabled live via shared_cfg['auto_exposure_enabled'] (auto_exposure_set
    maint command / Camera page toggle). Skips stale solutions so it never
    reacts to frames from before its own last adjustment.
    """
    cfg = ctx.cfg
    raise_streak = 0          # consecutive brightness-raise intents (debounce)
    while True:
        time.sleep(interval_s)
        try:
            if not ctx.shared_cfg.get("auto_exposure_enabled",
                                      cfg.auto_exposure_enabled):
                continue
            sol = dict(ctx.latest_solution)
            age = time.monotonic() - sol.get("epoch_monotonic", 0.0)
            if age > interval_s * 2:
                continue                  # no fresh detection data
            reply = _call_camera(CAMERA_OP_GET_EXPOSURE, {},
                                 ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
            if reply is None or not reply.ok:
                continue
            cur_s = float(reply.result.get("exposure_s", cfg.exposure_s))
            cur_g = float(reply.result.get("gain", cfg.gain))
            # target_stars/target_matches/max_s/max_gain are live-mutable (seeing
            # presets / UI write them to shared_cfg); read them fresh each cycle.
            # The exposure and gain floors stay config-only.
            target_stars = int(ctx.shared_cfg.get(
                "auto_exposure_target_stars", cfg.auto_exposure_target_stars))
            target_matches = int(ctx.shared_cfg.get(
                "auto_exposure_target_matches", cfg.auto_exposure_target_matches))
            max_s = float(ctx.shared_cfg.get(
                "auto_exposure_max_s", cfg.auto_exposure_max_s))
            max_g = float(ctx.shared_cfg.get(
                "auto_exposure_max_gain", cfg.auto_exposure_max_gain))
            peak_floor = float(ctx.shared_cfg.get(
                "auto_exposure_peak_floor",
                getattr(cfg, "auto_exposure_peak_floor", _AE_PEAK_FLOOR)))
            # Exposure anchor: the exposure the controller prefers to sit at and
            # trim around with gain. 0 -> use the configured/persisted exposure
            # (the last value a user or preset intentionally set; the loop never
            # persists its own moves, so cfg.exposure_s stays a stable anchor).
            nominal_cfg = float(ctx.shared_cfg.get(
                "auto_exposure_nominal_s",
                getattr(cfg, "auto_exposure_nominal_s", 0.0)))
            nominal_s = nominal_cfg if nominal_cfg > 0.0 else float(cfg.exposure_s)
            action = _auto_exposure_decision(
                solved=bool(sol.get("solved", False)),
                stars=sol.get("stars", 0), matches=sol.get("matches", 0),
                peak=sol.get("peak", 0), cur_s=cur_s, cur_g=cur_g,
                target_stars=target_stars, target_matches=target_matches,
                min_s=cfg.auto_exposure_min_s, max_s=max_s,
                min_g=cfg.auto_exposure_min_gain, max_g=max_g,
                nominal_s=nominal_s, peak_floor=peak_floor)
            # Debounce raises so a single transient dark frame doesn't bounce
            # brightness up (the cloud/wind oscillation). Reductions act at once.
            action, raise_streak = _ae_apply_raise_debounce(
                action, cur_s, cur_g, raise_streak)
            if not action:
                continue
            ctx_qs = (ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
            if "exposure_s" in action:
                reply = _call_camera(CAMERA_OP_SET_EXPOSURE,
                                     {"exposure_s": action["exposure_s"]}, *ctx_qs)
                if reply is not None and reply.ok:
                    log.info("auto-exposure: exp %.3fs -> %.3fs "
                             "(solved=%s stars=%s matches=%s peak=%s)",
                             cur_s, action["exposure_s"], sol.get("solved"),
                             sol.get("stars"), sol.get("matches"), sol.get("peak"))
            if "gain" in action:
                reply = _call_camera(CAMERA_OP_SET_GAIN,
                                     {"gain": action["gain"]}, *ctx_qs)
                if reply is not None and reply.ok:
                    log.info("auto-exposure: gain %.1f -> %.1f "
                             "(solved=%s stars=%s matches=%s peak=%s)",
                             cur_g, action["gain"], sol.get("solved"),
                             sol.get("stars"), sol.get("matches"), sol.get("peak"))
        except Exception as e:
            log.warning("auto-exposure step failed: %s", e)


# ----- Offline auto-tune sweep -----------------------------------------------
#
# A user-initiated, bounded coordinate search for the cheapest detection +
# photometric operating point that still clears the seeing-mode match target on
# the *current* sky. Runs in a background thread (the maint socket has a 15 s
# read timeout, far shorter than a multi-point sweep), reporting progress via
# auto_tune_status and abortable via auto_tune_cancel. This is NOT a live
# controller — see _auto_exposure_loop for the always-on single-axis loop.

# Default search axes. Only live-cheap detection knobs are swept; detect_bin
# (restart) and star_db (heavy reload) are deliberately excluded.
_AT_SIGMA_VALUES = (4.0, 5.0, 6.0, 8.0)
_AT_KERNEL_VALUES = (1.5, 2.5)
_AT_BG_MODES = ("row_percentile", "block_percentile")

# Merit weights (lower cost is better). Among candidates that clear the match
# target + rate floor, prefer faster solves, a tighter (cheaper) matched-filter
# kernel, a higher detection sigma (fewer false positives), and a cheaper
# background model — i.e. the "minimum kernel, maximum sigma" the user wants.
_AT_W_SOLVE = 1.0      # per second of median solve time
_AT_W_KERNEL = 0.30    # per unit kernel_sigma
_AT_W_SIGMA = 0.05     # per unit detection sigma (subtracted -> larger is cheaper)
# Background-model cost for every sweepable bg_mode (cheaper background = lower
# cost), roughly ordered by spatial work / per-frame expense. Modes not in the
# default sweep are scored too, so a user-supplied bg_modes list is ranked
# deliberately rather than via the fallback. NOTE: uniform_mean only matches its
# reference (tetra3/olive-solve) behaviour with noise_mode="global_rms", which
# auto_tune does not currently sweep — see the "future work" note in the
# Auto-exposure / gain controller docs.
_AT_BG_COST = {
    "row_percentile": 0.0,
    "line_median": 0.1,
    "column_percentile": 0.15,
    "block_percentile": 0.25,
    "row_column_percentile": 0.3,
    "uniform_mean": 0.35,
    "top_hat": 0.5,
}


def _auto_tune_cost(row):
    """Scalar cost for one evaluated candidate row (lower is better)."""
    return (
        _AT_W_SOLVE * (row.get("median_solve_ms", 0.0) / 1000.0)
        + _AT_W_KERNEL * float(row.get("kernel_sigma", 0.0))
        - _AT_W_SIGMA * float(row.get("sigma", 0.0))
        + _AT_BG_COST.get(row.get("bg_mode"), 0.3)
    )


def _auto_tune_select(rows, target_matches, match_rate_floor):
    """Pure selection step: pick the cheapest feasible candidate.

    A candidate is *feasible* when it clears the match-rate floor and reaches at
    least 80% of the target matched-star count. Among feasible rows the lowest
    ``_auto_tune_cost`` wins. If none are feasible, fall back to the row with the
    most mean matches (best effort) and report met=False.

    Returns ``(best_row, met_target)``; ``(None, False)`` for empty input.
    """
    if not rows:
        return None, False
    need = 0.8 * float(target_matches)
    feasible = [r for r in rows
                if r.get("match_rate", 0.0) >= match_rate_floor
                and r.get("mean_matches", 0.0) >= need]
    if feasible:
        return min(feasible, key=_auto_tune_cost), True
    return max(rows, key=lambda r: r.get("mean_matches", 0.0)), False


# Per-sample peak below which the frame had no usable signal (a dark / mid-
# exposure-change / starved grab). Matches the auto-tune precondition floor.
# Such a sample tells us nothing about a candidate's detection params, so it
# must NOT be averaged in as a 0-match failure — it is dropped, not scored.
_AT_SIGNAL_FLOOR = 20
# Extra grabs allowed to replace a no-signal sample before giving up on it.
_AT_MAX_SAMPLE_RETRY = 3


def _auto_tune_valid_samples(samples, signal_floor=_AT_SIGNAL_FLOOR):
    """Keep only signal-bearing samples (peak >= floor).

    A no-signal frame is a capture artifact (contention, a frame caught mid
    exposure/gain change, or a momentarily starved sky), not evidence about the
    candidate's detection params. Crucially, a sample that HAS signal but failed
    to solve (good peak, matches==0) is kept — that is a real vote against the
    candidate.
    """
    return [s for s in samples
            if int(s.get("peak", 0) or 0) >= signal_floor]


def _auto_tune_row(candidate, samples, signal_floor=_AT_SIGNAL_FLOOR):
    """Aggregate a candidate's samples into a merit row over only the
    signal-bearing samples. Returns ``None`` when none are valid — the candidate
    is then *indeterminate* and excluded from selection, never scored 0.
    """
    valid = _auto_tune_valid_samples(samples, signal_floor)
    if not valid:
        return None
    n_solved = sum(1 for s in valid if s.get("solved"))
    solved_ms = [s["solve_ms"] for s in valid if s.get("solved")]
    return {
        **candidate,
        "n_frames": len(valid),
        "n_dropped": len(samples) - len(valid),
        "match_rate": round(n_solved / len(valid), 3),
        "mean_matches": round(
            sum(s.get("matches", 0) for s in valid) / len(valid), 2),
        "mean_stars": round(
            sum(s.get("stars", 0) for s in valid) / len(valid), 1),
        "median_solve_ms": round(statistics.median(solved_ms), 1)
        if solved_ms else 0.0,
        "max_peak": max(s.get("peak", 0) for s in valid),
    }


_auto_tune_lock = threading.Lock()
_auto_tune_state = {
    "running": False, "phase": "idle", "message": "",
    "progress": 0.0, "result": None, "error": None,
    "cancel": False, "started_at": 0.0,
}


def _at_set(**kw):
    with _auto_tune_lock:
        _auto_tune_state.update(kw)


def _at_snapshot():
    with _auto_tune_lock:
        return dict(_auto_tune_state)


def _at_cancelled():
    with _auto_tune_lock:
        return _auto_tune_state["cancel"]


def _wait_fresh_solution(ctx, after_monotonic, timeout_s):
    """Return the first latest_solution published strictly after
    ``after_monotonic``, or the latest available once ``timeout_s`` elapses."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        sol = dict(ctx.latest_solution)
        if sol.get("epoch_monotonic", 0.0) > after_monotonic:
            return sol
        time.sleep(0.2)
    return dict(ctx.latest_solution)


def _auto_tune_photometric(ctx, *, target_stars, target_matches,
                           max_s, max_g, deadline, max_iters=6):
    """Drive exposure+gain to a good signal level using the live pipeline and
    the same ladder logic as the always-on controller. Returns
    ``(exposure_s, gain)`` — the converged point (or the current one if already
    settled / out of budget)."""
    cfg = ctx.cfg
    cur_s = cur_g = None
    for _ in range(max_iters):
        if _at_cancelled() or time.monotonic() > deadline:
            break
        reply = _call_camera(CAMERA_OP_GET_EXPOSURE, {},
                             ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
        if reply is None or not reply.ok:
            break
        cur_s = float(reply.result.get("exposure_s", cfg.exposure_s))
        cur_g = float(reply.result.get("gain", cfg.gain))
        sol = dict(ctx.latest_solution)
        action = _auto_exposure_decision(
            solved=bool(sol.get("solved", False)),
            stars=sol.get("stars", 0), matches=sol.get("matches", 0),
            peak=sol.get("peak", 0), cur_s=cur_s, cur_g=cur_g,
            target_stars=target_stars, target_matches=target_matches,
            min_s=cfg.auto_exposure_min_s, max_s=max_s,
            min_g=cfg.auto_exposure_min_gain, max_g=max_g,
            peak_floor=float(ctx.shared_cfg.get(
                "auto_exposure_peak_floor",
                getattr(cfg, "auto_exposure_peak_floor", _AE_PEAK_FLOOR))))
        if not action:
            break                              # settled
        t_set = time.monotonic()
        if "exposure_s" in action:
            _call_camera(CAMERA_OP_SET_EXPOSURE, {"exposure_s": action["exposure_s"]},
                         ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
            cur_s = action["exposure_s"]
        if "gain" in action:
            _call_camera(CAMERA_OP_SET_GAIN, {"gain": action["gain"]},
                         ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
            cur_g = action["gain"]
        # Let the new setting flush through a couple of frames before re-reading.
        _wait_fresh_solution(ctx, t_set, timeout_s=max(1.0, 3.0 * cur_s))
        time.sleep(max(0.3, cur_s))
    return cur_s, cur_g


def _auto_tune_run(ctx, params):
    """Background worker: photometric phase -> detection sweep -> select ->
    commit/restore. Updates _auto_tune_state throughout; restores the camera on
    cancel / no-commit / error."""
    cfg = ctx.cfg
    started = time.monotonic()
    deadline = started + params["time_budget_s"]
    snap = _call_camera(CAMERA_OP_GET_EXPOSURE, {},
                        ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
    snap_s = (float(snap.result.get("exposure_s", cfg.exposure_s))
              if (snap and snap.ok) else cfg.exposure_s)
    snap_g = (float(snap.result.get("gain", cfg.gain))
              if (snap and snap.ok) else cfg.gain)
    # Pause the always-on auto-exposure loop so it doesn't fight the sweep.
    ae_was = bool(ctx.shared_cfg.get("auto_exposure_enabled",
                                     cfg.auto_exposure_enabled))
    ctx.shared_cfg["auto_exposure_enabled"] = False

    def _restore_camera():
        _call_camera(CAMERA_OP_SET_EXPOSURE, {"exposure_s": snap_s},
                     ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
        _call_camera(CAMERA_OP_SET_GAIN, {"gain": snap_g},
                     ctx.camera_cmd_q, ctx.camera_cmd_reply_q)

    try:
        mode = params["mode"]
        preset = seeing_mod.SEEING_PRESETS.get(mode, {})
        target_matches = int(preset.get("auto_exposure_target_matches",
                                        cfg.auto_exposure_target_matches))
        target_stars = int(preset.get("auto_exposure_target_stars",
                                      cfg.auto_exposure_target_stars))
        max_s = float(preset.get("auto_exposure_max_s", cfg.auto_exposure_max_s))
        max_g = float(preset.get("auto_exposure_max_gain", cfg.auto_exposure_max_gain))

        # --- Phase 1: photometric (exposure + gain) ----------------------
        _at_set(phase="photometric", message="tuning exposure / gain",
                progress=0.05)
        exp_s, gain = _auto_tune_photometric(
            ctx, target_stars=target_stars, target_matches=target_matches,
            max_s=max_s, max_g=max_g, deadline=deadline)
        if exp_s is None:
            exp_s, gain = snap_s, snap_g

        # --- Phase 2: detection sweep ------------------------------------
        # uniform_mean only matches its reference (tetra3/olive-solve) pipeline
        # when paired with the global-RMS noise estimator, so sweep it as that
        # pair; every other mode keeps the robust MAD default. The eval op
        # accepts noise_mode per sample, so candidates carry it explicitly and
        # the winner's pairing is committed alongside its bg_mode.
        def _cand(bg, ks, sg):
            c = {"bg_mode": bg, "kernel_sigma": ks, "sigma": sg}
            if bg == "uniform_mean":
                c["noise_mode"] = "global_rms"
            return c

        candidates = [
            _cand(bg, ks, sg)
            for bg in params["bg_modes"]
            for ks in params["kernel_values"]
            for sg in params["sigma_values"]
        ]
        rows = []
        n = len(candidates)
        eval_timeout = params["eval_solve_timeout_ms"]
        settle_s = max(0.25, exp_s or cfg.exposure_s)
        signal_floor = int(params.get("signal_floor", _AT_SIGNAL_FLOOR))

        def _eval_once(c):
            reply = _call_solver(
                SOLVER_OP_AUTO_TUNE_EVAL,
                {**c, "solve_timeout_ms": eval_timeout},
                ctx.solver_cmd_q, ctx.solver_cmd_reply_q,
                timeout_s=eval_timeout / 1000.0 + 3.0)
            if reply is not None and reply.ok and reply.result:
                return reply.result
            return None

        # Settle the camera once after the photometric phase so the first grabs
        # aren't captured mid exposure/gain change.
        time.sleep(settle_s)
        for i, c in enumerate(candidates):
            if _at_cancelled():
                _at_set(message="cancelled")
                break
            if time.monotonic() > deadline:
                _at_set(message="time budget reached; stopping sweep early")
                break
            _at_set(phase="sweep",
                    message=(f"candidate {i + 1}/{n}: bg={c['bg_mode']} "
                             f"k={c['kernel_sigma']} sigma={c['sigma']}"),
                    progress=0.1 + 0.85 * (i / max(1, n)))
            samples = []
            for f in range(params["frames_per_point"]):
                if _at_cancelled() or time.monotonic() > deadline:
                    break
                # Get a SIGNAL-bearing sample; skip transient no-signal grabs so
                # they don't count as a 0-match failure against this candidate.
                s = None
                for _attempt in range(_AT_MAX_SAMPLE_RETRY):
                    if _at_cancelled() or time.monotonic() > deadline:
                        break
                    r = _eval_once(c)
                    if r is not None and int(r.get("peak", 0) or 0) >= signal_floor:
                        s = r
                        break
                    time.sleep(settle_s)   # let a fresh frame arrive, then retry
                if s is not None:
                    samples.append(s)
                if f < params["frames_per_point"] - 1:
                    time.sleep(settle_s)
            row = _auto_tune_row(c, samples, signal_floor)
            if row is not None:
                rows.append(row)

        best, met = _auto_tune_select(
            rows, target_matches, params["match_rate_floor"])

        result = {
            "mode": mode, "target_matches": target_matches,
            "met_target": met, "committed": False,
            "photometric": {
                "exposure_s": round(exp_s, 4) if exp_s else None,
                "gain": round(gain, 2) if gain else None},
            "detection": ({"sigma": best["sigma"],
                           "kernel_sigma": best["kernel_sigma"],
                           "bg_mode": best["bg_mode"]} if best else None),
            "best": best, "table": rows,
            "elapsed_s": round(time.monotonic() - started, 1),
            "cancelled": _at_cancelled(),
        }

        # --- Commit or restore -------------------------------------------
        if params["commit"] and best is not None and not _at_cancelled():
            updates = {
                "detect_sigma": float(best["sigma"]),
                "detect_kernel_sigma": float(best["kernel_sigma"]),
                "detect_bg_mode": str(best["bg_mode"]),
                "exposure_s": float(exp_s),
                "gain": float(gain),
            }
            # A winner swept as a (bg_mode, noise_mode) pair (uniform_mean +
            # global_rms) must be committed as that pair.
            if best.get("noise_mode"):
                updates["detect_noise_mode"] = str(best["noise_mode"])
            for k in ("detect_sigma", "detect_kernel_sigma", "detect_bg_mode",
                      "detect_noise_mode"):
                if k in updates:
                    ctx.shared_cfg[k] = updates[k]
            ctx.cfg.detect_sigma = updates["detect_sigma"]
            ctx.cfg.detect_kernel_sigma = updates["detect_kernel_sigma"]
            ctx.cfg.detect_bg_mode = updates["detect_bg_mode"]
            if "detect_noise_mode" in updates:
                ctx.cfg.detect_noise_mode = updates["detect_noise_mode"]
            ctx.cfg.exposure_s = updates["exposure_s"]
            ctx.cfg.gain = updates["gain"]
            try:
                cfg_mod.save_keys(updates)
            except Exception as e:
                log.warning("auto-tune could not persist: %s", e)
            # Save a labelled, reversible override artifact for this mode (the
            # factory preset stays untouched). The values applied live above
            # now also match the override -> lineage reads "tuned".
            try:
                entry = seeing_mod.save_override(mode, dict(updates),
                                                 source="auto_tune")
                result["override"] = entry
            except Exception as e:
                log.warning("auto-tune could not save override: %s", e)
            _invalidate_solver_cache()
            result["committed"] = True
            log.info("auto-tune committed (override saved): %s", updates)
        else:
            # Detection params were never changed live (the eval op took them by
            # argument); only the camera needs restoring.
            _restore_camera()

        _at_set(phase="done", message="complete", progress=1.0,
                result=result, error=None)
        log.info("auto-tune done: met=%s committed=%s elapsed=%.0fs",
                 met, result["committed"], result["elapsed_s"])
    except Exception as e:
        log.warning("auto-tune failed: %s", e)
        try:
            _restore_camera()
        except Exception:
            pass
        _at_set(phase="error", message=str(e),
                error=f"{type(e).__name__}: {e}")
    finally:
        ctx.shared_cfg["auto_exposure_enabled"] = ae_was
        _at_set(running=False)


def _watchdog_loop(ctx, interval_s=5.0):
    """Solver-hang watchdog.

    The solver publishes latest_solution["epoch_monotonic"] on every frame,
    including dark frames (peak < 20). If that epoch stops advancing for longer
    than cfg.watchdog_timeout_s, the solver loop is hung; log CRITICAL and
    os._exit(1) so systemd restarts the whole unit.

    Arming: we wait for the FIRST non-zero epoch (the solver hasn't published
    anything at startup) before we start enforcing staleness, so a slow boot or
    a long first database load never trips the watchdog.
    """
    cfg = ctx.cfg
    if not getattr(cfg, "watchdog_enabled", True):
        log.info("Solver watchdog disabled by config")
        return
    timeout_s = float(getattr(cfg, "watchdog_timeout_s", 30.0))
    armed = False
    last_epoch = 0.0
    last_change_mono = time.monotonic()
    log.info("Solver watchdog armed (timeout=%.0fs)", timeout_s)
    while True:
        time.sleep(interval_s)
        try:
            epoch = float(ctx.latest_solution.get("epoch_monotonic", 0.0) or 0.0)
        except Exception as e:
            log.warning("watchdog read failed: %s", e)
            continue
        now = time.monotonic()
        if not armed:
            if epoch > 0.0:
                armed = True
                last_epoch = epoch
                last_change_mono = now
            continue
        if epoch != last_epoch:
            last_epoch = epoch
            last_change_mono = now
            continue
        stale_s = now - last_change_mono
        if stale_s > timeout_s:
            log.critical(
                "Solver watchdog: no new solution for %.0fs (> %.0fs timeout); "
                "solver appears hung. Exiting so systemd restarts the unit.",
                stale_s, timeout_s)
            os._exit(1)


def _do_alignment(align_state, cfg, shared_cfg,
                  align_request_q, align_response_q):
    """Execute :CM# alignment: send target to solver, wait for result, persist boresight."""
    req = align_state.build_request()
    if req is None:
        return "no align target#"

    try:
        while True:
            align_response_q.get_nowait()
    except Empty:
        pass

    align_request_q.put(req)
    log.info("Alignment requested: RA=%.4f Dec=%.4f",
             req.target_ra_deg, req.target_dec_deg)

    deadline = time.monotonic() + CommsAlignState.DEFAULT_TIMEOUT_S
    result = None
    while time.monotonic() < deadline:
        try:
            candidate = align_response_q.get(timeout=0.5)
            if candidate.completed_at >= req.requested_at:
                result = candidate; break
        except Empty:
            continue

    align_state.reset()

    if result is None:
        log.warning("Alignment timed out waiting for solver")
        return "align timeout#"
    if not result.success:
        log.warning("Alignment failed: %s", result.error_message)
        return f"align fail: {result.error_message}#"

    cfg.boresight_y = result.boresight_y
    cfg.boresight_x = result.boresight_x
    shared_cfg["boresight_y"] = result.boresight_y
    shared_cfg["boresight_x"] = result.boresight_x

    try:
        cfg_mod.save_keys({
            "boresight_y": result.boresight_y,
            "boresight_x": result.boresight_x,
        })
    except Exception as e:
        log.warning("Could not persist boresight: %s", e)

    log.info("Alignment complete: boresight=(%.2f, %.2f)",
             result.boresight_y, result.boresight_x)
    return "M31 EX GAL MAG 3.5 SZ178.0'#"


# Rotation-RATE motion gate for the IMU pointing prediction. The IMU runs
# IMUPLUS (no magnetometer), so a parked scope still shows slow gyro heading
# drift; projected to the sky that walks the crosshair ~0.5 deg off a
# stationary mount between solves. The old displacement-since-solve gate cured
# that but froze the crosshair for SLOW pans too (each solve re-anchors the
# reference, so sub-gate motion never engaged). Rate distinguishes the two
# directly: parked drift is ~0.01 deg/s while even a slow manual pan is
# >0.1 deg/s. Rate is measured over a >= _IMU_RATE_BASELINE_S window of the
# samples observed at :GR/:GD poll time (quantization noise over a shorter
# window would swamp slow pans). Once engaged, the prediction HOLDS until a
# fresh solve re-anchors the reference after motion stops — otherwise the
# report would jump back by the whole slew distance at the moment you stop.
# Live-tunable via shared_cfg["imu_rate_gate_dps"].
_IMU_RATE_GATE_DPS = 0.1
_IMU_RATE_BASELINE_S = 0.8
_imu_rate_state = {}     # samples: deque[(imu_t, quat)], engaged, moving_t


def _imu_motion_engaged(state, q_now, imu_t, ref_t,
                        rate_gate_dps=_IMU_RATE_GATE_DPS,
                        baseline_s=_IMU_RATE_BASELINE_S):
    """Rate-based stationary detector for the pointing prediction.

    Feeds (imu_t, quat) samples into ``state`` and returns True while the
    device is judged to be moving (or has moved and no solve has re-anchored
    the reference yet). Pure on its inputs — unit-tested without hardware.
    """
    samples = state.setdefault("samples", deque())
    if not samples or imu_t > samples[-1][0]:
        samples.append((imu_t, tuple(q_now)))
    while samples and imu_t - samples[0][0] > 4.0 * baseline_s:
        samples.popleft()
    # Newest sample at least baseline_s older than now: long enough that real
    # slow motion clears quaternion quantization noise.
    rate = None
    for t_old, q_old in reversed(samples):
        if imu_t - t_old >= baseline_s:
            r = quat_delta_rotvec(q_now, q_old)
            ang = math.degrees(
                math.sqrt(r[0] * r[0] + r[1] * r[1] + r[2] * r[2]))
            rate = ang / (imu_t - t_old)
            break
    if rate is not None and rate >= rate_gate_dps:
        state["engaged"] = True
        state["moving_t"] = imu_t
    elif (state.get("engaged") and rate is not None
            and ref_t and ref_t > state.get("moving_t", 0.0)):
        # Motion has stopped AND a solve landed afterwards: the frozen solved
        # position now equals where we are — disengage seamlessly.
        state["engaged"] = False
    return bool(state.get("engaged"))


def _imu_predict(shared_cfg):
    """Return (ra_deg, dec_deg) predicted from IMU rotation since last solve, or None if unavailable."""
    if not shared_cfg.get("imu_available", False):
        return None
    if shared_cfg.get("imu_calib_n", 0) < 3:
        return None
    if shared_cfg.get("imu_calib_quality", 0.0) < 0.85:
        return None
    q_now  = shared_cfg.get("imu_q")
    imu_t  = shared_cfg.get("imu_t", 0.0)
    if q_now is None or time.monotonic() - imu_t > 2.0:
        return None
    q_ref   = shared_cfg.get("imu_ref_q")
    ra_ref  = shared_cfg.get("imu_ref_ra_deg")
    dec_ref = shared_cfg.get("imu_ref_dec_deg")
    ref_t   = shared_cfg.get("imu_ref_t", 0.0)
    if q_ref is None or ra_ref is None or time.monotonic() - ref_t > 120.0:
        return None
    C_flat = shared_cfg.get("imu_calib_C")
    if C_flat is None or len(C_flat) != 6:
        return None
    # Rotation-rate motion gate: parked = report the solved position (None);
    # moving (or moved with no re-anchoring solve yet) = predict. See
    # _imu_motion_engaged for the full rationale.
    rate_gate = float(shared_cfg.get("imu_rate_gate_dps", _IMU_RATE_GATE_DPS))
    with _imu_filt_lock:
        if not _imu_motion_engaged(_imu_rate_state, q_now, imu_t, ref_t,
                                   rate_gate_dps=rate_gate):
            return None
    r = quat_delta_rotvec(q_now, q_ref)
    c = C_flat
    dr = c[0]*r[0] + c[1]*r[1] + c[2]*r[2]
    du = c[3]*r[0] + c[4]*r[1] + c[5]*r[2]
    roll_ref = shared_cfg.get("imu_ref_roll_deg", 0.0)
    roll_rad = math.radians(roll_ref)
    cos_r, sin_r = math.cos(roll_rad), math.sin(roll_rad)
    dra_rad  =  dr * cos_r - du * sin_r
    ddec_rad =  dr * sin_r + du * cos_r
    if abs(dra_rad) > math.radians(5.0) or abs(ddec_rad) > math.radians(5.0):
        return None
    cos_dec = math.cos(math.radians(dec_ref))
    if abs(cos_dec) < 0.01:
        return None
    ra_pred  = (ra_ref + math.degrees(dra_rad) / cos_dec) % 360.0
    dec_pred = max(-90.0, min(90.0, dec_ref + math.degrees(ddec_rad)))
    return ra_pred, dec_pred


# Alpha-beta tracker for the IMU-predicted pointing reported to LX200 clients.
# The raw _imu_predict reads the live 20 Hz BNO055 quaternion, so a planetarium
# app polling :GR#/:GD# a few times a second samples sensor noise directly and
# its crosshair jitters even when the scope is parked. We smooth the IMU *rate*
# (not the position) so a parked scope is steady (velocity -> 0) while a real
# slew is still tracked without lag, and re-anchor to every fresh plate solve so
# solves stay authoritative. alpha=0.25 averages ~4 samples (~0.2 s) at 20 Hz.
_IMU_AB_ALPHA = 0.25
_IMU_AB_BETA = 0.05
_IMU_AB_RESET_GAP_S = 1.0          # IMU-sample gap that forces a re-snap
_imu_filt_lock = threading.Lock()
_imu_filt_state = {}               # ra, dec, vra, vdec, t (imu_t), ref_t


def _imu_predict_smoothed(shared_cfg):
    """IMU-predicted (ra_deg, dec_deg) with an alpha-beta rate filter applied.

    Wraps _imu_predict to remove the per-poll jitter SkySafari sees while still
    tracking genuine motion. Returns None whenever the raw prediction is
    unavailable, so the LX200 caller falls back to the last solved position.
    """
    z = _imu_predict(shared_cfg)
    with _imu_filt_lock:
        st = _imu_filt_state
        if z is None:
            st.clear()             # re-snap on the next valid sample
            return None
        imu_t = shared_cfg.get("imu_t", 0.0)
        ref_t = shared_cfg.get("imu_ref_t", 0.0)
        # Snap (no smoothing) on first sample, a fresh solve anchor, a stale
        # gap, or a non-increasing timestamp. Keeps plate solves authoritative
        # and avoids smoothing across a coordinate re-baseline.
        if (not st or st.get("ref_t") != ref_t
                or imu_t - st.get("t", imu_t) > _IMU_AB_RESET_GAP_S
                or imu_t < st.get("t", imu_t)):
            st.clear()
            st.update(ra=z[0], dec=z[1], vra=0.0, vdec=0.0, t=imu_t, ref_t=ref_t)
            return z[0], z[1]
        # Same IMU sample (e.g. :GR# then :GD# in one poll): return the cached
        # value so both coordinates agree and the filter steps once per sample.
        if imu_t == st["t"]:
            return st["ra"], st["dec"]
        dt = imu_t - st["t"]
        ra, dec, vra, vdec = alpha_beta_step(
            (st["ra"], st["dec"], st["vra"], st["vdec"]),
            z[0], z[1], dt, _IMU_AB_ALPHA, _IMU_AB_BETA)
        st.update(ra=ra, dec=dec, vra=vra, vdec=vdec, t=imu_t)
        return ra, dec


def _sync_clock(sl, sg, sc):
    """Set system clock from SkySafari's :SG/:SL/:SC local-time + UTC-offset sequence."""
    try:
        local_dt = datetime.datetime.strptime(f"{sc} {sl}", "%m/%d/%y %H:%M:%S")
        sg_hours = float(sg)
        utc_dt   = local_dt + datetime.timedelta(hours=sg_hours)
        time_str = utc_dt.strftime("%Y-%m-%d %H:%M:%S")
        now_utc  = datetime.datetime.utcnow()
        diff_s   = abs((utc_dt - now_utc).total_seconds())
        if diff_s < 10:
            log.debug("Clock already accurate (drift %.0fs); skipping", diff_s)
            return
        log.info("Clock drift %.0fs — syncing from SkySafari: %s UTC",
                 diff_s, time_str)
        result = subprocess.run(
            ["sudo", "/usr/local/bin/diofinder-set-time", time_str],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            log.info("Clock synced to %s UTC", time_str)
        else:
            log.warning("Clock sync failed (rc=%d): %s",
                        result.returncode, result.stderr.strip())
    except Exception as e:
        log.warning("Clock sync error: %s", e)


def _handle_lx200_command(cmd, latest_solution, align_state, time_state,
                          cfg, shared_cfg,
                          align_request_q, align_response_q, ctx=None):
    """Dispatch one LX200 command string and return the raw bytes reply."""
    if cmd == ":GR":
        pred = _imu_predict_smoothed(shared_cfg)
        if pred is not None:
            return _format_ra(pred[0] / 15.0).encode("ascii")
        sol = dict(latest_solution)
        return _format_ra(sol.get("ra_deg", 0.0) / 15.0).encode("ascii")
    if cmd == ":GD":
        pred = _imu_predict_smoothed(shared_cfg)
        if pred is not None:
            return _format_dec(pred[1]).encode("ascii")
        sol = dict(latest_solution)
        return _format_dec(sol.get("dec_deg", 0.0)).encode("ascii")
    if cmd == ":GW":
        return b"AT2#"
    if cmd in (":GVN", ":GVP"):
        return f"diofinder {cfg.version}#".encode("ascii")
    if cmd.startswith(":GV"):
        return f"diofinder {cfg.version}#".encode("ascii")
    if cmd.startswith(":Sr"):
        ok = align_state.set_target_ra(cmd[3:])
        return b"1" if ok else b"0"
    if cmd.startswith(":Sd"):
        ok = align_state.set_target_dec(cmd[3:])
        return b"1" if ok else b"0"
    if cmd == ":CM":
        reply = _do_alignment(align_state, cfg, shared_cfg,
                              align_request_q, align_response_q)
        return reply.encode("ascii")
    if cmd.startswith(":St"):
        try:
            from diofinder.align import _parse_dec_dms
            lat = _parse_dec_dms(cmd[3:])
            cfg.latitude_deg = lat
            cfg_mod.save_keys({"latitude_deg": lat})
            def _push_lat(l=lat):
                _call_solver(SOLVER_OP_POLAR_SET_LATITUDE,
                             {"latitude_deg": l},
                             ctx.solver_cmd_q, ctx.solver_cmd_reply_q,
                             timeout_s=10.0)
            threading.Thread(target=_push_lat, daemon=True).start()
            log.info("Latitude from :St -> %.4f", lat)
            return b"1"
        except Exception as e:
            log.warning("Could not parse :St latitude %r: %s", cmd[3:], e)
            return b"0"
    if cmd.startswith(":Sg"):
        try:
            from diofinder.align import _parse_dec_dms
            lon = _parse_dec_dms(cmd[3:])
            cfg.longitude_deg = lon
            cfg_mod.save_keys({"longitude_deg": lon})
            log.info("Longitude from :Sg -> %.4f", lon)
            return b"1"
        except Exception as e:
            log.warning("Could not parse :Sg longitude %r: %s", cmd[3:], e)
            return b"0"
    if cmd == ":Gt":
        lat = cfg.latitude_deg or 0.0
        sign = "+" if lat >= 0 else "-"
        a = abs(lat); d = int(a); m = int(round((a - d) * 60.0))
        return f"{sign}{d:02d}*{m:02d}#".encode("ascii")
    if cmd == ":Gg":
        lon = cfg.longitude_deg or 0.0
        sign = "+" if lon >= 0 else "-"
        a = abs(lon); d = int(a); m = int(round((a - d) * 60.0))
        return f"{sign}{d:03d}*{m:02d}#".encode("ascii")
    if cmd.startswith(":SG"):
        try:
            time_state["sg"] = float(cmd[3:])
        except ValueError:
            pass
        return b"1"
    if cmd.startswith(":SL"):
        time_state["sl"] = cmd[3:].strip()
        return b"1"
    if cmd.startswith(":SC"):
        sc_val = cmd[3:].strip()
        sl_val = time_state.get("sl")
        sg_val = time_state.get("sg")
        if sl_val and sg_val is not None:
            threading.Thread(
                target=_sync_clock, args=(sl_val, sg_val, sc_val),
                daemon=True, name="diofinder-timesync",
            ).start()
        return b"1Updating Planetary Data#                              #"
    if cmd == ":MS":
        return b"0"
    if cmd.startswith(":M") or cmd.startswith(":R") or cmd == ":Q":
        return b""
    if cmd == ":GT":
        return b"60.0#"
    if cmd == ":Gr":
        return b"4#"
    if cmd in (":GS", ":GL"):
        return b"00:00:00#"
    if cmd == ":GC":
        return b"01/01/00#"
    if cmd == ":GG":
        return b"+00#"
    if cmd in (":GA", ":GZ"):
        return b"+00*00#"
    log.debug("Unhandled LX200 command: %r", cmd)
    return b"#"


def _handle_maint_command(req: MaintRequest, ctx) -> MaintResponse:
    """Dispatch one maintenance-socket command and return a MaintResponse."""
    cmd  = req.cmd
    args = req.args

    try:
        if cmd == "ping":
            return MaintResponse(ok=True, result={"pong": True})

        if cmd == "version":
            # Resolution order: OTA-written release file -> git describe of the
            # /opt/diofinder checkout -> the in-code config default. The git
            # fallback is what makes a freshly burned image (no OTA file yet)
            # report its real tag/commit instead of the stale code default.
            tag, released_at = _release_info()
            git_desc = _git_describe()
            result = {
                "version": tag or git_desc or ctx.cfg.version,
                "code_version": ctx.cfg.version,
            }
            if tag:
                result["release"] = tag
            if released_at:
                result["released_at"] = released_at
            if git_desc:
                result["git"] = git_desc
            return MaintResponse(ok=True, result=result)

        if cmd == "status":
            sol         = dict(ctx.latest_solution)
            imu_n       = ctx.shared_cfg.get("imu_calib_n", 0)
            imu_quality = ctx.shared_cfg.get("imu_calib_quality", 0.0)
            imu_avail   = ctx.shared_cfg.get("imu_available", False)
            imu_active  = imu_avail and imu_n >= 3 and imu_quality >= 0.85
            return MaintResponse(ok=True, result={
                "solution":  sol,
                "boresight": {
                    "y": ctx.shared_cfg.get("boresight_y", ctx.cfg.boresight_y),
                    "x": ctx.shared_cfg.get("boresight_x", ctx.cfg.boresight_x),
                },
                "fov_deg":        ctx.shared_cfg.get("fov_deg", ctx.cfg.fov_deg),
                "config_summary": ctx.cfg.summary(),
                "imu": {
                    "available": imu_avail,
                    "calib_n":   imu_n,
                    "quality":   round(imu_quality, 3),
                    "active":    imu_active,
                    # Raw IMU output + post-solve reference, so a debug bundle
                    # records the actual attitude reading at capture time, not
                    # just whether the IMU is active.
                    "q":            ctx.shared_cfg.get("imu_q"),
                    "t":            ctx.shared_cfg.get("imu_t", 0.0),
                    "age_s":        (round(time.monotonic()
                                           - ctx.shared_cfg.get("imu_t", 0.0), 3)
                                     if ctx.shared_cfg.get("imu_t") else None),
                    "ref_q":        ctx.shared_cfg.get("imu_ref_q"),
                    "ref_ra_deg":   ctx.shared_cfg.get("imu_ref_ra_deg"),
                    "ref_dec_deg":  ctx.shared_cfg.get("imu_ref_dec_deg"),
                    "ref_roll_deg": ctx.shared_cfg.get("imu_ref_roll_deg"),
                },
                "solver_backend": "sycamore",
                "test_mode":      ctx.shared_cfg.get("test_mode", False),
            })

        if cmd == "boresight_show":
            return MaintResponse(ok=True, result={
                "y": ctx.shared_cfg.get("boresight_y", ctx.cfg.boresight_y),
                "x": ctx.shared_cfg.get("boresight_x", ctx.cfg.boresight_x),
            })

        if cmd == "boresight_center":
            new_y = ctx.cfg.frame_height / 2.0
            new_x = ctx.cfg.frame_width  / 2.0
            ctx.cfg.boresight_y = new_y
            ctx.cfg.boresight_x = new_x
            ctx.shared_cfg["boresight_y"] = new_y
            ctx.shared_cfg["boresight_x"] = new_x
            cfg_mod.save_keys({"boresight_y": new_y, "boresight_x": new_x})
            return MaintResponse(ok=True, result={"y": new_y, "x": new_x})

        if cmd == "boresight_set":
            try:
                new_y = float(args["y"]); new_x = float(args["x"])
            except (KeyError, ValueError, TypeError) as e:
                return MaintResponse(ok=False,
                                     error=f"boresight_set requires y, x: {e}")
            if not (0 <= new_y <= ctx.cfg.frame_height) or \
               not (0 <= new_x <= ctx.cfg.frame_width):
                return MaintResponse(ok=False, error="y/x outside frame bounds")
            ctx.cfg.boresight_y = new_y
            ctx.cfg.boresight_x = new_x
            ctx.shared_cfg["boresight_y"] = new_y
            ctx.shared_cfg["boresight_x"] = new_x
            cfg_mod.save_keys({"boresight_y": new_y, "boresight_x": new_x})
            return MaintResponse(ok=True, result={"y": new_y, "x": new_x})

        if cmd == "calibration_status":
            reply = _cached_call_solver(
                SOLVER_OP_CALIBRATION_STATUS,
                ctx.solver_cmd_q, ctx.solver_cmd_reply_q, ttl_s=5.0)
            if reply is None:
                return MaintResponse(ok=False, error="solver did not respond")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            return MaintResponse(ok=True, result=reply.result)

        if cmd == "calibration_reset":
            _invalidate_solver_cache(SOLVER_OP_CALIBRATION_STATUS)
            reply = _call_solver(SOLVER_OP_CALIBRATION_RESET, {},
                                 ctx.solver_cmd_q, ctx.solver_cmd_reply_q)
            if reply is None:
                return MaintResponse(ok=False, error="solver did not respond")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            return MaintResponse(ok=True, result=reply.result)

        if cmd == "polar_start":
            _invalidate_solver_cache(SOLVER_OP_POLAR_STATUS)
            reply = _call_solver(SOLVER_OP_POLAR_START, {},
                                 ctx.solver_cmd_q, ctx.solver_cmd_reply_q)
            if reply is None:
                return MaintResponse(ok=False, error="solver did not respond")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            return MaintResponse(ok=True, result=reply.result)

        if cmd == "polar_status":
            reply = _cached_call_solver(
                SOLVER_OP_POLAR_STATUS,
                ctx.solver_cmd_q, ctx.solver_cmd_reply_q, ttl_s=1.0)
            if reply is None:
                return MaintResponse(ok=False, error="solver did not respond")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            result = reply.result or {}
            if not result.get("latitude_deg") and ctx.cfg.latitude_deg:
                result = {**result, "latitude_deg": ctx.cfg.latitude_deg}
            return MaintResponse(ok=True, result=result)

        if cmd == "polar_cancel":
            _invalidate_solver_cache(SOLVER_OP_POLAR_STATUS)
            reply = _call_solver(SOLVER_OP_POLAR_CANCEL, {},
                                 ctx.solver_cmd_q, ctx.solver_cmd_reply_q)
            if reply is None:
                return MaintResponse(ok=False, error="solver did not respond")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            return MaintResponse(ok=True, result=reply.result)

        if cmd == "polar_set_latitude":
            try:
                lat = float(args["latitude_deg"])
            except (KeyError, ValueError, TypeError) as e:
                return MaintResponse(ok=False,
                                     error=f"requires numeric latitude_deg: {e}")
            persist = bool(args.get("persist", True))
            _invalidate_solver_cache(SOLVER_OP_POLAR_STATUS)
            reply = _call_solver(SOLVER_OP_POLAR_SET_LATITUDE,
                                 {"latitude_deg": lat},
                                 ctx.solver_cmd_q, ctx.solver_cmd_reply_q)
            if reply is None:
                return MaintResponse(ok=False, error="solver did not respond")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            ctx.cfg.latitude_deg = lat
            if persist:
                cfg_mod.save_keys({"latitude_deg": lat})
            return MaintResponse(ok=True, result={**reply.result,
                                                  "persisted": persist})

        if cmd == "exposure_get":
            reply = _call_camera(CAMERA_OP_GET_EXPOSURE, {},
                                 ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
            if reply is None:
                return MaintResponse(ok=False, error="camera did not respond")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            result = dict(reply.result)
            result["auto_exposure_enabled"] = bool(ctx.shared_cfg.get(
                "auto_exposure_enabled", ctx.cfg.auto_exposure_enabled))
            return MaintResponse(ok=True, result=result)

        if cmd == "auto_exposure_set":
            try:
                enabled = bool(args["enabled"])
            except (KeyError, TypeError):
                return MaintResponse(
                    ok=False, error="auto_exposure_set requires boolean 'enabled'")
            persist = bool(args.get("persist", False))
            ctx.shared_cfg["auto_exposure_enabled"] = enabled
            if persist:
                cfg_mod.save_keys({"auto_exposure_enabled": enabled})
            log.info("Auto-exposure -> %s", enabled)
            return MaintResponse(ok=True, result={
                "auto_exposure_enabled": enabled, "persisted": persist})

        if cmd == "auto_tune":
            # Precondition: we must currently see a star field (fresh detection
            # with signal), else the sweep has nothing to optimise against.
            # auto-tune optimises detection on signal it can already see — it
            # cannot manufacture signal, so refuse with an actionable checklist
            # rather than running a doomed sweep.
            sol = dict(ctx.latest_solution)
            age = time.monotonic() - sol.get("epoch_monotonic", 0.0)
            peak = int(sol.get("peak", 0) or 0)
            if age > 10.0 or peak < 20:
                why = (f"no fresh detection (last frame {age:.0f}s ago)"
                       if age > 10.0
                       else f"frame too dim to tune (peak {peak} < 20)")
                return MaintResponse(ok=False, error=(
                    f"auto-tune needs a live star field first: {why}. "
                    "Before retrying: (1) remove the lens cap; (2) point at "
                    "open sky with stars; (3) focus until dots are sharp; "
                    "(4) raise exposure / gain (Camera-page sliders or "
                    "`diofinder-ctl exposure set` / `gain set`) until the live "
                    "frame shows stars and peak >= 20. auto-tune tunes detection "
                    "on signal it can already see; it can't create signal that "
                    "isn't there."))
            mode = str(args.get("mode") or ctx.shared_cfg.get(
                "seeing_mode", ctx.cfg.seeing_mode)).strip().lower()
            if not seeing_mod.is_valid_mode(mode):
                return MaintResponse(ok=False,
                                     error=f"invalid mode {mode!r} (good|bad)")
            try:
                params = {
                    "mode": mode,
                    "frames_per_point": max(2, min(15, int(
                        args.get("frames_per_point", 3)))),
                    "match_rate_floor": max(0.0, min(1.0, float(
                        args.get("match_rate_floor", 0.6)))),
                    "commit": bool(args.get("commit", False)),
                    "time_budget_s": max(20.0, min(600.0, float(
                        args.get("time_budget_s", 180.0)))),
                    "eval_solve_timeout_ms": max(200, min(5000, int(
                        args.get("eval_solve_timeout_ms", 1500)))),
                    "signal_floor": max(1, min(250, int(
                        args.get("signal_floor", _AT_SIGNAL_FLOOR)))),
                    "sigma_values": [float(x) for x in
                                     args.get("sigma_values", _AT_SIGMA_VALUES)],
                    "kernel_values": [float(x) for x in
                                      args.get("kernel_values", _AT_KERNEL_VALUES)],
                    "bg_modes": [str(x) for x in
                                 args.get("bg_modes", _AT_BG_MODES)],
                }
            except (ValueError, TypeError) as e:
                return MaintResponse(ok=False, error=f"bad auto_tune args: {e}")
            # Atomically claim the single in-flight slot.
            with _auto_tune_lock:
                if _auto_tune_state["running"]:
                    return MaintResponse(ok=False,
                                         error="auto-tune already running")
                _auto_tune_state.update(
                    running=True, phase="starting", message="initialising",
                    progress=0.0, result=None, error=None, cancel=False,
                    started_at=time.time())
            threading.Thread(target=_auto_tune_run, args=(ctx, params),
                             daemon=True).start()
            return MaintResponse(ok=True, result={
                "started": True, "mode": mode, "params": params})

        if cmd == "auto_tune_status":
            return MaintResponse(ok=True, result=_at_snapshot())

        if cmd == "auto_tune_cancel":
            with _auto_tune_lock:
                if not _auto_tune_state["running"]:
                    return MaintResponse(ok=True, result={"running": False})
                _auto_tune_state["cancel"] = True
            return MaintResponse(ok=True, result={"cancelling": True})

        if cmd == "auto_tune_apply_last":
            # Apply a finished dry-run's winner after the fact (so you decide to
            # commit AFTER seeing the result). Mirrors the commit branch in
            # _auto_tune_run; the dry run restored the camera, so set it here.
            with _auto_tune_lock:
                if _auto_tune_state["running"]:
                    return MaintResponse(ok=False, error="auto-tune is running")
                snap = dict(_auto_tune_state)
            result = snap.get("result") or {}
            best = result.get("best")
            if not best:
                return MaintResponse(
                    ok=False,
                    error="no auto-tune result to apply — run a dry run first")
            mode = result.get("mode") or ctx.shared_cfg.get(
                "seeing_mode", ctx.cfg.seeing_mode)
            photo = result.get("photometric") or {}
            updates = {
                "detect_sigma": float(best["sigma"]),
                "detect_kernel_sigma": float(best["kernel_sigma"]),
                "detect_bg_mode": str(best["bg_mode"]),
            }
            # Winner swept as a (bg_mode, noise_mode) pair -> commit the pair.
            if best.get("noise_mode"):
                updates["detect_noise_mode"] = str(best["noise_mode"])
            for k in ("detect_sigma", "detect_kernel_sigma", "detect_bg_mode",
                      "detect_noise_mode"):
                if k in updates:
                    ctx.shared_cfg[k] = updates[k]
            ctx.cfg.detect_sigma = updates["detect_sigma"]
            ctx.cfg.detect_kernel_sigma = updates["detect_kernel_sigma"]
            ctx.cfg.detect_bg_mode = updates["detect_bg_mode"]
            if "detect_noise_mode" in updates:
                ctx.cfg.detect_noise_mode = updates["detect_noise_mode"]
            for cam_key, cam_op in (("exposure_s", CAMERA_OP_SET_EXPOSURE),
                                    ("gain", CAMERA_OP_SET_GAIN)):
                val = photo.get(cam_key)
                if val is None:
                    continue
                r = _call_camera(cam_op, {cam_key: float(val)},
                                 ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
                if r is not None and r.ok:
                    setattr(ctx.cfg, cam_key, float(val))
                    updates[cam_key] = float(val)
            try:
                cfg_mod.save_keys(updates)
            except Exception as e:
                log.warning("auto_tune_apply_last could not persist: %s", e)
            override = None
            try:
                override = seeing_mod.save_override(mode, dict(updates),
                                                    source="auto_tune")
            except Exception as e:
                log.warning("auto_tune_apply_last could not save override: %s", e)
            _invalidate_solver_cache()
            _at_set(result={**result, "committed": True})
            log.info("auto-tune apply-last (override saved): %s", updates)
            return MaintResponse(ok=True, result={
                "applied": updates, "mode": mode, "override": override})

        if cmd == "tuning_set":
            # Toggle the libcamera tuning profile. Takes effect on the NEXT
            # service restart (the camera is initialised once at startup).
            profile = str(args.get("profile", "")).strip().lower()
            paths = {
                "finder":     "/usr/share/libcamera/ipa/rpi/vc4/imx477_finder.json",
                "scientific": "/usr/share/libcamera/ipa/rpi/vc4/imx477_scientific.json",
                "standard":   "/usr/share/libcamera/ipa/rpi/vc4/imx477.json",
            }
            if profile not in paths:
                return MaintResponse(
                    ok=False,
                    error="tuning_set requires profile 'finder', 'scientific', or 'standard'")
            cfg_mod.save_keys({"camera_tuning_file": paths[profile]})
            log.info("Camera tuning -> %s (%s); restart required", profile,
                     paths[profile])
            return MaintResponse(ok=True, result={
                "profile": profile, "camera_tuning_file": paths[profile],
                "restart_required": True})

        if cmd == "exposure_set":
            try:
                new_s = float(args["exposure_s"])
            except (KeyError, ValueError, TypeError) as e:
                return MaintResponse(ok=False,
                                     error=f"requires numeric exposure_s: {e}")
            persist = bool(args.get("persist", False))
            reply = _call_camera(CAMERA_OP_SET_EXPOSURE, {"exposure_s": new_s},
                                 ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
            if reply is None:
                return MaintResponse(ok=False, error="camera did not respond")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            if persist:
                cfg_mod.save_keys({"exposure_s": new_s})
            return MaintResponse(ok=True, result={**reply.result,
                                                  "persisted": persist})

        if cmd == "gain_set":
            try:
                new_g = float(args["gain"])
            except (KeyError, ValueError, TypeError) as e:
                return MaintResponse(ok=False,
                                     error=f"requires numeric gain: {e}")
            persist = bool(args.get("persist", False))
            reply = _call_camera(CAMERA_OP_SET_GAIN, {"gain": new_g},
                                 ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
            if reply is None:
                return MaintResponse(ok=False, error="camera did not respond")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            if persist:
                cfg_mod.save_keys({"gain": new_g})
            return MaintResponse(ok=True, result={**reply.result,
                                                  "persisted": persist})

        if cmd == "solver_params_get":
            return MaintResponse(ok=True, result={
                "detect_sigma":        ctx.shared_cfg.get(
                    "detect_sigma",        ctx.cfg.detect_sigma),
                "detect_bg_mode":      ctx.shared_cfg.get(
                    "detect_bg_mode",      ctx.cfg.detect_bg_mode),
                "detect_tophat_radius": ctx.shared_cfg.get(
                    "detect_tophat_radius", ctx.cfg.detect_tophat_radius),
                "detect_bg_block_size": ctx.shared_cfg.get(
                    "detect_bg_block_size", ctx.cfg.detect_bg_block_size),
                "detect_uniform_filter_size": ctx.shared_cfg.get(
                    "detect_uniform_filter_size", ctx.cfg.detect_uniform_filter_size),
                "detect_noise_mode":    ctx.shared_cfg.get(
                    "detect_noise_mode",    ctx.cfg.detect_noise_mode),
                "extractor_backend":    ctx.shared_cfg.get(
                    "extractor_backend",
                    getattr(ctx.cfg, "extractor_backend", "sycamore")),
                "detect_kernel_sigma":  ctx.shared_cfg.get(
                    "detect_kernel_sigma",  ctx.cfg.detect_kernel_sigma),
                "detect_max_axis_ratio": ctx.shared_cfg.get(
                    "detect_max_axis_ratio", ctx.cfg.detect_max_axis_ratio),
                "detect_local_noise":   ctx.shared_cfg.get(
                    "detect_local_noise",   ctx.cfg.detect_local_noise),
                "star_name_brightest":  ctx.shared_cfg.get(
                    "star_name_brightest",
                    getattr(ctx.cfg, "star_name_brightest", True)),
                "min_centroids":       ctx.shared_cfg.get(
                    "min_centroids",       ctx.cfg.min_centroids),
                "max_solve_stars":     ctx.shared_cfg.get(
                    "max_solve_stars",     ctx.cfg.max_solve_stars),
                "fov_max_error_deg":   ctx.shared_cfg.get(
                    "fov_max_error_deg",   ctx.cfg.fov_max_error_deg),
                "solve_timeout_ms":    ctx.shared_cfg.get(
                    "solve_timeout_ms",    ctx.cfg.solve_timeout_ms),
                "tracking_enabled":    ctx.shared_cfg.get(
                    "tracking_enabled",    ctx.cfg.tracking_enabled),
                "tracking_window_px":  ctx.shared_cfg.get(
                    "tracking_window_px",  ctx.cfg.tracking_window_px),
                "tracking_min_recover": ctx.shared_cfg.get(
                    "tracking_min_recover", ctx.cfg.tracking_min_recover),
            })

        if cmd == "solver_params_set":
            persist  = bool(args.get("persist", False))
            updates  = {}
            if "detect_sigma" in args:
                try:
                    sigma = float(args["detect_sigma"])
                except (ValueError, TypeError) as e:
                    return MaintResponse(ok=False,
                                        error=f"detect_sigma must be numeric: {e}")
                if not (0.0 <= sigma <= 20.0):
                    return MaintResponse(ok=False,
                                        error="detect_sigma out of range [0, 20]")
                ctx.shared_cfg["detect_sigma"] = sigma
                updates["detect_sigma"] = sigma
            if "detect_bg_mode" in args:
                mode = str(args["detect_bg_mode"]).strip().lower()
                valid_modes = ("row_percentile", "line_median", "top_hat",
                               "column_percentile", "row_column_percentile",
                               "block_percentile", "uniform_mean",
                               "temporal_median")
                if mode not in valid_modes:
                    return MaintResponse(ok=False,
                                        error=f"detect_bg_mode must be one of "
                                              f"{valid_modes}, got {mode!r}")
                ctx.shared_cfg["detect_bg_mode"] = mode
                updates["detect_bg_mode"] = mode
            if "detect_tophat_radius" in args:
                try:
                    r = int(args["detect_tophat_radius"])
                except (ValueError, TypeError) as e:
                    return MaintResponse(ok=False,
                                        error=f"detect_tophat_radius must be int: {e}")
                if not (1 <= r <= 100):
                    return MaintResponse(ok=False,
                                        error="detect_tophat_radius out of range [1, 100]")
                ctx.shared_cfg["detect_tophat_radius"] = r
                updates["detect_tophat_radius"] = r
            if "detect_bg_block_size" in args:
                try:
                    bs = int(args["detect_bg_block_size"])
                except (ValueError, TypeError) as e:
                    return MaintResponse(ok=False,
                                        error=f"detect_bg_block_size must be int: {e}")
                if not (4 <= bs <= 256):
                    return MaintResponse(ok=False,
                                        error="detect_bg_block_size out of range [4, 256]")
                ctx.shared_cfg["detect_bg_block_size"] = bs
                updates["detect_bg_block_size"] = bs
            if "detect_uniform_filter_size" in args:
                try:
                    fs = int(args["detect_uniform_filter_size"])
                except (ValueError, TypeError) as e:
                    return MaintResponse(ok=False,
                                        error=f"detect_uniform_filter_size must be int: {e}")
                if not (3 <= fs <= 255):
                    return MaintResponse(ok=False,
                                        error="detect_uniform_filter_size out of range [3, 255]")
                ctx.shared_cfg["detect_uniform_filter_size"] = fs
                updates["detect_uniform_filter_size"] = fs
            if "detect_noise_mode" in args:
                nm = str(args["detect_noise_mode"]).strip().lower()
                if nm not in ("mad", "global_rms"):
                    return MaintResponse(ok=False,
                                        error="detect_noise_mode must be 'mad' or 'global_rms'")
                ctx.shared_cfg["detect_noise_mode"] = nm
                updates["detect_noise_mode"] = nm
            if "extractor_backend" in args:
                eb = str(args["extractor_backend"]).strip().lower()
                if eb not in ("sycamore", "tetra3"):
                    return MaintResponse(ok=False,
                                        error="extractor_backend must be 'sycamore' or 'tetra3'")
                ctx.shared_cfg["extractor_backend"] = eb
                updates["extractor_backend"] = eb
            if "detect_kernel_sigma" in args:
                try:
                    ks = float(args["detect_kernel_sigma"])
                except (ValueError, TypeError) as e:
                    return MaintResponse(ok=False,
                                        error=f"detect_kernel_sigma must be numeric: {e}")
                if not (1.0 <= ks <= 4.0):
                    return MaintResponse(ok=False,
                                        error="detect_kernel_sigma out of range [1.0, 4.0]")
                ctx.shared_cfg["detect_kernel_sigma"] = ks
                updates["detect_kernel_sigma"] = ks
            if "detect_max_axis_ratio" in args:
                try:
                    mar = float(args["detect_max_axis_ratio"])
                except (ValueError, TypeError) as e:
                    return MaintResponse(ok=False,
                                        error=f"detect_max_axis_ratio must be numeric: {e}")
                # 0 disables trail rejection; otherwise must be 1.5–10.0.
                if mar != 0.0 and not (1.5 <= mar <= 10.0):
                    return MaintResponse(ok=False,
                                        error="detect_max_axis_ratio must be 0 (off) or 1.5–10.0")
                ctx.shared_cfg["detect_max_axis_ratio"] = mar
                updates["detect_max_axis_ratio"] = mar
            if "detect_local_noise" in args:
                ln = bool(args["detect_local_noise"])
                ctx.shared_cfg["detect_local_noise"] = ln
                updates["detect_local_noise"] = ln
            if "star_name_brightest" in args:
                snb = bool(args["star_name_brightest"])
                ctx.shared_cfg["star_name_brightest"] = snb
                updates["star_name_brightest"] = snb
            if "min_centroids" in args:
                try:
                    mc = int(args["min_centroids"])
                except (ValueError, TypeError) as e:
                    return MaintResponse(ok=False,
                                        error=f"min_centroids must be int: {e}")
                if not (4 <= mc <= 50):
                    return MaintResponse(ok=False,
                                        error="min_centroids out of range [4, 50]")
                ctx.shared_cfg["min_centroids"] = mc
                updates["min_centroids"] = mc
            if "max_solve_stars" in args:
                try:
                    ms_cap = int(args["max_solve_stars"])
                except (ValueError, TypeError) as e:
                    return MaintResponse(ok=False,
                                        error=f"max_solve_stars must be int: {e}")
                if not (4 <= ms_cap <= 200):
                    return MaintResponse(ok=False,
                                        error="max_solve_stars out of range [4, 200]")
                ctx.shared_cfg["max_solve_stars"] = ms_cap
                updates["max_solve_stars"] = ms_cap
            if "fov_max_error_deg" in args:
                try:
                    fe = float(args["fov_max_error_deg"])
                except (ValueError, TypeError) as e:
                    return MaintResponse(ok=False,
                                        error=f"fov_max_error_deg must be numeric: {e}")
                if not (0.05 <= fe <= 5.0):
                    return MaintResponse(ok=False,
                                        error="fov_max_error_deg out of range [0.05, 5.0]")
                ctx.shared_cfg["fov_max_error_deg"] = fe
                updates["fov_max_error_deg"] = fe
            if "solve_timeout_ms" in args:
                try:
                    ms = int(args["solve_timeout_ms"])
                except (ValueError, TypeError) as e:
                    return MaintResponse(ok=False,
                                        error=f"solve_timeout_ms must be int: {e}")
                if not (200 <= ms <= 10000):
                    return MaintResponse(ok=False,
                                        error="solve_timeout_ms out of range")
                ctx.shared_cfg["solve_timeout_ms"] = ms
                updates["solve_timeout_ms"] = ms
            if "tracking_enabled" in args:
                te = bool(args["tracking_enabled"])
                ctx.shared_cfg["tracking_enabled"] = te
                updates["tracking_enabled"] = te
            if "tracking_window_px" in args:
                try:
                    wp = int(args["tracking_window_px"])
                except (ValueError, TypeError) as e:
                    return MaintResponse(ok=False,
                                        error=f"tracking_window_px must be int: {e}")
                if not (8 <= wp <= 256):
                    return MaintResponse(ok=False,
                                        error="tracking_window_px out of range [8, 256]")
                ctx.shared_cfg["tracking_window_px"] = wp
                updates["tracking_window_px"] = wp
            if "tracking_min_recover" in args:
                try:
                    mr = int(args["tracking_min_recover"])
                except (ValueError, TypeError) as e:
                    return MaintResponse(ok=False,
                                        error=f"tracking_min_recover must be int: {e}")
                if not (3 <= mr <= 50):
                    return MaintResponse(ok=False,
                                        error="tracking_min_recover out of range [3, 50]")
                ctx.shared_cfg["tracking_min_recover"] = mr
                updates["tracking_min_recover"] = mr
            if persist and updates:
                cfg_mod.save_keys(updates)
            return MaintResponse(ok=True, result={**updates, "persisted": persist})

        if cmd == "match_params_get":
            return MaintResponse(ok=True, result={
                "match_radius": ctx.shared_cfg.get(
                    "match_radius", ctx.cfg.match_radius),
                "match_threshold": ctx.shared_cfg.get(
                    "match_threshold", ctx.cfg.match_threshold),
            })

        if cmd == "match_params_set":
            persist = bool(args.get("persist", False))
            updates = {}
            if "match_radius" in args:
                try:
                    mr = float(args["match_radius"])
                except (ValueError, TypeError) as e:
                    return MaintResponse(ok=False,
                                        error=f"match_radius must be numeric: {e}")
                if not (0.005 <= mr <= 0.05):
                    return MaintResponse(ok=False,
                                        error="match_radius out of range [0.005, 0.05]")
                ctx.shared_cfg["match_radius"] = mr
                updates["match_radius"] = mr
            if "match_threshold" in args:
                try:
                    mt = float(args["match_threshold"])
                except (ValueError, TypeError) as e:
                    return MaintResponse(ok=False,
                                        error=f"match_threshold must be numeric: {e}")
                if not (1e-9 <= mt <= 1e-3):
                    return MaintResponse(ok=False,
                                        error="match_threshold out of range [1e-9, 1e-3]")
                ctx.shared_cfg["match_threshold"] = mt
                updates["match_threshold"] = mt
            if persist and updates:
                cfg_mod.save_keys(updates)
            return MaintResponse(ok=True, result={**updates, "persisted": persist})

        if cmd == "seeing_get":
            mode = ctx.shared_cfg.get("seeing_mode", ctx.cfg.seeing_mode)
            try:
                effective = seeing_mod.effective_values(ctx.cfg, ctx.shared_cfg)
                drift = seeing_mod.drift_from_preset(mode, ctx.cfg, ctx.shared_cfg)
                # Augment effective with live camera exposure/gain so the
                # lineage check can recognise an override that pins them.
                eff_full = dict(effective)
                cam = _call_camera(CAMERA_OP_GET_EXPOSURE, {},
                                   ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
                if cam is not None and cam.ok:
                    eff_full["exposure_s"] = cam.result.get("exposure_s")
                    eff_full["gain"] = cam.result.get("gain")
                else:
                    eff_full["exposure_s"] = ctx.cfg.exposure_s
                    eff_full["gain"] = ctx.cfg.gain
                lineage = seeing_mod.classify_lineage(mode, ctx.cfg, eff_full)
                overrides = seeing_mod.overrides_summary()
            except Exception as e:
                return MaintResponse(ok=False, error=f"seeing_get failed: {e}")
            return MaintResponse(ok=True, result={
                "mode": mode,
                "presets": seeing_mod.SEEING_PRESETS,
                "rationale": seeing_mod.PRESET_RATIONALE,
                "effective": effective,
                "drift": drift,
                "lineage": lineage,
                "overrides": overrides,
                "deep_db_configured": bool(
                    (getattr(ctx.cfg, "star_db_deep", "") or "").strip()),
            })

        if cmd == "seeing_set":
            mode = str(args.get("mode", "")).strip().lower()
            if not seeing_mod.is_valid_mode(mode):
                return MaintResponse(
                    ok=False,
                    error=f"seeing_set requires mode 'good' or 'bad', got {mode!r}")
            # Overrides are explicit: a plain toggle loads the factory preset;
            # the saved override is applied only when use_override is requested.
            use_override = bool(args.get("use_override", False))
            try:
                preset, override_applied = seeing_mod.merged_preset(
                    mode, ctx.cfg, use_override=use_override)
            except ValueError as e:
                return MaintResponse(ok=False, error=str(e))

            # Route each preset key through the right channel.
            #  * star_db          -> solver DB reload (in-process), persist solver_db
            #  * exposure_s / gain -> camera (override-only keys; presets lack them)
            #  * everything else   -> live shared_cfg write (solver / auto-exp read it)
            persisted = {"seeing_mode": mode}
            db_token = preset.pop("star_db", None)

            for cam_key, cam_op in (("exposure_s", CAMERA_OP_SET_EXPOSURE),
                                    ("gain", CAMERA_OP_SET_GAIN)):
                if cam_key in preset:
                    val = preset.pop(cam_key)
                    r = _call_camera(cam_op, {cam_key: val},
                                     ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
                    if r is not None and r.ok:
                        persisted[cam_key] = val
                        setattr(ctx.cfg, cam_key, val)

            for key, val in preset.items():
                ctx.shared_cfg[key] = val
                persisted[key] = val

            # Switch the solver database if the preset selected a different one.
            # First, remember the standard db the first time we leave it —
            # "standard" resolves via star_db_standard with a fallback to
            # cfg.solver_db, which this very switch mutates and persists.
            # Without this snapshot, one Bad toggle would make "standard"
            # resolve to the deep path forever.
            if (db_token and db_token != ctx.cfg.solver_db
                    and not getattr(ctx.cfg, "star_db_standard", "")
                    and ctx.cfg.solver_db != getattr(ctx.cfg, "star_db_deep", "")):
                ctx.cfg.star_db_standard = ctx.cfg.solver_db
                persisted["star_db_standard"] = ctx.cfg.solver_db
            db_result = None
            if db_token and db_token != ctx.cfg.solver_db:
                reply = _call_solver(SOLVER_OP_SET_DB, {"db": db_token},
                                     ctx.solver_cmd_q, ctx.solver_cmd_reply_q,
                                     timeout_s=15.0)
                if reply is None:
                    return MaintResponse(ok=False,
                                         error="solver did not respond to set_db")
                if not reply.ok:
                    return MaintResponse(ok=False,
                                         error=f"set_db failed: {reply.error}")
                ctx.cfg.solver_db = db_token
                persisted["solver_db"] = db_token
                db_result = reply.result
            elif db_token:
                persisted["solver_db"] = db_token

            ctx.cfg.seeing_mode = mode
            ctx.shared_cfg["seeing_mode"] = mode
            try:
                cfg_mod.save_keys(persisted)
            except Exception as e:
                log.warning("Could not persist seeing preset: %s", e)

            _invalidate_solver_cache()
            log.info("Seeing preset -> %s (db=%s override=%s)",
                     mode, db_token, override_applied)
            return MaintResponse(ok=True, result={
                "mode": mode, "applied": persisted, "db": db_result,
                "override_applied": override_applied})

        if cmd == "seeing_override_save":
            mode = str(args.get("mode") or ctx.shared_cfg.get(
                "seeing_mode", ctx.cfg.seeing_mode)).strip().lower()
            if not seeing_mod.is_valid_mode(mode):
                return MaintResponse(ok=False, error=f"invalid mode {mode!r}")
            source = str(args.get("source", "manual"))
            values = args.get("values")
            if not values:
                # Default "save from current" is SPARSE (mirrors auto_tune):
                # keep only the preset keys that drift from the factory preset,
                # plus the camera's current exposure / gain (which the factory
                # preset can't express). star_db is left out deliberately.
                effective = seeing_mod.effective_values(ctx.cfg, ctx.shared_cfg)
                drift = seeing_mod.drift_from_preset(
                    mode, ctx.cfg, ctx.shared_cfg)
                values = {k: effective[k] for k in drift
                          if k in effective and k != "star_db"}
                cam = _call_camera(CAMERA_OP_GET_EXPOSURE, {},
                                   ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
                if cam is not None and cam.ok:
                    values["exposure_s"] = cam.result.get("exposure_s")
                    values["gain"] = cam.result.get("gain")
            try:
                entry = seeing_mod.save_override(mode, values, source=source)
            except ValueError as e:
                return MaintResponse(ok=False, error=str(e))
            log.info("Seeing override saved for %s (source=%s)", mode, source)
            return MaintResponse(ok=True, result={"mode": mode, "override": entry})

        if cmd == "seeing_override_clear":
            mode = str(args.get("mode") or ctx.shared_cfg.get(
                "seeing_mode", ctx.cfg.seeing_mode)).strip().lower()
            if not seeing_mod.is_valid_mode(mode):
                return MaintResponse(ok=False, error=f"invalid mode {mode!r}")
            removed = seeing_mod.clear_override(mode)
            log.info("Seeing override clear for %s -> removed=%s", mode, removed)
            return MaintResponse(ok=True, result={"mode": mode, "removed": removed})

        if cmd == "dark_capture":
            try:
                frames = int(args.get("frames", 16) or 16)
            except (ValueError, TypeError):
                frames = 16
            # Optional fixed capture point. A hot-pixel mask should cover the
            # *worst case* the finder will actually run at (hot pixels scale
            # with both exposure and gain), so the web UI captures at a long
            # exposure + the gain ceiling regardless of the live setting and
            # then restores. When neither is supplied we capture at whatever
            # the camera is currently set to (legacy behaviour).
            def _opt_float(key):
                v = args.get(key, None)
                if v in (None, ""):
                    return None
                try:
                    return float(v)
                except (ValueError, TypeError):
                    return None
            want_s = _opt_float("exposure_s")
            want_g = _opt_float("gain")

            snap_s = snap_g = None
            ae_was = None
            if want_s is not None or want_g is not None:
                snap = _call_camera(CAMERA_OP_GET_EXPOSURE, {},
                                    ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
                snap_s = (float(snap.result.get("exposure_s", ctx.cfg.exposure_s))
                          if (snap and snap.ok) else ctx.cfg.exposure_s)
                snap_g = (float(snap.result.get("gain", ctx.cfg.gain))
                          if (snap and snap.ok) else ctx.cfg.gain)
                # Pause auto-exposure so it doesn't fight the fixed point.
                ae_was = bool(ctx.shared_cfg.get("auto_exposure_enabled",
                                                 ctx.cfg.auto_exposure_enabled))
                ctx.shared_cfg["auto_exposure_enabled"] = False
                if want_s is not None:
                    _call_camera(CAMERA_OP_SET_EXPOSURE, {"exposure_s": want_s},
                                 ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
                if want_g is not None:
                    _call_camera(CAMERA_OP_SET_GAIN, {"gain": want_g},
                                 ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
                # Let the new setting flush through a few frames before stacking.
                settle_s = want_s if want_s is not None else (snap_s or 0.5)
                time.sleep(max(0.5, 2.0 * settle_s))

            try:
                reply = _call_solver(SOLVER_OP_DARK_CAPTURE, {"frames": frames},
                                     ctx.solver_cmd_q, ctx.solver_cmd_reply_q,
                                     timeout_s=max(30.0, frames * 0.6 + 10.0))
            finally:
                if snap_s is not None:
                    _call_camera(CAMERA_OP_SET_EXPOSURE, {"exposure_s": snap_s},
                                 ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
                if snap_g is not None:
                    _call_camera(CAMERA_OP_SET_GAIN, {"gain": snap_g},
                                 ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
                if ae_was is not None:
                    ctx.shared_cfg["auto_exposure_enabled"] = ae_was
            if reply is None:
                return MaintResponse(ok=False, error="solver did not respond")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            return MaintResponse(ok=True, result=reply.result)

        if cmd == "hot_pixel_status":
            reply = _call_solver(SOLVER_OP_HOT_PIXEL_STATUS, {},
                                 ctx.solver_cmd_q, ctx.solver_cmd_reply_q)
            if reply is None:
                return MaintResponse(ok=False, error="solver did not respond")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            return MaintResponse(ok=True, result=reply.result)

        if cmd == "hot_pixel_clear":
            reply = _call_solver(SOLVER_OP_HOT_PIXEL_CLEAR, {},
                                 ctx.solver_cmd_q, ctx.solver_cmd_reply_q)
            if reply is None:
                return MaintResponse(ok=False, error="solver did not respond")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            return MaintResponse(ok=True, result=reply.result)

        if cmd == "bg_cache_status":
            # Live temporal-background-cache snapshot (state, model age,
            # cached-vs-fallback counters). Cheap; no DB or solve involved.
            reply = _cached_call_solver(SOLVER_OP_BG_CACHE_STATUS,
                                        ctx.solver_cmd_q, ctx.solver_cmd_reply_q,
                                        ttl_s=0.5)
            if reply is None:
                return MaintResponse(ok=False, error="solver did not respond")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            return MaintResponse(ok=True, result=reply.result)

        if cmd == "tracking_status":
            # Live ROI tracking-mode snapshot (enabled, state, frame counters).
            # Cheap; no DB or solve involved.
            reply = _cached_call_solver(SOLVER_OP_TRACKING_STATUS,
                                        ctx.solver_cmd_q, ctx.solver_cmd_reply_q,
                                        ttl_s=0.5)
            if reply is None:
                return MaintResponse(ok=False, error="solver did not respond")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            return MaintResponse(ok=True, result=reply.result)

        if cmd == "solve_centroids":
            # Solve a caller-supplied centroid list using the solver's
            # already-loaded database (no second DB instance — memory-safe).
            # Used by tests/diag_background.py --solve. Centroids are
            # [[row, col], ...] in full-resolution pixel coordinates.
            cents = args.get("centroids")
            if not isinstance(cents, list) or not cents:
                return MaintResponse(
                    ok=False, error="solve_centroids requires non-empty 'centroids'")
            try:
                timeout_s = float(args.get("timeout_s", 20.0))
            except (ValueError, TypeError):
                timeout_s = 20.0
            reply = _call_solver(SOLVER_OP_SOLVE_CENTROIDS, {"centroids": cents},
                                 ctx.solver_cmd_q, ctx.solver_cmd_reply_q,
                                 timeout_s=timeout_s)
            if reply is None:
                return MaintResponse(ok=False, error="solver did not respond")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            return MaintResponse(ok=True, result=reply.result)

        if cmd == "set_test_mode":
            try:
                enabled = bool(args["enabled"])
            except (KeyError, TypeError):
                return MaintResponse(ok=False,
                                     error="set_test_mode requires boolean 'enabled'")
            ctx.shared_cfg["test_mode"] = enabled
            log.info("Test mode -> %s", enabled)
            return MaintResponse(ok=True, result={"test_mode": enabled})

        return MaintResponse(ok=False, error=f"unknown command: {cmd!r}")

    except Exception as e:
        log.exception("Maintenance command %r failed", cmd)
        return MaintResponse(ok=False, error=f"{type(e).__name__}: {e}")


class _MaintContext:
    """Holds all IPC handles needed by _handle_maint_command."""

    def __init__(self, *, cfg, latest_solution, shared_cfg,
                 solver_cmd_q, solver_cmd_reply_q,
                 camera_cmd_q, camera_cmd_reply_q):
        self.cfg = cfg
        self.latest_solution    = latest_solution
        self.shared_cfg         = shared_cfg
        self.solver_cmd_q       = solver_cmd_q
        self.solver_cmd_reply_q = solver_cmd_reply_q
        self.camera_cmd_q       = camera_cmd_q
        self.camera_cmd_reply_q = camera_cmd_reply_q


def _handle_maint_client(client, ctx):
    """Serve one maintenance socket connection: read newline-delimited JSON, write JSON replies."""
    client.settimeout(15.0)
    try:
        buf = b""
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                if not line.strip():
                    continue
                try:
                    req = MaintRequest.decode(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    client.sendall(MaintResponse(
                        ok=False,
                        error=f"bad request: {e}").encode())
                    continue
                resp = _handle_maint_command(req, ctx)
                client.sendall(resp.encode())
    except socket.timeout:
        pass
    except Exception as e:
        log.warning("Maint client error: %s", e)
    finally:
        try: client.close()
        except Exception: pass


def _serve_maint_socket(ctx, socket_path=None):
    """Bind the Unix maintenance socket and accept clients, each handled in a daemon thread."""
    if socket_path is None:
        socket_path = SOCKET_PATH
    sock_dir = os.path.dirname(socket_path)
    try:
        os.makedirs(sock_dir, exist_ok=True)
    except Exception as e:
        log.warning("Could not ensure %s exists: %s", sock_dir, e)
    try:
        if os.path.exists(socket_path):
            os.unlink(socket_path)
    except Exception as e:
        log.warning("Could not remove stale socket %s: %s", socket_path, e)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(socket_path)
    try:
        os.chmod(socket_path, 0o660)
    except Exception as e:
        log.warning("Could not chmod socket: %s", e)
    sock.listen(64)
    log.info("Maintenance socket listening at %s", socket_path)
    while True:
        try:
            client, _ = sock.accept()
        except Exception as e:
            log.error("Maint accept failed: %s; retrying in 1s", e)
            time.sleep(1); continue
        t = threading.Thread(
            target=_handle_maint_client, args=(client, ctx), daemon=True)
        t.start()


def _serve_lx200(latest_solution, shared_cfg, cfg,
                 align_request_q, align_response_q, ctx):
    """Bind the LX200 TCP socket and serve clients, one active connection at a time."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", cfg.lx200_port))
    sock.listen(8)
    log.info("LX200 server listening on :%d", cfg.lx200_port)
    while True:
        client, addr = sock.accept()
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        try:
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE,  10)
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL,  5)
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT,    3)
        except AttributeError:
            pass
        client.settimeout(cfg.lx200_client_timeout_s)
        log.debug("LX200 client from %s", addr)
        align_state = CommsAlignState()
        time_state  = {}
        try:
            buf = b""
            while True:
                chunk = client.recv(256)
                if not chunk:
                    break
                buf += chunk
                while b"#" in buf:
                    raw, _, buf = buf.partition(b"#")
                    cmd = raw.decode("ascii", errors="ignore").strip()
                    if not cmd.startswith(":"):
                        continue
                    reply = _handle_lx200_command(
                        cmd, latest_solution, align_state, time_state, cfg,
                        shared_cfg, align_request_q, align_response_q, ctx)
                    if reply:
                        client.sendall(reply)
        except socket.timeout:
            log.info("LX200 client %s timed out", addr)
        except Exception as e:
            log.warning("LX200 client %s error: %s", addr, e)
        finally:
            try: client.close()
            except Exception: pass


def comms_main(latest_solution, shared_cfg,
               align_request_q, align_response_q,
               solver_cmd_q, solver_cmd_reply_q,
               camera_cmd_q, camera_cmd_reply_q,
               cfg):
    logging.basicConfig(
        level=os.environ.get("DIOFINDER_LOGLEVEL", "INFO"),
        format="comms %(levelname)s %(message)s",
    )
    _pin_to_cpu(cfg.cpu_comms)

    # Seed the IMU pointing rate-gate from config so the .conf value is
    # honored; setdefault leaves any live override in place.
    shared_cfg.setdefault("imu_rate_gate_dps",
                          float(getattr(cfg, "imu_rate_gate_dps",
                                        _IMU_RATE_GATE_DPS)))

    ctx = _MaintContext(
        cfg=cfg, latest_solution=latest_solution, shared_cfg=shared_cfg,
        solver_cmd_q=solver_cmd_q, solver_cmd_reply_q=solver_cmd_reply_q,
        camera_cmd_q=camera_cmd_q, camera_cmd_reply_q=camera_cmd_reply_q,
    )
    maint_thread = threading.Thread(
        target=_serve_maint_socket, args=(ctx,),
        name="diofinder-maint", daemon=True)
    maint_thread.start()

    threading.Thread(target=_auto_exposure_loop, args=(ctx,),
                     name="diofinder-autoexp", daemon=True).start()

    threading.Thread(target=_watchdog_loop, args=(ctx,),
                     name="diofinder-watchdog", daemon=True).start()

    while True:
        try:
            _serve_lx200(latest_solution, shared_cfg, cfg,
                         align_request_q, align_response_q, ctx)
        except Exception as e:
            log.error("LX200 server crashed: %s; restarting in 2s", e)
            time.sleep(2)
