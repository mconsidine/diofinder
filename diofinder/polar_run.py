"""
Polar alignment runtime state machine.

Lives in the solver process. The solver calls update_from_solve() on
every successful solve, which advances the state machine through:

  IDLE -> CAPTURING -> DONE

In CAPTURING, the state machine alternates between waiting for the user
to slew (RA changes substantially) and waiting for them to dwell (RA
and Dec stable across several consecutive solves). Each detected dwell
records a point. After N points (default 3, configurable), the math
runs and we transition to DONE with a result dict.

Maintenance protocol (via comms_proc):
  polar_start      : reset state, transition to CAPTURING with first
                     dwell coming up.
  polar_status     : return current state, points so far, last result
                     if any.
  polar_cancel     : return to IDLE, discard any in-progress capture.
  polar_set_latitude : update the observer latitude used in error
                     decomposition.
"""

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

from diofinder import polar as polar_math

log = logging.getLogger("diofinder.polar_run")


class PolarState(Enum):
    IDLE = "idle"
    WAITING_FOR_DWELL = "waiting_for_dwell"
    WAITING_FOR_SLEW = "waiting_for_slew"
    DONE = "done"
    ERROR = "error"


@dataclass
class PolarParams:
    # Number of points to capture before computing
    target_points: int = 3
    # Position change (deg) over the last few solves below which we
    # consider the mount stationary
    dwell_threshold_deg: float = 0.05
    # How many consecutive sub-threshold solves count as a dwell
    dwell_consecutive: int = 3
    # Position change (deg) above which we consider the mount has slewed
    # to a new position. Setting this larger than dwell_threshold makes
    # detection robust to small jitter.
    slew_threshold_deg: float = 5.0
    # Total wall-clock timeout for the whole alignment session (seconds);
    # if exceeded we go to ERROR state.
    timeout_s: float = 180.0


@dataclass
class PolarRunStatus:
    state: str
    points_captured: int
    target_points: int
    last_result: Optional[dict] = None
    error_message: str = ""
    elapsed_s: float = 0.0
    needed_action: str = ""


