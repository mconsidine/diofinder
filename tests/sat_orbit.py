#!/usr/bin/env python3
"""Fit orbits to satellite tracks from diofinder captures, and predict where
to look next (off-device; companion to sat_identify.py).

Two commands:

``fit``  OBS.json [OBS.json ...]
    Fits SGP4 mean elements to one object's track(s) from sat_identify's
    ``observations.json`` (one per capture; each capture uses its own site).
    * One pass: a single 10-60 s arc from one site measures the direction and
      the angular rate well but not the distance, so a full 6-element orbit is
      undetermined. The fit then assumes a CIRCULAR orbit (4 elements: mean
      motion, inclination, node, mean argument of latitude), which is what
      SeeSat-L observers do for a first look at an unknown object. Expect the
      period good to roughly a percent: enough to know where to look on the
      next pass to within minutes, not seconds.
    * Two or more passes (hours or days apart): all 6 elements. Revolution
      counting between passes is resolved by scanning the mean motion. The
      period then comes out to well under a second and positions days ahead
      to a few km (see docs/next-release-todo.md for the simulation).
    * ``--start FILE`` begins from a known element set instead (an
      identification, or an old TLE of the object) and refines it.
    Writes ``fit.json`` (elements, covariance, residuals), ``fit.tle``
    (classic TLE; Alpha-5 for 100000-339999) and ``fit_omm.{json,xml,kvn}``.

``predict``  ELEMENTS (fit.json, TLE/3LE or OMM file, or a .zip such as classfd.zip)
    With a file of many objects (and no --norad / --name filter) every object
    is predicted and the passes are listed together in time order, one line
    each (--tables adds the pointing tables). Upcoming visible passes (satellite sunlit, observer in darkness, above
    --min-elevation) for a site, with rise / highest / set times, alt/az and
    RA/Dec (J2000, what the finder and SkySafari charts show), and a pointing
    table every --step seconds. From a fit.json the covariance gives each
    pass's timing uncertainty ("02:41:10 +- 85 s"), i.e. how early to be
    pointed and waiting.

    --sky east|west|north|south (or ne/nw/se/sw, or a comma list) keeps only
    passes lying ENTIRELY in that part of the sky (east/west of the meridian,
    north/south of the east-west line through the zenith), for a site where
    buildings hide the rest; --sky-mode part instead cuts every pass to the
    stretch inside it. ELEMENTS may also be a CCSDS OEM ephemeris (state
    vectors, KVN or XML), predicted only inside the span it covers.

``ephem``  ELEMENTS (as predict, or fit.json) -> a CCSDS OEM ephemeris:
    SGP4 state vectors every --step s over the window, frame TEME (default),
    GCRF/EME2000/ICRF or ITRF, KVN (ephemeris.oem) or --xml. See sat_oem.py.

    Times: --start/--end are this computer's LOCAL time unless --utc (or
    --in-tz ZONE), or the time itself carries "Z" or an offset. The printed
    times and predictions.txt/.csv use --out-tz (UTC by default, ready to
    share; --out-tz local to read them in local time). predictions.json is
    always UTC.

Examples:
  python3 tests/sat_orbit.py fit cap1_ident/observations.json --track L1
  python3 tests/sat_orbit.py fit cap1_ident/observations.json cap2_ident/observations.json
  python3 tests/sat_orbit.py predict out/fit.json --site-from cap1_ident/observations.json \\
      --start 2026-11-03T00:00:00 --hours 36 --min-elevation 25
  python3 tests/sat_orbit.py predict classfd.zip --lat 43.95 --lon -72.667 \\
      --start 17:00 --end 06:00 --out-tz utc        # tonight, local in, UTC out
  python3 tests/sat_orbit.py predict classfd.zip --lat 43.95 --lon -72.667 \
      --start 17:00 --end 06:00 --sky east           # only the eastern sky is open
  python3 tests/sat_orbit.py ephem out/fit.json --start 17:00 --hours 12 --frame GCRF

Needs numpy, astropy, sgp4 (as sat_identify). No network.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sat_identify as si  # noqa: E402

MU = 398600.8                        # km^3/s^2, WGS72 (SGP4's constants)
RE = 6378.135
MIN_PER_DAY = 1440.0
TWO_PI = 2.0 * math.pi


# --------------------------------------------------------------------------- #
# Observations
# --------------------------------------------------------------------------- #

class Pass:
    """One track from one capture: unit vectors, times, sigma, geometry."""

    def __init__(self, doc, track, pad_s=60.0):
        obs = sorted(track["observations"], key=lambda o: o["jd"])
        self.capture = doc.get("capture")
        self.id = track["id"]
        self.track = track
        self.site = doc["site"]
        self.jd = np.array([o["jd"] for o in obs], float)
        self.v = si.radec_to_vec([o["ra"] for o in obs], [o["dec"] for o in obs])
        sig = [o.get("sigma_deg") or track.get("sigma_pos_deg") or 0.02 for o in obs]
        self.sigma = np.radians(np.array(sig, float))
        # The clock error is common to the whole pass (not per point): it is
        # added to the covariance after the fit (add_clock_cov).
        self.clock_sigma_s = float((doc.get("clock") or {}).get("time_unc_s") or 0.0)
        self.geom = si.Geometry(self.site, self.jd[0] - pad_s / 86400.0,
                                self.jd[-1] + pad_s / 86400.0)
        self.identified = track.get("identified")

    @property
    def mid(self):
        return float(np.median(self.jd))


def load_passes(paths, track_ids=None):
    """Passes from observations.json files. ``track_ids``: one id for every
    file, or FILE:ID entries, or None (the longest track in each file)."""
    out = []
    for p in paths:
        with open(p) as f:
            doc = json.load(f)
        if doc.get("format") != "diofinder-sat-observations/1":
            raise SystemExit(f"{p}: not a sat_identify observations.json (re-run "
                             "sat_identify to get the current format)")
        tracks = doc["tracks"]
        want = None
        for t in track_ids or []:
            if ":" in t and os.path.abspath(t.rsplit(":", 1)[0]) == os.path.abspath(p):
                want = t.rsplit(":", 1)[1]
            elif ":" not in t:
                want = t
        if want is not None:
            sel = [t for t in tracks if t["id"] == want]
            if not sel:
                raise SystemExit(f"{p}: no track {want} (have {[t['id'] for t in tracks]})")
            tr = sel[0]
        else:
            if not tracks:
                raise SystemExit(f"{p}: no tracks")
            tr = max(tracks, key=lambda t: len(t["observations"]))
            if len(tracks) > 1:
                print(f"{p}: {len(tracks)} tracks, using the longest ({tr['id']}); "
                      "choose with --track")
        out.append(Pass(doc, tr))
    return sorted(out, key=lambda q: q.jd[0])


# --------------------------------------------------------------------------- #
# Element parameterization
# --------------------------------------------------------------------------- #
# p = [n (rad/min, Kozai), incl, node, ex = e cos w, ey = e sin w, lam = M + w]
# (non-singular for small e). Circular fits hold ex = ey = 0.

FULL = [0, 1, 2, 3, 4, 5]
CIRCULAR_N_FRAC = 0.045        # mean-motion model error of a circular fit (1 sigma)
CIRCULAR = [0, 1, 2, 5]
STEP = np.array([1e-7, 1e-6, 1e-6, 1e-6, 1e-6, 1e-6])


def make_satrec(p, epoch_jd, bstar=0.0, satnum=99999):
    from sgp4.api import Satrec, WGS72
    n, inc, node, ex, ey, lam = (float(x) for x in p)
    e = math.hypot(ex, ey)
    w = math.atan2(ey, ex) if e > 1e-9 else 0.0
    s = Satrec()
    s.sgp4init(WGS72, "i", int(satnum) if satnum <= 339999 else 0, epoch_jd - 2433281.5,
               bstar, 0.0, 0.0, min(e, 0.95), w % TWO_PI, inc, (lam - w) % TWO_PI, n,
               node % TWO_PI)
    return s


def satrec_to_p(s):
    w = s.argpo
    return np.array([s.no_kozai, s.inclo, s.nodeo, s.ecco * math.cos(w),
                     s.ecco * math.sin(w), s.mo + w])


def move_epoch(s, epoch_jd):
    """Element set s carried to a new epoch with SGP4's own secular rates."""
    dtm = (epoch_jd - (s.jdsatepoch + s.jdsatepochF)) * MIN_PER_DAY
    p = satrec_to_p(s)
    p[2] += s.nodedot * dtm
    w_new = s.argpo + s.argpdot * dtm
    e = s.ecco
    p[3], p[4] = e * math.cos(w_new), e * math.sin(w_new)
    p[5] = (s.mo + s.mdot * dtm) + w_new
    return p


