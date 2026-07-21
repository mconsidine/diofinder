#!/usr/bin/env python3
"""
diofinder main launcher.

Spawns three pinned worker processes:
  * camera_proc  -> CPU cfg.cpu_camera : picamera2 or test image -> shared memory
  * solver_proc  -> CPUs {cfg.cpu_solver, cfg.cpu_camera, cfg.cpu_solver_aux} : sycamore extract + olive-solve
  * comms_proc   -> CPU cfg.cpu_comms  : LX200 server + alignment + maint socket

CPU 0 is left to the kernel.

Inter-process state:
  * Three SHM frame buffers, coordinated by FrameSlots
  * latest_solution: Manager dict published by solver, read by comms
  * shared_cfg: Manager dict for live-mutable settings:
      boresight_x/y, detect_sigma, solve_timeout_ms, test_mode (bool)
  * align_request_q / align_response_q: comms <-> solver alignment workflow
"""

import argparse
import logging
import multiprocessing as mp
import os
import signal
import sys
import time
from multiprocessing import shared_memory
from pathlib import Path

from diofinder.config import load_config
from diofinder.frame_slots import FrameSlots, NUM_BUFFERS, SHM_PREFIX

log = logging.getLogger("diofinder.main")


def _setup_logging():
    logging.basicConfig(
        level=os.environ.get("DIOFINDER_LOGLEVEL", "INFO"),
        format="%(name)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )


def _allocate_shared_frames(cfg):
    size = cfg.frame_height * cfg.frame_width
    shms = []
    for i in range(NUM_BUFFERS):
        name = f"{SHM_PREFIX}_{i}"
        try:
            stale = shared_memory.SharedMemory(name=name)
            stale.close(); stale.unlink()
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("Unexpected error cleaning stale SHM %s: %s", name, e)
        shms.append(shared_memory.SharedMemory(name=name, create=True, size=size))
    return shms


def _allocate_display_frame(cfg):
    """Dedicated display segment for the web live view (P4). Best-effort: a
    failure here just means the web UI keeps using the frame_get path."""
    from diofinder import display_shm
    try:
        return display_shm.create(cfg.frame_height, cfg.frame_width)
    except Exception as e:
        log.warning("Could not allocate display SHM (live view uses frame_get): %s", e)
        return None


def _resolve_test_image(args):
    if args.test_image:
        p = Path(args.test_image)
        if not p.exists():
            log.error("--test-image path not found: %s", p)
            sys.exit(1)
        return p
    search_dirs = [Path.cwd(), Path("/var/lib/diofinder"), Path("/opt/diofinder")]
    for name in ("test.png", "polaris.png"):
        for d in search_dirs:
            p = d / name
            if p.exists():
                log.info("Test image found: %s", p)
                return p
    if args.test:
        log.error(
            "--test specified but no test image found in %s",
            ", ".join(str(d) for d in search_dirs))
        sys.exit(1)
    return None


def main():
    parser = argparse.ArgumentParser(description="diofinder star-tracker daemon")
    test_grp = parser.add_mutually_exclusive_group()
    test_grp.add_argument(
        "--test", action="store_true",
        help="Test mode: auto-find test.png / polaris.png instead of the camera")
    test_grp.add_argument(
        "--test-image", metavar="PATH",
        help="Test mode: use the specified PNG instead of the camera")
    args = parser.parse_args()

    _setup_logging()
    # One-shot conf migrations BEFORE the config is read: rewrites keys that
    # still carry an old default whose replacement was a correctness fix
    # (never overrides a user-edited value; stamped with conf_version so it
    # runs once). Covers OTA-updated devices whose /etc conf predates the
    # tuning-profile / background-mode / FOV-recenter changes.
    try:
        from diofinder.conf_migrate import migrate as _conf_migrate
        _applied = _conf_migrate()
        if _applied:
            logging.getLogger("diofinder.main").warning(
                "conf migrated (%d keys): %s", len(_applied),
                ", ".join(f"{k}={v}" for k, v in _applied.items()))
    except Exception as _e:
        logging.getLogger("diofinder.main").warning("conf migration failed: %s", _e)
    cfg = load_config()
    os.sched_setaffinity(0, {cfg.cpu_comms})
    # Prefer the stamped release tag (written by install.sh / diofinder-update to
    # /var/lib/diofinder/version) over the in-code default, so the journal shows
    # the actual running build. Falls back to cfg.version.
    _release = cfg.version
    try:
        with open("/var/lib/diofinder/version") as _vf:
            _stamp = _vf.read().split()[0].strip()
            if _stamp:
                _release = _stamp
    except OSError:
        pass
    log.info("diofinder %s starting (code %s); launcher/IMU pinned to CPU %d; config: %s",
             _release, cfg.version, cfg.cpu_comms, cfg.summary())

    test_image_path = _resolve_test_image(args)
    default_test_mode = args.test or (args.test_image is not None)
    if default_test_mode:
        log.info("Starting in TEST MODE — camera replaced by %s", test_image_path)
    else:
        log.info("Starting in LIVE MODE%s",
                 f" (test image available: {test_image_path})" if test_image_path else "")

    mp.set_start_method("spawn", force=True)

    shms = _allocate_shared_frames(cfg)
    display_shm_handle = _allocate_display_frame(cfg)  # noqa: F841 (kept alive)
    slots = FrameSlots()

    manager = mp.Manager()
    latest_solution = manager.dict({
        "ra_deg": 0.0, "dec_deg": 0.0, "roll_deg": 0.0, "fov_deg": 0.0,
        "stars": 0, "matches": 0, "peak": 0, "noise": 0.0,
        "solve_ms": 0.0, "solved": False, "status": 0,
        "epoch_monotonic": 0.0,
    })
    shared_cfg = manager.dict({
        "boresight_y":   cfg.boresight_y,
        "boresight_x":   cfg.boresight_x,
        "imu_available": False,
        # Seed the BNO055 calibration-profile persistence flag (Unit B) so the
        # in-launcher IMU thread can read it without a config round-trip.
        "imu_persist_bno055": cfg.imu_persist_bno055,
        "test_mode":     default_test_mode,
    })
    align_request_q  = mp.Queue(maxsize=4)
    align_response_q = mp.Queue(maxsize=4)
    solver_cmd_q       = mp.Queue(maxsize=16)
    solver_cmd_reply_q = mp.Queue(maxsize=16)
    camera_cmd_q       = mp.Queue(maxsize=16)
    camera_cmd_reply_q = mp.Queue(maxsize=16)

    from diofinder.camera_proc import camera_main
    from diofinder.solver_proc import solver_main
    from diofinder.comms_proc import comms_main
    from diofinder.imu_proc import start_imu_thread

    start_imu_thread(shared_cfg)

    procs = [
        mp.Process(target=comms_main, name="diofinder-comms",
                   args=(latest_solution, shared_cfg,
                         align_request_q, align_response_q,
                         solver_cmd_q, solver_cmd_reply_q,
                         camera_cmd_q, camera_cmd_reply_q, cfg)),
        mp.Process(target=camera_main, name="diofinder-camera",
                   args=(slots, camera_cmd_q, camera_cmd_reply_q, cfg,
                         test_image_path, shared_cfg)),
        mp.Process(target=solver_main, name="diofinder-solver",
                   args=(slots, latest_solution, shared_cfg,
                         align_request_q, align_response_q,
                         solver_cmd_q, solver_cmd_reply_q, cfg)),
    ]

    for p in procs:
        p.start()
        log.info("Started %s pid=%d", p.name, p.pid)

    def _shutdown(signum, _frame):
        log.info("Received signal %d; shutting down children", signum)
        for p in procs:
            if p.is_alive():
                p.terminate()
        time.sleep(cfg.shutdown_grace_s)
        for p in procs:
            if p.is_alive():
                log.warning("Force killing %s", p.name)
                p.kill()
        try:
            manager.shutdown()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        while True:
            time.sleep(1)
            for p in procs:
                if not p.is_alive():
                    log.error("Worker %s exited (code=%s); aborting",
                              p.name, p.exitcode)
                    for q in procs:
                        if q.is_alive():
                            q.terminate()
                    time.sleep(cfg.shutdown_grace_s)
                    for q in procs:
                        if q.is_alive():
                            log.warning("Force killing %s", q.name)
                            q.kill()
                    try:
                        manager.shutdown()
                    except Exception:
                        pass
                    sys.exit(1)
    finally:
        for shm in shms:
            try: shm.close(); shm.unlink()
            except Exception: pass
        if display_shm_handle is not None:
            try: display_shm_handle.close(); display_shm_handle.unlink()
            except Exception: pass


if __name__ == "__main__":
    main()
