"""Identify the Messier deep-sky object the aim point is on in a solved field.

Loads the compact ``messier.csv`` catalog published by astro_databases
(columns: ``m, name, ra_deg, dec_deg, size_arcmin, mag, type``) and, given the
solved aim-point pointing, returns the Messier object the crosshair sits on —
the DSO sibling of the "Centered star" label (``star_names.py``).

Display only: this never touches detection, solving, calibration, or the aim
point. The catalog is tiny (110 rows) so the lookup is a single vectorized
dot-product over precomputed unit vectors — no scipy / KD-tree dependency, well
under a millisecond per solve.

A Messier object is "centered" when the angular separation from the aim point
is within the object's **own extent** plus a small margin (Messier sizes span
~1' for M57 to ~3deg for M31/M45, so a fixed radius would either miss a big
galaxy's disk or over-claim a tiny planetary). When several overlap (rare —
e.g. the Virgo cluster), the nearest center wins, tie-broken on brightness.
"""

from __future__ import annotations

import csv
import logging
import math

import numpy as np

log = logging.getLogger("diofinder.solver")

# Match radius per object = half its major axis + this margin, floored so that
# tiny objects still match within a finder-sensible circle. See the design doc
# (docs/messier-object-label-design.md): margin covers the fuzzy real edge, the
# floor keeps a 1' planetary matchable when the crosshair is a fraction off.
_MARGIN_DEG = 0.1
_FLOOR_DEG = 0.25


class MessierCatalog:
    def __init__(self, m, name, ra_deg, dec_deg, size_arcmin, mag, mtype):
        ra = np.radians(ra_deg)
        dec = np.radians(dec_deg)
        cos_dec = np.cos(dec)
        # Unit vectors, one row per object (N x 3).
        self._xyz = np.column_stack(
            [cos_dec * np.cos(ra), cos_dec * np.sin(ra), np.sin(dec)]
        )
        # Per-object match radius, precomputed as a cosine threshold so the
        # per-solve lookup is a single dot-product comparison.
        size_deg = np.asarray(size_arcmin, dtype=np.float64) / 60.0
        radius_deg = np.maximum(0.5 * size_deg + _MARGIN_DEG, _FLOOR_DEG)
        self._cos_radius = np.cos(np.radians(radius_deg))
        self._m = m
        self._name = name
        self._mag = np.asarray(mag, dtype=np.float64)
        self._type = mtype

    def __len__(self):
        return len(self._m)

    @classmethod
    def load(cls, path: str) -> "MessierCatalog":
        m, name, ra, dec, size, mag, mtype = [], [], [], [], [], [], []
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    ra.append(float(row["ra_deg"]))
                    dec.append(float(row["dec_deg"]))
                except (KeyError, ValueError):
                    continue
                m.append(row.get("m", ""))
                name.append(row.get("name", ""))
                # size/mag are optional per row; a blank cell means "unknown",
                # which becomes NaN and is handled by the match math below.
                size.append(_optfloat(row.get("size_arcmin")))
                mag.append(_optfloat(row.get("mag")))
                mtype.append(row.get("type", ""))
        if not m:
            raise ValueError(f"no usable rows in {path}")
        return cls(m, name, np.array(ra), np.array(dec),
                   np.array(size), np.array(mag), mtype)

    def centered(self, ra_deg: float, dec_deg: float):
        """Return the Messier object the aim point is on, or None.

        Result: ``{"m": str, "name": str, "mag": float|None, "type": str,
        "sep_deg": float}`` where sep_deg is the angular separation from the
        aim point to the object center. When several objects contain the aim
        point, the nearest center wins; ties break toward the brighter object.
        """
        ra = math.radians(ra_deg)
        dec = math.radians(dec_deg)
        cos_dec = math.cos(dec)
        b = np.array(
            [cos_dec * math.cos(ra), cos_dec * math.sin(ra), math.sin(dec)]
        )
        dots = self._xyz @ b  # cosine of angular separation, per object
        within = dots >= self._cos_radius
        if not within.any():
            return None
        idx = np.nonzero(within)[0]
        # Nearest center = largest cosine; break ties on brightness (smallest
        # mag). lexsort's last key is primary, so order (mag, -dots).
        order = np.lexsort((self._mag[idx], -dots[idx]))
        best = idx[order[0]]
        sep_deg = math.degrees(math.acos(min(1.0, max(-1.0, float(dots[best])))))
        mag = self._mag[best]
        return {
            "m": self._m[best],
            "name": self._name[best],
            "mag": None if math.isnan(mag) else round(float(mag), 2),
            "type": self._type[best],
            "sep_deg": round(sep_deg, 2),
        }


def _optfloat(s):
    """Parse an optional numeric cell; blank/malformed -> NaN."""
    try:
        return float(s)
    except (TypeError, ValueError):
        return float("nan")


def try_load(path: str):
    """Load the catalog, returning None (and logging) on any failure.

    The DSO label is a non-essential display overlay — a missing or malformed
    catalog must never stop the solver from running.
    """
    try:
        mc = MessierCatalog.load(path)
        log.info("Messier catalog loaded (%d objects) from %s", len(mc), path)
        return mc
    except FileNotFoundError:
        log.info("Messier catalog not found at %s; centered-object "
                 "naming disabled", path)
    except Exception as e:
        log.warning("Could not load Messier catalog %s: %s; naming disabled",
                    path, e)
    return None