# --------------------------------------------------------------------------- #
# Residuals and the least-squares fit
# --------------------------------------------------------------------------- #

def _basis(v):
    e1 = np.cross([0.0, 0.0, 1.0], v)
    n = np.linalg.norm(e1, axis=-1, keepdims=True)
    e1 = np.where(n > 1e-9, e1 / np.where(n > 0, n, 1), [1.0, 0.0, 0.0])
    return e1, np.cross(v, e1)


def residuals(p, passes, epoch_jd, bstar=0.0):
    """Tangent-plane residuals in units of each point's sigma, or None if
    SGP4 fails."""
    try:
        s = make_satrec(p, epoch_jd, bstar)
    except Exception:
        return None
    out = []
    for q in passes:
        e, r = si.propagate([s], q.jd)
        if np.any(e[0] != 0) or not np.all(np.isfinite(r)):
            return None
        u, _ = q.geom.topo_unit(r[0], q.jd)
        e1, e2 = _basis(q.v)
        d = u - q.v
        out += [np.sum(d * e1, -1) / q.sigma, np.sum(d * e2, -1) / q.sigma]
    return np.concatenate(out)


def add_clock_cov(cov, p, passes):
    """A clock error shifts a whole pass along its track: at the first pass
    it moves the mean longitude (n x dt); between the first and the last it
    changes the mean motion (n x (dt_last - dt_first) / interval)."""
    cov = cov.copy()
    n_s = p[0] / 60.0                                       # rad per second
    s1 = passes[0].clock_sigma_s
    cov[5, 5] += (n_s * s1) ** 2
    if len(passes) > 1:
        span = (passes[-1].mid - passes[0].mid) * 86400.0
        sl = passes[-1].clock_sigma_s
        if span > 0:
            cov[0, 0] += (p[0] ** 2) * (s1 ** 2 + sl ** 2) / span ** 2
    return cov


def lm_fit(p0, passes, epoch_jd, free, bstar=0.0, iterations=40, log=None):
    """Levenberg-Marquardt on the free parameters. Returns (p, cov (6x6, zeros
    for fixed parameters), chi2, dof) or None."""
    p = np.array(p0, float)
    r = residuals(p, passes, epoch_jd, bstar)
    if r is None:
        return None
    chi2 = float(r @ r)
    lam = 1e-3
    J = None
    for _ in range(iterations):
        step = np.zeros(len(free))
        J = np.empty((len(r), len(free)))
        for j, i in enumerate(free):
            dp = np.zeros(6)
            dp[i] = STEP[i] * (p[0] if i == 0 else 1.0)
            rk = residuals(p + dp, passes, epoch_jd, bstar)
            if rk is None:
                dp[i] = -dp[i]
                rk = residuals(p + dp, passes, epoch_jd, bstar)
                if rk is None:
                    return None
            J[:, j] = (rk - r) / dp[i]
        A, g = J.T @ J, J.T @ r
        improved = False
        for _ in range(12):
            try:
                step = -np.linalg.solve(A + lam * np.diag(np.diag(A) + 1e-30), g)
            except np.linalg.LinAlgError:
                lam *= 10
                continue
            pt = p.copy()
            pt[free] += step
            if math.hypot(pt[3], pt[4]) >= 0.9:
                lam *= 10
                continue
            rt = residuals(pt, passes, epoch_jd, bstar)
            if rt is not None and float(rt @ rt) < chi2:
                p, r, chi2 = pt, rt, float(rt @ rt)
                lam = max(lam / 5, 1e-9)
                improved = True
                break
            lam *= 10
        if not improved or np.max(np.abs(step) / (STEP[free] * 10)) < 1e-3:
            break
    dof = max(len(r) - len(free), 1)
    try:
        cf = np.linalg.pinv(J.T @ J) * max(1.0, chi2 / dof)
    except Exception:
        return None
    cov = np.zeros((6, 6))
    for a, i in enumerate(free):
        for b, k in enumerate(free):
            cov[i, k] = cf[a, b]
    return p, cov, chi2, dof


# --------------------------------------------------------------------------- #
# Initial orbits
# --------------------------------------------------------------------------- #

def circular_guess(q, epoch_jd):
    """Circular orbit through one pass: for a trial radius a every line of
    sight meets the sphere |r| = a once (above the observer); those points
    must lie on a plane through the Earth's centre and sweep it at
    n(a) = sqrt(mu/a^3). Scans a for that consistency. Returns p (TEME) and
    the radius, or None."""
    obs = q.geom.observer(q.jd)                       # GCRS km
    best = None
    t = (q.jd - epoch_jd) * 86400.0
    for a in np.geomspace(RE + 150.0, RE + 40000.0, 1500):
        b = np.sum(obs * q.v, -1)
        c = np.sum(obs * obs, -1) - a * a
        disc = b * b - c
        if np.any(disc < 0):
            continue
        rho = -b + np.sqrt(disc)
        P = obs + rho[:, None] * q.v
        _, _, vt = np.linalg.svd(P / a)
        nrm = vt[2]
        x_ = P[0] - np.dot(P[0], nrm) * nrm
        x_ /= np.linalg.norm(x_)
        y_ = np.cross(nrm, x_)
        th = np.unwrap(np.arctan2(P @ y_, P @ x_))
        if th[-1] < th[0]:
            nrm, y_, th = -nrm, -y_, -th
        w_fit, th0 = np.polyfit(t, th, 1)
        n_a = math.sqrt(MU / a ** 3)
        f = (w_fit - n_a) / n_a
        if best is not None and best[-1] is not None and np.sign(f) != np.sign(best[-1]) \
                and abs(f) < 0.5:
            # Bracketed: interpolate between this and the previous radius.
            a0, f0 = best[0], best[-1]
            a_root = a0 + (a - a0) * (0 - f0) / (f - f0)
            return _plane_to_p(q, a_root, epoch_jd), a_root
        best = (a, f)
    return None, None


