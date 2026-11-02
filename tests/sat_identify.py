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

Outputs (in --out, default ``<archive>_ident/``):
  * ``observations_iod.txt``: every trail's per-frame positions in the IOD
    format used by the SeeSat-L community and read by Find_Orb, sat_id and
    satfit. Unidentified trails get object number 99999. Set --station to
    your station number; the default 9999 is a placeholder.
  * ``observations.json``: the same with full precision and frame names.
  * ``identification.txt`` / ``.json``: per trail, the candidates (name,
    catalog number, source file, element-set age, time offset, rms, cross-
    track offset, rate ratio, direction difference, sunlit or in shadow).
  * Refined elements for each identified trail, in every current format:
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
(Utilities -> Clock) before capturing. The camera reads its rows out over
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


def parse_elements_text(text, source):
    """TLE / 3LE text, OMM CSV, OMM XML or OMM/GP JSON -> [ElementSet]."""
    from sgp4 import omm
    t = text.lstrip()
    if t.startswith("<"):
        return _omm_to_elements(omm.parse_xml(io.StringIO(text)), source)
    if t.startswith("[") or t.startswith("{"):
        data = json.loads(text)
        return _omm_to_elements(data if isinstance(data, list) else [data], source)
    if t.startswith("OBJECT_NAME") or t.startswith('"OBJECT_NAME'):
        return _omm_to_elements(omm.parse_csv(io.StringIO(text)), source)
    return _parse_tle_text(text, source)


def load_elements(paths):
    """Element sets from files (TLE/3LE/OMM; .zip archives are opened and
    every member read). Per catalog number the NEWEST epoch wins."""
    best = {}
    for path in paths:
        base = os.path.basename(path)
        texts = []
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                for n in zf.namelist():
                    if not n.endswith("/"):
                        texts.append((f"{base}:{n}", zf.read(n).decode("utf-8", "replace")))
        else:
            with open(path, encoding="utf-8", errors="replace") as f:
                texts.append((base, f.read()))
        for src, text in texts:
            for e in parse_elements_text(text, src):
                k = e.norad
                if k not in best or e.epoch_jd > best[k].epoch_jd:
                    best[k] = e
    return list(best.values())


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
    out = []
    for jd, s in trail.get("track_obs") or []:
        x, y = p0 + u_ * s
        ra, dec = w.pixel_to_world_values(x, y)
        jd_row = jd + (readout_ms / 1000.0) * (y / height) / SECONDS_PER_DAY
        out.append({"jd": float(jd_row), "ra": float(ra), "dec": float(dec),
                    "x": float(x), "y": float(y)})
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
    """IOD 'MX' uncertainty: value = M * 10**(X - 8), M and X single digits."""
    if not value or value <= 0:
        return "  "
    x = int(math.floor(math.log10(value))) + unit_exp_offset
    m = int(round(value / 10 ** (x - unit_exp_offset)))
    if m >= 10:
        m, x = 1, x + 1
    return f"{min(max(m, 1), 9)}{min(max(x, 0), 9)}"


def iod_line(ob, norad="99999", intldesg="", station="9999", status="G",
             time_unc_s=0.1, pos_unc_deg=0.02, behaviour=" "):
    """One observation in IOD format (angle format 2 = RA HHMMmmm, Dec
    +DDMMmm; epoch code 5 = J2000). Columns as in the SeeSat-L IOD spec
    example '23794 96 010A   2701 G 19990402012334770 17 25 1324520+382450 37 S'."""
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
            f"{_mx(time_unc_s)} 25 {angles} {_mx(pos_unc_deg)} {behaviour}")
    return line.rstrip()


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

def _utc(jd):
    return so.utc_str(jd)


