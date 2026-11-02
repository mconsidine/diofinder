#!/usr/bin/env python3
"""Mark predicted satellites on the frames of a diofinder live capture.

Evolved from the SatChecker FOV notebook
(https://satchecker.readthedocs.io/en/latest/notebooks/fov.html). Instead of
one hand-typed time and pointing, it reads a ``livecap_<UTC>.zip`` from the
finder (Home page -> Live capture) and, for each frame:

1. **Fits a WCS to the frame** from the stars the plate solver matched in it
   (pixel <-> RA/Dec pairs the finder records while capturing). That gives the
   exact sky-to-pixel mapping for that frame, mirroring included, with no
   roll-angle convention to get wrong. A frame that didn't solve borrows the
   WCS of the nearest solved frame (the field barely moves in seconds); if
   there are no matched stars at all (older finder software) it falls back to
   centre + roll + FOV, which is approximate.
2. **Asks SatChecker** which satellites crossed the field. Consecutive frames
   at about the same pointing share one query (``--window`` seconds), so a
   long capture costs a few API calls, not one per frame.
3. **Draws each satellite's predicted streak for THAT frame's exposure**
   (start->end of the exposure, from interpolating the predicted track) on
   the frame itself, plus its whole predicted track as a dashed line, so a
   streak in the image can be read straight off: "that one is STARLINK-1234".
4. **Scores** each prediction by the brightness along the predicted track in
   the image (in noise sigmas, best of a few px either side, because TLE
   predictions are often off by several pixels across-track and more
   along-track). Treat it as a hint for which overlays to look at first.

Outputs (in --out, default ``<archive>_sats/``):
  * ``frame_000123_sats.png``: the frame with the predictions drawn on it, for
    every frame with at least one satellite inside it (``--all-frames``: all).
  * ``report.csv``: one row per (frame, satellite): exposure segment ends in
    pixels, whether it falls in the frame, the score, how the WCS was
    obtained, and its fit residual.
  * ``chart_<n>.png`` (``--chart``): the notebook's sky chart per query group,
    with the real frame outline instead of a circle.
  * ``stack_<n>_moving.png`` / ``stack_<n>.png`` (``--stack``): each query
    group's frames aligned on its middle frame (through the fitted mappings,
    so stars line up despite the drift) and stacked. ``_moving`` is the
    brightest value at each pixel minus the median, so only what changed
    remains: a satellite becomes a dashed line, one dash per exposure. Each
    predicted pass is drawn as rails along its whole track, and each frame's
    predicted dash sits just outside them, so the real dashes can be checked
    against where and when they were predicted. The plain one keeps the
    stars. ``--window 600`` stacks ten-minute stretches.
  * ``satchecker_responses.json``: every SatChecker response. Re-runs read it
    (``--responses FILE``) so you can re-plot offline without new queries.

Needs: numpy, astropy, matplotlib, Pillow (no scipy) (``pip install astropy matplotlib
pillow``). Network only for new queries.

Examples:
  python3 tests/satcheck_overlay.py livecap_20261102T013045Z.zip
  python3 tests/satcheck_overlay.py livecap_...zip --frames 120-180 --chart
  python3 tests/satcheck_overlay.py livecap_...zip --responses out/satchecker_responses.json

Caveats: predicted positions are only as good as the TLEs (Starlink
manoeuvres; a day-old TLE can be several arcminutes off along-track) and as
the finder's clock (the Pi has no RTC: a clock that is 1 s off moves a LEO
satellite ~0.5 deg). ``session.json`` records whether the clock looked sane.
SatChecker positions are assumed to be J2000/ICRS like the finder's; if every
overlay is offset by roughly the same ~0.3 deg (about 20 px), try
``--sat-frame jnow``.
"""
import argparse
import csv
import io
import json
import math
import os
import sys
import zipfile
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import satcheck_archive as sa  # noqa: E402  (load_archive, query, parse_satellites)

MIN_MATCHES = 6           # matched stars needed to fit a frame's own WCS
BORROW_S = 30.0           # an unsolved frame may borrow a WCS this close in time
GROUP_POINTING_DEG = 1.0  # frames this close to the group's first share a query
RAIL_PX = 9               # half-gap between the two rails marking a prediction


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #

def jd_to_utc(jd):
    # Round to the millisecond: JD floats carry ~10 us of round-off, which
    # otherwise prints 02:00:00.000 as 01:59:59.999.
    return datetime.fromtimestamp(round((jd - 2440587.5) * 86400.0, 3), tz=timezone.utc)


def utc_str(jd):
    return jd_to_utc(jd).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " UTC"


def frame_window(rec):
    """(jd_start, jd_end) of a frame's exposure, or None."""
    if rec.get("jd_start") is not None and rec.get("exposure_s"):
        return rec["jd_start"], rec["jd_start"] + float(rec["exposure_s"]) / 86400.0
    if rec.get("jd_saved") is not None:      # hand-over time: after the exposure
        return rec["jd_saved"] - 1.0 / 86400.0, rec["jd_saved"]
    return None


# --------------------------------------------------------------------------- #
# Per-frame WCS
# --------------------------------------------------------------------------- #

