"""Solver worker process — combo branch.

Supports two interchangeable plate-solve backends selectable at runtime
via shared_cfg["solver_backend"] (default: "hybrid"):

  "hybrid"  cedar-detect gRPC (C++ centroid server, port 50051) +
            olive-solve tetra3-py solve_from_centroids (Rust plate solve).
            cedar-detect.service must be running.

  "olive"   olive-solve tetra3-py (Rust, in-process): centroid extraction +
            plate solve in a single call with no external server dependency.

Both backends share a single olive-solve Tetra3 instance loaded at startup.
If cedar-detect is unavailable the hybrid backend is disabled but olive
continues normally. Switching takes effect on the very next frame without
restarting the process.
"""

import logging
import math
import os
import pathlib
import sys
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


def _pin_to_cpu(cpu: int) -> None:
    try:
        os.sched_setaffinity(0, {cpu})
        log.info("Pinned to CPU %d", cpu)
    except Exception as e:
        log.warning("Could not pin to CPU %d: %s", cpu, e)


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


def _imu_propagate_hint(last_sky_q, last_imu_q, shared_cfg):
    """
    Option-C attitude hint: apply the IMU rotation delta since the last solve
    to the last sky quaternion.

    Approximation: treats IMU body axes ≈ camera axes.  Frame-mismatch error
    (from non-ideal mounting) is absorbed by a wider uncertainty window rather
    than requiring an explicit mount-calibration step.

    Returns (q_hint, uncertainty_deg).  Falls back to (last_sky_q, 0.1) —
    matching the previous behaviour — whenever the IMU is absent or stale.
    """
    if last_sky_q is None or last_imu_q is None:
        return last_sky_q, 0.1
    if not shared_cfg.get("imu_available", False):
        return last_sky_q, 0.1
    q_cur = shared_cfg.get("imu_q")
    imu_t = shared_cfg.get("imu_t", 0.0)
    if q_cur is None or time.monotonic() - imu_t > 2.0:
        return last_sky_q, 0.1

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

    ws, xs, ys, zs = (float(v) for v in last_sky_q)
    wh = wd*ws - xd*xs - yd*ys - zd*zs
    xh = wd*xs + xd*ws + yd*zs - zd*ys
    yh = wd*ys - xd*zs + yd*ws + zd*xs
    zh = wd*zs + xd*ys - yd*xs + zd*ws

    angle_deg = math.degrees(2.0 * math.acos(min(1.0, abs(wd))))
    uncertainty_deg = max(2.0, angle_deg * 1.5)

    return (wh, xh, yh, zh), uncertainty_deg


