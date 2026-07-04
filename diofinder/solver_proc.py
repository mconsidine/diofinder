"""Solver worker process.

Pipeline: sycamore star_detect (matched_filter gate) → olive-solve tetra3-py.

Pi Zero 2W optimisations:
  * Solver process affinity is {cpu_solver, cpu_camera, cpu_solver_aux} so
    the sycamore/olive-solve rayon pools can spread work across three
    physical cores (CPUs 1, 2, 3); comms/webui live on CPU 0.
  * Frame buffer pre-allocated once with np.empty; each iteration fills
    it in-place via np.copyto, eliminating per-frame heap allocation.
  * The shared-memory slot is released immediately after np.copyto so
    camera_proc is never blocked waiting for a solve to finish.
  * target_pixel and target_sky_coord must be float64: the Rust PyO3
    binding extracts them as PyReadonlyArray2<f64>.
  * Sycamore centroids must be float64 for the same reason; the (x, y)
    tuple from detect_stars is swapped to (row, col) for tetra3.
"""

import logging
import math
import os
import time

import numpy as np

from diofinder.frame_slots import SHM_PREFIX, NUM_BUFFERS
from diofinder.align import AlignResult
from diofinder.calibration import FovCalibrator, FallbackGate
from diofinder.imu_math import (quat_delta_rotvec, quat_to_rotvec,
                                rotvec_to_quat, get_imu_qt)
from diofinder import imu_frame as _imu_frame
from diofinder.polar_run import PolarAligner
from diofinder import frame_health
from multiprocessing import shared_memory

log = logging.getLogger("diofinder.solver")

MATCH_FOUND = 1
NO_MATCH    = 2
TIMEOUT     = 3
CANCELLED   = 4
TOO_FEW     = 5

# olive-solve returns status as a Rust Debug string (e.g. "MatchFound").
# Map both PascalCase (Debug format) and SCREAMING_SNAKE_CASE (docs) to ints.
_OLIVE_STATUS = {
    "MatchFound":  MATCH_FOUND, "MATCH_FOUND":  MATCH_FOUND,
    "NoMatch":     NO_MATCH,    "NO_MATCH":     NO_MATCH,
    "Timeout":     TIMEOUT,     "TIMEOUT":      TIMEOUT,
    "Cancelled":   CANCELLED,   "CANCELLED":    CANCELLED,
    "TooFew":      TOO_FEW,     "TOO_FEW":      TOO_FEW,
}


def _solver_db_path(raw: str) -> str:
    """Expand a bare database name to an absolute .npz path.

    If raw is already an absolute path it is used as-is.
    Otherwise it is treated as a stem name relative to
    /var/lib/diofinder/ and .npz is appended.

    Examples:
        "default_database"                   -> /var/lib/diofinder/default_database.npz
        "/var/lib/diofinder/mydb.npz"          -> /var/lib/diofinder/mydb.npz
        "/opt/diofinder/data/custom_db.npz"    -> /opt/diofinder/data/custom_db.npz
    """
    if os.path.isabs(raw):
        return raw
    return f"/var/lib/diofinder/{raw}.npz"


_CAPTURE_DIR_CAP_BYTES = 100 * 1024 * 1024   # 100 MB cap on the captures dir


def _enforce_dir_cap(directory: str, cap_bytes: int) -> None:
    """Delete oldest *.png until the directory is under cap_bytes.

    Best-effort: any IO error is swallowed so capture never breaks the loop.
    """
    try:
        entries = []
        total = 0
        with os.scandir(directory) as it:
            for e in it:
                if not e.name.endswith(".png"):
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                entries.append((st.st_mtime, st.st_size, e.path))
                total += st.st_size
        if total <= cap_bytes:
            return
        entries.sort()  # oldest first
        for _mtime, size, path in entries:
            if total <= cap_bytes:
                break
            try:
                os.remove(path)
                total -= size
            except OSError:
                continue
    except OSError:
        pass


def _save_frame(frame, cfg, label: str) -> None:
    """Write a frame PNG to the captures dir, enforcing a 100 MB cap.

    Filename: {utc-timestamp}_{status}.png. Never raises — any IO error is
    logged and swallowed so the solver loop keeps running.
    """
    import datetime
    try:
        from PIL import Image
        directory = cfg.failed_frames_dir
        os.makedirs(directory, exist_ok=True)
        ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S_%f")[:-3]
        path = os.path.join(directory, f"{ts}_{label}.png")
        Image.fromarray(frame, mode="L").save(path)
        _enforce_dir_cap(directory, _CAPTURE_DIR_CAP_BYTES)
        log.info("Saved frame: %s", path)
    except Exception as e:
        log.warning("Could not save frame: %s", e)


def _empty_solution(stars=0, peak=0, noise=0.0, solve_ms=0.0, status=0):
    return {
        "stars": int(stars), "matches": 0, "peak": int(peak),
        "noise": float(noise), "solve_ms": float(solve_ms),
        "solved": False, "status": int(status),
        "epoch_monotonic": time.monotonic(),
    }


def _filled_solution(*, ra, dec, roll, fov, stars, matches,
                     peak, noise, solve_ms, status, star=None):
    sol = {
        "ra_deg": float(ra), "dec_deg": float(dec),
        "roll_deg": float(roll), "fov_deg": float(fov),
        "stars": int(stars), "matches": int(matches),
        "peak": int(peak), "noise": float(noise),
        "solve_ms": float(solve_ms), "solved": True,
        "status": int(status),
        "epoch_monotonic": time.monotonic(),
    }
    if star:
        sol["star_name"] = star["name"]
        sol["star_desig"] = star["desig"]
        sol["star_mag"] = star["mag"]
        sol["star_sep_deg"] = star["sep_deg"]
    return sol


def _drain_align_queue(q, response_q):
    latest = None
    superseded = []
    try:
        while True:
            item = q.get_nowait()
            if latest is not None:
                superseded.append(latest)
            latest = item
    except Exception:
        pass
    for old_req in superseded:
        _align_reply(response_q, old_req, False,
                     error_message="superseded by a newer sync request")
    return latest


def _align_reply(response_q, req, success, **kw):
    """Echo the request id and NEVER block: a full response queue (comms not
    draining) blocking the solver here stalled the publish epoch and tripped
    the watchdog — a restart triggered purely by queued sync spam (audit
    2026-07 W2)."""
    try:
        response_q.put_nowait(AlignResult(
            success=success,
            request_id=getattr(req, "request_id", 0),
            completed_at=time.monotonic(), **kw))
    except Exception:
        log.warning("align response queue full — result dropped")


def _drain_cmd_queue(q):
    cmds = []
    try:
        while True:
            cmds.append(q.get_nowait())
    except Exception:
        pass
    return cmds


class _SolverState:
    """Mutable holder for solver objects that out-of-band commands can swap.

    solver_t3 is reloaded in-place by set_db; hot_pixel_mask is loaded/cleared
    by the dark-capture commands. read_frame() returns a fresh copy of the
    current SHM frame for dark capture.
    """
    def __init__(self, solver_t3, cfg, db_path=None):
        self.solver_t3 = solver_t3
        self.cfg = cfg
        self.db_path = db_path   # currently-loaded database (set_db fallback)
        self.hot_pixel_mask = None
        self.read_frame = None   # set by solver_main
        # Tracking-mode observability (written by solver_main each frame, read
        # by the tracking_status maint command). Plain ints — no lock needed:
        # the GIL makes single int assignments/reads atomic and a stale read in
        # a status query is harmless.
        self.tracking_state = "FULL"
        self.frames_tracked = 0
        self.frames_full = 0
        self.tracking_recover_fail = 0
        self.tracking_solve_fail = 0
        self.tracking_enabled = bool(getattr(cfg, "tracking_enabled", False))
        # Latch so the "tetra3 extractor unavailable" fallback warns only once.
        self.tetra3_backend_warned = False


