"""Frame-by-frame moving-object tracks in a diofinder live capture (off-device;
used by sat_identify.py).

satcheck_overlay finds trails in STACKS: each query group's frames are aligned
on the stars and a satellite becomes a line of dashes. That works when the
finder is held still. Two cases it cannot handle:

* **A scope hand-guided to follow a satellite** (to keep it in view for a long
  arc). The object then stays near the same place in the frame and is a dot
  or a short dash, while the stars streak; the field moves several degrees,
  so satcheck_overlay's groups (frames within 1 deg of pointing) hold only a
  few frames each.
* **An arc longer than one group** (one pass crossing several fields).

This module works one frame at a time instead. For every frame with its own
WCS it builds the steady sky from neighbouring frames (aligned on the stars,
median of up to 2 x NEIGHBOURS_PER_SIDE frames at least NEIGHBOUR_MIN_SEP_S
away), subtracts it, and keeps what is left (anything that moved relative to
the stars). Each leftover blob gets an RA/Dec from that frame's WCS and a time
(exposure mid-point; a dash's light-weighted centre is its mid-exposure
position for a steady object). Blobs are then linked from frame to frame into
tracks that move smoothly across the sky (locally a great circle at a
smoothly changing rate), whatever the scope was doing.

Rejected on the way: star cores (bright in the sky median; twinkling residuals),
hot pixels and other fixed-pattern defects (blobs that recur at the same
PIXEL in many frames, wherever the scope pointed), single noise spikes (a
track needs MIN_TRACK_POINTS frames), and stationary objects (slower than
MIN_RATE_DEG_S).

Hand-tracking notes (see docs/next-release-todo.md):
  * Keep trail rejection off on the finder (``detect_max_axis_ratio`` 0, the
    shipped default): while panning, the stars are short trails and the
    solver must still use them.
  * Use short exposures (0.1-0.3 s) so the stars trail only a few pixels at
    the panning rate; a frame that does not solve has no WCS of its own and is
    left out here (borrowed/interpolated WCS is not good enough while the
    scope is being pushed by hand).
  * Plate solving while panning has not been tested on sky yet.
"""
from __future__ import annotations

import math

import numpy as np

import satcheck_overlay as so

LINK_SIGMA = 6.0             # residual threshold, noise sigmas
MIN_TRACK_POINTS = 5         # frames a track must be seen in
NEIGHBOURS_PER_SIDE = 3      # frames each side used for the sky median
NEIGHBOUR_MIN_SEP_S = 1.5    # ...at least this far away in time (object moved on)
NEIGHBOUR_MAX_SEP_S = 60.0
NEIGHBOUR_MAX_SEP_FOV = 0.6  # ...pointing within this fraction of the frame width
STAR_MASK_SIGMA = 8.0        # sky-median pixels this bright (star cores) are masked
STAR_MASK_GROW_PX = 3
HOT_RECUR_FRAC = 0.15        # same pixel in more than this fraction of frames = defect
HOT_RECUR_MIN = 4
MIN_RATE_DEG_S = 0.02
MAX_RATE_DEG_S = 3.0
LINK_TOL_DEG = 0.06          # prediction tolerance (+ a share of the step)
MAX_GAP_FRAMES = 3           # frames a track may go unseen
EDGE_PX = 4


def usable_frames(res, include=("fit",)):
    """Analysed frames with a WCS of the given sources, in time order."""
    rs = [r for r in res["results"] if r["wcs"] is not None
          and str(r["wcs_source"]).split(":")[0] in include and r["jd"][0] is not None]
    return sorted(rs, key=lambda r: r["jd"][0])


def _grow(mask, r):
    out = mask.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx * dx + dy * dy > r * r:
                continue
            sh = np.zeros_like(mask)
            ys = slice(max(0, dy), mask.shape[0] + min(0, dy))
            yd = slice(max(0, -dy), mask.shape[0] + min(0, -dy))
            xs = slice(max(0, dx), mask.shape[1] + min(0, dx))
            xd = slice(max(0, -dx), mask.shape[1] + min(0, -dx))
            sh[yd, xd] = mask[ys, xs]
            out |= sh
    return out


class FrameCache:
    """Loads frames once each, keeping the most recent few."""

    def __init__(self, zf, keep=16):
        self.zf, self.keep, self.d, self.order = zf, keep, {}, []

    def __call__(self, name):
        if name not in self.d:
            self.d[name] = so._load_frame(self.zf, name)
            self.order.append(name)
            if len(self.order) > self.keep:
                self.d.pop(self.order.pop(0), None)
        return self.d[name]


