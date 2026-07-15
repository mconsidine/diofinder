"""Precession between the J2000 mean equator/equinox and the equinox of date.

diofinder solves in the catalog frame — Gaia DR3 + Hipparcos, i.e. **ICRS
(≈ J2000)** — and applies no precession internally (see docs/onstep-design.md).
But SkySafari's LX200 telescope link and OnStepX both work in **JNow** (the
equinox of date), so coordinates must be converted at the comms I/O boundary:

  * outbound  (:GR/:GD reporting, mount sync):  J2000 -> JNow
  * inbound   (:CM# / :Sr / :Sd align target):  JNow  -> J2000

The internal pipeline stays single-epoch (J2000); only this boundary converts.

Implementation: rigorous IAU 1976 precession (Meeus, *Astronomical
Algorithms*, ch. 21), pure ``math`` — no numpy, no astropy. Sub-arcsecond over
the J2000..2026 span, which is far below a finder's needs. Nutation (~9") and
aberration (~20") are deliberately omitted: mean-place precession is what
SkySafari/OnStepX expect for the equinox of date, and the residual is
negligible on a 13.6-degree finder.

Cost is a handful of trig ops, run once per report/sync (not per frame).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

_ARCSEC = math.pi / (180.0 * 3600.0)   # arcseconds -> radians
_J2000_JD = 2451545.0
_DAYS_PER_CENTURY = 36525.0


def julian_centuries_now() -> float:
    """Julian centuries (TT≈UTC precision is ample here) from J2000.0 to now."""
    now = datetime.now(timezone.utc)
    # JD of the current instant. total_seconds() from the J2000 epoch instant
    # (2000-01-01 12:00 UTC) in days, divided into Julian centuries.
    days = (now - datetime(2000, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
            ).total_seconds() / 86400.0
    return days / _DAYS_PER_CENTURY


def _precession_angles(t: float):
    """IAU 1976 precession angles zeta_A, z_A, theta_A (radians) from J2000 to
    the epoch ``t`` Julian centuries after J2000."""
    zeta = (2306.2181 * t + 0.30188 * t * t + 0.017998 * t ** 3) * _ARCSEC
    z = (2306.2181 * t + 1.09468 * t * t + 0.018203 * t ** 3) * _ARCSEC
    theta = (2004.3109 * t - 0.42665 * t * t - 0.041833 * t ** 3) * _ARCSEC
    return zeta, z, theta


def precess(ra_deg: float, dec_deg: float, t: float,
            inverse: bool = False) -> tuple:
    """Precess (ra_deg, dec_deg) by ``t`` Julian centuries from J2000.

    ``inverse=False``: J2000 -> epoch-of-date (t).
    ``inverse=True``:  epoch-of-date (t) -> J2000.

    ``t`` is Julian centuries since J2000 (e.g. ~0.265 for mid-2026). Pure on
    its inputs so the boundary is testable without a clock.
    """
    zeta, z, theta = _precession_angles(t)
    a = math.radians(ra_deg)
    d = math.radians(dec_deg)
    sin_theta, cos_theta = math.sin(theta), math.cos(theta)

    if not inverse:
        # J2000 -> date (Meeus 21.4).
        a_zeta = a + zeta
        A = math.cos(d) * math.sin(a_zeta)
        B = cos_theta * math.cos(d) * math.cos(a_zeta) - sin_theta * math.sin(d)
        C = sin_theta * math.cos(d) * math.cos(a_zeta) + cos_theta * math.sin(d)
        out_a = math.atan2(A, B) + z
    else:
        # date -> J2000 (inverse rotation).
        a_z = a - z
        A = math.cos(d) * math.sin(a_z)
        B = cos_theta * math.cos(d) * math.cos(a_z) + sin_theta * math.sin(d)
        C = -sin_theta * math.cos(d) * math.cos(a_z) + cos_theta * math.sin(d)
        out_a = math.atan2(A, B) - zeta

    # atan2(C, hypot(A,B)) instead of asin(C) — stable next to the poles.
    out_d = math.atan2(C, math.hypot(A, B))
    return math.degrees(out_a) % 360.0, math.degrees(out_d)


def j2000_to_jnow(ra_deg: float, dec_deg: float, t: float = None) -> tuple:
    """J2000 -> equinox of date. ``t`` defaults to now (Julian centuries)."""
    if t is None:
        t = julian_centuries_now()
    return precess(ra_deg, dec_deg, t, inverse=False)


def jnow_to_j2000(ra_deg: float, dec_deg: float, t: float = None) -> tuple:
    """Equinox of date -> J2000. ``t`` defaults to now (Julian centuries)."""
    if t is None:
        t = julian_centuries_now()
    return precess(ra_deg, dec_deg, t, inverse=True)
