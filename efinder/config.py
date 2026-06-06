"""
eFinder runtime configuration.

Single source of truth for every value a user might want to tune
without editing code. Loaded from /etc/efinder/efinder.conf
(key:value pairs, # for comments). Per-key environment overrides
EFINDER_<KEY> win for ops without editing the file.

Defaults are conservative for the Pi Zero 2W + Arducam 12 MP target.

Anything here can be changed at runtime by editing the conf file and
restarting the service (`sudo systemctl restart efinder`). No code
push required.
"""

import dataclasses
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger("efinder.config")

DEFAULT_CONFIG_PATH = "/etc/efinder/efinder.conf"


@dataclasses.dataclass
class Config:
    # -------- Identity --------
    version: str = "0.8.0"

    # -------- Camera --------
    frame_width: int = 960
    frame_height: int = 760

    # Full sensor readout dimensions for the IMX477.  picamera2's
    # create_still_configuration defaults to the smallest sensor sub-mode that
    # can produce the requested output size (1332×990 for 960×760), which crops
    # ~35% of the sensor and reduces FOV.  Specifying the full array forces the
    # ISP to downscale from the complete sensor, restoring the expected FOV.
    sensor_full_width: int = 4056
    sensor_full_height: int = 3040

    # Path to the libcamera IMX477 scientific tuning profile.  The scientific
    # profile disables all ISP processing (AGC, AWB, noise reduction, sharpening,
    # colour correction) that would corrupt photometry.  Change 'vc4' to 'pisp'
    # if running on a Pi 5.  Set to "" to use the default tuning file.
    camera_tuning_file: str = "/usr/share/libcamera/ipa/rpi/vc4/imx477_scientific.json"

    exposure_s: float = 0.2
    gain: float = 20.0

    auto_exposure_enabled: bool = False
    auto_exposure_target_stars: int = 20
    auto_exposure_min_s: float = 0.05
    auto_exposure_max_s: float = 1.0

    # Optical properties
    fov_deg: float = 13.5
    arcsec_per_pixel: float = 51.15  # 6.2 µm eff. pixel × 206265 / 25 mm FL

    latitude_deg: float = 0.0
    longitude_deg: float = 0.0

    fov_calibrated: bool = False
    fov_calibrated_stddev: float = 0.05
    fov_calibrated_max_error_deg: float = 0.1

    distortion: float = 0.0

    # -------- Star detection (sycamore, matched_filter gate) --------
    # sigma threshold passed to star_detect.detect_stars.
    # Matches the shipped efinder.conf.default so the fallback (no conf file)
    # agrees with what devices actually run.
    detect_sigma: float = 9.0

    # Detection binning passed to star_detect (1=full-res, 2=2x2-binned).
    detect_bin: int = 1

    # Per-frame background mode: "row_percentile" (default, cheapest),
    # "line_median" (robust to per-row offset/vignetting), or "top_hat"
    # (opt-in morphological 2-D gradient removal — needs sycamore >= 0.9.0).
    detect_bg_mode: str = "row_percentile"
    # Structuring-element radius (px) used only when detect_bg_mode == "top_hat".
    # Must be comfortably larger than the largest star radius.
    detect_tophat_radius: int = 12
    # Tile side length (px) for block_percentile mode. 0 = use sycamore's
    # default (32 for bin=2). Must exceed the largest star radius.
    detect_bg_block_size: int = 0
    # Sliding-window side length (px) for uniform_mean mode. 0 = use sycamore's
    # default (25, matching tetra3/olive-solve filtsize=25).
    detect_uniform_filter_size: int = 0
    # Noise estimation mode: "mad" (default, robust) or "global_rms" (faster,
    # matches tetra3/olive-solve GlobalRootSquare). Set "global_rms" when using
    # uniform_mean to replicate the tetra3 pipeline exactly.
    detect_noise_mode: str = "mad"

    # -------- Temporal "analytic-threading" background cache --------
    # When enabled, a worker thread in solver_proc maintains a temporally
    # median-stacked per-row background + noise model; steady-state detection
    # consumes it via detect_stars_with_cache (√N noise reduction, free
    # hot-pixel rejection). Falls back to per-frame detection during slew /
    # warm-up. Set false to disable entirely if the per-frame submit/stack
    # bookkeeping proves too costly.
    bg_cache_enabled: bool = True
    bg_cache_stack: int = 8            # frames median-stacked per rebuild
    bg_cache_refresh_s: float = 5.0    # min interval between rebuilds
    bg_cache_slew_deg: float = 0.5     # IMU angle that invalidates the cache
    bg_cache_max_age_s: float = 60.0   # rebuild if model older than this

    # -------- Solver (olive-solve tetra3-py) --------
    # Path to a tetra3 .npz star database compatible with olive-solve.
    solver_db: str = "default_database"
    fov_max_error_deg: float = 1.0
    min_centroids: int = 8
    max_solve_stars: int = 50
    solve_timeout_ms: int = 1500
    match_threshold: float = 1e-5
    match_radius: float = 0.01

    # -------- Boresight offset --------
    boresight_y: float = 380.0   # frame_height / 2
    boresight_x: float = 480.0   # frame_width / 2

    # -------- Comms --------
    lx200_port: int = 4060
    lx200_client_timeout_s: float = 30.0

    # -------- CPU affinity --------
    # Pi Zero 2W: 4 cores (0 = kernel/IRQs, never pinned).
    #   1 = comms_proc + efinder-webui  (I/O bound)
    #   2 = solver_proc primary core
    #   3 = camera_proc + solver_proc secondary core
    #
    # The solver process is allowed affinity {cpu_solver, cpu_camera}
    # so olive-solve's rayon thread pool can spread solving work
    # across two physical cores (CPUs 2 and 3).
    cpu_camera: int = 3
    cpu_solver: int = 2
    cpu_comms: int = 1

    # -------- Diagnostics --------
    save_failed_frames: bool = False
    save_solved_frames: bool = False
    failed_frames_dir: str = "/var/lib/efinder/captures"
    log_solve_stats_every_n: int = 50

    # -------- Shutdown --------
    shutdown_grace_s: float = 2.0

    def summary(self) -> str:
        return (
            f"exp={self.exposure_s}s gain={self.gain} "
            f"fov={self.fov_deg}deg sigma={self.detect_sigma} "
            f"db={self.solver_db} "
            f"boresight=({self.boresight_y:.1f},{self.boresight_x:.1f}) "
            f"affinity[cam={self.cpu_camera},solv={self.cpu_solver},comm={self.cpu_comms}]"
        )