def _centre(r, width, height):
    ra, dec = r["wcs"].pixel_to_world_values((width - 1) / 2.0, (height - 1) / 2.0)
    return float(ra), float(dec)


def neighbours(rs, k, centres, fov_deg, per_side=NEIGHBOURS_PER_SIDE,
               min_sep_s=NEIGHBOUR_MIN_SEP_S, max_sep_s=NEIGHBOUR_MAX_SEP_S):
    mid = 0.5 * (rs[k]["jd"][0] + rs[k]["jd"][1])
    out = []
    for direction in (-1, 1):
        got, j = 0, k + direction
        while 0 <= j < len(rs) and got < per_side:
            mj = 0.5 * (rs[j]["jd"][0] + rs[j]["jd"][1])
            dt = abs(mj - mid) * 86400.0
            if dt > max_sep_s:
                break
            if dt >= min_sep_s and so.sep_deg(*centres[k], *centres[j]) <= \
                    NEIGHBOUR_MAX_SEP_FOV * fov_deg:
                out.append(j)
                got += 1
            j += direction
    return out


def residual(rs, k, nb, load, width, height, terms):
    """(frame - steady sky) for frame k on its own pixel grid, the sky
    median, and the mask of usable pixels (>= 2 neighbours, not a star core,
    not the frame edge). 8-bit units."""
    r = rs[k]
    img = load(r["rec"]["file"]).astype(np.float32)
    layers = []
    for j in nb:
        cx, cy = so.ref_to_frame_map(r["wcs"], rs[j]["wcs"], width, height)
        warped, valid = so.warp_to_ref(load(rs[j]["rec"]["file"]), cx, cy, terms)
        layers.append(np.where(valid, warped, np.nan))
    st = np.stack(layers)
    cnt = np.sum(np.isfinite(st), axis=0).reshape(height, width)
    with np.errstate(all="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            med = np.nanmedian(st, axis=0).reshape(height, width)
    med = np.nan_to_num(med, nan=0.0)
    ok = cnt >= 2
    sky = med[ok]
    if sky.size < 100:
        return None
    bg = float(np.median(sky))
    nz = 1.4826 * float(np.median(np.abs(sky - bg))) or 1.0
    stars = _grow(med > bg + STAR_MASK_SIGMA * max(nz, 1.0), STAR_MASK_GROW_PX)
    ok &= ~stars
    ok[:EDGE_PX, :] = ok[-EDGE_PX:, :] = False
    ok[:, :EDGE_PX] = ok[:, -EDGE_PX:] = False
    resid = np.where(ok, img - med, 0.0)
    return resid, med, ok


def frame_sources(resid, ok, sigma=LINK_SIGMA):
    """Blobs in a residual image (noise from the residual itself)."""
    v = resid[ok]
    if v.size < 100:
        return []
    m = float(np.median(v))
    noise = max(1.4826 * float(np.median(np.abs(v - m))), 1.0)
    return so._blobs(np.clip(resid, 0, None), sigma, min_px=3, stats=(m, noise))


def extract(zf, res, include=("fit",), sigma=LINK_SIGMA, log=None, on_frame=None):
    """Residual sources of every usable frame. Returns (frames, sources):
    frames = the usable results in time order; sources = list per frame of
    blob dicts with ra, dec, jd (exposure mid) and k (frame index).
    ``on_frame(k, r, resid, med, ok, blobs)`` is called per frame (photometry
    hooks in there while the residual is at hand)."""
    width, height = res["size"]
    rs = usable_frames(res, include)
    if len(rs) < 3:
        return rs, [[] for _ in rs]
    yy, xx = np.mgrid[0:height, 0:width]
    terms = so._poly_terms(xx.ravel().astype(float), yy.ravel().astype(float), width, height)
    cam = res["session"].get("camera") or {}
    fov = float(cam.get("fov_deg") or 13.5)
    centres = [_centre(r, width, height) for r in rs]
    load = FrameCache(zf, keep=4 * NEIGHBOURS_PER_SIDE + 4)
    out = []
    for k, r in enumerate(rs):
        nb = neighbours(rs, k, centres, fov)
        got = residual(rs, k, nb, load, width, height, terms) if len(nb) >= 2 else None
        blobs = []
        if got is not None:
            resid, med, ok = got
            blobs = frame_sources(resid, ok, sigma)
            jd = 0.5 * (r["jd"][0] + r["jd"][1])
            for b in blobs:
                ra, dec = r["wcs"].pixel_to_world_values(b["x"], b["y"])
                b.update(ra=float(ra), dec=float(dec), jd=float(jd), k=k,
                         file=r["rec"]["file"])
            if on_frame is not None:
                on_frame(k, r, resid, med, ok, blobs)
        out.append(blobs)
        if log and (k + 1) % 50 == 0:
            log(f"  residuals: {k + 1}/{len(rs)} frames")
    drop_fixed_pattern(out, len(rs))
    return rs, out


def drop_fixed_pattern(sources, n_frames, frac=HOT_RECUR_FRAC, min_count=HOT_RECUR_MIN):
    """Remove blobs at a pixel that has a blob in many frames (hot pixels,
    reflections, defects stay put in the FRAME whatever the sky does)."""
    counts = {}
    for blobs in sources:
        seen = set()
        for b in blobs:
            key = (int(b["x"] // 3), int(b["y"] // 3))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    kk = (key[0] + dx, key[1] + dy)
                    if kk not in seen:
                        seen.add(kk)
                        counts[kk] = counts.get(kk, 0) + 1
    limit = max(min_count, frac * n_frames)
    for i, blobs in enumerate(sources):
        sources[i] = [b for b in blobs
                      if counts.get((int(b["x"] // 3), int(b["y"] // 3)), 0) <= limit]


# --------------------------------------------------------------------------- #
# Linking
# --------------------------------------------------------------------------- #

def _vec(ra, dec):
    return so._vec(np.asarray(ra, float), np.asarray(dec, float))


def _ang(a, b):
    c = np.cross(a, b)
    return np.degrees(np.arctan2(np.linalg.norm(c, axis=-1), np.sum(a * b, axis=-1)))


def local_model(pts, t_ref):
    """Fit unit vectors pts (N,3) at times (s, relative) to a great circle with
    a quadratic angle-vs-time; returns a predictor f(t) -> unit vector and the
    rms residual (deg)."""
    P = np.array([p[0] for p in pts])
    t = np.array([p[1] for p in pts]) - t_ref
    if len(P) == 2:
        a, b = P
        n = np.cross(a, b)
        n /= np.linalg.norm(n) or 1.0
        w = math.radians(float(_ang(a, b))) / ((t[1] - t[0]) or 1e-9)

        def f(tt, a=a, n=n, w=w, t0=t[0]):
            th = w * (np.asarray(tt, float) - t0)
            return (np.multiply.outer(np.cos(th), a)
                    + np.multiply.outer(np.sin(th), np.cross(n, a)))
        return f, 0.0
    _, _, vt = np.linalg.svd(P)
    n = vt[2]
    a = P[0] - np.dot(P[0], n) * n
    a /= np.linalg.norm(a)
    bb = np.cross(n, a)
    th = np.unwrap(np.arctan2(P @ bb, P @ a))
    if th[-1] < th[0]:
        n, bb, th = -n, -bb, -th
    deg = 2 if len(P) >= 7 else 1
    cth = np.polyfit(t, th, deg)
    lat = np.arcsin(np.clip(P @ n, -1, 1))
    clat = np.polyfit(t, lat, 1 if len(P) >= 4 else 0)

    def f(tt, a=a, bb=bb, n=n, cth=cth, clat=clat):
        tt = np.asarray(tt, float)
        th_ = np.polyval(cth, tt)
        la = np.polyval(clat, tt)
        v = (np.multiply.outer(np.cos(la) * np.cos(th_), a)
             + np.multiply.outer(np.cos(la) * np.sin(th_), bb)
             + np.multiply.outer(np.sin(la), n))
        return v
    res = _ang(f(t), P)
    return f, float(np.sqrt(np.mean(res ** 2)))


def link(sources, times, min_points=MIN_TRACK_POINTS, max_gap=MAX_GAP_FRAMES,
         min_rate=MIN_RATE_DEG_S, max_rate=MAX_RATE_DEG_S, tol_deg=LINK_TOL_DEG,
         window=8):
    """Link per-frame sources into tracks. ``sources[k]`` = blobs of frame k
    (with ra, dec, jd); ``times[k]`` = frame k's exposure mid JD. Greedy:
    the brightest unused blob of the earliest frame seeds, every partner
    within max_gap frames at a plausible rate is tried, each candidate is
    extended frame by frame with a local fit of its last ``window`` points,
    and the longest wins. Returns [{"points": [blob...], "rms_deg",
    "rate_deg_s", "duration_s"}] longest first."""
    T = np.asarray(times, float) * 86400.0
    vec = [[_vec(b["ra"], b["dec"]) for b in bl] for bl in sources]
    used = [[False] * len(bl) for bl in sources]
    tracks = []
    n = len(sources)

    def extend(chain):
        k_last = chain[-1][0]
        miss = 0
        k = k_last + 1
        while k < n and miss <= max_gap:
            pts = [(vec[kk][ii], T[kk]) for kk, ii in chain[-window:]]
            f, _ = local_model(pts, T[chain[-1][0]])
            pred = f(T[k] - T[chain[-1][0]])
            dt = T[k] - T[chain[-1][0]]
            step = float(_ang(pts[-1][0], pts[-2][0])) / max(pts[-1][1] - pts[-2][1], 1e-6) * abs(dt)
            tol = tol_deg + 0.15 * step
            best = None
            for i, v in enumerate(vec[k]):
                if used[k][i]:
                    continue
                d = float(_ang(v, pred))
                if d <= tol and (best is None or d < best[0]):
                    best = (d, i)
            if best is not None:
                chain.append((k, best[1]))
                miss = 0
            else:
                miss += 1
            k += 1
        return chain

    for k0 in range(n):
        order = sorted(range(len(sources[k0])), key=lambda i: -sources[k0][i]["flux"])
        for i0 in order:
            if used[k0][i0]:
                continue
            best = None
            for k1 in range(k0 + 1, min(n, k0 + max_gap + 2)):
                dt = T[k1] - T[k0]
                if dt <= 0:
                    continue
                for i1, v1 in enumerate(vec[k1]):
                    if used[k1][i1]:
                        continue
                    rate = float(_ang(vec[k0][i0], v1)) / dt
                    if not min_rate <= rate <= max_rate:
                        continue
                    chain = extend([(k0, i0), (k1, i1)])
                    if len(chain) >= min_points and (best is None or len(chain) > len(best)):
                        best = chain
            if best is None:
                continue
            pts = [(vec[k][i], T[k]) for k, i in best]
            # Quality: residual of each point about a local fit around it.
            res = []
            for j in range(len(best)):
                lo, hi = max(0, j - window // 2), min(len(best), j + window // 2 + 1)
                ff, _ = local_model(pts[lo:hi], T[best[j][0]])
                res.append(float(_ang(ff(0.0), pts[j][0])))
            rms = float(np.sqrt(np.mean(np.square(res))))
            if rms > 3 * tol_deg:
                continue
            for k, i in best:
                used[k][i] = True
            span = T[best[-1][0]] - T[best[0][0]]
            tracks.append({"points": [sources[k][i] for k, i in best],
                           "frames": [k for k, _ in best],
                           "residual_deg": res,
                           "rms_deg": rms, "duration_s": float(span),
                           "rate_deg_s": float(_ang(pts[0][0], pts[-1][0])) / span
                           if span > 0 else 0.0})
    tracks.sort(key=lambda t: -len(t["points"]))
    return tracks


def track_observations(track, readout_ms=0.0, height=760):
    """sat_identify observation dicts for a linked track."""
    out = []
    for b, rr in zip(track["points"], track["residual_deg"]):
        jd = b["jd"] + (readout_ms / 1000.0) * (b["y"] / height) / 86400.0
        ph = b.get("phot")
        out.append({"jd": float(jd), "ra": b["ra"], "dec": b["dec"], "x": b["x"],
                    "y": b["y"], "file": b["file"], "resid_deg": rr,
                    "dash_px": b["length"], "phot": ph,
                    "full": bool(ph["full"]) if ph else True})
    return sorted(out, key=lambda o: o["jd"])


def same_object(obs_a, obs_b, max_deg=0.15, max_dt_s=1.0):
    """Fraction of obs_b matched by obs_a at (about) the same time and place."""
    if not obs_a or not obs_b:
        return 0.0
    ja = np.array([o["jd"] for o in obs_a])
    va = _vec([o["ra"] for o in obs_a], [o["dec"] for o in obs_a])
    hit = 0
    for o in obs_b:
        i = int(np.argmin(np.abs(ja - o["jd"])))
        if abs(ja[i] - o["jd"]) * 86400 <= max_dt_s and \
                float(_ang(va[i], _vec(o["ra"], o["dec"]))) <= max_deg:
            hit += 1
    return hit / len(obs_b)
