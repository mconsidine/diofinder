#!/usr/bin/env python3
"""CCSDS Orbit Ephemeris Messages (OEM, CCSDS 502.0-B-2): write and read.

An OEM is a table of state vectors (epoch, position km, velocity km/s) in a
named reference frame, with an interpolation method: an ephemeris, not mean
elements. Unlike a TLE/OMM it needs no propagator to use, and it is the form
other software (GMAT, STK, Orekit, SatelliteToolbox, some observation
planners) takes from precise orbit sources.

Writing: sample SGP4 (from any element set, or a sat_orbit fit.json) every
``step_s`` seconds; frames TEME (SGP4's own, the default), GCRF / EME2000 /
ICRF (inertial J2000 axes; EME2000 differs from GCRF by the 23 mas frame
bias, ignored here, ~1 m in LEO) or ITRF (Earth-fixed). Time system UTC.
KVN (text, ``.oem``) or XML.

Reading: KVN or XML, any number of segments; frames as above, time systems
UTC, TAI, TT, GPS. ``teme_positions`` interpolates (Lagrange, the file's
INTERPOLATION_DEGREE, default 7) onto any times, outside the covered span
returning an error code, so ``sat_orbit predict`` can list passes straight
from an OEM. Covariance blocks are skipped (not written either).

Used by ``sat_orbit.py ephem`` (write) and ``sat_orbit.py predict`` (read).
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
from xml.sax import saxutils as _xml

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import sat_identify as si  # noqa: E402

FRAMES = ("TEME", "GCRF", "EME2000", "ICRF", "ITRF")
_INERTIAL = ("GCRF", "EME2000", "ICRF")
TIME_SYSTEMS = ("UTC", "TAI", "TT", "GPS")
DEFAULT_DEGREE = 7
SECONDS_PER_DAY = 86400.0


# --------------------------------------------------------------------------- #
# States and frames
# --------------------------------------------------------------------------- #

def states(satrec, jd):
    """SGP4 TEME states: (err (N,), r (N,3) km, v (N,3) km/s)."""
    w, f = si._split_jd(jd)
    e, r, v = satrec.sgp4_array(w, f)
    return si.sane_states([satrec], e, r, v), r, v


def frame_name(name):
    n = (name or "").strip().upper().replace("ITRF2000", "ITRF").replace("ITRF93", "ITRF")
    n = {"ITRF-93": "ITRF", "ITRF2008": "ITRF", "ITRF2014": "ITRF", "ITRF2020": "ITRF",
         "J2000": "EME2000", "ICRF3": "ICRF", "GCRS": "GCRF", "ITRS": "ITRF"}.get(n, n)
    if n not in FRAMES:
        raise SystemExit(f"OEM reference frame {name!r} not supported (use one of "
                         + ", ".join(FRAMES) + ")")
    return n


def convert(r, v, jd, frm, to):
    """State vectors from frame ``frm`` to ``to`` (both from FRAMES)."""
    frm, to = frame_name(frm), frame_name(to)
    if frm == to or (frm in _INERTIAL and to in _INERTIAL):
        return np.asarray(r, float), np.asarray(v, float)
    si.so._offline_astropy()
    import astropy.units as u
    from astropy.coordinates import (GCRS, ITRS, TEME, CartesianDifferential,
                                     CartesianRepresentation)
    from astropy.time import Time
    cls = {"TEME": TEME, "ITRF": ITRS}
    t = Time(np.asarray(jd, float), format="jd", scale="utc")
    rep = CartesianRepresentation(np.asarray(r, float).T * u.km,
                                  differentials=CartesianDifferential(
                                      np.asarray(v, float).T * u.km / u.s))
    src = cls.get(frm, GCRS)(rep, obstime=t)
    dst = src.transform_to(cls.get(to, GCRS)(obstime=t))
    return (dst.cartesian.xyz.to(u.km).value.T,
            dst.velocity.d_xyz.to(u.km / u.s).value.T)


# --------------------------------------------------------------------------- #
# Segments
# --------------------------------------------------------------------------- #

def segment_from_satrec(satrec, name, object_id, jd0, jd1, step_s=60.0, frame="TEME",
                        comments=()):
    """One OEM segment sampled from SGP4 between jd0 and jd1 (inclusive).
    The start is moved back to a whole second, so the epochs written (to the
    millisecond) are the times the states were computed for."""
    jd0 = 2440587.5 + np.floor((jd0 - 2440587.5) * SECONDS_PER_DAY + 1e-6) / SECONDS_PER_DAY
    n = int(np.floor((jd1 - jd0) * SECONDS_PER_DAY / step_s + 1e-9)) + 1
    jd = jd0 + np.arange(n) * step_s / SECONDS_PER_DAY
    e, r, v = states(satrec, jd)
    ok = e == 0
    if not np.any(ok):
        raise SystemExit(f"{name}: SGP4 fails over the whole span (decayed?)")
    notes = list(comments)
    if not np.all(ok):
        notes.append(f"{int(np.sum(~ok))} sample(s) dropped where SGP4 failed")
    jd, r, v = jd[ok], r[ok], v[ok]
    r, v = convert(r, v, jd, "TEME", frame)
    return {"name": name, "object_id": object_id, "center": "EARTH",
            "frame": frame_name(frame), "time_system": "UTC", "jd": jd, "r": r, "v": v,
            "interpolation": "LAGRANGE", "degree": DEFAULT_DEGREE, "comments": notes,
            "start": float(jd[0]), "stop": float(jd[-1])}


def _iso(jd):
    return si.so.jd_to_utc(jd).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def _object_id(satrec, name, norad):
    return si.omm_record(satrec, name, norad)["OBJECT_ID"]


def write_kvn(segments, path, originator="diofinder sat_orbit", comment=None):
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    lines = ["CCSDS_OEM_VERS = 2.0"]
    if comment:
        lines += [f"COMMENT {c}" for c in str(comment).splitlines()]
    lines += [f"CREATION_DATE = {now}", f"ORIGINATOR = {originator}", ""]
    for s in segments:
        lines += ["META_START", f"OBJECT_NAME = {s['name']}", f"OBJECT_ID = {s['object_id']}",
                  f"CENTER_NAME = {s.get('center', 'EARTH')}", f"REF_FRAME = {s['frame']}",
                  f"TIME_SYSTEM = {s.get('time_system', 'UTC')}",
                  f"START_TIME = {_iso(s['start'])}", f"STOP_TIME = {_iso(s['stop'])}",
                  f"INTERPOLATION = {s.get('interpolation', 'LAGRANGE')}",
                  f"INTERPOLATION_DEGREE = {s.get('degree', DEFAULT_DEGREE)}", "META_STOP", ""]
        lines += [f"COMMENT {c}" for c in s.get("comments", ())]
        for j, r, v in zip(s["jd"], s["r"], s["v"]):
            lines.append(f"{_iso(j)} " + " ".join(f"{x:.6f}" for x in r) + " "
                         + " ".join(f"{x:.9f}" for x in v))
        lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines))


def write_xml(segments, path, originator="diofinder sat_orbit", comment=None):
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    esc = _xml.escape
    p = ['<?xml version="1.0" encoding="UTF-8"?>',
         '<oem xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
         'xsi:noNamespaceSchemaLocation="https://sanaregistry.org/r/ndmxml_unqualified/'
         'ndmxml-2.0.0-master-2.0.xsd" id="CCSDS_OEM_VERS" version="2.0">', "<header>"]
    if comment:
        p += [f"<COMMENT>{esc(c)}</COMMENT>" for c in str(comment).splitlines()]
    p += [f"<CREATION_DATE>{now}</CREATION_DATE>", f"<ORIGINATOR>{esc(originator)}</ORIGINATOR>",
          "</header>", "<body>"]
    for s in segments:
        p += ["<segment>", "<metadata>",
              f"<OBJECT_NAME>{esc(str(s['name']))}</OBJECT_NAME>",
              f"<OBJECT_ID>{esc(str(s['object_id']))}</OBJECT_ID>",
              f"<CENTER_NAME>{s.get('center', 'EARTH')}</CENTER_NAME>",
              f"<REF_FRAME>{s['frame']}</REF_FRAME>",
              f"<TIME_SYSTEM>{s.get('time_system', 'UTC')}</TIME_SYSTEM>",
              f"<START_TIME>{_iso(s['start'])}</START_TIME>",
              f"<STOP_TIME>{_iso(s['stop'])}</STOP_TIME>",
              f"<INTERPOLATION>{s.get('interpolation', 'LAGRANGE')}</INTERPOLATION>",
              f"<INTERPOLATION_DEGREE>{s.get('degree', DEFAULT_DEGREE)}</INTERPOLATION_DEGREE>",
              "</metadata>", "<data>"]
        p += [f"<COMMENT>{esc(c)}</COMMENT>" for c in s.get("comments", ())]
        for j, r, v in zip(s["jd"], s["r"], s["v"]):
            p.append(f"<stateVector><EPOCH>{_iso(j)}</EPOCH>"
                     + "".join(f'<{k} units="km">{x:.6f}</{k}>' for k, x in zip("XYZ", r))
                     + "".join(f'<{k}_DOT units="km/s">{x:.9f}</{k}_DOT>'
                               for k, x in zip("XYZ", v))
                     + "</stateVector>")
        p += ["</data>", "</segment>"]
    p += ["</body>", "</oem>"]
    with open(path, "w") as f:
        f.write("\n".join(p) + "\n")


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

def is_oem_text(text):
    head = text[:4000]
    return "CCSDS_OEM_VERS" in head or bool(re.search(r"<oem[\s>]", head))


def is_oem(path):
    """True for a local file that holds an OEM (KVN or XML)."""
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "r", errors="replace") as f:
            return is_oem_text(f.read(4000))
    except OSError:
        return False


def _epoch(text, time_system):
    """OEM epoch text (calendar or day-of-year form) -> UTC JD."""
    t = text.strip().rstrip("Zz")
    m = re.fullmatch(r"(\d{4})-(\d{3})T(\d{2}):(\d{2}):(\d{2}(?:\.\d*)?)", t)
    if m:
        d = dt.datetime(int(m.group(1)), 1, 1) + dt.timedelta(days=int(m.group(2)) - 1)
        hh, mm, ss = int(m.group(3)), int(m.group(4)), float(m.group(5))
    else:
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2}(?:\.\d*)?)", t)
        if not m:
            raise SystemExit(f"OEM: cannot read the epoch {text!r}")
        d = dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        hh, mm, ss = int(m.group(4)), int(m.group(5)), float(m.group(6))
    jd = (2440587.5 + d.replace(tzinfo=dt.timezone.utc).timestamp() / SECONDS_PER_DAY
          + (hh * 3600 + mm * 60 + ss) / SECONDS_PER_DAY)
    return jd if time_system == "UTC" else _to_utc(np.array([jd]), time_system)[0]


def _to_utc(jd, time_system):
    ts = (time_system or "UTC").upper()
    if ts == "UTC":
        return jd
    if ts not in TIME_SYSTEMS:
        raise SystemExit(f"OEM time system {time_system!r} not supported ("
                         + ", ".join(TIME_SYSTEMS) + ")")
    si.so._offline_astropy()
    from astropy.time import Time
    if ts == "GPS":                                   # GPS = TAI - 19 s
        return Time(np.asarray(jd) + 19.0 / SECONDS_PER_DAY, format="jd", scale="tai").utc.jd
    return Time(np.asarray(jd), format="jd", scale=ts.lower()).utc.jd


def _finish(seg, rows, source):
    if not rows:
        return None
    ts = seg.get("time_system", "UTC").upper()
    jd = np.array([r[0] for r in rows])                # still in the file's time system
    jd = _to_utc(jd, ts) if ts != "UTC" else jd
    st = np.array([r[1] for r in rows], float)
    order = np.argsort(jd, kind="stable")
    seg.update(jd=jd[order], r=st[order, :3], v=st[order, 3:6], source=source,
               frame=frame_name(seg.get("frame", "")), time_system="UTC")
    seg["start"] = seg.get("start") or float(seg["jd"][0])
    seg["stop"] = seg.get("stop") or float(seg["jd"][-1])
    for k in ("useable_start", "useable_stop"):
        if seg.get(k):
            seg["start" if k.endswith("start") else "stop"] = seg[k]
    seg.setdefault("name", "OEM object")
    seg.setdefault("object_id", seg["name"])
    seg.setdefault("degree", DEFAULT_DEGREE)
    seg.setdefault("interpolation", "LAGRANGE")
    return seg


_META_KEYS = {"OBJECT_NAME": "name", "OBJECT_ID": "object_id", "CENTER_NAME": "center",
              "REF_FRAME": "frame", "TIME_SYSTEM": "time_system", "INTERPOLATION": "interpolation"}


def _meta(seg, key, val, pending_times):
    key = key.upper()
    if key in _META_KEYS:
        seg[_META_KEYS[key]] = val.strip()
    elif key == "INTERPOLATION_DEGREE":
        seg["degree"] = int(float(val))
    elif key in ("START_TIME", "STOP_TIME", "USEABLE_START_TIME", "USEABLE_STOP_TIME"):
        pending_times[key] = val.strip()


def _times(seg, pending):
    ts = seg.get("time_system", "UTC").upper()
    for key, name in (("START_TIME", "start"), ("STOP_TIME", "stop"),
                      ("USEABLE_START_TIME", "useable_start"),
                      ("USEABLE_STOP_TIME", "useable_stop")):
        if key in pending:
            seg[name] = _epoch(pending[key], ts)


def parse_kvn(text, source="oem"):
    segs, seg, rows, pending = [], None, [], {}
    in_meta = in_cov = False
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("COMMENT"):
            if ln.startswith("COMMENT") and seg is not None and not in_meta:
                seg.setdefault("comments", []).append(ln[7:].strip())
            continue
        if ln == "META_START":
            if seg is not None:
                done = _finish(seg, rows, source)
                if done:
                    segs.append(done)
            seg, rows, pending, in_meta = {"comments": []}, [], {}, True
            continue
        if ln == "META_STOP":
            _times(seg, pending)
            in_meta = False
            continue
        if ln in ("COVARIANCE_START", "COVARIANCE_STOP"):
            in_cov = ln == "COVARIANCE_START"
            continue
        if in_cov:
            continue
        if in_meta:
            if "=" in ln:
                k, v = ln.split("=", 1)
                _meta(seg, k.strip(), v, pending)
            continue
        if seg is None or "=" in ln:
            continue                                   # header lines
        parts = ln.split()
        if len(parts) >= 7:
            rows.append((_epoch(parts[0], "UTC"), [float(x) for x in parts[1:7]]))
    if seg is not None:
        done = _finish(seg, rows, source)
        if done:
            segs.append(done)
    return segs


def parse_xml(text, source="oem"):
    import xml.etree.ElementTree as ET
    root = ET.fromstring(text)
    for el in root.iter():                             # drop namespaces
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    segs = []
    for sg in root.iter("segment"):
        seg, pending, rows = {"comments": []}, {}, []
        md = sg.find("metadata")
        for el in (md if md is not None else []):
            if el.text is not None:
                _meta(seg, el.tag, el.text, pending)
        _times(seg, pending)
        data = sg.find("data")
        for el in (data if data is not None else []):
            if el.tag == "COMMENT" and el.text:
                seg["comments"].append(el.text.strip())
            elif el.tag == "stateVector":
                g = {c.tag: c.text for c in el}
                rows.append((_epoch(g["EPOCH"], "UTC"),
                             [float(g[k]) for k in ("X", "Y", "Z", "X_DOT", "Y_DOT", "Z_DOT")]))
        done = _finish(seg, rows, source)
        if done:
            segs.append(done)
    return segs


def read_oem(path):
    """All segments of an OEM file (KVN or XML), times UTC, frames normalised."""
    with open(path, "r", errors="replace") as f:
        text = f.read()
    src = os.path.basename(path)
    segs = parse_xml(text, src) if text.lstrip().startswith("<") else parse_kvn(text, src)
    if not segs:
        raise SystemExit(f"{path}: no state vectors in this OEM")
    return segs


# --------------------------------------------------------------------------- #
# Interpolation
# --------------------------------------------------------------------------- #

def _lagrange(tn, y, t, k):
    """Lagrange interpolation of y (n, d) at nodes tn onto t, k nodes each."""
    n = len(tn)
    k = max(2, min(k, n))
    idx = np.searchsorted(tn, t)
    lo = np.clip(idx - k // 2, 0, n - k)
    win = lo[:, None] + np.arange(k)
    tw, yw = tn[win], y[win]
    w = np.ones(win.shape)
    for j in range(k):
        for m in range(k):
            if m != j:
                w[:, j] *= (t - tw[:, m]) / (tw[:, j] - tw[:, m])
    return np.einsum("mk,mkd->md", w, yw)


def interpolate(seg, jd):
    """(err (N,), r (N,3), v (N,3)) in the segment's frame; err 1 outside its span."""
    jd = np.asarray(jd, float)
    err = np.ones(len(jd), int)
    r = np.full((len(jd), 3), np.nan)
    v = np.full((len(jd), 3), np.nan)
    inside = (jd >= seg["start"] - 1e-9) & (jd <= seg["stop"] + 1e-9) & \
        (jd >= seg["jd"][0] - 1e-9) & (jd <= seg["jd"][-1] + 1e-9)
    if np.any(inside):
        t0 = seg["jd"][0]
        tn = (seg["jd"] - t0) * SECONDS_PER_DAY
        t = (jd[inside] - t0) * SECONDS_PER_DAY
        k = int(seg.get("degree") or DEFAULT_DEGREE) + 1
        r[inside] = _lagrange(tn, seg["r"], t, k)
        v[inside] = _lagrange(tn, seg["v"], t, k)
        err[inside] = 0
    return err, r, v


def teme_positions(segments, jd):
    """TEME positions of one object from its segments (a later segment wins
    where they overlap): (err (N,), r (N,3) km)."""
    jd = np.asarray(jd, float)
    err = np.ones(len(jd), int)
    r = np.full((len(jd), 3), np.nan)
    for seg in segments:
        e, rr, vv = interpolate(seg, jd)
        ok = e == 0
        if not np.any(ok):
            continue
        rt, _ = convert(rr[ok], vv[ok], jd[ok], seg["frame"], "TEME")
        r[ok] = rt
        err[ok] = 0
    return err, r


def by_object(segments):
    """[(name, object_id, [segments])] in file order."""
    out = {}
    for s in segments:
        out.setdefault((s["name"], s["object_id"]), []).append(s)
    return [(k[0], k[1], v) for k, v in out.items()]


def coverage(segments):
    return min(s["start"] for s in segments), max(s["stop"] for s in segments)
