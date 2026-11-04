#!/usr/bin/env python3
"""Identify satellites in a diofinder live capture and refine their elements.

Off-device companion to ``satcheck_overlay.py`` (which asks SatChecker, i.e.
the public catalog). This script takes the straight trails found in a
capture's stacked frames, measures each trail frame by frame (RA/Dec at every
exposure midpoint), and matches them against element sets you supply, such as
Mike McCants' classified file (https://mmccants.org/tles/classfd.zip), his
integrated file (https://mmccants.org/tles/inttles.zip), or CelesTrak
downloads. Satellites SatChecker does not know (classified, analyst objects,
very recent launches) can be identified this way.

How the matching works, and why old element sets are fine: SGP4 propagates
an element set to any time, before or after its epoch. An element set that is
days or weeks old keeps its orbital PLANE well long after its TIMING has
drifted (drag, manoeuvres). So each candidate is compared with the trail by
line, direction and rate, while the along-track time is left free: the result
reads "NOSS 3-4 (A), 41 s early, 0.03 deg rms" (the object passed 41 s
before its elements said it would). The best match is then
REFINED: its elements are moved to the time of the observations and its mean
anomaly and node are fitted to them, giving an element set that reproduces
this pass. (A single 10-60 s arc from one site cannot determine an orbit from
scratch: the direction and rate are well measured, the distance is not.)

Two kinds of track are measured: trails in the stacked frames (finder held
still; satcheck_overlay) and frame-by-frame tracks (sat_tracks.py: an object
the scope was hand-guided to follow, or an arc longer than one stack group).

Outputs (in --out, default ``<archive>_ident/``):
  * ``observations_iod.txt``: --points-per-pass (4) positions per track, spread
    over the arc, in the IOD format used by the SeeSat-L community and read by
    Find_Orb, sat_id and satfit. Uncertainty codes come from the measurement
    (arc scatter + frame WCS fit) and from the capture's clock record; set
    --station (9999 = placeholder) and --status (sky: E/G/F/P/B/T).
    Unidentified objects are written as 99999 99 099A (--unknown-desig).
    Behaviour (S/F/R/I/X), magnitude, its uncertainty and the flash period go
    in columns 66-80 (--no-iod-mag to omit the photometry).
  * ``observations_rde.txt``: the same points in Russell Eberst's R.D.E.
    format (header line, day lines, brightest/faintest magnitude, flash
    period, remark code; unknown objects 9900000; --time-standard).
  * ``seesat_post.txt``: a draft SeeSat-L message (station, equipment, clock,
    conditions (--status sky code + --conditions), the IOD lines, the object's
    name or unkYYMMDD, the refined TLE).
  * ``observations.json``: every frame's position with sigma, the site and the
    clock; ``sat_orbit.py fit`` reads it.
  * ``lightcurve_<track>.csv`` / ``.png``: magnitude per frame and along the
    dashes; the identification report gives the magnitude, flash period and
    behaviour (S steady, F constant flash period, I irregular).
  * ``identification.txt`` / ``.json``: per track, the candidates (name,
    catalog number, source file, element-set age, time offset, rms, cross-
    track offset, rate ratio, direction difference, sunlit or in shadow).
  * Refined elements for each identified track, in every current format:
    ``refined.tle``  - classic two-line elements (catalog numbers 100000-
                       339999 are written in the Alpha-5 form, e.g. A0001 =
                       100001; numbers beyond that cannot be expressed as a
                       TLE at all and are skipped there),
    ``refined_omm.kvn``  - CCSDS Orbit Mean-Elements Message, key = value,
    ``refined_omm.xml``  - CCSDS OMM in XML (the NDM schema CelesTrak and
                           Space-Track serve),
    ``refined_omm.json`` / ``refined_omm.csv`` - the GP formats CelesTrak
                           serves (OMM keywords; 9-digit catalog numbers).

Needs: numpy, astropy, matplotlib, Pillow (as satcheck_overlay) plus
``pip install sgp4``. No network unless --download is given.

Examples:
  python3 tests/sat_identify.py livecap_20261102T020749Z.zip \\
      --elements classfd.zip inttles.zip --window 600
  python3 tests/sat_identify.py livecap_...zip --download --window 600
  python3 tests/sat_identify.py livecap_...zip --elements active.tle \\
      --responses out/satchecker_responses.json --offline

Accuracy: across the trail ~1 px (51 arcsec); along it the clock decides
(0.1 s at 0.5 deg/s is 0.05 deg), so set the finder's clock from your phone
(Utilities -> Clock) before capturing. Magnitudes: about +-0.2-0.3 mag
(sat_photometry.py explains the limits). The camera reads its rows out over
some tens of milliseconds (rolling shutter); --readout-ms adds that per row.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import sys
import urllib.request
import xml.sax.saxutils as _xml
import zipfile
from dataclasses import dataclass, field

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import satcheck_overlay as so  # noqa: E402
import sat_photometry as ph  # noqa: E402
import sat_tracks as tr  # noqa: E402

DOWNLOAD_SOURCES = {
    "classfd": "https://mmccants.org/tles/classfd.zip",
    "inttles": "https://mmccants.org/tles/inttles.zip",
}

SECONDS_PER_DAY = 86400.0
EARTH_RADIUS_KM = 6378.137


# --------------------------------------------------------------------------- #
# Element sets
# --------------------------------------------------------------------------- #

@dataclass
class ElementSet:
    satrec: object
    name: str
    source: str
    intldesg: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def epoch_jd(self):
        return self.satrec.jdsatepoch + self.satrec.jdsatepochF

    @property
    def norad(self):
        """The real catalog number. SGP4's Satrec cannot hold numbers above
        339999 (the Alpha-5 limit), so 9-digit OMM objects are propagated
        under a placeholder number and keep theirs here."""
        return int(self.extra.get("norad", self.satrec.satnum))

    @property
    def norad_str(self):
        return self.satrec.satnum_str if self.norad <= 339999 else str(self.norad)


def _parse_tle_text(text, source):
    from sgp4.api import Satrec
    lines = [ln.rstrip("\r\n") for ln in text.splitlines()]
    out = []
    i = 0
    while i < len(lines) - 1:
        l1, l2 = lines[i], lines[i + 1]
        if l1.startswith("1 ") and l2.startswith("2 "):
            name = ""
            if i > 0 and lines[i - 1].strip() and not lines[i - 1].startswith(("1 ", "2 ")):
                name = lines[i - 1].strip()
                if name.startswith("0 "):        # 3LE style "0 NAME"
                    name = name[2:].strip()
            try:
                s = Satrec.twoline2rv(l1, l2)
            except Exception:
                s = None
            if s is not None and s.error == 0:
                out.append(ElementSet(s, name or s.satnum_str, source,
                                      intldesg=l1[9:17].strip()))
            i += 2
        else:
            i += 1
    return out


def _omm_to_elements(rows, source):
    from sgp4 import omm
    from sgp4.api import Satrec
    out = []
    for row in rows:
        try:
            row = {k: str(v) for k, v in row.items()}
            norad = int(float(row.get("NORAD_CAT_ID", "0") or 0))
            extra = {}
            if norad > 339999:
                extra["norad"] = norad
                row["NORAD_CAT_ID"] = "0"
            s = Satrec()
            omm.initialize(s, row)
            out.append(ElementSet(s, row.get("OBJECT_NAME", "") or str(norad),
                                  source, intldesg=_cospar_to_tle(row.get("OBJECT_ID", "")),
                                  extra=extra))
        except Exception:
            continue
    return out


def _cospar_to_tle(object_id):
    """'1973-056B' -> '73056B' (TLE international designator)."""
    if len(object_id) >= 9 and object_id[4] == "-":
        return object_id[2:4] + object_id[5:]
    return object_id


def _parse_kvn(text):
    """CCSDS OMM in KVN form ('KEY = VALUE' lines; several messages may
    follow each other, each starting with CCSDS_OMM_VERS) -> [dict]."""
    out, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("COMMENT") or "=" not in line:
            continue
        k, v = (x.strip() for x in line.split("=", 1))
        if k == "CCSDS_OMM_VERS":
            if cur:
                out.append(cur)
            cur = {}
            continue
        if cur is None:
            cur = {}
        cur[k] = v.split("[")[0].strip()                 # drop "[deg]"-style units
    if cur:
        out.append(cur)
    rows = []
    for d in out:
        if "MEAN_MOTION" not in d or "EPOCH" not in d:
            continue
        d.setdefault("EPHEMERIS_TYPE", "0")
        d.setdefault("CLASSIFICATION_TYPE", "U")
        d.setdefault("ELEMENT_SET_NO", "999")
        d.setdefault("REV_AT_EPOCH", "0")
        d.setdefault("BSTAR", "0")
        d.setdefault("MEAN_MOTION_DOT", "0")
        d.setdefault("MEAN_MOTION_DDOT", "0")
        d.setdefault("OBJECT_ID", "")
        rows.append(d)
    return rows


def parse_elements_text(text, source):
    """TLE / 3LE text, OMM CSV, OMM XML, OMM KVN or OMM/GP JSON ->
    [ElementSet]."""
    from sgp4 import omm
    t = text.lstrip("\ufeff").lstrip()
    if t.startswith("<"):
        return _omm_to_elements(omm.parse_xml(io.StringIO(text)), source)
    if t.startswith("[") or t.startswith("{"):
        data = json.loads(t)
        return _omm_to_elements(data if isinstance(data, list) else [data], source)
    if t.startswith("OBJECT_NAME") or t.startswith('"OBJECT_NAME'):
        return _omm_to_elements(omm.parse_csv(io.StringIO(t)), source)
    if "CCSDS_OMM_VERS" in t[:2000]:
        return _omm_to_elements(_parse_kvn(t), source)
    return _parse_tle_text(text, source)


ELEMENT_SUFFIXES = (".tle", ".tles", ".txt", ".3le", ".2le", ".csv", ".xml", ".json",
                    ".kvn", ".omm", ".zip", ".gz")


def _read_texts(path):
    """[(source name, text)] from a file: plain, .gz, or a .zip (every member,
    nested .gz members too)."""
    import gzip
    base = os.path.basename(path)
    if zipfile.is_zipfile(path):
        out = []
        with zipfile.ZipFile(path) as zf:
            for n in zf.namelist():
                if n.endswith("/"):
                    continue
                raw = zf.read(n)
                if n.lower().endswith(".gz"):
                    raw = gzip.decompress(raw)
                out.append((f"{base}:{n}", raw.decode("utf-8", "replace")))
        return out
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return [(base, raw.decode("utf-8", "replace"))]


def load_elements(paths, near_jd=None):
    """Element sets from files (TLE/3LE, OMM XML/KVN/JSON/CSV; .zip and .gz
    are opened). Per catalog number the NEWEST epoch wins (or, with
    ``near_jd``, the epoch closest to that time). ``paths`` may also hold
    folders, comma-separated lists and URLs: see expand_element_sources."""
    best = {}
    for path in expand_element_sources(paths):
        for src, text in _read_texts(path):
            for e in parse_elements_text(text, src):
                k = e.norad
                if k not in best:
                    best[k] = e
                elif near_jd is None:
                    if e.epoch_jd > best[k].epoch_jd:
                        best[k] = e
                elif abs(e.epoch_jd - near_jd) < abs(best[k].epoch_jd - near_jd):
                    best[k] = e
    return list(best.values())


URL_CACHE_HOURS = 2.0      # CelesTrak asks clients not to re-fetch more often


def fetch_url(url, cache_dir=None, max_age_h=URL_CACHE_HOURS, log=print):
    """Download an element file (cached for max_age_h: CelesTrak throttles
    repeated requests for the same data). Returns the local path."""
    import hashlib
    import time
    cache_dir = cache_dir or os.path.join(os.path.expanduser("~"), ".diofinder_elements")
    os.makedirs(cache_dir, exist_ok=True)
    tail = os.path.basename(url.split("?")[0]) or "elements"
    dest = os.path.join(cache_dir, hashlib.sha1(url.encode()).hexdigest()[:10] + "_" + tail)
    if os.path.exists(dest) and time.time() - os.path.getmtime(dest) < max_age_h * 3600:
        return dest
    (log or print)(f"downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "diofinder-sat-tools"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
    except Exception as e:
        if os.path.exists(dest):              # offline: an older copy beats nothing
            age_h = (time.time() - os.path.getmtime(dest)) / 3600.0
            (log or print)(f"WARNING: download failed ({e}); using the cached copy from "
                           f"{age_h:.1f} h ago ({dest})")
            return dest
        raise SystemExit(f"could not download {url}: {e} (and no cached copy)")
    with open(dest, "wb") as f:
        f.write(data)
    return dest


def expand_element_sources(items, cache_dir=None, log=print):
    """Element-file arguments -> local file paths. Each item may be a file
    (plain, .gz or .zip), a folder (every element-looking file in it and its
    subfolders), a URL (downloaded, cached), or several of these joined by
    commas."""
    out = []
    for item in items or []:
        for part in str(item).split(","):
            part = part.strip().strip('"')
            if not part:
                continue
            if part.lower().startswith(("http://", "https://")):
                out.append(fetch_url(part, cache_dir, log=log))
            elif os.path.isdir(part):
                for root, _, files in sorted(os.walk(part)):
                    for fn in sorted(files):
                        if fn.lower().endswith(ELEMENT_SUFFIXES):
                            out.append(os.path.join(root, fn))
            elif os.path.exists(part):
                out.append(part)
            else:
                raise SystemExit(f"element source not found: {part}")
    return out


def download(sources, cache_dir):
    """Fetch the named element files into cache_dir; returns their paths."""
    os.makedirs(cache_dir, exist_ok=True)
    paths = []
    for name in sources:
        url = DOWNLOAD_SOURCES.get(name, name)
        dest = os.path.join(cache_dir, os.path.basename(url.split("?")[0]) or name)
        print(f"downloading {url}")
        with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:
            f.write(r.read())
        paths.append(dest)
    return paths


# --------------------------------------------------------------------------- #
# Geometry: TEME (SGP4) -> geocentric celestial (GCRS ~ J2000/ICRS axes)
# --------------------------------------------------------------------------- #

class Geometry:
    """Observer position and the TEME->GCRS rotation, around a capture.

    TEME -> GCRS is precession + nutation only (both frames are
    geocentric and non-rotating), constant to far better than an arcsecond
    over an hour, so one matrix serves the whole capture. The observer's
    GCRS position rotates with the Earth; it is tabulated every second and
    interpolated."""

    def __init__(self, site, jd_lo, jd_hi):
        so._offline_astropy()
        import astropy.units as u
        from astropy.coordinates import (GCRS, TEME, CartesianRepresentation,
                                         EarthLocation)
        from astropy.time import Time
        self.loc = EarthLocation(lat=site["latitude_deg"] * u.deg,
                                 lon=site["longitude_deg"] * u.deg,
                                 height=(site.get("elevation_m") or 0.0) * u.m)
        jd_mid = 0.5 * (jd_lo + jd_hi)
        tm = Time(np.full(3, jd_mid), format="jd", scale="utc")
        rep = CartesianRepresentation(np.eye(3) * 1000.0 * u.km)   # columns = axes
        g = TEME(rep, obstime=tm).transform_to(GCRS(obstime=tm))
        self.M = (g.cartesian.xyz.to(u.km).value / 1000.0)        # M[:, k] = image of e_k
        n = int(math.ceil((jd_hi - jd_lo) * SECONDS_PER_DAY)) + 3
        self.jd0 = jd_lo - 1.0 / SECONDS_PER_DAY
        self.grid = self.jd0 + np.arange(n) / SECONDS_PER_DAY
        t = Time(self.grid, format="jd", scale="utc")
        pos, _ = self.loc.get_gcrs_posvel(t)
        self.obs = pos.xyz.to(u.km).value.T                        # (n, 3)
        self.tm_jd = jd_mid

    def observer(self, jd):
        jd = np.asarray(jd, float)
        x = (jd - self.jd0) * SECONDS_PER_DAY
        x = np.clip(x, 0, len(self.grid) - 1.000001)
        i = np.floor(x).astype(int)
        f = (x - i)[..., None]
        return self.obs[i] * (1 - f) + self.obs[i + 1] * f

    def zenith(self, jd):
        o = self.observer(jd)
        return o / np.linalg.norm(o, axis=-1, keepdims=True)

    def sat_gcrs(self, r_teme):
        return np.asarray(r_teme) @ self.M.T

    def topo_unit(self, r_teme, jd):
        """Unit vector observer->satellite (GCRS axes) and the elevation sine."""
        g = self.sat_gcrs(r_teme)
        v = g - self.observer(jd)
        d = np.linalg.norm(v, axis=-1, keepdims=True)
        u_ = v / d
        return u_, np.sum(u_ * self.zenith(jd), axis=-1)


def vec_to_radec(v):
    v = np.asarray(v, float)
    ra = np.degrees(np.arctan2(v[..., 1], v[..., 0])) % 360.0
    dec = np.degrees(np.arcsin(np.clip(v[..., 2] / np.linalg.norm(v, axis=-1), -1, 1)))
    return ra, dec


def radec_to_vec(ra, dec):
    ra, dec = np.radians(ra), np.radians(dec)
    return np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)], -1)


def _ang(a, b):
    """Angle (deg) between unit vectors (broadcasting)."""
    c = np.cross(a, b)
    return np.degrees(np.arctan2(np.linalg.norm(c, axis=-1), np.sum(a * b, axis=-1)))


def _split_jd(jd):
    jd = np.asarray(jd, float)
    whole = np.floor(jd - 0.5) + 0.5
    return whole, jd - whole


def propagate(satrecs, jd):
    """SGP4 for many satellites x many times: (err (S,N), r_teme (S,N,3) km)."""
    from sgp4.api import SatrecArray
    whole, frac = _split_jd(jd)
    e, r, _ = SatrecArray(list(satrecs)).sgp4(whole, frac)
    return e, r


def sunlit(r_gcrs_km, jd):
    """True unless inside Earth's cylindrical shadow."""
    so._offline_astropy()
    from astropy.coordinates import get_sun
    from astropy.time import Time
    s = get_sun(Time(jd, format="jd", scale="utc")).cartesian.xyz.value
    s = s / np.linalg.norm(s)
    along = float(np.dot(r_gcrs_km, s))
    perp = float(np.linalg.norm(np.asarray(r_gcrs_km) - along * s))
    return not (along < 0 and perp < EARTH_RADIUS_KM)


