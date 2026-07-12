"""
Internal command types between comms and worker processes.

Different from maint.py: maint.py is the JSON wire protocol on the
maintenance Unix socket. This module defines the in-process queue
messages that comms uses to ask camera_proc and solver_proc to do
things on its behalf.

Why a separate layer: a maintenance command might need to touch
multiple workers, or might be answerable by comms alone, or might
trigger a sequence of actions. The maintenance handler is the right
place to know that; the workers shouldn't.
"""

import dataclasses
import time
from typing import Any, Optional


# ----- Solver commands -----

@dataclasses.dataclass
class SolverCmd:
    """Generic command sent from comms to the solver."""
    op: str
    args: dict
    request_id: int  # for correlating responses
    requested_at: float = 0.0

    def __post_init__(self):
        if self.requested_at == 0.0:
            self.requested_at = time.monotonic()


@dataclasses.dataclass
class SolverCmdReply:
    request_id: int
    ok: bool
    result: Any = None
    error: str = ""
    completed_at: float = 0.0

    def __post_init__(self):
        if self.completed_at == 0.0:
            self.completed_at = time.monotonic()


# Op names. Kept as constants so typos are caught at lookup time
# rather than silently treated as unknown commands.
SOLVER_OP_CALIBRATION_STATUS = "calibration_status"
SOLVER_OP_CALIBRATION_RESET = "calibration_reset"
SOLVER_OP_POLAR_START = "polar_start"
SOLVER_OP_POLAR_STATUS = "polar_status"
SOLVER_OP_POLAR_CANCEL = "polar_cancel"
SOLVER_OP_POLAR_SET_LATITUDE = "polar_set_latitude"
# Solve a caller-supplied centroid list with the solver's already-loaded
# database (used by the diag_background --solve A/B, so it never loads a second
# copy of the star DB). args: {"centroids": [[row, col], ...]}.
SOLVER_OP_SOLVE_CENTROIDS = "solve_centroids"
# Live snapshot of the temporal background cache (state, model age, counters).
SOLVER_OP_BG_CACHE_STATUS = "bg_cache_status"
# Switch the resident plate-solver database. args: {"db": "<name-or-path>"}.
# Reloads tetra3.Tetra3 in-place; no second copy held.
SOLVER_OP_SET_DB = "set_db"
# Capture N frames from SHM, median-stack, build a hot-pixel mask, save + load
# it. args: {"frames": int}.
SOLVER_OP_DARK_CAPTURE = "dark_capture"
# Single-frame extract+solve probe for the offline auto-tune sweep. Grabs the
# current SHM frame, extracts with caller-supplied detection params (forced
# per-frame so the live temporal cache is untouched), solves on the resident
# database, and returns one sample {solved, matches, stars, peak, solve_ms}.
# args: {sigma, kernel_sigma, bg_mode, max_axis_ratio, bg_block_size,
#        uniform_filter_size, noise_mode, local_noise, solve_timeout_ms}.
SOLVER_OP_AUTO_TUNE_EVAL = "auto_tune_eval"
# Hot-pixel mask status: {"count", "mtime", "loaded"}.
SOLVER_OP_HOT_PIXEL_STATUS = "hot_pixel_status"
# Tracking-mode status: {"enabled", "state", "frames_tracked", "frames_full",
# "recover_fail"}.
SOLVER_OP_TRACKING_STATUS = "tracking_status"
# Return the rolling per-successful-solve records (FULL vs TRACKING) for the
# tracking A/B harness. args: {"after": epoch_monotonic} to fetch only newer
# records. Result: {"records": [[epoch, tracked, solve_ms, extract_ms,
# matches, ra, dec], ...], "now": monotonic}.
SOLVER_OP_SOLVE_STATS = "solve_stats"
# Clear the hot-pixel mask (delete file + unload). args: {}.
SOLVER_OP_HOT_PIXEL_CLEAR = "hot_pixel_clear"
# Return the newest published camera frame (bytes + shape + seq), read via
# the FrameSlots protocol so it can never be torn by a concurrent camera
# write. Serves webui frame displays, debug bundles, and A/B captures.
SOLVER_OP_FRAME_GET = "frame_get"
# Reconstruct the background a bg mode subtracts, for the webui Background
# page's visual A/B. Returns the paired frame + full-res background bytes for
# the SAME seq (so the subtracted view is consistent), plus preview metadata.
# Uniquely, temporal_median renders the solver's live cached median stack,
# which cannot be seen from any other process. args: {mode?, tophat_radius?,
# bg_block_size?, uniform_filter_size?, noise_mode?}.
SOLVER_OP_BG_PREVIEW = "bg_preview"


# ----- Camera commands -----

@dataclasses.dataclass
class CameraCmd:
    op: str
    args: dict
    request_id: int
    requested_at: float = 0.0

    def __post_init__(self):
        if self.requested_at == 0.0:
            self.requested_at = time.monotonic()


@dataclasses.dataclass
class CameraCmdReply:
    request_id: int
    ok: bool
    result: Any = None
    error: str = ""
    completed_at: float = 0.0

    def __post_init__(self):
        if self.completed_at == 0.0:
            self.completed_at = time.monotonic()


CAMERA_OP_GET_EXPOSURE = "get_exposure"
CAMERA_OP_SET_EXPOSURE = "set_exposure"
CAMERA_OP_SET_GAIN = "set_gain"
