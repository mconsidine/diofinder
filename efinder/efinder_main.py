#!/usr/bin/env python3
"""
eFinder main launcher.

Spawns three pinned worker processes:
  * camera_proc   -> CPU cfg.cpu_camera : picamera2 -> shared memory
  * solver_proc   -> CPU cfg.cpu_solver : cedar-detect + cedar-solve
  * comms_proc    -> CPU cfg.cpu_comms  : LX200 server + alignment
CPU 0 is left to the kernel.

Inter-process state:
  * Three SHM frame buffers, coordinated by FrameSlots
  * latest_solution: Manager dict published by solver, read by comms
  * shared_cfg: Manager dict for live-mutable settings (boresight)
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

from efinder.config import load_config
from efinder.frame_slots import FrameSlots, NUM_BUFFERS, SHM_PREFIX

log = logging.getLogger("efinder.main")


def _setup_logging():
    logging.basicConfig(
        level=os.environ.get("EFINDER_LOGLEVEL", "INFO"),
        format="%(name)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )


def _allocate_shared_frames(cfg):
    """Create NUM_BUFFERS shared memory blocks. Stale ones are unlinked
    first so this process owns them.
    """
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


def _resolve_test_image(args):
    """Return an absolute Path to the test image, or None for live mode."""
    if args.test_image:
        p = Path(args.test_image)
        if not p.exists():
            log.error("--test-image path not found: %s", p)
            sys.exit(1)
        return p
    if args.test:
        search_dirs = [Path.cwd(), Path("/var/lib/efinder")]
        for name in ("polaris.png", "test.png"):
            for d in search_dirs:
                p = d / name
                if p.exists():
                    log.info("Test mode: found %s", p)
                    return p
        log.error(
            "--test specified but neither polaris.png nor test.png found in "
            "%s", ", ".join(str(d) for d in search_dirs))
        sys.exit(1)
    return None


def main():
    parser = argparse.ArgumentParser(description="eFinder star-tracker daemon")
    test_grp = parser.add_mutually_exclusive_group()
    test_grp.add_argument(
        "--test", action="store_true",
        help="Test mode: auto-find polaris.png or test.png instead of using the camera")
    test_grp.add_argument(
        "--test-image", metavar="PATH",
        help="Test mode: use the specified PNG instead of the camera")
    args = parser.parse_args()

    _setup_logging()
    cfg = load_config()
    log.info("eFinder %s starting; config: %s", cfg.version, cfg.summary())

    test_image_path = _resolve_test_image(args)
    if test_image_path:
        log.info("TEST MODE active — camera replaced by %s", test_image_path)

    mp.set_start_method("spawn", force=True)

    shms = _allocate_shared_frames(cfg)
    slots = FrameSlots()

    manager = mp.Manager()
    # Solution published by solver, read by comms
    latest_solution = manager.dict({
        "ra_deg": 0.0, "dec_deg": 0.0, "roll_deg": 0.0, "fov_deg": 0.0,
        "stars": 0, "matches": 0, "peak": 0, "noise": 0.0,
        "solve_ms": 0.0, "solved": False, "status": 0,
        "epoch_monotonic": 0.0,
    })
    # Live-mutable settings (boresight). Other settings stay in cfg.
    shared_cfg = manager.dict({
        "boresight_y": cfg.boresight_y,
        "boresight_x": cfg.boresight_x,
        # IMU dead-reckoning state (populated by imu_proc thread + solver_proc)
        "imu_available": False,
    })
    align_request_q = mp.Queue(maxsize=4)
    align_response_q = mp.Queue(maxsize=4)
    # Maintenance command queues. Replies are correlated by request_id
    # since multiple maintenance clients could in principle race; in
    # practice there's at most one efinder-ctl invocation at a time.
    solver_cmd_q = mp.Queue(maxsize=16)
    solver_cmd_reply_q = mp.Queue(maxsize=16)
    camera_cmd_q = mp.Queue(maxsize=16)
    camera_cmd_reply_q = mp.Queue(maxsize=16)

    from efinder.camera_proc import camera_main
    from efinder.solver_proc import solver_main
    from efinder.comms_proc import comms_main
    from efinder.imu_proc import start_imu_thread

    # IMU runs as a daemon thread in the main process.  It probes for a
    # BNO055 on I2C every 3 s and publishes to shared_cfg when found.
    # If smbus2 is not installed or the chip is absent, it exits silently
    # and imu_available stays False — no impact on the rest of the system.
    start_imu_thread(shared_cfg)

    procs = [
        mp.Process(target=comms_main, name="efinder-comms",
                   args=(latest_solution, shared_cfg,
                         align_request_q, align_response_q,
                         solver_cmd_q, solver_cmd_reply_q,
                         camera_cmd_q, camera_cmd_reply_q, cfg)),
        mp.Process(target=camera_main, name="efinder-camera",
                   args=(slots, camera_cmd_q, camera_cmd_reply_q, cfg,
                         test_image_path)),
        mp.Process(target=solver_main, name="efinder-solver",
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


if __name__ == "__main__":
    main()
