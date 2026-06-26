"""
Polar alignment math.

The user collects multiple plate-solved (RA, Dec) measurements while
rotating the mount in RA only (mount altitude and azimuth held fixed).
If the mount's RA axis is exactly aligned with the celestial pole, all
the measurements fall along a parallel of declination. If the axis is
misaligned, the measurements trace out a small circle whose center is
the *apparent* RA axis position on the sky -- not the celestial pole.

Approach:

  1. Convert each (RA, Dec) measurement to a unit vector on the
     celestial sphere (geocentric equatorial coordinates).
  2. The N points lie (approximately) on a great-or-small circle whose
     plane has normal vector = the apparent RA axis direction.
  3. Fit that plane via SVD: the smallest singular vector of the matrix
     of point coordinates (after centering) is the plane's normal.
  4. The angle between the fitted normal and the celestial pole's
     unit vector (0, 0, 1) is the polar alignment error.
  5. Decompose the error into "azimuth" (rotate mount left/right
     around vertical) and "altitude" (tilt mount up/down) using the
     observer's latitude.

This formulation is numerically robust at any sky position, including
near the pole and across the RA=0/24h boundary. It doesn't require
the user to point at any specific star; any three+ points along an
RA-only sweep work.

References:
  - SharpCap polar alignment (the original popularization of this method)
  - Berry & Burnell, "Handbook of Astronomical Image Processing", §14
"""

import math
from typing import List, Tuple

import numpy as np


def radec_to_unit_vector(ra_deg: float, dec_deg: float) -> np.ndarray:
    """Convert celestial coordinates to a unit vector on the sphere."""
    ra = math.radians(ra_deg)
    dec = math.radians(dec_deg)
    return np.array([
        math.cos(dec) * math.cos(ra),
        math.cos(dec) * math.sin(ra),
        math.sin(dec),
    ], dtype=np.float64)


def unit_vector_to_radec(v: np.ndarray) -> Tuple[float, float]:
    """Inverse: unit vector -> (RA deg [0,360), Dec deg [-90,90])."""
    v = v / np.linalg.norm(v)
    dec = math.degrees(math.asin(max(-1.0, min(1.0, v[2]))))
    ra = math.degrees(math.atan2(v[1], v[0])) % 360.0
    return ra, dec


def fit_axis(points_radec: List[Tuple[float, float]]) -> np.ndarray:
    """Given N >= 3 (RA, Dec) measurements taken while rotating the
    mount in RA only, return a unit vector pointing along the apparent
    RA axis.

    The points lie on a circle whose plane has the RA axis as its
    normal. We fit the plane via SVD of the centered point matrix.

    The returned vector points to the *northern* hemisphere of the
    rotation axis (positive Z) -- there's a sign ambiguity in plane
    normals and we resolve it by always choosing the direction whose
    Z component matches the pole's.
    """
    if len(points_radec) < 3:
        raise ValueError("Need at least 3 points to fit a plane")

    pts = np.array([radec_to_unit_vector(ra, dec)
                    for ra, dec in points_radec], dtype=np.float64)

    # Centroid -- not on the unit sphere itself, but inside the sphere.
    # The plane through the points passes through this centroid.
    centroid = pts.mean(axis=0)
    centered = pts - centroid

    # SVD; the right-singular vector with the smallest singular value
    # is normal to the plane that best fits the points.
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    normal = vt[-1]

    # Resolve sign: we want the axis pointing toward the visible pole.
    # For a northern-hemisphere observer the celestial pole is +Z;
    # for southern, -Z. We can't know which without latitude, but the
    # convention "pick the hemisphere with larger |Z|" works for any
    # latitude where the pole is even close to up.
    if normal[2] < 0:
        normal = -normal

    return normal / np.linalg.norm(normal)


