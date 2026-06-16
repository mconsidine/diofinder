"""Brightest-star naming for the solved field.

Loads the compact ``star_names.csv`` catalog published by astro_databases
(columns: ``ra_deg, dec_deg, mag, name, desig``) and, given a solved
pointing, returns the brightest cataloged star within the field of view.

The catalog is small (~15.6k stars to mag 7) so the lookup is a single
vectorized dot-product over precomputed unit vectors — no scipy / KD-tree
dependency, well under a millisecond per solve.
"""

from __future__ import annotations

import csv
import logging
import math

import numpy as np

log = logging.getLogger("efinder.solver")

# Search radius as a fraction of the published FOV (the wider frame dimension).
# The frame is 960x760; half its diagonal is ~0.64 of the 960-axis FOV, so a
# star anywhere in the frame falls within this cone around the center.
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

    def brightest(self, ra_deg: float, dec_deg: float, fov_deg: float):
        """Return the brightest star within the field, or None.

        Result: ``{"name": str, "desig": str, "mag": float}``.
        """
        ra = math.radians(ra_deg)
        dec = math.radians(dec_deg)
        cos_dec = math.cos(dec)
        b = np.array(
            [cos_dec * math.cos(ra), cos_dec * math.sin(ra), math.sin(dec)]
        )
        cos_radius = math.cos(math.radians(fov_deg * _RADIUS_FACTOR))
        dots = self._xyz @ b
        within = dots >= cos_radius
        if not within.any():
            return None
        idx = np.nonzero(within)[0]
        best = idx[np.argmin(self._mag[idx])]
        return {
            "name": self._names[best],
            "desig": self._desigs[best],
            "mag": round(float(self._mag[best]), 2),
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
