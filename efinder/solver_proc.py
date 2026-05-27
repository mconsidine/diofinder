"""Solver worker process — olive branch.

Single backend: olive-solve tetra3-py (Rust, fully in-process).
No external server dependency.

Pi Zero 2W optimisations:
  * Solver process affinity is set to {cpu_solver, cpu_camera} so
    olive-solve's rayon thread pool can spread parallel star extraction
    across two physical cores. cedar-detect no longer occupies either
    core, so both CPUs 2 and 3 are free for solver work.
  * Frame buffer pre-allocated once with np.empty; each iteration fills
    it in-place via np.copyto, eliminating per-frame heap allocation.
  * The shared-memory slot is released immediately after np.copyto so
    camera_proc is never blocked waiting for a solve to finish.
  * target_pixel and target_sky_coord must be float64: the Rust PyO3
    binding extracts them as PyReadonlyArray2<f64>.
  * solve_from_image_fast accepts the raw u8 frame directly; no float32
    conversion step is required.
"""

import logging
import math
import os
import time

import numpy as np

from efinder.frame_slots import SHM_PREFIX, NUM_BUFFERS
from efinder.align import AlignResult
from efinder.calibration import FovCalibrator
from efinder.imu_math import quat_delta_rotvec
from efinder.polar_run import PolarAligner
from multiprocessing import shared_memory

