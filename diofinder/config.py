"""
diofinder runtime configuration.

Single source of truth for every value a user might want to tune
without editing code. Loaded from /etc/diofinder/diofinder.conf
(key:value pairs, # for comments). Per-key environment overrides
DIOFINDER_<KEY> win for ops without editing the file.

Defaults are conservative for the Pi Zero 2W + Arducam 12 MP target.

Anything here can be changed at runtime by editing the conf file and
restarting the service (`sudo systemctl restart diofinder`). No code
push required.
"""

import dataclasses
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger("diofinder.config")

DEFAULT_CONFIG_PATH = "/etc/diofinder/diofinder.conf"


@dataclasses.dataclass
class Config:
    # -------- Identity --------
    version: str = "0.11.12"

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
    gain: float = 5.0

    # Auto-exposure defaults ON: a finder usually wants the exposure tracking
    # the target star count without a manual nudge. Toggle off on the Camera
    # page if you prefer a fixed exposure.
    auto_exposure_enabled: bool = True
    # auto_exposure_target_stars and auto_exposure_max_s are live-mutable via
    # shared_cfg (the controller reads them each cycle; seeing presets write
    # them). The min floor stays config-only.
    auto_exposure_target_stars: int = 20
    auto_exposure_min_s: float = 0.05
    auto_exposure_max_s: float = 1.0
    # Matches-driven target: when the frame is solving, the controller steers
    # toward this many *matched* stars (the real currency) instead of raw
    # detected-star count. Falls back to target_stars while lost-in-space.
    # Live-mutable via shared_cfg; seeing presets write it.
    auto_exposure_target_matches: int = 8
    # Gain ladder bounds for the controller. Gain is the primary trim and is
    # raised first; exposure is only stretched once gain hits auto_exposure_max_gain.
    # max_gain is live-mutable (seeing presets write it); the floor stays config-only.
    auto_exposure_min_gain: float = 1.0
    auto_exposure_max_gain: float = 16.0
    # Exposure the controller anchors to and trims around with gain. 0 = use the
    # configured exposure_s (the last value a user/preset set). Live-mutable.
    auto_exposure_nominal_s: float = 0.0
    # Contrast floor (8-bit peak): the controller will not shed brightness
    # (lower gain / shorten exposure) once the frame peak is below this, even
    # when match-rich — it sits too near the detection cliff. The asymmetric
    # partner of the peak=250 saturation backoff. Live-mutable via shared_cfg.
    auto_exposure_peak_floor: float = 70.0

    # Optical properties
    fov_deg: float = 13.64  # 25 mm + IMX477 full-sensor mode (see CLAUDE.md)
    arcsec_per_pixel: float = 51.15  # 6.2 µm eff. pixel × 206265 / 25 mm FL

    latitude_deg: float = 0.0
    longitude_deg: float = 0.0

    fov_calibrated: bool = False
    fov_calibrated_stddev: float = 0.05
    fov_calibrated_max_error_deg: float = 0.1

    distortion: float = 0.0

    # -------- Star detection (sycamore, matched_filter gate) --------
    # sigma threshold passed to star_detect.detect_stars.
    # Matches the shipped diofinder.conf.default so the fallback (no conf file)
    # agrees with what devices actually run.
    detect_sigma: float = 5.0

    # Centroid extractor backend (live-mutable via shared_cfg / seeing preset):
    #   "sycamore" — the default matched-filter extractor (bg_cache + temporal
    #                cache + hot-pixel; all the diofinder detection knobs apply).
    #   "tetra3"   — AstroKeith's exact eFinder_cli extractor via the olive-solve
    #                tetra3 get_centroids_from_image (local_mean bg + global-RMS
    #                noise + sigma threshold, no matched filter, no temporal
    #                cache). Used by the "Legacy" seeing preset as a baseline.
    # The tetra3 backend is capability-probed at runtime; if the installed
    # olive-solve wheel lacks the extractor feature, detection falls back to
    # sycamore and logs a warning.
    extractor_backend: str = "sycamore"

    # Detection binning passed to star_detect. 2 = 2x2-binned detection:
    # ~2-3x faster extraction and lower noise; centroids remain full-res via
    # centroid_full_res. Set 1 only if faint-star recall measurably suffers
    # (validate with the Background page Capture & A/B). Restart to apply.
    detect_bin: int = 2

    # Per-frame background mode: "row_percentile" (default, cheapest),
    # "line_median" (robust to per-row offset/vignetting), or "top_hat"
    # (opt-in morphological 2-D gradient removal — needs sycamore >= 0.9.0).
    detect_bg_mode: str = "row_percentile"
    # Matched-filter kernel sigma (px) passed to star_detect (sycamore >= 0.12).
    # 1.5 ≈ a well-focused HQ Camera PSF; widen toward 2.5 for bad seeing /
    # bloated stars. Capability-probed: ignored on older wheels.
    detect_kernel_sigma: float = 1.5
    # Trail / elongation rejection (max axis ratio) passed to star_detect.
    # 0.0 disables it (treated as float("inf")); otherwise 1.5–10.0.
    detect_max_axis_ratio: float = 0.0
    # Per-window local noise estimate in the matched filter (sycamore >= 0.12).
    # Capability-probed: ignored on older wheels.
    detect_local_noise: bool = True
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
    bg_cache_slew_deg: float = 0.5     # IMU/solved-pose angle that invalidates
    bg_cache_max_age_s: float = 60.0   # rebuild if model older than this
    bg_cache_fail_invalidate: int = 3  # consecutive solve failures (no IMU) that
                                       # invalidate the cache; 0 disables

    # -------- Tracking mode (experimental, opt-in) --------
    # After a run of confident full-frame solves, switch star DETECTION from
    # full-frame extraction to small ROI windows around the previous frame's
    # solved star positions (saves the dominant ~6 ms extraction cost). The
    # recovered centroids are still solved with the ordinary solver under a
    # tight attitude hint — this is ROI detection + tight-hint solving, NOT a
    # verify-only fast path (olive-solve exposes no verify-only API; see
    # diofinder/tracking.py). Default OFF pending on-sky validation.
    # tracking_enabled is live-mutable via shared_cfg (maint solver_params_set).
    tracking_enabled: bool = False
    tracking_window_px: int = 48          # ROI side length (full-frame px)
    tracking_lock_frames: int = 3         # consecutive good solves before TRACKING
    tracking_min_recover: int = 5         # min ROI-recovered stars to stay tracking

    # -------- Seeing presets --------
    # One-tap Good/Bad night tuning (see diofinder/seeing.py). "good" is the
    # default; "bad" widens the matched filter, switches to a 2-D block
    # background, loosens trail rejection, and lengthens exposure / solve
    # budgets. Applying a preset overwrites the individual keys it controls.
    seeing_mode: str = "good"

    # -------- Solver (olive-solve tetra3-py) --------
    # Path to a tetra3 .npz star database compatible with olive-solve.
    solver_db: str = "default_database"
    # Optional deeper-magnitude database used by the "bad" seeing preset
    # (star_db="deep"). Empty = unset → presets stay on the standard db.
    # Applied only when this names a file that exists on disk.
    star_db_deep: str = ""
    # Remembered standard database. Captured automatically by seeing_set the
    # first time it switches away to the deep db, so the "standard" token never
    # resolves circularly to the (persisted, mutated) solver_db.
    star_db_standard: str = ""
    # Star-names catalog (star_names.csv from astro_databases) used to label
    # the brightest star in a solved field. Missing file → naming disabled.
    star_names_path: str = "/var/lib/diofinder/star_names.csv"
    fov_max_error_deg: float = 0.3  # tightened: lens FOV is fixed & known
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
    # IMU pointing motion gate (degrees of physical rotation since the last
    # solve). Below this the device is treated as stationary and :GR/:GD report
    # the last solved RA/Dec instead of the IMU prediction — so a parked scope
    # doesn't show gyro drift. The IMU prediction engages only for a real slew.
    # Seeded into shared_cfg at comms startup; live-tunable there.
    imu_pointing_gate_deg: float = 1.0

    # -------- CPU affinity --------
    # Pi Zero 2W: 4 cores.
    #   0 = comms_proc + diofinder-webui + IMU thread (I/O bound) + kernel/IRQs
    #   1 = solver_proc auxiliary core
    #   2 = solver_proc primary core
    #   3 = camera_proc + solver_proc secondary core
    #
    # The solver process is allowed affinity {cpu_solver, cpu_camera,
    # cpu_solver_aux} so the sycamore/olive-solve rayon pools can spread
    # work across three physical cores. Kernel+IRQ load is far below one
    # core, so sharing CPU 0 with the I/O-bound comms/webui is cheap.
    cpu_camera: int = 3
    cpu_solver: int = 2
    cpu_solver_aux: int = 1
    cpu_comms: int = 0

    # -------- Solver-hang watchdog --------
    # A daemon thread in comms_proc checks that the solver keeps publishing
    # solutions (it publishes every frame, including dark ones). If the latest
    # solution's epoch goes stale for longer than watchdog_timeout_s the
    # process is hung; comms logs CRITICAL and exits so systemd restarts it.
    watchdog_enabled: bool = True
    watchdog_timeout_s: float = 30.0

    # -------- Diagnostics --------
    save_failed_frames: bool = False
    save_solved_frames: bool = False
    failed_frames_dir: str = "/var/lib/diofinder/captures"
    log_solve_stats_every_n: int = 50

    # -------- Shutdown --------
    shutdown_grace_s: float = 2.0

    def summary(self) -> str:
        return (
            f"exp={self.exposure_s}s gain={self.gain} "
            f"fov={self.fov_deg}deg sigma={self.detect_sigma} "
            f"db={self.solver_db} "
            f"boresight=({self.boresight_y:.1f},{self.boresight_x:.1f}) "
            f"affinity[cam={self.cpu_camera},solv={self.cpu_solver}+{self.cpu_solver_aux},comm={self.cpu_comms}]"
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
    p = Path(path or os.environ.get("DIOFINDER_CONFIG", DEFAULT_CONFIG_PATH))

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
        env_key = "DIOFINDER_" + f.name.upper()
        if env_key in os.environ:
            try:
                setattr(cfg, f.name, _coerce(os.environ[env_key], type(getattr(cfg, f.name))))
            except Exception as e:
                log.warning("Bad env %s=%r: %s", env_key, os.environ[env_key], e)
    return cfg


def save_keys(updates: dict, path: Optional[str] = None) -> None:
    """Write key/value updates back to the config file in place,
    preserving comments and unknown lines."""
    p = Path(path or os.environ.get("DIOFINDER_CONFIG", DEFAULT_CONFIG_PATH))
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