def _plane_to_p(q, a, epoch_jd):
    obs = q.geom.observer(q.jd)
    b = np.sum(obs * q.v, -1)
    rho = -b + np.sqrt(b * b - (np.sum(obs * obs, -1) - a * a))
    P = obs + rho[:, None] * q.v
    P_teme = P @ q.geom.M                               # GCRS -> TEME (M orthonormal)
    _, _, vt = np.linalg.svd(P_teme)
    h = vt[2]
    if np.dot(np.cross(P_teme[0], P_teme[-1]), h) < 0:
        h = -h
    inc = math.acos(max(-1.0, min(1.0, h[2])))
    node = math.atan2(h[0], -h[1])
    nvec = np.array([math.cos(node), math.sin(node), 0.0])
    t = (q.jd - epoch_jd) * 86400.0
    k = int(np.argmin(np.abs(t)))
    r = P_teme[k] / np.linalg.norm(P_teme[k])
    u = math.atan2(np.dot(np.cross(nvec, r), h), np.dot(nvec, r))
    u += math.sqrt(MU / a ** 3) * (-t[k])               # back to the epoch
    n_rad_min = math.sqrt(MU / a ** 3) * 60.0
    return np.array([n_rad_min, inc, node % TWO_PI, 0.0, 0.0, u % TWO_PI])


def _align_lambda(p, passes, epoch_jd, bstar):
    """Best mean longitude for fixed other elements (coarse scan)."""
    best = None
    for lam in np.radians(np.arange(0, 360, 0.5)):
        pt = p.copy()
        pt[5] = lam
        r = residuals(pt, passes[:1], epoch_jd, bstar)
        if r is not None:
            c = float(r @ r)
            if best is None or c < best[0]:
                best = (c, lam)
    if best is not None:
        p = p.copy()
        p[5] = best[1]
    return p


def resolve_revolutions(p1, cov1, passes, epoch_jd, bstar, n_keep=4):
    """Starting from a fit to the first pass, scan the mean motion over the
    range its uncertainty (and a margin) allows; for each trial n, the mean
    longitude at the epoch is kept (the first pass fixes it) and the other
    passes' residuals are evaluated. Returns the best few trial p's."""
    span_min = (passes[-1].mid - epoch_jd) * MIN_PER_DAY
    if span_min <= 0:
        return [p1]
    n1 = p1[0]
    sig_n = math.sqrt(max(cov1[0, 0], 0.0))
    half = max(4 * sig_n, 0.10 * n1)      # an eccentric orbit fitted as circular
    step = math.radians(0.5) / span_min                     # 0.5 deg of phase at the end
    trials = np.arange(n1 - half, n1 + half + step / 2, step)
    if len(trials) > 20000:
        trials = np.linspace(n1 - half, n1 + half, 20000)
    scores = []
    for n in trials:
        pt = p1.copy()
        pt[0] = n
        r = residuals(pt, passes[1:], epoch_jd, bstar)
        scores.append(float(r @ r) if r is not None else np.inf)
    scores = np.array(scores)
    order = np.argsort(scores)
    picks = []
    for i in order:
        if not np.isfinite(scores[i]):
            break
        if all(abs(trials[i] - trials[j]) > 5 * step for j in picks):
            picks.append(i)
        if len(picks) >= n_keep:
            break
    out = []
    for i in picks:
        pt = p1.copy()
        pt[0] = trials[i]
        out.append(pt)
    return out


def fit(passes, start=None, circular=None, log=print):
    """Fit elements to passes. ``start`` = an ElementSet to begin from (else
    a circular orbit through the longest pass). ``circular`` None = decide:
    circular for a single pass, full otherwise. Returns a result dict."""
    if not passes:
        raise ValueError("no passes")
    first = max(passes, key=lambda q: len(q.jd)) if len(passes) == 1 else passes[0]
    epoch = first.mid
    bstar = float(start.satrec.bstar) if start is not None else 0.0
    single = len(passes) == 1
    circ = (single and start is None) if circular is None else bool(circular)
    # One pass from a known element set: keep its shape and period, fit only
    # where in its orbit the object is and the node (as sat_identify refine).
    free = CIRCULAR if circ else ([2, 5] if single and start is not None else FULL)
    if start is not None:
        p0 = move_epoch(start.satrec, epoch)
        if circ:
            p0[3] = p0[4] = 0.0
        p0 = _align_lambda(p0, [first], epoch, bstar)
        how = f"start: {start.name} ({start.source})"
    else:
        p0, a = circular_guess(first, epoch)
        if p0 is None:
            raise SystemExit("no circular orbit fits the first pass (too short, or not a "
                             "satellite?)")
        how = f"circular orbit through pass {first.id} (radius {a:.0f} km)"
    log(f"initial orbit: {how}")
    f1 = lm_fit(p0, [first], epoch, CIRCULAR if start is None else [2, 5], bstar)
    if f1 is None:
        raise SystemExit("the fit to the first pass failed")
    p1, c1, chi1, dof1 = f1
    best = (p1, c1, chi1, dof1)
    if len(passes) > 1:
        others = [q for q in passes if q is not first]
        ordered = [first] + others
        cands = resolve_revolutions(p1, c1, ordered, epoch, bstar)
        log(f"  {len(cands)} revolution-count candidate(s) across "
            f"{(passes[-1].mid - passes[0].mid) * 24:.1f} h")
        best = None
        for pt in cands:
            got = lm_fit(pt, ordered, epoch, CIRCULAR, bstar)
            if got is None:
                continue
            if not circ:
                # Eccentricity is not visible from one pass: start the full
                # fit from several shapes and keep the best.
                starts = [got[0]]
                for e0 in (0.01, 0.03, 0.06, 0.12):
                    for w0 in np.radians([0, 60, 120, 180, 240, 300]):
                        q0 = got[0].copy()
                        q0[3], q0[4] = e0 * math.cos(w0), e0 * math.sin(w0)
                        starts.append(q0)
                for q0 in starts:
                    g = lm_fit(q0, ordered, epoch, FULL, bstar)
                    if g is not None and (best is None or g[2] < best[2]):
                        best = g
            elif best is None or got[2] < best[2]:
                best = got
        if best is None:
            raise SystemExit("no orbit fits all passes together")
    p, cov, chi2, dof = best
    p = p.copy()
    p[2] %= TWO_PI
    p[5] %= TWO_PI
    cov = add_clock_cov(cov, p, [first] + [q for q in passes if q is not first])
    if circ:
        # Model error: real orbits are rarely exactly circular, and a circular
        # fit to one pass of an orbit with eccentricity e gets the mean motion
        # wrong by up to ~1.5 e (simulated: e = 0.046 -> 6 %). Allow for a
        # typical LEO e of 0.03.
        cov[0, 0] += (CIRCULAR_N_FRAC * p[0]) ** 2
    s = make_satrec(p, epoch, bstar)
    rms = {}
    for q in passes:
        e, r = si.propagate([s], q.jd)
        u, _ = q.geom.topo_unit(r[0], q.jd)
        rms[f"{q.capture}:{q.id}"] = float(np.sqrt(np.mean(si._ang(u, q.v) ** 2)))
    sd = np.sqrt(np.clip(np.diag(cov), 0, None))
    period_min = TWO_PI / p[0]
    return {"p": p, "cov": cov, "epoch_jd": epoch, "bstar": bstar, "chi2": chi2, "dof": dof,
            "circular": circ, "satrec": s, "rms_deg": rms, "how": how,
            "sigma": {"period_s": period_min * 60.0 * sd[0] / p[0],
                      "inclination_deg": math.degrees(sd[1]),
                      "node_deg": math.degrees(sd[2]),
                      "eccentricity": float(math.hypot(sd[3], sd[4])),
                      "mean_longitude_deg": math.degrees(sd[5])},
            "period_min": period_min,
            "passes": [{"capture": q.capture, "track": q.id, "n": len(q.jd),
                        "utc": si._utc(q.jd[0])} for q in passes]}


