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
    pixels, whether it falls in the frame, the score, rate (deg/s), compass
    direction of motion, whether it was sunlit, how the WCS was obtained,
    and its fit residual.
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
    stars. ``--window 600`` stacks ten-minute stretches. Straight trails in
    the moving image that no prediction explains are outlined in red and
    labelled "unidentified"; one whose brightness comes in regular bright
    knots (strobes/navigation lights) is flagged "flashing -> aircraft?".
  * ``trails.csv`` (``--stack``): every straight trail found in each stack,
    its length, number of dashes, rate (from the dash length; blank when the
    dashes merged into one line), flash ratio, and the satellite it matched.

Predictions you could not have seen are hidden by default and counted in the
console output: satellites in Earth's shadow (``--show-unlit``) and
near-stationary objects such as geostationary satellites, which don't streak
(``--show-stationary``; threshold ``--min-rate`` deg/s). Labels give each
satellite's direction of motion on the sky (compass point) and rate, and an
arrow marks the direction on the image.
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
    segment misses the frame.

    Uses a trimmed mean (the brightest 5% of samples dropped): a prediction
    that merely CROSSES a bright trail touches it for a few pixels only, which
    a plain mean let dominate (a crossing rocket body scored 69 sigma on real
    data). A median would also stop that, but it would miss stacked trails,
    which are dashed (gaps between exposures)."""
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
        v = np.sort(f[yi[ok], xi[ok]])
        v = v[:max(1, int(len(v) * 0.95))]
        val = (float(np.mean(v)) - med) / noise * math.sqrt(len(v))
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

# --------------------------------------------------------------------------- #
# Which predictions could actually be seen
# --------------------------------------------------------------------------- #

MIN_RATE_DEG_S = 0.02     # slower than this = near-stationary (GEO is ~0.004)
_COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _offline_astropy():
    """Never let astropy try to download Earth-orientation tables (the tool
    must work offline); the bundled tables are ample for a shadow test."""
    try:
        from astropy.utils import iers
        iers.conf.auto_download = False
    except Exception:
        pass


def sat_properties(resp, site, tracks):
    """Per satellite: {"rate_deg_s", "toward" (sky direction of motion, e.g.
    "SW"), "lit" (True / False = in Earth's shadow / None = unknown)}.

    The shadow test uses SatChecker's own altitude, azimuth and range at the
    middle of the pass, the Sun's position, and a cylindrical Earth shadow
    (penumbra ignored). Satellites in shadow can't be seen at all; at night
    that is most low-orbit objects."""
    out = {}
    for name, tr in tracks.items():
        if len(tr) >= 2:
            (j0, r0, d0), (j1, r1, d1) = tr[0], tr[-1]
            dt = (j1 - j0) * 86400.0
            rate = sep_deg(r0, d0, r1, d1) / dt if dt > 0 else None
            dra = ((r1 - r0 + 180.0) % 360.0 - 180.0) * math.cos(math.radians((d0 + d1) / 2))
            pa = math.degrees(math.atan2(dra, d1 - d0)) % 360.0     # E of N
            toward = _COMPASS[int((pa + 22.5) // 45) % 8]
        else:
            rate, toward = None, None
        out[name] = {"rate_deg_s": rate, "toward": toward, "lit": None}
    sats = ((resp or {}).get("data") or {}).get("satellites") if isinstance(resp, dict) else None
    if not isinstance(sats, dict) or not sats:
        return out
    mids = []
    for name, sd in sats.items():
        pos = [p for p in (sd or {}).get("positions") or []
               if all(k in p for k in ("altitude", "azimuth", "range_km", "julian_date"))]
        if name in out and pos:
            mids.append((name, pos[len(pos) // 2]))
    if not mids:
        return out
    try:
        _offline_astropy()
        import astropy.units as u
        from astropy.coordinates import AltAz, EarthLocation, GCRS, SkyCoord, get_sun
        from astropy.time import Time
        t = Time([m[1]["julian_date"] for m in mids], format="jd", scale="utc")
        loc = EarthLocation(lat=site["latitude_deg"] * u.deg, lon=site["longitude_deg"] * u.deg,
                            height=(site.get("elevation_m") or 0.0) * u.m)
        sat = SkyCoord(alt=[m[1]["altitude"] for m in mids] * u.deg,
                       az=[m[1]["azimuth"] for m in mids] * u.deg,
                       distance=[m[1]["range_km"] for m in mids] * u.km,
                       frame=AltAz(obstime=t, location=loc))
        g = sat.transform_to(GCRS(obstime=t)).cartesian.xyz.to(u.km).value.T
        sun = get_sun(t).cartesian.xyz.value.T
        sun = sun / np.linalg.norm(sun, axis=1)[:, None]
        along = np.sum(g * sun, axis=1)
        perp = np.linalg.norm(g - along[:, None] * sun, axis=1)
        for (name, _), a, pp in zip(mids, along, perp):
            out[name]["lit"] = not (a < 0 and pp < 6378.137)
    except Exception as e:                     # never fatal: just unknown
        print(f"note: shadow test skipped ({e})", file=sys.stderr)
    return out


def filter_tracks(tracks, props, show_unlit=False, show_stationary=False,
                  min_rate=MIN_RATE_DEG_S):
    """(visible tracks, {"unlit": [...], "stationary": [...]} hidden names)."""
    keep, hidden = {}, {"unlit": [], "stationary": []}
    for name, tr in tracks.items():
        p = props.get(name, {})
        if not show_unlit and p.get("lit") is False:
            hidden["unlit"].append(name)
        elif (not show_stationary and p.get("rate_deg_s") is not None
              and p["rate_deg_s"] < min_rate):
            hidden["stationary"].append(name)
        else:
            keep[name] = tr
    return keep, hidden


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
            sleep_s=1.0, show_unlit=False, show_stationary=False,
            min_rate=MIN_RATE_DEG_S):
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
        all_tracks = to_icrs_tracks(sa.parse_satellites(cache[k]), sat_frame)
        g["props"] = sat_properties(cache[k], site, all_tracks)
        g["tracks"], g["hidden"] = filter_tracks(all_tracks, g["props"], show_unlit,
                                                 show_stationary, min_rate)
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
                entry.update({k: g["props"].get(name, {}).get(k)
                              for k in ("rate_deg_s", "toward", "lit")})
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


def _motion_text(p):
    """'  NE 0.34°/s' for a label, from a properties dict (empty if unknown)."""
    out = ""
    if p.get("toward"):
        out += f"  \u2192{p['toward']}"
    if p.get("rate_deg_s") is not None:
        out += f" {p['rate_deg_s']:.2f}\u00b0/s"
    if p.get("lit") is False:
        out += " (in shadow)"
    return out


def _arrow(ax, tip_from, d, color, length=18):
    """An arrowhead just past `tip_from`, pointing along `d` (direction of
    motion), so the real trail between the rails stays uncovered."""
    d = np.asarray(d, float)
    n = float(np.hypot(*d))
    if n < 1e-9:
        return
    u = d / n
    a = np.asarray(tip_from, float) + u * 4
    ax.annotate("", xy=tuple(a + u * length), xytext=tuple(a),
                arrowprops=dict(arrowstyle="-|>", color=color, lw=1.2))


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
            _arrow(ax, (x1, y1), d, c)                              # direction
            lbl = s["satellite"]
            if s.get("score") is not None:
                lbl += f"  ({s['score']:.0f}σ)"
            lbl += _motion_text(s)
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


# --------------------------------------------------------------------------- #
# Trails nobody predicted (aircraft, uncatalogued or mis-predicted objects)
# --------------------------------------------------------------------------- #

TRAIL_SIGMA = 6.0          # moving-image threshold, in noise sigmas
TRAIL_MIN_DASHES = 4       # dashes needed on one line to call it a trail
TRAIL_LONG_PX = 60.0       # ...or a single dash at least this long
TRAIL_LINE_TOL_PX = 4.0    # dash centre's distance from the line
TRAIL_ANGLE_TOL_DEG = 12.0
TRAIL_MATCH_PX = 20.0      # trail-to-prediction distance for a match
TRAIL_MATCH_DEG = 10.0
FLASH_RATIO = 2.0         # along-trail peak/typical brightness suggesting strobes


def _label(mask):
    """4-connected components of a boolean image -> list of (ys, xs) arrays.
    Pure numpy/Python (no scipy); fine for the sparse masks used here."""
    h, w = mask.shape
    seen = np.zeros_like(mask, bool)
    out = []
    for y0, x0 in zip(*np.nonzero(mask)):
        if seen[y0, x0]:
            continue
        seen[y0, x0] = True
        stack, ys, xs = [(y0, x0)], [], []
        while stack:
            y, x = stack.pop()
            ys.append(y)
            xs.append(x)
            for yy, xx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= yy < h and 0 <= xx < w and mask[yy, xx] and not seen[yy, xx]:
                    seen[yy, xx] = True
                    stack.append((yy, xx))
        out.append((np.array(ys), np.array(xs)))
    return out


def _bg_noise(img):
    v = img[img > 0]
    if v.size < 10:
        return None, None
    med = float(np.median(v))
    return med, 1.4826 * float(np.median(np.abs(v - med))) or 1.0


def _blobs(img, sigma=TRAIL_SIGMA, min_px=3):
    """Bright blobs in the moving image with their shape: centroid, length
    along the major axis, elongation, orientation (deg, 0..180), flux."""
    med, noise = _bg_noise(img)
    if med is None:
        return []
    blobs = []
    for ys, xs in _label(img > med + sigma * noise):
        if len(ys) < min_px:
            continue
        wgt = img[ys, xs].astype(float) - med
        cx, cy = np.average(xs, weights=wgt), np.average(ys, weights=wgt)
        cov = np.cov(np.vstack([xs, ys]).astype(float)) if len(ys) > 2 else np.eye(2) * 0.1
        evals, evecs = np.linalg.eigh(cov)
        lmin, lmax = max(evals[0], 0.08), max(evals[1], 0.08)
        ax_ = evecs[:, 1]
        blobs.append({"x": float(cx), "y": float(cy),
                      "length": float(math.sqrt(12.0 * lmax)) + 1.0,
                      "ratio": float(math.sqrt(lmax / lmin)),
                      "angle": float(math.degrees(math.atan2(ax_[1], ax_[0])) % 180.0),
                      "flux": float(wgt.sum()), "npix": int(len(ys))})
    return blobs


def _angle_diff(a, b):
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def _flash_ratio(img, p0, p1, thr, half_width=3):
    """Peak (90th percentile) over typical (median) brightness along a trail,
    using only samples on the trail itself (gaps between dashes skipped).
    A satellite's trail is steady (about 1-1.5); an aircraft's strobes and
    navigation lights make regular bright knots (2-3 or more)."""
    h, w = img.shape
    p0, p1 = np.asarray(p0, float), np.asarray(p1, float)
    L = float(np.hypot(*(p1 - p0)))
    if L < 10:
        return None
    u_ = (p1 - p0) / L
    n_ = np.array([-u_[1], u_[0]])
    prof = []
    for s in np.arange(0.0, L, 1.0):
        best = 0.0
        for o in range(-half_width, half_width + 1):
            x, y = np.rint(p0 + u_ * s + n_ * o).astype(int)
            if 0 <= x < w and 0 <= y < h:
                best = max(best, float(img[y, x]))
        if best > thr:
            prof.append(best)
    if len(prof) < 10:
        return None
    p50 = float(np.percentile(prof, 50))
    return float(np.percentile(prof, 90)) / p50 if p50 > 0 else None


def detect_trails(img, min_dashes=TRAIL_MIN_DASHES, sigma=TRAIL_SIGMA):
    """Find straight dashed (or long continuous) trails in a stack's moving
    image. Stars leave round residuals; a moving object leaves one elongated
    dash per exposure, all on one line and all pointing along it. When the
    exposures nearly touch (short gaps, slow object) the dashes merge into one
    long blob, which counts too.

    Returns a list of {"p0", "p1" (end points, px), "angle", "n_dashes",
    "dash_px" (median dash length; for a merged trail, its whole length),
    "flash_ratio" (see _flash_ratio), "dashes"}."""
    med, noise = _bg_noise(img)
    dashes = [b for b in _blobs(img, sigma) if b["ratio"] >= 2.5 and b["length"] >= 6.0]
    dashes.sort(key=lambda b: -b["flux"])
    used = set()
    trails = []
    for i, b in enumerate(dashes):
        if i in used:
            continue
        th = math.radians(b["angle"])
        u_ = np.array([math.cos(th), math.sin(th)])
        n_ = np.array([-u_[1], u_[0]])
        members = [j for j, c in enumerate(dashes)
                   if j not in used
                   and abs(np.dot([c["x"] - b["x"], c["y"] - b["y"]], n_)) <= TRAIL_LINE_TOL_PX
                   and _angle_diff(c["angle"], b["angle"]) <= TRAIL_ANGLE_TOL_DEG]
        if len(members) < min_dashes and b["length"] < TRAIL_LONG_PX:
            continue
        ms = [dashes[j] for j in members]
        # Refit the line through the member centres, weighted by length.
        pts = np.array([[c["x"], c["y"]] for c in ms])
        if len(ms) >= 2:
            c0 = pts.mean(axis=0)
            _, _, vt = np.linalg.svd(pts - c0)
            u_ = vt[0]
        else:
            c0 = pts[0]
        t = (pts - c0) @ u_
        half = np.array([c["length"] / 2 for c in ms])
        p0, p1 = c0 + u_ * float((t - half).min()), c0 + u_ * float((t + half).max())
        trails.append({"p0": (float(p0[0]), float(p0[1])), "p1": (float(p1[0]), float(p1[1])),
                       "angle": float(math.degrees(math.atan2(u_[1], u_[0])) % 180.0),
                       "n_dashes": len(ms),
                       "dash_px": float(np.median([c["length"] for c in ms])),
                       "flash_ratio": _flash_ratio(img, p0, p1, med + sigma * noise),
                       "dashes": ms})
        used.update(members)
    return trails


def trail_kinematics(zf, st, res, trails, sigma=TRAIL_SIGMA, half_width=4):
    """When and where along each trail the object was, from the individual
    frames: each member frame is re-aligned onto the stack, the median sky
    subtracted, and the bright samples on the trail averaged into one
    position. A straight-line fit of position against exposure mid-time gives
    the speed and which way it moved, even when the dashes merged into one
    line. Sets t["px_per_s"] (signed, along t["u"] = p0->p1) and t["n_seen"]
    (frames it was seen in); leaves them None with fewer than 3 frames."""
    if not trails:
        return trails
    width, height = res["size"]
    med_img = st["median"]
    _, noise = _bg_noise(st["moving"])
    thr = sigma * (noise or 1.0)
    lines = []
    for t in trails:
        p0, p1 = np.array(t["p0"]), np.array(t["p1"])
        L = float(np.hypot(*(p1 - p0))) or 1.0
        u_ = (p1 - p0) / L
        n_ = np.array([-u_[1], u_[0]])
        ss = np.arange(0.0, L, 1.0)
        oo = np.arange(-half_width, half_width + 1)
        S, O = np.meshgrid(ss, oo, indexing="ij")
        pts = p0[None, None, :] + S[..., None] * u_ + O[..., None] * n_
        xr = np.clip(np.rint(pts[..., 0]).astype(int), 0, width - 1)
        yr = np.clip(np.rint(pts[..., 1]).astype(int), 0, height - 1)
        lines.append((u_, ss, xr, yr, []))
        t["u"] = (float(u_[0]), float(u_[1]))
    for r in st["members"]:
        img = _load_frame(zf, r["rec"]["file"]).astype(np.float32)
        cx, cy = ref_to_frame_map(st["ref"]["wcs"], r["wcs"], width, height)
        for u_, ss, xr, yr, obs in lines:
            # Map the strip's reference pixels into this frame directly
            # (cheaper than warping the whole frame).
            terms = _poly_terms(xr.ravel().astype(float), yr.ravel().astype(float),
                                width, height)
            fx = np.rint(terms @ cx).astype(int).reshape(xr.shape)
            fy = np.rint(terms @ cy).astype(int).reshape(xr.shape)
            ok = (fx >= 0) & (fx < width) & (fy >= 0) & (fy < height)
            v = np.where(ok, img[np.clip(fy, 0, height - 1), np.clip(fx, 0, width - 1)]
                         - med_img[yr, xr], 0.0)
            prof = v.max(axis=1)
            hit = prof > thr
            if hit.sum() >= 3:
                wgt = prof[hit]
                obs.append(((r["jd"][0] + r["jd"][1]) / 2,
                            float(np.average(ss[hit], weights=wgt))))
    for t, (_, _, _, _, obs) in zip(trails, lines):
        t["n_seen"] = len(obs)
        t["px_per_s"] = None
        if len(obs) >= 3:
            jd = np.array([o[0] for o in obs])
            pos = np.array([o[1] for o in obs])
            tsec = (jd - jd[0]) * 86400.0
            if np.ptp(tsec) > 0:
                t["px_per_s"] = float(np.polyfit(tsec, pos, 1)[0])
    return trails


def _predicted_px_per_s(w_ref, track, k):
    """Predicted velocity (px/s, 2-vector) along a track at sample k."""
    i0, i1 = max(0, k - 1), min(len(track) - 1, k + 1)
    if i1 == i0:
        return None
    (x0, x1), (y0, y1) = w_ref.world_to_pixel_values(
        np.array([track[i0][1], track[i1][1]]), np.array([track[i0][2], track[i1][2]]))
    dt = (track[i1][0] - track[i0][0]) * 86400.0
    return np.array([x1 - x0, y1 - y0]) / dt if dt > 0 else None


def match_trails(trails, st, g, res, max_px=TRAIL_MATCH_PX, max_deg=TRAIL_MATCH_DEG):
    """Attach the predicted satellite (if any) each trail lies along:
    trail["satellite"] = name or None. When the trail's own motion is known
    (trail_kinematics) the prediction must also move the SAME WAY at a
    similar speed (within 2x); a prediction that lies on the line but runs
    the other way is recorded in trail["rejected"] instead."""
    w_ref = st["ref"]["wcs"]
    paths = {name: _track_ref_px(w_ref, tr) for name, tr in g["tracks"].items()}
    for t in trails:
        mid = (np.array(t["p0"]) + np.array(t["p1"])) / 2
        best = None
        t["rejected"] = []
        for name, p in paths.items():
            if len(p) < 2:
                continue
            k = int(np.argmin(np.hypot(p[:, 0] - mid[0], p[:, 1] - mid[1])))
            a, b = p[max(0, k - 1)], p[min(len(p) - 1, k + 1)]
            d = b - a
            L = float(np.hypot(*d)) or 1.0
            dist = abs((mid[0] - a[0]) * d[1] - (mid[1] - a[1]) * d[0]) / L
            ang = math.degrees(math.atan2(d[1], d[0])) % 180.0
            if dist > max_px or _angle_diff(ang, t["angle"]) > max_deg:
                continue
            if t.get("px_per_s") is not None and t.get("u") is not None:
                vel = _predicted_px_per_s(w_ref, g["tracks"][name], k)
                if vel is not None:
                    along = float(np.dot(vel, t["u"]))
                    seen = t["px_per_s"]
                    if along * seen <= 0:
                        t["rejected"].append((name, "moves the opposite way"))
                        continue
                    ratio = abs(seen) / (abs(along) or 1e-9)
                    if not 0.5 <= ratio <= 2.0:
                        t["rejected"].append((name, f"speed differs {ratio:.1f}x"))
                        continue
            if best is None or dist < best[0]:
                best = (dist, name)
        t["satellite"] = best[1] if best else None
    return trails


def trail_rate_deg_s(t, plate_deg_px, exposure_s):
    """Angular rate: from the frame-by-frame motion when trail_kinematics
    measured it, else from the dash length (one dash = one exposure). None
    for a merged trail with no timing: its length spans many exposures."""
    if not plate_deg_px:
        return None
    if t.get("px_per_s") is not None:
        return abs(t["px_per_s"]) * plate_deg_px
    if not exposure_s or t["n_dashes"] < 2:
        return None
    return t["dash_px"] * plate_deg_px / exposure_s


def trail_direction(t, w_ref):
    """Compass direction of motion on the sky ("NE", ...), or None."""
    if t.get("px_per_s") is None or t.get("u") is None or w_ref is None:
        return None
    sgn = 1.0 if t["px_per_s"] > 0 else -1.0
    mid = (np.array(t["p0"]) + np.array(t["p1"])) / 2
    a = mid - sgn * np.array(t["u"]) * 10
    b = mid + sgn * np.array(t["u"]) * 10
    (r0, r1), (d0, d1) = w_ref.pixel_to_world_values(np.array([a[0], b[0]]),
                                                     np.array([a[1], b[1]]))
    dra = ((r1 - r0 + 180.0) % 360.0 - 180.0) * math.cos(math.radians((d0 + d1) / 2))
    pa = math.degrees(math.atan2(dra, d1 - d0)) % 360.0
    return _COMPASS[int((pa + 22.5) // 45) % 8]


def trail_label(t):
    if t.get("satellite"):
        return t["satellite"]
    if t.get("flash_ratio") is not None and t["flash_ratio"] >= FLASH_RATIO:
        return "unidentified (flashing → aircraft?)"
    return "unidentified"


def draw_stack(st, g, res, path, which="moving", scores=None, trails=None):
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
        _arrow(ax, tuple(p[-1]), p[-1] - p[-2], c)
        lbl = name
        if scores and scores.get(name) is not None:
            lbl += f"  ({scores[name]:.0f}σ)"
        lbl += _motion_text(g.get("props", {}).get(name, {}))
        ax.annotate(lbl, tuple(p[0]), color=c, fontsize=8, xytext=(8, -12),
                    textcoords="offset points",
                    bbox=dict(boxstyle="round,pad=0.2", fc="black", ec=c, alpha=0.7))
    for t in trails or []:
        if t.get("satellite"):
            continue                  # already drawn as its prediction
        (x0, y0), (x1, y1) = t["p0"], t["p1"]
        d = np.array([x1 - x0, y1 - y0], float)
        nv = np.array([-d[1], d[0]]) / (np.hypot(*d) or 1.0) * (RAIL_PX + 4)
        for sgn in (1, -1):
            ax.plot([x0 + sgn * nv[0], x1 + sgn * nv[0]],
                    [y0 + sgn * nv[1], y1 + sgn * nv[1]], "--", color="red", lw=1.0)
        # Label on one side of the trail's middle, direction arrow on the
        # other (the ends are often at the frame edge).
        mid = np.array([(x0 + x1) / 2, (y0 + y1) / 2])
        side = nv / (np.hypot(*nv) or 1.0)
        if side[0] < 0 or (abs(side[0]) < 1e-6 and side[1] < 0):
            side = -side
        if t.get("px_per_s") is not None and t.get("u") is not None:
            u = np.array(t["u"]) * (1 if t["px_per_s"] > 0 else -1)
            a0 = mid - side * (RAIL_PX + 14)
            _arrow(ax, tuple(a0 - u * 16), u, "red", length=28)
        lbl = trail_label(t)
        toward = trail_direction(t, st["ref"]["wcs"])
        rate = t.get("rate_deg_s")
        if toward:
            lbl += f"  \u2192{toward}"
        if rate is not None:
            lbl += f" {rate:.2f}\u00b0/s"
        ax.annotate(lbl, tuple(mid + side * (RAIL_PX + 10)), color="white", fontsize=8,
                    va="center", ha="left" if side[0] > 0.3 else "center",
                    bbox=dict(boxstyle="round,pad=0.2", fc="darkred", ec="red", alpha=0.85))
    jd0, jd1 = st["members"][0]["jd"][0], st["members"][-1]["jd"][1]
    kind = ("moving = brightest minus median, i.e. only what changed"
            if which == "moving" else "brightest value per pixel")
    fig.text(0.01, 1 - 0.3 * 48 / (height + 48),
             f"{len(st['members'])} frames stacked on {st['ref']['rec']['file']}   "
             f"{utc_str(jd0)} – {jd_to_utc(jd1).strftime('%H:%M:%S')} UTC\n"
             f"({kind}; rails = predicted pass, outside dashes = predicted "
             f"position each exposure, red dashed = trail nobody predicted)",
             fontsize=8, va="top")
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
    ap.add_argument("--show-unlit", action="store_true",
                    help="also show satellites in Earth's shadow (invisible; "
                         "hidden by default)")
    ap.add_argument("--show-stationary", action="store_true",
                    help="also show near-stationary objects (geostationary etc.; "
                         "hidden by default: they don't streak)")
    ap.add_argument("--min-rate", type=float, default=MIN_RATE_DEG_S,
                    help=f"deg/s below which an object counts as stationary "
                         f"({MIN_RATE_DEG_S})")
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
                  sleep_s=a.sleep, show_unlit=a.show_unlit,
                  show_stationary=a.show_stationary, min_rate=a.min_rate)
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
                     "score_sigma", "rate_deg_s", "toward", "lit",
                     "wcs_source", "wcs_rms_px"])
        for r in res["results"]:
            rec = r["rec"]
            for s in r["sats"]:
                seg = s.get("seg_px") or ((None, None), (None, None))
                wr.writerow([rec["file"], rec.get("exposure_start_utc"),
                             rec.get("exposure_s"), s["satellite"],
                             *[None if v is None else round(v, 1) for v in (*seg[0], *seg[1])],
                             s.get("in_frame"),
                             None if s.get("score") is None else round(s["score"], 1),
                             None if s.get("rate_deg_s") is None else round(s["rate_deg_s"], 4),
                             s.get("toward"), s.get("lit"),
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
    for gi, g in enumerate(res["groups"]):
        hid = g.get("hidden") or {}
        if hid.get("unlit") or hid.get("stationary"):
            print(f"query {gi + 1}: {len(g['tracks'])} predictions shown; hidden "
                  f"{len(hid.get('unlit', []))} in Earth's shadow (--show-unlit), "
                  f"{len(hid.get('stationary', []))} near-stationary (--show-stationary)")
    if a.stack:
        cam = res["session"].get("camera") or {}
        plate = (cam.get("fov_width_deg") or 0) / res["size"][0] or None
        exps = [r["rec"].get("exposure_s") for r in res["results"] if r["rec"].get("exposure_s")]
        exp_s = float(np.median(exps)) if exps else None
        with zipfile.ZipFile(a.archive) as zf, \
                open(os.path.join(out, "trails.csv"), "w", newline="") as ftr:
            tw = csv.writer(ftr)
            tw.writerow(["stack", "x_start", "y_start", "x_end", "y_end", "length_px",
                         "n_dashes", "frames_seen", "rate_deg_s", "toward",
                         "flash_ratio", "identified_as", "rejected_predictions"])
            for gi, g in enumerate(res["groups"]):
                st = stack_group(zf, g, res)
                if st is None:
                    print(f"stack {gi + 1}: fewer than two frames with a fitted "
                          f"mapping; skipped")
                    continue
                sc = stack_scores(st, g, res)
                trails = match_trails(trail_kinematics(zf, st, res, detect_trails(st["moving"])),
                                      st, g, res)
                for t in trails:
                    t["rate_deg_s"] = trail_rate_deg_s(t, plate, exp_s)
                for which, suffix in (("moving", "_moving"), ("max", "")):
                    draw_stack(st, g, res, os.path.join(out, f"stack_{gi + 1}{suffix}.png"),
                               which, sc, trails)
                ranked = sorted(((v, k) for k, v in sc.items() if v is not None), reverse=True)
                print(f"stack {gi + 1}: {len(st['members'])} frames; "
                      + (", ".join(f"{k} {v:.0f}\u03c3" for v, k in ranked[:8])
                         or "no predicted pass crosses the frame"))
                for t in trails:
                    rate = t["rate_deg_s"]
                    L = float(np.hypot(t["p1"][0] - t["p0"][0], t["p1"][1] - t["p0"][1]))
                    fr = t.get("flash_ratio")
                    toward = trail_direction(t, st["ref"]["wcs"])
                    rej = "; ".join(f"{n}: {why}" for n, why in t.get("rejected", []))
                    tw.writerow([gi + 1, *[round(v, 1) for v in (*t["p0"], *t["p1"])],
                                 round(L, 1), t["n_dashes"], t.get("n_seen"),
                                 None if rate is None else round(rate, 4), toward,
                                 None if fr is None else round(fr, 2),
                                 t.get("satellite") or "", rej])
                    print(f"  trail {L:.0f} px, {t['n_dashes']} dash(es), seen in "
                          f"{t.get('n_seen')} frames"
                          + (f", {rate:.2f}\u00b0/s" if rate is not None else "")
                          + (f" toward {toward}" if toward else "")
                          + (f", flash ratio {fr:.1f}" if fr is not None else "")
                          + f": {trail_label(t)}"
                          + (f"  [not {rej}]" if rej else ""))

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