def _tan_project(ra, dec, ra0, dec0):
    """Gnomonic (TAN) standard coordinates (xi, eta) in degrees about
    (ra0, dec0)."""
    ra, dec, ra0, dec0 = map(np.radians, (ra, dec, ra0, dec0))
    cosc = np.sin(dec0) * np.sin(dec) + np.cos(dec0) * np.cos(dec) * np.cos(ra - ra0)
    xi = np.cos(dec) * np.sin(ra - ra0) / cosc
    eta = (np.cos(dec0) * np.sin(dec) - np.sin(dec0) * np.cos(dec) * np.cos(ra - ra0)) / cosc
    return np.degrees(xi), np.degrees(eta)


def fit_frame_wcs(matched, width=960, height=760):
    """TAN WCS fitted to matched [x, y, ra, dec, mag] rows (finder pixel
    convention: (0.5, 0.5) = centre of pixel (0, 0); astropy uses 0-based
    pixel centres, hence the -0.5). Linear least squares on the tangent plane
    (numpy only), re-centred on the frame centre so the projection point is
    where a TAN WCS wants it. Returns (wcs, rms_px) or None."""
    from astropy.wcs import WCS
    if not matched or len(matched) < MIN_MATCHES:
        return None
    m = np.asarray([r[:4] for r in matched], dtype=float)
    x, y = m[:, 0] - 0.5, m[:, 1] - 0.5
    v = np.sum(_vec(m[:, 2], m[:, 3]), axis=0)
    ra0 = math.degrees(math.atan2(v[1], v[0])) % 360.0
    dec0 = math.degrees(math.asin(v[2] / np.linalg.norm(v)))
    A = np.column_stack([x, y, np.ones_like(x)])
    w = None
    for _ in range(3):
        xi, eta = _tan_project(m[:, 2], m[:, 3], ra0, dec0)
        coef, *_ = np.linalg.lstsq(A, np.column_stack([xi, eta]), rcond=None)
        cd = coef[:2, :].T                       # [[dxi/dx, dxi/dy], [deta/dx, deta/dy]]
        if abs(np.linalg.det(cd)) < 1e-12:
            return None
        p0 = -np.linalg.solve(cd, coef[2, :])     # 0-based pixel of (ra0, dec0)
        w = WCS(naxis=2)
        w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
        w.wcs.crval = [ra0, dec0]
        w.wcs.crpix = [p0[0] + 1.0, p0[1] + 1.0]
        w.wcs.cd = cd
        # Move the tangent point to the frame centre and refit.
        ra0, dec0 = (float(c) for c in w.pixel_to_world_values(
            (width - 1) / 2.0, (height - 1) / 2.0))
    px, py = w.world_to_pixel_values(m[:, 2], m[:, 3])
    rms = float(np.sqrt(np.mean((px - x) ** 2 + (py - y) ** 2)))
    return w, rms


def roll_wcs(rec, width, height, flip_roll=False):
    """Approximate TAN WCS from the solve's centre / roll / FOV / mirroring,
    for frames with no matched stars. Uses the documented convention (roll =
    celestial North's angle counter-clockwise from image up; a mirrored frame
    is flipped left-right first), which has not been verified on sky, so
    overlays from this path are marked 'approx'."""
    from astropy.wcs import WCS
    ra, dec = rec.get("center_ra_deg"), rec.get("center_dec_deg")
    roll, fov = rec.get("roll_deg"), rec.get("fov_deg")
    if ra is None or dec is None or roll is None or not fov:
        return None
    s = float(fov) / width                                   # deg / px
    th = math.radians(-float(roll) if flip_roll else float(roll))
    # North-up/East-left image (x right, y down): dxi = -s dx, deta = -s dy.
    cd0 = np.array([[-s, 0.0], [0.0, -s]])
    # The frame is the North-up image turned counter-clockwise by roll.
    rinv = np.array([[math.cos(th), -math.sin(th)], [math.sin(th), math.cos(th)]])
    flip = np.diag([-1.0, 1.0]) if rec.get("is_mirrored") else np.eye(2)
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [float(ra), float(dec)]
    w.wcs.crpix = [width / 2.0 + 0.5, height / 2.0 + 0.5]    # 1-based centre
    w.wcs.cd = cd0 @ rinv @ flip
    return w


def interp_wcs(wa, wb, f):
    """WCS a fraction f of the way from wa to wb (same frame size). Both are
    fitted with the tangent point at the frame centre, so interpolating the
    tangent point (on the sphere) and the CD matrix is a good model of the
    field drifting/rotating between two nearby frames."""
    from astropy.wcs import WCS
    va, vb = _vec(*wa.wcs.crval), _vec(*wb.wcs.crval)
    v = (1 - f) * va + f * vb
    v /= np.linalg.norm(v)
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [math.degrees(math.atan2(v[1], v[0])) % 360.0,
                   math.degrees(math.asin(v[2]))]
    w.wcs.crpix = (1 - f) * np.asarray(wa.wcs.crpix) + f * np.asarray(wb.wcs.crpix)
    w.wcs.cd = (1 - f) * wa.wcs.cd + f * wb.wcs.cd
    return w


