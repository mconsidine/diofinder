#!/usr/bin/env python3
"""
diag_camera.py — Camera exposure/gain sweep diagnostic for diofinder.

Captures one frame for every combination of exposure and gain in the
specified ranges, with optional 2×2 software binning.  Individual frames are
named:

    YYYYMMDDHHMMSSMMM-EEE-GG[-2x2].png

where EEE is exposure in milliseconds (zero-padded to 3 digits) and GG is
the analogue gain rounded to the nearest integer.  Example:

    20260603190304010-050-20-2x2.png   ← 50 ms, gain 20, 2×2 binned

After all frames are captured they are bundled into a ZIP archive named
after the sweep start timestamp (e.g. 20260603190304010.zip) and the
individual PNGs are deleted.  The ZIP is the only artifact left in the
output directory, making it easy to transfer off the device.

The ZIP always contains two extra files for diagnostic purposes:
  capture_info.txt  — sweep parameters, system info, and live daemon status
  diofinder.conf      — copy of /etc/diofinder/diofinder.conf at capture time

Files are written to the directory where test.png lives (/var/lib/diofinder
by default), or the current working directory if test.png is not found.

Usage:
    sudo /opt/diofinder/venv/bin/python3 tests/diag_camera.py [options]

Examples:
    # Default sweep (exposures 0.05–0.30 s step 0.05, gains 15–40 step 5)
    sudo .../diag_camera.py

    # Custom range with 2×2 binning
    sudo .../diag_camera.py --exp-min 0.1 --exp-max 0.5 --exp-step 0.1 \\
                             --gain-min 10 --gain-max 30 --gain-step 5 \\
                             --binning

    # Single exposure/gain pair (set min = max)
    sudo .../diag_camera.py --exp-min 0.2 --exp-max 0.2 \\
                             --gain-min 20 --gain-max 20

    # Override frame size or output directory
    sudo .../diag_camera.py --width 480 --height 380 --output-dir /tmp/frames

Requires: picamera2, numpy, Pillow  (all present in the diofinder venv)
Must run as root (or a member of the 'video' group) on the Pi.
"""

import argparse
import datetime
import json
import logging
import os
import platform
import socket
import sys
import time
import zipfile
from pathlib import Path

import numpy as np

log = logging.getLogger("diag_camera")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_output_dir() -> Path:
    """Return the directory where test.png lives, or cwd."""
    for d in [Path("/var/lib/diofinder"), Path.cwd(), Path("/opt/diofinder")]:
        if (d / "test.png").exists():
            return d
    return Path.cwd()


def _arange_float(start: float, stop: float, step: float) -> list:
    """Inclusive float range.  Avoids floating-point fence-post errors."""
    vals, v = [], start
    while v <= stop + 1e-9:
        vals.append(round(v, 9))
        v += step
    return vals


def _make_filename(exp_s: float, gain: float, binning: bool) -> str:
    now    = datetime.datetime.now()
    ms     = now.microsecond // 1000
    ts     = now.strftime("%Y%m%d%H%M%S") + f"{ms:03d}"
    exp_ms = int(round(exp_s * 1000))
    gs     = str(int(round(gain)))
    suffix = "-2x2" if binning else ""
    return f"{ts}-{exp_ms:03d}-{gs}{suffix}.png"


