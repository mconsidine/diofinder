"""
FOV and distortion calibration.

Cedar-solve returns the horizontal field of view (degrees) and distortion
coefficient on every successful solve. These are intrinsic camera/lens
properties -- they shouldn't change unless the user re-focuses or swaps
optics. So we treat them as values to *measure once and persist*, not
configure manually.

The calibration state machine:

  * UNCALIBRATED: the value in the config file is a starting estimate
    (default 13.5 deg from a coarse measurement). Wide FOV tolerance
    is used for solving (cfg.fov_max_error_deg), since we don't know
    the true value yet.

  * CALIBRATING: we've accumulated < N successful solves. Continue
    measuring; don't commit yet.

  * CALIBRATED: N solves accumulated, the spread (stddev) is below
    threshold, and we've written the median to the config. Subsequent
    solves use a TIGHT tolerance (cfg.fov_calibrated_max_error_deg).
    We keep accumulating to detect drift -- if the rolling median
    drifts > 3*stddev from the committed value, we revert to
    CALIBRATING and recommit when stable.

The same mechanism handles distortion. Distortion's measurement noise
is higher (it's a small correction term) so we use a longer window
and looser convergence criteria.

This class is owned by the solver process. The solver calls
update_from_solve() after every successful solve. When a commit
happens, the calibrator writes to the config file via
config.save_keys() AND updates the shared_cfg dict so live solves
pick up the new tolerance immediately.
"""

import logging
import statistics
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from efinder import config as cfg_mod

log = logging.getLogger("efinder.calibration")


class CalState(Enum):
    UNCALIBRATED = "uncalibrated"
    CALIBRATING = "calibrating"
    CALIBRATED = "calibrated"


@dataclass
class CalibrationParams:
    # Number of successful solves to accumulate before committing
    window_size: int = 30
    # Maximum stddev (degrees for FOV) for the window to be "converged"
    fov_convergence_stddev: float = 0.05
    # Drift threshold (in stddevs of the committed window) that
    # triggers re-calibration
    fov_drift_sigmas: float = 3.0
    # Distortion convergence threshold
    distortion_convergence_stddev: float = 0.005
    # How many solves between recalibration checks once CALIBRATED
    drift_check_interval: int = 50