# --------------------------------------------------------------------------- #
# Observations
# --------------------------------------------------------------------------- #

def trail_observations(trail, st, readout_ms=0.0, height=760):
    """Per-frame (jd, ra, dec) of a timed trail (satcheck_overlay
    trail_kinematics), in time order. ``readout_ms``: rolling-shutter
    readout of the whole frame; a row y is exposed y/height of it later."""
    w = st["ref"]["wcs"]
    p0 = np.array(trail["p0"], float)
    u_ = np.array(trail["u"], float)
    files = trail.get("track_files") or [None] * len(trail.get("track_obs") or [])
    out = []
    for (jd, s), fname in zip(trail.get("track_obs") or [], files):
        x, y = p0 + u_ * s
        ra, dec = w.pixel_to_world_values(x, y)
        jd_row = jd + (readout_ms / 1000.0) * (y / height) / SECONDS_PER_DAY
        out.append({"jd": float(jd_row), "ra": float(ra), "dec": float(dec),
                    "x": float(x), "y": float(y), "file": fname})
    return sorted(out, key=lambda o: o["jd"])


def _arc_summary(obs):
    """Mid point, motion direction (unit tangent at mid) and rate (deg/s)."""
    v = radec_to_vec([o["ra"] for o in obs], [o["dec"] for o in obs])
    jd = np.array([o["jd"] for o in obs])
    k = len(obs) // 2
    m = v[k]
    span = (jd[-1] - jd[0]) * SECONDS_PER_DAY
    rate = float(_ang(v[0], v[-1])) / span if span > 0 else 0.0
    d = v[-1] - v[0]
    d = d - np.dot(d, m) * m
    n = np.linalg.norm(d)
    return m, (d / n if n > 0 else d), rate, float(jd[k]), v, jd


# --------------------------------------------------------------------------- #
# Identification
# --------------------------------------------------------------------------- #

