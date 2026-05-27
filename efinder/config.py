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
    version: str = "0.7.0"

    # -------- Camera --------
    frame_width: int = 960
    frame_height: int = 760

    exposure_s: float = 0.2
    gain: float = 20.0

    auto_exposure_enabled: bool = False
    auto_exposure_target_stars: int = 20
    auto_exposure_min_s: float = 0.05
    auto_exposure_max_s: float = 1.0

    # Optical properties
    fov_deg: float = 13.5
    arcsec_per_pixel: float = 50.8

    latitude_deg: float = 0.0
    longitude_deg: float = 0.0

    fov_calibrated: bool = False
    fov_calibrated_stddev: float = 0.05
    fov_calibrated_max_error_deg: float = 0.1

    distortion: float = 0.0

    # -------- Cedar-detect knobs --------
    cedar_detect_socket: str = "localhost:50051"
    detect_sigma: float = 9.0
    detect_hot_pixels: bool = True
    detect_use_binned: bool = True

    # -------- Solver database (.npz, used by both hybrid and olive backends) --------
    olive_db: str = "default_database"
    fov_max_error_deg: float = 1.0
    min_centroids: int = 8
    solve_timeout_ms: int = 1500
    match_threshold: float = 1e-5
    match_radius: float = 0.01

    # -------- Boresight offset --------
    boresight_y: float = 380.0
    boresight_x: float = 480.0

    # -------- Comms --------
    lx200_port: int = 4060
    lx200_client_timeout_s: float = 30.0

    # -------- CPU affinity --------
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
            f"db={self.olive_db} "
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

    # Environment overrides
    for f in dataclasses.fields(cfg):
        env_key = "EFINDER_" + f.name.upper()
        if env_key in os.environ:
            try:
                setattr(cfg, f.name, _coerce(os.environ[env_key], type(getattr(cfg, f.name))))
            except Exception as e:
                log.warning("Bad env %s=%r: %s", env_key, os.environ[env_key], e)
    return cfg


def save_keys(updates: dict, path: Optional[str] = None) -> None:
    """Write the given key/value updates back to the config file in place,
    preserving comments and unknown lines. Adds new keys at the end if
    they weren't previously present.
    """
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