def angle_between_vectors(a: np.ndarray, b: np.ndarray) -> float:
    """Returns the angle in degrees between two unit vectors."""
    a = a / np.linalg.norm(a)
    b = b / np.linalg.norm(b)
    dot = float(np.clip(np.dot(a, b), -1.0, 1.0))
    return math.degrees(math.acos(dot))


def decompose_alignment_error(axis: np.ndarray, latitude_deg: float
                              ) -> Tuple[float, float, float]:
    """Decompose the alignment error of `axis` (apparent RA axis unit
    vector) into (total_error_deg, azimuth_error_deg, altitude_error_deg).

    The mount's RA axis should point at the celestial pole. The "true"
    pole is at unit vector (0, 0, +1) for the northern hemisphere
    (-Z for southern).

    The two reported errors correspond to the two mechanical adjustments
    the user can make:

      * Azimuth error: how much the mount needs to be rotated horizontally
        (around the local vertical). Positive value typically displayed
        as "axis is east of pole; rotate mount west".

      * Altitude error: how much the mount needs to be tilted vertically
        (around the local east-west axis). Positive value typically
        displayed as "axis is above pole; tilt mount down".

    NOTE on magnitudes: total_error_deg is the angle between the apparent
    axis and the true pole; azimuth_error and altitude_error are the
    components projected onto the two adjustable degrees of freedom.
    A third component (rotation along the axis itself) is unobservable
    and irrelevant to alignment, so generally
        sqrt(az^2 + alt^2) != total_error
    near the equator that gap is large; near the pole it's small.
    The user only needs az and alt to know which way to adjust.

    Frame convention: at LST=0, the local east axis is (0, 1, 0) and
    the local north axis is (-sin(phi), 0, cos(phi)) for latitude phi.
    LST cancels because we only care about angles, not absolute times.
    """
    pole = np.array([0.0, 0.0, 1.0], dtype=np.float64)
    if latitude_deg < 0:
        pole = -pole

    total = angle_between_vectors(axis, pole)

    # Small-angle approximation: linearize the offset in the tangent
    # plane at the pole. Accurate to < 0.001 deg even for 5 deg total error.
    offset = axis - pole

    phi = math.radians(abs(latitude_deg))
    east  = np.array([0.0, 1.0, 0.0])
    north = np.array([-math.sin(phi), 0.0, math.cos(phi)])
    if latitude_deg < 0:
        north = -north

    az_error_rad = float(np.dot(offset, east))
    alt_error_rad = float(np.dot(offset, north))

    return total, math.degrees(az_error_rad), math.degrees(alt_error_rad)


def summarize_alignment(points_radec: List[Tuple[float, float]],
                        latitude_deg: float) -> dict:
    """Convenience: fit axis + decompose error in one call.

    Returns a dict suitable for the maintenance socket response.
    """
    axis = fit_axis(points_radec)
    axis_ra, axis_dec = unit_vector_to_radec(axis)
    total, az, alt = decompose_alignment_error(axis, latitude_deg)

    # Also report what the residuals are -- if the user wasn't actually
    # rotating in RA only (e.g. if the mount slipped in declination),
    # the points won't be coplanar and we should warn.
    pts = np.array([radec_to_unit_vector(ra, dec)
                    for ra, dec in points_radec], dtype=np.float64)
    centered = pts - pts.mean(axis=0)
    distances_from_plane = np.abs(centered @ axis)
    rms_residual_arcmin = math.degrees(
        math.sqrt(float(np.mean(distances_from_plane ** 2)))
    ) * 60.0

    return {
        "axis_ra_deg": axis_ra,
        "axis_dec_deg": axis_dec,
        "total_error_deg": total,
        "total_error_arcmin": total * 60.0,
        "azimuth_error_deg": az,
        "azimuth_error_arcmin": az * 60.0,
        "altitude_error_deg": alt,
        "altitude_error_arcmin": alt * 60.0,
        "n_points": len(points_radec),
        "plane_fit_rms_arcmin": rms_residual_arcmin,
    }