def _handle_solver_cmd(cmd, calibrator, polar,
                       solver_t3=None, cfg=None, shared_cfg=None, bg_cache=None,
                       state=None):
    from diofinder.worker_cmds import (
        SolverCmdReply,
        SOLVER_OP_CALIBRATION_STATUS, SOLVER_OP_CALIBRATION_RESET,
        SOLVER_OP_POLAR_START, SOLVER_OP_POLAR_STATUS,
        SOLVER_OP_POLAR_CANCEL, SOLVER_OP_POLAR_SET_LATITUDE,
        SOLVER_OP_SOLVE_CENTROIDS, SOLVER_OP_BG_CACHE_STATUS,
        SOLVER_OP_SET_DB, SOLVER_OP_DARK_CAPTURE,
        SOLVER_OP_HOT_PIXEL_STATUS, SOLVER_OP_HOT_PIXEL_CLEAR,
        SOLVER_OP_FRAME_GET,
        SOLVER_OP_TRACKING_STATUS,
        SOLVER_OP_AUTO_TUNE_EVAL,
    )
    try:
        if cmd.op == SOLVER_OP_SET_DB:
            if state is None:
                return SolverCmdReply(request_id=cmd.request_id, ok=False,
                                      error="solver state unavailable")
            db = str(cmd.args.get("db", "")).strip()
            if not db:
                return SolverCmdReply(request_id=cmd.request_id, ok=False,
                                      error="set_db requires 'db'")
            db_path = _solver_db_path(db)
            if not os.path.exists(db_path):
                return SolverCmdReply(request_id=cmd.request_id, ok=False,
                                      error=f"database not found: {db_path}")
            # Busy flag: the load blocks the solve loop, so the publish epoch
            # stalls — a slow SD-card load past watchdog_timeout_s (30 s) used
            # to restart the whole unit mid-switch. The watchdog skips
            # enforcement while this timestamp is fresh.
            if shared_cfg is not None:
                shared_cfg["solver_busy_t"] = time.monotonic()
            prev_db_path = None
            try:
                import tetra3 as _tetra3
                # Release the old DB BEFORE loading the new one: holding both
                # doubles database RSS on a 512 MB device (OOM-kill during a
                # Good<->Bad toggle). On a failed load we reload the previous
                # db; if even that fails, the watchdog restart reloads the
                # configured db at startup.
                prev_db_path = getattr(state, "db_path", None)
                state.solver_t3 = None
                import gc as _gc
                _gc.collect()
                new_t3 = _tetra3.Tetra3(db_path)
            except Exception as e:
                if prev_db_path:
                    try:
                        state.solver_t3 = _tetra3.Tetra3(prev_db_path)
                        log.warning("set_db failed (%s); previous db reloaded", e)
                    except Exception as e2:
                        # Double-fault: no database at all. Publishing empty
                        # solutions would keep the watchdog epoch fresh, so
                        # the old "log CRITICAL and limp on" left a zombie
                        # that extracted stars but never solved until a
                        # manual restart (audit 2026-07 W3/F1). Exit instead:
                        # systemd restarts the unit, which loads the
                        # configured db at startup.
                        log.critical("set_db failed AND previous db reload "
                                     "failed (%s / %s) — exiting so systemd "
                                     "restarts with the configured db", e, e2)
                        os._exit(1)
                return SolverCmdReply(request_id=cmd.request_id, ok=False,
                                      error=f"failed to load {db_path}: {e}")
            finally:
                if shared_cfg is not None:
                    shared_cfg["solver_busy_t"] = 0.0
            state.solver_t3 = new_t3
            state.db_path = db_path
            log.info("Solver database switched -> %s", db_path)
            return SolverCmdReply(request_id=cmd.request_id, ok=True,
                                  result={"db": db, "db_path": db_path})

        if cmd.op == SOLVER_OP_DARK_CAPTURE:
            if state is None or state.read_frame is None:
                return SolverCmdReply(request_id=cmd.request_id, ok=False,
                                      error="dark capture unavailable")
            from diofinder import hot_pixel as _hp
            n = int(cmd.args.get("frames", 16) or 16)
            n = max(2, min(64, n))
            shape = (cfg.frame_height, cfg.frame_width)
            # Mark the solver busy for the watchdog, like set_db: 64 frames x
            # 0.3 s + the median stack is ~20 s of no publishes — thin margin
            # against the 30 s default and NEGATIVE against a user-lowered
            # watchdog_timeout_s (audit 2026-07 W-L3).
            if shared_cfg is not None:
                shared_cfg["solver_busy_t"] = time.monotonic()
            _meta = {
                "sensor_mode": f"{cfg.sensor_full_width}x{cfg.sensor_full_height}",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            for _k in ("exposure_s", "gain"):
                if cmd.args.get(_k) is not None:
                    try:
                        _meta[_k] = float(cmd.args[_k])
                    except (TypeError, ValueError):
                        pass
            try:
                try:
                    mask = _hp.capture_dark_mask(state.read_frame, n, shape,
                                                 meta=_meta)
                    if _hp.implausibly_large(mask.count, shape):
                        # The mask saw light: lens not capped, or a light leak.
                        # Saving it would silently break every subsequent solve
                        # (observed: a 195k-pixel mask -> healthy star counts,
                        # zero solves). MAD-collapse on a clean capped frame is
                        # handled by MIN_THRESH_DN in hot_pixel.py, so by the
                        # time this trips the capture genuinely saw light.
                        return SolverCmdReply(
                            request_id=cmd.request_id, ok=False,
                            error=(f"dark capture flagged {mask.count} pixels "
                                   f"(~{100.0 * mask.count / (shape[0] * shape[1]):.0f}% "
                                   "of the frame) — the sensor saw light, not hot "
                                   "pixels. Cap or cover the lens completely "
                                   "(check for light leaks) and retry. "
                                   "The existing mask was left unchanged."))
                    mask.save(_hp.DEFAULT_MASK_PATH)
                    state.hot_pixel_mask = mask
                except Exception as e:
                    return SolverCmdReply(request_id=cmd.request_id, ok=False,
                                          error=f"dark capture failed: {e}")
                log.info("Hot-pixel mask captured: %d pixels from %d frames",
                         mask.count, n)
                return SolverCmdReply(request_id=cmd.request_id, ok=True, result={
                    "count": mask.count, "frames": n,
                    "path": _hp.DEFAULT_MASK_PATH})
            finally:
                if shared_cfg is not None:
                    shared_cfg["solver_busy_t"] = 0.0

        if cmd.op == SOLVER_OP_HOT_PIXEL_STATUS:
            from diofinder import hot_pixel as _hp
            m = state.hot_pixel_mask if state else None
            mtime = None
            try:
                if os.path.exists(_hp.DEFAULT_MASK_PATH):
                    mtime = os.path.getmtime(_hp.DEFAULT_MASK_PATH)
            except OSError:
                pass
            return SolverCmdReply(request_id=cmd.request_id, ok=True, result={
                "count": (m.count if m else 0),
                "loaded": m is not None,
                "mtime": mtime,
                "path": _hp.DEFAULT_MASK_PATH,
                "meta": (m.meta if m else {}),
            })

        if cmd.op == SOLVER_OP_HOT_PIXEL_CLEAR:
            from diofinder import hot_pixel as _hp
            try:
                if os.path.exists(_hp.DEFAULT_MASK_PATH):
                    os.remove(_hp.DEFAULT_MASK_PATH)
            except OSError as e:
                return SolverCmdReply(request_id=cmd.request_id, ok=False,
                                      error=f"could not delete mask: {e}")
            if state is not None:
                state.hot_pixel_mask = None
            log.info("Hot-pixel mask cleared")
            return SolverCmdReply(request_id=cmd.request_id, ok=True,
                                  result={"count": 0, "loaded": False})

        if cmd.op == SOLVER_OP_FRAME_GET:
            if state is None or state.read_frame is None:
                return SolverCmdReply(request_id=cmd.request_id, ok=False,
                                      error="frame source unavailable")
            after = int(cmd.args.get("after_seq", -1))
            res = state.read_frame(with_seq=True, after_seq=after)
            if res is None:
                return SolverCmdReply(request_id=cmd.request_id, ok=False,
                                      error="no frame published yet")
            frame, seq = res
            return SolverCmdReply(request_id=cmd.request_id, ok=True, result={
                "shape": [int(frame.shape[0]), int(frame.shape[1])],
                "seq": int(seq),
                "data": frame.tobytes(),
            })

        if cmd.op == SOLVER_OP_TRACKING_STATUS:
            if state is None:
                return SolverCmdReply(request_id=cmd.request_id, ok=True,
                                      result={"enabled": False, "state": "FULL",
                                              "frames_tracked": 0,
                                              "frames_full": 0,
                                              "recover_fail": 0})
            return SolverCmdReply(request_id=cmd.request_id, ok=True, result={
                "enabled":        bool(state.tracking_enabled),
                "state":          state.tracking_state,
                "frames_tracked": int(state.frames_tracked),
                "frames_full":    int(state.frames_full),
                "recover_fail":   int(state.tracking_recover_fail),
                "solve_fail":     int(state.tracking_solve_fail),
            })

        if cmd.op == SOLVER_OP_BG_CACHE_STATUS:
            if bg_cache is None:
                return SolverCmdReply(request_id=cmd.request_id, ok=True,
                                      result={"enabled": False, "state": "NONE"})
            return SolverCmdReply(request_id=cmd.request_id, ok=True,
                                  result=bg_cache.stats())
        if cmd.op == SOLVER_OP_SOLVE_CENTROIDS:
            if state is not None and state.solver_t3 is not None:
                solver_t3 = state.solver_t3
            if solver_t3 is None or cfg is None:
                return SolverCmdReply(request_id=cmd.request_id, ok=False,
                                      error="solver not available")
            raw = cmd.args.get("centroids") or []
            if len(raw) < cfg.min_centroids:
                return SolverCmdReply(request_id=cmd.request_id, ok=True, result={
                    "solved": False, "status": "TooFew",
                    "matches": 0, "stars": len(raw)})
            cents = np.array(raw, dtype=np.float64)
            _sc = shared_cfg if shared_cfg is not None else {}
            timeout_ms = _sc.get("solve_timeout_ms", cfg.solve_timeout_ms)
            soln = solver_t3.solve_from_centroids(
                cents, (cfg.frame_height, cfg.frame_width),
                fov_estimate=calibrator.get_fov_estimate(),
                fov_max_error=calibrator.get_fov_max_error(),
                solve_timeout=timeout_ms,
                match_threshold=float(_sc.get("match_threshold", cfg.match_threshold)),
                match_radius=float(_sc.get("match_radius", cfg.match_radius)),
                distortion=calibrator.get_distortion_estimate(),
                return_matches=False,
            )
            solved = bool(soln is not None and soln.get("RA") is not None)
            return SolverCmdReply(request_id=cmd.request_id, ok=True, result={
                "solved":  solved,
                "status":  (soln.get("status", "NoMatch") if soln else "None"),
                "matches": int(soln.get("Matches", 0) or 0) if soln else 0,
                "stars":   len(raw),
                "ra":      (soln.get("RA") if soln else None),
                "dec":     (soln.get("Dec") if soln else None),
                "fov":     (soln.get("FOV") if soln else None),
            })
        if cmd.op == SOLVER_OP_AUTO_TUNE_EVAL:
            # One extract+solve sample for the offline auto-tune sweep. Grabs
            # the current SHM frame, extracts with the caller's candidate
            # detection params (forced per-frame so the live temporal cache is
            # untouched), and solves on the resident DB. Kept to a single frame
            # so the call returns well within the solver-hang watchdog window.
            if state is not None and state.solver_t3 is not None:
                solver_t3 = state.solver_t3
            if solver_t3 is None or cfg is None or bg_cache is None:
                return SolverCmdReply(request_id=cmd.request_id, ok=False,
                                      error="solver not available")
            if state is None or state.read_frame is None:
                return SolverCmdReply(request_id=cmd.request_id, ok=False,
                                      error="frame source unavailable")
            frame = state.read_frame()
            if frame is None:
                return SolverCmdReply(request_id=cmd.request_id, ok=False,
                                      error="no frame available")
            a = cmd.args
            _mar = float(a.get("max_axis_ratio", 0.0) or 0.0)
            max_axis_ratio = float("inf") if _mar <= 0.0 else _mar
            local_peak = int(frame[::2, ::2].max())
            t_extract = time.monotonic()
            try:
                _raw = bg_cache.detect(
                    frame,
                    sigma=float(a.get("sigma", cfg.detect_sigma)),
                    bg_mode=str(a.get("bg_mode", cfg.detect_bg_mode)),
                    tophat_radius=int(a.get("tophat_radius", cfg.detect_tophat_radius)),
                    max_axis_ratio=max_axis_ratio,
                    bg_block_size=int(a.get("bg_block_size", cfg.detect_bg_block_size)),
                    uniform_filter_size=int(
                        a.get("uniform_filter_size", cfg.detect_uniform_filter_size)),
                    noise_mode=str(a.get("noise_mode", cfg.detect_noise_mode)),
                    kernel_sigma=float(a.get("kernel_sigma", cfg.detect_kernel_sigma)),
                    local_noise=bool(a.get("local_noise", cfg.detect_local_noise)),
                    force_per_frame=True,
                )
            except Exception as e:
                return SolverCmdReply(request_id=cmd.request_id, ok=False,
                                      error=f"extract failed: {type(e).__name__}: {e}")
            extract_ms = (time.monotonic() - t_extract) * 1000.0
            n_stars = len(_raw)
            min_c = a.get("min_centroids",
                          (shared_cfg or {}).get("min_centroids", cfg.min_centroids))
            if n_stars < int(min_c):
                return SolverCmdReply(request_id=cmd.request_id, ok=True, result={
                    "solved": False, "matches": 0, "stars": n_stars,
                    "peak": local_peak, "solve_ms": 0.0,
                    "extract_ms": round(extract_ms, 2)})
            # sycamore returns (x=col, y=row, ...); tetra3 wants (row, col).
            cents = (np.asarray(_raw, dtype=np.float64)[:, [1, 0]]
                     if _raw else np.empty((0, 2), dtype=np.float64))
            timeout_ms = int(a.get(
                "solve_timeout_ms",
                (shared_cfg or {}).get("solve_timeout_ms", cfg.solve_timeout_ms)))
            t_solve = time.monotonic()
            soln = solver_t3.solve_from_centroids(
                cents, (cfg.frame_height, cfg.frame_width),
                fov_estimate=calibrator.get_fov_estimate(),
                fov_max_error=calibrator.get_fov_max_error(),
                solve_timeout=timeout_ms,
                match_threshold=float(
                    (shared_cfg or {}).get("match_threshold", cfg.match_threshold)),
                match_radius=float(
                    (shared_cfg or {}).get("match_radius", cfg.match_radius)),
                distortion=calibrator.get_distortion_estimate(),
                return_matches=False,
            )
            solve_ms = (time.monotonic() - t_solve) * 1000.0
            solved = bool(soln is not None and soln.get("RA") is not None)
            return SolverCmdReply(request_id=cmd.request_id, ok=True, result={
                "solved": solved,
                "matches": int(soln.get("Matches", 0) or 0) if soln else 0,
                "stars": n_stars, "peak": local_peak,
                "solve_ms": round(solve_ms, 2),
                "extract_ms": round(extract_ms, 2)})

        if cmd.op == SOLVER_OP_CALIBRATION_STATUS:
            result = calibrator.get_status()
            # FallbackGate observability (audit 2026-07 F-L6): whether the
            # self-healing loose retry has engaged is the first question in
            # a "healthy stars, zero solves" field diagnosis.
            if state is not None and getattr(state, "fallback_gate", None):
                result["fallback"] = {
                    "streak": int(state.fallback_gate.streak),
                    "fires": int(getattr(state, "fallback_fires", 0)),
                    "last_fire_t": float(getattr(state, "last_fallback_t", 0.0)),
                }
            return SolverCmdReply(request_id=cmd.request_id, ok=True,
                                  result=result)
        if cmd.op == SOLVER_OP_CALIBRATION_RESET:
            persisted = calibrator.force_recalibrate()
            result = {"state": calibrator.state.value,
                      "persisted": bool(persisted)}
            if not persisted:
                result["warning"] = ("reset applied live but NOT persisted "
                                     "(conf write failed) — it will revert "
                                     "on restart")
            return SolverCmdReply(request_id=cmd.request_id, ok=True,
                                  result=result)
        if cmd.op == SOLVER_OP_POLAR_START:
            polar.start()
            return SolverCmdReply(request_id=cmd.request_id, ok=True,
                                  result=polar.get_status())
        if cmd.op == SOLVER_OP_POLAR_STATUS:
            return SolverCmdReply(request_id=cmd.request_id, ok=True,
                                  result=polar.get_status())
        if cmd.op == SOLVER_OP_POLAR_CANCEL:
            polar.cancel()
            return SolverCmdReply(request_id=cmd.request_id, ok=True,
                                  result=polar.get_status())
        if cmd.op == SOLVER_OP_POLAR_SET_LATITUDE:
            try:
                lat = float(cmd.args["latitude_deg"])
            except (KeyError, ValueError, TypeError) as e:
                return SolverCmdReply(
                    request_id=cmd.request_id, ok=False,
                    error=f"polar_set_latitude requires numeric latitude_deg: {e}")
            polar.set_latitude(lat)
            return SolverCmdReply(request_id=cmd.request_id, ok=True,
                                  result={"latitude_deg": lat})
        return SolverCmdReply(request_id=cmd.request_id, ok=False,
                              error=f"unknown solver op: {cmd.op!r}")
    except Exception as e:
        return SolverCmdReply(request_id=cmd.request_id, ok=False,
                              error=f"{type(e).__name__}: {e}")


def _imu_propagate_hint(last_sky_q, last_imu_q, shared_cfg):
    """Return (q_hint, uncertainty_deg) for the upcoming solve.

    Applies the IMU rotation delta since the last successful solve to the last
    known sky quaternion.  Falls back to (last_sky_q, 0.1) — a very tight
    cone reusing the exact last attitude — when the IMU is absent or stale.
    Returns (None, 0) when no previous solve is available (blind solve).
    """
    if last_sky_q is None:
        return None, 0.0
    if last_imu_q is None or not shared_cfg.get("imu_available", False):
        return last_sky_q, 0.1
    q_cur, imu_t = get_imu_qt(shared_cfg)
    if q_cur is None or time.monotonic() - imu_t > 2.0:
        return last_sky_q, 0.1

    # q_delta = q_cur * conj(q_ref)  —  rotation of the IMU since last solve
    w0, x0, y0, z0 = q_cur
    wr, xr, yr, zr = last_imu_q[0], -last_imu_q[1], -last_imu_q[2], -last_imu_q[3]
    wd = w0*wr - x0*xr - y0*yr - z0*zr
    xd = w0*xr + x0*wr + y0*zr - z0*yr
    yd = w0*yr - x0*zr + y0*wr + z0*xr
    zd = w0*zr + x0*yr - y0*xr + z0*wr
    nd = math.sqrt(wd*wd + xd*xd + yd*yd + zd*zd)
    if nd < 0.5:
        return last_sky_q, 0.1
    wd, xd, yd, zd = wd/nd, xd/nd, yd/nd, zd/nd

    # q_hint = q_delta * q_last_sky
    ws, xs, ys, zs = (float(v) for v in last_sky_q)
    wh = wd*ws - xd*xs - yd*ys - zd*zs
    xh = wd*xs + xd*ws + yd*zs - zd*ys
    yh = wd*ys - xd*zs + yd*ws + zd*xs
    zh = wd*zs + xd*ys - yd*xs + zd*ws

    angle_deg = math.degrees(2.0 * math.acos(min(1.0, abs(wd))))
    # Frame-corrected hint (quality-gated fit published by
    # _imu_update_reference): conjugate the body-frame delta into the CAMERA
    # frame so the hint points AT the truth instead of merely containing it
    # in a wide cone — the raw body-frame delta was measured on-sky landing
    # 1.76x the slew angle away. With the transform active the cone tightens
    # to 1.2x; without it the defensive 2.5x cone keeps truth inside for any
    # mounting (worst case 2x). olive-solve >= 0.1.3's blind fallback still
    # backstops both paths.
    R9 = shared_cfg.get("imu_frame_R")
    if R9:
        rx, ry, rz = quat_to_rotvec((wd, xd, yd, zd))
        wd, xd, yd, zd = rotvec_to_quat(_imu_frame.apply_rotation(R9, (rx, ry, rz)))
        # Recompose the hint with the camera-frame delta.
        wh = wd*ws - xd*xs - yd*ys - zd*zs
        xh = wd*xs + xd*ws + yd*zs - zd*ys
        yh = wd*ys - xd*zs + yd*ws + zd*xs
        zh = wd*zs + xd*ys - yd*xs + zd*ws
        uncertainty_deg = max(2.0, angle_deg * 1.2)
    else:
        uncertainty_deg = max(2.0, angle_deg * 2.5)

    return (wh, xh, yh, zh), uncertainty_deg


# Rolling IMU<->sky calibration pairs. Process-local by design: only the
# solver writes and reads them, so publishing the list through shared_cfg
# just fattened every snapshot RPC in the system (audit 2026-07 P2).
_imu_calib_pairs = []


def _imu_update_reference(shared_cfg, new_ra_deg, new_dec_deg, new_roll_deg,
                          snap=None, sky_q=None, prev_sky_q=None):
    """Record current IMU quaternion alongside the just-solved sky position.
    comms_proc uses these pairs to predict pointing between solves.

    ``snap`` (the solver loop's per-frame shared_cfg snapshot) serves all
    READS — each Manager .get() is an IPC round-trip, and this function did
    ~8 of them (plus round-tripping the 20-pair calibration list) on every
    successful solve. The solver is the only writer of every key read here,
    so its own snapshot is authoritative. Writes still go to shared_cfg.
    """
    src = snap if snap is not None else shared_cfg
    if not src.get("imu_available", False):
        return
    q_now, imu_t = get_imu_qt(src)
    if q_now is None or time.monotonic() - imu_t > 2.0:
        return
    # Previous reference from the atomic tuple (this function is its only
    # writer, so the loop snapshot is authoritative). The split imu_ref_*
    # keys are no longer published — they cost five extra Manager RPCs per
    # solve and every reader has moved to the tuple (audit 2026-07 P1).
    prev_ref = src.get("imu_ref")
    if prev_ref is not None:
        q_prev, ra_prev, dec_prev, roll_prev = prev_ref[:4]
    else:
        q_prev, ra_prev, dec_prev, roll_prev = None, None, None, 0.0
    _ref_t = time.monotonic()
    # Atomic tuple: comms' pointing prediction reads this single key, so it
    # can never observe a new quaternion paired with the previous solve's
    # RA/Dec.
    _sky_q_t = tuple(sky_q) if sky_q is not None else None
    shared_cfg["imu_ref"] = (tuple(q_now), float(new_ra_deg),
                             float(new_dec_deg), float(new_roll_deg), _ref_t,
                             _sky_q_t)
    if q_prev is None or ra_prev is None or dec_prev is None:
        return
    r_imu = quat_delta_rotvec(q_now, q_prev)
    imu_dist = math.sqrt(r_imu[0]**2 + r_imu[1]**2 + r_imu[2]**2)
    if imu_dist < 1e-6:
        return
    dec_avg_rad = math.radians((dec_prev + new_dec_deg) / 2.0)
    cos_dec = math.cos(dec_avg_rad)
    dra = new_ra_deg - ra_prev
    if dra >  180: dra -= 360
    if dra < -180: dra += 360
    dra_rad  = math.radians(dra) * cos_dec
    ddec_rad = math.radians(new_dec_deg - dec_prev)
    sky_dist = math.sqrt(dra_rad**2 + ddec_rad**2)
    if sky_dist < math.radians(0.1) or sky_dist > math.radians(15.0):
        return
    roll_rad = math.radians(roll_prev)
    cos_r, sin_r = math.cos(roll_rad), math.sin(roll_rad)
    cam_r =  dra_rad * cos_r + ddec_rad * sin_r
    cam_u = -dra_rad * sin_r + ddec_rad * cos_r
    # Solver-local: the pairs list is written and read only by this process,
    # and round-tripping 20 float tuples through shared_cfg inflated every
    # dict(shared_cfg) snapshot in the system (audit 2026-07 P2).
    pairs = _imu_calib_pairs
    # Full 3-D sky rotation vector (from consecutive SOLVED attitudes) rides
    # along with the 2-D projection: it feeds the frame-rotation fit that
    # lets the solve hint apply the IMU delta in the CAMERA frame instead of
    # the body frame (r_sky = R r_imu for a fixed mounting). Pairs without a
    # quaternion history stay 5-tuples and are skipped by that fit.
    entry = (r_imu[0], r_imu[1], r_imu[2], cam_r, cam_u)
    if sky_q is not None and prev_sky_q is not None:
        r_sky3 = quat_delta_rotvec(tuple(sky_q), tuple(prev_sky_q))
        entry = entry + (r_sky3[0], r_sky3[1], r_sky3[2])
    pairs.append(entry)
    if len(pairs) > 20:
        del pairs[:-20]
    # Batch every calibration key into ONE Manager RPC (dict.update is a
    # single proxy method call) instead of up to six individual writes per
    # moving solve (audit 2026-07 P1).
    payload = {"imu_calib_n": len(pairs)}
    if len(pairs) >= 3:
        R = np.array([[p[0], p[1], p[2]] for p in pairs])
        S = np.array([[p[3], p[4]]       for p in pairs])
        C, _, _, _ = np.linalg.lstsq(R, S, rcond=None)
        S_pred = R @ C
        ss_res = float(np.sum((S - S_pred)**2))
        ss_tot = float(np.sum((S - S.mean(axis=0))**2))
        r2 = max(0.0, 1.0 - ss_res / ss_tot) if ss_tot > 1e-15 else 0.0
        payload["imu_calib_C"]       = C.T.flatten().tolist()
        payload["imu_calib_quality"] = r2
    # IMU-body -> camera frame rotation (quality-gated Kabsch fit over the
    # 3-D pairs). Published only when demonstrably good; the hint path falls
    # back to the wide-cone body-frame behavior otherwise.
    full = [p for p in pairs if len(p) >= 8]
    if len(full) >= _imu_frame.MIN_PAIRS:
        R9, quality = _imu_frame.fit_frame_rotation(
            [p[0:3] for p in full], [p[5:8] for p in full])
        payload["imu_frame_R"] = R9
        payload["imu_frame_quality"] = (
            quality if R9 is not None else {"rejected": str(quality)})
    shared_cfg.update(payload)


def solver_main(slots, latest_solution, shared_cfg,
                align_request_q, align_response_q,
                solver_cmd_q, solver_cmd_reply_q,
                cfg):
    logging.basicConfig(
        level=os.environ.get("DIOFINDER_LOGLEVEL", "INFO"),
        format="solver %(levelname)s %(message)s",
    )

    # Allow solver threads (including rayon worker pools) to use three cores:
    # cpu_solver (primary), cpu_camera (camera_proc is I/O-bound and mostly
    # sleeping between captures), and cpu_solver_aux (freed by moving the
    # I/O-bound comms/webui onto CPU 0 with the kernel).
    solver_cpus = {cfg.cpu_solver, cfg.cpu_camera,
                   getattr(cfg, "cpu_solver_aux", 1)}
    try:
        os.sched_setaffinity(0, solver_cpus)
        log.info("Solver pinned to CPUs %s", sorted(solver_cpus))
    except Exception as e:
        log.warning("Could not set solver CPU affinity: %s", e)

    # ---- Load olive-solve --------------------------------------------------
    try:
        import tetra3 as _tetra3
        db_path = _solver_db_path(cfg.solver_db)
        solver_t3 = _tetra3.Tetra3(db_path)
        from diofinder.wheels import wheel_versions
        _wv = wheel_versions()
        log.info("olive-solve ready (wheel %s, db: %s)",
                 _wv.get("olive_solve") or "unknown", db_path)
    except Exception as e:
        log.error("Failed to load olive-solve: %s", e)
        raise RuntimeError(f"olive-solve unavailable: {e}") from e

    # ---- Load star-names catalog (optional display overlay) ----------------
    # Never fatal: a missing/bad catalog just disables brightest-star naming.
    from diofinder import star_names as _star_names_mod
    star_names = _star_names_mod.try_load(cfg.star_names_path)

    # ---- Load sycamore extractor -------------------------------------------
    try:
        import star_detect as _star_detect
        # One-time thread-count init, matched to the solver's affinity set.
        # Three cores are dedicated to this process, so a 3-thread pool does
        # not contend with comms/webui (which live on CPU 0).
        _star_detect.set_num_threads(len(solver_cpus))
        # Imported here (not at module top) so star_detect stays a runtime, not
        # import-time, dependency — this try owns the "missing wheel" error.
        # Must be bound before the log line below uses HAS_TOPHAT.
        from diofinder.bg_cache import BackgroundCache, HAS_TOPHAT, CacheState
        log.info("sycamore star_detect ready (wheel %s, top_hat support: %s)",
                 _wv.get("sycamore") or "unknown", HAS_TOPHAT)
    except ImportError as e:
        log.error("sycamore star_detect not installed: %s", e)
        raise RuntimeError("sycamore star_detect unavailable") from e

    # Temporal background cache (decision #5). Routes steady-state detection
    # through detect_stars_with_cache; falls back to per-frame on slew/warmup.
    # Disable via bg_cache_enabled if its bookkeeping proves too costly.
    bg_cache = BackgroundCache(cfg)
    bg_cache.start()
    log.info(
        "background: cache=%s bg_mode=%s tophat_radius=%d bin=%d",
        bg_cache.enabled, cfg.detect_bg_mode, cfg.detect_tophat_radius,
        cfg.detect_bin)

    # Mutable holder so out-of-band commands can swap the database and the
    # hot-pixel mask without restarting the process.
    state = _SolverState(solver_t3, cfg, db_path=db_path)

    # Load a previously-captured hot-pixel mask if present (rejects hot pixels
    # during slews when the temporal cache is offline).
    try:
        from diofinder import hot_pixel as _hp
        state.hot_pixel_mask = _hp.HotPixelMask.load(_hp.DEFAULT_MASK_PATH)
        if state.hot_pixel_mask is not None:
            log.info("Hot-pixel mask loaded: %d pixels", state.hot_pixel_mask.count)
            _mode_now = f"{cfg.sensor_full_width}x{cfg.sensor_full_height}"
            _mode_mask = state.hot_pixel_mask.meta.get("sensor_mode")
            if _mode_mask and _mode_mask != _mode_now:
                # Positions are sensor-mode dependent but the frame is always
                # 960x760, so the shape guard can't catch this (audit F-L1).
                log.warning(
                    "Hot-pixel mask was captured in sensor mode %s but the "
                    "camera runs %s — every masked pixel points at the wrong "
                    "sky position. Recapture the dark frame.",
                    _mode_mask, _mode_now)
    except Exception as e:
        log.warning("Could not load hot-pixel mask: %s", e)

    calibrator = FovCalibrator(cfg, shared_cfg)
    # Loose-window blind retry after a run of failed attempts: the escape
    # hatch for a stale committed FOV or a poisoned attitude hint, both of
    # which otherwise deadlock (the calibrator only learns from successes).
    state.fallback_fires = 0
    state.last_fallback_t = 0.0
    fallback_gate = FallbackGate(
        fail_threshold=int(getattr(cfg, "fov_fallback_fails", 20)))
    state.fallback_gate = fallback_gate
    log.info("Calibrator: state=%s fov=%.4f° tolerance=%.3f°",
             calibrator.state.value,
             calibrator.get_fov_estimate(),
             calibrator.get_fov_max_error())

    polar = PolarAligner(
        latitude_deg=cfg.latitude_deg if cfg.latitude_deg != 0.0 else None,
    )

    shms = [shared_memory.SharedMemory(name=f"{SHM_PREFIX}_{i}")
            for i in range(NUM_BUFFERS)]
    bufs = [np.ndarray((cfg.frame_height, cfg.frame_width), dtype=np.uint8,
                        buffer=s.buf) for s in shms]

    def _read_current_frame(with_seq=False, after_seq=-1):
        """Copy the most-recent published SHM frame via the FrameSlots
        protocol. Claiming the read slot means the camera cannot rewrite the
        buffer mid-copy (the old bare latest_ready read could return a torn
        frame). ``after_seq >= 0`` waits (bounded) for a frame NEWER than
        that sequence — the frame_get burst path chains it for strictly
        consecutive frames. Called from the solver-command handler, which
        runs between frames, so the loop's own read slot is never held here.
        """
        idx, seq = slots.acquire_read_slot(timeout=2.0, after_seq=after_seq)
        if idx is None or idx < 0:
            return None
        try:
            frame = np.array(bufs[idx], dtype=np.uint8, copy=True)
        finally:
            slots.release_read_slot()
        return (frame, seq) if with_seq else frame
    state.read_frame = _read_current_frame

    # ---- Pre-allocate hot-path buffers -------------------------------------
    # Reused every frame to avoid per-frame heap allocation.
    # target_pixel and target_sky_coord must be float64: the Rust PyO3
    # binding extracts them as PyReadonlyArray2<f64>.
    frame_buf    = np.empty((cfg.frame_height, cfg.frame_width), dtype=np.uint8)
    target_pixel = np.zeros((1, 2), dtype=np.float64)
    _bs_y = float(cfg.boresight_y)
    _bs_x = float(cfg.boresight_x)
    target_pixel[0, 0] = _bs_y
    target_pixel[0, 1] = _bs_x

    # IMU-seeded attitude hint state
    last_sky_q       = None   # quaternion (w,x,y,z) from last successful solve
    last_solve_imu_q = None   # IMU quaternion recorded at that same solve

    # ---- Tracking-mode state machine (experimental, opt-in) ---------------
    # Two states: FULL (full-frame detect + blind/IMU-hint solve, the shipped
    # behaviour) and TRACKING (ROI-windowed detect around the previous solved
    # centroids + tight-hint solve). Entirely skipped when tracking_enabled is
    # false: tracking_state stays FULL and the `if tracking_active:` guard below
    # is never taken, so the default path is byte-for-byte unchanged.
    from diofinder import tracking as _tracking_mod
    TRACK_FULL = "FULL"
    TRACK_TRACKING = "TRACKING"
    tracking_state      = TRACK_FULL
    tracking_good_run   = 0       # consecutive successful solves (for lock-in)
    tracking_prev_xy    = []      # (x,y) full-frame predictions for next frame
    frames_tracked      = 0       # counters for tracking_status
    frames_full         = 0
    tracking_recover_fail = 0
    tracking_solve_fail   = 0     # verify/solve failures while TRACKING (F3)
    tracking_fail_episodes = 0    # drives the relock backoff (audit F3)
    tracking_run_len      = 0     # consecutive tracked frames this episode
    # Capability-probe: only pass strict_hint to the solver when it accepts it.
    try:
        import inspect as _inspect
        _solve_params = _inspect.signature(
            solver_t3.solve_from_centroids).parameters
        SOLVER_HAS_STRICT_HINT = "strict_hint" in _solve_params
    except (TypeError, ValueError):
        SOLVER_HAS_STRICT_HINT = True  # builtin without signature; current API has it
    # olive-solve >= 0.1.6: true verify-only entry point (catalog projected
    # through a known attitude, matched, refined — no 4-star pattern hashing).
    # Probed on the class, which survives set_db instance swaps.
    SOLVER_HAS_VERIFY = hasattr(solver_t3, "verify_attitude")
    # sycamore >= 0.14: native batched window-list ROI detection (one GIL
    # round-trip for all tracking windows instead of one per window).
    EXTRACT_HAS_ROI = bool(getattr(_star_detect, "HAS_ROI", False))
    log.info("tracking fast paths: verify_attitude=%s detect_stars_roi=%s",
             SOLVER_HAS_VERIFY, EXTRACT_HAS_ROI)

    fail_streak = 0
    dark_streak = 0
    camera_stale_streak = 0
    fallback_fires = 0        # loose-retry fires (audit F2 escalation cadence)
    pending_gate_fire = False # gate fired on a raise path; retry next attempt
    solve_count = 0
    frame_seq = -1   # last frame sequence processed; gates re-work on stale frames
    _last_health_t = 0.0      # throttle for the exposure/contrast health warning
    _last_health_warn = False

    try:
        while True:
            # Drain out-of-band solver commands (calibration, polar, etc.)
            for cmd in _drain_cmd_queue(solver_cmd_q):
                reply = _handle_solver_cmd(cmd, calibrator, polar,
                                           solver_t3=state.solver_t3, cfg=cfg,
                                           shared_cfg=shared_cfg, bg_cache=bg_cache,
                                           state=state)
                try:
                    solver_cmd_reply_q.put_nowait(reply)
                except Exception as e:
                    log.warning("Could not enqueue solver reply: %s", e)

            # New-frame gate: block until the camera publishes a frame newer
            # than the one already handled, so the solver doesn't burn CPU
            # re-extracting and re-solving an identical frame when a solve
            # finishes faster than the exposure-limited frame period. On a
            # camera stall the 5 s timeout still returns the current frame so
            # command housekeeping keeps running — but a sustained stall now
            # freezes the publish epoch (below) instead of masquerading as a
            # live camera.
            prev_seq = frame_seq
            idx, frame_seq = slots.acquire_read_slot(timeout=5.0, after_seq=frame_seq)
            t0  = time.monotonic()

            # Camera-stall detection (audit 2026-07 W1): the timeout path
            # above returns the CURRENT frame with an unchanged seq. A
            # blocking (non-raising) capture stall therefore shows up as
            # consecutive same-seq returns — the only place in the system
            # that can see it. After ~30 s stop publishing so the watchdog
            # epoch goes stale and systemd restarts the unit, instead of
            # re-solving the frozen frame forever and serving stale pointing
            # as live. Threshold: 6 x 5 s timeouts; even a 10 s exposure
            # produces a new frame every frame period, so a healthy slow
            # camera never accumulates more than ~2.
            if prev_seq >= 0 and frame_seq == prev_seq:
                camera_stale_streak += 1
                if camera_stale_streak >= 6:
                    if (camera_stale_streak == 6
                            or camera_stale_streak % 12 == 0):
                        log.critical(
                            "camera appears stalled: no new frame for ~%.0f s "
                            "(seq stuck at %d) — freezing the publish epoch "
                            "so the watchdog restarts the unit",
                            camera_stale_streak * 5.0, frame_seq)
                    slots.release_read_slot()
                    continue
            else:
                camera_stale_streak = 0

            # Subsampled peak: the <20 gate is about overall illumination and
            # the >=250 saturation check (auto-exposure) is about regions, not
            # single pixels; a 2x2 stride scans 1/4 the data and still sees any
            # feature 2 px wide.
            local_peak = int(bufs[idx][::2, ::2].max())

            if local_peak < 20:
                slots.release_read_slot()
                # Publish the FIRST dark frame immediately (UI flips state
                # promptly), then every 5th: dark frames carry no information
                # beyond "alive", and at the 0.05 s exposure floor the
                # unconditional publish was 20 Manager RPCs/s (audit 2026-07
                # P11). Worst-case epoch cadence 5 x exposure = 5 s at a 1 s
                # exposure — far inside the 30 s watchdog window.
                if dark_streak % 5 == 0:
                    latest_solution.update(_empty_solution(peak=local_peak))
                dark_streak += 1
                continue
            dark_streak = 0

            # Drain the align queue only for frames that will actually be
            # processed: draining before the dark gate consumed a pending
            # :CM# on a dark frame (mid-slew / cloud / exposure step) and
            # silently dropped it — SkySafari then blocked for the full 15 s
            # alignment timeout. Requests now stay queued until a bright frame.
            align_req  = _drain_align_queue(align_request_q, align_response_q)

            # ONE Manager IPC round-trip for all per-frame knob reads: every
            # snap.get() is a pickled unix-socket RPC to the Manager
            # process on CPU 0; this loop previously issued ~30 per frame —
            # comparable to the entire extraction budget on a Pi Zero 2W.
            # Reads below use the snapshot; writes still go to shared_cfg.
            snap = dict(shared_cfg)

            # Update boresight target in-place only when it has changed.
            new_bs_y = snap.get("boresight_y", cfg.boresight_y)
            new_bs_x = snap.get("boresight_x", cfg.boresight_x)
            if new_bs_y != _bs_y or new_bs_x != _bs_x:
                _bs_y, _bs_x = new_bs_y, new_bs_x
                target_pixel[0, 0] = _bs_y
                target_pixel[0, 1] = _bs_x

            # Snapshot for optional diagnostics save.
            frame_snapshot = (
                np.copy(bufs[idx])
                if (cfg.save_failed_frames or cfg.save_solved_frames)
                else None
            )

            # Copy frame into pre-allocated buffer, then release slot
            # immediately so camera_proc is never blocked by solve latency.
            np.copyto(frame_buf, bufs[idx])
            slots.release_read_slot()

            # Exposure/contrast health: flag a crushed/clipped histogram (the
            # detection-throttling condition no extractor can fix). Throttled to
            # ~10 s and assessed on a 2x2-subsampled view (1/4 the data) so the
            # hot path is untouched; logged only on a False->True transition so
            # it never spams. The fix is exposure/gain (auto-exposure) or the
            # libcamera black level, not anything in the solver.
            now_h = time.monotonic()
            if now_h - _last_health_t > 10.0:
                _last_health_t = now_h
                _h = frame_health.assess(frame_buf[::2, ::2])
                if _h["warn"] and not _last_health_warn:
                    log.warning(
                        "frame exposure/contrast crushed: %s — raise exposure/gain "
                        "or lower the libcamera black level (rpi.black_level); "
                        "faint stars are likely quantization-limited", _h["msg"])
                _last_health_warn = _h["warn"]

            # Hot-pixel repair: replace masked pixels with their 8-neighbor
            # mean before detection. Vectorized; < 1 ms for a few hundred
            # pixels. No-op when no mask is loaded.
            if state.hot_pixel_mask is not None:
                try:
                    state.hot_pixel_mask.repair(frame_buf)
                except Exception as e:
                    log.warning("hot-pixel repair failed: %s", e)

            # Feed the temporal background worker (copies internally) and let it
            # track slew via the IMU. No-ops when the cache is disabled.
            extractor_backend = str(
                snap.get("extractor_backend",
                         getattr(cfg, "extractor_backend", "sycamore")))
            # Legacy (tetra3) extraction never consumes the temporal cache:
            # skip the ~0.7 MB per-frame copy and the worker's ~100 ms
            # GIL-holding median rebuilds while that backend is active. The
            # cache refills within one stack (8 frames) after switching back.
            if extractor_backend != "tetra3":
                bg_cache.submit_frame(frame_buf)
            _imu_q_snap, _imu_t_snap = get_imu_qt(snap)
            if (snap.get("imu_available", False)
                    and time.monotonic() - _imu_t_snap < 2.0):
                # Staleness gate (audit 2026-07 W5): an IMU wedged in a kernel
                # I2C read leaves imu_available=True with a FROZEN quaternion.
                # Feeding that to note_motion means slews are never detected
                # AND the fail-streak invalidation stays disabled ("IMU-less
                # only") — a stale background model then suppresses the new
                # field's stars on every slew, unlogged. The 2 s freshness
                # window matches the hint/prediction gates.
                bg_cache.note_motion(_imu_q_snap)
            else:
                # Un-latch _imu_feeding so solver-derived slew detection and
                # the fail-streak invalidation take over if the IMU dies
                # mid-session (previously latched forever after the first
                # note_motion).
                bg_cache.note_imu_lost()
            # Exposure/gain changed? Old-setting frames poison the stack: flush
            # and rebuild at the new setting (adopt-first, so a solver restart
            # never invalidates a healthy state).
            bg_cache.note_camera_settings(
                snap.get("camera_settings_epoch"))

            # float64: Rust extracts target_sky_coord as PyReadonlyArray2<f64>
            target_sky = None
            if align_req is not None:
                target_sky = np.array(
                    [[align_req.target_ra_deg, align_req.target_dec_deg]],
                    dtype=np.float64)

            q_hint, hint_unc = _imu_propagate_hint(
                last_sky_q, last_solve_imu_q, snap)

            # --- Step 1: extract centroids (sycamore, matched_filter) --------
            # Background handling (per-row floor, top-hat, or temporal cache) is
            # routed by BackgroundCache.detect; all knobs are live-overridable
            # via shared_cfg, falling back to the config defaults.
            t_extract = time.monotonic()
            sigma = snap.get("detect_sigma", cfg.detect_sigma)
            bg_mode = snap.get("detect_bg_mode", cfg.detect_bg_mode)
            tophat_radius = int(
                snap.get("detect_tophat_radius", cfg.detect_tophat_radius))
            bg_block_size = int(
                snap.get("detect_bg_block_size", cfg.detect_bg_block_size))
            uniform_filter_size = int(
                snap.get("detect_uniform_filter_size", cfg.detect_uniform_filter_size))
            noise_mode = snap.get("detect_noise_mode", cfg.detect_noise_mode)
            kernel_sigma = float(
                snap.get("detect_kernel_sigma", cfg.detect_kernel_sigma))
            # 0 disables trail rejection -> infinite axis ratio.
            _mar = float(
                snap.get("detect_max_axis_ratio", cfg.detect_max_axis_ratio))
            max_axis_ratio = float("inf") if _mar <= 0.0 else _mar
            local_noise = bool(
                snap.get("detect_local_noise", cfg.detect_local_noise))

            # --- Tracking-mode gate ------------------------------------------
            # Decide whether THIS frame is served by ROI tracking. The whole
            # block is skipped when tracking_enabled is false (default): the
            # state machine never leaves FULL and tracking_active stays False,
            # so the extraction path below is the original full-frame detect.
            tracking_on = bool(
                snap.get("tracking_enabled", cfg.tracking_enabled))
            track_window = int(
                snap.get("tracking_window_px", cfg.tracking_window_px))
            track_min_recover = int(
                snap.get("tracking_min_recover", cfg.tracking_min_recover))
            track_lock_frames = int(cfg.tracking_lock_frames)
            if not tracking_on:
                # Disabled at runtime mid-session: reset the machine to FULL so
                # re-enabling starts from a clean lock-in.
                tracking_state = TRACK_FULL
                tracking_good_run = 0
                tracking_prev_xy = []
            # A solver-detected/IMU slew makes the previous positions stale.
            try:
                _slewing = bg_cache.state() is CacheState.SLEWING
            except Exception:
                _slewing = False
            tracking_active = (
                tracking_on
                and tracking_state == TRACK_TRACKING
                and not _slewing
                and len(tracking_prev_xy) >= track_min_recover
            )
            state.tracking_state = tracking_state

            def _window_detect(win_u8):
                """Per-window detector for roi_detect: same params as the
                full-frame path, routed through bg_cache (which falls to the
                per-frame path for a non-matching window shape — exactly what
                we want for a small ROI). Returns the raw (x,y,bri,peak) list."""
                return bg_cache.detect(
                    win_u8,
                    sigma=sigma,
                    bg_mode=bg_mode,
                    tophat_radius=tophat_radius,
                    max_axis_ratio=max_axis_ratio,
                    bg_block_size=bg_block_size,
                    uniform_filter_size=uniform_filter_size,
                    noise_mode=noise_mode,
                    kernel_sigma=kernel_sigma,
                    local_noise=local_noise,
                )

            served_by_tracking = False
            try:
                if tracking_active:
                    # ROI-windowed detection around last frame's solved stars.
                    # NOTE (v1): predictions are the previous successful frame's
                    # centroid (x,y) with NO IMU/sidereal shift — sub-pixel drift
                    # between frames at this cadence is < 1 px, well inside the
                    # window. A slew is caught by the _slewing fallback above.
                    _track_max = int(
                        snap.get("max_solve_stars", cfg.max_solve_stars))
                    if EXTRACT_HAS_ROI:
                        # Native window-list path (sycamore >= 0.14): the full
                        # frame + all windows cross into Rust once; per-window
                        # line_median floor + whole-window MAD noise built in.
                        def _roi_fn(fr, windows):
                            return _star_detect.detect_stars_roi(
                                fr, windows,
                                sigma=float(sigma),
                                kernel_sigma=float(kernel_sigma),
                                local_noise=bool(local_noise),
                                max_axis_ratio=float(max_axis_ratio),
                            )
                        _roi_stars, _n_hit = _tracking_mod.roi_detect_native(
                            frame_buf, tracking_prev_xy, track_window,
                            _roi_fn, max_stars=_track_max)
                    else:
                        _roi_stars, _n_hit = _tracking_mod.roi_detect(
                            frame_buf,
                            tracking_prev_xy,
                            track_window,
                            _window_detect,
                            bin=int(cfg.detect_bin),
                            max_stars=_track_max,
                        )
                    if len(_roi_stars) >= track_min_recover:
                        served_by_tracking = True
                        centroids = np.array(
                            [[s[1], s[0]] for s in _roi_stars], dtype=np.float64)
                    else:
                        # Not enough recovered -> fall back to FULL this frame
                        # and drop the lock so we re-acquire blind.
                        tracking_recover_fail += 1
                        tracking_fail_episodes += 1
                        tracking_run_len = 0
                        tracking_state = TRACK_FULL
                        tracking_good_run = 0
                        tracking_prev_xy = []

                if not served_by_tracking:
                    _t3 = getattr(state, "solver_t3", None) if state else None
                    use_tetra3 = (
                        extractor_backend == "tetra3"
                        and _t3 is not None
                        and hasattr(_t3, "get_centroids_from_image_fast"))
                    if extractor_backend == "tetra3" and not use_tetra3:
                        # Requested but unavailable (olive-solve wheel built
                        # without the extractor feature) -> fall back to
                        # sycamore, but only warn once to avoid log spam.
                        if not state.tetra3_backend_warned:
                            log.warning(
                                "extractor_backend=tetra3 requested but the "
                                "olive-solve extractor is unavailable; using "
                                "sycamore. Rebuild the wheel with --features "
                                "extractor.")
                            state.tetra3_backend_warned = True
                    if use_tetra3:
                        # AstroKeith's exact extractor: tetra3
                        # get_centroids_from_image (no matched filter, no
                        # temporal cache, no bg_cache). Returns [y, x] = (row,
                        # col) already, so NO axis swap (unlike sycamore).
                        sigma_mode = ("global_root_square"
                                      if noise_mode == "global_rms"
                                      else "local_median_abs")
                        _opts = dict(
                            downsample=1,
                            sigma=float(sigma),
                            filtsize=int(uniform_filter_size) or 25,
                            bg_sub_mode="local_mean",
                            sigma_mode=sigma_mode,
                            binary_open=True,
                            min_area=5,
                            max_area=100,
                        )
                        if max_axis_ratio != float("inf"):
                            _opts["max_axis_ratio"] = max_axis_ratio
                        _yx = _t3.get_centroids_from_image_fast(
                            frame_buf, **_opts)
                        centroids = (
                            np.asarray(_yx, dtype=np.float64)
                            if _yx is not None and len(_yx) else None)
                    else:
                        _raw = bg_cache.detect(
                            frame_buf,
                            sigma=sigma,
                            bg_mode=bg_mode,
                            tophat_radius=tophat_radius,
                            max_axis_ratio=max_axis_ratio,
                            bg_block_size=bg_block_size,
                            uniform_filter_size=uniform_filter_size,
                            noise_mode=noise_mode,
                            kernel_sigma=kernel_sigma,
                            local_noise=local_noise,
                        )
                        # sycamore returns (x=col, y=row, brightness, peak).
                        # tetra3 solve_from_centroids expects (row, col) = (y, x).
                        # Must be float64: Rust PyO3 binding rejects float32.
                        centroids = (
                            np.asarray(_raw, dtype=np.float64)[:, [1, 0]]
                            if _raw else None
                        )
            except Exception as e:
                if fail_streak == 0 or (fail_streak + 1) % 20 == 0:
                    log.warning("centroid extraction raised: %s", e)
                latest_solution.update(_empty_solution(peak=local_peak))
                if align_req is not None:
                    _align_reply(align_response_q, align_req, False,
                                 error_message=f"centroid extraction raised: {e}")
                fail_streak += 1
                # Same failure bookkeeping as a NoMatch: without it an
                # exception-class failure (e.g. a poisoned cache model) never
                # drove the fail-streak cache invalidation or dropped the
                # tracking lock, making it self-sustaining.
                bg_cache.note_solve_result(None, False)
                tracking_state = TRACK_FULL
                tracking_good_run = 0
                tracking_prev_xy = []
                continue

            extract_ms = (time.monotonic() - t_extract) * 1000.0
            n_stars    = len(centroids) if centroids is not None else 0
            if served_by_tracking:
                frames_tracked += 1
            else:
                frames_full += 1
            # Mirror live tracking observability into state for tracking_status.
            state.tracking_enabled = tracking_on
            state.frames_tracked = frames_tracked
            state.frames_full = frames_full
            state.tracking_recover_fail = tracking_recover_fail
            state.tracking_solve_fail = tracking_solve_fail

            # --- Step 2: star-count gate -------------------------------------
            min_c = snap.get("min_centroids", cfg.min_centroids)
            if n_stars < min_c:
                latest_solution.update(_empty_solution(
                    stars=n_stars, peak=local_peak,
                    solve_ms=extract_ms, status=TOO_FEW))
                if align_req is not None:
                    _align_reply(align_response_q, align_req, False,
                                 error_message=f"too few stars ({n_stars}<{min_c})")
                fail_streak += 1
                if served_by_tracking:
                    tracking_solve_fail += 1
                    tracking_fail_episodes += 1
                    tracking_run_len = 0
                if fail_streak == 1 or fail_streak % 20 == 0:
                    log.info(
                        "no solve: too few stars (%d<%d) peak=%d ext=%.0fms",
                        n_stars, min_c, local_peak, extract_ms)
                if cfg.save_failed_frames and frame_snapshot is not None:
                    _save_frame(frame_snapshot, cfg, "failed_TooFew")
                bg_cache.note_solve_result(None, False)
                # Lost the lock: drop to FULL and re-acquire.
                tracking_state = TRACK_FULL
                tracking_good_run = 0
                tracking_prev_xy = []
                continue

            # --- Step 3: centroid cap ----------------------------------------
            max_c = snap.get("max_solve_stars", cfg.max_solve_stars)
            if n_stars > max_c:
                centroids = centroids[:max_c]

            # --- Step 4: plate solve -----------------------------------------
            t_solve = time.monotonic()
            match_threshold = float(
                snap.get("match_threshold", cfg.match_threshold))
            match_radius = float(
                snap.get("match_radius", cfg.match_radius))
            # In TRACKING the search is constrained: with olive-solve >= 0.1.6
            # the centroids go through verify_attitude (catalog projected
            # through last_sky_q, matched, refined — the pattern hash is
            # skipped entirely; a NoMatch drops the lock exactly like a failed
            # solve). On older wheels: reuse last_sky_q with a tight cone and
            # strict_hint=True. In FULL keep the existing blind /
            # IMU-propagated hint (strict_hint=False). strict_hint is passed
            # only when the installed solver accepts it (capability-probed).
            use_verify = (served_by_tracking and last_sky_q is not None
                          and SOLVER_HAS_VERIFY)
            if served_by_tracking and last_sky_q is not None:
                solve_q_hint = last_sky_q
                solve_hint_unc = 2.0       # deg — tight cone around last solve
                solve_strict = True
            else:
                solve_q_hint = q_hint
                solve_hint_unc = hint_unc
                solve_strict = False
                if fail_streak >= 5 and solve_q_hint is not None:
                    # A failing run with a hint in force: the hint (stale
                    # anchor / body-frame delta) may be what's excluding the
                    # match — drop it and solve blind until a success
                    # re-anchors it.
                    solve_q_hint, solve_hint_unc = None, 0.0
            _solve_kw = dict(
                fov_estimate=calibrator.get_fov_estimate(),
                fov_max_error=calibrator.get_fov_max_error(snap),
                solve_timeout=snap.get(
                    "solve_timeout_ms", cfg.solve_timeout_ms),
                match_threshold=match_threshold,
                match_radius=match_radius,
                distortion=calibrator.get_distortion_estimate(),
                target_pixel=target_pixel,
                target_sky_coord=target_sky,
                return_matches=False,
                attitude_hint=solve_q_hint,
                hint_uncertainty_deg=solve_hint_unc,
            )
            if SOLVER_HAS_STRICT_HINT:
                _solve_kw["strict_hint"] = solve_strict
            try:
                if use_verify:
                    # Verify-only fast path: no pattern search. The hint/
                    # window/timeout knobs are meaningless here (nothing to
                    # bound), so pass only what verification consumes.
                    soln = state.solver_t3.verify_attitude(
                        centroids,
                        (cfg.frame_height, cfg.frame_width),
                        tuple(float(v) for v in last_sky_q),
                        fov_estimate=calibrator.get_fov_estimate(),
                        match_threshold=match_threshold,
                        match_radius=match_radius,
                        distortion=calibrator.get_distortion_estimate(),
                        target_pixel=target_pixel,
                        target_sky_coord=target_sky,
                        return_matches=False,
                    )
                else:
                    soln = state.solver_t3.solve_from_centroids(
                        centroids,
                        (cfg.frame_height, cfg.frame_width),
                        **_solve_kw,
                    )
            except Exception as e:
                if fail_streak == 0 or (fail_streak + 1) % 20 == 0:
                    log.warning("solve_from_centroids raised: %s", e)
                latest_solution.update(_empty_solution(
                    stars=n_stars, peak=local_peak,
                    solve_ms=extract_ms, status=NO_MATCH))
                if align_req is not None:
                    _align_reply(align_response_q, align_req, False,
                                 error_message=f"solver raised: {e}")
                fail_streak += 1
                if served_by_tracking:
                    # A tracked verify/solve failure says "the tight attitude
                    # check failed", not "the FOV window excludes reality" —
                    # keep it out of the FallbackGate streak (audit F3); the
                    # next frame re-acquires with the FULL solver anyway.
                    tracking_solve_fail += 1
                    tracking_fail_episodes += 1
                    tracking_run_len = 0
                else:
                    # A raise counts toward the loose retry like a NoMatch.
                    # The fire signal can't run the retry here (no solution
                    # flow), so carry it to the next attempt — otherwise an
                    # exception-class failure streak (NaN centroid, wheel API
                    # mismatch) never triggers the escape hatch (audit F5).
                    if fallback_gate.note_failure():
                        pending_gate_fire = True
                bg_cache.note_solve_result(None, False)
                tracking_state = TRACK_FULL
                tracking_good_run = 0
                tracking_prev_xy = []
                continue

            solve_only_ms = (time.monotonic() - t_solve) * 1000.0
            elapsed_ms    = extract_ms + solve_only_ms
            # Status is a Rust Debug string; RA presence is the reliable
            # success indicator.
            status_str = soln.get("status", "NoMatch")
            status_int = _OLIVE_STATUS.get(status_str, NO_MATCH)
            solve_count += 1

            gate_fired = False
            if soln.get("RA") is None and not served_by_tracking:
                gate_fired = fallback_gate.note_failure() or pending_gate_fire
                pending_gate_fire = False
            if gate_fired:
                # Loose blind retry: same centroids, the LOOSE FOV window and
                # no attitude hint. One call escapes both self-sustaining
                # failure modes — a committed FOV that excludes reality
                # (calibrator starves: it only learns from successes) and a
                # poisoned hint (wheels without the blind-fallback pass).
                fallback_fires += 1
                state.fallback_fires = fallback_fires
                state.last_fallback_t = time.monotonic()
                retry_kw = dict(_solve_kw)
                if fallback_fires % 3 == 0:
                    # Every 3rd fire, escalate to a FULLY blind FOV search:
                    # the loose window is only +/-fov_max_error_deg (0.3 deg)
                    # around the SAME committed estimate, which can never
                    # recover from a lens change (13.5 -> 6.8 deg swaps every
                    # solve outside it forever, and force_recalibrate needs a
                    # SUCCESS to fire). fov_estimate=None lets olive-solve
                    # search the database's full FOV range (audit 2026-07 F2).
                    retry_kw["fov_estimate"] = None
                    retry_kw["fov_max_error"] = None
                    log.warning(
                        "fallback fire #%d: escalating to full-range blind "
                        "FOV search", fallback_fires)
                else:
                    retry_kw["fov_max_error"] = max(
                        float(snap.get("fov_max_error_deg",
                                             cfg.fov_max_error_deg)),
                        float(retry_kw.get("fov_max_error") or 0.0))
                retry_kw.pop("attitude_hint", None)
                retry_kw.pop("hint_uncertainty_deg", None)
                if SOLVER_HAS_STRICT_HINT:
                    retry_kw["strict_hint"] = False
                try:
                    retry_soln = state.solver_t3.solve_from_centroids(
                        centroids, (cfg.frame_height, cfg.frame_width),
                        **retry_kw)
                except Exception as e:
                    log.warning("loose fallback solve raised: %s", e)
                    retry_soln = None
                if retry_soln is not None and retry_soln.get("RA") is not None:
                    fov_r = retry_soln.get("FOV")
                    est   = calibrator.get_fov_estimate()
                    tight = float(getattr(cfg, "fov_calibrated_max_error_deg",
                                          0.1))
                    log.warning(
                        "loose blind fallback solved after %d failed attempts "
                        "(FOV %.3f vs estimate %.3f, tight window ±%.2f)",
                        fallback_gate.streak, fov_r if fov_r else -1.0,
                        est, tight)
                    if (fov_r is not None and calibrator.use_tight_tolerance
                            and abs(fov_r - est) > tight):
                        # The committed calibration excludes reality: drop it
                        # and relearn (subsequent frames solve loose until the
                        # rolling window recommits the true value).
                        calibrator.force_recalibrate()
                    soln = retry_soln
                    status_str = soln.get("status", "MatchFound")
                    status_int = _OLIVE_STATUS.get(status_str, MATCH_FOUND)
                    solve_only_ms = (time.monotonic() - t_solve) * 1000.0
                    elapsed_ms    = extract_ms + solve_only_ms

            if soln.get("RA") is None:
                latest_solution.update(_empty_solution(
                    stars=n_stars, peak=local_peak,
                    solve_ms=elapsed_ms, status=status_int))
                if align_req is not None:
                    _align_reply(align_response_q, align_req, False,
                                 error_message=f"no match (status={status_str})")
                fail_streak += 1
                if fail_streak == 1 or fail_streak % 20 == 0:
                    log.info(
                        "no solve: status=%s stars=%d peak=%d "
                        "t=%.0fms (ext=%.0fms slv=%.0fms)",
                        status_str, n_stars, local_peak,
                        elapsed_ms, extract_ms, solve_only_ms)
                if cfg.save_failed_frames and frame_snapshot is not None:
                    _save_frame(frame_snapshot, cfg, f"failed_{status_str}")
                bg_cache.note_solve_result(None, False)
                if served_by_tracking:
                    tracking_solve_fail += 1
                    tracking_fail_episodes += 1
                    tracking_run_len = 0
                tracking_state = TRACK_FULL
                tracking_good_run = 0
                tracking_prev_xy = []
                continue

            fallback_gate.note_success()
            measured_fov        = soln.get("FOV") or calibrator.get_fov_estimate()
            measured_distortion = soln.get("distortion") or 0.0
            calibrator.update_from_solve(measured_fov, measured_distortion)
            polar.update_from_solve(soln["RA"], soln["Dec"])

            # Update attitude hint state for next frame
            q_solved = soln.get("quaternion")
            prev_sky_q_for_ref = None
            if q_solved is not None:
                prev_sky_q_for_ref = last_sky_q    # previous solve's attitude
                last_sky_q       = tuple(q_solved)
                last_solve_imu_q = get_imu_qt(snap)[0]
            # --- Tracking-mode bookkeeping (success) -------------------------
            # Remember this frame's centroid (x,y) for next frame's ROI windows
            # and advance the lock-in counter. After tracking_lock_frames
            # consecutive solves (with tracking enabled) enter TRACKING. When
            # tracking is disabled this still runs but tracking_state is forced
            # to FULL above, so it only maintains counters cheaply.
            if tracking_on:
                tracking_prev_xy = _tracking_mod.centroids_to_xy(centroids)
                tracking_good_run += 1
                if served_by_tracking:
                    tracking_run_len += 1
                    if tracking_run_len >= 20:
                        # A sustained healthy episode clears the backoff.
                        tracking_fail_episodes = 0
                # Relock backoff (audit F3): without it a systematic verify
                # failure produced 3 good solves -> lock -> 1 failed tracked
                # frame -> repeat forever, dropping one pointing frame in
                # four invisibly. Each failed episode doubles the
                # consecutive-solves requirement (capped at 8x).
                _lock_needed = track_lock_frames * (
                    2 ** min(tracking_fail_episodes, 3))
                if (tracking_state == TRACK_FULL
                        and tracking_good_run >= _lock_needed
                        and len(tracking_prev_xy) >= track_min_recover):
                    tracking_state = TRACK_TRACKING
            else:
                tracking_prev_xy = []
                tracking_good_run = 0

            # Solver-derived cache invalidation (IMU-less safety net): a solved
            # attitude jump signals an unsensed slew so the temporal background
            # is rebuilt rather than served stale.
            bg_cache.note_solve_result(q_solved, True)

            ra_target  = soln.get("RA_target")
            dec_target = soln.get("Dec_target")
            if ra_target is None or dec_target is None:
                ra_out  = soln["RA"]
                dec_out = soln["Dec"]
            else:
                ra_out  = ra_target[0]  if hasattr(ra_target,  "__len__") else ra_target
                dec_out = dec_target[0] if hasattr(dec_target, "__len__") else dec_target

            n_matches = soln.get("Matches", 0)
            # Name the "centered star" (display only). Default: the BRIGHTEST
            # cataloged star within star_name_radius_deg of the boresight
            # (falling back to plain nearest when none is that close) — the
            # notable star beats a faint catalog entry a hair closer. The
            # expert toggle star_name_brightest=false reverts to pure nearest.
            star = None
            if star_names is not None:
                try:
                    brightest = bool(snap.get(
                        "star_name_brightest",
                        getattr(cfg, "star_name_brightest", True)))
                    if brightest:
                        star = star_names.brightest_within(
                            ra_out, dec_out,
                            float(getattr(cfg, "star_name_radius_deg", 2.0)))
                    if star is None:
                        star = star_names.nearest(ra_out, dec_out, measured_fov)
                except Exception as e:
                    log.debug("Star-name lookup failed: %s", e)
            latest_solution.update(_filled_solution(
                ra=ra_out, dec=dec_out,
                roll=soln.get("Roll", 0.0), fov=measured_fov,
                stars=n_stars, matches=n_matches,
                peak=local_peak, noise=0.0,
                solve_ms=elapsed_ms, status=MATCH_FOUND,
                star=star,
            ))
            _imu_update_reference(
                shared_cfg, ra_out, dec_out, soln.get("Roll", 0.0), snap=snap,
                sky_q=(tuple(q_solved) if q_solved is not None else None),
                prev_sky_q=prev_sky_q_for_ref)

            if align_req is not None:
                xt = soln.get("x_target")
                yt = soln.get("y_target")
                if xt is None or yt is None:
                    _align_reply(align_response_q, align_req, False,
                                 error_message="no x_target/y_target in solution")
                else:
                    x = xt[0] if hasattr(xt, "__len__") else xt
                    y = yt[0] if hasattr(yt, "__len__") else yt
                    if x is None or y is None:
                        _align_reply(align_response_q, align_req, False,
                                     error_message="target outside camera FOV")
                    else:
                        log.info(
                            "ALIGN: (%.4f, %.4f) -> pixel (y=%.2f, x=%.2f)",
                            align_req.target_ra_deg,
                            align_req.target_dec_deg, y, x)
                        _align_reply(align_response_q, align_req, True,
                                     boresight_y=float(y),
                                     boresight_x=float(x))

            if cfg.save_solved_frames and frame_snapshot is not None:
                _save_frame(frame_snapshot, cfg, "solved")

            if fail_streak:
                log.info(
                    "solved after %d failed: stars=%d matches=%d "
                    "t=%.0fms (ext=%.0fms slv=%.0fms)",
                    fail_streak, n_stars, n_matches,
                    elapsed_ms, extract_ms, solve_only_ms)
                fail_streak = 0
            elif solve_count % cfg.log_solve_stats_every_n == 0:
                log.info(
                    "solve #%d: stars=%d matches=%d peak=%d "
                    "t=%.0fms (ext=%.0fms slv=%.0fms)",
                    solve_count, n_stars, n_matches, local_peak,
                    elapsed_ms, extract_ms, solve_only_ms)

    finally:
        bg_cache.stop()
        for s in shms:
            s.close()