# --------------------------------------------------------------------------- #
# Uncertainty of a prediction
# --------------------------------------------------------------------------- #

def timing_sigma(p, cov, epoch_jd, bstar, jd, n_samples=200, seed=1):
    """Along-track timing (s) and cross-track (km) 1-sigma at times jd:
    timing from the mean longitude's linear growth (lam + n dt), cross-track
    by sampling the covariance (each sample's position compared with the
    nominal across the nominal velocity)."""
    jd = np.atleast_1d(np.asarray(jd, float))
    s0 = make_satrec(p, epoch_jd, bstar)
    e0, r0 = si.propagate([s0], jd)
    _, r1 = si.propagate([s0], jd + 1.0 / 86400.0)
    v = (r1[0] - r0[0])                                  # km per s
    speed = np.linalg.norm(v, axis=-1)
    vhat = v / speed[:, None]
    rng = np.random.default_rng(seed)
    al, cr = [], []
    for x in rng.multivariate_normal(p, cov, n_samples, check_valid="ignore"):
        if math.hypot(x[3], x[4]) >= 0.9 or x[0] <= 0:
            continue
        try:
            e, r = si.propagate([make_satrec(x, epoch_jd, bstar)], jd)
        except Exception:
            continue
        if np.any(e[0] != 0):
            continue
        d = r[0] - r0[0]
        a = np.sum(d * vhat, -1)
        al.append(a / speed)
        cr.append(np.linalg.norm(d - a[:, None] * vhat, axis=-1))
    # Along track: linear propagation of the mean longitude, lam + n dt (does
    # not wrap round the orbit the way sampled positions do).
    dtm = (jd - epoch_jd) * MIN_PER_DAY
    var = cov[5, 5] + 2 * dtm * cov[0, 5] + dtm ** 2 * cov[0, 0]
    t_sig = np.sqrt(np.clip(var, 0, None)) / p[0] * 60.0
    if not al:
        return t_sig, np.full(len(jd), np.nan)
    return np.maximum(t_sig, np.std(np.array(al), axis=0)), \
        np.sqrt(np.mean(np.square(cr), axis=0))


# --------------------------------------------------------------------------- #
# Prediction
# --------------------------------------------------------------------------- #

def _sun_dirs(jd):
    si.so._offline_astropy()
    from astropy.coordinates import get_sun
    from astropy.time import Time
    s = get_sun(Time(jd, format="jd", scale="utc")).cartesian.xyz.value.T
    return s / np.linalg.norm(s, axis=-1, keepdims=True)


def altaz(u, zen):
    """Altitude/azimuth (deg) of GCRS unit vectors u for zenith zen (J2000
    pole as north: within ~0.4 deg of the true horizon frame, plenty for
    pointing a finder)."""
    pole = np.array([0.0, 0.0, 1.0])
    north = pole - np.sum(pole * zen, -1, keepdims=True) * zen
    north /= np.linalg.norm(north, axis=-1, keepdims=True)
    east = np.cross(north, zen)
    alt = np.degrees(np.arcsin(np.clip(np.sum(u * zen, -1), -1, 1)))
    az = np.degrees(np.arctan2(np.sum(u * east, -1), np.sum(u * north, -1))) % 360.0
    return alt, az


class Sky:
    """The time grid, observer, zenith and Sun for a prediction window,
    computed once and shared by every object predicted over it."""

    def __init__(self, site, jd_start, hours=24.0, step_s=10.0):
        self.jd = jd_start + np.arange(0, hours * 3600.0 + step_s, step_s) / 86400.0
        self.geom = si.Geometry(site, self.jd[0], self.jd[-1])
        self.zen = self.geom.zenith(self.jd)
        sun = _sun_dirs(self.jd[::6])
        self.sun = np.repeat(sun, 6, axis=0)[:len(self.jd)]
        self.sun_alt = np.degrees(np.arcsin(np.clip(np.sum(self.sun * self.zen, -1), -1, 1)))


SKY_HALVES = {"east": "east of the meridian", "west": "west of the meridian",
              "north": "north of the east-west line through the zenith",
              "south": "south of the east-west line through the zenith"}
_SKY_ALIASES = {"e": ("east",), "w": ("west",), "n": ("north",), "s": ("south",),
                "ne": ("north", "east"), "nw": ("north", "west"), "se": ("south", "east"),
                "sw": ("south", "west")}


def parse_sky(text):
    """'east', 'north,west', 'ne', ... -> a tuple of halves (their
    intersection is the allowed part of the sky), or None for all of it."""
    if not text:
        return None
    halves = []
    for part in str(text).lower().replace(" ", "").split(","):
        if not part or part in ("all", "any"):
            continue
        got = _SKY_ALIASES.get(part, (part,))
        for h in got:
            if h not in SKY_HALVES:
                raise SystemExit(f"--sky {text!r}: use east, west, north, south (or ne, nw, "
                                 "se, sw; comma lists combine, e.g. east,north)")
            halves.append(h)
    halves = tuple(dict.fromkeys(halves))
    if {"east", "west"} <= set(halves) or {"north", "south"} <= set(halves):
        raise SystemExit(f"--sky {text!r}: opposite halves leave no sky")
    return halves or None


def in_sky(az, halves):
    """Boolean mask: azimuth (deg, N=0, E=90) inside every half in ``halves``."""
    az = np.radians(np.asarray(az, float))
    m = np.ones(az.shape, bool)
    for h in halves or ():
        m &= {"east": np.sin(az) > 0, "west": np.sin(az) < 0,
              "north": np.cos(az) > 0, "south": np.cos(az) < 0}[h]
    return m


def sky_text(halves, whole=True):
    if not halves:
        return ""
    where = " and ".join(SKY_HALVES[h] for h in halves)
    return (f"Sky: only passes entirely {where}" if whole else
            f"Sky: only the part of each pass {where}")


