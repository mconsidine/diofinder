"""
Camera worker process — combo branch.

Unified loop that supports runtime switching between test mode (static
test image at ~5 fps) and live mode (picamera2). The active mode is
read from shared_cfg["test_mode"] on every iteration.

Behaviour:
  * Defaults to test mode if a test image was provided at startup.
  * Lazily initialises picamera2 the first time live mode is requested.
  * If picamera2 fails to initialise, reverts to test mode automatically
    and logs an error.
  * Camera commands (exposure/gain) are accepted in both modes; in test
    mode they update the stored state but don’t touch hardware.
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
    """Dispatch a CameraCmd. cam may be None (test mode); hardware ops are
    skipped when cam is None but state is still updated.
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
            if cam is not None:
                cam.set_controls({
                    "ExposureTime": int(new_s * 1_000_000),
                    "FrameDurationLimits": (
                        int(new_s * 1_000_000),
                        1_000_000_000,
                    ),
                })
            current_state["exposure_s"] = new_s
            log.info("exposure -> %.3fs%s", new_s,
                     "" if cam is not None else " (test mode, stored only)")
            return CameraCmdReply(
                request_id=cmd.request_id, ok=True,
                result={"exposure_s": new_s})
        if cmd.op == CAMERA_OP_SET_GAIN:
            new_g = float(cmd.args["gain"])
            if not (1.0 <= new_g <= 64.0):
                return CameraCmdReply(
                    request_id=cmd.request_id, ok=False,
                    error=f"gain {new_g} out of range [1.0, 64.0]")
            if cam is not None:
                cam.set_controls({"AnalogueGain": new_g})
            current_state["gain"] = new_g
            log.info("gain -> %.1f%s", new_g,
                     "" if cam is not None else " (test mode, stored only)")
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


def _load_test_image(path, frame_height, frame_width):
    """Load a PNG as a grayscale uint8 array sized (frame_height, frame_width)."""
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError(
            "Pillow is required for test-image mode: pip install Pillow")
    img = Image.open(path).convert("L")
    if img.size != (frame_width, frame_height):
        log.info("Test image %dx%d -> resizing to %dx%d",
                 img.width, img.height, frame_width, frame_height)
        img = img.resize((frame_width, frame_height), Image.LANCZOS)
    return np.array(img, dtype=np.uint8)


def _find_test_image(cfg):
    """Search standard paths for a test image; return loaded array or None."""
    from pathlib import Path
    search_dirs = [Path.cwd(), Path("/var/lib/efinder"), Path("/opt/efinder")]
    for name in ("test.png", "polaris.png"):
        for d in search_dirs:
            p = d / name
            if p.exists():
                try:
                    frame = _load_test_image(p, cfg.frame_height, cfg.frame_width)
                    log.info("Test image auto-discovered: %s", p)
                    return frame
                except Exception as e:
                    log.warning("Could not load test image %s: %s", p, e)
    return None


def _init_camera(cfg, current_state):
    """Initialise and start picamera2 using current_state for exposure/gain."""
    from picamera2 import Picamera2
    cam = Picamera2()
    # Request the full sensor readout (4056×3040) so the ISP downscales from
    # the complete pixel array rather than the default 1332×990 sub-mode.
    # Without this hint libcamera picks the smallest viable sensor mode, which
    # crops ~35% of the sensor and reduces the horizontal FOV from ~13.5° to
    # ~8.8° for a 25mm lens on the IMX477.
    full_sensor = (cfg.sensor_full_width, cfg.sensor_full_height)
    config = cam.create_still_configuration(
        main={"format": "YUV420",
              "size": (cfg.frame_width, cfg.frame_height)},
        sensor={"output_size": full_sensor},
        controls={
            "ExposureTime": int(current_state["exposure_s"] * 1_000_000),
            "AnalogueGain": float(current_state["gain"]),
            "AeEnable": False,
            "AwbEnable": False,
            "FrameDurationLimits": (
                int(current_state["exposure_s"] * 1_000_000),
                1_000_000_000,
            ),
        },
    )
    cam.configure(config)
    cam.start()
    log.info("Camera started: %dx%d exp=%.3fs gain=%.1f",
             cfg.frame_width, cfg.frame_height,
             current_state["exposure_s"], current_state["gain"])
    # Log the actual sensor mode chosen by libcamera so plate-scale can be
    # verified against the configured arcsec_per_pixel.
    try:
        sc = cam.camera_configuration().get("sensor", {})
        log.info("Sensor mode: output_size=%s bit_depth=%s",
                 sc.get("output_size"), sc.get("bit_depth"))
        raw = cam.camera_configuration().get("raw")
        if raw:
            log.info("Raw stream: size=%s format=%s",
                     raw.get("size"), raw.get("format"))
    except Exception as e:
        log.debug("Could not read sensor mode: %s", e)
    return cam


