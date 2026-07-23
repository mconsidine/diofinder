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
from queue import Empty, Full, Queue

from diofinder import config as cfg_mod
from diofinder import bg_modes as bg_modes_mod
from diofinder import precession as _precession
from diofinder import mountlink as _mountlink
from diofinder.align import AlignRequest, AlignResult, CommsAlignState
from diofinder.imu_math import (quat_delta_rotvec, alpha_beta_step,
                                rotvec_to_quat, quat_mul, quat_to_radec,
                                get_imu_qt)
from diofinder.imu_frame import apply_rotation as _imu_apply_rotation
from diofinder import imu_solve_cal as _imu_solve_cal
from diofinder.maint import MaintRequest, MaintResponse, SOCKET_PATH
from diofinder.worker_cmds import (
    SolverCmd, CameraCmd,
    SOLVER_OP_CALIBRATION_STATUS, SOLVER_OP_CALIBRATION_RESET,
    SOLVER_OP_POLAR_START, SOLVER_OP_POLAR_STATUS,
    SOLVER_OP_POLAR_CANCEL, SOLVER_OP_POLAR_SET_LATITUDE,
    SOLVER_OP_SOLVE_CENTROIDS, SOLVER_OP_BG_CACHE_STATUS,
    SOLVER_OP_SET_DB, SOLVER_OP_DARK_CAPTURE,
    SOLVER_OP_HOT_PIXEL_STATUS, SOLVER_OP_HOT_PIXEL_CLEAR,
    SOLVER_OP_FRAME_GET, SOLVER_OP_BG_PREVIEW,
    SOLVER_OP_TRACKING_STATUS, SOLVER_OP_SOLVE_STATS,
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


def _call_solver(op, args, solver_cmd_q, solver_cmd_reply_q, timeout_s=8.0):
    """Send op/args to solver_proc and return the SolverCmdReply, or None on timeout.

    Default 8 s: the solver drains commands once per loop iteration, and the
    loop can legitimately sit 5 s inside its frame wait — the old 5 s default
    raced that window and returned spurious "solver did not respond" at long
    exposures (audit 2026-07 W4)."""
    rid = next(_request_id_seq)
    with _solver_call_lock:
        try:
            # Bounded: while the solver is legitimately non-draining for a
            # long stretch (60 s set_db, ~20 s dark capture) pollers can fill
            # the 16-slot queue; a BLOCKING put then froze every solver-backed
            # maint command while holding the call lock (audit 2026-07 W-L2).
            solver_cmd_q.put(SolverCmd(op=op, args=args or {}, request_id=rid),
                             timeout=1.0)
        except Full:
            log.warning("solver command queue full (op=%s) — solver busy", op)
            return None
        return _wait_for_reply(solver_cmd_reply_q, rid, timeout_s=timeout_s)


# Last exposure comms observed (from any camera reply carrying exposure_s).
# Scales the camera RPC timeout: camera_proc drains its command queue once
# per loop iteration, each of which blocks ~one exposure inside capture, so a
# fixed 2 s timeout made EVERY camera RPC (auto-exposure reads, UI controls,
# dark-capture snapshots) time out at exposures over ~2 s (audit 2026-07 W4).
# Plain float assignment is GIL-atomic; no lock needed.
_last_exposure_s = 0.5


def _call_camera(op, args, camera_cmd_q, camera_cmd_reply_q, timeout_s=None):
    """Send op/args to camera_proc and return the CameraCmdReply, or None on timeout."""
    global _last_exposure_s
    if timeout_s is None:
        timeout_s = max(2.0, 2.0 + 2.0 * _last_exposure_s)
    rid = next(_request_id_seq)
    with _camera_call_lock:
        camera_cmd_q.put(CameraCmd(op=op, args=args or {}, request_id=rid))
        reply = _wait_for_reply(camera_cmd_reply_q, rid, timeout_s=timeout_s)
    try:
        if reply is not None and reply.ok and isinstance(reply.result, dict):
            v = reply.result.get("exposure_s")
            if v:
                _last_exposure_s = float(v)
    except Exception:
        pass
    return reply


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
      * low-contrast **and not solving** -> the raw star-count fallback is not
        just untrustworthy here, it is actively backwards: a near-black,
        quantization-limited frame lets a sigma*noise threshold trip on pure
        noise (the noise floor itself is clamped, e.g. MAD-floored at 0.5 DN,
        so the threshold sits only a couple of DN above background), producing
        a count that looks *plentiful* while matching nothing. Treated as an
        ordinary "over-served" reading, that count would suppress the raise
        this frame actually needs. So this state is forced into the starved
        branch instead — peak, not the corrupted count, drives the decision.
        Observed live: peak pinned at 20-22 (floor is 70), "stars" 183-353,
        matches 0, stuck for ~4.5 minutes / 146 consecutive failed solves
        before an unrelated exposure change broke the deadlock.
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
    #    An unsolved, low-contrast frame is forced into this branch even if
    #    the star-count metric looks well above target — see the low-contrast
    #    docstring note above.
    starved = metric < _AE_STARVED_FRAC * target or (not solved and low_contrast)
    if starved:
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


def _ae_apply_reversal_damping(action, cur_s, cur_g, last_dir):
    """Halve the step (in log space) when AE reverses direction, so the ladder
    can land inside the deadband instead of limit-cycling across it.

    Observed on-sky: gain ping-ponged 2.1 <-> 3.2 for minutes because the x1.5
    gain step straddles the 0.8x-1.5x star-count deadband — at the low rung the
    frame yields too few stars (raise), at the high rung too many (reduce), and
    the full step never lands inside. On a direction reversal this replaces the
    proposed step with its square root (x1.5 -> x1.22, x0.8 -> x0.89). The
    damped step is computed from the CURRENT full-step proposal each time, so
    it is a constant half-step on every reversal (not compounding); it works
    because a x1.22 rung always fits inside the 1.875-wide relative deadband.
    Same-direction moves (a genuine trend) keep full steps.

    ``last_dir`` is +1 / -1 / 0: the direction of the last APPLIED change (0 =
    none yet). Returns ``(action, new_last_dir)``; a held cycle (action None)
    keeps ``last_dir`` unchanged. The saturation backoff must not be damped —
    the caller skips this helper on saturated frames.
    """
    if not action:
        return action, last_dir
    new_g = action.get("gain", cur_g)
    new_s = action.get("exposure_s", cur_s)
    cur_dir = 1 if (new_g > cur_g or new_s > cur_s) else -1
    if last_dir and cur_dir == -last_dir:
        if "gain" in action:
            damped = cur_g * math.sqrt(new_g / cur_g)
            if round(damped, 2) == round(cur_g, 2):
                return None, last_dir          # step too small to matter: hold
            action = {"gain": round(damped, 2)}
        else:
            damped = cur_s * math.sqrt(new_s / cur_s)
            if abs(damped - cur_s) < _AE_MIN_EXP_DELTA_S:
                return None, last_dir
            action = {"exposure_s": round(damped, 4)}
    return action, cur_dir


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


# Refcounted auto-exposure pause: dark_capture and auto_tune both need AE
# quiet while they drive the camera. Snapshot/restore of the shared flag
# clobbered under interleaving (one path could restore a stale False,
# disabling AE until restart). The user-facing enable flag is untouched;
# the loop simply holds while the pause count is non-zero.
_ae_pause_lock = threading.Lock()
_ae_pause_count = 0


def _ae_pause():
    global _ae_pause_count
    with _ae_pause_lock:
        _ae_pause_count += 1


def _ae_resume():
    global _ae_pause_count
    with _ae_pause_lock:
        _ae_pause_count = max(0, _ae_pause_count - 1)


def _ae_paused() -> bool:
    with _ae_pause_lock:
        return _ae_pause_count > 0


# --------------------------------------------------------------------------- #
# Mount link (outbound SYNC to a GoTo mount; default OFF). Owns one serial
# connection shared by the auto-push loop and the manual mount_sync/mount_test
# maint commands, serialized by a lock. Sync-only — it cannot move the mount.
# --------------------------------------------------------------------------- #

_MOUNT_MANUAL_MAX_AGE_S = 10.0   # a manual "sync now" tolerates a slightly older fix
_MOUNT_FAIL_BACKOFF_S = 30.0     # auto-mode: pause after a failed sync (no spam)


def _mount_params(cfg, scfg):
    proto = str(scfg.get("mount_protocol",
                         getattr(cfg, "mount_protocol", "synscan"))).lower()
    port = str(scfg.get("mount_serial_port",
                        getattr(cfg, "mount_serial_port", "/dev/serial0")))
    baud = int(scfg.get("mount_serial_baud",
                        getattr(cfg, "mount_serial_baud", 9600)))
    epoch = str(scfg.get("mount_epoch",
                         getattr(cfg, "mount_epoch", "jnow"))).lower()
    return proto, port, baud, epoch


def _mount_gates(cfg, scfg):
    def g(key, default):
        return scfg.get(key, getattr(cfg, key, default))
    return {
        "max_age_s": float(g("mount_auto_max_age_s", 3.0)),
        "min_matches": int(g("mount_auto_min_matches", 8)),
        "settle_s": float(g("mount_auto_settle_s", 3.0)),
        "deadband_arcmin": float(g("mount_auto_deadband_arcmin", 1.0)),
        "min_interval_s": float(g("mount_auto_min_interval_s", 15.0)),
    }


def _solved_j2000(sol, max_age_s):
    """(ra, dec) J2000 of a fresh solve, else None. Uses the raw solved
    position (boresight-corrected) — NOT the IMU prediction — since an
    alignment sync wants the real fix, not a smoothed extrapolation."""
    if not sol.get("solved"):
        return None
    ra = sol.get("ra_deg")
    dec = sol.get("dec_deg")
    if ra is None or dec is None:
        return None
    if time.monotonic() - sol.get("epoch_monotonic", 0.0) > max_age_s:
        return None
    return float(ra), float(dec)


class _MountManager:
    """One serial mount connection, shared by the auto loop and manual commands.

    All serial access goes through ``_lock`` so the push loop and a manual
    ``mount_sync`` can't interleave on the wire. The link is (re)built when the
    protocol/port/baud change; the mount epoch updates without a rebuild.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._link = None
        self._transport = None
        self._key = None            # (protocol, port, baud) the link was built for
        self.connected = False
        self.mount_version = None
        self.last_sync = None       # {ra, dec, utc, result, t, ...}
        self.syncs_ok = 0
        self.syncs_fail = 0
        self.last_error = None

    def _teardown(self):
        if self._transport is not None:
            self._transport.close()
        self._transport = None
        self._link = None
        self._key = None
        self.connected = False

    def _ensure(self, cfg, scfg):
        """(Re)build + open the link. Caller holds the lock. Raises MountError."""
        proto, port, baud, epoch = _mount_params(cfg, scfg)
        key = (proto, port, baud)
        if self._link is None or self._key != key:
            self._teardown()
            self._transport = _mountlink.SerialTransport(port, baud)
            self._link = _mountlink.make_link(proto, self._transport, epoch=epoch)
            self._key = key
        self._link.epoch = epoch    # live-updatable, no rebuild
        if not self._transport.is_open:
            self._transport.open()
        return self._link

    def test(self, cfg, scfg):
        with self._lock:
            try:
                ver = self._ensure(cfg, scfg).version()
                self.mount_version = ver
                self.connected = True
                self.last_error = None
                return True, ver
            except _mountlink.MountError as e:
                self.last_error = str(e)
                self._teardown()
                return False, str(e)

    def sync(self, ra_j2000, dec_j2000, cfg, scfg):
        with self._lock:
            try:
                link = self._ensure(cfg, scfg)
                ok, detail = link.sync(ra_j2000, dec_j2000)
                self.connected = True
                rec = {
                    "ra": round(ra_j2000, 5), "dec": round(dec_j2000, 5),
                    "utc": datetime.datetime.now(
                        datetime.timezone.utc).isoformat(timespec="seconds"),
                    "t": time.monotonic(),
                    "result": "ok" if ok else "rejected",
                    "sent_ra_deg": detail.get("sent_ra_deg"),
                    "sent_dec_deg": detail.get("sent_dec_deg"),
                    "epoch": detail.get("epoch"),
                }
                self.last_sync = rec
                if ok:
                    self.syncs_ok += 1
                    self.last_error = None
                else:
                    self.syncs_fail += 1
                    self.last_error = f"mount rejected sync (reply {detail.get('reply')!r})"
                return ok, rec
            except _mountlink.MountError as e:
                self.syncs_fail += 1
                self.last_error = str(e)
                self._teardown()
                return False, {"result": "error", "error": str(e)}

    def status(self, cfg, scfg):
        proto, port, baud, epoch = _mount_params(cfg, scfg)
        with self._lock:
            return {
                "enabled": bool(scfg.get(
                    "mount_enabled", getattr(cfg, "mount_enabled", False))),
                "protocol": proto, "port": port, "baud": baud, "epoch": epoch,
                "mode": str(scfg.get(
                    "mount_mode", getattr(cfg, "mount_mode", "manual"))).lower(),
                "connected": self.connected,
                "mount_version": self.mount_version,
                "last_sync": self.last_sync,
                "syncs_ok": self.syncs_ok, "syncs_fail": self.syncs_fail,
                "last_error": self.last_error,
            }


_mount_mgr = _MountManager()


def _mount_loop(ctx, interval_s=2.0):
    """Auto-push daemon: in 'auto' mode, sync the mount after gated solves.

    Gated by mount_enabled + mount_mode=='auto'. Manual mode idles here (the
    user drives it via the mount_sync maint command). Never dies; a serial
    failure is logged, the link torn down, and retried next cycle.
    """
    cfg = ctx.cfg
    last_sync = None          # {"ra","dec","t"} of the last accepted auto sync
    fail_until = 0.0          # back off attempts until this monotonic time
    fail_logged = False       # WARNING logged once per failure streak, not per cycle
    while True:
        time.sleep(interval_s)
        try:
            scfg = dict(ctx.shared_cfg)
            if not scfg.get("mount_enabled", getattr(cfg, "mount_enabled", False)):
                continue
            if str(scfg.get("mount_mode",
                            getattr(cfg, "mount_mode", "manual"))).lower() != "auto":
                continue
            now = time.monotonic()
            if now < fail_until:
                # A recent sync failed (no mount / wrong port / rejected).
                # Back off so a missing mount doesn't retry — and log — every
                # cycle; mount_status still shows last_error + syncs_fail.
                continue
            sol = dict(ctx.latest_solution)
            gates = _mount_gates(cfg, scfg)
            do, why = _mountlink.should_sync(
                sol, now, last_sync, gates, _imu_is_moving(scfg))
            if not do:
                continue
            ra, dec = float(sol["ra_deg"]), float(sol["dec_deg"])
            ok, rec = _mount_mgr.sync(ra, dec, cfg, scfg)
            if ok:
                last_sync = {"ra": ra, "dec": dec, "t": rec["t"]}
                if fail_logged:
                    log.info("Mount link recovered")
                    fail_logged = False
                log.info("Mount auto-sync: RA %.4f Dec %.4f (%s)",
                         ra, dec, rec.get("epoch"))
            else:
                fail_until = now + _MOUNT_FAIL_BACKOFF_S
                if not fail_logged:
                    log.warning("Mount auto-sync failed (%s); backing off %ds "
                                "(see mount_status)",
                                rec.get("error") or rec.get("result"),
                                int(_MOUNT_FAIL_BACKOFF_S))
                    fail_logged = True
        except Exception as e:
            log.warning("mount loop step failed: %s", e)


def _auto_exposure_loop(ctx, interval_s=5.0):
    """Background controller: adjust exposure toward the target star count.

    Enabled live via shared_cfg['auto_exposure_enabled'] (auto_exposure_set
    maint command / Camera page toggle). Skips stale solutions so it never
    reacts to frames from before its own last adjustment.
    """
    cfg = ctx.cfg
    raise_streak = 0          # consecutive brightness-raise intents (debounce)
    last_dir = 0              # direction of the last APPLIED change (damping)
    last_apply_t = 0.0        # when the controller last changed the camera
    while True:
        time.sleep(interval_s)
        try:
            if _ae_paused() or not ctx.shared_cfg.get(
                    "auto_exposure_enabled", cfg.auto_exposure_enabled):
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
            # Settle guard: a control change reaches the sensor 1-3 frames
            # later, so a solution read soon after our own last change is
            # evidence about the OLD operating point. Raises are protected by
            # the debounce, but reductions act immediately — without this a
            # 5 s cycle could reduce twice on pre-change frames and overshoot
            # into starvation. Saturation is urgent and stays exempt.
            settle_s = max(3.0, 2.0 * cur_s)
            if (time.monotonic() - last_apply_t < settle_s
                    and sol.get("peak", 0) < _AE_PEAK_SATURATION):
                continue
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
            # Damp direction reversals so the ladder converges into the
            # deadband instead of limit-cycling across it (gain 2.1<->3.2 seen
            # on-sky). Saturation backoff is a safety action — never damped.
            if sol.get("peak", 0) < _AE_PEAK_SATURATION:
                action, last_dir = _ae_apply_reversal_damping(
                    action, cur_s, cur_g, last_dir)
            elif action:
                last_dir = -1        # the backoff is an applied reduce
            if not action:
                continue
            ctx_qs = (ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
            if "exposure_s" in action:
                reply = _call_camera(CAMERA_OP_SET_EXPOSURE,
                                     {"exposure_s": action["exposure_s"]}, *ctx_qs)
                if reply is not None and reply.ok:
                    last_apply_t = time.monotonic()
                    log.info("auto-exposure: exp %.3fs -> %.3fs "
                             "(solved=%s stars=%s matches=%s peak=%s)",
                             cur_s, action["exposure_s"], sol.get("solved"),
                             sol.get("stars"), sol.get("matches"), sol.get("peak"))
            if "gain" in action:
                reply = _call_camera(CAMERA_OP_SET_GAIN,
                                     {"gain": action["gain"]}, *ctx_qs)
                if reply is not None and reply.ok:
                    last_apply_t = time.monotonic()
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
# A dark capture in flight (synchronous inside the maint handler). Mutual
# exclusion with the auto_tune sweep: both drive the camera with
# snapshot-and-restore sequences that interleave last-writer-wins
# (audit 2026-07 F-L7). Written under the GIL from maint handler threads.
_dark_capture_active = False

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
    # Pause the always-on auto-exposure loop so it doesn't fight the sweep
    # (refcounted — safe against a concurrent dark_capture pause).
    _ae_pause()

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
                result["persisted"] = True
            except Exception as e:
                # Surface it: a silent ok=True here is the "settings saved !=
                # settings in force" class — live values applied, but a
                # restart reverts them (audit 2026-07 F4).
                log.warning("auto-tune could not persist: %s", e)
                result["persisted"] = False
                result["persist_error"] = str(e)
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
        _ae_resume()
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
    # Generous one-shot deadline for the FIRST publish: a solver that wedges
    # during startup (corrupt DB, import hang) previously never armed the
    # watchdog and sat silent forever. 300 s covers the slowest legitimate
    # first DB load on an SD card with a wide margin.
    first_publish_deadline_s = 300.0
    started_mono = time.monotonic()
    armed = False
    last_epoch = 0.0
    last_change_mono = time.monotonic()
    log.info("Solver watchdog armed (timeout=%.0fs, first-publish deadline=%.0fs)",
             timeout_s, first_publish_deadline_s)
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
            elif now - started_mono > first_publish_deadline_s:
                log.critical(
                    "Solver watchdog: no FIRST solution published within "
                    "%.0fs of startup; solver appears wedged during init. "
                    "Exiting so systemd restarts the unit.",
                    first_publish_deadline_s)
                os._exit(1)
            continue
        if epoch != last_epoch:
            last_epoch = epoch
            last_change_mono = now
            continue
        # Solver declared itself busy (database load blocks the solve loop,
        # so the epoch legitimately stalls): don't enforce while the flag is
        # fresh. Bounded at 120 s so a solver that dies mid-load still gets
        # restarted.
        busy_t = float(ctx.shared_cfg.get("solver_busy_t", 0.0) or 0.0)
        if busy_t > 0.0 and now - busy_t < 120.0:
            last_change_mono = now
            continue
        stale_s = now - last_change_mono
        if stale_s > timeout_s:
            log.critical(
                "Solver watchdog: no new solution for %.0fs (> %.0fs timeout); "
                "solver appears hung. Exiting so systemd restarts the unit.",
                stale_s, timeout_s)
            os._exit(1)


# Monotonic :CM# correlation ids. `next()` on an itertools.count is atomic
# under the GIL, so id generation is thread-safe. But the align exchange
# (drain align_response_q -> put request -> wait for the matching reply) must
# not interleave across the per-connection LX200 threads (v0.11.52): a second
# thread's initial queue-drain could eat the first's in-flight response,
# stranding it. _align_lock serializes the whole exchange (alignment is a rare,
# user-initiated tap, so serializing costs nothing).
_align_req_seq = itertools.count(1)
_align_lock = threading.Lock()

# Shared LX200 align target across ALL connection threads (v0.11.56 fix).
# SkySafari sends :Sr/:Sd (set target RA/Dec) then :CM# (sync). v0.11.52's
# threaded server gave each connection its OWN CommsAlignState, so if the client
# split those commands across connections — or reconnected between them — the
# :CM# landed on a connection whose target was never set: build_request()
# returned None, _do_alignment bailed with a silent "no align target#", and the
# boresight never moved (align appeared to do nothing, no log). One shared
# instance restores the pre-v0.11.52 behaviour. Aligns are rare and effectively
# single-client, so the tiny race between two simultaneous aligns is acceptable.
_lx200_align_state = CommsAlignState()

# LX200 commands we don't handle — logged once each (bounded) at INFO so a
# client using a *different* sync/align command than :CM# is visible in a
# bundle instead of hiding at DEBUG.
_lx200_unhandled_seen: set = set()

# Rapid-reconnect ("storm") detection. Some SkySafari configs open a NEW TCP
# connection per poll (~several/sec). We tolerate it (it's why the align target
# is shared, v0.11.56), but the per-connection log stays at DEBUG so it can't
# flood the journal — instead the accept loop surfaces the *pattern* with ONE
# throttled WARNING (evaluated per 5 s window; a persistent connection is
# lighter on CPU 0). Mutated only by the single-threaded accept loop — no race.
_LX200_STORM_PER_5S = 10          # >= this many connects in a 5 s window = storm
_LX200_STORM_WARN_EVERY_S = 120.0
_lx200_conn_count = 0
_lx200_conn_window_t = 0.0
_lx200_storm_warned_t = 0.0


def _lx200_note_connection(addr):
    """Count accepted connections and emit a throttled storm WARNING. Called
    only from the single-threaded accept loop, so the module counters are safe
    without a lock."""
    global _lx200_conn_count, _lx200_conn_window_t, _lx200_storm_warned_t
    now = time.monotonic()
    if now - _lx200_conn_window_t > 5.0:
        prev = _lx200_conn_count
        _lx200_conn_window_t = now
        _lx200_conn_count = 0
        if prev >= _LX200_STORM_PER_5S and \
                now - _lx200_storm_warned_t > _LX200_STORM_WARN_EVERY_S:
            _lx200_storm_warned_t = now
            log.warning(
                "LX200: %d rapid reconnects in ~5s from %s — some SkySafari "
                "configs open a new connection per poll. Tolerated (align target "
                "is shared), but a persistent connection is lighter on CPU 0.",
                prev, addr)
    _lx200_conn_count += 1


def _do_alignment(align_state, cfg, shared_cfg,
                  align_request_q, align_response_q):
    """Execute :CM# alignment: send target to solver, wait for result, persist boresight."""
    req = align_state.build_request()
    if req is None:
        log.warning("LX200 :CM# ignored — no align target set (:Sr/:Sd not "
                    "received before :CM#). Align makes no change.")
        return "no align target#"

    # The align target arrives from SkySafari in its reporting epoch (JNow by
    # default). The solver's boresight math runs in the internal J2000 frame,
    # so convert the target JNow -> J2000 to keep the align epoch-consistent
    # with the solved field. report_epoch=j2000 -> passthrough (the boresight
    # then silently absorbs the epoch offset, as it did pre-v0.11.53).
    if str(shared_cfg.get("report_epoch", "jnow")).lower() == "jnow":
        req.target_ra_deg, req.target_dec_deg = _precession.jnow_to_j2000(
            req.target_ra_deg, req.target_dec_deg)

    # Serialize the whole shared-queue exchange: with per-connection LX200
    # threads (v0.11.52), a second align's initial drain could otherwise eat
    # the first's in-flight response off the shared align_response_q.
    with _align_lock:
        try:
            while True:
                align_response_q.get_nowait()
        except Empty:
            pass

        req.request_id = next(_align_req_seq)
        try:
            # NEVER block: with the solver not consuming (dark frames keep
            # requests queued), a 5th retry's blocking put wedged the comms
            # thread forever — no LX200 client could be served again, and
            # nothing restarts a blocked-but-alive thread (audit 2026-07 W2).
            align_request_q.put_nowait(req)
        except Full:
            align_state.reset()
            log.warning("align request queue full (solver not consuming — dark "
                        "frames / mid-slew?); replying busy")
            return "align fail: solver busy#"
        log.info("Alignment requested: RA=%.4f Dec=%.4f (id %d)",
                 req.target_ra_deg, req.target_dec_deg, req.request_id)

        deadline = time.monotonic() + CommsAlignState.DEFAULT_TIMEOUT_S
        result = None
        while time.monotonic() < deadline:
            try:
                candidate = align_response_q.get(timeout=0.5)
                # Strict correlation: only THIS request's echo counts. The old
                # completed_at >= requested_at check accepted a late result from
                # a PREVIOUS sync — in the worst interleaving a success computed
                # for the old target was persisted as the new sync's boresight
                # (audit 2026-07 W2).
                if getattr(candidate, "request_id", 0) == req.request_id:
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
# Live-tunable via shared_cfg["imu_rate_gate_dps"] — set through
# solver_params_get/set (v0.11.50: Camera-page "IMU pointing gate" slider /
# diofinder-ctl), no restart. Raise it if a parked scope still jitters.
_IMU_RATE_GATE_DPS = 0.1
_IMU_RATE_BASELINE_S = 0.8
_imu_rate_state = {}     # samples: deque[(imu_t, quat)], engaged, moving_t

# Pointing is "stale" (surfaced to the web UI, not the LX200 wire) when the
# last solution is older than this. ~1-2 Hz solving means >10 s = several
# missed solves, i.e. a genuine drought worth flagging.
_POINTING_STALE_S = 10.0


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
        # 1 us slack: (t0 + baseline) - t0 can round a hair below baseline in
        # float64, and an exact-equality miss here silently drops the sample.
        if imu_t - t_old >= baseline_s - 1e-6:
            r = quat_delta_rotvec(q_now, q_old)
            ang = math.degrees(
                math.sqrt(r[0] * r[0] + r[1] * r[1] + r[2] * r[2]))
            rate = ang / (imu_t - t_old)
            break
    if rate is not None and rate >= rate_gate_dps:
        state["engaged"] = True
        state["moving_t"] = imu_t
    elif (state.get("engaged")
            and ref_t and ref_t > state.get("moving_t", 0.0)):
        # A solve landed after the last observed motion: the reference is
        # re-anchored, so the frozen solved position equals where we are —
        # disengage seamlessly. Deliberately does NOT require a measurable
        # rate: at slow poll intervals the sample deque prunes to one entry
        # (rate is None), and requiring a rate here latched "engaged" forever
        # so a parked scope walked with gyro drift — the exact bug the gate
        # exists to fix. If the scope is in fact still moving, the next
        # fast-enough sample pair re-engages via the branch above.
        state["engaged"] = False
    return bool(state.get("engaged"))


_mount_rate_state = {}          # dedicated motion state for the mount push loop
_mount_rate_lock = threading.Lock()


def _imu_is_moving(scfg) -> bool:
    """Best-effort slew detector for the mount auto-push (its own rate state,
    so it never perturbs the LX200 pointing path). Returns False when the IMU
    is unavailable — a missing motion signal must not silently block syncs; the
    freshness / deadband gates still protect against a mid-slew push."""
    if not scfg.get("imu_available", False):
        return False
    q_now, imu_t = get_imu_qt(scfg)
    if q_now is None or time.monotonic() - imu_t > 2.0:
        return False
    ref = scfg.get("imu_ref")
    ref_t = ref[4] if ref is not None and len(ref) >= 5 else 0.0
    rate_gate = float(scfg.get("imu_rate_gate_dps", _IMU_RATE_GATE_DPS))
    with _mount_rate_lock:
        return _imu_motion_engaged(_mount_rate_state, q_now, imu_t, ref_t,
                                   rate_gate_dps=rate_gate)


def _imu_predict(shared_cfg):
    """Return (ra_deg, dec_deg) predicted from IMU rotation since last solve, or None if unavailable."""
    if not shared_cfg.get("imu_available", False):
        return None
    q_now, imu_t = get_imu_qt(shared_cfg)
    if q_now is None or time.monotonic() - imu_t > 2.0:
        return None
    ref = shared_cfg.get("imu_ref")
    if ref is not None:
        # Atomic tuple (v0.11.20+ solver): one RPC, and immune to a solve
        # landing between reads (the split keys could pair a new quaternion
        # with the previous solve's RA/Dec — a degrees-scale pointing error
        # exactly at post-slew re-anchor). v0.11.23 appends the solved sky
        # quaternion as a 6th element for the exact frame-corrected path.
        q_ref, ra_ref, dec_ref, roll_ref_v, ref_t = ref[:5]
        sky_q_ref = ref[5] if len(ref) >= 6 else None
    else:
        q_ref   = shared_cfg.get("imu_ref_q")
        ra_ref  = shared_cfg.get("imu_ref_ra_deg")
        dec_ref = shared_cfg.get("imu_ref_dec_deg")
        roll_ref_v = None
        ref_t   = shared_cfg.get("imu_ref_t", 0.0)
        sky_q_ref = None
    if q_ref is None or ra_ref is None or time.monotonic() - ref_t > 120.0:
        return None
    # Unit C Mode-1 (default off): correct the static accel-tilt bias in BOTH
    # the current and reference IMU quaternions so the delta between them is
    # measured in the true-gravity frame. Software-only, reversible, and never
    # touches the chip; a no-op unless imu_solve_cal_enabled with a published
    # estimate.
    if shared_cfg.get("imu_solve_cal_enabled", False):
        _sc = shared_cfg.get("imu_solve_cal")
        _bias = _sc.get("bias_tilt") if isinstance(_sc, dict) else None
        if _bias:
            q_now = _imu_solve_cal.correct_quaternion(q_now, _bias)
            q_ref = _imu_solve_cal.correct_quaternion(q_ref, _bias)
    # Exact frame-corrected path (v0.11.23): when the solver has published a
    # quality-gated IMU-body -> camera rotation fit AND the reference carries
    # the solved sky quaternion, predict by exact quaternion composition —
    # the same construction the solve hint uses (imu_frame fit pairs). No
    # small-angle approximation, so no 5-degree clamp and no pole guard; the
    # C-matrix calibration is not needed at all on this path.
    # Kill switch (v0.11.28): if the on-sky crosshair looks wrong during a
    # slew, `imu_exact_predict=false` forces the legacy C-matrix small-angle
    # path (the pre-v0.11.23 behavior) without a downgrade. Default true.
    R9 = shared_cfg.get("imu_frame_R")
    use_frame = (R9 is not None and len(R9) == 9 and sky_q_ref is not None
                 and shared_cfg.get("imu_exact_predict", True))
    if not use_frame:
        # Legacy small-angle C-matrix path needs its calibration to be
        # present and healthy.
        if shared_cfg.get("imu_calib_n", 0) < 3:
            return None
        if shared_cfg.get("imu_calib_quality", 0.0) < 0.85:
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
    if use_frame:
        r_cam = _imu_apply_rotation(R9, r)
        q_pred = quat_mul(rotvec_to_quat(r_cam), sky_q_ref)
        return quat_to_radec(q_pred)
    c = C_flat
    dr = c[0]*r[0] + c[1]*r[1] + c[2]*r[2]
    du = c[3]*r[0] + c[4]*r[1] + c[5]*r[2]
    roll_ref = (roll_ref_v if roll_ref_v is not None
                else shared_cfg.get("imu_ref_roll_deg", 0.0))
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
        imu_t = get_imu_qt(shared_cfg)[1]
        # Re-anchor timestamp from the atomic imu_ref tuple (v0.11.24 solvers
        # no longer publish the split imu_ref_t key); fall back to the split
        # key for older solvers.
        _ref = shared_cfg.get("imu_ref")
        ref_t = _ref[4] if _ref is not None else shared_cfg.get("imu_ref_t", 0.0)
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


# Snapshot cache for the :GR/:GD poll pair. :GD virtually always follows
# :GR within milliseconds and the alpha-beta filter already dedupes on
# imu_t, so <=100 ms staleness is invisible — while cutting up to 4 full-dict
# Manager RPCs per poll cycle down to 2 per 100 ms window (audit 2026-07 P3).
_POLL_SNAP_TTL_S = 0.1
_poll_snap_lock = threading.Lock()
_poll_snap = {"t": 0.0, "cfg": None, "sol": None}


def _poll_snapshots(shared_cfg, latest_solution):
    """(shared_cfg snapshot, latest_solution snapshot) with a 100 ms TTL."""
    now = time.monotonic()
    with _poll_snap_lock:
        if _poll_snap["cfg"] is None or now - _poll_snap["t"] > _POLL_SNAP_TTL_S:
            _poll_snap["cfg"] = dict(shared_cfg)
            _poll_snap["sol"] = dict(latest_solution)
            _poll_snap["t"] = now
        return _poll_snap["cfg"], _poll_snap["sol"]


def _report_radec(scfg, sol):
    """(ra_deg, dec_deg) to report to LX200 clients — IMU-predicted between
    solves when available, else the last solved position — converted from the
    internal **J2000** frame to the reporting epoch.

    diofinder solves in J2000/ICRS and never precesses internally, but
    SkySafari's LX200 link uses the equinox of date (JNow), so the default
    `report_epoch=jnow` precesses J2000 -> JNow at this boundary. Precession
    mixes RA and Dec, so the full pair is converted together here and :GR/:GD
    each take their component from the same (TTL-cached) snapshot. Kill switch:
    `report_epoch=j2000` reports the raw J2000 frame (pre-v0.11.53 behaviour)."""
    pred = _imu_predict_smoothed(scfg)
    if pred is not None:
        ra, dec = float(pred[0]), float(pred[1])
    else:
        ra, dec = float(sol.get("ra_deg", 0.0)), float(sol.get("dec_deg", 0.0))
    if str(scfg.get("report_epoch", "jnow")).lower() == "jnow":
        ra, dec = _precession.j2000_to_jnow(ra, dec)
    return ra, dec


def _handle_lx200_command(cmd, latest_solution, align_state, time_state,
                          cfg, shared_cfg,
                          align_request_q, align_response_q, ctx=None):
    """Dispatch one LX200 command string and return the raw bytes reply."""
    if cmd == ":GR":
        # TTL-cached snapshots = at most TWO Manager IPC round-trips per
        # 100 ms window shared across :GR and :GD; the prediction path then
        # reads ~14 keys locally. Also makes the multi-key read coherent —
        # no solve can land between key reads. Reported in the epoch the
        # client expects (JNow by default); see _report_radec.
        scfg, sol = _poll_snapshots(shared_cfg, latest_solution)
        ra, _dec = _report_radec(scfg, sol)
        return _format_ra(ra / 15.0).encode("ascii")
    if cmd == ":GD":
        scfg, sol = _poll_snapshots(shared_cfg, latest_solution)
        _ra, dec = _report_radec(scfg, sol)
        return _format_dec(dec).encode("ascii")
    if cmd == ":GW":
        return b"AT2#"
    if cmd in (":GVN", ":GVP"):
        return f"diofinder {cfg.version}#".encode("ascii")
    if cmd.startswith(":GV"):
        return f"diofinder {cfg.version}#".encode("ascii")
    if cmd.startswith(":Sr"):
        ok = align_state.set_target_ra(cmd[3:])
        log.info("LX200 :Sr target RA %s (%r)",
                 "set" if ok else "REJECTED", cmd[3:].strip())
        return b"1" if ok else b"0"
    if cmd.startswith(":Sd"):
        ok = align_state.set_target_dec(cmd[3:])
        log.info("LX200 :Sd target Dec %s (%r)",
                 "set" if ok else "REJECTED", cmd[3:].strip())
        return b"1" if ok else b"0"
    if cmd == ":CM":
        log.info("LX200 :CM# sync received — target %s",
                 "on record" if align_state.can_align()
                 else "MISSING (no :Sr/:Sd before :CM#)")
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
    if cmd not in _lx200_unhandled_seen and len(_lx200_unhandled_seen) < 64:
        _lx200_unhandled_seen.add(cmd)
        log.info("LX200 unhandled command (first seen): %r — if this is your "
                 "client's align/sync command, that's why :CM# alignment "
                 "isn't firing", cmd)
    else:
        log.debug("Unhandled LX200 command: %r", cmd)
    return b"#"


def _handle_maint_command(req: MaintRequest, ctx) -> MaintResponse:
    """Dispatch one maintenance-socket command and return a MaintResponse."""
    global _dark_capture_active
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
            # Wheel versions travel with every version report: the wheels are
            # refreshed independently of the code (image build / OTA / pip),
            # and a stale olive-solve wheel has already masqueraded as an
            # application regression once.
            from diofinder.wheels import wheel_versions
            result["wheels"] = wheel_versions()
            # Active DIOFINDER_* env overrides mask conf values (applied last
            # at load); surfacing them here turns "persist succeeded but the
            # value reverts" reports into a one-look diagnosis (audit F-L9).
            env_ov = getattr(ctx.cfg, "env_overrides", None)
            if env_ov:
                result["env_overrides"] = env_ov
            return MaintResponse(ok=True, result=result)

        if cmd == "status":
            # ONE Manager snapshot instead of ~15 individual gets — this
            # command is polled at 0.8 Hz by the home page (audit 2026-07 P6).
            scfg        = dict(ctx.shared_cfg)
            sol         = dict(ctx.latest_solution)
            imu_n       = scfg.get("imu_calib_n", 0)
            imu_quality = scfg.get("imu_calib_quality", 0.0)
            imu_avail   = scfg.get("imu_available", False)
            imu_active  = imu_avail and imu_n >= 3 and imu_quality >= 0.85
            imu_qv, imu_t = get_imu_qt(scfg)
            # Post-solve reference from the atomic tuple (split keys are no
            # longer published); fall back to them for older solvers.
            _ref = scfg.get("imu_ref")
            if _ref is not None:
                ref_q, ref_ra, ref_dec, ref_roll = _ref[:4]
            else:
                ref_q    = scfg.get("imu_ref_q")
                ref_ra   = scfg.get("imu_ref_ra_deg")
                ref_dec  = scfg.get("imu_ref_dec_deg")
                ref_roll = scfg.get("imu_ref_roll_deg")
            # Pointing staleness: the LX200 :GR/:GD path already holds the last
            # solved position when no fresh solve/prediction is available, so a
            # solve drought (e.g. low on the horizon) never blanks SkySafari —
            # but nothing told the USER the crosshair was minutes old. Surface
            # the age of the last solution + a stale flag so the web UI can say
            # so (v0.11.52). Cross-process monotonic is comparable (same
            # CLOCK_MONOTONIC origin), as the watchdog already relies on.
            _sol_epoch = sol.get("epoch_monotonic")
            _sol_age = (time.monotonic() - _sol_epoch) if _sol_epoch else None
            # Reporting-epoch position (JNow by default) so the web UI matches
            # what SkySafari shows. The raw ra_deg/dec_deg in `sol` stay J2000
            # for any internal consumer; report_* is the converted copy.
            _rep_epoch = str(scfg.get("report_epoch", "jnow")).lower()
            sol["report_epoch"] = _rep_epoch
            if sol.get("solved") and sol.get("ra_deg") is not None:
                if _rep_epoch == "jnow":
                    _rra, _rdec = _precession.j2000_to_jnow(
                        sol["ra_deg"], sol["dec_deg"])
                else:
                    _rra, _rdec = sol["ra_deg"], sol["dec_deg"]
                sol["report_ra_deg"] = _rra
                sol["report_dec_deg"] = _rdec
            return MaintResponse(ok=True, result={
                "solution":  sol,
                "pointing_age_s": (round(_sol_age, 1)
                                   if _sol_age is not None else None),
                "pointing_stale": bool(_sol_age is not None
                                       and _sol_age > _POINTING_STALE_S),
                "boresight": {
                    "y": scfg.get("boresight_y", ctx.cfg.boresight_y),
                    "x": scfg.get("boresight_x", ctx.cfg.boresight_x),
                },
                "fov_deg":        scfg.get("fov_deg", ctx.cfg.fov_deg),
                "config_summary": ctx.cfg.summary(),
                "imu": {
                    "available": imu_avail,
                    "calib_n":   imu_n,
                    "quality":   round(imu_quality, 3),
                    "active":    imu_active,
                    # Raw IMU output + post-solve reference, so a debug bundle
                    # records the actual attitude reading at capture time, not
                    # just whether the IMU is active.
                    "q":            imu_qv,
                    "t":            imu_t,
                    "age_s":        (round(time.monotonic() - imu_t, 3)
                                     if imu_t else None),
                    "ref_q":        ref_q,
                    "ref_ra_deg":   ref_ra,
                    "ref_dec_deg":  ref_dec,
                    "ref_roll_deg": ref_roll,
                    # Calibration state for debug bundles / field validation:
                    # Unit A extrinsic fit quality, Unit C solve-cal estimate,
                    # Unit B chip calib status.
                    "frame_quality": scfg.get("imu_frame_quality"),
                    "solve_cal":     scfg.get("imu_solve_cal"),
                    "calib_status":  scfg.get("imu_calib_status"),
                },
                "solver_backend": "sycamore",
                "test_mode":      scfg.get("test_mode", False),
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
            updates = {"auto_exposure_enabled": enabled}
            # Optional controller knobs (audit 2026-07 F-M4): the AE loop
            # already reads these from shared_cfg each cycle, but no maint
            # writer existed — the docs called them live-mutable and nothing
            # honored it.
            if args.get("peak_floor") is not None:
                try:
                    pf = float(args["peak_floor"])
                except (ValueError, TypeError):
                    return MaintResponse(ok=False, error="bad peak_floor")
                if not 0.0 <= pf <= 250.0:
                    return MaintResponse(
                        ok=False, error="peak_floor must be in [0, 250]")
                updates["auto_exposure_peak_floor"] = pf
            if args.get("nominal_s") is not None:
                try:
                    ns = float(args["nominal_s"])
                except (ValueError, TypeError):
                    return MaintResponse(ok=False, error="bad nominal_s")
                if not 0.0 <= ns <= 10.0:
                    return MaintResponse(
                        ok=False, error="nominal_s must be in [0, 10]")
                updates["auto_exposure_nominal_s"] = ns
            ctx.shared_cfg.update(updates)
            if persist:
                cfg_mod.save_keys(updates)
            log.info("Auto-exposure -> %s (%s)", enabled, updates)
            return MaintResponse(ok=True, result={
                **updates, "persisted": persist})

        # ---- Mount link (outbound SYNC; sync-only, never slews) ----
        if cmd == "mount_status":
            return MaintResponse(
                ok=True, result=_mount_mgr.status(ctx.cfg, dict(ctx.shared_cfg)))

        if cmd == "mount_test":
            ok, detail = _mount_mgr.test(ctx.cfg, dict(ctx.shared_cfg))
            if not ok:
                return MaintResponse(ok=False, error=detail)
            return MaintResponse(ok=True, result={"connected": True,
                                                  "mount_version": detail})

        if cmd == "mount_sync":
            scfg = dict(ctx.shared_cfg)
            sol = dict(ctx.latest_solution)
            fix = _solved_j2000(sol, _MOUNT_MANUAL_MAX_AGE_S)
            if fix is None:
                return MaintResponse(ok=False, error="no fresh solution to sync")
            ok, rec = _mount_mgr.sync(fix[0], fix[1], ctx.cfg, scfg)
            if not ok:
                return MaintResponse(
                    ok=False, error=rec.get("error", "mount rejected sync"))
            log.info("Mount manual sync: RA %.4f Dec %.4f (%s)",
                     fix[0], fix[1], rec.get("epoch"))
            return MaintResponse(ok=True, result=rec)

        if cmd == "mount_set":
            # Live-tunable mount keys (enabled/mode/epoch + the auto gates).
            # Transport keys (protocol/port/baud) are restart-level and set in
            # the conf, so they are NOT accepted here.
            updates = {}
            if "enabled" in args:
                updates["mount_enabled"] = bool(args["enabled"])
            if args.get("mode") is not None:
                mode = str(args["mode"]).lower()
                if mode not in ("manual", "auto"):
                    return MaintResponse(ok=False, error="mode must be manual/auto")
                updates["mount_mode"] = mode
            if args.get("epoch") is not None:
                epoch = str(args["epoch"]).lower()
                if epoch not in ("jnow", "j2000"):
                    return MaintResponse(ok=False, error="epoch must be jnow/j2000")
                updates["mount_epoch"] = epoch
            _num_gates = {
                "mount_auto_max_age_s": (float, 0.5, 60.0),
                "mount_auto_min_matches": (int, 0, 100),
                "mount_auto_settle_s": (float, 0.0, 60.0),
                "mount_auto_deadband_arcmin": (float, 0.0, 120.0),
                "mount_auto_min_interval_s": (float, 0.0, 3600.0),
            }
            for key, (cast, lo, hi) in _num_gates.items():
                short = key[len("mount_auto_"):]
                if args.get(short) is None and args.get(key) is None:
                    continue
                raw = args.get(short, args.get(key))
                try:
                    val = cast(raw)
                except (ValueError, TypeError):
                    return MaintResponse(ok=False, error=f"bad {short}")
                if not lo <= val <= hi:
                    return MaintResponse(
                        ok=False, error=f"{short} must be in [{lo}, {hi}]")
                updates[key] = val
            if not updates:
                return MaintResponse(ok=False, error="mount_set: nothing to set")
            persist = bool(args.get("persist", True))
            ctx.shared_cfg.update(updates)
            if persist:
                cfg_mod.save_keys(updates)
            log.info("Mount settings -> %s", updates)
            return MaintResponse(ok=True, result={**updates, "persisted": persist})

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
            if _dark_capture_active:
                return MaintResponse(ok=False, error=(
                    "a dark-frame capture is in progress — it drives the "
                    "camera to a fixed worst-case point; wait for it to "
                    "finish, then retry"))
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
            persist_ok, persist_err = True, None
            try:
                cfg_mod.save_keys(updates)
            except Exception as e:
                log.warning("auto_tune_apply_last could not persist: %s", e)
                persist_ok, persist_err = False, str(e)
            override = None
            try:
                override = seeing_mod.save_override(mode, dict(updates),
                                                    source="auto_tune")
            except Exception as e:
                log.warning("auto_tune_apply_last could not save override: %s", e)
            _invalidate_solver_cache()
            _at_set(result={**result, "committed": True})
            log.info("auto-tune apply-last (override saved): %s", updates)
            _r = {"applied": updates, "mode": mode, "override": override,
                  "persisted": persist_ok}
            if persist_err:
                _r["persist_error"] = persist_err
            return MaintResponse(ok=True, result=_r)

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
            # Keep the in-process cfg in step: the auto-exposure controller
            # anchors its "walk exposure back to nominal" behaviour on
            # cfg.exposure_s, so a stale value here silently unwinds a
            # deliberate user setting one AE cycle at a time.
            ctx.cfg.exposure_s = new_s
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
            ctx.cfg.gain = new_g
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
                "star_name_whole_fov":  ctx.shared_cfg.get(
                    "star_name_whole_fov",
                    getattr(ctx.cfg, "star_name_whole_fov", False)),
                "star_name_dso":        ctx.shared_cfg.get(
                    "star_name_dso",
                    getattr(ctx.cfg, "star_name_dso", True)),
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
                "bg_cache_bin_at_submit": ctx.shared_cfg.get(
                    "bg_cache_bin_at_submit", ctx.cfg.bg_cache_bin_at_submit),
                "imu_exact_predict": ctx.shared_cfg.get(
                    "imu_exact_predict", ctx.cfg.imu_exact_predict),
                "imu_rate_gate_dps": ctx.shared_cfg.get(
                    "imu_rate_gate_dps",
                    getattr(ctx.cfg, "imu_rate_gate_dps", _IMU_RATE_GATE_DPS)),
                "report_epoch": ctx.shared_cfg.get(
                    "report_epoch",
                    str(getattr(ctx.cfg, "report_epoch", "jnow")).lower()),
                "imu_solve_cal_enabled": ctx.shared_cfg.get(
                    "imu_solve_cal_enabled",
                    getattr(ctx.cfg, "imu_solve_cal_enabled", False)),
                # Live estimate (Unit C) for the Camera-page readout, or None.
                "imu_solve_cal": ctx.shared_cfg.get("imu_solve_cal"),
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
                # Single source of truth for mode names (bg_modes registry).
                valid_modes = bg_modes_mod.MODE_NAMES
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
            if "star_name_whole_fov" in args:
                snwf = bool(args["star_name_whole_fov"])
                ctx.shared_cfg["star_name_whole_fov"] = snwf
                updates["star_name_whole_fov"] = snwf
            if "star_name_dso" in args:
                snd = bool(args["star_name_dso"])
                ctx.shared_cfg["star_name_dso"] = snd
                updates["star_name_dso"] = snd
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
            if "bg_cache_bin_at_submit" in args:
                bs = bool(args["bg_cache_bin_at_submit"])
                ctx.shared_cfg["bg_cache_bin_at_submit"] = bs
                updates["bg_cache_bin_at_submit"] = bs
            if "imu_exact_predict" in args:
                ep = bool(args["imu_exact_predict"])
                ctx.shared_cfg["imu_exact_predict"] = ep
                updates["imu_exact_predict"] = ep
            if "imu_solve_cal_enabled" in args:
                # Unit C master switch — read live by the solver (estimator) and
                # comms (_imu_predict correction), so this toggles without a
                # restart. Mode 2 (chip write) stays conf-only, not exposed here.
                sce = bool(args["imu_solve_cal_enabled"])
                ctx.shared_cfg["imu_solve_cal_enabled"] = sce
                updates["imu_solve_cal_enabled"] = sce
            if "imu_rate_gate_dps" in args:
                try:
                    rg = float(args["imu_rate_gate_dps"])
                except (ValueError, TypeError) as e:
                    return MaintResponse(
                        ok=False, error=f"imu_rate_gate_dps must be numeric: {e}")
                if not (0.0 <= rg <= 10.0):
                    return MaintResponse(
                        ok=False, error="imu_rate_gate_dps out of range [0, 10]")
                # Read live by _imu_predict's motion gate; no solver round-trip.
                ctx.shared_cfg["imu_rate_gate_dps"] = rg
                updates["imu_rate_gate_dps"] = rg
            if "report_epoch" in args:
                ep = str(args["report_epoch"]).strip().lower()
                if ep not in ("jnow", "j2000"):
                    return MaintResponse(
                        ok=False, error="report_epoch must be 'jnow' or 'j2000'")
                ctx.shared_cfg["report_epoch"] = ep
                updates["report_epoch"] = ep
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
                # With AE on, exposure/gain are the controller's to move —
                # exclude them from the tuned-match so the badge doesn't
                # flip to Custom on the first AE trim (audit F-L8).
                _ae_on = bool(ctx.shared_cfg.get(
                    "auto_exposure_enabled", ctx.cfg.auto_exposure_enabled))
                lineage = seeing_mod.classify_lineage(
                    mode, ctx.cfg, eff_full,
                    ignore_keys=(("exposure_s", "gain") if _ae_on else ()))
                overrides = seeing_mod.overrides_summary()
            except Exception as e:
                return MaintResponse(ok=False, error=f"seeing_get failed: {e}")
            return MaintResponse(ok=True, result={
                "mode": mode,
                "presets": seeing_mod.display_presets(ctx.cfg),
                "rationale": seeing_mod.PRESET_RATIONALE,
                "effective": effective,
                "drift": drift,
                "lineage": lineage,
                "overrides": overrides,
                # File-existence check, not just a non-empty path: the
                # shipped conf pre-points at the mag85 path even on images
                # built without the deep asset, and resolve_star_db silently
                # falls back to standard — the UI must not imply otherwise.
                "deep_db_configured": bool(
                    (getattr(ctx.cfg, "star_db_deep", "") or "").strip()
                    and seeing_mod._db_exists(
                        (getattr(ctx.cfg, "star_db_deep", "") or "").strip())),
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

            # Switch the solver database FIRST: it is the only step that can
            # fail (missing file / slow SD load / solver busy), and it used to
            # run AFTER every shared_cfg + camera write — a failure then left
            # the solver on one preset's detection params with the other
            # preset's DB, nothing persisted and the UI showing the old mode.
            # Failing before any write keeps the toggle atomic: either the
            # whole preset applies or none of it does.
            # Remember the standard db the first time we leave it —
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
                                     timeout_s=60.0)
                if reply is None:
                    return MaintResponse(ok=False,
                                         error="solver did not respond to set_db "
                                               "— preset NOT applied")
                if not reply.ok:
                    return MaintResponse(ok=False,
                                         error=f"set_db failed: {reply.error} "
                                               "— preset NOT applied")
                ctx.cfg.solver_db = db_token
                persisted["solver_db"] = db_token
                db_result = reply.result
            elif db_token:
                persisted["solver_db"] = db_token

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

            ctx.cfg.seeing_mode = mode
            ctx.shared_cfg["seeing_mode"] = mode
            persist_ok, persist_err = True, None
            try:
                cfg_mod.save_keys(persisted)
            except Exception as e:
                # Applied live but NOT persisted: without surfacing this, a
                # read-only SD (post-power-blip remount) made the toggle
                # silently revert on the next restart (audit 2026-07 F4).
                log.warning("Could not persist seeing preset: %s", e)
                persist_ok, persist_err = False, str(e)

            _invalidate_solver_cache()
            log.info("Seeing preset -> %s (db=%s override=%s persisted=%s)",
                     mode, db_token, override_applied, persist_ok)
            result = {"mode": mode, "applied": persisted, "db": db_result,
                      "override_applied": override_applied,
                      "persisted": persist_ok}
            if persist_err:
                result["persist_error"] = persist_err
            return MaintResponse(ok=True, result=result)

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
            if _auto_tune_state["running"]:
                return MaintResponse(ok=False, error=(
                    "an auto-tune sweep is running — it drives the camera; "
                    "cancel it (auto_tune_cancel) or wait, then retry"))
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

            # Pause AE FIRST and put the camera set + settle inside the
            # try: the old order let an AE cycle move gain during the ~2 s
            # settle (the "fixed worst-case" mask was then captured at a
            # different point than requested), and an exception in the setup
            # window left the camera parked at the worst-case setting with
            # AE resumed against it (audit 2026-07 F7).
            snap_s = snap_g = None
            _dark_capture_active = True
            _ae_pause()
            try:
                if want_s is not None or want_g is not None:
                    snap = _call_camera(CAMERA_OP_GET_EXPOSURE, {},
                                        ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
                    snap_s = (float(snap.result.get("exposure_s", ctx.cfg.exposure_s))
                              if (snap and snap.ok) else ctx.cfg.exposure_s)
                    snap_g = (float(snap.result.get("gain", ctx.cfg.gain))
                              if (snap and snap.ok) else ctx.cfg.gain)
                    if want_s is not None:
                        _call_camera(CAMERA_OP_SET_EXPOSURE, {"exposure_s": want_s},
                                     ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
                    if want_g is not None:
                        _call_camera(CAMERA_OP_SET_GAIN, {"gain": want_g},
                                     ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
                    # Let the new setting flush through a few frames before
                    # stacking.
                    settle_s = want_s if want_s is not None else (snap_s or 0.5)
                    time.sleep(max(0.5, 2.0 * settle_s))

                # Record the capture conditions in the mask metadata
                # (audit F-L1): the fixed-point values when supplied, else
                # the live setting observed at capture time.
                _cap_args = {"frames": frames}
                _eff_s, _eff_g = want_s, want_g
                if _eff_s is None or _eff_g is None:
                    _live = _call_camera(CAMERA_OP_GET_EXPOSURE, {},
                                         ctx.camera_cmd_q,
                                         ctx.camera_cmd_reply_q)
                    if _live is not None and _live.ok:
                        if _eff_s is None:
                            _eff_s = _live.result.get("exposure_s")
                        if _eff_g is None:
                            _eff_g = _live.result.get("gain")
                if _eff_s is not None:
                    _cap_args["exposure_s"] = float(_eff_s)
                if _eff_g is not None:
                    _cap_args["gain"] = float(_eff_g)
                reply = _call_solver(SOLVER_OP_DARK_CAPTURE, _cap_args,
                                     ctx.solver_cmd_q, ctx.solver_cmd_reply_q,
                                     timeout_s=max(30.0, frames * 0.6 + 10.0))
            finally:
                if snap_s is not None:
                    _call_camera(CAMERA_OP_SET_EXPOSURE, {"exposure_s": snap_s},
                                 ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
                if snap_g is not None:
                    _call_camera(CAMERA_OP_SET_GAIN, {"gain": snap_g},
                                 ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
                _ae_resume()
                _dark_capture_active = False
            if reply is None:
                return MaintResponse(ok=False, error="solver did not respond")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            return MaintResponse(ok=True, result=reply.result)

        if cmd == "display_start":
            # Web-UI live-view keepalive: arm the solver's demand-gated
            # display-segment writes for a short TTL. Cheap — one shared_cfg
            # write. The browser re-arms every couple of seconds while the
            # page polls; when it stops, the writes lapse and the solver
            # stops paying the copy (P4).
            ctx.shared_cfg["display_wanted_until"] = time.monotonic() + 5.0
            return MaintResponse(ok=True, result={"armed_s": 5.0})

        if cmd == "frame_get":
            # Newest camera frame via the solver's FrameSlots-bracketed read
            # (never torn by a concurrent camera write). Serves the webui
            # live view, debug bundles, and A/B captures; args:
            # {"after_seq": N} waits (<=2 s solver-side) for a frame newer
            # than N so bursts can chain strictly consecutive frames.
            try:
                after = int(args.get("after_seq", -1))
            except (ValueError, TypeError):
                after = -1
            reply = _call_solver(SOLVER_OP_FRAME_GET, {"after_seq": after},
                                 ctx.solver_cmd_q, ctx.solver_cmd_reply_q,
                                 timeout_s=6.0)
            if reply is None:
                return MaintResponse(ok=False, error="solver did not respond")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            import base64
            r = dict(reply.result)
            r["data_b64"] = base64.b64encode(r.pop("data")).decode("ascii")
            return MaintResponse(ok=True, result=r)

        if cmd == "bg_preview":
            # Reconstruct the background a mode subtracts, for the Background
            # page's visual A/B. Returns the paired frame + full-res background
            # for the SAME seq (consistent subtracted view) plus preview meta;
            # temporal_median renders the solver's live cached median stack,
            # which no other process can see.
            reply = _call_solver(SOLVER_OP_BG_PREVIEW, dict(args),
                                 ctx.solver_cmd_q, ctx.solver_cmd_reply_q,
                                 timeout_s=6.0)
            if reply is None:
                return MaintResponse(ok=False, error="solver did not respond")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            import base64
            r = dict(reply.result)
            r["frame_b64"] = base64.b64encode(r.pop("frame")).decode("ascii")
            r["bg_b64"] = base64.b64encode(r.pop("bg")).decode("ascii")
            return MaintResponse(ok=True, result=r)

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

        if cmd == "solve_stats":
            # Per-successful-solve records (FULL vs TRACKING) for the tracking
            # A/B harness. args: {"after": epoch} for only-newer records.
            try:
                after = float(args.get("after", -1.0))
            except (TypeError, ValueError):
                after = -1.0
            reply = _call_solver(SOLVER_OP_SOLVE_STATS, {"after": after},
                                 ctx.solver_cmd_q, ctx.solver_cmd_reply_q)
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
                # Clamped: the wait holds the global solver-call lock, so an
                # oversized value would wedge every solver-backed maint
                # command for its duration.
                timeout_s = min(30.0, max(1.0, float(args.get("timeout_s", 20.0))))
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
            if len(buf) > 65536:
                # No delimiter in 64 KB: not a JSON-lines client. Drop it
                # before it grows the buffer for the whole 15 s timeout on a
                # 512 MB device (audit 2026-07 W-L5).
                log.warning("maint client sent %d bytes with no newline — "
                            "dropping connection", len(buf))
                return
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


# Per-connection LX200 threads are capped so a looping/garbage client can't
# spawn threads without bound. SkySafari uses 1-2; 8 is generous. Dead
# half-open connections are reaped by TCP keepalive (~25 s) and the recv
# timeout, freeing slots.
# Fixed worker pool (v0.11.58): a SkySafari readout rate of N opens a NEW TCP
# connection N times/sec (per-poll reconnect — a client-side behaviour, not
# configurable away). Thread-per-connection (v0.11.52) then spawned/tore down a
# thread that often on the Zero 2W's shared CPU 0, behind the transient
# "camera unavailable"/jitter. A fixed pool of long-lived workers draining a
# bounded queue keeps the v0.11.52 isolation (a blocking :CM# / half-open phone
# occupies one WORKER, not the accept loop) with zero per-connection thread
# churn. Pool size = the old concurrency cap; queue absorbs bursts, overflow
# sheds. See docs/lx200-connection-pool-design.md.
_LX200_POOL_WORKERS = 8
_LX200_QUEUE_MAX = 16
# Retained name for the (unchanged) 8-way concurrency ceiling the pool provides.
_LX200_MAX_CLIENTS = _LX200_POOL_WORKERS


def _serve_lx200_client(client, addr, latest_solution, shared_cfg, cfg,
                        align_request_q, align_response_q, ctx):
    """Serve one LX200 connection to completion on a pool worker (v0.11.58).

    A blocking :CM# align — or a half-open phone the old single-connection
    server would sit and wait on — occupies one worker but can no longer starve
    other clients' :GR/:GD polls (the poll-timeout -> reconnect -> broken-pipe
    storm). The per-connection handling is unchanged from the v0.11.52 threaded
    server; only the dispatch (pool + queue) differs."""
    try:
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        try:
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE,  10)
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL,  5)
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT,    3)
        except (AttributeError, OSError):
            pass
        client.settimeout(cfg.lx200_client_timeout_s)
        log.debug("LX200 client connected from %s", addr)
        # SHARED align target (v0.11.56): :Sr/:Sd on one connection must be
        # visible to a :CM# that arrives on another (SkySafari splits/reconnects).
        align_state = _lx200_align_state
        time_state  = {}
        buf = b""
        while True:
            chunk = client.recv(256)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 4096:
                # LX200 commands are tens of bytes; 4 KB with no '#' is
                # a garbage-spewing client on the network-exposed port
                # (audit 2026-07 W-L5).
                log.warning("LX200 client sent %d bytes with no '#' — "
                            "dropping connection", len(buf))
                break
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


def _serve_lx200(latest_solution, shared_cfg, cfg,
                 align_request_q, align_response_q, ctx):
    """Bind the LX200 TCP socket and serve connections from a FIXED WORKER POOL
    (v0.11.58): the accept loop only enqueues sockets; _LX200_POOL_WORKERS
    long-lived workers drain a bounded queue. A per-poll-reconnect client
    (SkySafari) no longer churns a thread per connection. See
    docs/lx200-connection-pool-design.md."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", cfg.lx200_port))
    sock.listen(16)
    work_q: Queue = Queue(maxsize=_LX200_QUEUE_MAX)

    def _worker():
        while True:
            client, addr = work_q.get()
            try:
                _serve_lx200_client(client, addr, latest_solution, shared_cfg,
                                    cfg, align_request_q, align_response_q, ctx)
            except Exception as e:
                log.warning("LX200 worker error serving %s: %s", addr, e)
                try: client.close()
                except Exception: pass
            finally:
                work_q.task_done()

    for i in range(_LX200_POOL_WORKERS):
        threading.Thread(target=_worker, name=f"lx200-worker-{i}",
                         daemon=True).start()
    log.info("LX200 server listening on :%d (%d-worker pool, queue %d)",
             cfg.lx200_port, _LX200_POOL_WORKERS, _LX200_QUEUE_MAX)

    while True:
        client, addr = sock.accept()
        _lx200_note_connection(addr)
        try:
            work_q.put_nowait((client, addr))
        except Full:
            # All workers busy AND the backlog is full — almost always stale
            # half-open connections a phone left behind (reaped by keepalive /
            # the recv timeout). Shed the newcomer rather than grow unbounded,
            # exactly as the old semaphore cap did.
            log.warning("LX200 work queue full (%d busy + %d queued); "
                        "dropping %s", _LX200_POOL_WORKERS, _LX200_QUEUE_MAX,
                        addr)
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
    # Seed the exact-prediction kill switch from the conf so a persisted
    # `imu_exact_predict: false` takes effect from boot (comms reads
    # shared_cfg, not cfg). setdefault so a live toggle isn't clobbered.
    shared_cfg.setdefault("imu_exact_predict",
                          bool(getattr(cfg, "imu_exact_predict", True)))
    # Unit C accel-tilt / gyro-scale calibration + Mode-1 correction (default
    # off). Seeded from the conf; the solver reads the same key to gate the
    # estimator, comms to gate the correction.
    shared_cfg.setdefault("imu_solve_cal_enabled",
                          bool(getattr(cfg, "imu_solve_cal_enabled", False)))
    shared_cfg.setdefault("imu_solve_cal_write_chip",
                          bool(getattr(cfg, "imu_solve_cal_write_chip", False)))
    # Reporting epoch (jnow default) — precesses J2000 -> JNow at the LX200
    # boundary. Seeded from the conf so a persisted `report_epoch: j2000`
    # (the kill switch) is honored from boot.
    shared_cfg.setdefault("report_epoch",
                          str(getattr(cfg, "report_epoch", "jnow")).lower())
    # Mount link live-mutable keys, seeded from the conf so a persisted state is
    # honored from boot (transport keys are read directly from cfg, not seeded).
    shared_cfg.setdefault("mount_enabled",
                          bool(getattr(cfg, "mount_enabled", False)))
    shared_cfg.setdefault("mount_mode",
                          str(getattr(cfg, "mount_mode", "manual")).lower())
    shared_cfg.setdefault("mount_epoch",
                          str(getattr(cfg, "mount_epoch", "jnow")).lower())

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

    threading.Thread(target=_mount_loop, args=(ctx,),
                     name="diofinder-mount", daemon=True).start()

    while True:
        try:
            _serve_lx200(latest_solution, shared_cfg, cfg,
                         align_request_q, align_response_q, ctx)
        except Exception as e:
            log.error("LX200 server crashed: %s; restarting in 2s", e)
            time.sleep(2)