def assign_wcs(frames, width, height, flip_roll=False):
    """Per frame: (wcs, source, rms_px). Source is 'fit',
    'interp:<a>+<b>' (unsolved, between two solved frames), 'borrowed:<file>'
    (unsolved, nearest solved frame on one side), 'approx' or None."""
    fits = {}
    for i, r in enumerate(frames):
        f = fit_frame_wcs(r.get("matched"), width, height)
        if f is not None:
            fits[i] = f
    times = [(frame_window(r) or (None, None))[0] for r in frames]
    out = []
    for i, r in enumerate(frames):
        if i in fits:
            out.append((fits[i][0], "fit", fits[i][1]))
            continue
        best = None
        before = after = None
        if times[i] is not None:
            for j in fits:
                if times[j] is None:
                    continue
                dt = abs(times[j] - times[i]) * 86400.0
                if dt <= BORROW_S and (best is None or dt < best[0]):
                    best = (dt, j)
                if dt <= BORROW_S and times[j] < times[i] and (
                        before is None or times[j] > times[before]):
                    before = j
                if dt <= BORROW_S and times[j] > times[i] and (
                        after is None or times[j] < times[after]):
                    after = j
        if before is not None and after is not None:
            # Solved frames on both sides: interpolate in time, which follows
            # the field's drift on a non-tracking mount (borrowing one side
            # would be off by the drift since that frame).
            f = (times[i] - times[before]) / (times[after] - times[before])
            w = interp_wcs(fits[before][0], fits[after][0], f)
            out.append((w, f"interp:{frames[before]['file']}+{frames[after]['file']}",
                        max(fits[before][1], fits[after][1])))
            continue
        if best is not None:
            j = best[1]
            out.append((fits[j][0], f"borrowed:{frames[j]['file']}", fits[j][1]))
            continue
        src = r if r.get("center_ra_deg") is not None else (r.get("last_solved") or {})
        w = roll_wcs(dict(src, is_mirrored=r.get("is_mirrored", src.get("is_mirrored"))),
                     width, height, flip_roll)
        out.append((w, "approx" if w is not None else None, None))
    return out


def frame_center(rec):
    if rec.get("center_ra_deg") is not None:
        return rec["center_ra_deg"], rec["center_dec_deg"]
    ls = rec.get("last_solved") or {}
    if ls.get("center_ra_deg") is not None:
        return ls["center_ra_deg"], ls["center_dec_deg"]
    return None


# --------------------------------------------------------------------------- #
# Spherical helpers
# --------------------------------------------------------------------------- #

def _vec(ra, dec):
    ra, dec = np.radians(ra), np.radians(dec)
    return np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)], -1)


def sep_deg(ra1, dec1, ra2, dec2):
    d = float(np.clip(np.dot(_vec(ra1, dec1), _vec(ra2, dec2)), -1.0, 1.0))
    return math.degrees(math.acos(d))


def interp_track(track, jd):
    """(ra, dec) on a predicted track [(jd, ra, dec), ...] at time jd, or None
    outside it. Interpolated on the unit sphere (safe across RA=0)."""
    eps = 0.005 / 86400.0                  # 5 ms: rounding slack at the ends
    if len(track) == 1:
        return (track[0][1], track[0][2]) if abs(jd - track[0][0]) * 86400 < 0.5 else None
    if jd < track[0][0] - eps or jd > track[-1][0] + eps:
        return None
    jd = min(max(jd, track[0][0]), track[-1][0])
    jds = [p[0] for p in track]
    k = max(1, min(len(track) - 1, int(np.searchsorted(jds, jd))))
    (j0, r0, d0), (j1, r1, d1) = track[k - 1], track[k]
    f = 0.0 if j1 == j0 else (jd - j0) / (j1 - j0)
    v = (1 - f) * _vec(r0, d0) + f * _vec(r1, d1)
    v = v / np.linalg.norm(v)
    return math.degrees(math.atan2(v[1], v[0])) % 360.0, math.degrees(math.asin(v[2]))


# --------------------------------------------------------------------------- #
# SatChecker queries (grouped)
# --------------------------------------------------------------------------- #

def group_frames(frames, idxs, window_s, radius_deg):
    """Split the selected frames into query groups: consecutive in time, at
    most window_s long, pointing within GROUP_POINTING_DEG of the group's
    first frame. Returns [{"idx": [...], "params": {...}}]."""
    groups, cur = [], []

    def close(g):
        if not g:
            return
        cs = [frame_center(frames[i]) for i in g]
        ws = [frame_window(frames[i]) for i in g]
        ra0, dec0 = cs[0]
        v = np.sum([_vec(*c) for c in cs], axis=0)
        v /= np.linalg.norm(v)
        ra_c = math.degrees(math.atan2(v[1], v[0])) % 360.0
        dec_c = math.degrees(math.asin(v[2]))
        spread = max(sep_deg(ra_c, dec_c, *c) for c in cs)
        jd0 = min(w[0] for w in ws) - 2.0 / 86400.0      # 2 s margin each side
        jd1 = max(w[1] for w in ws) + 2.0 / 86400.0
        groups.append({"idx": list(g), "center": (ra_c, dec_c),
                       "radius": radius_deg + spread, "jd": (jd0, jd1)})

    for i in idxs:
        c, w = frame_center(frames[i]), frame_window(frames[i])
        if c is None or w is None:
            continue
        if cur:
            c0, w0 = frame_center(frames[cur[0]]), frame_window(frames[cur[0]])
            if ((w[1] - w0[0]) * 86400.0 > window_s
                    or sep_deg(*c0, *c) > GROUP_POINTING_DEG):
                close(cur)
                cur = []
        cur.append(i)
    close(cur)
    return groups