def _passes_from(sky, err, r, min_elevation=20.0, sun_max_alt=-6.0, fit_result=None,
                 table_step_s=30.0, region=None, region_whole=True):
    """Visible passes of one object from its propagated TEME positions r
    (N, 3) on the sky grid (err = SGP4 error codes). ``region``: halves of
    the sky (parse_sky); ``region_whole`` keeps only passes whose whole
    visible part lies there, otherwise each pass is cut to that part."""
    jd, geom = sky.jd, sky.geom
    ok = err == 0
    dark = sky.sun_alt <= sun_max_alt
    if not np.any(ok & dark):
        return []
    with np.errstate(invalid="ignore"):
        u, _ = geom.topo_unit(r, jd)
        alt, az = altaz(u, sky.zen)
        up = ok & dark & (alt >= min_elevation)
    if not np.any(up):
        return []
    g = geom.sat_gcrs(r)
    along = np.sum(g * sky.sun, -1)
    perp = np.linalg.norm(g - along[:, None] * sky.sun, axis=-1)
    lit = ~((along < 0) & (perp < si.EARTH_RADIUS_KM))
    vis = up & lit
    inside = in_sky(az, region) if region else None
    if region and not region_whole:
        vis = vis & inside
    # A gap in the grid (predict_all keeps only the dark times) ends a pass.
    dts = np.diff(jd)
    brk = np.r_[dts > 1.5 * np.min(dts), False] if len(jd) > 1 else np.zeros(len(jd), bool)
    passes = []
    i = 0
    while i < len(jd):
        if not vis[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(jd) and vis[j + 1] and not brk[j]:
            j += 1
        if region and (not np.all(inside[i:j + 1]) if region_whole else j == i):
            i = j + 1              # not wholly inside / a one-sample sliver of a pass
            continue
        k = i + int(np.argmax(alt[i:j + 1]))
        ra, dec = si.vec_to_radec(u[i:j + 1])
        tab = []
        last = None
        for m in range(i, j + 1):
            if last is None or (jd[m] - last) * 86400.0 >= table_step_s - 0.5 or m == j:
                tab.append({"utc": si._utc(jd[m]), "jd": float(jd[m]),
                            "alt": round(float(alt[m]), 1), "az": round(float(az[m]), 1),
                            "ra": round(float(ra[m - i]), 3), "dec": round(float(dec[m - i]), 3)})
                last = jd[m]
        kk = min(k + 1, len(jd) - 1)
        rate = float(si._ang(u[k], u[kk])) / max((jd[kk] - jd[k]) * 86400.0, 1e-9) \
            if kk != k else None
        p = {"start_utc": si._utc(jd[i]), "max_utc": si._utc(jd[k]), "end_utc": si._utc(jd[j]),
             "start_jd": float(jd[i]), "max_jd": float(jd[k]), "end_jd": float(jd[j]), "max_alt": round(float(alt[k]), 1),
             "max_az": round(float(az[k]), 1), "ra_at_max": round(float(ra[k - i]), 3),
             "dec_at_max": round(float(dec[k - i]), 3),
             "range_km_at_max": round(float(np.linalg.norm(g[k] - geom.observer(jd[k]))), 0),
             "rate_deg_s_at_max": round(rate, 3) if rate is not None else None,
             "table": tab, "timing_sigma_s": None, "cross_sigma_km": None}
        if fit_result is not None:
            ts, cs = timing_sigma(fit_result["p"], fit_result["cov"], fit_result["epoch_jd"],
                                  fit_result["bstar"], np.array([jd[k]]))
            period_s = TWO_PI / fit_result["p"][0] * 60.0
            if np.isfinite(ts[0]):
                p["timing_sigma_s"] = round(float(ts[0]), 1)
                # Beyond ~1/6 of a revolution the samples wrap round the orbit
                # and the spread stops meaning anything: the timing is unknown.
                p["timing_unknown"] = bool(ts[0] > period_s / 6.0)
                p["cross_sigma_km"] = round(float(cs[0]), 1)
                rng_km = p["range_km_at_max"]
                p["cross_sigma_deg"] = round(math.degrees(float(cs[0]) / rng_km), 2) \
                    if rng_km else None
        passes.append(p)
        i = j + 1
    return passes


def predict(satrec, site, jd_start, hours=24.0, min_elevation=20.0, step_s=10.0,
            sun_max_alt=-6.0, fit_result=None, table_step_s=30.0, sky=None, region=None,
            region_whole=True):
    """Visible passes of one object. Returns a list of dicts (rise/max/set,
    pointing table, timing sigma when a fit's covariance is given)."""
    sky = sky or Sky(site, jd_start, hours, step_s)
    e, r = si.propagate([satrec], sky.jd)
    return _passes_from(sky, e[0], r[0], min_elevation, sun_max_alt, fit_result, table_step_s,
                        region, region_whole)


def predict_oem(segments, site, jd_start, hours=24.0, min_elevation=20.0, step_s=10.0,
                sun_max_alt=-6.0, table_step_s=30.0, sky=None, region=None,
                region_whole=True):
    """Visible passes from a CCSDS OEM ephemeris (sat_oem.read_oem segments),
    every object in it, sorted by time; only inside the span it covers."""
    import sat_oem
    sky = sky or Sky(site, jd_start, hours, step_s)
    out = []
    for name, oid, segs in sat_oem.by_object(segments):
        e, r = sat_oem.teme_positions(segs, sky.jd)
        for p in _passes_from(sky, e, r, min_elevation, sun_max_alt, None, table_step_s,
                              region, region_whole):
            p.update(name=name, norad=None, norad_str=str(oid)[:9], source=segs[0].get("source"),
                     epoch_age_days=None)
            out.append(p)
    out.sort(key=lambda p: p["max_jd"])
    return out


MIN_REV_PER_DAY = 2.0     # slower objects (GEO, high orbits) are skipped by default


def dark_windows(sky, sun_max_alt=-6.0):
    """[(jd_start, jd_end)] stretches of the sky grid with the Sun below
    sun_max_alt at the site."""
    dark = sky.sun_alt <= sun_max_alt
    out, i = [], 0
    while i < len(dark):
        if dark[i]:
            j = i
            while j + 1 < len(dark) and dark[j + 1]:
                j += 1
            out.append((float(sky.jd[i]), float(sky.jd[j])))
            i = j + 1
        else:
            i += 1
    return out


def darkness_text(sky, sun_max_alt=-6.0):
    """One line saying when the site's sky is dark in the window (passes are
    only listed then)."""
    w = dark_windows(sky, sun_max_alt)
    if not w:
        return (f"NO DARK SKY at the site between {_hm(sky.jd[0], True)} and "
                f"{_hm(sky.jd[-1], True)} (Sun above {sun_max_alt:g} deg the whole time), so "
                "no pass can be listed. Start the window at dusk with --start (local time "
                "unless --utc), or give a later --end / more --hours.")
    parts = [f"{_hm(a, True, zone=False)} - {_hm(b)}" for a, b in w]
    return f"Dark sky (Sun below {sun_max_alt:g} deg) at the site: " + ", ".join(parts)


def predict_all(elems, site, jd_start, hours=24.0, min_elevation=20.0, step_s=10.0,
                sun_max_alt=-6.0, table_step_s=30.0, min_rev_per_day=MIN_REV_PER_DAY,
                batch=40, log=None, sky=None, region=None, region_whole=True,
                max_age_days=None):
    """Visible passes of every element set in ``elems``, merged and sorted by
    the time of highest elevation. Each pass also carries name, norad,
    source and the element set's age (days) at the pass. ``max_age_days``:
    skip element sets whose epoch is further than that from the window."""
    sky = sky or Sky(site, jd_start, hours, step_s)
    # Propagate only at dark times (nothing can be seen otherwise): a big
    # saving for large files and long windows.
    idx = np.nonzero(sky.sun_alt <= sun_max_alt)[0]
    if len(idx) == 0:
        return []
    if len(idx) < len(sky.jd):
        dark = Sky.__new__(Sky)
        dark.geom = sky.geom
        dark.jd, dark.zen = sky.jd[idx], sky.zen[idx]
        dark.sun, dark.sun_alt = sky.sun[idx], sky.sun_alt[idx]
        sky = dark
    keep = [e for e in elems
            if e.satrec.no_kozai * MIN_PER_DAY / TWO_PI >= (min_rev_per_day or 0)]
    if log and len(keep) < len(elems):
        log(f"{len(elems) - len(keep)} slow object(s) (< {min_rev_per_day:g} rev/day: "
            "geostationary, high orbits) skipped; --all-orbits to include them")
    if max_age_days:
        lo, hi = float(sky.jd[0]), float(sky.jd[-1])
        fresh = [e for e in keep
                 if max(lo - e.epoch_jd, e.epoch_jd - hi, 0.0) <= max_age_days]
        if log and len(fresh) < len(keep):
            log(f"{len(keep) - len(fresh)} element set(s) older than {max_age_days:g} days "
                "skipped (their timing is no longer useful, and long past a decay SGP4 "
                "invents positions); --max-age-days 0 to include them")
        keep = fresh
    out = []
    for b0 in range(0, len(keep), batch):
        chunk = keep[b0:b0 + batch]
        e, r = si.propagate([x.satrec for x in chunk], sky.jd)
        for i, el in enumerate(chunk):
            for p in _passes_from(sky, e[i], r[i], min_elevation, sun_max_alt, None,
                                  table_step_s, region, region_whole):
                p.update(name=el.name, norad=el.norad, norad_str=el.norad_str,
                         source=el.source,
                         epoch_age_days=round(p["max_jd"] - el.epoch_jd, 1))
                out.append(p)
    out.sort(key=lambda p: p["max_jd"])
    return out


def _rate(r):
    r = r or 0.0
    return f"{r:5.3f}" if r < 0.1 else f"{r:5.2f}"


def format_summary(passes, title, tables=False):
    """One line per pass (several objects), optional pointing tables."""
    lines = [f"{title}: {len(passes)} visible pass(es)"]
    if not passes:
        lines.append("  none in the window")
    lines.append("  highest at                    object                     number  alt  az   "
                 "RA       Dec     rate    visible              elements")
    for p in passes:
        lines.append(
            f"  {si.when(p['max_jd'], ms=False):<29} {p.get('name', '')[:26]:<26} "
            f"{p.get('norad_str', ''):>6} "
            f"{p['max_alt']:4.0f} {p['max_az']:4.0f}  {p['ra_at_max']:7.2f} "
            f"{p['dec_at_max']:+7.2f}  {_rate(p.get('rate_deg_s_at_max'))}/s  "
            f"{_clock(_jd(p, 'start'))}-{_clock(_jd(p, 'end'))}  "
            + (f"{p['epoch_age_days']:+.1f} d" if p.get("epoch_age_days") is not None
               else "ephemeris"))
        if tables:
            for row in p["table"]:
                lines.append(f"      {si.when(row['jd'], ms=False)}  alt {row['alt']:5.1f}  "
                             f"az {row['az']:5.1f}  RA {row['ra']:8.3f}  Dec {row['dec']:+7.3f}")
    if not passes or any(p.get("epoch_age_days") is not None for p in passes):
        lines.append("Elements a few days old or more: the path across the sky stays close, "
                     "but the TIME can be off by minutes (classified objects manoeuvre). Be "
                     "pointed early.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #

def save_fit(res, out, name, norad, intldesg, log=print):
    os.makedirs(out, exist_ok=True)
    s = res["satrec"]
    s.intldesg = intldesg or ""
    s.classification = "U"
    note = (f"diofinder sat_orbit {'circular ' if res['circular'] else ''}fit to "
            f"{len(res['passes'])} pass(es); period {res['period_min']:.3f} min "
            f"+- {res['sigma']['period_s']:.1f} s; epoch {si._utc(res['epoch_jd'])}")
    si.write_tle([((s, name, norad), note)], os.path.join(out, "fit.tle"))
    rec = si.omm_record(s, name, norad)
    si.write_omm_json([(rec, note)], os.path.join(out, "fit_omm.json"))
    si.write_omm_xml([(rec, note)], os.path.join(out, "fit_omm.xml"))
    si.write_omm_kvn([(rec, note)], os.path.join(out, "fit_omm.kvn"))
    doc = {"format": "diofinder-sat-orbit/1", "name": name, "norad": norad,
           "intldesg": intldesg, "epoch_jd": res["epoch_jd"], "epoch_utc": si._utc(res["epoch_jd"]),
           "bstar": res["bstar"], "p": list(map(float, res["p"])),
           "p_names": ["n_rad_per_min", "inclination", "node", "e_cos_w", "e_sin_w",
                       "mean_longitude"],
           "cov": res["cov"].tolist(), "chi2": res["chi2"], "dof": res["dof"],
           "circular": res["circular"], "how": res["how"], "rms_deg": res["rms_deg"],
           "sigma": res["sigma"], "period_min": res["period_min"], "passes": res["passes"],
           "tle": si.tle_lines(s, norad), "note": note}
    with open(os.path.join(out, "fit.json"), "w") as f:
        json.dump(doc, f, indent=1)
    log(note)
    return doc


def load_fit(path):
    with open(path) as f:
        doc = json.load(f)
    p = np.array(doc["p"], float)
    res = {"p": p, "cov": np.array(doc["cov"], float), "epoch_jd": doc["epoch_jd"],
           "bstar": doc.get("bstar", 0.0)}
    s = make_satrec(p, doc["epoch_jd"], res["bstar"], doc.get("norad") or 99999)
    return s, doc.get("name", "fit"), res


def _zoned(jd):
    return si.to_zone(si.so.jd_to_utc(jd), si.OUT_TZ)


def _hm(jd, date=False, zone=True):
    """'HH:MM' (or 'MM-DD HH:MM') in the output zone, with the zone after it."""
    t = _zoned(jd)
    text = t.strftime("%m-%d %H:%M" if date else "%H:%M")
    return f"{text} {si.zone_label(t)}" if zone else text


def _clock(jd):
    """'HH:MM:SS' in the output zone (no zone: it is in the column heading)."""
    return _zoned(jd).strftime("%H:%M:%S")


def _jd(p, which):
    """A pass's start/end JD (older predictions.json files only have text)."""
    v = p.get(f"{which}_jd")
    if v is not None:
        return v
    t = dt.datetime.strptime(p[f"{which}_utc"].replace(" UTC", ""), "%Y-%m-%d %H:%M:%S.%f")
    return 2440587.5 + t.replace(tzinfo=dt.timezone.utc).timestamp() / 86400.0


def format_passes(passes, name):
    lines = [f"Visible passes of {name}:"]
    if not passes:
        lines.append("  none in the window")
    for p in passes:
        unc = ""
        if p.get("timing_unknown"):
            unc = ("  timing UNKNOWN (a one-pass orbit is off by many minutes this far "
                   "ahead): a rough guide only; a second pass makes it usable")
        elif p.get("timing_sigma_s") is not None:
            unc = (f"  timing +- {p['timing_sigma_s']:.0f} s (1 sigma), cross-track "
                   f"+- {p.get('cross_sigma_deg')} deg")
        lines.append(f"  {si.when(_jd(p, 'start'), ms=False)} -> {_clock(_jd(p, 'end'))}: "
                     f"highest {p['max_alt']:.0f} deg "
                     f"at az {p['max_az']:.0f} ({_clock(p['max_jd'])}), RA {p['ra_at_max']:.2f} "
                     f"Dec {p['dec_at_max']:+.2f} (J2000), range {p['range_km_at_max']:.0f} km"
                     + unc)
        for t in p["table"]:
            lines.append(f"      {si.when(t['jd'], ms=False)}  alt {t['alt']:5.1f}  "
                         f"az {t['az']:5.1f}  RA {t['ra']:8.3f}  Dec {t['dec']:+7.3f}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def cmd_fit(a):
    passes = load_passes(a.observations, a.track)
    start = None
    if a.start:
        els = si.load_elements([a.start])
        if a.norad:
            els = [e for e in els if e.norad == si.parse_norad(a.norad)]
        if not els:
            raise SystemExit(f"{a.start}: no element set" + (f" for {a.norad}" if a.norad else ""))
        start = els[0]
    res = fit(passes, start, True if a.circular else (False if a.full else None))
    ident = next((q.identified for q in passes if q.identified), None)
    norad = si.parse_norad(a.norad) if a.norad else (ident["norad"] if ident else 99999)
    name = a.name or (ident["name"] if ident else si.unknown_name(passes[0].jd[0]))
    intl = a.intldesg if a.intldesg is not None else (
        ident.get("intldesg") if ident else "99099A")
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.observations[0])), "orbit")
    doc = save_fit(res, out, name, norad, intl)
    for k, v in res["rms_deg"].items():
        print(f"  {k}: rms {v:.4f} deg")
    sg = res["sigma"]
    print(f"  1 sigma: period {sg['period_s']:.1f} s, inclination {sg['inclination_deg']:.3f} "
          f"deg, node {sg['node_deg']:.3f} deg, e {sg['eccentricity']:.4f}")
    if doc["tle"]:
        print(f"{name}\n{doc['tle'][0]}\n{doc['tle'][1]}")
    print(f"written to {out}")


