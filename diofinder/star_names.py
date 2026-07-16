"""Identify the cataloged star nearest the boresight in a solved field.

Loads the compact ``star_names.csv`` catalog published by astro_databases
(columns: ``ra_deg, dec_deg, mag, name, desig``) and, given the solved
boresight pointing, returns the nearest cataloged star — i.e. the star the
crosshair is on (or closest to).

The catalog is small (~15.6k stars to mag 7) so the lookup is a single
vectorized dot-product over precomputed unit vectors — no scipy / KD-tree
dependency, well under a millisecond per solve.
"""

from __future__ import annotations

import csv
import logging
import math

import numpy as np

log = logging.getLogger("diofinder.solver")

# Only consider stars within this fraction of the FOV (wider frame dimension)
# of the boresight, so a pointing at near-blank sky reports nothing rather
# than naming a star off in a far corner. The frame is 960x760; half its
# diagonal is ~0.64 of the 960-axis FOV, so this still spans the whole frame.
_RADIUS_FACTOR = 0.65


class StarNames:
    def __init__(self, ra_deg, dec_deg, mag, names, desigs):
        ra = np.radians(ra_deg)
        dec = np.radians(dec_deg)
        cos_dec = np.cos(dec)
        # Unit vectors, one row per star (N x 3).
        self._xyz = np.column_stack(
            [cos_dec * np.cos(ra), cos_dec * np.sin(ra), np.sin(dec)]
        )
        self._mag = np.asarray(mag, dtype=np.float64)
        self._names = names
        self._desigs = desigs

    def __len__(self):
        return len(self._names)

    @classmethod
    def load(cls, path: str) -> "StarNames":
        ra, dec, mag, names, desigs = [], [], [], [], []
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    ra.append(float(row["ra_deg"]))
                    dec.append(float(row["dec_deg"]))
                    mag.append(float(row["mag"]))
                except (KeyError, ValueError):
                    continue
                names.append(row.get("name", ""))
                desigs.append(row.get("desig", ""))
        if not names:
            raise ValueError(f"no usable rows in {path}")
        return cls(np.array(ra), np.array(dec), np.array(mag), names, desigs)

    def brightest_within(self, ra_deg: float, dec_deg: float,
                         radius_deg: float):
        """Return the BRIGHTEST cataloged star within radius_deg, or None.

        Preferred display behaviour: "the star the crosshair is on" is usually
        the naked-eye-notable one, not whichever faint catalog entry happens to
        sit a fraction of a degree closer. Magnitude decides; separation is
        reported for context.
        """
        ra = math.radians(ra_deg)
        dec = math.radians(dec_deg)
        cos_dec = math.cos(dec)
        b = np.array(
            [cos_dec * math.cos(ra), cos_dec * math.sin(ra), math.sin(dec)]
        )
        cos_radius = math.cos(math.radians(radius_deg))
        dots = self._xyz @ b
        within = dots >= cos_radius
        if not within.any():
            return None
        idx = np.nonzero(within)[0]
        best = idx[np.argmin(self._mag[idx])]   # brightest = smallest mag
        sep_deg = math.degrees(math.acos(min(1.0, max(-1.0, float(dots[best])))))
        return {
            "name": self._names[best],
            "desig": self._desigs[best],
            "mag": round(float(self._mag[best]), 2),
            "sep_deg": round(sep_deg, 2),
        }

    def brightest_in_fov(self, ra_deg: float, dec_deg: float,
                         fov_deg: float):
        """Return the BRIGHTEST cataloged star anywhere in the frame, or None.

        Same magnitude-wins selection as ``brightest_within``, but the search
        radius is the frame half-extent (``fov_deg * _RADIUS_FACTOR``, the same
        radius ``nearest`` uses) instead of a small circle around the
        boresight. This names the dominant star in the whole field of view —
        useful as a stable alignment anchor when the boresight itself is off
        (a near-blank-center pointing still reports the bright star in a
        corner, which ``brightest_within(radius=2°)`` would miss).
        """
        return self.brightest_within(ra_deg, dec_deg, fov_deg * _RADIUS_FACTOR)

    def nearest(self, ra_deg: float, dec_deg: float, fov_deg: float):
        """Return the cataloged star nearest the given pointing, or None.

        Result: ``{"name": str, "desig": str, "mag": float, "sep_deg": float}``
        where sep_deg is the angular separation from the pointing.
        """
        ra = math.radians(ra_deg)
        dec = math.radians(dec_deg)
        cos_dec = math.cos(dec)
        b = np.array(
            [cos_dec * math.cos(ra), cos_dec * math.sin(ra), math.sin(dec)]
        )
        cos_radius = math.cos(math.radians(fov_deg * _RADIUS_FACTOR))
        dots = self._xyz @ b  # cosine of angular separation
        within = dots >= cos_radius
        if not within.any():
            return None
        idx = np.nonzero(within)[0]
        # Nearest = largest cosine = smallest angle.
        best = idx[np.argmax(dots[idx])]
        sep_deg = math.degrees(math.acos(min(1.0, max(-1.0, float(dots[best])))))
        return {
            "name": self._names[best],
            "desig": self._desigs[best],
            "mag": round(float(self._mag[best]), 2),
            "sep_deg": round(sep_deg, 2),
        }


def try_load(path: str):
    """Load the catalog, returning None (and logging) on any failure.

    Naming is a non-essential display overlay — a missing or malformed
    catalog must never stop the solver from running.
    """
    try:
        sn = StarNames.load(path)
        log.info("Star-names catalog loaded (%d stars) from %s", len(sn), path)
        return sn
    except FileNotFoundError:
        log.info("Star-names catalog not found at %s; brightest-star "
                 "naming disabled", path)
    except Exception as e:
        log.warning("Could not load star-names catalog %s: %s; naming disabled",
                    path, e)
    return None