def group_params(g, site):
    jd0, jd1 = g["jd"]
    return {
        "latitude": site["latitude_deg"], "longitude": site["longitude_deg"],
        "elevation": site.get("elevation_m") or 0.0,
        "mid_obs_time_jd": f"{(jd0 + jd1) / 2.0:.8f}",
        "duration": round((jd1 - jd0) * 86400.0, 3),
        "ra": round(g["center"][0], 5), "dec": round(g["center"][1], 5),
        "fov_radius": round(g["radius"], 3),
        "group_by": "satellite", "async": "False",
    }


def _key(params):
    return json.dumps(params, sort_keys=True)


def to_icrs_tracks(tracks, sat_frame):
    """Optionally precess JNow (FK5 of date) satellite positions to ICRS."""
    if sat_frame != "jnow" or not tracks:
        return tracks
    from astropy.coordinates import FK5, SkyCoord
    from astropy.time import Time
    import astropy.units as u
    out = {}
    for name, pts in tracks.items():
        jd = np.array([p[0] for p in pts])
        c = SkyCoord([p[1] for p in pts] * u.deg, [p[2] for p in pts] * u.deg,
                     frame=FK5(equinox=Time(jd.mean(), format="jd")))
        i = c.icrs
        out[name] = list(zip(jd.tolist(), i.ra.deg.tolist(), i.dec.deg.tolist()))
    return out


# --------------------------------------------------------------------------- #
# Image work
# --------------------------------------------------------------------------- #

def stretch(img):
    """asinh stretch for display (same idea as the finder's live view)."""
    f = img.astype(np.float32)
    sky = float(np.median(f))
    x = np.clip(f - sky, 0, None)
    beta = max(1.0, sky * 0.1)
    xs = np.arcsinh(x / beta)
    top = float(np.percentile(xs, 99.9)) or float(xs.max()) or 1.0
    return np.clip(xs / top, 0, 1)


def line_score(img, p0, p1, tol_px=8, extend=0.5):
    """Brightness along the segment p0->p1 (extended by `extend` of its length
    each way, for along-track timing error), best over cross-track offsets of
    up to +/-tol_px, in noise sigmas above the frame median. None if the
    segment misses the frame."""
    h, w = img.shape
    f = img.astype(np.float32)
    med = float(np.median(f))
    noise = 1.4826 * float(np.median(np.abs(f - med))) or 1.0
    p0, p1 = np.asarray(p0, float), np.asarray(p1, float)
    d = p1 - p0
    L = float(np.hypot(*d))
    if L < 1e-6:
        d, L = np.array([1.0, 0.0]), 1.0
    u_ = d / L
    n_ = np.array([-u_[1], u_[0]])
    a = p0 - u_ * L * extend
    b = p1 + u_ * L * extend
    n = max(8, int(np.hypot(*(b - a))))
    ts = np.linspace(0, 1, n)
    best = None
    for off in range(-tol_px, tol_px + 1):
        pts = a[None, :] + (b - a)[None, :] * ts[:, None] + n_[None, :] * off
        xi, yi = np.round(pts[:, 0]).astype(int), np.round(pts[:, 1]).astype(int)
        ok = (xi >= 0) & (xi < w) & (yi >= 0) & (yi < h)
        if ok.sum() < 5:
            continue
        val = (float(np.mean(f[yi[ok], xi[ok]])) - med) / noise * math.sqrt(ok.sum())
        best = val if best is None else max(best, val)
    return best


def segment_in_frame(p0, p1, w, h, margin=0):
    """True if the segment p0->p1 touches the frame (sampled)."""
    for t in np.linspace(0, 1, 25):
        x = p0[0] + (p1[0] - p0[0]) * t
        y = p0[1] + (p1[1] - p0[1]) * t
        if -margin <= x < w + margin and -margin <= y < h + margin:
            return True
    return False


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def parse_frames_arg(spec, n):
    if not spec:
        return list(range(n))
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a) - 1, int(b)))
        elif part:
            out.append(int(part) - 1)
    return [i for i in out if 0 <= i < n]