def _select(els, norad=None, name=None):
    if norad:
        want = si.parse_norad_list(norad)
        els = [e for e in els if e.norad in want]
    if name:
        els = [e for e in els if name.lower() in (e.name or "").lower()]
    return els


def window(start, end=None, hours=24.0, tz="local"):
    """(jd_start, hours) from the command line: start default now; an end
    replaces hours (a bare time of day before the start = the next day)."""
    import re
    jd0 = si.parse_time(start, tz) if start else \
        2440587.5 + dt.datetime.now(dt.timezone.utc).timestamp() / 86400.0
    if end:
        if re.fullmatch(r"\s*\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?\s*", end):
            # A bare time of day counts from the start's date (in that zone).
            day = si.to_zone(si.so.jd_to_utc(jd0), tz).date()
            jd1 = si.parse_time(f"{day.isoformat()}T{end.strip()}", tz)
            while jd1 <= jd0:
                jd1 += 1.0
        else:
            jd1 = si.parse_time(end, tz)
        if jd1 <= jd0:
            raise SystemExit(f"--end {end} is not after --start")
        hours = (jd1 - jd0) * 24.0
    return jd0, hours


def _load_source(path, norad=None, name=None):
    """(els, fit) from a fit.json or element sources; one of them is None."""
    if path.lower().endswith(".json") and os.path.isfile(path):
        with open(path) as f:
            head = json.load(f)
        if isinstance(head, dict) and head.get("format") == "diofinder-sat-orbit/1":
            satrec, fname, fit_res = load_fit(path)
            satrec.intldesg = head.get("intldesg") or ""
            return None, (satrec, fname, head.get("norad") or 99999, fit_res)
    els = _select(si.load_elements([path]), norad, name)
    if not els:
        raise SystemExit(f"no element set in {path}" + (" matches the --norad/--name filter"
                                                        if norad or name else ""))
    return els, None


