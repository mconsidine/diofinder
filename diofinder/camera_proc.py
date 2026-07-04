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

from diofinder.frame_slots import SHM_PREFIX, NUM_BUFFERS
from multiprocessing import shared_memory

log = logging.getLogger("diofinder.camera")

# IMX477 maximum *analog* gain. libcamera's imx477 driver tops out here
# (gain register 1024/(1024-code)); any AnalogueGain request above this is
# silently realized as digital gain — a pure brightness multiply with zero SNR
# benefit. Clamp at the analog ceiling so the controller/UI can't ask for
# pointless digital gain. The auto-exposure ladder ceiling is separate and
# lower (auto_exposure_max_gain, default 16).
MAX_ANALOG_GAIN = 22.26


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


def _bump_camera_epoch(shared_cfg):
    """Advance the camera-settings epoch after a successful exposure/gain
    change. bg_cache watches this: frames captured at the old setting no
    longer share the new frames' background pedestal, so the temporal stack
    must be flushed (single writer — this process — so read+write is safe)."""
    if shared_cfg is not None:
        try:
            shared_cfg["camera_settings_epoch"] = (
                shared_cfg.get("camera_settings_epoch", 0) + 1)
        except Exception:
            pass


def _handle_camera_cmd(cmd, cam, current_state, shared_cfg=None):
    """Dispatch a CameraCmd. cam may be None (test mode); hardware ops are
    skipped when cam is None but state is still updated.
    """
    from diofinder.worker_cmds import (
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
            if new_s == current_state.get("exposure_s"):
                # No-op set (e.g. auto_tune/dark_capture restoring the same
                # value): don't touch the camera and, critically, don't bump
                # the settings epoch — each bump flushes the temporal
                # background stack for a full rebuild period.
                return CameraCmdReply(
                    request_id=cmd.request_id, ok=True,
                    result={"exposure_s": new_s})
            if cam is not None:
                cam.set_controls({
                    "ExposureTime": int(new_s * 1_000_000),
                    "FrameDurationLimits": (
                        int(new_s * 1_000_000),
                        1_000_000_000,
                    ),
                })
            current_state["exposure_s"] = new_s
            _bump_camera_epoch(shared_cfg)
            log.info("exposure -> %.3fs%s", new_s,
                     "" if cam is not None else " (test mode, stored only)")
            return CameraCmdReply(
                request_id=cmd.request_id, ok=True,
                result={"exposure_s": new_s})
        if cmd.op == CAMERA_OP_SET_GAIN:
            new_g = float(cmd.args["gain"])
            if not (1.0 <= new_g <= MAX_ANALOG_GAIN):
                return CameraCmdReply(
                    request_id=cmd.request_id, ok=False,
                    error=f"gain {new_g} out of range [1.0, {MAX_ANALOG_GAIN}]")
            if new_g == current_state.get("gain"):
                return CameraCmdReply(
                    request_id=cmd.request_id, ok=True,
                    result={"gain": new_g})
            if cam is not None:
                cam.set_controls({"AnalogueGain": new_g})
            current_state["gain"] = new_g
            _bump_camera_epoch(shared_cfg)
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
    search_dirs = [Path.cwd(), Path("/var/lib/diofinder"), Path("/opt/diofinder")]
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
    import os
    from picamera2 import Picamera2

    # Load the IMX477 scientific tuning profile so the ISP does not apply noise
    # reduction, sharpening, AWB, or colour correction — all of which corrupt
    # photometry.  The path is configurable via cfg.camera_tuning_file; set it
    # to "" to fall back to the default libcamera tuning.
    tuning_file = getattr(cfg, "camera_tuning_file", "")
    if tuning_file and not os.path.exists(tuning_file):
        log.warning(
            "IMX477 scientific tuning file not found at %s — "
            "falling back to default tuning", tuning_file)
        tuning_file = ""
    # cam = Picamera2(tuning_file=tuning_file) if tuning_file else Picamera2()
    # added MattC per Claude Cowork
    # tuning_file= kwarg was added in picamera2 ≥ 0.3.17; older releases only
    # expose load_tuning_file() + tuning= (a pre-loaded dict).  Try the newer
    # API first; catch TypeError and fall back so older installs still work.
    if tuning_file:
        try:
            cam = Picamera2(tuning_file=tuning_file)
        except TypeError:
            log.info("tuning_file= kwarg not supported by this picamera2 version; "
                     "using load_tuning_file() fallback")
            try:
                tuning = Picamera2.load_tuning_file(tuning_file)
                cam = Picamera2(tuning=tuning)
            except Exception as e2:
                log.warning("Could not load tuning file via old API either: %s; "
                            "using default tuning", e2)
                cam = Picamera2()
    else:
        cam = Picamera2()

    # Request the full sensor readout (4056×3040) so the ISP downscales from
    # the complete pixel array rather than the default 1332×990 sub-mode.
    # Without this hint libcamera picks the smallest viable sensor mode, which
    # crops ~35% of the sensor and reduces the horizontal FOV from ~13.5° to
    # ~8.8° for a 25mm lens on the IMX477.
    full_sensor = (cfg.sensor_full_width, cfg.sensor_full_height)
    exp_us = int(current_state["exposure_s"] * 1_000_000)
    _controls = {
        "ExposureTime":        exp_us,
        "AnalogueGain":        float(current_state["gain"]),
        "AeEnable":            False,
        "AwbEnable":           False,
        "NoiseReductionMode":  0,
        "Sharpness":           0.0,
        "Saturation":          0.0,
        "FrameDurationLimits": (exp_us, 1_000_000_000),
    }
    # buffer_count=2: with the still-configuration default of ONE buffer the
    # sensor cannot expose frame N+1 while frame N's buffer is held, so the
    # frame period collapses to ~2x the exposure time (measured on-device:
    # 1.0 s exposure -> 0.5 fps, 0.2 s -> 2.1 fps). Two buffers restore
    # ~1/exposure cadence — doubling the solve rate at long exposures.
    # raw=None: nothing reads the RAW stream, and at full-res it costs
    # ~18.5 MB of CMA per buffer; dropping it more than pays for the second
    # main buffer. The sensor mode is still forced by the sensor= hint.
    # Fallback keeps the raw stream for older picamera2 that may reject
    # raw=None (mirrors the tuning_file fallback above).
    try:
        config = cam.create_still_configuration(
            main={"format": "YUV420",
                  "size": (cfg.frame_width, cfg.frame_height)},
            raw=None,
            buffer_count=2,
            sensor={"output_size": full_sensor},
            controls=_controls,
        )
        cam.configure(config)
    except Exception as e:
        log.info("raw=None/buffer_count config rejected (%s); "
                 "falling back to legacy still configuration", e)
        config = cam.create_still_configuration(
            main={"format": "YUV420",
                  "size": (cfg.frame_width, cfg.frame_height)},
            sensor={"output_size": full_sensor},
            controls=_controls,
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
        level=os.environ.get("DIOFINDER_LOGLEVEL", "INFO"),
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
    init_fails = 0
    last_log = time.monotonic()

    try:
        test_mode_cached = False
        test_mode_read_t = -10.0
        while True:
            for cmd in _drain_cmd_queue(camera_cmd_q):
                reply = _handle_camera_cmd(cmd, cam, current_state, shared_cfg)
                try:
                    camera_cmd_reply_q.put_nowait(reply)
                except Exception as e:
                    log.warning("Could not enqueue camera reply: %s", e)

            # Determine active mode. TTL-cached: the bare get was one
            # Manager RPC per frame on CPU 3 — a solver rayon core (audit
            # 2026-07 P10). test_mode flips via a maint command, so a 1 s
            # stale read is invisible.
            now_tm = time.monotonic()
            if shared_cfg is not None:
                if now_tm - test_mode_read_t > 1.0:
                    test_mode_cached = bool(
                        shared_cfg.get("test_mode", test_frame is not None))
                    test_mode_read_t = now_tm
                use_test = test_mode_cached
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
                    init_fails = 0
                except Exception as e:
                    init_fails += 1
                    if init_fails < 5:
                        # Transient (busy device / CMA fragmentation at boot):
                        # retry live before giving up. Reverting to test mode
                        # on the FIRST failure served the canned test image as
                        # real pointing with no client-visible indication.
                        log.error("Camera init failed (attempt %d/5): %s",
                                  init_fails, e)
                        time.sleep(2.0)
                        continue
                    log.critical(
                        "Camera init failed %d times: %s — REVERTING TO TEST "
                        "MODE (canned image; reported positions are NOT real "
                        "sky). Fix the camera and restart.", init_fails, e)
                    if shared_cfg is not None:
                        shared_cfg["test_mode"] = True
                    time.sleep(2.0)
                    continue

            idx = slots.acquire_write_slot()
            try:
                arr = cam.capture_array("main")
                np.copyto(bufs[idx], arr[:cfg.frame_height, :cfg.frame_width])
            except Exception as e:
                # Do NOT publish: the slot holds an old frame, and publishing
                # it would hand the solver stale sky with a fresh sequence
                # number — pointing looks live while actually frozen, and the
                # no-sleep retry loop would peg a solver core. Back off and
                # let a persistent fault surface as a stale epoch instead.
                log.error("Camera capture failed: %s", e)
                time.sleep(0.5)
                continue
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