def _imu_update_reference(shared_cfg, new_ra_deg, new_dec_deg, new_roll_deg):
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
    _pin_to_cpu(cfg.cpu_solver)

    # ---- cedar-detect gRPC (used by hybrid backend) ------------------------
    _cedar_shm_opened: set = set()
    stub = None; pb = None; pb_grpc = None; channel = None

    proto_dir = str(pathlib.Path(__file__).parent.parent / "proto")
    if proto_dir not in sys.path:
        sys.path.insert(0, proto_dir)

    try:
        import grpc as _grpc
        import cedar_detect_pb2 as _pb
        import cedar_detect_pb2_grpc as _pb_grpc
        pb = _pb
        pb_grpc = _pb_grpc
        log.info("Connecting to cedar-detect at %s", cfg.cedar_detect_socket)
        channel = _grpc.insecure_channel(cfg.cedar_detect_socket)
        stub = _pb_grpc.CedarDetectStub(channel)
        log.info("cedar-detect gRPC channel open")
    except Exception as e:
        log.warning("cedar-detect unavailable (hybrid backend disabled): %s", e)

    # ---- olive-solve (shared by hybrid + olive backends) -------------------
    olive_available = False
    olive_t3 = None

    try:
        import tetra3 as _olive_tetra3
        if not hasattr(_olive_tetra3.Tetra3, "solve_from_image_fast"):
            raise ImportError(
                "installed tetra3 lacks solve_from_image_fast; "
                "install olive-solve's tetra3-py wheel")
        db_path = (cfg.olive_db if cfg.olive_db.startswith('/')
                   else f'/var/lib/efinder/{cfg.olive_db}.npz')
        olive_t3 = _olive_tetra3.Tetra3(db_path)
        olive_available = True
        log.info("Olive-solve ready (db: %s)", db_path)
    except Exception as e:
        log.warning("Olive-solve unavailable: %s", e)

    hybrid_available = stub is not None and olive_available

    if not olive_available:
        log.error("No solver backend available (olive-solve required); exiting")
        time.sleep(5)
        raise RuntimeError("No solver backend available")

    calibrator = FovCalibrator(cfg, shared_cfg)
    log.info("Calibrator: state=%s fov=%.4f tolerance=%.3f",
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

    fail_streak = 0
    solve_count = 0
    _warned_unavailable: set = set()

    try:
        while True:
            for cmd in _drain_cmd_queue(solver_cmd_q):
                reply = _handle_solver_cmd(cmd, calibrator, polar)
                try:
                    solver_cmd_reply_q.put_nowait(reply)
                except Exception as e:
                    log.warning("Could not enqueue solver reply: %s", e)

            idx = slots.acquire_read_slot(timeout=5.0)
            t0 = time.monotonic()

            align_req = _drain_align_queue(align_request_q, align_response_q)
            backend   = shared_cfg.get("solver_backend", "hybrid")

            local_peak = int(bufs[idx].max())
            if local_peak < 20:
                slots.release_read_slot()
                latest_solution.update(_empty_solution(peak=local_peak))
                continue

            # ================================================================
            # Hybrid backend: cedar-detect extraction + olive-solve centroids
            # ================================================================
            if backend == "hybrid":
                if not hybrid_available:
                    slots.release_read_slot()
                    if "hybrid" not in _warned_unavailable:
                        log.warning("Hybrid backend selected but not available; "
                                    "cedar-detect required — switch to 'olive'")
                        _warned_unavailable.add("hybrid")
                    time.sleep(0.1)
                    continue

                frame_snapshot = (
                    np.copy(bufs[idx])
                    if (cfg.save_failed_frames or cfg.save_solved_frames)
                    else None
                )

                shm_name   = f"{SHM_PREFIX}_{idx}"
                reopen_shm = shm_name not in _cedar_shm_opened
                if reopen_shm:
                    _cedar_shm_opened.add(shm_name)
                req = pb.CentroidsRequest(
                    input_image=pb.Image(
                        width=cfg.frame_width, height=cfg.frame_height,
                        shmem_name=shm_name, reopen_shmem=reopen_shm,
                    ),
                    sigma=shared_cfg.get("detect_sigma", cfg.detect_sigma),
                    detect_hot_pixels=cfg.detect_hot_pixels,
                    use_binned_for_star_candidates=shared_cfg.get(
                        "detect_use_binned", cfg.detect_use_binned),
                    return_binned=False,
                )

                t_detect = time.monotonic()
                try:
                    resp = stub.ExtractCentroids(req, timeout=2.0)
                except Exception as e:
                    slots.release_read_slot()
                    log.warning("cedar-detect call failed: %s", e)
                    latest_solution.update(_empty_solution(peak=local_peak))
                    if align_req is not None:
                        align_response_q.put(AlignResult(
                            success=False,
                            error_message=f"cedar-detect failed: {e}",
                            completed_at=time.monotonic(),
                        ))
                    fail_streak += 1; time.sleep(0.05); continue
                slots.release_read_slot()
                detect_ms = (time.monotonic() - t_detect) * 1000.0

                n     = len(resp.star_candidates)
                peak  = int(resp.peak_star_pixel) if resp.peak_star_pixel else local_peak
                noise = float(resp.noise_estimate)

                if n < cfg.min_centroids:
                    latest_solution.update(_empty_solution(
                        stars=n, peak=peak, noise=noise, status=TOO_FEW))
                    if align_req is not None:
                        align_response_q.put(AlignResult(
                            success=False,
                            error_message=f"only {n} stars (need {cfg.min_centroids})",
                            completed_at=time.monotonic(),
                        ))
                    fail_streak += 1; continue

                centroids = np.array(
                    [[c.centroid_position.y, c.centroid_position.x]
                     for c in resp.star_candidates],
                    dtype=np.float64,
                )
                bs_y = shared_cfg.get("boresight_y", cfg.boresight_y)
                bs_x = shared_cfg.get("boresight_x", cfg.boresight_x)
                target_pixel = np.array([[bs_y, bs_x]], dtype=np.float64)
                target_sky   = None
                if align_req is not None:
                    target_sky = np.array(
                        [[align_req.target_ra_deg, align_req.target_dec_deg]],
                        dtype=np.float64)

                t_solve = time.monotonic()
                try:
                    soln = olive_t3.solve_from_centroids(
                        centroids,
                        (float(cfg.frame_height), float(cfg.frame_width)),
                        fov_estimate=calibrator.get_fov_estimate(),
                        fov_max_error=calibrator.get_fov_max_error(),
                        solve_timeout=shared_cfg.get(
                            "solve_timeout_ms", cfg.solve_timeout_ms),
                        match_threshold=cfg.match_threshold,
                        match_radius=cfg.match_radius,
                        target_pixel=target_pixel,
                        target_sky_coord=target_sky,
                        return_matches=False,
                    )
                except Exception as e:
                    log.warning("hybrid solve_from_centroids raised: %s", e)
                    latest_solution.update(
                        _empty_solution(stars=n, peak=peak, noise=noise))
                    if align_req is not None:
                        align_response_q.put(AlignResult(
                            success=False,
                            error_message=f"solver raised: {e}",
                            completed_at=time.monotonic(),
                        ))
                    fail_streak += 1; continue

                t_end         = time.monotonic()
                elapsed_ms    = (t_end - t0) * 1000.0
                solve_only_ms = (t_end - t_solve) * 1000.0
                status        = soln.get("status", NO_MATCH)
                solve_count  += 1

                if status != MATCH_FOUND or soln.get("RA") is None:
                    latest_solution.update(_empty_solution(
                        stars=n, peak=peak, noise=noise,
                        solve_ms=elapsed_ms, status=status))
                    if align_req is not None:
                        align_response_q.put(AlignResult(
                            success=False,
                            error_message=f"no match (status={status})",
                            completed_at=time.monotonic(),
                        ))
                    fail_streak += 1
                    if fail_streak == 1 or fail_streak % 20 == 0:
                        log.info(
                            "hybrid no solve: status=%d n=%d peak=%d "
                            "t=%.0fms (det=%.0fms slv=%.0fms)",
                            status, n, peak, elapsed_ms, detect_ms, solve_only_ms)
                    if cfg.save_failed_frames and frame_snapshot is not None:
                        _save_frame(frame_snapshot, cfg, f"hybrid_failed_s{status}")
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

                latest_solution.update(_filled_solution(
                    ra=ra_out, dec=dec_out,
                    roll=soln.get("Roll", 0.0), fov=measured_fov,
                    stars=n, matches=soln.get("Matches", 0),
                    peak=peak, noise=noise,
                    solve_ms=elapsed_ms, status=status,
                ))
                _imu_update_reference(
                    shared_cfg, ra_out, dec_out, soln.get("Roll", 0.0))

                if align_req is not None:
                    xt = soln.get("x_target")
                    yt = soln.get("y_target")
                    if xt is None or yt is None:
                        align_response_q.put(AlignResult(
                            success=False,
                            error_message="hybrid-solve returned no x_target/y_target",
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
                                "ALIGN (hybrid): (%.4f, %.4f) -> "
                                "pixel (y=%.2f, x=%.2f)",
                                align_req.target_ra_deg,
                                align_req.target_dec_deg, y, x)
                            align_response_q.put(AlignResult(
                                success=True,
                                boresight_y=float(y), boresight_x=float(x),
                                completed_at=time.monotonic(),
                            ))

                if cfg.save_solved_frames and frame_snapshot is not None:
                    _save_frame(frame_snapshot, cfg, "hybrid_solved")

                if fail_streak:
                    log.info(
                        "hybrid solved after %d failed: n=%d matches=%d "
                        "t=%.0fms (det=%.0fms slv=%.0fms)",
                        fail_streak, n, soln.get("Matches", 0),
                        elapsed_ms, detect_ms, solve_only_ms)
                    fail_streak = 0
                elif solve_count % cfg.log_solve_stats_every_n == 0:
                    log.info(
                        "hybrid solve #%d: n=%d matches=%d peak=%d "
                        "t=%.0fms (det=%.0fms slv=%.0fms)",
                        solve_count, n, soln.get("Matches", 0), peak,
                        elapsed_ms, detect_ms, solve_only_ms)

            # ================================================================
            # Olive backend (olive-solve tetra3-py, fully in-process)
            # ================================================================
            elif backend == "olive":
                if not olive_available:
                    slots.release_read_slot()
                    if "olive" not in _warned_unavailable:
                        log.warning("Olive backend selected but not available; "
                                    "build and install olive-solve's tetra3-py wheel")
                        _warned_unavailable.add("olive")
                    time.sleep(0.1)
                    continue

                frame_snapshot = (
                    np.copy(bufs[idx])
                    if (cfg.save_failed_frames or cfg.save_solved_frames)
                    else None
                )
                frame_u8 = np.copy(bufs[idx])
                slots.release_read_slot()

                bs_y = shared_cfg.get("boresight_y", cfg.boresight_y)
                bs_x = shared_cfg.get("boresight_x", cfg.boresight_x)
                target_pixel = np.array([[bs_y, bs_x]], dtype=np.float32)
                target_sky   = None
                if align_req is not None:
                    target_sky = np.array(
                        [[align_req.target_ra_deg, align_req.target_dec_deg]],
                        dtype=np.float32)

                t_solve = time.monotonic()
                try:
                    soln = olive_t3.solve_from_image_fast(
                        frame_u8,
                        sigma=shared_cfg.get("detect_sigma", cfg.detect_sigma),
                        fov_estimate=calibrator.get_fov_estimate(),
                        fov_max_error=calibrator.get_fov_max_error(),
                        solve_timeout=shared_cfg.get(
                            "solve_timeout_ms", cfg.solve_timeout_ms),
                        match_threshold=cfg.match_threshold,
                        match_radius=cfg.match_radius,
                        target_pixel=target_pixel,
                        target_sky_coord=target_sky,
                        return_matches=False,
                    )
                except Exception as e:
                    log.warning("olive solve_from_image_fast raised: %s", e)
                    latest_solution.update(_empty_solution(peak=local_peak))
                    if align_req is not None:
                        align_response_q.put(AlignResult(
                            success=False,
                            error_message=f"solver raised: {e}",
                            completed_at=time.monotonic(),
                        ))
                    fail_streak += 1; continue

                elapsed_ms    = (time.monotonic() - t_solve) * 1000.0
                extract_ms    = soln.get("T_extract", 0.0)
                solve_only_ms = soln.get("T_solve",   0.0)
                status        = soln.get("status", NO_MATCH)
                solve_count  += 1

                if status != MATCH_FOUND or soln.get("RA") is None:
                    latest_solution.update(_empty_solution(
                        stars=0, peak=local_peak,
                        solve_ms=elapsed_ms, status=status))
                    if align_req is not None:
                        align_response_q.put(AlignResult(
                            success=False,
                            error_message=f"no match (status={status})",
                            completed_at=time.monotonic(),
                        ))
                    fail_streak += 1
                    if fail_streak == 1 or fail_streak % 20 == 0:
                        log.info(
                            "olive no solve: status=%d peak=%d "
                            "t=%.0fms (ext=%.0fms slv=%.0fms)",
                            status, local_peak, elapsed_ms,
                            extract_ms, solve_only_ms)
                    if cfg.save_failed_frames and frame_snapshot is not None:
                        _save_frame(frame_snapshot, cfg, f"olive_failed_s{status}")
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

                n_matches = soln.get("Matches", 0)
                latest_solution.update(_filled_solution(
                    ra=ra_out, dec=dec_out,
                    roll=soln.get("Roll", 0.0), fov=measured_fov,
                    stars=n_matches, matches=n_matches,
                    peak=local_peak, noise=0.0,
                    solve_ms=elapsed_ms, status=status,
                ))
                _imu_update_reference(
                    shared_cfg, ra_out, dec_out, soln.get("Roll", 0.0))

                if align_req is not None:
                    xt = soln.get("x_target")
                    yt = soln.get("y_target")
                    if xt is None or yt is None:
                        align_response_q.put(AlignResult(
                            success=False,
                            error_message="olive-solve returned no x_target/y_target",
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
                                "ALIGN (olive): (%.4f, %.4f) -> "
                                "pixel (y=%.2f, x=%.2f)",
                                align_req.target_ra_deg,
                                align_req.target_dec_deg, y, x)
                            align_response_q.put(AlignResult(
                                success=True,
                                boresight_y=float(y), boresight_x=float(x),
                                completed_at=time.monotonic(),
                            ))

                if cfg.save_solved_frames and frame_snapshot is not None:
                    _save_frame(frame_snapshot, cfg, "olive_solved")

                if fail_streak:
                    log.info(
                        "olive solved after %d failed: matches=%d "
                        "t=%.0fms (ext=%.0fms slv=%.0fms)",
                        fail_streak, n_matches,
                        elapsed_ms, extract_ms, solve_only_ms)
                    fail_streak = 0
                elif solve_count % cfg.log_solve_stats_every_n == 0:
                    log.info(
                        "olive solve #%d: matches=%d peak=%d "
                        "t=%.0fms (ext=%.0fms slv=%.0fms)",
                        solve_count, n_matches, local_peak,
                        elapsed_ms, extract_ms, solve_only_ms)

            # ================================================================
            # Unknown backend
            # ================================================================
            else:
                slots.release_read_slot()
                if backend not in _warned_unavailable:
                    log.warning(
                        "Unknown solver_backend %r; valid: hybrid, olive",
                        backend)
                    _warned_unavailable.add(backend)
                time.sleep(0.1)

    finally:
        for s in shms:
            s.close()
        if channel is not None:
            try: channel.close()
            except Exception: pass
