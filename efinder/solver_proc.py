"""
Solver worker process.

Pinned to its dedicated CPU. Pulls the latest published frame from the
shared-memory triple buffer, asks cedar-detect for star centroids over
gRPC, then plate-solves with cedar-solve.

Two queues from the comms process:
  * align_request_q  : new boresight alignment requests
  * align_response_q : completed alignment results

When an alignment request is pending, the solver passes the target's
RA/Dec via cedar-solve's `target_sky_coord` parameter on the next
successful solve. The returned x_target/y_target is the pixel where
the scope is actually pointing -- that becomes the new boresight.

Until then the solver runs as normal: it passes the *current* boresight
(from cfg.boresight_x/y) as `target_pixel` so RA_target/Dec_target
in the response are where the scope points (after offset).
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

# tetra3 status codes (from tetra3.py)
MATCH_FOUND = 1
NO_MATCH = 2
TIMEOUT = 3
CANCELLED = 4
TOO_FEW = 5


def _save_frame(frame, cfg, label: str) -> None:
    """Save a frame snapshot to cfg.failed_frames_dir as a PNG."""
    import datetime
    try:
        from PIL import Image
        import os
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
    # Intentionally omits ra_deg/dec_deg/roll_deg/fov_deg so that
    # latest_solution keeps the last successful values.  comms_proc
    # always serves those fields to SkySafari; zeroing them on every
    # failed frame would cause the mount position to jump to 0h 0°.
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
    """Take only the most recent alignment request; NACK superseded ones.

    If SkySafari sends two rapid syncs, the comms process is waiting on
    each. Silently dropping the older ones would leave comms blocked on
    align_response_q until its timeout. Posting a failure result unblocks
    it immediately.
    """
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
    """Drain all pending solver commands. Returns a list (not just the
    latest) because, unlike alignment, every command is independent and
    needs an individual response.
    """
    cmds = []
    try:
        while True:
            cmds.append(q.get_nowait())
    except Exception:
        pass
    return cmds


def _handle_solver_cmd(cmd, calibrator, polar):
    """Dispatch a SolverCmd. Returns a SolverCmdReply."""
    from efinder.worker_cmds import (
        SolverCmdReply,
        SOLVER_OP_CALIBRATION_STATUS, SOLVER_OP_CALIBRATION_RESET,
        SOLVER_OP_POLAR_START, SOLVER_OP_POLAR_STATUS,
        SOLVER_OP_POLAR_CANCEL, SOLVER_OP_POLAR_SET_LATITUDE,
    )
    try:
        if cmd.op == SOLVER_OP_CALIBRATION_STATUS:
            return SolverCmdReply(
                request_id=cmd.request_id, ok=True,
                result=calibrator.get_status())
        if cmd.op == SOLVER_OP_CALIBRATION_RESET:
            calibrator.force_recalibrate()
            return SolverCmdReply(
                request_id=cmd.request_id, ok=True,
                result={"state": calibrator.state.value})
        if cmd.op == SOLVER_OP_POLAR_START:
            polar.start()
            return SolverCmdReply(
                request_id=cmd.request_id, ok=True,
                result=polar.get_status())
        if cmd.op == SOLVER_OP_POLAR_STATUS:
            return SolverCmdReply(
                request_id=cmd.request_id, ok=True,
                result=polar.get_status())
        if cmd.op == SOLVER_OP_POLAR_CANCEL:
            polar.cancel()
            return SolverCmdReply(
                request_id=cmd.request_id, ok=True,
                result=polar.get_status())
        if cmd.op == SOLVER_OP_POLAR_SET_LATITUDE:
            try:
                lat = float(cmd.args["latitude_deg"])
            except (KeyError, ValueError, TypeError) as e:
                return SolverCmdReply(
                    request_id=cmd.request_id, ok=False,
                    error=f"polar_set_latitude requires numeric latitude_deg: {e}")
            polar.set_latitude(lat)
            return SolverCmdReply(
                request_id=cmd.request_id, ok=True,
                result={"latitude_deg": lat})
        return SolverCmdReply(
            request_id=cmd.request_id, ok=False,
            error=f"unknown solver op: {cmd.op!r}")
    except Exception as e:
        return SolverCmdReply(
            request_id=cmd.request_id, ok=False,
            error=f"{type(e).__name__}: {e}")


def _imu_update_reference(shared_cfg, new_ra_deg, new_dec_deg, new_roll_deg):
    """
    Called after every successful plate-solve.

    1. Reads the current IMU quaternion from shared_cfg.
    2. If a previous solve reference exists, appends the (IMU-delta,
       sky-delta) pair to the calibration ring buffer and re-fits the
       2×3 transform matrix C that maps IMU rotation vectors to
       (Δcam_right, Δcam_up) — the delta expressed in the *camera frame*
       rather than the sky frame.

       Expressing pairs in the camera frame (by rotating the sky delta by
       -roll before fitting) removes orientation-dependent variation from
       the training data.  C then represents the fixed IMU→camera mounting
       transform, which is valid across the whole sky rather than just near
       the calibration region.  comms_proc rotates back by +roll_ref when
       predicting, recovering the correct sky delta.

    3. Writes the new solve as the IMU reference for the next call.

    All paths are no-ops when the IMU is absent (imu_available=False).
    """
    if not shared_cfg.get("imu_available", False):
        return

    q_now = shared_cfg.get("imu_q")
    imu_t = shared_cfg.get("imu_t", 0.0)
    if q_now is None or time.monotonic() - imu_t > 2.0:
        return  # stale or missing IMU data

    q_prev    = shared_cfg.get("imu_ref_q")
    ra_prev   = shared_cfg.get("imu_ref_ra_deg")
    dec_prev  = shared_cfg.get("imu_ref_dec_deg")
    roll_prev = shared_cfg.get("imu_ref_roll_deg", 0.0)

    # Write new reference before the expensive calibration work so that
    # comms_proc always has the freshest possible anchor point.
    shared_cfg["imu_ref_q"]       = q_now
    shared_cfg["imu_ref_ra_deg"]  = new_ra_deg
    shared_cfg["imu_ref_dec_deg"] = new_dec_deg
    shared_cfg["imu_ref_roll_deg"] = new_roll_deg
    shared_cfg["imu_ref_t"]       = time.monotonic()

    if q_prev is None or ra_prev is None or dec_prev is None:
        return  # first solve — no delta to compute yet

    # ---- Compute the calibration pair (IMU rot-vec, camera-frame sky-delta) ----

    r_imu = quat_delta_rotvec(q_now, q_prev)
    imu_dist = math.sqrt(r_imu[0]**2 + r_imu[1]**2 + r_imu[2]**2)
    if imu_dist < 1e-6:
        return  # IMU saw no movement — uninformative pair

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

    # Rotate the sky delta (east, north) into the camera frame using the
    # roll angle at the *previous* solve.  Convention: roll is the CCW
    # rotation of celestial north from image "up", so rotating the sky
    # vector by -roll brings it into the camera's "north=up" frame.
    roll_rad = math.radians(roll_prev)
    cos_r, sin_r = math.cos(roll_rad), math.sin(roll_rad)
    cam_r =  dra_rad * cos_r + ddec_rad * sin_r
    cam_u = -dra_rad * sin_r + ddec_rad * cos_r

    # ---- Update calibration ring buffer and refit -------------------------

    pairs = list(shared_cfg.get("imu_calib_pairs", []))
    pairs.append((r_imu[0], r_imu[1], r_imu[2], cam_r, cam_u))
    if len(pairs) > 20:
        pairs = pairs[-20:]

    if len(pairs) >= 3:
        R = np.array([[p[0], p[1], p[2]] for p in pairs])   # N×3
        S = np.array([[p[3], p[4]]       for p in pairs])   # N×2
        C, _, _, _ = np.linalg.lstsq(R, S, rcond=None)      # C: 3×2

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

    proto_dir = str(pathlib.Path(__file__).parent.parent / "proto")
    if proto_dir not in sys.path:
        sys.path.insert(0, proto_dir)

    try:
        import grpc
        import cedar_detect_pb2 as pb
        import cedar_detect_pb2_grpc as pb_grpc
    except ImportError as e:
        log.error("gRPC stubs not importable (%s); is cedar-detect installed?", e)
        time.sleep(10); raise

    try:
        import tetra3
    except ImportError as e:
        log.error("tetra3/cedar-solve not importable: %s", e)
        time.sleep(10); raise

    log.info("Loading tetra3 database %s", cfg.tetra3_db)
    t3 = tetra3.Tetra3(cfg.tetra3_db)

    # FOV / distortion calibration. Restores prior state from cfg if
    # fov_calibrated is True; otherwise starts in UNCALIBRATED.
    calibrator = FovCalibrator(cfg, shared_cfg)
    log.info("Calibrator: state=%s fov=%.4f tolerance=%.3f",
             calibrator.state.value,
             calibrator.get_fov_estimate(),
             calibrator.get_fov_max_error())

    # Polar alignment runtime. Latitude may not be set yet (zero default
    # is treated as "unset"); user calls polar_set_latitude or it gets
    # populated from SkySafari's :St command.
    polar = PolarAligner(
        latitude_deg=cfg.latitude_deg if cfg.latitude_deg != 0.0 else None,
    )

    log.info("Connecting to cedar-detect at %s", cfg.cedar_detect_socket)
    channel = grpc.insecure_channel(cfg.cedar_detect_socket)
    stub = pb_grpc.CedarDetectStub(channel)

    shms = [shared_memory.SharedMemory(name=f"{SHM_PREFIX}_{i}")
            for i in range(NUM_BUFFERS)]
    bufs = [np.ndarray((cfg.frame_height, cfg.frame_width), dtype=np.uint8,
                        buffer=s.buf) for s in shms]

    fail_streak = 0
    solve_count = 0
    # Force cedar-detect to reopen each SHM slot on first use so it gets a
    # fresh file descriptor after efinder recreates the SHM blocks on startup.
    # Without this, cedar-detect reuses a stale fd from a previous session and
    # reads old/garbage data instead of the current frame.
    _cedar_shm_opened: set = set()

    try:
        while True:
            # Process any pending maintenance commands first; they're
            # cheap and shouldn't be delayed by frame acquisition.
            for cmd in _drain_cmd_queue(solver_cmd_q):
                reply = _handle_solver_cmd(cmd, calibrator, polar)
                try:
                    solver_cmd_reply_q.put_nowait(reply)
                except Exception as e:
                    log.warning("Could not enqueue solver reply: %s", e)

            idx = slots.acquire_read_slot(timeout=5.0)
            t0 = time.monotonic()

            # Check for a pending alignment request; if there are several
            # queued, take only the latest.
            align_req = _drain_align_queue(align_request_q, align_response_q)

            shm_name = f"{SHM_PREFIX}_{idx}"
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
            local_peak = int(bufs[idx].max())

            # Snapshot for optional frame saving (copy taken while we own the
            # slot; None when saving is disabled so there is no per-frame cost).
            frame_snapshot = (
                np.copy(bufs[idx])
                if (cfg.save_failed_frames or cfg.save_solved_frames)
                else None
            )

            # Dark-frame fast-path: if the brightest pixel is below the noise
            # floor, there are no stars and running cedar-detect + cedar-solve
            # is pointless. numpy.max() on a 960x760 uint8 array costs ~0.1ms
            # and is already computed above, so this check is essentially free.
            # Threshold of 20 ADU is well below any real star signal but safely
            # above sensor read noise (~2-4 ADU) and dark current artefacts.
            if local_peak < 20:
                slots.release_read_slot()
                latest_solution.update(_empty_solution(peak=local_peak))
                continue

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

            n = len(resp.star_candidates)
            peak = int(resp.peak_star_pixel) if resp.peak_star_pixel else local_peak
            noise = float(resp.noise_estimate)

            if n < cfg.min_centroids:
                latest_solution.update(_empty_solution(
                    stars=n, peak=peak, noise=noise, status=TOO_FEW))
                if align_req is not None:
                    align_response_q.put(AlignResult(
                        success=False,
                        error_message=f"only {n} stars detected (need {cfg.min_centroids})",
                        completed_at=time.monotonic(),
                    ))
                fail_streak += 1; continue

            centroids = np.array(
                [[c.centroid_position.y, c.centroid_position.x]
                 for c in resp.star_candidates],
                dtype=np.float32,
            )

            # Always pass the current boresight as target_pixel so the
            # normal reply carries scope-coordinates, not camera-coordinates.
            # Read from shared_cfg so that alignment updates are picked up
            # immediately, not on restart.
            bs_y = shared_cfg.get("boresight_y", cfg.boresight_y)
            bs_x = shared_cfg.get("boresight_x", cfg.boresight_x)
            target_pixel = np.array([[bs_y, bs_x]], dtype=np.float32)

            # If aligning, also pass the requested RA/Dec so we get back
            # x_target/y_target -- the pixel where the scope points.
            target_sky = None
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
                    solve_timeout=shared_cfg.get("solve_timeout_ms", cfg.solve_timeout_ms),
                    match_threshold=cfg.match_threshold,
                    match_radius=cfg.match_radius,
                    distortion=calibrator.get_distortion_estimate(),
                    target_pixel=target_pixel,
                    target_sky_coord=target_sky,
                    return_matches=False,
                )
            except Exception as e:
                log.warning("solve_from_centroids raised: %s", e)
                latest_solution.update(_empty_solution(
                    stars=n, peak=peak, noise=noise))
                if align_req is not None:
                    align_response_q.put(AlignResult(
                        success=False,
                        error_message=f"solver raised: {e}",
                        completed_at=time.monotonic(),
                    ))
                fail_streak += 1; continue

            t_end = time.monotonic()
            elapsed_ms = (t_end - t0) * 1000.0
            solve_only_ms = (t_end - t_solve) * 1000.0
            status = soln.get("status", NO_MATCH)
            solve_count += 1

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
                        "No solve: status=%d n=%d peak=%d noise=%.1f t=%.0fms "
                        "fov_est=%.4f fov_err=%.4f",
                        status, n, peak, noise, elapsed_ms,
                        calibrator.get_fov_estimate(),
                        calibrator.get_fov_max_error())
                if cfg.save_failed_frames and frame_snapshot is not None:
                    _save_frame(frame_snapshot, cfg, f"failed_s{status}")
                continue

            # ---- Normal scope-pointing report ----
            measured_fov = soln.get("FOV", calibrator.get_fov_estimate())
            measured_distortion = soln.get("distortion", 0.0)
            calibrator.update_from_solve(measured_fov, measured_distortion)

            # Polar aligner consumes the camera-center solution, not the
            # boresight-corrected one. The geometry it analyzes is the
            # camera's path around the mount's RA axis; the boresight
            # offset is a fixed sub-arcmin correction that doesn't
            # affect the axis fit.
            polar.update_from_solve(soln["RA"], soln["Dec"])

            ra_target = soln.get("RA_target"); dec_target = soln.get("Dec_target")
            if ra_target is None or dec_target is None:
                ra_out = soln["RA"]; dec_out = soln["Dec"]
            else:
                ra_out = ra_target[0] if hasattr(ra_target, "__len__") else ra_target
                dec_out = dec_target[0] if hasattr(dec_target, "__len__") else dec_target
            latest_solution.update(_filled_solution(
                ra=ra_out, dec=dec_out,
                roll=soln.get("Roll", 0.0),
                fov=measured_fov,
                stars=n, matches=soln.get("Matches", 0),
                peak=peak, noise=noise,
                solve_ms=elapsed_ms, status=status,
            ))

            # ---- IMU dead-reckoning reference update ----
            _imu_update_reference(shared_cfg, ra_out, dec_out, soln.get("Roll", 0.0))

            # ---- Alignment response, if requested ----
            if align_req is not None:
                xt = soln.get("x_target"); yt = soln.get("y_target")
                if xt is None or yt is None:
                    align_response_q.put(AlignResult(
                        success=False,
                        error_message="cedar-solve returned no x_target/y_target",
                        completed_at=time.monotonic(),
                    ))
                else:
                    # tetra3 returns lists; first (and only) element is ours.
                    x = xt[0] if hasattr(xt, "__len__") else xt
                    y = yt[0] if hasattr(yt, "__len__") else yt
                    if x is None or y is None:
                        # Target was outside the FOV; alignment cannot use this frame.
                        align_response_q.put(AlignResult(
                            success=False,
                            error_message="target outside camera FOV",
                            completed_at=time.monotonic(),
                        ))
                    else:
                        log.info("ALIGN solved: target (%.4f, %.4f) -> pixel (y=%.2f, x=%.2f)",
                                 align_req.target_ra_deg, align_req.target_dec_deg, y, x)
                        align_response_q.put(AlignResult(
                            success=True,
                            boresight_y=float(y),
                            boresight_x=float(x),
                            completed_at=time.monotonic(),
                        ))

            if cfg.save_solved_frames and frame_snapshot is not None:
                _save_frame(frame_snapshot, cfg, "solved")

            if fail_streak:
                log.info("Solved after %d failed frames; n=%d matches=%d "
                         "t=%.0fms (detect=%.0fms solve=%.0fms)",
                         fail_streak, n, soln.get("Matches", 0),
                         elapsed_ms, detect_ms, solve_only_ms)
                fail_streak = 0
            elif solve_count % cfg.log_solve_stats_every_n == 0:
                log.info("solve #%d: n=%d matches=%d peak=%d noise=%.1f "
                         "t=%.0fms (detect=%.0fms solve=%.0fms)",
                         solve_count, n, soln.get("Matches", 0), peak, noise,
                         elapsed_ms, detect_ms, solve_only_ms)
    finally:
        for s in shms: s.close()
        try: channel.close()
        except Exception: pass
