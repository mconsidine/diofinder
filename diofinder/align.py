"""
Boresight alignment workflow.

Bridges the comms process (which receives LX200 sync commands from
SkySafari) with the solver process (which can compute pixel coordinates
for a given RA/Dec via olive-solve's solve_from_centroids).

Two primary alignment paths:

1. SkySafari sync (the typical workflow):
   * User centers a known target in the eyepiece.
   * SkySafari sends `:Sr HH:MM:SS#` (target RA), `:Sd sDD*MM:SS#`
     (target Dec), then `:CM#` (sync).
   * Comms process accumulates :Sr / :Sd, then on :CM# packages a
     request for the solver: "on the next successful solve, also
     report the pixel coordinates of (target_ra, target_dec)."
   * Solver does the next solve with `target_sky_coord`, gets back
     (x_target, y_target), publishes those into the response queue.
   * Comms reads the response, calls config.save_keys to persist
     the new boresight, and replies to SkySafari.

2. Center-of-frame sync (used via Unix socket maintenance command,
   e.g. `diofinder-align center` from ssh):
   * Sets boresight back to image center (no-op alignment, useful
     for resetting after camera maintenance).

The design keeps alignment requests well off the hot path: the solver
process checks for an alignment request opportunistically once per
solve, doesn't change behavior unless one is pending, and never blocks.
"""

import dataclasses
import logging
import time
from typing import Optional

log = logging.getLogger("diofinder.align")


@dataclasses.dataclass
class AlignRequest:
    """Posted by comms onto the alignment queue when SkySafari issues
    a :CM# after :Sr / :Sd. The solver consumes it on its next solve.
    """
    target_ra_deg: float
    target_dec_deg: float
    requested_at: float  # time.monotonic()
    # Correlation id echoed in AlignResult so comms can never accept a stale
    # result from a previous sync as the current one (audit 2026-07 W2).
    request_id: int = 0


@dataclasses.dataclass
class AlignResult:
    """Posted by solver back to comms after handling an AlignRequest.

    success=True means the next solve succeeded *and* the requested
    target_sky_coord fell within the FOV; the new boresight pixel
    is in (boresight_y, boresight_x).
    """
    success: bool
    boresight_y: Optional[float] = None
    boresight_x: Optional[float] = None
    error_message: str = ""
    completed_at: float = 0.0
    request_id: int = 0    # echoes AlignRequest.request_id


class CommsAlignState:
    """Per-connection LX200 alignment state.

    SkySafari sends :Sr (set right ascension), :Sd (set declination),
    then :CM# to commit. We accumulate the first two and act on the
    third. State resets on connection close.
    """

    # Pi Zero 2W: one solve can take exposure_s + detect + up to
    # solve_timeout_ms ≈ 0.2 + 0.1 + 1.5 = ~1.8 s.  Give the solver
    # several attempts before giving up on a sync command.
    DEFAULT_TIMEOUT_S = 15.0

    def __init__(self):
        self.target_ra_hours: Optional[float] = None
        self.target_dec_deg: Optional[float] = None

    def set_target_ra(self, ra_string: str) -> bool:
        """Parse an LX200 :Sr argument, e.g. '12:34:56' or '12:34.5'.
        Returns True on success.
        """
        try:
            self.target_ra_hours = _parse_ra_hms(ra_string)
            return True
        except Exception as e:
            log.warning("Bad RA string %r: %s", ra_string, e)
            self.target_ra_hours = None
            return False

    def set_target_dec(self, dec_string: str) -> bool:
        """Parse an LX200 :Sd argument, e.g. '+45*30:00' or '-12*15.5'."""
        try:
            self.target_dec_deg = _parse_dec_dms(dec_string)
            return True
        except Exception as e:
            log.warning("Bad Dec string %r: %s", dec_string, e)
            self.target_dec_deg = None
            return False

    def can_align(self) -> bool:
        return self.target_ra_hours is not None and self.target_dec_deg is not None

    def build_request(self) -> Optional[AlignRequest]:
        if not self.can_align():
            return None
        return AlignRequest(
            target_ra_deg=self.target_ra_hours * 15.0,
            target_dec_deg=self.target_dec_deg,
            requested_at=time.monotonic(),
        )

    def reset(self) -> None:
        self.target_ra_hours = None
        self.target_dec_deg = None


def _parse_ra_hms(s: str) -> float:
    """Parse LX200 RA format. Two LX200 dialects in the wild:
       'HH:MM:SS'   high precision
       'HH:MM.M'    low precision (decimal minutes)
    Returns hours (0..24).
    """
    s = s.strip()
    parts = s.split(":")
    if len(parts) == 3:
        h = int(parts[0]); m = int(parts[1]); sec = float(parts[2])
        return h + m / 60.0 + sec / 3600.0
    if len(parts) == 2:
        h = int(parts[0]); m = float(parts[1])
        return h + m / 60.0
    raise ValueError(f"Cannot parse RA {s!r}")


def _parse_dec_dms(s: str) -> float:
    """Parse LX200 Dec format. Both '*' and ':' may appear as the
    degree-arcmin separator.
       '+DD*MM:SS'  high precision
       '+DD*MM'     low precision
    Returns degrees in [-90, 90].
    """
    s = s.strip()
    sign = 1
    if s and s[0] in "+-":
        if s[0] == "-":
            sign = -1
        s = s[1:]
    # Normalize separators
    s = s.replace("*", ":").replace("'", ":").replace('"', "")
    parts = s.split(":")
    if len(parts) == 3:
        d = int(parts[0]); m = int(parts[1]); sec = float(parts[2])
        return sign * (d + m / 60.0 + sec / 3600.0)
    if len(parts) == 2:
        d = int(parts[0]); m = float(parts[1])
        return sign * (d + m / 60.0)
    if len(parts) == 1:
        return sign * float(parts[0])
    raise ValueError(f"Cannot parse Dec {s!r}")