log = logging.getLogger("efinder.solver")

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
    /var/lib/efinder/ and .npz is appended.

    Examples:
        "default_database"                   -> /var/lib/efinder/default_database.npz
        "/var/lib/efinder/mydb.npz"          -> /var/lib/efinder/mydb.npz
        "/opt/efinder/data/custom_db.npz"    -> /opt/efinder/data/custom_db.npz
    """
    if os.path.isabs(raw):
        return raw
    return f"/var/lib/efinder/{raw}.npz"


def _save_frame(frame, cfg, label: str) -> None:
    import datetime
    try:
        from PIL import Image
        os.makedirs(cfg.failed_frames_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        path = os.path.join(cfg.failed_frames_dir, f"capture_{ts}_{label}.png")
        Image.fromarray(frame, mode="L").save(path)
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
                     peak, noise, solve_ms, status):
    return {
        "ra_deg": float(ra), "dec_deg": float(dec),
        "roll_deg": float(roll), "fov_deg": float(fov),
        "stars": int(stars), "matches": int(matches),
        "peak": int(peak), "noise": float(noise),
        "solve_ms": float(solve_ms), "solved": True,
        "status": int(status),
        "epoch_monotonic": time.monotonic(),
    }


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
        response_q.put(AlignResult(
            success=False,
            error_message="superseded by a newer sync request",
            completed_at=time.monotonic(),
        ))
    return latest


def _drain_cmd_queue(q):
    cmds = []
    try:
        while True:
            cmds.append(q.get_nowait())
    except Exception:
        pass
    return cmds


def _handle_solver_cmd(cmd, calibrator, polar):
    from efinder.worker_cmds import (
        SolverCmdReply,
        SOLVER_OP_CALIBRATION_STATUS, SOLVER_OP_CALIBRATION_RESET,
        SOLVER_OP_POLAR_START, SOLVER_OP_POLAR_STATUS,
        SOLVER_OP_POLAR_CANCEL, SOLVER_OP_POLAR_SET_LATITUDE,
    )
    try:
        if cmd.op == SOLVER_OP_CALIBRATION_STATUS:
            return SolverCmdReply(request_id=cmd.request_id, ok=True,
                                  result=calibrator.get_status())
        if cmd.op == SOLVER_OP_CALIBRATION_RESET:
            calibrator.force_recalibrate()
            return SolverCmdReply(request_id=cmd.request_id, ok=True,
                                  result={"state": calibrator.state.value})
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


def _imu_update_reference(shared_cfg, new_ra_deg, new_dec_deg, new_roll_deg):
    """Record current IMU quaternion alongside the just-solved sky position.
    comms_proc uses these pairs to predict pointing between solves."""
    if not shared_cfg.get("imu_available", False):
        return
    q_now = shared_cfg.get("imu_q")
    imu_t = shared_cfg.get("imu_t", 0.0)
    if q_now is None or time.monotonic() - imu_t > 2.0:
        return
    q_prev    = shared_cfg.get("imu_ref_q")
    ra_prev   = shared_cfg.get("imu_ref_ra_deg")
    dec_prev  = shared_cfg.get("imu_ref_dec_deg")
    roll_prev = shared_cfg.get("imu_ref_roll_deg", 0.0)
    shared_cfg["imu_ref_q"]        = q_now
    shared_cfg["imu_ref_ra_deg"]   = new_ra_deg
    shared_cfg["imu_ref_dec_deg"]  = new_dec_deg
    shared_cfg["imu_ref_roll_deg"] = new_roll_deg
    shared_cfg["imu_ref_t"]        = time.monotonic()
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
    pairs = list(shared_cfg.get("imu_calib_pairs", []))
    pairs.append((r_imu[0], r_imu[1], r_imu[2], cam_r, cam_u))
    if len(pairs) > 20:
        pairs = pairs[-20:]
    if len(pairs) >= 3:
        R = np.array([[p[0], p[1], p[2]] for p in pairs])
        S = np.array([[p[3], p[4]]       for p in pairs])
        C, _, _, _ = np.linalg.lstsq(R, S, rcond=None)
        S_pred = R @ C
        ss_res = float(np.sum((S - S_pred)**2))
        ss_tot = float(np.sum((S - S.mean(axis=0))**2))
        r2 = max(0.0, 1.0 - ss_res / ss_tot) if ss_tot > 1e-15 else 0.0
        shared_cfg["imu_calib_C"]       = C.T.flatten().tolist()
        shared_cfg["imu_calib_quality"] = r2
    shared_cfg["imu_calib_pairs"] = pairs
    shared_cfg["imu_calib_n"]     = len(pairs)


def solver_main(slots, latest_solution, shared_cfg,
                align_request_q, align_response_q,
                solver_cmd_q, solver_cmd_reply_q,
                cfg):
    logging.basicConfig(
        level=os.environ.get("EFINDER_LOGLEVEL", "INFO"),
        format="solver %(levelname)s %(message)s",
    )

    # Allow solver threads (including rayon worker pool) to use two cores.
    # CPUs {cpu_solver, cpu_camera}: cpu_camera is available because
    # cedar-detect no longer runs there, and camera_proc is mostly sleeping.
    try:
        os.sched_setaffinity(0, {cfg.cpu_solver, cfg.cpu_camera})
        log.info("Solver pinned to CPUs {%d, %d}", cfg.cpu_solver, cfg.cpu_camera)
    except Exception as e:
        log.warning("Could not set solver CPU affinity: %s", e)

    # ---- Load olive-solve --------------------------------------------------
    try:
        import tetra3 as _tetra3
        db_path = _solver_db_path(cfg.solver_db)
        solver_t3 = _tetra3.Tetra3(db_path)
        log.info("olive-solve ready (db: %s)", db_path)
    except Exception as e:
        log.error("Failed to load olive-solve: %s", e)
        raise RuntimeError(f"olive-solve unavailable: {e}") from e

    calibrator = FovCalibrator(cfg, shared_cfg)
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

    fail_streak = 0
    solve_count = 0

    try:
        while True:
            # Drain out-of-band solver commands (calibration, polar, etc.)
            for cmd in _drain_cmd_queue(solver_cmd_q):
                reply = _handle_solver_cmd(cmd, calibrator, polar)
                try:
                    solver_cmd_reply_q.put_nowait(reply)
                except Exception as e:
                    log.warning("Could not enqueue solver reply: %s", e)

            idx = slots.acquire_read_slot(timeout=5.0)
            t0  = time.monotonic()

            align_req  = _drain_align_queue(align_request_q, align_response_q)
            local_peak = int(bufs[idx].max())

            if local_peak < 20:
                slots.release_read_slot()
                latest_solution.update(_empty_solution(peak=local_peak))
                continue

            # Update boresight target in-place only when it has changed.
            new_bs_y = shared_cfg.get("boresight_y", cfg.boresight_y)
            new_bs_x = shared_cfg.get("boresight_x", cfg.boresight_x)
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

            # float64: Rust extracts target_sky_coord as PyReadonlyArray2<f64>
            target_sky = None
            if align_req is not None:
                target_sky = np.array(
                    [[align_req.target_ra_deg, align_req.target_dec_deg]],
                    dtype=np.float64)

            t_solve = time.monotonic()
            try:
                soln = solver_t3.solve_from_image_fast(
                    frame_buf,
                    sigma=shared_cfg.get("detect_sigma", cfg.detect_sigma),
                    fov_estimate=calibrator.get_fov_estimate(),
                    fov_max_error=calibrator.get_fov_max_error(),
                    solve_timeout=shared_cfg.get(
                        "solve_timeout_ms", cfg.solve_timeout_ms),
                    match_threshold=cfg.match_threshold,
                    match_radius=cfg.match_radius,
                    distortion=calibrator.get_distortion_estimate(),
                    target_pixel=target_pixel,
                    target_sky_coord=target_sky,
                    return_matches=False,
                )
            except Exception as e:
                log.warning("solve_from_image_fast raised: %s", e)
                latest_solution.update(_empty_solution(peak=local_peak))
                if align_req is not None:
                    align_response_q.put(AlignResult(
                        success=False,
                        error_message=f"solver raised: {e}",
                        completed_at=time.monotonic(),
                    ))
                fail_streak += 1
                continue

            elapsed_ms    = (time.monotonic() - t_solve) * 1000.0
            extract_ms    = soln.get("T_extract", 0.0)
            solve_only_ms = soln.get("T_solve",   0.0)
            # Status is a Rust Debug string; RA presence is the reliable
            # success indicator.
            status_str = soln.get("status", "NoMatch")
            status_int = _OLIVE_STATUS.get(status_str, NO_MATCH)
            solve_count += 1

            if soln.get("RA") is None:
                latest_solution.update(_empty_solution(
                    stars=0, peak=local_peak,
                    solve_ms=elapsed_ms, status=status_int))
                if align_req is not None:
                    align_response_q.put(AlignResult(
                        success=False,
                        error_message=f"no match (status={status_str})",
                        completed_at=time.monotonic(),
                    ))
                fail_streak += 1
                if fail_streak == 1 or fail_streak % 20 == 0:
                    log.info(
                        "no solve: status=%s peak=%d "
                        "t=%.0fms (ext=%.0fms slv=%.0fms)",
                        status_str, local_peak,
                        elapsed_ms, extract_ms, solve_only_ms)
                if cfg.save_failed_frames and frame_snapshot is not None:
                    _save_frame(frame_snapshot, cfg, f"failed_{status_str}")
                continue

            measured_fov        = soln.get("FOV", calibrator.get_fov_estimate())
            measured_distortion = soln.get("distortion", 0.0)
            calibrator.update_from_solve(measured_fov, measured_distortion)
            polar.update_from_solve(soln["RA"], soln["Dec"])

            ra_target  = soln.get("RA_target")
            dec_target = soln.get("Dec_target")
            if ra_target is None or dec_target is None:
                ra_out  = soln["RA"]
                dec_out = soln["Dec"]
            else:
                ra_out  = ra_target[0]  if hasattr(ra_target,  "__len__") else ra_target
                dec_out = dec_target[0] if hasattr(dec_target, "__len__") else dec_target

            # solve_from_image_fast reports matched count, not total detected.
            n_matches = soln.get("Matches", 0)
            latest_solution.update(_filled_solution(
                ra=ra_out, dec=dec_out,
                roll=soln.get("Roll", 0.0), fov=measured_fov,
                stars=n_matches, matches=n_matches,
                peak=local_peak, noise=0.0,
                solve_ms=elapsed_ms, status=MATCH_FOUND,
            ))
            _imu_update_reference(
                shared_cfg, ra_out, dec_out, soln.get("Roll", 0.0))

            if align_req is not None:
                xt = soln.get("x_target")
                yt = soln.get("y_target")
                if xt is None or yt is None:
                    align_response_q.put(AlignResult(
                        success=False,
                        error_message="no x_target/y_target in solution",
                        completed_at=time.monotonic(),
                    ))
                else:
                    x = xt[0] if hasattr(xt, "__len__") else xt
                    y = yt[0] if hasattr(yt, "__len__") else yt
                    if x is None or y is None:
                        align_response_q.put(AlignResult(
                            success=False,
                            error_message="target outside camera FOV",
                            completed_at=time.monotonic(),
                        ))
                    else:
                        log.info(
                            "ALIGN: (%.4f, %.4f) -> pixel (y=%.2f, x=%.2f)",
                            align_req.target_ra_deg,
                            align_req.target_dec_deg, y, x)
                        align_response_q.put(AlignResult(
                            success=True,
                            boresight_y=float(y), boresight_x=float(x),
                            completed_at=time.monotonic(),
                        ))

            if cfg.save_solved_frames and frame_snapshot is not None:
                _save_frame(frame_snapshot, cfg, "solved")

            if fail_streak:
                log.info(
                    "solved after %d failed: matches=%d "
                    "t=%.0fms (ext=%.0fms slv=%.0fms)",
                    fail_streak, n_matches,
                    elapsed_ms, extract_ms, solve_only_ms)
                fail_streak = 0
            elif solve_count % cfg.log_solve_stats_every_n == 0:
                log.info(
                    "solve #%d: matches=%d peak=%d "
                    "t=%.0fms (ext=%.0fms slv=%.0fms)",
                    solve_count, n_matches, local_peak,
                    elapsed_ms, extract_ms, solve_only_ms)

    finally:
        for s in shms:
            s.close()