def identify(obs, elems, geom, max_offset_s=600.0, coarse_step_s=4.0,
             max_cross_deg=1.0, top=5, batch=2000):
    """Rank element sets against one trail's observations.

    Returns a list of dicts (best first) with: element set, offset_s (the
    element set's timing error: the elements put the object where it was
    seen offset_s LATER, i.e. positive = the object is AHEAD of its elements,
    passing early), rms_deg (all observations, after the time shift),
    cross_deg (mean cross-track offset), rate_ratio, dir_deg, lit."""
    if len(obs) < 3:
        return []
    m, d_obs, rate_obs, jd_mid, v_obs, jd_obs = _arc_summary(obs)
    offs = np.arange(-max_offset_s, max_offset_s + coarse_step_s / 2, coarse_step_s)
    jd_grid = jd_mid + offs / SECONDS_PER_DAY
    survivors = []
    for b in range(0, len(elems), batch):
        chunk = elems[b:b + batch]
        e, r = propagate([x.satrec for x in chunk], jd_grid)
        uvec, sin_el = geom.topo_unit(r, jd_grid[None, :])
        ang = _ang(uvec, m[None, None, :])
        bad = (e != 0) | (sin_el < 0) | ~np.isfinite(ang)
        ang = np.where(bad, np.inf, ang)
        k = np.argmin(ang, axis=1)
        for i, el in enumerate(chunk):
            ki = k[i]
            a = ang[i, ki]
            if not np.isfinite(a):
                continue
            kj = ki + 1 if ki + 1 < len(jd_grid) else ki - 1
            step = float(_ang(uvec[i, ki], uvec[i, kj])) if np.isfinite(ang[i, kj]) else 5.0
            if a <= max_cross_deg + 0.6 * step + 0.05:
                survivors.append((el, float(offs[ki])))
    cands = []
    for el, off0 in survivors:
        c = _fine_match(el, off0, coarse_step_s, obs, m, d_obs, rate_obs, jd_mid,
                        v_obs, jd_obs, geom)
        if c is not None and c["cross_deg"] <= max_cross_deg:
            cands.append(c)
    cands.sort(key=lambda c: c["score"])
    return cands[:top]


