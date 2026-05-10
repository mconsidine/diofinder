"""
Camera worker process.

Runs the picamera2 capture loop and writes each frame into a
shared-memory buffer obtained from FrameSlots. Pinned to its dedicated
CPU before importing picamera2 so libcamera helper threads inherit
the affinity mask.

The picamera2 wiring assumes an Arducam 12 MP IMX477-class sensor
configured for an 8-bit Y plane via YUV420 (this is the recommended
path for monochrome capture from a color sensor on the Pi).
"""

import logging
import os
import time

import numpy as np

from efinder.frame_slots import SHM_PREFIX, NUM_BUFFERS
from multiprocessing import shared_memory

log = logging.getLogger("efinder.camera")


def _pin_to_cpu(cpu: int) -> None:
    try:
        os.sched_setaffinity(0, {cpu})
        log.info("Pinned to CPU %d", cpu)
    except Exception as e:
        log.warning("Could not pin to CPU %d: %s", cpu, e)


def _drain_cmd_queue(q):
    cmds = []
    try:
        while True:
            cmds.append(q.get_nowait())
    except Exception:
        pass
    return cmds


def _handle_camera_cmd(cmd, cam, current_state):
    """Dispatch a CameraCmd. Returns a CameraCmdReply.

    current_state is a mutable dict tracking exposure/gain so 'get'
    commands don't need to interrogate picamera2 directly.
    """
    from efinder.worker_cmds import (
        CameraCmdReply,
        CAMERA_OP_GET_EXPOSURE, CAMERA_OP_SET_EXPOSURE, CAMERA_OP_SET_GAIN,
    )
    try:
        if cmd.op == CAMERA_OP_GET_EXPOSURE:
            return CameraCmdReply(
                request_id=cmd.request_id, ok=True,
                result={"exposure_s": current_state["exposure_s"],
                        "gain": current_state["gain"]})
        if cmd.op == CAMERA_OP_SET_EXPOSURE:
            new_s = float(cmd.args["exposure_s"])
            if not (0.001 <= new_s <= 10.0):
                return CameraCmdReply(
                    request_id=cmd.request_id, ok=False,
                    error=f"exposure_s {new_s} out of range [0.001, 10.0]")
            cam.set_controls({
                "ExposureTime": int(new_s * 1_000_000),
                "FrameDurationLimits": (
                    int(new_s * 1_000_000),
                    1_000_000_000,
                ),
            })
            current_state["exposure_s"] = new_s
            log.info("exposure -> %.3fs", new_s)
            return CameraCmdReply(
                request_id=cmd.request_id, ok=True,
                result={"exposure_s": new_s})
        if cmd.op == CAMERA_OP_SET_GAIN:
            new_g = float(cmd.args["gain"])
            if not (1.0 <= new_g <= 64.0):
                return CameraCmdReply(
                    request_id=cmd.request_id, ok=False,
                    error=f"gain {new_g} out of range [1.0, 64.0]")
            cam.set_controls({"AnalogueGain": new_g})
            current_state["gain"] = new_g
            log.info("gain -> %.1f", new_g)
            return CameraCmdReply(
                request_id=cmd.request_id, ok=True,
                result={"gain": new_g})
        return CameraCmdReply(
            request_id=cmd.request_id, ok=False,
            error=f"unknown camera op: {cmd.op!r}")
    except Exception as e:
        return CameraCmdReply(
            request_id=cmd.request_id, ok=False,
            error=f"{type(e).__name__}: {e}")


def camera_main(slots, camera_cmd_q, camera_cmd_reply_q, cfg):
    logging.basicConfig(
        level=os.environ.get("EFINDER_LOGLEVEL", "INFO"),
        format="camera %(levelname)s %(message)s",
    )
    _pin_to_cpu(cfg.cpu_camera)

    try:
        from picamera2 import Picamera2
    except ImportError as e:
        log.error("picamera2 not importable: %s", e)
        time.sleep(10); raise

    cam = Picamera2()
    config = cam.create_still_configuration(
        main={"format": "YUV420",
              "size": (cfg.frame_width, cfg.frame_height)},
        controls={
            "ExposureTime": int(cfg.exposure_s * 1_000_000),
            "AnalogueGain": float(cfg.gain),
            "AeEnable": False,
            "AwbEnable": False,
            # Min = exposure time (hard physics floor).
            # Max = large sentinel; let the hardware/ISP determine the
            # actual achievable rate. The old value of exposure+200ms
            # capped the camera at 2.5fps with the default 0.2s exposure,
            # even though the solver would benefit from frames sooner.
            "FrameDurationLimits": (
                int(cfg.exposure_s * 1_000_000),
                1_000_000_000,
            ),
        },
    )
    cam.configure(config)
    cam.start()
    log.info("Camera started: %dx%d exp=%.3fs gain=%.1f",
             cfg.frame_width, cfg.frame_height, cfg.exposure_s, cfg.gain)

    shms = [shared_memory.SharedMemory(name=f"{SHM_PREFIX}_{i}")
            for i in range(NUM_BUFFERS)]
    bufs = [np.ndarray((cfg.frame_height, cfg.frame_width), dtype=np.uint8,
                        buffer=s.buf) for s in shms]

    # Track current camera settings so commands can read them without
    # round-tripping through picamera2's metadata.
    current_state = {"exposure_s": cfg.exposure_s, "gain": cfg.gain}

    frame_count = 0
    last_log = time.monotonic()

    try:
        while True:
            # Process any pending camera commands first.
            for cmd in _drain_cmd_queue(camera_cmd_q):
                reply = _handle_camera_cmd(cmd, cam, current_state)
                try:
                    camera_cmd_reply_q.put_nowait(reply)
                except Exception as e:
                    log.warning("Could not enqueue camera reply: %s", e)

            idx = slots.acquire_write_slot()
            arr = cam.capture_array("main")
            # YUV420 packed into (H*3/2, W); first H rows are Y (luminance).
            np.copyto(bufs[idx], arr[:cfg.frame_height, :cfg.frame_width])
            slots.publish(idx)

            frame_count += 1
            now = time.monotonic()
            if now - last_log > 30.0:
                fps = frame_count / (now - last_log)
                log.info("captured %d frames (%.1f fps)", frame_count, fps)
                frame_count = 0; last_log = now
    finally:
        cam.stop()
        for s in shms: s.close()