def run(archive, elems, out, window_s=600.0, responses=None, fetch=None,
        max_offset_s=600.0, max_cross_deg=1.0, readout_ms=0.0, station="9999",
        show_unlit=False, log=print):
    """Everything; returns the per-trail results (also written to ``out``)."""
    os.makedirs(out, exist_ok=True)
    empty = {"data": {"satellites": {}}}
    res = so.analyse(archive, None, window_s, responses or {},
                     fetch=fetch or (lambda p: empty), sleep_s=0, show_unlit=show_unlit)
    site = res["session"].get("observer") or {}
    clock = res["session"].get("clock") or {}
    time_unc = 0.1 if clock.get("trusted") or clock.get("ntp_synchronized") else 1.0
    width, height = res["size"]
    results = []
    with zipfile.ZipFile(archive) as zf:
        for gi, g in enumerate(res["groups"]):
            st = so.stack_group(zf, g, res)
            if st is None:
                continue
            trails = so.trail_kinematics(zf, st, res, so.detect_trails(st["moving"]))
            so.match_trails(trails, st, g, res)
            for ti, t in enumerate(trails):
                obs = trail_observations(t, st, readout_ms, height)
                results.append({"group": gi + 1, "trail": ti + 1, "trail_obj": t,
                                "satchecker": t.get("satellite"), "obs": obs,
                                "label": so.trail_label(t)})
    jds = [o["jd"] for r in results for o in r["obs"]]
    if not jds:
        log("no timed trails found (need a trail seen in 3 or more frames)")
        return results
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
    _write_outputs(results, refined, out, station, time_unc, log)
    return results


def _write_outputs(results, refined, out, station, time_unc, log):
    # Observations: IOD + JSON.
    with open(os.path.join(out, "observations_iod.txt"), "w") as f:
        for r in results:
            m = r.get("match")
            norad = m["norad_str"] if m else "99999"
            intl = m["element_set"].intldesg if m else ""
            fr = r["trail_obj"].get("flash_ratio")
            beh = "F" if fr is not None and fr >= so.FLASH_RATIO else "S"
            for ob in r["obs"]:
                f.write(iod_line(ob, norad, intl, station, "G", time_unc, 0.02, beh) + "\n")
    with open(os.path.join(out, "observations.json"), "w") as f:
        json.dump([{"group": r["group"], "trail": r["trail"], "satchecker": r["satchecker"],
                    "label": r["label"],
                    "observations": [dict(o, utc=_utc(o["jd"])) for o in r["obs"]]}
                   for r in results], f, indent=1)
    # Identification report.
    rep, lines = [], []
    for r in results:
        head = (f"trail {r['group']}.{r['trail']}: {len(r['obs'])} frames"
                + (f", {_utc(r['obs'][0]['jd'])}" if r["obs"] else "")
                + f"; SatChecker: {r['satchecker'] or 'no match'}")
        lines.append(head)
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
        rep.append({"group": r["group"], "trail": r["trail"], "label": r["label"],
                    "satchecker": r["satchecker"], "candidates": cands,
                    "refined": ({k: v for k, v in r["refined"].items() if k != "satrec"}
                                if r.get("refined") else None)})
    with open(os.path.join(out, "identification.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(out, "identification.json"), "w") as f:
        json.dump(rep, f, indent=1, default=str)
    for ln in lines:
        log(ln)
    # Refined element sets, every format.
    if refined:
        tle_recs, omm_recs = [], []
        for r, rf in refined:
            m = r["match"]
            note = (f"diofinder fit of {m['source']} element set to trail "
                    f"{r['group']}.{r['trail']} ({len(r['obs'])} frames, rms "
                    f"{rf['rms_after_deg']:.3f} deg); valid near {_utc(rf['epoch_jd'])}")
            tle_recs.append(((rf["satrec"], m["name"], m["norad"]), note))
            omm_recs.append((omm_record(rf["satrec"], m["name"], m["norad"]), note))
        write_tle(tle_recs, os.path.join(out, "refined.tle"))
        write_omm_kvn(omm_recs, os.path.join(out, "refined_omm.kvn"))
        write_omm_xml(omm_recs, os.path.join(out, "refined_omm.xml"))
        write_omm_json(omm_recs, os.path.join(out, "refined_omm.json"))
        write_omm_csv(omm_recs, os.path.join(out, "refined_omm.csv"))
        log(f"wrote refined elements for {len(refined)} trail(s) (TLE, OMM KVN/XML/JSON/CSV)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("archive")
    ap.add_argument("--elements", nargs="*", default=[],
                    help="element files: TLE/3LE, OMM CSV/XML/JSON, or .zip of them")
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
        a.readout_ms, a.station)
    print(f"results in {out}")


if __name__ == "__main__":
    main()
