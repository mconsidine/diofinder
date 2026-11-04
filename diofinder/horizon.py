"""Horizon-based solve rejection helpers (pure, no hardware/numpy deps).

olive-solve's solver gained an opt-in horizon prune: given the observer's
latitude and Local Sidereal Time it drops candidate star patterns that sit
below the horizon before the expensive verification (and rejects sub-horizon
false positives). This module computes the LST diofinder needs to feed it and
decides, safely, whether to enable the prune for a given frame.

**Default OFF** (`horizon_reject_enabled`, config). The prune is only worth
anything on the blind / lost-in-space path, and a *wrong clock* rotates the
horizon and would reject valid solves — the Pi Zero 2W has no RTC — so the
feature stays off until the operator explicitly enables it AND the site and
clock look sane. `observer_solve_kwargs` returns an empty dict (a no-op:
the solver behaves exactly as before) whenever any gate fails.

Pure functions, unit-tested in `tests/test_horizon.py` with no hardware.
"""

from __future__ import annotations

import datetime
import math

# A UTC year below this means the system clock was never set (Pi has no RTC).
# With no trusted time the horizon would be rotated arbitrarily, so we refuse
# to prune. 2025 predates the first shipped build; anything earlier is bogus.
_MIN_PLAUSIBLE_YEAR = 2025


def _julian_date(dt_utc: datetime.datetime) -> float:
    """Julian Date for a naive/aware UTC datetime (Fliegel–Van Flandern)."""
    y, m = dt_utc.year, dt_utc.month
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    day_frac = (
        dt_utc.hour
        + dt_utc.minute / 60.0
        + (dt_utc.second + dt_utc.microsecond / 1e6) / 3600.0
    ) / 24.0
    return (
        int(365.25 * (y + 4716))
        + int(30.6001 * (m + 1))
        + dt_utc.day
        + day_frac
        + b
        - 1524.5
    )


def local_sidereal_time_deg(dt_utc: datetime.datetime, longitude_deg: float) -> float:
    """Local (apparent≈mean) Sidereal Time in degrees [0, 360).

    `longitude_deg` is East-positive. Uses UTC as a UT1 proxy (error < 1 s ≈
    0.004°) and mean GMST (equation of the equinoxes < 0.005°) — both far below
    the solver's ~5° horizon margin. The zenith it yields is of-date while the
    catalog is J2000; precession (< ~0.4° over decades) is likewise absorbed by
    that margin.
    """
    d = _julian_date(dt_utc) - 2451545.0
    gmst = (280.46061837 + 360.98564736629 * d) % 360.0
    return (gmst + longitude_deg) % 360.0


def radec_to_altaz(ra_deg: float, dec_deg: float,
                   latitude_deg: float, lst_deg: float):
    """Equatorial (RA/Dec, of date) -> horizontal (altitude, azimuth) for an
    observer at ``latitude_deg`` whose local sidereal time is ``lst_deg`` (all
    degrees). Azimuth uses the compass convention: measured from North and
    increasing toward East (0=N, 90=E, 180=S, 270=W). Returns
    ``(altitude_deg, azimuth_deg)`` with azimuth in [0, 360).

    Pure trig, no numpy. Precision tracks local_sidereal_time_deg (mean equinox
    of date, well under a degree) — ample for a finder readout. The caller is
    responsible for passing RA/Dec of date (JNow) and a valid site + clock.
    """
    H = math.radians(lst_deg - ra_deg)              # local hour angle
    phi = math.radians(latitude_deg)
    dec = math.radians(dec_deg)
    sin_alt = (math.sin(phi) * math.sin(dec)
               + math.cos(phi) * math.cos(dec) * math.cos(H))
    sin_alt = max(-1.0, min(1.0, sin_alt))
    alt = math.asin(sin_alt)
    az = math.atan2(
        -math.cos(dec) * math.sin(H),
        math.sin(dec) * math.cos(phi)
        - math.cos(dec) * math.sin(phi) * math.cos(H),
    )
    return math.degrees(alt), math.degrees(az) % 360.0


def lx200_longitude_to_east(lon_lx200: float) -> float:
    """LX200 :Sg longitude -> diofinder's East-positive longitude in (-180, 180].

    The Meade LX200 protocol (and SkySafari's LX200 drivers, OnStep, and
    AstroKeith's eFinder: "Longitude as decimal degrees West +ve") measures
    site longitude WEST-positive: east longitudes are negative, or 180-360
    in the classic unsigned form. Everything inside diofinder (the conf, the
    Config page "+E" label, local_sidereal_time_deg) is East-positive, so the
    conversion happens only at the LX200 boundary, like the J2000<->JNow one.
    Vermont, 72 deg 40' W:  "+072*40" -> -72.667.  10 deg E: "350*00" or
    "-010*00" -> +10.0."""
    east = (-float(lon_lx200) + 180.0) % 360.0 - 180.0
    return 180.0 if east == -180.0 else east


def east_longitude_to_lx200(lon_east: float) -> float:
    """East-positive longitude -> LX200 West-positive signed degrees in
    (-180, 180], for the :Gg reply (inverse of lx200_longitude_to_east)."""
    west = (-float(lon_east) + 180.0) % 360.0 - 180.0
    return 180.0 if west == -180.0 else west