def analyse(archive, frames_spec=None, window_s=60.0, responses=None,
            sat_frame="icrs", flip_roll=False, fetch=None, base=sa.DEFAULT_BASE,
            sleep_s=1.0):
    """Do everything except drawing. Returns a dict with per-frame results.
    ``fetch(params) -> response`` can be injected (tests / offline)."""
    import time
    session, frames = sa.load_archive(archive)
    cam = session.get("camera") or {}
    width, height = int(cam.get("width_px", 960)), int(cam.get("height_px", 760))
    site = session.get("observer") or {}
    if site.get("latitude_deg") is None or site.get("longitude_deg") is None:
        raise SystemExit("archive has no observer site (set it on the finder, or "
                         "edit session.json)")
    radius = cam.get("fov_radius_deg") or 8.6
    wcs = assign_wcs(frames, width, height, flip_roll)
    idxs = parse_frames_arg(frames_spec, len(frames))
    groups = group_frames(frames, idxs, window_s, radius)
    cache = dict(responses or {})
    fetch = fetch or (lambda p: sa.query(base, p)[1])
    for gi, g in enumerate(groups):
        g["params"] = group_params(g, site)
        k = _key(g["params"])
        if k not in cache:
            cache[k] = fetch(g["params"])
            if gi + 1 < len(groups):
                time.sleep(sleep_s)
        g["tracks"] = to_icrs_tracks(sa.parse_satellites(cache[k]), sat_frame)
    results = []
    for g in groups:
        for i in g["idx"]:
            rec = frames[i]
            w, src, rms = wcs[i]
            jd0, jd1 = frame_window(rec)
            sats = []
            for name, track in g["tracks"].items():
                s0, s1 = interp_track(track, jd0), interp_track(track, jd1)
                entry = {"satellite": name}
                if w is not None:
                    tx, ty = w.world_to_pixel_values(
                        np.array([p[1] for p in track]), np.array([p[2] for p in track]))
                    entry["track_px"] = list(zip(tx.tolist(), ty.tolist()))
                    if s0 and s1:
                        (x0, x1), (y0, y1) = w.world_to_pixel_values(
                            np.array([s0[0], s1[0]]), np.array([s0[1], s1[1]]))
                        entry["seg_px"] = ((float(x0), float(y0)), (float(x1), float(y1)))
                        entry["in_frame"] = segment_in_frame(*entry["seg_px"], width, height)
                sats.append(entry)
            results.append({"i": i, "rec": rec, "wcs": w, "wcs_source": src,
                            "wcs_rms_px": rms, "jd": (jd0, jd1), "sats": sats,
                            "group": g})
    return {"session": session, "frames": frames, "groups": groups,
            "results": results, "responses": cache, "size": (width, height)}


def score_results(archive, res):
    with zipfile.ZipFile(archive) as zf:
        for r in res["results"]:
            if not any(s.get("in_frame") for s in r["sats"]):
                continue
            img = _load_frame(zf, r["rec"]["file"])
            for s in r["sats"]:
                if s.get("in_frame"):
                    s["score"] = line_score(img, *s["seg_px"])


def _load_frame(zf, name):
    from PIL import Image
    return np.array(Image.open(io.BytesIO(zf.read(name))).convert("L"))


def draw_frame(img, r, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    h, w = img.shape
    fig = plt.figure(figsize=(w / 80, h / 80 + 0.6), dpi=100)
    ax = fig.add_axes([0, 0, 1, h / (h + 48)])
    ax.imshow(stretch(img), cmap="gray", origin="upper", interpolation="nearest")
    ax.set_xlim(-0.5, w - 0.5)
    ax.set_ylim(h - 0.5, -0.5)
    ax.axis("off")
    colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]
    k = 0
    for s in r["sats"]:
        if "track_px" not in s:
            continue
        c = colors[k % len(colors)]
        k += 1
        tx, ty = zip(*s["track_px"])
        ax.plot(tx, ty, ":", color=c, lw=0.8, alpha=0.6)    # whole predicted pass
        if s.get("seg_px"):
            # This exposure's predicted streak as two rails either side of the
            # path, so the real trail (if any) stays visible between them.
            (x0, y0), (x1, y1) = s["seg_px"]
            d = np.array([x1 - x0, y1 - y0], float)
            nv = np.array([-d[1], d[0]]) / (np.hypot(*d) or 1.0) * RAIL_PX
            for sgn in (1, -1):
                ax.plot([x0 + sgn * nv[0], x1 + sgn * nv[0]],
                        [y0 + sgn * nv[1], y1 + sgn * nv[1]], "-", color=c, lw=1.2)
            ax.plot([x0], [y0], "o", color=c, ms=6, mfc="none")   # exposure start
            lbl = s["satellite"]
            if s.get("score") is not None:
                lbl += f"  ({s['score']:.0f}σ)"
            if s.get("in_frame"):
                ax.annotate(lbl, (x0, y0), color=c, fontsize=8,
                            xytext=(8, -12), textcoords="offset points",
                            bbox=dict(boxstyle="round,pad=0.2", fc="black",
                                      ec=c, alpha=0.7))
    jd0, jd1 = r["jd"]
    src = r["wcs_source"] or "none"
    if r["wcs_rms_px"] is not None:
        src += f", fit rms {r['wcs_rms_px']:.2f} px"
    fig.text(0.01, 1 - 0.3 * 48 / (h + 48),
             f"{r['rec']['file']}   {utc_str(jd0)}  +{(jd1 - jd0) * 86400:.2f} s   "
             f"WCS: {src}   (rails = predicted streak this exposure, circle = "
             f"start, dotted = whole pass)",
             fontsize=8, va="top")
    fig.savefig(path, dpi=100, facecolor="white")
    plt.close(fig)


