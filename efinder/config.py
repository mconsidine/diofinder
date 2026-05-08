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
    # Frame dimensions. Coupled to the camera's configured ROI; if you
    # change these you also need to change what camera_proc requests
    # from picamera2. Both must agree.
    frame_width: int = 960
    frame_height: int = 760

    # Exposure / gain. Used as the starting point; if auto_exposure
    # is on, the solver feeds back adjustments.
    exposure_s: float = 0.2
    gain: float = 20.0

    # Adaptive exposure: solver targets `auto_exposure_target_stars`
    # detected stars by adjusting exposure between the bounds.
    # Disabled by default until calibration is more developed.
    auto_exposure_enabled: bool = False
    auto_exposure_target_stars: int = 20
    auto_exposure_min_s: float = 0.05
    auto_exposure_max_s: float = 1.0

    # Optical properties
    fov_deg: float = 13.5
    arcsec_per_pixel: float = 50.8

    # Observer location. Latitude is required for polar-alignment math.
    # Negative for southern hemisphere. Set via efinder-ctl polar
    # set-latitude or persisted from SkySafari's :St command.
    latitude_deg: float = 0.0
    longitude_deg: float = 0.0

    # Calibration state. fov_deg above is the *current best* value;
    # fov_calibrated indicates whether it came from a measured median
    # (True) or a user/default starting estimate (False). When True, the
    # solver uses fov_calibrated_max_error_deg as the FOV tolerance.
    fov_calibrated: bool = False
    fov_calibrated_stddev: float = 0.05
    fov_calibrated_max_error_deg: float = 0.1

    # Distortion coefficient (cedar-solve convention: negative = barrel,
    # positive = pincushion). 0.0 means "no estimate; let cedar-solve
    # fit it per solve."
    distortion: float = 0.0

    # -------- Cedar-detect knobs --------
    cedar_detect_socket: str = "localhost:50051"
    detect_sigma: float = 8.0
    detect_hot_pixels: bool = True
    # If True, cedar-detect bins the image down 2x before star search.
    # Good for oversampled / poorly-focused images. Centroid positions
    # are still reported at full resolution.
    detect_use_binned: bool = False

    # -------- Cedar-solve knobs --------
    tetra3_db: str = "default_database"
    fov_max_error_deg: float = 1.0
    min_centroids: int = 8
    solve_timeout_ms: int = 1500
    # Maximum allowed false-positive probability for accepting a match.
    # Lower = stricter. Cedar-solve default is 1e-5.
    match_threshold: float = 1e-5
    # Maximum distance (as fraction of FOV) for matching star centroids
    # to catalog stars. Default 0.01 = 1% of FOV; ~8 px for our FOV.
    match_radius: float = 0.01

    # -------- Boresight offset --------
    # Where in the frame the telescope axis points, in pixel coordinates
    # (y, x). Defaults to image center. Updated by alignment workflow.
    # Stored in pixels rather than degrees for accuracy & to avoid
    # depending on arcsec_per_pixel uniformity across the field.
    boresight_y: float = 380.0   # frame_height / 2
    boresight_x: float = 480.0   # frame_width / 2

    # -------- Comms --------
    lx200_port: int = 4060
    lx200_client_timeout_s: float = 30.0

    # -------- CPU affinity --------
    # Pi Zero 2W has 4 cores; we leave 0 to the kernel.
    cpu_camera: int = 2
    cpu_solver: int = 3
    cpu_comms: int = 1

    # -------- Diagnostics --------
    save_failed_frames: bool = False
    failed_frames_dir: str = "/var/lib/efinder/captures"
    log_solve_stats_every_n: int = 50

    # -------- Shutdown --------
    shutdown_grace_s: float = 2.0

    def summary(self) -> str:
        return (
            f"exp={self.exposure_s}s gain={self.gain} "
            f"fov={self.fov_deg}deg sigma={self.detect_sigma} "
            f"db={self.tetra3_db} "
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

    Used by the alignment workflow to persist the boresight pixel.
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