class FovCalibrator:
    """Tracks the rolling FOV measurement, decides when to commit."""

    def __init__(self, cfg, shared_cfg, params: Optional[CalibrationParams] = None):
        self.cfg = cfg
        self.shared_cfg = shared_cfg
        self.params = params or CalibrationParams()

        # Restore prior calibration from cfg if present
        self.state = (CalState.CALIBRATED
                      if getattr(cfg, "fov_calibrated", False)
                      else CalState.UNCALIBRATED)
        self.committed_fov = cfg.fov_deg if self.state == CalState.CALIBRATED else None
        self.committed_fov_stddev = (getattr(cfg, "fov_calibrated_stddev", 0.05)
                                     if self.state == CalState.CALIBRATED else None)
        self.committed_distortion = (getattr(cfg, "distortion", 0.0)
                                     if self.state == CalState.CALIBRATED else 0.0)

        # Rolling windows
        self._fov_window = deque(maxlen=self.params.window_size)
        self._distortion_window = deque(maxlen=self.params.window_size)
        self._solves_since_check = 0
        self._last_log_state = None

    @property
    def use_tight_tolerance(self) -> bool:
        """Solver should use a tight fov_max_error when this is True."""
        return self.state == CalState.CALIBRATED

    def get_fov_estimate(self) -> float:
        """Best current FOV estimate to feed to olive-solve."""
        if self.committed_fov is not None:
            return self.committed_fov
        return self.cfg.fov_deg

    def get_fov_max_error(self) -> float:
        """Tolerance to feed to olive-solve. Tight after calibration.

        The loose (uncalibrated / blind) tolerance honours a live
        shared_cfg["fov_max_error_deg"] override so it can be A/B-tuned over
        the maint socket without a restart; absent an override it falls back to
        the config value. The tight (calibrated) tolerance is left to the
        calibration machinery.
        """
        if self.use_tight_tolerance:
            return getattr(self.cfg, "fov_calibrated_max_error_deg", 0.1)
        override = self.shared_cfg.get("fov_max_error_deg")
        if override is not None:
            return float(override)
        return self.cfg.fov_max_error_deg

    def get_distortion_estimate(self) -> float:
        """Distortion coefficient to feed to olive-solve. 0.0 means
        caller has no estimate; olive-solve fits distortion itself.
        """
        return self.committed_distortion

    def update_from_solve(self, fov_deg: float, distortion: float) -> None:
        """Solver calls this on every successful solve."""
        self._fov_window.append(fov_deg)
        self._distortion_window.append(distortion)

        if self.state == CalState.UNCALIBRATED:
            self.state = CalState.CALIBRATING
            self._log_state_change()

        if len(self._fov_window) < self.params.window_size:
            return

        # Window is full; check convergence
        if self.state == CalState.CALIBRATING:
            self._maybe_commit()
        elif self.state == CalState.CALIBRATED:
            self._solves_since_check += 1
            if self._solves_since_check >= self.params.drift_check_interval:
                self._solves_since_check = 0
                self._check_for_drift()

    def _maybe_commit(self) -> None:
        fov_med = statistics.median(self._fov_window)
        fov_std = statistics.stdev(self._fov_window) if len(self._fov_window) > 1 else 0.0
        dist_med = statistics.median(self._distortion_window)
        dist_std = statistics.stdev(self._distortion_window) if len(self._distortion_window) > 1 else 0.0

        if fov_std > self.params.fov_convergence_stddev:
            log.debug("FOV not converged: median=%.4f stddev=%.4f (need < %.4f)",
                      fov_med, fov_std, self.params.fov_convergence_stddev)
            return

        # Distortion converging is nice-to-have, not blocking
        distortion_converged = dist_std <= self.params.distortion_convergence_stddev

        log.info("FOV calibration converged: %.4f deg +/- %.4f (n=%d)%s",
                 fov_med, fov_std, len(self._fov_window),
                 "" if distortion_converged
                 else f"; distortion not yet converged (stddev=%.4f)" % dist_std)

        # Compute derived plate scale
        arcsec_per_pixel = fov_med * 3600.0 / self.cfg.frame_width

        updates = {
            "fov_deg": fov_med,
            "fov_calibrated": True,
            "fov_calibrated_stddev": fov_std,
            "arcsec_per_pixel": arcsec_per_pixel,
        }
        if distortion_converged:
            updates["distortion"] = dist_med

        try:
            cfg_mod.save_keys(updates)
        except Exception as e:
            log.warning("Could not persist FOV calibration: %s", e)
            # Don't update in-memory state if persist failed -- we'll
            # try again on the next window
            return

        self.committed_fov = fov_med
        self.committed_fov_stddev = fov_std
        if distortion_converged:
            self.committed_distortion = dist_med

        # Update live cfg + shared_cfg so the solver picks up tight
        # tolerance and the new FOV estimate immediately
        self.cfg.fov_deg = fov_med
        self.cfg.arcsec_per_pixel = arcsec_per_pixel
        if distortion_converged:
            self.cfg.distortion = dist_med
        self.shared_cfg["fov_deg"] = fov_med
        self.shared_cfg["arcsec_per_pixel"] = arcsec_per_pixel
        if distortion_converged:
            self.shared_cfg["distortion"] = dist_med

        self.state = CalState.CALIBRATED
        self._log_state_change()

    def _check_for_drift(self) -> None:
        """Rolling median moved too far from committed value -> recalibrate."""
        if self.committed_fov is None or self.committed_fov_stddev is None:
            return
        recent_med = statistics.median(self._fov_window)
        # Drift in units of the committed window's stddev. Use the larger
        # of stored stddev or convergence threshold to avoid hyper-sensitive
        # triggering on a very tight original calibration.
        scale = max(self.committed_fov_stddev,
                    self.params.fov_convergence_stddev)
        drift = abs(recent_med - self.committed_fov) / scale
        if drift > self.params.fov_drift_sigmas:
            log.warning("FOV drift detected: was %.4f, now %.4f (%.1f sigma); "
                        "recalibrating", self.committed_fov, recent_med, drift)
            self.state = CalState.CALIBRATING
            self._log_state_change()
            # Don't clear committed_fov yet; keep using it until the next
            # commit lands so we don't briefly lose tight tolerance during
            # the recalibration window.

    def _log_state_change(self) -> None:
        if self.state != self._last_log_state:
            log.info("Calibration state -> %s (committed_fov=%s)",
                     self.state.value, self.committed_fov)
            self._last_log_state = self.state

    # ---- Maintenance helpers ----

    def get_status(self) -> dict:
        """Snapshot suitable for the maintenance protocol."""
        import statistics as st
        recent_med = (st.median(self._fov_window)
                      if len(self._fov_window) > 0 else None)
        recent_std = (st.stdev(self._fov_window)
                      if len(self._fov_window) > 1 else None)
        return {
            "state": self.state.value,
            "window_size": self.params.window_size,
            "window_filled": len(self._fov_window),
            "convergence_threshold": self.params.fov_convergence_stddev,
            "recent_median": recent_med,
            "recent_stddev": recent_std,
            "committed_fov": self.committed_fov,
            "committed_fov_stddev": self.committed_fov_stddev,
            "committed_distortion": self.committed_distortion,
            "use_tight_tolerance": self.use_tight_tolerance,
            "current_max_error": self.get_fov_max_error(),
        }

    def force_recalibrate(self) -> None:
        """Discard committed state, clear the window, force re-measurement.

        Used when the user knows they've changed something (lens swap,
        major refocus) and wants the calibrator to start over rather
        than wait for drift detection.
        """
        log.info("Force recalibrate: clearing window and committed state")
        self._fov_window.clear()
        self._distortion_window.clear()
        self.committed_fov = None
        self.committed_fov_stddev = None
        self.committed_distortion = 0.0
        self.state = CalState.UNCALIBRATED
        self._solves_since_check = 0
        # Also clear the persisted flag so a restart doesn't restore the
        # old calibration. The fov_deg value itself is left as the
        # current best estimate -- the user may have hand-tuned it after
        # the lens swap to give the solver a starting point.
        try:
            cfg_mod.save_keys({
                "fov_calibrated": False,
                "fov_calibrated_stddev": self.params.fov_convergence_stddev,
            })
        except Exception as e:
            log.warning("Could not persist calibration reset: %s", e)
        self._log_state_change()
