"""
Comms worker process.

Pinned to its dedicated CPU. Two server endpoints:

  1. LX200 TCP server on cfg.lx200_port (default 4060) -- talks to
     SkySafari and any other LX200 client. Handles :GR/:GD pointing
     queries and the :Sr/:Sd/:CM# boresight alignment workflow.

  2. Maintenance Unix socket at /run/efinder/maint.sock -- accepts
     newline-delimited JSON requests for inspection, calibration,
     boresight management, exposure tuning, and mode switching.
     Used by efinder-ctl and the web UI.

Available maintenance commands:
  set_test_mode {"enabled": true | false}
  status, boresight_show/set/center, calibration_status/reset,
  polar_start/status/cancel/set_latitude, exposure_get/set, gain_set,
  solver_params_get/set, solve_centroids
"""

import datetime
import itertools
import json
import logging
import math
import os
import socket
import subprocess
import threading
import time
from queue import Empty

from efinder import config as cfg_mod
from efinder.align import AlignRequest, AlignResult, CommsAlignState
from efinder.imu_math import quat_delta_rotvec
from efinder.maint import MaintRequest, MaintResponse, SOCKET_PATH
from efinder.worker_cmds import (
    SolverCmd, CameraCmd,
    SOLVER_OP_CALIBRATION_STATUS, SOLVER_OP_CALIBRATION_RESET,
    SOLVER_OP_POLAR_START, SOLVER_OP_POLAR_STATUS,
    SOLVER_OP_POLAR_CANCEL, SOLVER_OP_POLAR_SET_LATITUDE,
    SOLVER_OP_SOLVE_CENTROIDS,
    CAMERA_OP_GET_EXPOSURE, CAMERA_OP_SET_EXPOSURE, CAMERA_OP_SET_GAIN,
)

log = logging.getLogger("efinder.comms")

_request_id_seq = itertools.count(1)

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
            ["sudo", "/usr/local/bin/efinder-set-time", time_str],
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
        pred = _imu_predict(shared_cfg)
        if pred is not None:
            return _format_ra(pred[0] / 15.0).encode("ascii")
        sol = dict(latest_solution)
        return _format_ra(sol.get("ra_deg", 0.0) / 15.0).encode("ascii")
    if cmd == ":GD":
        pred = _imu_predict(shared_cfg)
        if pred is not None:
            return _format_dec(pred[1]).encode("ascii")
        sol = dict(latest_solution)
        return _format_dec(sol.get("dec_deg", 0.0)).encode("ascii")
    if cmd == ":GW":
        return b"AT2#"
    if cmd in (":GVN", ":GVP"):
        return f"eFinder {cfg.version}#".encode("ascii")
    if cmd.startswith(":GV"):
        return f"eFinder {cfg.version}#".encode("ascii")
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
            from efinder.align import _parse_dec_dms
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
            from efinder.align import _parse_dec_dms
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
                daemon=True, name="efinder-timesync",
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
            return MaintResponse(ok=True, result={"version": ctx.cfg.version})

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
            return MaintResponse(ok=True, result=reply.result)

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
                "solve_timeout_ms":    ctx.shared_cfg.get(
                    "solve_timeout_ms",    ctx.cfg.solve_timeout_ms),
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
                if not (1.0 <= sigma <= 50.0):
                    return MaintResponse(ok=False,
                                        error="detect_sigma out of range [1, 50]")
                ctx.shared_cfg["detect_sigma"] = sigma
                updates["detect_sigma"] = sigma
            if "detect_bg_mode" in args:
                mode = str(args["detect_bg_mode"]).strip().lower()
                valid_modes = ("row_percentile", "line_median", "top_hat")
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
            if persist and updates:
                cfg_mod.save_keys(updates)
            return MaintResponse(ok=True, result={**updates, "persisted": persist})

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
        level=os.environ.get("EFINDER_LOGLEVEL", "INFO"),
        format="comms %(levelname)s %(message)s",
    )
    _pin_to_cpu(cfg.cpu_comms)

    ctx = _MaintContext(
        cfg=cfg, latest_solution=latest_solution, shared_cfg=shared_cfg,
        solver_cmd_q=solver_cmd_q, solver_cmd_reply_q=solver_cmd_reply_q,
        camera_cmd_q=camera_cmd_q, camera_cmd_reply_q=camera_cmd_reply_q,
    )
    maint_thread = threading.Thread(
        target=_serve_maint_socket, args=(ctx,),
        name="efinder-maint", daemon=True)
    maint_thread.start()

    while True:
        try:
            _serve_lx200(latest_solution, shared_cfg, cfg,
                         align_request_q, align_response_q, ctx)
        except Exception as e:
            log.error("LX200 server crashed: %s; restarting in 2s", e)
            time.sleep(2)