def camera_main(slots, camera_cmd_q, camera_cmd_reply_q, cfg,
                test_image_path=None, shared_cfg=None):
    logging.basicConfig(
        level=os.environ.get("EFINDER_LOGLEVEL", "INFO"),
        format="camera %(levelname)s %(message)s",
    )
    _pin_to_cpu(cfg.cpu_camera)

    test_frame = None
    if test_image_path is not None:
        log.info("Loading test image: %s", test_image_path)
        test_frame = _load_test_image(
            test_image_path, cfg.frame_height, cfg.frame_width)

    cam = None
    current_state = {"exposure_s": cfg.exposure_s, "gain": cfg.gain}

    shms = [shared_memory.SharedMemory(name=f"{SHM_PREFIX}_{i}")
            for i in range(NUM_BUFFERS)]
    bufs = [np.ndarray((cfg.frame_height, cfg.frame_width), dtype=np.uint8,
                        buffer=s.buf) for s in shms]

    frame_count = 0
    last_log = time.monotonic()

    try:
        while True:
            for cmd in _drain_cmd_queue(camera_cmd_q):
                reply = _handle_camera_cmd(cmd, cam, current_state)
                try:
                    camera_cmd_reply_q.put_nowait(reply)
                except Exception as e:
                    log.warning("Could not enqueue camera reply: %s", e)

            # Determine active mode
            if shared_cfg is not None:
                use_test = bool(shared_cfg.get("test_mode", test_frame is not None))
            else:
                use_test = test_frame is not None

            if use_test:
                if test_frame is None:
                    test_frame = _find_test_image(cfg)
                if test_frame is None:
                    log.warning(
                        "Test mode requested but no test image found; "
                        "switching to live mode")
                    if shared_cfg is not None:
                        shared_cfg["test_mode"] = False
                    use_test = False
                else:
                    idx = slots.acquire_write_slot()
                    np.copyto(bufs[idx], test_frame)
                    slots.publish(idx)
                    frame_count += 1
                    now = time.monotonic()
                    if now - last_log > 30.0:
                        fps = frame_count / (now - last_log)
                        log.info("test-image: %d frames (%.1f fps)",
                                 frame_count, fps)
                        frame_count = 0; last_log = now
                    time.sleep(0.2)
                    continue

            # Live mode
            if cam is None:
                try:
                    cam = _init_camera(cfg, current_state)
                except Exception as e:
                    log.error("Camera init failed: %s; reverting to test mode", e)
                    if shared_cfg is not None:
                        shared_cfg["test_mode"] = True
                    time.sleep(2.0)
                    continue

            idx = slots.acquire_write_slot()
            try:
                arr = cam.capture_array("main")
                np.copyto(bufs[idx], arr[:cfg.frame_height, :cfg.frame_width])
            except Exception as e:
                log.error("Camera capture failed: %s", e)
            slots.publish(idx)

            frame_count += 1
            now = time.monotonic()
            if now - last_log > 30.0:
                fps = frame_count / (now - last_log)
                log.info("captured %d frames (%.1f fps)", frame_count, fps)
                frame_count = 0; last_log = now

    finally:
        if cam is not None:
            try: cam.stop()
            except Exception: pass
        for s in shms:
            s.close()