def _bin2x2(frame: np.ndarray) -> np.ndarray:
    """2×2 average downsample.  Input (H, W) uint8 → output (H//2, W//2) uint8."""
    h, w   = frame.shape
    frame  = frame[: h - h % 2, : w - w % 2]
    h, w   = frame.shape
    return (frame.reshape(h // 2, 2, w // 2, 2)
                 .mean(axis=(1, 3))
                 .astype(np.uint8))


def _query_daemon_status() -> str:
    """Query the diofinder maint socket for live status. Returns formatted string."""
    try:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(2.0)
        s.connect("/run/diofinder/maint.sock")
        s.sendall(b'{"cmd":"status","args":{}}\n')
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        status = json.loads(buf.split(b"\n")[0])
        return json.dumps(status, indent=2)
    except Exception as exc:
        return f"(unavailable: {exc})"


def _build_info_txt(args, exposures, gains, width, height,
                    sweep_ts, elapsed, saved, total) -> str:
    """Build the capture_info.txt content bundled into the ZIP."""
    lines = []

    lines += [
        "diofinder camera diagnostic sweep — capture info",
        "=" * 52,
        f"Sweep timestamp : {sweep_ts}",
        f"Captured        : {saved}/{total} frames in {elapsed:.1f} s",
        f"Frame size      : {width}×{height} px (capture resolution)",
        f"Software binning: {'2×2 (saved images are half-size)' if args.binning else 'none (matches daemon resolution)'}",
        f"Exposures       : {', '.join(f'{e:.3f}s' for e in exposures)}",
        f"Gains           : {', '.join(f'{g:.0f}' for g in gains)}",
        f"Warmup frames   : {args.warmup} per setting",
        "",
        "Frame pipeline note",
        "-" * 52,
        "The IMX477 native sensor is 4056×3040.  When picamera2 is asked for",
        f"{width}×{height}, it selects the 2×2 hardware-binned sensor mode",
        "(2028×1520) and the ISP scales the result to the requested size.",
        "The full sensor area (full FOV) is always used — this is NOT a crop.",
        "Captured PNGs are raw 8-bit grayscale Y-plane, identical to what the",
        "diofinder solver receives.  No display stretch is applied.",
        "",
    ]

    # System info
    lines += ["System", "-" * 52]
    lines.append(f"Hostname : {socket.gethostname()}")
    lines.append(f"Platform : {platform.platform()}")
    try:
        model = Path("/proc/device-tree/model").read_text().rstrip("\x00").strip()
        lines.append(f"Pi model : {model}")
    except Exception:
        pass
    try:
        import subprocess
        uname = subprocess.check_output(["uname", "-a"], text=True).strip()
        lines.append(f"uname    : {uname}")
    except Exception:
        pass
    lines.append("")

    # diofinder config
    conf_path = Path("/etc/diofinder/diofinder.conf")
    lines += ["diofinder.conf", "-" * 52]
    if conf_path.exists():
        lines.append(conf_path.read_text())
    else:
        lines.append("(not found — /etc/diofinder/diofinder.conf does not exist)")
    lines.append("")

    # live daemon status
    lines += ["Daemon status (maint socket)", "-" * 52]
    lines.append(_query_daemon_status())
    lines.append("")

    return "\n".join(lines)


def _camera_settings_text(cam, tuning_used, applied_controls) -> str:
    """Dump the picamera2 tuning, sensor properties, control defaults/ranges,
    and the controls this sweep applies."""
    lines = ["Camera (picamera2)", "=" * 52,
             f"Tuning file : {tuning_used}", ""]

    lines += ["Sensor properties", "-" * 52]
    try:
        props = dict(cam.camera_properties)
        for k in sorted(props):
            lines.append(f"  {k:26} {props[k]}")
    except Exception as e:
        lines.append(f"  (camera_properties unavailable: {e})")
    lines.append("")

    lines += ["Available controls  (min / max / default)", "-" * 52]
    try:
        cc = cam.camera_controls  # {name: (min, max, default)}
        for name in sorted(cc):
            lo, hi, dflt = cc[name]
            lines.append(f"  {name:26} {lo} / {hi} / {dflt}")
    except Exception as e:
        lines.append(f"  (camera_controls unavailable: {e})")
    lines.append("")

    lines += ["Sensor modes", "-" * 52]
    try:
        for i, m in enumerate(cam.sensor_modes):
            lines.append(
                f"  [{i}] size={m.get('size')} bit_depth={m.get('bit_depth')} "
                f"fps={m.get('fps')} format={m.get('format')}")
    except Exception as e:
        lines.append(f"  (sensor_modes unavailable: {e})")
    lines.append("")

    lines += ["Controls applied by this sweep", "-" * 52]
    for k, v in applied_controls.items():
        lines.append(f"  {k:26} {v}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Camera exposure/gain sweep — saves one PNG per combination.")
    ap.add_argument("--exp-min",    type=float, default=0.05,
                    metavar="S", help="Min exposure in seconds (default: 0.05)")
    ap.add_argument("--exp-max",    type=float, default=0.30,
                    metavar="S", help="Max exposure in seconds (default: 0.30)")
    ap.add_argument("--exp-step",   type=float, default=0.05,
                    metavar="S", help="Exposure step in seconds (default: 0.05)")
    ap.add_argument("--gain-min",   type=float, default=15.0,
                    metavar="G", help="Min analogue gain (default: 15)")
    ap.add_argument("--gain-max",   type=float, default=40.0,
                    metavar="G", help="Max analogue gain (default: 40)")
    ap.add_argument("--gain-step",  type=float, default=5.0,
                    metavar="G", help="Gain step (default: 5)")
    ap.add_argument("--binning",    action="store_true",
                    help="Apply 2×2 software binning (halves width and height)")
    ap.add_argument("--width",      type=int, default=None,
                    help="Frame width in pixels (default: from diofinder config or 960)")
    ap.add_argument("--height",     type=int, default=None,
                    help="Frame height in pixels (default: from diofinder config or 760)")
    ap.add_argument("--warmup",     type=int, default=3,
                    help="Frames to discard after each settings change (default: 3)")
    ap.add_argument("--output-dir", type=Path, default=None,
                    metavar="PATH",
                    help="Save directory (default: where test.png lives)")
    ap.add_argument("--tuning-file", default=None, metavar="PATH",
                    help="libcamera tuning JSON (default: the IMX477 scientific "
                         "profile; pass '' or a missing path to use libcamera's "
                         "built-in tuning)")
    ap.add_argument("--info-only", action="store_true",
                    help="Print camera tuning/properties/controls and exit "
                         "(no capture sweep)")
    args = ap.parse_args()

    # ---- Resolve frame dimensions from config --------------------------------
    width, height = args.width, args.height
    cfg = None
    if width is None or height is None:
        try:
            sys.path.insert(0, "/opt/diofinder")
            from diofinder.config import load_config
            cfg = load_config()
            if width  is None: width  = cfg.frame_width
            if height is None: height = cfg.frame_height
        except Exception:
            if width  is None: width  = 960
            if height is None: height = 760

    # ---- Build sweep arrays --------------------------------------------------
    exposures = _arange_float(args.exp_min, args.exp_max, args.exp_step)
    gains     = _arange_float(args.gain_min, args.gain_max, args.gain_step)

    if not exposures:
        log.error("Empty exposure range (check --exp-min/max/step)")
        sys.exit(1)
    if not gains:
        log.error("Empty gain range (check --gain-min/max/step)")
        sys.exit(1)

    total = len(exposures) * len(gains)

    output_dir = args.output_dir or _find_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Rough time estimate: initial settle + (warmup+1) frames per combination
    mean_exp  = (exposures[0] + exposures[-1]) / 2.0
    est_s     = max(exposures[0] + 0.5, 1.5) + total * (args.warmup + 1) * mean_exp
    img_dims  = (f"{width//2}×{height//2} (after binning)"
                 if args.binning else f"{width}×{height}")

    print("=" * 60)
    print(" diofinder camera diagnostic sweep")
    print("=" * 60)
    log.info("Frame size:      %s", img_dims)
    log.info("Exposures (%d):  %s s",
             len(exposures), "  ".join(f"{e:.3f}" for e in exposures))
    log.info("Gains (%d):      %s",
             len(gains), "  ".join(f"{g:.0f}" for g in gains))
    log.info("Binning:         %s", "2×2 software" if args.binning else "none")
    log.info("Warmup frames:   %d per setting", args.warmup)
    log.info("Total captures:  %d", total)
    log.info("Output dir:      %s", output_dir)
    log.info("Est. duration:   %.0f s", est_s)
    print()

    # ---- Import dependencies -------------------------------------------------
    try:
        from picamera2 import Picamera2
    except ImportError:
        log.error("picamera2 not found — run on the Pi using the diofinder venv")
        sys.exit(1)

    try:
        from PIL import Image
    except ImportError:
        log.error("Pillow not found — pip install Pillow")
        sys.exit(1)

    # ---- Initialise camera ---------------------------------------------------
    # Use the IMX477 scientific tuning profile (suppresses ISP noise reduction,
    # sharpening, AWB, colour correction — all distort photometry). The correct
    # picamera2 API is load_tuning_file() -> Picamera2(tuning=...); there is no
    # `tuning_file=` constructor kwarg. Change 'vc4' to 'pisp' on Pi 5.
    default_tuning = "/usr/share/libcamera/ipa/rpi/vc4/imx477_scientific.json"
    tuning_path = default_tuning if args.tuning_file is None else args.tuning_file
    tuning_used = "default (libcamera built-in)"
    cam = None
    if tuning_path and os.path.exists(tuning_path):
        try:
            _d, _fn = os.path.split(tuning_path)
            _tuning = Picamera2.load_tuning_file(_fn, dir=(_d or None))
            cam = Picamera2(tuning=_tuning)
            tuning_used = tuning_path
        except Exception as e:
            log.warning("Could not load tuning %s (%s) — using default tuning",
                        tuning_path, e)
    elif tuning_path:
        log.warning("Tuning file not found at %s — using default tuning", tuning_path)
    if cam is None:
        cam = Picamera2()

    init_exp   = exposures[0]
    init_gain  = gains[0]
    init_exp_us = int(init_exp * 1_000_000)

    applied_controls = {
        "ExposureTime":        init_exp_us,
        "AnalogueGain":        float(init_gain),
        "AeEnable":            False,
        "AwbEnable":           False,
        "NoiseReductionMode":  0,
        "Sharpness":           0.0,
        "Saturation":          0.0,
        "FrameDurationLimits": (init_exp_us, 1_000_000_000),
    }
    config = cam.create_still_configuration(
        main={"format": "YUV420", "size": (width, height)},
        controls=applied_controls,
    )
    cam.configure(config)

    # Dump tuning / sensor properties / control defaults-and-ranges before the
    # sweep. camera_controls/properties are populated after configure().
    cam_settings = _camera_settings_text(cam, tuning_used, applied_controls)
    print()
    print(cam_settings)
    print()
    if args.info_only:
        cam.close()
        return

    cam.start()

    # Let the first settings settle before the loop begins.
    settle_s = max(init_exp + 0.5, 1.5)
    log.info("Camera started — settling for %.1f s …", settle_s)
    time.sleep(settle_s)

    # ---- Capture loop --------------------------------------------------------
    # Record the sweep start timestamp once — used as the ZIP filename.
    sweep_dt  = datetime.datetime.now()
    sweep_ts  = sweep_dt.strftime("%Y%m%d%H%M%S") + f"{sweep_dt.microsecond // 1000:03d}"

    saved       = 0
    saved_paths = []
    t_start     = time.monotonic()

    try:
        for exp_s in exposures:
            for gain in gains:
                # Push new settings to the ISP
                cam.set_controls({
                    "ExposureTime":        int(exp_s * 1_000_000),
                    "AnalogueGain":        float(gain),
                    "FrameDurationLimits": (int(exp_s * 1_000_000), 1_000_000_000),
                })

                # Discard warmup frames so the ISP has fully applied the new
                # exposure and gain before we capture the keeper
                for _ in range(args.warmup):
                    cam.capture_array("main")

                # Capture the keeper
                arr   = cam.capture_array("main")
                frame = arr[:height, :width].copy()   # Y plane (grayscale)

                if args.binning:
                    frame = _bin2x2(frame)

                fname = _make_filename(exp_s, gain, args.binning)
                fpath = output_dir / fname
                Image.fromarray(frame, mode="L").save(fpath, format="PNG")
                saved_paths.append(fpath)

                saved += 1
                peak   = int(frame.max())
                mean   = float(frame.mean())
                log.info("[%2d/%d]  exp=%5.3fs  gain=%4.0f  peak=%3d  mean=%5.1f  %s",
                         saved, total, exp_s, gain, peak, mean, fname)

    finally:
        cam.stop()

    elapsed = time.monotonic() - t_start
    print()
    log.info("Captured %d/%d frames in %.1f s", saved, total, elapsed)
    if saved < total:
        log.warning("%d captures were not saved (camera error)", total - saved)

    # ---- Bundle into ZIP and remove individual PNGs -------------------------
    if saved_paths:
        zip_path = output_dir / f"{sweep_ts}.zip"
        log.info("Creating %s …", zip_path)

        info_txt  = _build_info_txt(args, exposures, gains, width, height,
                                    sweep_ts, elapsed, saved, total)
        conf_path = Path("/etc/diofinder/diofinder.conf")

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
            for p in saved_paths:
                zf.write(p, arcname=p.name)
            zf.writestr("capture_info.txt", info_txt)
            zf.writestr("camera_settings.txt", cam_settings)
            if conf_path.exists():
                zf.write(conf_path, arcname="diofinder.conf")

        # Verify the archive is intact before deleting the source files
        with zipfile.ZipFile(zip_path, "r") as zf:
            bad = zf.testzip()
        if bad is not None:
            log.error("ZIP integrity check failed on %s — PNGs NOT deleted", bad)
        else:
            for p in saved_paths:
                p.unlink()
            log.info("ZIP OK (%d files + capture_info.txt + diofinder.conf, %.1f MB)"
                     " — individual PNGs deleted",
                     len(saved_paths),
                     zip_path.stat().st_size / 1_048_576)
            log.info("Archive: %s", zip_path)
        log.info("Transfer with:  scp diofinder@diofinder.local:%s .", zip_path)
    else:
        log.warning("No frames captured — no ZIP created")


if __name__ == "__main__":
    main()