def _fine_match(el, off0, step, obs, m, d_obs, rate_obs, jd_mid, v_obs, jd_obs, geom):
    offs = off0 + np.arange(-step, step + 0.01, 0.05)
    jd = jd_mid + offs / SECONDS_PER_DAY
    e, r = propagate([el.satrec], jd)
    if np.any(e[0] != 0):
        return None
    u_, _ = geom.topo_unit(r[0], jd)
    a = _ang(u_, m[None, :])
    k = int(np.argmin(a))
    k = min(max(k, 1), len(offs) - 2)
    # Closest approach on the local segment.
    p, q = u_[k - 1], u_[k + 1]
    seg = q - p
    t = float(np.clip(np.dot(m - p, seg) / max(np.dot(seg, seg), 1e-30), 0, 1))
    off = float(offs[k - 1] + t * (offs[k + 1] - offs[k - 1]))
    # Residuals of every observation with the same time shift.
    e2, r2 = propagate([el.satrec], jd_obs + off / SECONDS_PER_DAY)
    if np.any(e2[0] != 0):
        return None
    pv, sin_el = geom.topo_unit(r2[0], jd_obs + off / SECONDS_PER_DAY)
    res = _ang(pv, v_obs)
    rms = float(np.sqrt(np.mean(res ** 2)))
    nrm = np.cross(m, d_obs)
    cross = float(np.degrees(np.mean(np.arcsin(np.clip(pv @ nrm, -1, 1)))))
    span = (jd_obs[-1] - jd_obs[0]) * SECONDS_PER_DAY
    rate_p = float(_ang(pv[0], pv[-1])) / span if span > 0 else 0.0
    dp = pv[-1] - pv[0]
    dp = dp - np.dot(dp, m) * m
    dir_deg = float(_ang(dp / (np.linalg.norm(dp) or 1), d_obs))
    ratio = rate_p / rate_obs if rate_obs > 0 else float("nan")
    lit = sunlit(geom.sat_gcrs(r2[0][len(obs) // 2]), jd_mid + off / SECONDS_PER_DAY)
    age_days = jd_mid - el.epoch_jd
    score = (rms / 0.05) ** 2 + (dir_deg / 10.0) ** 2 + (math.log(max(ratio, 1e-6)) / 0.1) ** 2 \
        + (off / 300.0) ** 2
    return {"element_set": el, "name": el.name, "norad": el.norad,
            "norad_str": el.norad_str, "source": el.source,
            "epoch_age_days": float(age_days), "offset_s": off, "rms_deg": rms,
            "cross_deg": abs(cross), "rate_ratio": ratio, "dir_deg": dir_deg,
            "lit": lit, "min_elevation_deg": float(np.degrees(np.arcsin(np.min(sin_el)))),
            "score": score}


def is_match(c, max_rms_deg=0.25, max_dir_deg=20.0, rate_tol=0.25):
    return (c["rms_deg"] <= max_rms_deg and c["dir_deg"] <= max_dir_deg
            and abs(c["rate_ratio"] - 1.0) <= rate_tol)


# --------------------------------------------------------------------------- #
# Refinement: move the elements to the observation time and fit them
# --------------------------------------------------------------------------- #

def _jd_to_sgp4_epoch(jd):
    return jd - 2433281.5        # days since 1949 December 31 00:00 UT


def _make_satrec(base, epoch_jd, mo, nodeo, argpo, no_kozai=None):
    from sgp4.api import Satrec, WGS72
    s = Satrec()
    s.sgp4init(WGS72, "i", base.satnum, _jd_to_sgp4_epoch(epoch_jd), base.bstar,
               base.ndot, base.nddot, base.ecco, argpo, base.inclo, mo % (2 * math.pi),
               base.no_kozai if no_kozai is None else no_kozai, nodeo % (2 * math.pi))
    s.classification = getattr(base, "classification", "U") or "U"
    s.intldesg = getattr(base, "intldesg", "") or ""
    s.elnum = 999
    revs = (epoch_jd - (base.jdsatepoch + base.jdsatepochF)) * base.no_kozai * 1440 / (2 * math.pi)
    s.revnum = int(max(0, (getattr(base, "revnum", 0) or 0) + revs)) % 100000
    return s


def _residuals(satrec, geom, jd_obs, v_obs):
    e, r = propagate([satrec], jd_obs)
    if np.any(e[0] != 0):
        return None
    p, _ = geom.topo_unit(r[0], jd_obs)
    # Tangent-plane residuals (radians) in a fixed local basis per point.
    out = []
    for pi, oi in zip(p, v_obs):
        e1 = np.cross([0.0, 0.0, 1.0], oi)
        e1 = e1 / (np.linalg.norm(e1) or 1)
        e2 = np.cross(oi, e1)
        dv = pi - oi
        out += [np.dot(dv, e1), np.dot(dv, e2)]
    return np.array(out)


def refine(cand, obs, geom, iterations=8):
    """Element set moved to the observation midpoint (secular rates from
    SGP4 itself) and its mean anomaly and node fitted to the observations.
    Returns {satrec, rms_before_deg, rms_after_deg, epoch_jd}."""
    base = cand["element_set"].satrec
    v_obs = radec_to_vec([o["ra"] for o in obs], [o["dec"] for o in obs])
    jd_obs = np.array([o["jd"] for o in obs])
    epoch = float(np.median(jd_obs))
    dtm = (epoch - (base.jdsatepoch + base.jdsatepochF)) * 1440.0
    # Secular motion to the new epoch, plus the measured timing error.
    mo = base.mo + base.mdot * (dtm + cand["offset_s"] / 60.0)
    nodeo = base.nodeo + base.nodedot * dtm
    argpo = base.argpo + base.argpdot * dtm
    x = np.array([0.0, 0.0])
    r0 = _residuals(base, geom, jd_obs, v_obs)
    rms_before = float(np.degrees(np.sqrt(np.mean(r0 ** 2)) * math.sqrt(2))) if r0 is not None else None
    best = None
    for _ in range(iterations):
        s = _make_satrec(base, epoch, mo + x[0], nodeo + x[1], argpo)
        r = _residuals(s, geom, jd_obs, v_obs)
        if r is None:
            break
        best = (s, r)
        J = np.empty((len(r), 2))
        for k, h in enumerate((1e-6, 1e-6)):
            dx = np.zeros(2)
            dx[k] = h
            sk = _make_satrec(base, epoch, mo + x[0] + dx[0], nodeo + x[1] + dx[1], argpo)
            rk = _residuals(sk, geom, jd_obs, v_obs)
            J[:, k] = (rk - r) / h
        step, *_ = np.linalg.lstsq(J, -r, rcond=None)
        x = x + step
        if np.max(np.abs(step)) < 1e-9:
            break
    s = _make_satrec(base, epoch, mo + x[0], nodeo + x[1], argpo)
    r = _residuals(s, geom, jd_obs, v_obs)
    if r is None and best is not None:
        s, r = best
    rms_after = float(np.degrees(np.sqrt(np.mean(r ** 2)) * math.sqrt(2))) if r is not None else None
    return {"satrec": s, "rms_before_deg": rms_before, "rms_after_deg": rms_after,
            "epoch_jd": epoch, "dM_deg": math.degrees(x[0]), "dnode_deg": math.degrees(x[1])}


# --------------------------------------------------------------------------- #
# Element-set formats
# --------------------------------------------------------------------------- #

OMM_KEYS = ["OBJECT_NAME", "OBJECT_ID", "EPOCH", "MEAN_MOTION", "ECCENTRICITY",
            "INCLINATION", "RA_OF_ASC_NODE", "ARG_OF_PERICENTER", "MEAN_ANOMALY",
            "EPHEMERIS_TYPE", "CLASSIFICATION_TYPE", "NORAD_CAT_ID", "ELEMENT_SET_NO",
            "REV_AT_EPOCH", "BSTAR", "MEAN_MOTION_DOT", "MEAN_MOTION_DDOT"]
_MEAN_ELEMENTS = ["EPOCH", "MEAN_MOTION", "ECCENTRICITY", "INCLINATION",
                  "RA_OF_ASC_NODE", "ARG_OF_PERICENTER", "MEAN_ANOMALY"]
_TLE_PARAMS = ["EPHEMERIS_TYPE", "CLASSIFICATION_TYPE", "NORAD_CAT_ID", "ELEMENT_SET_NO",
               "REV_AT_EPOCH", "BSTAR", "MEAN_MOTION_DOT", "MEAN_MOTION_DDOT"]


def tle_lines(satrec, norad=None):
    """Classic two-line elements; Alpha-5 for 100000-339999 (sgp4 writes the
    letter form itself). None when the number cannot be written in a TLE."""
    from sgp4 import exporter
    if (norad if norad is not None else satrec.satnum) > 339999:
        return None
    return exporter.export_tle(satrec)


def omm_record(satrec, name, norad=None):
    """OMM keywords (CelesTrak GP order). ``norad`` overrides the Satrec's
    number (9-digit catalog numbers live outside the Satrec)."""
    from sgp4 import exporter
    intl = getattr(satrec, "intldesg", "") or ""
    if len(intl) < 5 or not intl[:2].isdigit():
        # sgp4's exporter needs a designator to build OBJECT_ID; objects
        # without one (some classified sets) get an empty OBJECT_ID.
        satrec.intldesg = "00000A"
        try:
            rec = exporter.export_omm(satrec, name)
        finally:
            satrec.intldesg = intl
        rec["OBJECT_ID"] = ""
    else:
        rec = exporter.export_omm(satrec, name)
    out = {k: rec[k] for k in OMM_KEYS}
    if norad is not None:
        out["NORAD_CAT_ID"] = int(norad)
    return out


def _fmt(v):
    if isinstance(v, float):
        return repr(v) if abs(v) < 1e-4 and v != 0 else f"{v:.10g}"
    return str(v)


def write_omm_kvn(records, path, originator="diofinder sat_identify"):
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    with open(path, "w") as f:
        for rec, comment in records:
            f.write("CCSDS_OMM_VERS = 2.0\n")
            f.write(f"CREATION_DATE = {now}\nORIGINATOR = {originator}\n")
            if comment:
                f.write(f"COMMENT {comment}\n")
            f.write("\n")
            for k in ("OBJECT_NAME", "OBJECT_ID"):
                f.write(f"{k} = {rec[k]}\n")
            f.write("CENTER_NAME = EARTH\nREF_FRAME = TEME\nTIME_SYSTEM = UTC\n"
                    "MEAN_ELEMENT_THEORY = SGP4\n\n")
            for k in _MEAN_ELEMENTS + _TLE_PARAMS:
                f.write(f"{k} = {_fmt(rec[k])}\n")
            f.write("\n")


def write_omm_xml(records, path, originator="diofinder sat_identify"):
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    esc = _xml.escape
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<ndm xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
             'xsi:noNamespaceSchemaLocation="https://sanaregistry.org/r/ndmxml_unqualified/'
             'ndmxml-2.0.0-master-2.0.xsd">']
    for rec, comment in records:
        parts.append('<omm id="CCSDS_OMM_VERS" version="2.0">')
        parts.append(f"<header><CREATION_DATE>{now}</CREATION_DATE>"
                     f"<ORIGINATOR>{esc(originator)}</ORIGINATOR></header>")
        parts.append("<body><segment><metadata>")
        parts.append(f"<OBJECT_NAME>{esc(str(rec['OBJECT_NAME']))}</OBJECT_NAME>"
                     f"<OBJECT_ID>{esc(str(rec['OBJECT_ID']))}</OBJECT_ID>"
                     "<CENTER_NAME>EARTH</CENTER_NAME><REF_FRAME>TEME</REF_FRAME>"
                     "<TIME_SYSTEM>UTC</TIME_SYSTEM>"
                     "<MEAN_ELEMENT_THEORY>SGP4</MEAN_ELEMENT_THEORY>")
        parts.append("</metadata><data>")
        if comment:
            parts.append(f"<COMMENT>{esc(comment)}</COMMENT>")
        parts.append("<meanElements>" + "".join(
            f"<{k}>{_fmt(rec[k])}</{k}>" for k in _MEAN_ELEMENTS) + "</meanElements>")
        parts.append("<tleParameters>" + "".join(
            f"<{k}>{_fmt(rec[k])}</{k}>" for k in _TLE_PARAMS) + "</tleParameters>")
        parts.append("</data></segment></body></omm>")
    parts.append("</ndm>")
    with open(path, "w") as f:
        f.write("\n".join(parts) + "\n")


def write_omm_json(records, path):
    with open(path, "w") as f:
        json.dump([rec for rec, _ in records], f, indent=1)


def write_omm_csv(records, path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(OMM_KEYS)
        for rec, _ in records:
            w.writerow([_fmt(rec[k]) for k in OMM_KEYS])


def write_tle(records, path):
    with open(path, "w") as f:
        for (satrec, name, *rest), comment in records:
            norad = rest[0] if rest else satrec.satnum
            lines = tle_lines(satrec, norad)
            if lines is None:
                f.write(f"# {name}: catalog number {norad} cannot be written "
                        f"as a TLE (above 339999); see the OMM files\n")
                continue
            if comment:
                f.write(f"# {comment}\n")
            f.write(f"{name[:24]}\n{lines[0]}\n{lines[1]}\n")


# --------------------------------------------------------------------------- #
# IOD observation format
# --------------------------------------------------------------------------- #

def _mx(value, unit_exp_offset=8):
    """IOD 'MX' uncertainty: value = M * 10**(X - 8), M and X single digits.
    Rounded UP (an uncertainty is never understated): 1.2 -> 2 x 10**0."""
    if not value or value <= 0:
        return "  "
    x = int(math.floor(math.log10(value) + 1e-9)) + unit_exp_offset
    m = int(math.ceil(value / 10 ** (x - unit_exp_offset) - 1e-6))
    if m >= 10:
        m, x = 1, x + 1
    if x > 9:
        return "99"                                    # the largest code
    return f"{min(max(m, 1), 9)}{max(x, 0)}"


def iod_line(ob, norad="99999", intldesg="", station="9999", status="G",
             time_unc_s=0.1, pos_unc_deg=0.02, behaviour=" ", mag=None, mag_unc=None,
             flash_period_s=None):
    """One observation in IOD format (G. Lewis, IOD Version 0, clarified
    2002; satobs.org/position/IODformat.html): angle format 2 = RA HHMMmmm,
    Dec +DDMMmm, whose positional uncertainty (cols 63-64) is in MINUTES of
    arc (``pos_unc_deg`` is converted); epoch code 5 = J2000; time
    uncertainty (42-43) in seconds. Both MX = M * 10**(X - 8). Photometry
    after the behaviour code (col 66): magnitude sign (col 67) and value
    (68-70, point implied between 69 and 70: '+045' = 4.5), its uncertainty
    (72-73, point between 72 and 73: '03' = 0.3), and the flash period in
    seconds (75-80, point between 77 and 78: '002250' = 2.25 s). Example
    '23794 96 010A   2701 G 19990402012334770 17 25 1324520+382450 37 S'."""
    t = dt.datetime(2000, 1, 1, 12, tzinfo=dt.timezone.utc) + \
        dt.timedelta(days=ob["jd"] - 2451545.0)
    ms = int(round(t.microsecond / 1000.0))
    if ms == 1000:
        t, ms = t + dt.timedelta(seconds=1), 0
    stamp = t.strftime("%Y%m%d%H%M%S") + f"{ms:03d}"
    ra_h = (ob["ra"] % 360.0) / 15.0
    hh = int(ra_h)
    mmm = int(round((ra_h - hh) * 60.0 * 1000.0))
    if mmm >= 60000:
        hh, mmm = (hh + 1) % 24, 0
    sign = "-" if ob["dec"] < 0 else "+"
    dabs = abs(ob["dec"])
    dd = int(dabs)
    mm = int(round((dabs - dd) * 60.0 * 100.0))
    if mm >= 6000:
        dd, mm = dd + 1, 0
    angles = f"{hh:02d}{mmm:05d}{sign}{dd:02d}{mm:04d}"
    yy, num, piece = "  ", "   ", "   "
    if intldesg and len(intldesg) >= 5:
        yy, num, piece = intldesg[:2], intldesg[2:5], intldesg[5:].ljust(3)[:3]
    line = (f"{str(norad)[:5]:>5} {yy} {num}{piece} {station:>4} {status} {stamp} "
            f"{_mx(time_unc_s)} 25 {angles} {_mx(pos_unc_deg * 60.0)} {behaviour or ' '}")
    if mag is not None and -9.9 <= mag <= 99.9:
        tenths = int(math.floor(abs(mag) * 10 + 0.5))
        line += f"{'-' if mag < 0 else '+'}{min(tenths, 999):03d}"
        if mag_unc is not None:
            line += f" {min(int(math.floor(mag_unc * 10 + 0.5)), 99):02d}"
            if flash_period_s:
                ms_ = int(math.floor(flash_period_s * 1000 + 0.5))
                line += f" {min(ms_, 999999):06d}"
    return line.rstrip()


# --------------------------------------------------------------------------- #
# Reporting helpers: clock, uncertainties, point selection
# --------------------------------------------------------------------------- #

# IOD col 22 sky condition codes (G. Lewis, IOD Version 0).
SKY_CODES = {"E": "excellent", "G": "good", "F": "fair", "P": "poor", "B": "bad",
             "T": "terrible"}

TIME_UNC_NTP_S = 0.05
TIME_UNC_PHONE_S = 0.2
TIME_UNC_SKYSAFARI_S = 1.0
TIME_UNC_UNKNOWN_S = 10.0
COLOUR_TERM_MAG = 0.15        # sunlit satellite vs catalog-star colours


def clock_uncertainty(session):
    """(time uncertainty s, description, fit to post) from the capture's
    clock record (session.json ``clock``, from diofinder/clock_info.py)."""
    clock = (session or {}).get("clock") or {}
    last = clock.get("last_sync") or {}
    if clock.get("ntp_synchronized"):
        return TIME_UNC_NTP_S, "network time (synchronized)", True
    if clock.get("trusted") and clock.get("this_boot"):
        src = last.get("source")
        if src == "phone":
            return TIME_UNC_PHONE_S, "set from a phone this session", True
        if src == "skysafari":
            return TIME_UNC_SKYSAFARI_S, "set by SkySafari this session (whole seconds)", True
    if clock:
        return (TIME_UNC_UNKNOWN_S, "NOT SET this session: " + str(clock.get("summary", "")),
                False)
    return (TIME_UNC_UNKNOWN_S, "unknown (capture predates clock recording"
            + ("" if session.get("clock_plausible") else "; clock looked implausible") + ")",
            False)


def arc_residuals(obs, window=8):
    """Each observation's distance (deg) from a smooth local fit of its
    neighbours (great circle, smoothly changing rate): the measurement
    scatter, whatever the arc's length."""
    if len(obs) < 4:
        return [0.0] * len(obs)
    v = radec_to_vec([o["ra"] for o in obs], [o["dec"] for o in obs])
    t = np.array([o["jd"] for o in obs]) * SECONDS_PER_DAY
    out = []
    for j in range(len(obs)):
        lo, hi = max(0, j - window // 2), min(len(obs), j + window // 2 + 1)
        idx = [i for i in range(lo, hi) if i != j]
        if len(idx) < 3:
            out.append(0.0)
            continue
        f, _ = tr.local_model([(v[i], t[i]) for i in idx], t[j])
        out.append(float(_ang(f(0.0), v[j])))
    return out


def select_points(obs, n):
    """Indices of up to n observations spread evenly in time, preferring a
    dash lying wholly inside the frame and, among those, the smallest
    residual. n <= 0: all."""
    if n <= 0 or len(obs) <= n:
        return list(range(len(obs)))
    jd = np.array([o["jd"] for o in obs])
    edges = np.linspace(jd[0], jd[-1], n + 1)
    big = 2 * _med_resid(obs)
    picks = []
    for b in range(n):
        lo, hi = edges[b], edges[b + 1]
        idx = [i for i in range(len(obs))
               if (lo <= jd[i] < hi or (b == n - 1 and jd[i] == hi)) and i not in picks]
        if not idx:
            continue
        mid = 0.5 * (lo + hi)
        idx.sort(key=lambda i: (not obs[i].get("full", True),
                                abs(obs[i].get("resid_deg") or 0.0) > big,
                                abs(jd[i] - mid)))
        picks.append(idx[0])
    return sorted(picks)


def _med_resid(obs):
    r = [abs(o.get("resid_deg") or 0.0) for o in obs]
    return float(np.median(r)) if r else 0.0


def unknown_name(jd):
    return "unk" + so.jd_to_utc(jd).strftime("%y%m%d")


# --------------------------------------------------------------------------- #
# Photometry over a capture
# --------------------------------------------------------------------------- #

class CapturePhotometry:
    """Linearizer + per-frame zero points from matched stars."""

    def __init__(self, zf, res, log=print):
        self.width, self.height = res["size"]
        self.tone, self.tone_source = ph.tone_for_session(res["session"], log)
        self.lin = ph.Linearizer(self.tone)
        stars = {}
        for r in res["results"]:
            m = r["rec"].get("matched")
            if not m or r.get("wcs_source") != "fit":
                continue
            raw = so._load_frame(zf, r["rec"]["file"])
            stars[r["rec"]["file"]] = ph.star_measurements(self.lin(raw), raw, m)
        self.cal = ph.calibrate(stars, self.width, self.height)
        self.exposure = {r["rec"]["file"]: float(r["rec"].get("exposure_s") or 0.0)
                         for r in res["results"]}

    def lin_values(self, v8):
        """Linear light for (possibly fractional) 8-bit values (medians)."""
        return np.interp(v8, np.arange(256), self.lin.lut)

    def zp(self, fname):
        return self.cal["zp"].get(fname)

    def mag(self, fname, flux, flux_err, x, y):
        zp = self.zp(fname)
        if zp is None or flux is None or flux <= 0:
            return None, None
        m = ph.magnitude(zp, self.cal["k"], flux, x, y, self.width, self.height)
        rms = self.cal["per_frame_rms"].get(fname) or self.cal["rms"] or 0.1
        return m, math.hypot(1.0857 * flux_err / flux, rms)

    def cflux(self, fname, flux, x, y):
        zp = self.zp(fname)
        if zp is None or flux is None:
            return None
        return ph.calibrated(flux, zp, self.cal["k"], x, y, self.width, self.height)


def _frame_entry(cp, fname, m, exp):
    """Per-frame photometry record from a measure_trail/segment_flux dict."""
    mag, err = cp.mag(fname, m["flux"], m["flux_err"], m["x"], m["y"])
    scale = cp.cflux(fname, 1.0, m["x"], m["y"])
    e = {"mag": mag, "mag_err": err, "full": bool(m["full"]),
         "saturated": m["saturated_px"] > 0, "masked_frac": m.get("masked_frac", 0.0),
         "flux": m["flux"], "cflux": cp.cflux(fname, m["flux"], m["x"], m["y"]),
         "lc_jd": None, "lc_c": None, "lc_noise": None, "speed_px_s": m.get("speed_px_s")}
    if scale is not None and len(m.get("lc_jd", [])):
        e.update(lc_jd=np.asarray(m["lc_jd"]), lc_c=np.asarray(m["lc_rate"]) * exp * scale,
                 lc_noise=m["noise_rate"] * exp * scale)
    return e


def stack_trail_photometry(zf, st, t, cp):
    """Per frame of a stack trail: magnitude, full-in-frame, saturation and
    within-dash light-curve samples. Returns {file: {...}}."""
    if not t.get("track_obs") or len(t["track_obs"]) < 2:
        return {}
    width, height = cp.width, cp.height
    s0, v, jd0 = ph.fit_motion(t["track_obs"])
    p0 = np.array(t["p0"], float)
    u_ = np.array(t["u"], float)
    med_lin = cp.lin_values(st["median"])
    by_file = {r["rec"]["file"]: r for r in st["members"]}
    out = {}
    for fname in t.get("track_files") or []:
        r = by_file.get(fname)
        if r is None:
            continue
        raw = so._load_frame(zf, fname)
        cx, cy = so.ref_to_frame_map(st["ref"]["wcs"], r["wcs"], width, height)
        ref_end = np.array([p0 + u_ * (s0 + v * (jd - jd0)) for jd in r["jd"]])
        terms = so._poly_terms(ref_end[:, 0], ref_end[:, 1], width, height)
        fa, fb = np.column_stack([terms @ cx, terms @ cy])
        # Star-subtracted residual in a box around the dash (the steady sky
        # = the stack median, mapped from the reference grid into this frame).
        ix, iy = so.ref_to_frame_map(r["wcs"], st["ref"]["wcs"], width, height)
        m_ = 14
        x0, x1 = int(max(0, min(fa[0], fb[0]) - m_)), int(min(width, max(fa[0], fb[0]) + m_ + 1))
        y0, y1 = int(max(0, min(fa[1], fb[1]) - m_)), int(min(height, max(fa[1], fb[1]) + m_ + 1))
        if x1 - x0 < 3 or y1 - y0 < 3:
            continue
        yy, xx = np.mgrid[y0:y1, x0:x1]
        tb = so._poly_terms(xx.ravel().astype(float), yy.ravel().astype(float), width, height)
        rx = np.rint(tb @ ix).astype(int)
        ry = np.rint(tb @ iy).astype(int)
        inside = (rx >= 0) & (rx < width) & (ry >= 0) & (ry < height)
        sky = np.where(inside, med_lin[np.clip(ry, 0, height - 1), np.clip(rx, 0, width - 1)], 0)
        resid = np.zeros((height, width))
        ok = np.zeros((height, width), bool)
        resid[y0:y1, x0:x1] = cp.lin(raw[y0:y1, x0:x1]) - sky.reshape(yy.shape)
        ok[y0:y1, x0:x1] = inside.reshape(yy.shape)
        m = ph.measure_trail(resid, raw, fa, fb, r["jd"][0], r["jd"][1], ok)
        if m is None:
            continue
        exp = cp.exposure.get(fname) or (r["jd"][1] - r["jd"][0]) * SECONDS_PER_DAY
        out[fname] = _frame_entry(cp, fname, m, exp)
    return out


class LinkedPhotometry:
    """on_frame hook for sat_tracks.extract: photometry of every residual
    blob while its frame's residual is at hand."""

    def __init__(self, cp, zf):
        self.cp, self.zf = cp, zf

    def __call__(self, k, r, resid, med, ok, blobs):
        if not blobs:
            return
        cp = self.cp
        raw = so._load_frame(self.zf, r["rec"]["file"])
        lin_res = np.where(ok, cp.lin(raw) - cp.lin_values(med), 0.0)
        v = lin_res[ok]
        noise = 1.4826 * float(np.median(np.abs(v - np.median(v)))) if v.size else 1.0
        noise = noise or 1.0
        fname = r["rec"]["file"]
        exp = cp.exposure.get(fname) or (r["jd"][1] - r["jd"][0]) * SECONDS_PER_DAY
        h, w = raw.shape
        for b in blobs:
            th = math.radians(b["angle"])
            half = max(0.0, (b["length"] - 3.0) / 2.0)        # path, PSF removed
            a = (b["x"] - half * math.cos(th), b["y"] - half * math.sin(th))
            c = (b["x"] + half * math.cos(th), b["y"] + half * math.sin(th))
            f = ph.segment_flux(lin_res, ok, raw, a, c, noise)
            if f is None:
                continue
            f.update(x=b["x"], y=b["y"],
                     full=(min(a[0], c[0]) >= 6 and min(a[1], c[1]) >= 6
                           and max(a[0], c[0]) < w - 6 and max(a[1], c[1]) < h - 6))
            e = _frame_entry(cp, fname, f, exp)
            if 2 * half >= 10:
                d = ph.measure_trail(lin_res, raw, a, c, r["jd"][0], r["jd"][1], ok)
                if d is not None:
                    de = _frame_entry(cp, fname, d, exp)
                    e.update(lc_jd=de["lc_jd"], lc_c=de["lc_c"], lc_noise=de["lc_noise"],
                             speed_px_s=de["speed_px_s"])
            e["noise"] = noise
            e["dash_axis"] = (math.cos(th), math.sin(th))
            e["jd_window"] = tuple(r["jd"])
            b["phot"] = e


def linked_light_curve(track, res):
    """Within-dash samples of a linked track's long dashes. A dash's
    direction in time is not known from one frame; the track's motion on the
    sky (projected into that frame) decides it."""
    jd, c, nz, sp = [], [], [], []
    pts = sorted(track["points"], key=lambda b: b["jd"])
    by_file = {r["rec"]["file"]: r for r in res["results"]}
    for j, b in enumerate(pts):
        e, r = b.get("phot"), by_file.get(b["file"])
        if not e or e.get("lc_jd") is None or r is None or len(pts) < 2:
            continue
        a, z = pts[max(0, j - 1)], pts[min(len(pts) - 1, j + 1)]
        (xa, xz), (ya, yz) = r["wcs"].world_to_pixel_values(
            np.array([a["ra"], z["ra"]]), np.array([a["dec"], z["dec"]]))
        ux, uy = e["dash_axis"]
        lj = np.asarray(e["lc_jd"])
        if (xz - xa) * ux + (yz - ya) * uy < 0:            # moved the other way
            j0, j1 = e["jd_window"]
            lj = (j0 + j1) - lj
        jd += list(lj)
        c += list(e["lc_c"])
        nz.append(e["lc_noise"])
        if e.get("speed_px_s"):
            sp.append(e["speed_px_s"])
    return _lc_tuple(jd, c, nz, sp)


def _lc_tuple(jd, c, nz, sp):
    """(jd, cflux, noise, shortest resolvable period s) of along-dash samples.
    Flashes shorter than ~4 px of travel along the dash are not resolved
    (the PSF), and the pixel grid alone can fake a period of 1-2 px."""
    order = np.argsort(jd) if len(jd) else []
    jd, c = np.asarray(jd)[order] if len(jd) else np.array([]), \
        np.asarray(c)[order] if len(c) else np.array([])
    min_p = max(0.15, 4.0 / float(np.median(sp))) if sp else 0.15
    return jd, c, (float(np.median(nz)) if nz else None), min_p


def summarize_photometry(r, cadence_s):
    """Track magnitude, light curve and behaviour from the per-observation
    photometry in r["obs"] (o["phot"]) and r["lc"] (within-dash samples)."""
    phot = [o.get("phot") for o in r["obs"]]
    good = [p for p in phot if p and p.get("mag") is not None]
    clean = [p for p in good if p["full"] and not p["saturated"] and p["masked_frac"] < 0.2]
    use = clean or [p for p in good if not p["saturated"]]
    out = {"n_frames": len(good), "n_clean": len(clean), "saturated_frames":
           sum(1 for p in good if p["saturated"])}
    if use:
        mags = np.array([p["mag"] for p in use])
        med = float(np.median(mags))
        scat = 1.4826 * float(np.median(np.abs(mags - med))) if len(mags) > 1 else 0.0
        stat = (scat / math.sqrt(len(mags))) if len(mags) > 1 else float(use[0]["mag_err"])
        zp_err = float(np.median([p["mag_err"] for p in use]))
        out.update(mag=round(med, 2), mag_err=round(math.sqrt(stat ** 2 + COLOUR_TERM_MAG ** 2
                                                                + (zp_err / 2) ** 2), 2),
                   frame_scatter_mag=round(scat, 2))
    elif good:
        out.update(mag=round(float(np.median([p["mag"] for p in good])), 2), mag_err=None,
                   bright_limit=True)
    # Light curve: within-dash samples when there are enough, else per frame.
    lc_jd, lc_c, lc_n, lc_min = r.get("lc") or (np.array([]), np.array([]), None, 0.15)
    if len(lc_jd) >= 20 and lc_n:
        lc = ph.light_curve_summary(lc_jd, lc_c, lc_n, min_period_s=lc_min)
        lc["sampling"] = "along the dashes"
    else:
        pts = [(o["jd"], o["phot"]["cflux"], o["phot"]) for o in r["obs"]
               if o.get("phot") and o["phot"].get("cflux") is not None
               and not o["phot"]["saturated"]]
        if pts:
            cs = np.array([p[1] for p in pts])
            nz = float(np.median([abs(p[1]) * (p[2]["mag_err"] or 0.1) / 1.0857 for p in pts]))
            lc = ph.light_curve_summary([p[0] for p in pts], cs, nz,
                                        min_period_s=max(0.15, 2.0 * cadence_s))
        else:
            lc = ph.light_curve_summary([], [], 1.0)
        lc["sampling"] = "one value per frame"
    if lc["behaviour"] is None and out.get("frame_scatter_mag") is not None \
            and out["n_clean"] >= 3:
        lc["behaviour"] = "S" if out["frame_scatter_mag"] < 0.3 else \
            ("I" if out["frame_scatter_mag"] > 0.75 else "S")
    out["light_curve"] = lc
    return out


# --------------------------------------------------------------------------- #
# R.D.E. format (Russell D. Eberst; satobs.org/position/RDEformat.html)
# --------------------------------------------------------------------------- #

# COSPAR piece letters skip I and O; R.D.E. numbers them A = 01, B = 02, ...
_PIECE_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"


def rde_desig(intldesg):
    """'04012C' -> '0401203'; unknown or unrepresentable -> '9900000'."""
    if not intldesg or len(intldesg) < 6 or not intldesg[:5].isdigit():
        return "9900000"
    piece = intldesg[5:].strip().upper()
    n = 0
    for ch in piece:
        if ch not in _PIECE_LETTERS:
            return "9900000"
        n = n * len(_PIECE_LETTERS) + _PIECE_LETTERS.index(ch) + 1
    return intldesg[:5] + f"{n:02d}" if 1 <= n <= 99 else "9900000"


def _rde_mag(m):
    if m is None:
        return "    "
    m = max(-9.9, min(99.9, m))
    return f"{m:4.1f}" if m < 0 or m >= 10 else f" {m:3.1f}"


def _rde_period(p):
    """SSS.sss: leading zero below 1, no trailing zeros, no point for whole
    numbers; '0' when there is no flash period."""
    if not p:
        return "0"
    s = f"{p:.3f}".rstrip("0").rstrip(".")
    return s


def rde_line(ob, intldesg, mag_bright=None, mag_faint=None, period_s=None, remark="S"):
    t = so.jd_to_utc(ob["jd"])
    cs = int(round(t.microsecond / 1e4))
    if cs == 100:
        t, cs = t + dt.timedelta(seconds=1), 0
    ra_s = int(round((ob["ra"] % 360.0) / 15.0 * 3600.0)) % 86400
    dec_s = int(round(abs(ob["dec"]) * 3600.0))
    sign = "-" if ob["dec"] < 0 else "+"
    radec = (f"{ra_s // 3600:02d}{ra_s // 60 % 60:02d}{ra_s % 60:02d}"
             f"{sign}{dec_s // 3600:02d}{dec_s // 60 % 60:02d}{dec_s % 60:02d}")
    if mag_faint is None:
        mag_faint = mag_bright
    return (f"{rde_desig(intldesg)} {t.strftime('%H%M%S')}.{cs:02d} {radec}"
            f"{_rde_mag(mag_bright)}{_rde_mag(mag_faint)} {_rde_period(period_s)} "
            f"{remark or ' '}").rstrip()


def rde_report(results, station, t_unc, time_standard="1"):
    """Header line (site, YYMM, time accuracy, time standard, position format
    1, position accuracy in arcsec, epoch 5 = 2000), a day line whenever the
    day changes, then one line per reported observation."""
    rows = []
    for r in results:
        m = r.get("match")
        intl = m["element_set"].intldesg if m else ""
        p = r.get("photometry") or {}
        lc = p.get("light_curve") or {}
        beh = lc.get("behaviour") or "S"
        bright = faint = None
        if p.get("mag") is not None and not p.get("bright_limit"):
            amp = 0.0 if beh == "S" else (lc.get("amplitude_mag") or 0.0)
            bright, faint = p["mag"] - amp / 2, p["mag"] + amp / 2
        period = lc.get("period_s") if beh in ("F", "R") else None
        pos_as = max(1, int(math.ceil(r["sigma_pos_deg"] * 3600.0)))
        for i in r.get("selected") or []:
            ob = r["obs"][i]
            rows.append((ob["jd"], pos_as, rde_line(ob, intl, bright, faint, period, beh)))
    if not rows:
        return ""
    rows.sort()
    out, cur_month, cur_day = [], None, None
    for jd, pos_as, line in rows:
        t = so.jd_to_utc(jd)
        if t.strftime("%y%m") != cur_month:
            cur_month, cur_day = t.strftime("%y%m"), None
            acc = max(pos_as for _, pos_as, _ in rows)
            if out:
                out.append("999")
            out.append(f"{str(station)[:4]:>4} {cur_month} "
                       f"{min(max(t_unc, 0.1), 9.9):3.1f}{str(time_standard)[:1]}1 "
                       f"{min(acc, 999):03d}5")
        if t.strftime("%d") != cur_day:
            cur_day = t.strftime("%d")
            out.append(cur_day)
        out.append(line)
    out.append("999")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

def _utc(jd):
    return so.utc_str(jd)


def _cadence_s(res):
    t = sorted(0.5 * (r["jd"][0] + r["jd"][1]) for r in res["results"] if r["jd"][0])
    return float(np.median(np.diff(t)) * SECONDS_PER_DAY) if len(t) > 2 else 1.0


def run(archive, elems, out, window_s=600.0, responses=None, fetch=None,
        max_offset_s=600.0, max_cross_deg=1.0, readout_ms=0.0, station="9999",
        show_unlit=False, log=print, link=True, link_include=("fit",), status="G",
        points_per_pass=4, time_unc_s=None, unknown_desig="99099A", photometry=True,
        iod_photometry=True, report=None):
    """Everything; returns the per-track results (also written to ``out``).
    ``report``: optional {"conditions", "equipment", "time_standard"} for the
    message draft and the R.D.E. header."""
    os.makedirs(out, exist_ok=True)
    empty = {"data": {"satellites": {}}}
    res = so.analyse(archive, None, window_s, responses or {},
                     fetch=fetch or (lambda p: empty), sleep_s=0, show_unlit=show_unlit)
    session = res["session"]
    site = session.get("observer") or {}
    t_unc, clock_note, postable = clock_uncertainty(session)
    if time_unc_s is not None:
        t_unc, clock_note = float(time_unc_s), f"{clock_note}; overridden to {time_unc_s} s"
    if not postable:
        log(f"WARNING: clock {clock_note}. Times may be seconds off: do not post these "
            "observations unless you know the clock offset (--time-unc).")
    width, height = res["size"]
    cam = session.get("camera") or {}
    plate_deg = float(cam.get("fov_deg") or 13.54) / width
    wcs_rms = {r["rec"]["file"]: r.get("wcs_rms_px") for r in res["results"]}
    results = []
    with zipfile.ZipFile(archive) as zf:
        cp = CapturePhotometry(zf, res, log) if photometry else None
        if cp is not None:
            c = cp.cal
            log(f"photometry: tone curve {cp.tone_source}; {c['n']} star measurements "
                f"in {len(c['zp'])} frames, rms {c['rms'] if c['rms'] is None else round(c['rms'], 3)}"
                f" mag, vignetting {c['k']:+.2f} mag at the corners")
        # Trails in the stacks (finder held still).
        for gi, g in enumerate(res["groups"]):
            st = so.stack_group(zf, g, res)
            if st is None:
                continue
            trails = so.trail_kinematics(zf, st, res, so.detect_trails(st["moving"]))
            so.match_trails(trails, st, g, res)
            for ti, t in enumerate(trails):
                obs = trail_observations(t, st, readout_ms, height)
                if len(obs) < 3:
                    continue
                ph_by = stack_trail_photometry(zf, st, t, cp) if cp is not None else {}
                for o in obs:
                    p = ph_by.get(o["file"])
                    o["phot"] = p
                    o["full"] = bool(p["full"]) if p else True
                js, cs, ns, sp = [], [], [], []
                for p in ph_by.values():
                    if p.get("lc_c") is not None and not p["saturated"]:
                        js += list(p["lc_jd"])
                        cs += list(p["lc_c"])
                        ns.append(p["lc_noise"])
                        if p.get("speed_px_s"):
                            sp.append(p["speed_px_s"])
                lc = _lc_tuple(js, cs, ns, sp)
                results.append({"id": f"S{gi + 1}.{ti + 1}", "kind": "stack",
                                "group": gi + 1, "trail": ti + 1, "trail_obj": t,
                                "satchecker": t.get("satellite"), "obs": obs, "lc": lc,
                                "label": so.trail_label(t)})
        # Frame-by-frame tracks (hand-guided scope, long arcs).
        if link:
            log("linking frame-by-frame residuals (hand-tracked or long arcs)...")
            hook = LinkedPhotometry(cp, zf) if cp is not None else None
            rs, sources = tr.extract(zf, res, include=link_include, log=log, on_frame=hook)
            times = [0.5 * (r["jd"][0] + r["jd"][1]) for r in rs]
            tracks = tr.link(sources, times)
            n_linked = 0
            for track in tracks:
                obs = tr.track_observations(track, readout_ms, height)
                dup = [r for r in results if r["kind"] == "stack"
                       and tr.same_object(obs, r["obs"]) >= 0.5]
                if dup and len(obs) <= sum(len(r["obs"]) for r in dup):
                    continue                     # the stack trail(s) already have it
                for r in dup:
                    results.remove(r)
                n_linked += 1
                results.append({"id": f"L{n_linked}", "kind": "linked", "group": None,
                                "trail": None, "trail_obj": None,
                                "satchecker": (dup[0]["satchecker"] if dup else None),
                                "obs": obs, "track": track,
                                "lc": linked_light_curve(track, res) if cp is not None
                                else None,
                                "label": (dup[0]["label"] if dup else "unidentified"),
                                "replaces": [r["id"] for r in dup]})
            log(f"  {len(tracks)} linked track(s), {n_linked} new or longer than the "
                "stack trails")
    jds = [o["jd"] for r in results for o in r["obs"]]
    if not jds:
        log("no timed trails or tracks found (need an object seen in 3 or more frames)")
        _write_outputs(results, [], out, archive, session, station, status, t_unc,
                       clock_note, postable, points_per_pass, unknown_desig, iod_photometry,
                       log, report)
        return results
    cadence = _cadence_s(res)
    for r in results:
        r["obs"].sort(key=lambda o: o["jd"])
        for o, rr in zip(r["obs"], arc_residuals(r["obs"])):
            o["resid_deg"] = rr
        scatter = float(np.sqrt(np.mean([o["resid_deg"] ** 2 for o in r["obs"]])))
        wr = [wcs_rms.get(o.get("file")) for o in r["obs"]]
        wr = [x for x in wr if x is not None]
        w_deg = (float(np.median(wr)) * plate_deg) if wr else plate_deg
        r["sigma_pos_deg"] = math.hypot(scatter, w_deg)
        r["photometry"] = summarize_photometry(r, cadence) if photometry else None
    geom = Geometry(site, min(jds) - max_offset_s / SECONDS_PER_DAY - 1e-3,
                    max(jds) + max_offset_s / SECONDS_PER_DAY + 1e-3)
    refined = []
    for r in results:
        r["candidates"] = identify(r["obs"], elems, geom, max_offset_s=max_offset_s,
                                   max_cross_deg=max_cross_deg) if elems else []
        best = r["candidates"][0] if r["candidates"] else None
        r["match"] = best if best is not None and is_match(best) else None
        if r["match"] is not None:
            r["refined"] = refine(r["match"], r["obs"], geom)
            refined.append((r, r["refined"]))
    _write_outputs(results, refined, out, archive, session, station, status, t_unc,
                   clock_note, postable, points_per_pass, unknown_desig, iod_photometry, log,
                   report)
    return results


def _iod_for(r, station, status, t_unc, points_per_pass, unknown_desig, iod_photometry):
    """(selected indices, IOD lines) for one track."""
    m = r.get("match")
    norad = m["norad_str"] if m else "99999"
    intl = m["element_set"].intldesg if m else unknown_desig
    p = r.get("photometry") or {}
    lc = p.get("light_curve") or {}
    beh = lc.get("behaviour") or " "
    period = lc.get("period_s") if beh in ("F", "R") else None
    sel = select_points(r["obs"], points_per_pass)
    lines = []
    for i in sel:
        ob = r["obs"][i]
        mag = mag_unc = None
        if iod_photometry and p.get("mag") is not None and not p.get("bright_limit"):
            fp = ob.get("phot") or {}
            ok = fp.get("mag") is not None and fp.get("full") and not fp.get("saturated")
            mag = fp["mag"] if ok else p["mag"]
            mag_unc = math.hypot(fp["mag_err"], COLOUR_TERM_MAG) if ok else p.get("mag_err")
        lines.append(iod_line(ob, norad, intl, station, status, t_unc,
                              max(r["sigma_pos_deg"], 0.001), beh, mag, mag_unc, period))
    return sel, lines


def _write_lightcurve(r, out):
    """lightcurve_<id>.csv and .png for one track; returns the csv name."""
    rows = []
    t0 = r["obs"][0]["jd"]
    for o in r["obs"]:
        p = o.get("phot") or {}
        if p.get("mag") is not None:
            rows.append(("frame", o["jd"], p["mag"], p["mag_err"], o.get("file"),
                         p.get("full"), p.get("saturated")))
    lc_jd, lc_c = (r.get("lc") or (np.array([]), np.array([])))[:2]
    for j, c in zip(lc_jd, lc_c):
        if c is not None and c > 0:
            rows.append(("dash", float(j), -2.5 * math.log10(c), None, None, None, None))
    if not rows:
        return None
    rows.sort(key=lambda x: x[1])
    name = f"lightcurve_{r['id']}.csv"
    with open(os.path.join(out, name), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["kind", "utc", "jd", "seconds", "mag", "mag_err", "frame", "full",
                    "saturated"])
        for kind, jd, mag, err, fname, full, sat in rows:
            w.writerow([kind, _utc(jd), f"{jd:.8f}", f"{(jd - t0) * SECONDS_PER_DAY:.3f}",
                        f"{mag:.3f}", "" if err is None else f"{err:.3f}", fname or "",
                        "" if full is None else int(bool(full)),
                        "" if sat is None else int(bool(sat))])
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(8, 3.2), dpi=100)
        d = [x for x in rows if x[0] == "dash"]
        if d:
            ax.plot([(x[1] - t0) * SECONDS_PER_DAY for x in d], [x[2] for x in d], ".",
                    ms=2, color="0.6", label="along the dashes")
        fr = [x for x in rows if x[0] == "frame"]
        if fr:
            ax.errorbar([(x[1] - t0) * SECONDS_PER_DAY for x in fr], [x[2] for x in fr],
                        yerr=[x[3] or 0 for x in fr], fmt="o", ms=4, color="C3",
                        label="per frame")
        ax.invert_yaxis()
        ax.set_xlabel(f"seconds after {_utc(t0)}")
        ax.set_ylabel("magnitude")
        lc = (r.get("photometry") or {}).get("light_curve") or {}
        ttl = f"{r['id']} {r.get('label') or ''}"
        if lc.get("period_s"):
            ttl += f"  flash period {lc['period_s']:.2f} s"
        elif lc.get("behaviour"):
            ttl += f"  behaviour {lc['behaviour']}"
        ax.set_title(ttl, fontsize=9)
        ax.legend(fontsize=7)
        fig.tight_layout()
        fig.savefig(os.path.join(out, name.replace(".csv", ".png")))
        plt.close(fig)
    except Exception:                       # plotting is a convenience
        pass
    return name


def _write_outputs(results, refined, out, archive, session, station, status, t_unc,
                   clock_note, postable, points_per_pass, unknown_desig, iod_photometry,
                   log, report=None):
    report = report or {}
    site = session.get("observer") or {}
    all_iod, post = [], []
    tracks_json = []
    for r in results:
        sel, lines = _iod_for(r, station, status, t_unc, points_per_pass, unknown_desig,
                              iod_photometry)
        r["iod"] = lines
        r["selected"] = sel
        all_iod += lines
        lc_file = _write_lightcurve(r, out) if r.get("photometry") else None
        m = r.get("match")
        tracks_json.append({
            "id": r["id"], "kind": r["kind"], "label": r["label"],
            "satchecker": r["satchecker"], "replaces": r.get("replaces"),
            "identified": ({"norad": m["norad"], "name": m["name"],
                            "intldesg": m["element_set"].intldesg, "source": m["source"]}
                           if m else None),
            "sigma_pos_deg": r["sigma_pos_deg"],
            "photometry": r.get("photometry"), "light_curve_file": lc_file,
            "observations": [
                {"jd": o["jd"], "utc": _utc(o["jd"]), "ra": o["ra"], "dec": o["dec"],
                 "x": o.get("x"), "y": o.get("y"), "file": o.get("file"),
                 "resid_deg": o.get("resid_deg"), "sigma_deg": r["sigma_pos_deg"],
                 "full": o.get("full"), "reported": i in sel,
                 "mag": (o.get("phot") or {}).get("mag"),
                 "mag_err": (o.get("phot") or {}).get("mag_err")}
                for i, o in enumerate(r["obs"])]})
    with open(os.path.join(out, "observations_iod.txt"), "w") as f:
        f.write("".join(ln + "\n" for ln in all_iod))
    with open(os.path.join(out, "observations_rde.txt"), "w") as f:
        f.write(rde_report(results, station, t_unc, report.get("time_standard", "1")))
    with open(os.path.join(out, "observations.json"), "w") as f:
        json.dump({"format": "diofinder-sat-observations/1",
                   "capture": os.path.basename(archive), "site": site,
                   "station": station, "status": status,
                   "clock": {"time_unc_s": t_unc, "note": clock_note, "postable": postable},
                   "tracks": tracks_json}, f, indent=1, default=_json_default)
    # Identification report.
    rep, lines = [], []
    for r in results:
        p = r.get("photometry") or {}
        lc = p.get("light_curve") or {}
        head = (f"track {r['id']} ({r['kind']}): {len(r['obs'])} frames"
                + (f", {_utc(r['obs'][0]['jd'])}" if r["obs"] else "")
                + f"; SatChecker: {r['satchecker'] or 'no match'}"
                + f"; position sigma {r['sigma_pos_deg']:.3f} deg")
        lines.append(head)
        if not r.get("match") and _looks_like_aircraft(r):
            lines.append("  CHECK: may be an AIRCRAFT (flashing lights, no satellite "
                         "match); a tumbling unknown satellite can look the same")
        if p.get("mag") is not None:
            lines.append(f"  magnitude {p['mag']:.1f}"
                         + (f" +- {p['mag_err']:.1f}" if p.get("mag_err") else
                            " (saturated: brighter than this)")
                         + f" ({p['n_clean']} clean frames"
                         + (f", frame-to-frame scatter {p['frame_scatter_mag']:.2f}"
                            if p.get("frame_scatter_mag") is not None else "") + ")"
                         + (f"; light curve {lc.get('sampling')}: behaviour "
                            f"{lc.get('behaviour') or '?'}"
                            + (f", flash period {lc['period_s']:.2f} s" if lc.get("period_s")
                               else "")
                            + (f", amplitude {lc['amplitude_mag']:.1f} mag"
                               if lc.get("amplitude_mag") is not None else "")))
        cands = []
        for c in r.get("candidates", []):
            ok = "MATCH" if c is r.get("match") else "     "
            lines.append(
                f"  {ok} {c['name']:<24} {c['norad_str']:>5}  {c['source']}  "
                f"elements {c['epoch_age_days']:+.1f} d old, {c['offset_s']:+.1f} s "
                f"{'early' if c['offset_s'] > 0 else 'late'}, rms {c['rms_deg']:.3f} deg, "
                f"cross {c['cross_deg']:.3f} deg, rate x{c['rate_ratio']:.2f}, "
                f"direction {c['dir_deg']:.0f} deg, {'sunlit' if c['lit'] else 'IN SHADOW'}")
            cands.append({k: v for k, v in c.items() if k != "element_set"})
        if not r.get("candidates"):
            lines.append("  no candidate within the limits")
        if r.get("refined"):
            rf = r["refined"]
            lines.append(f"  refined: rms {rf['rms_before_deg']:.3f} -> "
                         f"{rf['rms_after_deg']:.3f} deg at epoch {_utc(rf['epoch_jd'])}")
        rep.append({"id": r["id"], "group": r["group"], "trail": r["trail"],
                    "label": r["label"], "satchecker": r["satchecker"], "candidates": cands,
                    "photometry": r.get("photometry"),
                    "refined": ({k: v for k, v in r["refined"].items() if k != "satrec"}
                                if r.get("refined") else None)})
    with open(os.path.join(out, "identification.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(out, "identification.json"), "w") as f:
        json.dump(rep, f, indent=1, default=_json_default)
    for ln in lines:
        log(ln)
    # Refined element sets, every format.
    tle_recs, omm_recs = [], []
    for r, rf in refined:
        m = r["match"]
        note = (f"diofinder fit of {m['source']} element set to track "
                f"{r['id']} ({len(r['obs'])} frames, rms "
                f"{rf['rms_after_deg']:.3f} deg); valid near {_utc(rf['epoch_jd'])}")
        tle_recs.append(((rf["satrec"], m["name"], m["norad"]), note))
        omm_recs.append((omm_record(rf["satrec"], m["name"], m["norad"]), note))
    if refined:
        write_tle(tle_recs, os.path.join(out, "refined.tle"))
        write_omm_kvn(omm_recs, os.path.join(out, "refined_omm.kvn"))
        write_omm_xml(omm_recs, os.path.join(out, "refined_omm.xml"))
        write_omm_json(omm_recs, os.path.join(out, "refined_omm.json"))
        write_omm_csv(omm_recs, os.path.join(out, "refined_omm.csv"))
        log(f"wrote refined elements for {len(refined)} track(s) (TLE, OMM KVN/XML/JSON/CSV)")
    # A SeeSat-L style message draft.
    post.append(f"Station {station}: {_site_text(site)}")
    post.append("Equipment: " + (report.get("equipment") or (
        "diofinder plate-solving finder (Raspberry Pi HQ camera IMX477, "
        f"~{(session.get('camera') or {}).get('fov_deg') or 13.5:.1f} deg field), "
        "positions from per-frame plate solutions (J2000)")))
    post.append(f"Conditions: {status} ({SKY_CODES.get(status, '?')})"
                + (f"; {report['conditions']}" if report.get("conditions") else ""))
    post.append(f"Times: {clock_note}; time uncertainty {t_unc:g} s")
    if not postable:
        post.append("*** CLOCK NOT VERIFIED: check the times before posting ***")
    post.append("")
    for r in results:
        if not r.get("iod"):
            continue
        m = r.get("match")
        if not m and _looks_like_aircraft(r):
            post.append(f"*** CHECK track {r['id']}: flashing like an aircraft's lights; "
                        "delete it if that is what it was ***")
        post += r["iod"]
        if m:
            post.append(f"{m['name']} ({m['norad_str']}), identified against {m['source']}")
        else:
            post.append(f"{unknown_name(r['obs'][0]['jd'])} (unidentified)")
        if r.get("refined"):
            l12 = tle_lines(r["refined"]["satrec"], m["norad"])
            if l12:
                post += list(l12)
        post.append("")
    with open(os.path.join(out, "seesat_post.txt"), "w") as f:
        f.write("\n".join(post).rstrip() + "\n")


def _looks_like_aircraft(r):
    """Unidentified and flashing: the stack's along-trail flash ratio
    (satcheck_overlay's aircraft hint), or a flash-like light curve with a
    period typical of strobes / beacons (0.5-2 s) and a big amplitude."""
    t = r.get("trail_obj") or {}
    if t.get("flash_ratio") is not None and t["flash_ratio"] >= so.FLASH_RATIO:
        return True
    lc = ((r.get("photometry") or {}).get("light_curve")) or {}
    return bool(lc.get("flashy") and lc.get("period_s") and 0.5 <= lc["period_s"] <= 2.0
                and (lc.get("amplitude_mag") or 0) > 2.0)


def _site_text(site):
    lat, lon = site.get("latitude_deg"), site.get("longitude_deg")
    if lat is None or lon is None:
        return "site not recorded"
    return (f"{abs(lat):.4f} {'N' if lat >= 0 else 'S'}, {abs(lon):.4f} "
            f"{'E' if lon >= 0 else 'W'}, {site.get('elevation_m') or 0:.0f} m")


def _json_default(o):
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.bool_):
        return bool(o)
    return str(o)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("archive")
    ap.add_argument("--elements", nargs="*", default=[],
                    help="element sources: files (TLE/3LE, OMM XML/KVN/JSON/CSV, "
                         ".zip/.gz of them), folders, URLs, or comma-separated lists of "
                         "these; per object the newest epoch wins")
    ap.add_argument("--download", nargs="*", metavar="NAME_OR_URL",
                    help="download element files first (names: " +
                         ", ".join(DOWNLOAD_SOURCES) + "; default both)")
    ap.add_argument("--cache", help="where downloads go (default <out>/elements)")
    ap.add_argument("--out", help="output folder (default <archive>_ident)")
    ap.add_argument("--window", type=float, default=600.0,
                    help="seconds of frames stacked together (600)")
    ap.add_argument("--responses", help="SatChecker responses cache from satcheck_overlay "
                                        "(optional; without it SatChecker is not asked)")
    ap.add_argument("--satchecker", action="store_true",
                    help="also query SatChecker for groups not in --responses")
    ap.add_argument("--max-offset", type=float, default=600.0,
                    help="largest element-set timing error searched, s (600)")
    ap.add_argument("--max-cross", type=float, default=1.0,
                    help="largest cross-track offset accepted, deg (1.0)")
    ap.add_argument("--readout-ms", type=float, default=0.0,
                    help="rolling-shutter readout time of a whole frame, ms (0)")
    ap.add_argument("--station", default="9999",
                    help="your IOD station number (9999 = placeholder)")
    ap.add_argument("--status", default="G", choices=list(SKY_CODES),
                    help="IOD sky condition: " + "; ".join(
                        f"{k} {v}" for k, v in SKY_CODES.items()) + " (G)")
    ap.add_argument("--conditions", default="",
                    help="free-text observing conditions for seesat_post.txt "
                         "(e.g. 'limiting mag 5, light haze')")
    ap.add_argument("--equipment", help="equipment line for seesat_post.txt")
    ap.add_argument("--time-standard", default="1",
                    help="R.D.E. time standard code (1 = radio time signal; also used "
                         "here for network/phone time) (1)")
    ap.add_argument("--points-per-pass", type=int, default=4,
                    help="IOD lines per track (spread over the arc; 0 = every frame) (4)")
    ap.add_argument("--time-unc", type=float,
                    help="time uncertainty, s (default: from the capture's clock record)")
    ap.add_argument("--unknown-desig", default="99099A",
                    help="international designator for unidentified objects, as in the "
                         "SeeSat-L sample '99999 99 099A' (99099A; '' = blank)")
    ap.add_argument("--no-iod-mag", action="store_true",
                    help="leave magnitude and flash period out of the IOD lines")
    ap.add_argument("--no-photometry", action="store_true",
                    help="skip magnitudes and light curves")
    ap.add_argument("--no-link", action="store_true",
                    help="stack trails only (skip frame-by-frame tracks)")
    ap.add_argument("--link-interp", action="store_true",
                    help="frame-by-frame tracks may use unsolved frames' interpolated WCS")
    a = ap.parse_args(argv)
    out = a.out or os.path.splitext(a.archive)[0] + "_ident"
    os.makedirs(out, exist_ok=True)
    paths = list(a.elements)
    if a.download is not None:
        paths += download(a.download or list(DOWNLOAD_SOURCES), a.cache or os.path.join(out, "elements"))
    elems = load_elements(paths) if paths else []
    print(f"{len(elems)} element sets loaded" + (f" from {len(paths)} file(s)" if paths else
                                                   " (none given: only SatChecker matches)"))
    cache = {}
    if a.responses and os.path.exists(a.responses):
        with open(a.responses) as f:
            cache = json.load(f)
    fetch = None
    if a.satchecker:
        import satcheck_archive as sa
        fetch = lambda p: sa.query(sa.DEFAULT_BASE, p)[1]   # noqa: E731
    run(a.archive, elems, out, a.window, cache, fetch, a.max_offset, a.max_cross,
        a.readout_ms, a.station, link=not a.no_link,
        link_include=("fit", "interp") if a.link_interp else ("fit",),
        status=a.status, points_per_pass=a.points_per_pass, time_unc_s=a.time_unc,
        unknown_desig=a.unknown_desig, photometry=not a.no_photometry,
        iod_photometry=not a.no_iod_mag,
        report={"conditions": a.conditions, "equipment": a.equipment,
                "time_standard": a.time_standard})
    print(f"results in {out}")


if __name__ == "__main__":
    main()
