"""
Solver worker process — combo branch.

Supports two interchangeable plate-solve backends selectable at runtime
via shared_cfg["solver_backend"] (default: "cedar"):

  "cedar"  cedar-detect gRPC (C++ centroid server, port 50051) +
           tetra3 Python library for plate solving.
           cedar-detect.service must be running.

  "tetra"  tetra3rs (Rust, in-process): centroid extraction + plate solve
           in a single step with no external server dependency.

Both backends are loaded and initialised at startup. If one fails
(missing library, service down) it is marked unavailable and a warning
is logged; the other backend continues normally. Switching takes effect
on the very next frame without restarting the process.
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

    # ---- Cedar backend init ------------------------------------------------
    cedar_available = False
    stub = None
    pb = None
    pb_grpc = None
    t3 = None
    channel = None
    _cedar_shm_opened: set = set()

    proto_dir = str(pathlib.Path(__file__).parent.parent / "proto")
    if proto_dir not in sys.path:
        sys.path.insert(0, proto_dir)

    try:
        import grpc as _grpc
        import cedar_detect_pb2 as _pb
        import cedar_detect_pb2_grpc as _pb_grpc
        import tetra3 as _t3
        pb = _pb
        pb_grpc = _pb_grpc
        log.info("Connecting to cedar-detect at %s", cfg.cedar_detect_socket)
        channel = _grpc.insecure_channel(cfg.cedar_detect_socket)
        stub = _pb_grpc.CedarDetectStub(channel)
        log.info("Loading tetra3 database %s", cfg.tetra3_db)
        t3 = _t3.Tetra3(cfg.tetra3_db)
        cedar_available = True
        log.info("Cedar backend ready (gRPC + tetra3)")
    except Exception as e:
        log.warning("Cedar backend unavailable: %s", e)

    # ---- Tetra3rs backend init ---------------------------------------------
    tetra_available = False
    tetra3rs = None
    db = None
    last_quaternion = None

    try:
        import tetra3rs as _tetra3rs
        tetra3rs = _tetra3rs
        log.info("Loading tetra3rs database %s", cfg.tetra3rs_db)
        db = tetra3rs.SolverDatabase.load_from_file(cfg.tetra3rs_db)
        tetra_available = True
        log.info("Tetra3rs backend ready")
    except Exception as e:
        log.warning("Tetra3rs backend unavailable: %s", e)

    if not cedar_available and not tetra_available:
        log.error("No solver backend available; exiting")
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
            backend   = shared_cfg.get("solver_backend", "cedar")

            local_peak = int(bufs[idx].max())
            if local_peak < 20:
                slots.release_read_slot()
                latest_solution.update(_empty_solution(peak=local_peak))
                continue

            # ================================================================
            # Cedar backend
            # ================================================================
            if backend == "cedar":
                if not cedar_available:
                    slots.release_read_slot()
                    if "cedar" not in _warned_unavailable:
                        log.warning("Cedar backend selected but not available; "
                                    "switch to 'tetra' via web UI or maint socket")
                        _warned_unavailable.add("cedar")
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
                    use_binned_for_star_candidates=cfg.detect_use_binned,
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
                    dtype=np.float32,
                )
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
                    soln = t3.solve_from_centroids(
                        centroids,
                        (cfg.frame_height, cfg.frame_width),
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
                    log.warning("cedar solve_from_centroids raised: %s", e)
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
                            "cedar no solve: status=%d n=%d peak=%d "
                            "t=%.0fms (det=%.0fms slv=%.0fms)",
                            status, n, peak, elapsed_ms, detect_ms, solve_only_ms)
                    if cfg.save_failed_frames and frame_snapshot is not None:
                        _save_frame(frame_snapshot, cfg, f"cedar_failed_s{status}")
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
                            error_message="cedar-solve returned no x_target/y_target",
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
                                "ALIGN (cedar): (%.4f, %.4f) -> "
                                "pixel (y=%.2f, x=%.2f)",
                                align_req.target_ra_deg,
                                align_req.target_dec_deg, y, x)
                            align_response_q.put(AlignResult(
                                success=True,
                                boresight_y=float(y), boresight_x=float(x),
                                completed_at=time.monotonic(),
                            ))

                if cfg.save_solved_frames and frame_snapshot is not None:
                    _save_frame(frame_snapshot, cfg, "cedar_solved")

                if fail_streak:
                    log.info(
                        "cedar solved after %d failed: n=%d matches=%d "
                        "t=%.0fms (det=%.0fms slv=%.0fms)",
                        fail_streak, n, soln.get("Matches", 0),
                        elapsed_ms, detect_ms, solve_only_ms)
                    fail_streak = 0
                elif solve_count % cfg.log_solve_stats_every_n == 0:
                    log.info(
                        "cedar solve #%d: n=%d matches=%d peak=%d "
                        "t=%.0fms (det=%.0fms slv=%.0fms)",
                        solve_count, n, soln.get("Matches", 0), peak,
                        elapsed_ms, detect_ms, solve_only_ms)

            # ================================================================
            # Tetra3rs backend
            # ================================================================
            elif backend == "tetra":
                if not tetra_available:
                    slots.release_read_slot()
                    if "tetra" not in _warned_unavailable:
                        log.warning("Tetra3rs backend selected but not available; "
                                    "switch to 'cedar' via web UI or maint socket")
                        _warned_unavailable.add("tetra")
                    time.sleep(0.1)
                    continue

                frame_snapshot = (
                    np.copy(bufs[idx])
                    if (cfg.save_failed_frames or cfg.save_solved_frames)
                    else None
                )
                frame = frame_snapshot if frame_snapshot is not None \
                    else np.copy(bufs[idx])
                slots.release_read_slot()

                t_extract = time.monotonic()
                try:
                    extraction = tetra3rs.extract_centroids(
                        frame,
                        sigma_threshold=shared_cfg.get(
                            "detect_sigma", cfg.detect_sigma),
                        max_centroids=150,
                    )
                    centroids = extraction.centroids
                except Exception as e:
                    log.warning("tetra3rs extract_centroids raised: %s", e)
                    latest_solution.update(_empty_solution(peak=local_peak))
                    if align_req is not None:
                        align_response_q.put(AlignResult(
                            success=False,
                            error_message=f"centroid extraction failed: {e}",
                            completed_at=time.monotonic(),
                        ))
                    fail_streak += 1; time.sleep(0.05); continue

                n          = len(centroids)
                extract_ms = (time.monotonic() - t_extract) * 1000.0

                if n < cfg.min_centroids:
                    latest_solution.update(_empty_solution(
                        stars=n, peak=local_peak, status=TOO_FEW))
                    if align_req is not None:
                        align_response_q.put(AlignResult(
                            success=False,
                            error_message=f"only {n} stars (need {cfg.min_centroids})",
                            completed_at=time.monotonic(),
                        ))
                    fail_streak += 1; continue

                timeout_ms = int(shared_cfg.get(
                    "solve_timeout_ms", cfg.solve_timeout_ms))
                hint_label = "seeded" if last_quaternion is not None else "blind"
                t_solve    = time.monotonic()
                try:
                    result = db.solve_from_centroids(
                        centroids,
                        fov_estimate_deg=calibrator.get_fov_estimate(),
                        fov_max_error_deg=calibrator.get_fov_max_error(),
                        image_width=cfg.frame_width,
                        image_height=cfg.frame_height,
                        match_radius=cfg.match_radius,
                        match_threshold=cfg.match_threshold,
                        solve_timeout_ms=timeout_ms,
                        attitude_hint=last_quaternion,
                        hint_uncertainty_deg=5.0,
                        strict_hint=False,
                    )
                except Exception as e:
                    log.warning("tetra3rs solve_from_centroids raised: %s", e)
                    latest_solution.update(
                        _empty_solution(stars=n, peak=local_peak))
                    if align_req is not None:
                        align_response_q.put(AlignResult(
                            success=False,
                            error_message=f"solver raised: {e}",
                            completed_at=time.monotonic(),
                        ))
                    fail_streak += 1; continue

                solve_only_ms = (time.monotonic() - t_solve) * 1000.0
                elapsed_ms    = (time.monotonic() - t0) * 1000.0
                solve_count  += 1

                if result is None:
                    latest_solution.update(_empty_solution(
                        stars=n, peak=local_peak,
                        solve_ms=elapsed_ms, status=NO_MATCH))
                    if align_req is not None:
                        align_response_q.put(AlignResult(
                            success=False, error_message="no match",
                            completed_at=time.monotonic(),
                        ))
                    fail_streak += 1
                    if fail_streak == 1 or fail_streak % 20 == 0:
                        log.info(
                            "tetra no solve (%s): n=%d peak=%d "
                            "ext=%.0fms slv=%.0fms",
                            hint_label, n, local_peak,
                            extract_ms, solve_only_ms)
                    if cfg.save_failed_frames and frame_snapshot is not None:
                        _save_frame(frame_snapshot, cfg, "tetra_failed")
                    continue

                last_quaternion = result.quaternion
                measured_fov    = result.fov_deg
                calibrator.update_from_solve(measured_fov, 0.0)
                polar.update_from_solve(result.ra_deg, result.dec_deg)

                bs_y   = shared_cfg.get("boresight_y", cfg.boresight_y)
                bs_x   = shared_cfg.get("boresight_x", cfg.boresight_x)
                bs_x_c = bs_x - cfg.frame_width  / 2.0
                bs_y_c = bs_y - cfg.frame_height / 2.0
                try:
                    ra_out, dec_out = result.pixel_to_world(bs_x_c, bs_y_c)
                    if ra_out is None or math.isnan(float(ra_out)):
                        ra_out, dec_out = result.ra_deg, result.dec_deg
                except Exception:
                    ra_out, dec_out = result.ra_deg, result.dec_deg

                ra_out  = float(ra_out)
                dec_out = float(dec_out)
                roll    = float(result.roll_deg)

                latest_solution.update(_filled_solution(
                    ra=ra_out, dec=dec_out, roll=roll, fov=measured_fov,
                    stars=n, matches=int(result.num_matches),
                    peak=local_peak, noise=0.0,
                    solve_ms=elapsed_ms, status=MATCH_FOUND,
                ))
                _imu_update_reference(shared_cfg, ra_out, dec_out, roll)

                if align_req is not None:
                    try:
                        x_c, y_c = result.world_to_pixel(
                            align_req.target_ra_deg,
                            align_req.target_dec_deg)
                        if x_c is None or math.isnan(float(x_c)):
                            align_response_q.put(AlignResult(
                                success=False,
                                error_message="target outside camera FOV",
                                completed_at=time.monotonic(),
                            ))
                        else:
                            new_bs_x = float(x_c) + cfg.frame_width  / 2.0
                            new_bs_y = float(y_c) + cfg.frame_height / 2.0
                            log.info(
                                "ALIGN (tetra): (%.4f, %.4f) -> "
                                "pixel (y=%.2f, x=%.2f)",
                                align_req.target_ra_deg,
                                align_req.target_dec_deg,
                                new_bs_y, new_bs_x)
                            align_response_q.put(AlignResult(
                                success=True,
                                boresight_y=new_bs_y, boresight_x=new_bs_x,
                                completed_at=time.monotonic(),
                            ))
                    except Exception as e:
                        align_response_q.put(AlignResult(
                            success=False,
                            error_message=f"world_to_pixel raised: {e}",
                        ))

                if cfg.save_solved_frames and frame_snapshot is not None:
                    _save_frame(frame_snapshot, cfg, "tetra_solved")

                if fail_streak:
                    log.info(
                        "tetra solved after %d failed (%s): "
                        "n=%d matches=%d ext=%.0fms slv=%.0fms total=%.0fms",
                        fail_streak, hint_label,
                        n, result.num_matches,
                        extract_ms, solve_only_ms, elapsed_ms)
                    fail_streak = 0
                elif solve_count % cfg.log_solve_stats_every_n == 0:
                    log.info(
                        "tetra solve #%d (%s): n=%d matches=%d peak=%d "
                        "ext=%.0fms slv=%.0fms total=%.0fms",
                        solve_count, hint_label,
                        n, result.num_matches, local_peak,
                        extract_ms, solve_only_ms, elapsed_ms)

            # ================================================================
            # Unknown backend
            # ================================================================
            else:
                slots.release_read_slot()
                if backend not in _warned_unavailable:
                    log.warning(
                        "Unknown solver_backend %r; valid: cedar, tetra",
                        backend)
                    _warned_unavailable.add(backend)
                time.sleep(0.1)

    finally:
        for s in shms:
            s.close()
        if channel is not None:
            try: channel.close()
            except Exception: pass
