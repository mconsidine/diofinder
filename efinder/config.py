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

    # -------- Star detection --------
    # sigma threshold passed to the active extractor.
    detect_sigma: float = 7.0

    # -------- Solver (olive-solve tetra3-py) --------
    # Path to a tetra3 .npz star database compatible with olive-solve.
    solver_db: str = "default_database"
    fov_max_error_deg: float = 1.0
    min_centroids: int = 8
    max_solve_stars: int = 50
    solve_timeout_ms: int = 1500
    match_threshold: float = 1e-5
    match_radius: float = 0.01

    # -------- Extractor backend --------
    # "sycamore" — use sycamore-extract star_detect with matched-filter gate (default)
    # "olive"    — use olive-solve get_centroids_from_image_fast (fallback)
    extract_backend: str = "sycamore"

    # Gate algorithm used when extract_backend = "sycamore".
    # "matched_filter" (default) — Gaussian matched filter, v0.8.0+ default.
    # "cedar"                    — legacy heuristic gate (pre-v0.8.0 behaviour).
    sycamore_gate_mode: str = "matched_filter"

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
    # so olive-solve's rayon thread pool can spread star extraction
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
            f"backend={self.extract_backend} db={self.solver_db} "
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