def parse_utc_timestamp(s) -> datetime.datetime | None:
    """ISO-8601 timestamp -> naive UTC datetime, or None if it can't be parsed.

    Accepts what diofinder itself writes, e.g. frames.json
    `exposure_start_utc` = '2026-07-23T01:24:00.798+00:00' (fractional
    seconds + an explicit offset), the plain '2026-07-23T01:24:00Z' form, a
    space instead of 'T', and the easy-to-make hybrid '...+00:00Z'. A value
    with no offset is taken as UTC; any other offset is converted to UTC."""
    if s is None:
        return None
    t = str(s).strip()
    if not t:
        return None
    if t[-1] in "Zz":
        t = t[:-1]
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt


def _coord(value, pos: str, neg: str) -> float:
    """A number, or a number with a hemisphere letter ('72.667W' -> -72.667)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    s = str(value).strip().upper().replace("°", "").replace(" ", "")
    sign = 1.0
    if s and s[-1] in (pos, neg):
        sign = 1.0 if s[-1] == pos else -1.0
        s = s[:-1]
        if s.startswith("-"):
            raise ValueError(f"{value!r}: give a sign or a letter, not both")
    return sign * float(s)


ELEVATION_RANGE_M = (-500.0, 9000.0)


def parse_site(args: dict) -> dict:
    """Validated site fields from a dict with any of latitude_deg (+N, or
    '43.95N'), longitude_deg (East-positive, or '72.667W'; 0..360 accepted and
    folded to -180..180) and elevation_m. Returns only the fields given;
    raises ValueError with a readable message."""
    out = {}
    if args.get("latitude_deg") not in (None, ""):
        lat = _coord(args["latitude_deg"], "N", "S")
        if not -90.0 <= lat <= 90.0:
            raise ValueError(f"latitude {lat} is outside -90..90")
        out["latitude_deg"] = lat
    if args.get("longitude_deg") not in (None, ""):
        lon = _coord(args["longitude_deg"], "E", "W")
        if not -180.0 <= lon < 360.0:
            raise ValueError(f"longitude {lon} is outside -180..360")
        out["longitude_deg"] = lon - 360.0 if lon > 180.0 else lon
    if args.get("elevation_m") not in (None, ""):
        el = float(str(args["elevation_m"]).strip().lower().rstrip("m"))
        lo, hi = ELEVATION_RANGE_M
        if not lo <= el <= hi:
            raise ValueError(f"elevation {el} m is outside {lo:g}..{hi:g} m")
        out["elevation_m"] = el
    return out


def site_is_set(latitude_deg: float, longitude_deg: float) -> bool:
    """True unless lat/lon are the unconfigured (0, 0) default (null island)."""
    return not (latitude_deg == 0.0 and longitude_deg == 0.0)


# The round placeholder bench site test mode used to ship as its explicit
# test_latitude_deg/test_longitude_deg defaults. Still the last resort, but no
# longer treated as a deliberate choice: a conf carrying exactly these values
# (every pre-0.11.76 device) gets the real site instead.
TEST_SITE_PLACEHOLDER = (40.0, -75.0)


def test_site(cfg):
    """(latitude_deg, longitude_deg, source) to use in TEST MODE.

    1. ``test_latitude_deg``/``test_longitude_deg`` when the user set them
       (not 0,0 and not the old shipped placeholder) -> "test";
    2. else the configured site ``latitude_deg``/``longitude_deg`` (set by
       SkySafari or the Config page) -> "site";
    3. else the 40 N / 75 W placeholder -> "placeholder".
    Never (0, 0)."""
    def _f(name):
        try:
            return float(getattr(cfg, name, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
    tlat, tlon = _f("test_latitude_deg"), _f("test_longitude_deg")
    if site_is_set(tlat, tlon) and (tlat, tlon) != TEST_SITE_PLACEHOLDER:
        return tlat, tlon, "test"
    lat, lon = _f("latitude_deg"), _f("longitude_deg")
    if site_is_set(lat, lon):
        return lat, lon, "site"
    return TEST_SITE_PLACEHOLDER[0], TEST_SITE_PLACEHOLDER[1], "placeholder"


def clock_is_plausible(now_utc: datetime.datetime | None = None) -> bool:
    """True if the system clock has plausibly been set (year >= 2025)."""
    now_utc = now_utc or datetime.datetime.utcnow()
    return now_utc.year >= _MIN_PLAUSIBLE_YEAR


def observer_solve_kwargs(
    enabled: bool,
    latitude_deg: float,
    longitude_deg: float,
    now_utc: datetime.datetime | None = None,
) -> dict:
    """Solve kwargs enabling the horizon prune, or ``{}`` when any gate fails.

    Gates (all must hold): the feature is `enabled`, the site is configured
    (not 0,0), and the clock is plausibly set. On success returns
    ``{"observer_latitude": lat, "observer_lst": lst}`` (per-star prune only;
    the boresight-altitude gate is deliberately not used). Empty dict → the
    solve is byte-identical to horizon-off behavior.
    """
    if not enabled:
        return {}
    if not site_is_set(latitude_deg, longitude_deg):
        return {}
    now_utc = now_utc or datetime.datetime.utcnow()
    if not clock_is_plausible(now_utc):
        return {}
    return {
        "observer_latitude": float(latitude_deg),
        "observer_lst": local_sidereal_time_deg(now_utc, longitude_deg),
    }