def _site(a):
    if a.site_from:
        with open(a.site_from) as f:
            return json.load(f)["site"]
    if a.lat is not None and a.lon is not None:
        return {"latitude_deg": a.lat, "longitude_deg": a.lon, "elevation_m": a.elev}
    raise SystemExit("give --site-from OBS.json or --lat/--lon (east positive)")


def cmd_predict(a):
    import sat_oem
    region = parse_sky(a.sky)
    whole = a.sky_mode == "whole"
    site = _site(a)
    jd0, hours = window(a.start, a.end, a.hours, a.in_tz)
    sky = Sky(site, jd0, hours)
    head = (f"Window: {si.when(jd0, ms=False)} to {si.when(jd0 + hours / 24.0, ms=False)}"
            f" ({hours:g} h)\n" + darkness_text(sky, a.sun_alt))
    if region:
        head += "\n" + sky_text(region, whole)
    kw = dict(sun_max_alt=a.sun_alt, table_step_s=a.step, sky=sky, region=region,
              region_whole=whole)
    if sat_oem.is_oem(a.elements):
        segs = sat_oem.read_oem(a.elements)
        objs = sat_oem.by_object(segs)
        keep = [x for x in objs if (not a.name or a.name.lower() in x[0].lower())]
        segs = [sg for x in keep for sg in x[2]]
        if not segs:
            raise SystemExit(f"no object in {a.elements} matches --name {a.name}")
        lo, hi = sat_oem.coverage(segs)
        head += (f"\nEphemeris (CCSDS OEM, {len(keep)} object(s)) covers "
                 f"{si.when(lo, ms=False)} to {si.when(hi, ms=False)}")
        if hi < jd0 or lo > jd0 + hours / 24.0:
            head += "  -- NOT the window asked for: no pass can be listed"
        passes = predict_oem(segs, site, jd0, hours, a.min_elevation, **kw)
        if len(keep) == 1:
            text = format_passes(passes, keep[0][0])
        else:
            text = format_summary(passes, os.path.basename(a.elements), tables=a.tables)
    else:
        els, fit_ = _load_source(a.elements, a.norad, a.name)
        if els is not None and len(els) > 1:
            print(f"{len(els)} element sets; predicting all of them")
            passes = predict_all(els, site, jd0, hours, a.min_elevation,
                                 min_rev_per_day=0 if a.all_orbits else MIN_REV_PER_DAY,
                                 max_age_days=a.max_age_days or None,
                                 log=print, **kw)
            title = os.path.basename(os.path.normpath(a.elements.rstrip("\\/"))) or a.elements
            text = format_summary(passes, title, tables=a.tables)
        else:
            satrec, name, fit_res = (els[0].satrec, els[0].name, None) if els else \
                (fit_[0], fit_[1], fit_[3])
            passes = predict(satrec, site, jd0, hours, a.min_elevation, fit_result=fit_res, **kw)
            if els and a.max_age_days:
                age = max(jd0 - els[0].epoch_jd, els[0].epoch_jd - jd0 - hours / 24.0, 0.0)
                if age > a.max_age_days:
                    head += (f"\nWARNING: this element set is {age:.0f} days from the window "
                             "(more than --max-age-days): pass times may be far off, or the "
                             "object may have re-entered")
            text = format_passes(passes, name)
    text = head + "\n" + text
    print(text)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        with open(os.path.join(a.out, "predictions.txt"), "w") as f:
            f.write(text + "\n")
        with open(os.path.join(a.out, "predictions.json"), "w") as f:
            json.dump(passes, f, indent=1)
        with open(os.path.join(a.out, "predictions.csv"), "w", newline="") as f:
            import csv
            w = csv.writer(f)
            zoned = si.OUT_TZ.lower() != "utc"     # then also the times in --out-tz
            w.writerow((["max_time", "start_time", "end_time"] if zoned else []) +
                       ["max_utc", "name", "norad", "max_alt", "max_az", "ra_at_max",
                        "dec_at_max", "rate_deg_s", "start_utc", "end_utc", "range_km",
                        "elements_age_days", "timing_sigma_s"])
            for p in passes:
                age = p.get("epoch_age_days")
                w.writerow(([si.when(p["max_jd"], ms=False), si.when(_jd(p, "start"), ms=False),
                             si.when(_jd(p, "end"), ms=False)] if zoned else []) +
                           [p["max_utc"], p.get("name", ""), p.get("norad_str", ""),
                            p["max_alt"], p["max_az"], p["ra_at_max"], p["dec_at_max"],
                            p.get("rate_deg_s_at_max"), p["start_utc"], p["end_utc"],
                            p["range_km_at_max"], "" if age is None else age,
                            p.get("timing_sigma_s") or ""])


MAX_EPHEM_OBJECTS = 100