def _coerce(value: str, target_type):
    if target_type is bool:
        return value.strip().lower() in ("1", "true", "yes", "on")
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    return value.strip()


def load_config(path: Optional[str] = None) -> Config:
    cfg = Config()
    p = Path(path or os.environ.get("EFINDER_CONFIG", DEFAULT_CONFIG_PATH))

    if p.exists():
        for raw in p.read_text().splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            if hasattr(cfg, key):
                target_type = type(getattr(cfg, key))
                try:
                    setattr(cfg, key, _coerce(value, target_type))
                except Exception as e:
                    log.warning("Bad config %s=%r: %s", key, value, e)
            else:
                log.warning("Unknown config key %r ignored", key)
    else:
        log.warning("Config file %s missing; using defaults", p)

    for f in dataclasses.fields(cfg):
        env_key = "EFINDER_" + f.name.upper()
        if env_key in os.environ:
            try:
                setattr(cfg, f.name, _coerce(os.environ[env_key], type(getattr(cfg, f.name))))
            except Exception as e:
                log.warning("Bad env %s=%r: %s", env_key, os.environ[env_key], e)
    return cfg


def save_keys(updates: dict, path: Optional[str] = None) -> None:
    """Write key/value updates back to the config file in place,
    preserving comments and unknown lines."""
    p = Path(path or os.environ.get("EFINDER_CONFIG", DEFAULT_CONFIG_PATH))
    if not p.exists():
        log.warning("Cannot save updates; config file %s missing", p)
        return
    lines = p.read_text().splitlines()
    out = []
    seen = set()
    for line in lines:
        stripped = line.split("#", 1)[0].strip()
        if ":" in stripped:
            key = stripped.split(":", 1)[0].strip().lower()
            if key in updates:
                value = updates[key]
                out.append(f"{key}: {_format_value(value)}")
                seen.add(key)
                continue
        out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}: {_format_value(value)}")
    p.write_text("\n".join(out) + "\n")


def _format_value(v):
    if isinstance(v, float):
        return f"{v:.6f}"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)