class PolarAligner:
    def __init__(self, latitude_deg: Optional[float] = None,
                 params: Optional[PolarParams] = None):
        self.params = params or PolarParams()
        self.latitude_deg = latitude_deg
        self.state = PolarState.IDLE
        self._points: List[Tuple[float, float]] = []
        self._recent: deque = deque(maxlen=self.params.dwell_consecutive)
        self._last_captured_pos: Optional[Tuple[float, float]] = None
        self._started_at: float = 0.0
        self._error_message: str = ""
        self._last_result: Optional[dict] = None

    # ---- External API used by maintenance handlers ----

    def start(self) -> None:
        """Begin a new alignment session. Discards any prior progress.

        Latitude is NOT required to start. We can capture points fine
        without it; only the final decomposition into az/alt errors
        needs latitude. If latitude is still unset when we reach the
        final point, we report the error as a 3D sky-vector and tell
        the user to set latitude (typically by connecting SkySafari).
        """
        self._points = []
        self._recent.clear()
        self._last_captured_pos = None
        self._started_at = time.monotonic()
        self._error_message = ""
        self._last_result = None
        self.state = PolarState.WAITING_FOR_DWELL
        if self.latitude_deg is None:
            log.info("Polar alignment started; latitude not yet set "
                     "(connect SkySafari or run polar set-latitude before "
                     "completing); need %d points",
                     self.params.target_points)
        else:
            log.info("Polar alignment started; latitude=%.2f, need %d points",
                     self.latitude_deg, self.params.target_points)

    def cancel(self) -> None:
        log.info("Polar alignment cancelled (was %s, %d pts)",
                 self.state.value, len(self._points))
        self.state = PolarState.IDLE
        self._points = []
        self._recent.clear()
        self._last_captured_pos = None
        self._error_message = ""

    def set_latitude(self, lat_deg: float) -> None:
        self.latitude_deg = float(lat_deg)
        log.info("Polar latitude set to %.4f", self.latitude_deg)
        # If we already finished a capture without latitude, retroactively
        # decompose now that we have it.
        if (self.state == PolarState.DONE
                and self._last_result
                and self._last_result.get("needs_latitude")):
            log.info("Retroactively decomposing prior alignment with new latitude")
            try:
                self._last_result = polar_math.summarize_alignment(
                    self._points, self.latitude_deg)
                r = self._last_result
                log.info("Retro decompose: total=%.2f arcmin "
                         "(az=%.2f, alt=%.2f)",
                         r["total_error_arcmin"],
                         r["azimuth_error_arcmin"],
                         r["altitude_error_arcmin"])
            except Exception as e:
                log.exception("Retroactive decomposition failed")
                self._error_message = f"retro decompose failed: {e}"

    def get_status(self) -> dict:
        elapsed = (time.monotonic() - self._started_at
                   if self._started_at > 0 else 0.0)
        needed = ""
        if self.state == PolarState.WAITING_FOR_DWELL:
            needed = (f"hold mount stationary "
                      f"(point {len(self._points) + 1} of {self.params.target_points})")
        elif self.state == PolarState.WAITING_FOR_SLEW:
            needed = (f"rotate mount in RA "
                      f">~{self.params.slew_threshold_deg:.0f} deg, then hold "
                      f"(captured {len(self._points)} of {self.params.target_points})")
        elif self.state == PolarState.DONE:
            needed = "alignment complete"
        return {
            "state": self.state.value,
            "points_captured": len(self._points),
            "target_points": self.params.target_points,
            "latitude_deg": self.latitude_deg,
            "needed_action": needed,
            "elapsed_s": elapsed,
            "error_message": self._error_message,
            "last_result": self._last_result,
        }

    # ---- Internal state advancement ----

    def update_from_solve(self, ra_deg: float, dec_deg: float) -> None:
        """Solver calls this after every successful solve. Drives the
        state machine forward.
        """
        if self.state in (PolarState.IDLE, PolarState.DONE, PolarState.ERROR):
            return

        # Global timeout check
        if time.monotonic() - self._started_at > self.params.timeout_s:
            log.warning("Polar alignment timed out after %.0fs",
                        time.monotonic() - self._started_at)
            self.state = PolarState.ERROR
            self._error_message = "timeout"
            return

        # Maintain a small ring buffer of recent positions for dwell detection
        self._recent.append((ra_deg, dec_deg))

        if self.state == PolarState.WAITING_FOR_DWELL:
            if self._is_dwelling():
                self._capture_point(ra_deg, dec_deg)
            return

        if self.state == PolarState.WAITING_FOR_SLEW:
            if self._has_slewed(ra_deg, dec_deg):
                log.debug("Slew detected, now waiting for next dwell")
                self.state = PolarState.WAITING_FOR_DWELL
                self._recent.clear()
                self._recent.append((ra_deg, dec_deg))
            return

    def _is_dwelling(self) -> bool:
        if len(self._recent) < self.params.dwell_consecutive:
            return False
        # Spread = max angular separation between recent points
        max_sep = 0.0
        pts = list(self._recent)
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                sep = _angular_separation_deg(*pts[i], *pts[j])
                if sep > max_sep:
                    max_sep = sep
        return max_sep < self.params.dwell_threshold_deg

    def _has_slewed(self, ra: float, dec: float) -> bool:
        if self._last_captured_pos is None:
            return True  # shouldn't happen, but be safe
        sep = _angular_separation_deg(
            self._last_captured_pos[0], self._last_captured_pos[1],
            ra, dec,
        )
        return sep > self.params.slew_threshold_deg

    def _capture_point(self, ra: float, dec: float) -> None:
        self._points.append((ra, dec))
        self._last_captured_pos = (ra, dec)
        log.info("Polar: captured point %d/%d at RA=%.4f Dec=%.4f",
                 len(self._points), self.params.target_points, ra, dec)

        if len(self._points) >= self.params.target_points:
            self._compute_result()
        else:
            self.state = PolarState.WAITING_FOR_SLEW
            self._recent.clear()

    def _compute_result(self) -> None:
        try:
            if self.latitude_deg is not None:
                self._last_result = polar_math.summarize_alignment(
                    self._points, self.latitude_deg)
                self.state = PolarState.DONE
                r = self._last_result
                log.info("Polar alignment done: total=%.2f arcmin "
                         "(az=%.2f, alt=%.2f) plane_rms=%.2f arcmin",
                         r["total_error_arcmin"],
                         r["azimuth_error_arcmin"],
                         r["altitude_error_arcmin"],
                         r["plane_fit_rms_arcmin"])
            else:
                # Capture is complete but we can't decompose without
                # latitude. Fit the axis and report what we have.
                axis = polar_math.fit_axis(self._points)
                axis_ra, axis_dec = polar_math.unit_vector_to_radec(axis)
                pole = [0.0, 0.0, 1.0]
                import numpy as np
                total = polar_math.angle_between_vectors(axis, np.array(pole))
                self._last_result = {
                    "axis_ra_deg": axis_ra,
                    "axis_dec_deg": axis_dec,
                    "total_error_deg": total,
                    "total_error_arcmin": total * 60.0,
                    "azimuth_error_deg": None,
                    "azimuth_error_arcmin": None,
                    "altitude_error_deg": None,
                    "altitude_error_arcmin": None,
                    "n_points": len(self._points),
                    "needs_latitude": True,
                    "message": ("axis fitted; set latitude to decompose "
                                "into azimuth/altitude (connect SkySafari "
                                "or run polar set-latitude)"),
                }
                self.state = PolarState.DONE
                log.info("Polar axis fitted (total=%.2f arcmin) but "
                         "latitude unset; cannot decompose to az/alt",
                         total * 60.0)
        except Exception as e:
            log.exception("Polar math failed")
            self.state = PolarState.ERROR
            self._error_message = f"math error: {e}"


def _angular_separation_deg(ra1, dec1, ra2, dec2):
    """Great-circle angular separation between two (RA, Dec) in degrees."""
    r1 = math.radians(ra1); d1 = math.radians(dec1)
    r2 = math.radians(ra2); d2 = math.radians(dec2)
    # Vincenty great-circle (numerically robust at small separations)
    sdr = math.sin((r1 - r2) / 2)
    sdd = math.sin((d1 - d2) / 2)
    a = sdd * sdd + math.cos(d1) * math.cos(d2) * sdr * sdr
    a = max(0.0, min(1.0, a))
    return math.degrees(2 * math.asin(math.sqrt(a)))