def cmd_ephem(a):
    """Write a CCSDS OEM (state vectors every --step s) from elements or a fit."""
    import sat_oem
    jd0, hours = window(a.start, a.end, a.hours, a.in_tz)
    jd1 = jd0 + hours / 24.0
    els, fit_ = _load_source(a.elements, a.norad, a.name)
    if els is not None:
        if len(els) > MAX_EPHEM_OBJECTS:
            raise SystemExit(f"{len(els)} objects: pick some with --norad / --name "
                             f"(at most {MAX_EPHEM_OBJECTS} in one OEM)")
        targets = [(e.satrec, e.name or str(e.norad), e.norad,
                    f"SGP4 samples of the {e.source} element set, epoch {si._utc(e.epoch_jd)}")
                   for e in els]
    else:
        s, name, norad, res = fit_
        with open(a.elements) as f:
            sg = json.load(f).get("sigma") or {}
        targets = [(s, name, norad,
                    f"SGP4 samples of a diofinder sat_orbit fit, epoch {si._utc(res['epoch_jd'])}"
                    f"; fit 1 sigma: period {sg.get('period_s', float('nan')):.1f} s, "
                    f"inclination {sg.get('inclination_deg', float('nan')):.3f} deg")]
    segs = []
    for s, name, norad, note in targets:
        oid = sat_oem._object_id(s, name, norad) or str(norad)
        segs.append(sat_oem.segment_from_satrec(
            s, name, oid, jd0, jd1, a.step, a.frame,
            comments=[note, "SGP4 is a mean-element theory: positions are good to about "
                      "a km near the epoch, worse by km per day away from it"]))
    out = a.out or ("ephemeris_oem.xml" if a.xml else "ephemeris.oem")
    if os.path.isdir(out) or out.endswith(("/", "\\")) or \
            "." not in os.path.basename(out.rstrip("/\\")):       # no extension: a folder
        os.makedirs(out, exist_ok=True)
        out = os.path.join(out, "ephemeris_oem.xml" if a.xml else "ephemeris.oem")
    (sat_oem.write_xml if a.xml else sat_oem.write_kvn)(segs, out)
    n = sum(len(sg["jd"]) for sg in segs)
    print(f"{len(segs)} object(s), {n} state vectors every {a.step:g} s, frame {segs[0]['frame']}, "
          f"{si.when(jd0, ms=False)} to {si.when(jd1, ms=False)} -> {out}")


def _window_args(p):
    p.add_argument("--start", help="window start (default now): 2026-11-04T18:00, "
                                   "'2026-11-04 18:00' or 18:00 (today); LOCAL time unless "
                                   "--utc / --in-tz, or the time carries Z or an offset")
    p.add_argument("--end", help="window end, same forms as --start (replaces --hours; a "
                                 "time of day earlier than the start means the next day)")
    p.add_argument("--hours", type=float, default=24.0, help="window length (24)")
    p.add_argument("--in-tz", default="local",
                   help="zone of --start/--end without an offset: local (default, this "
                        "computer's zone), utc, an offset (UTC-5, UTC+05:30) or a name "
                        "(America/New_York; Windows: pip install tzdata)")
    p.add_argument("--utc", dest="in_tz", action="store_const", const="utc",
                   help="--start/--end are UTC (= --in-tz utc)")
    p.add_argument("--out-tz", default="utc",
                   help="zone of the times in the console and predictions.txt/.csv: utc "
                        "(default, for sharing), local, an offset (UTC-5) or a name. "
                        "predictions.json and OEM files are always UTC")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fit", help="fit elements to observations.json track(s)")
    f.add_argument("observations", nargs="+")
    f.add_argument("--track", nargs="*", help="track id (e.g. L1, S2.1) or FILE:ID")
    f.add_argument("--start", help="start from a known element set (any element source: "
                                   "file, .zip, folder, URL, comma list; --norad picks one)")
    f.add_argument("--norad", help="catalog number (selects in --start; written out)")
    f.add_argument("--name", help="object name (default: identified name, else unkYYMMDD)")
    f.add_argument("--intldesg", help="international designator (default identified, "
                                      "else 99099A)")
    g = f.add_mutually_exclusive_group()
    g.add_argument("--circular", action="store_true", help="force a circular orbit")
    g.add_argument("--full", action="store_true", help="force all 6 elements")
    f.add_argument("--out", help="output folder (default <first obs folder>/orbit)")
    p = sub.add_parser("predict", help="upcoming visible passes")
    p.add_argument("elements", help="fit.json, a CCSDS OEM ephemeris (.oem / OEM XML), or "
                                    "element sources: TLE/3LE/OMM files, "
                                    ".zip/.gz of them, folders, URLs, comma-separated "
                                    "(e.g. classfd.zip,inttles.zip); newest epoch wins")
    p.add_argument("--sky", help="only this part of the sky (buildings in the way): east, "
                                 "west (of the meridian), north, south (of the east-west line "
                                 "through the zenith), or ne/nw/se/sw, or a comma list "
                                 "(east,north)")
    p.add_argument("--sky-mode", choices=("whole", "part"), default="whole",
                   help="whole (default): only passes entirely in --sky; part: every pass "
                        "cut to the stretch inside --sky")
    p.add_argument("--norad", help="only these catalog numbers, comma-separated (51445,53323; "
                                   "Alpha-5 such as A0658 too); without --norad/--name every "
                                   "object in the file is predicted")
    p.add_argument("--name", help="only objects whose name contains this (e.g. NOSS, USA)")
    p.add_argument("--max-age-days", type=float, default=30.0,
                   help="with several objects, skip element sets whose epoch is more than "
                        "this many days from the window (30; 0 = keep all). One object is "
                        "always predicted, with a warning")
    p.add_argument("--all-orbits", action="store_true",
                   help="include slow objects (< 2 rev/day: geostationary, high orbits)")
    p.add_argument("--tables", action="store_true",
                   help="with several objects: also print each pass's pointing table")
    p.add_argument("--site-from", help="observations.json whose site to use")
    p.add_argument("--lat", type=float)
    p.add_argument("--lon", type=float, help="east positive")
    p.add_argument("--elev", type=float, default=0.0)
    _window_args(p)
    p.add_argument("--min-elevation", type=float, default=20.0)
    p.add_argument("--sun-alt", type=float, default=-6.0,
                   help="the sky counts as dark with the Sun below this altitude, deg "
                        "(-6 civil twilight; -3 allows brighter twilight, -12 darker)")
    p.add_argument("--step", type=float, default=30.0, help="pointing table step, s")
    p.add_argument("--out", help="also write predictions.txt/.json/.csv here")
    e = sub.add_parser("ephem", help="write a CCSDS OEM ephemeris (state vectors)")
    e.add_argument("elements", help="fit.json or element sources (as predict)")
    e.add_argument("--norad", help="only these catalog numbers, comma-separated "
                                   "(Alpha-5 such as A0658 too)")
    e.add_argument("--name", help="only objects whose name contains this")
    _window_args(e)
    e.add_argument("--step", type=float, default=60.0, help="seconds between states (60)")
    e.add_argument("--frame", default="TEME", type=str.upper,
                   choices=("TEME", "GCRF", "EME2000", "ICRF", "ITRF"),
                   help="reference frame (TEME, SGP4's own, default; GCRF/EME2000/ICRF "
                        "inertial J2000 axes; ITRF Earth-fixed)")
    e.add_argument("--xml", action="store_true", help="OEM XML instead of KVN text")
    e.add_argument("--out", help="output file, or a folder (no extension) for ephemeris.oem / ephemeris_oem.xml (default: ephemeris.oem here)")
    a = ap.parse_args(argv)
    si.set_output_tz(getattr(a, "out_tz", "utc"))
    {"fit": cmd_fit, "predict": cmd_predict, "ephem": cmd_ephem}[a.cmd](a)


if __name__ == "__main__":
    main()