def draw_chart(g, frames, res, path):
    """The notebook's sky chart for one query group, with frame outlines."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from astropy.wcs import WCS
    ra_c, dec_c = g["center"]
    cw = WCS(naxis=2)
    cw.wcs.crpix = [0, 0]
    cw.wcs.crval = [ra_c, dec_c]
    cw.wcs.cdelt = [-0.1, 0.1]
    cw.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    fig = plt.figure(figsize=(11, 9))
    ax = fig.add_subplot(1, 1, 1, projection=cw)
    tr = ax.get_transform("world")
    w, h = res["size"]
    by_i = {r["i"]: r for r in res["results"]}
    for i in (g["idx"][0], g["idx"][-1]):
        fw = by_i[i]["wcs"]
        if fw is None:
            continue
        xs = [-0.5, w - 0.5, w - 0.5, -0.5, -0.5]
        ys = [-0.5, -0.5, h - 0.5, h - 0.5, -0.5]
        ra, dec = fw.pixel_to_world_values(xs, ys)
        ax.plot(ra, dec, "b--" if i == g["idx"][0] else "c--", transform=tr,
                label=f"frame {frames[i]['file']}")
    for name, track in g["tracks"].items():
        ra = [p[1] for p in track]
        dec = [p[2] for p in track]
        ax.plot(ra, dec, transform=tr, label=name)
        ax.scatter(ra, dec, s=6, transform=tr)
        ax.text(ra[0], dec[0], f"{name} start", transform=tr, fontsize=7)
    ax.set_aspect("equal")
    ax.legend(bbox_to_anchor=(1.05, 1), loc="upper left", fontsize=7)
    ax.coords[0].set_axislabel("Right Ascension (J2000)")
    ax.coords[1].set_axislabel("Declination (J2000)")
    jd0, jd1 = g["jd"]
    ax.set_title(pad=28, label=f"Satellite passes, {utc_str(jd0)} – "
                 f"{jd_to_utc(jd1).strftime('%H:%M:%S')} UTC, "
                 f"radius {g['radius']:.1f}°")
    ax.grid(color="lightgray", linestyle="--")
    fig.savefig(path, dpi=100, bbox_inches="tight")
    plt.close(fig)


# --------------------------------------------------------------------------- #
# Stacked composite (one image per query group)
# --------------------------------------------------------------------------- #

STACK_MEDIAN_FRAMES = 30   # frames sampled for the median (background) stack


def _poly_terms(x, y, width, height):
    u, v = x / width, y / height
    return np.stack([np.ones_like(u), u, v, u * u, u * v, v * v], -1)


def ref_to_frame_map(w_ref, w_i, width, height, grid=9):
    """Quadratic map from reference-frame pixels to frame-i pixels, fitted
    through the sky on a grid (both mappings are TAN; the quadratic absorbs
    the small difference between their tangent points). Returns (cx, cy)."""
    gx, gy = np.meshgrid(np.linspace(0, width - 1, grid), np.linspace(0, height - 1, grid))
    gx, gy = gx.ravel(), gy.ravel()
    ra, dec = w_ref.pixel_to_world_values(gx, gy)
    x, y = w_i.world_to_pixel_values(ra, dec)
    A = _poly_terms(gx, gy, width, height)
    cx, *_ = np.linalg.lstsq(A, x, rcond=None)
    cy, *_ = np.linalg.lstsq(A, y, rcond=None)
    return cx, cy


def warp_to_ref(img, cx, cy, terms):
    """Resample frame i onto the reference grid (nearest neighbour). `terms`
    is _poly_terms over the whole reference grid. Returns (warped, valid)."""
    h, w = img.shape
    xi = np.rint(terms @ cx).astype(np.int64)
    yi = np.rint(terms @ cy).astype(np.int64)
    valid = (xi >= 0) & (xi < w) & (yi >= 0) & (yi < h)
    out = np.zeros(xi.shape, np.float32)
    out[valid] = img[yi[valid], xi[valid]]
    return out, valid


def stack_group(zf, g, res, include=("fit", "interp")):
    """Align a query group's frames onto its middle frame and stack them.
    Returns None if fewer than two frames have a usable mapping, else a dict:
    max (brightest value per pixel), median (the steady sky), moving
    (max - median: what changed, i.e. satellites/planes/meteors), the
    reference result, and the member results."""
    width, height = res["size"]
    by_i = {r["i"]: r for r in res["results"]}
    members = [by_i[i] for i in g["idx"]
               if by_i[i]["wcs"] is not None
               and str(by_i[i]["wcs_source"]).split(":")[0] in include]
    if len(members) < 2:
        return None
    ref = members[len(members) // 2]
    yy, xx = np.mgrid[0:height, 0:width]
    terms = _poly_terms(xx.ravel().astype(float), yy.ravel().astype(float), width, height)
    step = max(1, len(members) // STACK_MEDIAN_FRAMES)
    mx = np.full(height * width, -1.0, np.float32)
    cover = np.zeros(height * width, np.int32)
    med_rows = []
    for k, r in enumerate(members):
        img = _load_frame(zf, r["rec"]["file"]).astype(np.float32)
        cx, cy = ref_to_frame_map(ref["wcs"], r["wcs"], width, height)
        warped, valid = warp_to_ref(img, cx, cy, terms)
        np.maximum(mx, np.where(valid, warped, -1.0), out=mx)
        cover += valid
        if k % step == 0:
            med_rows.append(np.where(valid, warped, np.nan))
    med = np.nanmedian(np.stack(med_rows), axis=0) if len(med_rows) > 1 else mx.copy()
    med = np.nan_to_num(med, nan=0.0)
    mx = np.where(cover > 0, mx, 0.0)
    moving = np.where(cover >= 2, np.clip(mx - med, 0, None), 0.0)
    shape = (height, width)
    return {"max": mx.reshape(shape), "median": med.reshape(shape),
            "moving": moving.reshape(shape), "ref": ref, "members": members}


def _track_ref_px(w_ref, track):
    x, y = w_ref.world_to_pixel_values(np.array([p[1] for p in track]),
                                       np.array([p[2] for p in track]))
    return np.column_stack([x, y])


def stack_scores(st, g, res):
    """Per satellite: brightness along its whole predicted pass in the moving
    image (sigmas). {name: score or None}."""
    width, height = res["size"]
    out = {}
    for name, track in g["tracks"].items():
        pts = _track_ref_px(st["ref"]["wcs"], track)
        inside = [p for p in pts if 0 <= p[0] < width and 0 <= p[1] < height]
        out[name] = (line_score(st["moving"], inside[0], inside[-1], extend=0.0)
                     if len(inside) >= 2 else None)
    return out


def draw_stack(st, g, res, path, which="moving", scores=None):
    """The stacked composite with every predicted pass as two rails along its
    whole track, and each frame's predicted dash drawn just outside the rails
    (so the real dashes, between the rails, can be compared with where and
    when they were predicted)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    width, height = res["size"]
    img = st[which]
    if which == "moving":
        top = float(np.percentile(img[img > 0], 99.9)) if np.any(img > 0) else 1.0
        disp = np.sqrt(np.clip(img / (top or 1.0), 0, 1))
    else:
        disp = stretch(img)
    fig = plt.figure(figsize=(width / 80, height / 80 + 0.6), dpi=100)
    ax = fig.add_axes([0, 0, 1, height / (height + 48)])
    ax.imshow(disp, cmap="gray", origin="upper", interpolation="nearest")
    ax.set_xlim(-0.5, width - 0.5)
    ax.set_ylim(height - 0.5, -0.5)
    ax.axis("off")
    colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]
    w_ref = st["ref"]["wcs"]
    for k, (name, track) in enumerate(g["tracks"].items()):
        c = colors[k % len(colors)]
        pts = _track_ref_px(w_ref, track)
        inside = (pts[:, 0] > -50) & (pts[:, 0] < width + 50) & \
                 (pts[:, 1] > -50) & (pts[:, 1] < height + 50)
        if inside.sum() < 2:
            continue
        p = pts[inside]
        d = np.gradient(p, axis=0)
        nrm = np.column_stack([-d[:, 1], d[:, 0]])
        nrm /= np.maximum(np.hypot(nrm[:, 0], nrm[:, 1]), 1e-9)[:, None]
        for sgn in (1, -1):
            q = p + sgn * RAIL_PX * nrm
            ax.plot(q[:, 0], q[:, 1], "-", color=c, lw=1.0)
        # Each frame's predicted dash, offset just outside one rail.
        n0 = nrm[len(nrm) // 2] * (RAIL_PX + 6)
        for r in st["members"]:
            s0, s1 = interp_track(track, r["jd"][0]), interp_track(track, r["jd"][1])
            if s0 and s1:
                (x0, x1), (y0, y1) = w_ref.world_to_pixel_values(
                    np.array([s0[0], s1[0]]), np.array([s0[1], s1[1]]))
                ax.plot([x0 + n0[0], x1 + n0[0]], [y0 + n0[1], y1 + n0[1]],
                        "-", color=c, lw=2.0, alpha=0.8)
        lbl = name
        if scores and scores.get(name) is not None:
            lbl += f"  ({scores[name]:.0f}σ)"
        ax.annotate(lbl, tuple(p[0]), color=c, fontsize=8, xytext=(8, -12),
                    textcoords="offset points",
                    bbox=dict(boxstyle="round,pad=0.2", fc="black", ec=c, alpha=0.7))
    jd0, jd1 = st["members"][0]["jd"][0], st["members"][-1]["jd"][1]
    kind = ("moving = brightest minus median, i.e. only what changed"
            if which == "moving" else "brightest value per pixel")
    fig.text(0.01, 1 - 0.3 * 48 / (height + 48),
             f"{len(st['members'])} frames stacked on {st['ref']['rec']['file']}   "
             f"{utc_str(jd0)} – {jd_to_utc(jd1).strftime('%H:%M:%S')} UTC   "
             f"({kind}; rails = predicted pass, outside dashes = predicted "
             f"position each exposure)", fontsize=8, va="top")
    fig.savefig(path, dpi=100, facecolor="white")
    plt.close(fig)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("archive")
    ap.add_argument("--out", help="output folder (default <archive>_sats)")
    ap.add_argument("--frames", help="1-based frame numbers, e.g. 5,10-20")
    ap.add_argument("--window", type=float, default=60.0,
                    help="max seconds of frames per SatChecker query (60)")
    ap.add_argument("--responses", help="read/write SatChecker responses here "
                                        "(default <out>/satchecker_responses.json)")
    ap.add_argument("--offline", action="store_true",
                    help="never query; use only --responses")
    ap.add_argument("--all-frames", action="store_true",
                    help="draw every frame, not just those with a satellite in it")
    ap.add_argument("--chart", action="store_true", help="also draw sky charts")
    ap.add_argument("--stack", action="store_true",
                    help="also align and stack each query group's frames into one "
                         "composite (satellites become dashed lines); use a larger "
                         "--window (e.g. 600) to stack a longer stretch")
    ap.add_argument("--sat-frame", choices=("icrs", "jnow"), default="icrs")
    ap.add_argument("--flip-roll", action="store_true",
                    help="approx WCS only: use -roll (convention check)")
    ap.add_argument("--base-url", default=sa.DEFAULT_BASE)
    ap.add_argument("--sleep", type=float, default=1.0)
    a = ap.parse_args(argv)

    out = a.out or os.path.splitext(a.archive)[0] + "_sats"
    os.makedirs(out, exist_ok=True)
    resp_path = a.responses or os.path.join(out, "satchecker_responses.json")
    cache = {}
    if os.path.exists(resp_path):
        with open(resp_path) as f:
            cache = json.load(f)

    def offline_fetch(params):
        raise SystemExit(f"--offline: no cached response for {params}")

    res = analyse(a.archive, a.frames, a.window, cache, a.sat_frame, a.flip_roll,
                  fetch=offline_fetch if a.offline else None, base=a.base_url,
                  sleep_s=a.sleep)
    with open(resp_path, "w") as f:
        json.dump(res["responses"], f)
    if res["session"].get("clock_plausible") is False:
        print("WARNING: the finder's clock looked wrong during this capture; "
              "predicted positions will be off.", file=sys.stderr)
    score_results(a.archive, res)

    n_img = 0
    with zipfile.ZipFile(a.archive) as zf, \
            open(os.path.join(out, "report.csv"), "w", newline="") as fcsv:
        wr = csv.writer(fcsv)
        wr.writerow(["frame", "exposure_start_utc", "exposure_s", "satellite",
                     "x_start", "y_start", "x_end", "y_end", "in_frame",
                     "score_sigma", "wcs_source", "wcs_rms_px"])
        for r in res["results"]:
            rec = r["rec"]
            for s in r["sats"]:
                seg = s.get("seg_px") or ((None, None), (None, None))
                wr.writerow([rec["file"], rec.get("exposure_start_utc"),
                             rec.get("exposure_s"), s["satellite"],
                             *[None if v is None else round(v, 1) for v in (*seg[0], *seg[1])],
                             s.get("in_frame"),
                             None if s.get("score") is None else round(s["score"], 1),
                             r["wcs_source"],
                             None if r["wcs_rms_px"] is None else round(r["wcs_rms_px"], 2)])
            if a.all_frames or any(s.get("in_frame") for s in r["sats"]):
                img = _load_frame(zf, rec["file"])
                stem = os.path.splitext(rec["file"])[0]
                draw_frame(img, r, os.path.join(out, f"{stem}_sats.png"))
                n_img += 1
    if a.chart:
        for gi, g in enumerate(res["groups"]):
            draw_chart(g, res["frames"], res, os.path.join(out, f"chart_{gi + 1}.png"))
    if a.stack:
        with zipfile.ZipFile(a.archive) as zf:
            for gi, g in enumerate(res["groups"]):
                st = stack_group(zf, g, res)
                if st is None:
                    print(f"stack {gi + 1}: fewer than two frames with a fitted "
                          f"mapping; skipped")
                    continue
                sc = stack_scores(st, g, res)
                for which, suffix in (("moving", "_moving"), ("max", "")):
                    draw_stack(st, g, res, os.path.join(out, f"stack_{gi + 1}{suffix}.png"),
                               which, sc)
                ranked = sorted(((v, k) for k, v in sc.items() if v is not None), reverse=True)
                print(f"stack {gi + 1}: {len(st['members'])} frames; "
                      + (", ".join(f"{k} {v:.0f}\u03c3" for v, k in ranked[:8])
                         or "no predicted pass crosses the frame"))

    hits = [(r["rec"]["file"], s) for r in res["results"] for s in r["sats"]
            if s.get("in_frame")]
    print(f"{len(res['results'])} frames, {len(res['groups'])} SatChecker queries, "
          f"{len(hits)} predicted crossings inside a frame")
    for fname, s in sorted(hits, key=lambda h: -(h[1].get("score") or -1e9))[:20]:
        sc = s.get("score")
        print(f"  {fname}  {s['satellite']}" + (f"  {sc:.0f}σ" if sc is not None else ""))
    print(f"wrote {n_img} images, report.csv and {os.path.basename(resp_path)} to {out}")


if __name__ == "__main__":
    main()
