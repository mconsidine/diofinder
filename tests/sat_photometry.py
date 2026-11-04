"""Photometry for diofinder live captures: star calibration, satellite
magnitudes and light curves (used by sat_identify.py; off-device).

The saved frames are 8-bit and have passed through the camera's tone curve
(the libcamera ``rpi.contrast`` gamma curve; diofinder's finder tuning makes
it deliberately steep so faint stars survive the 8-bit reduction). Nothing
else in the pipeline changes brightness in a frame-dependent way: adaptive
contrast, auto exposure / white balance, denoise and sharpening are off, and
the finder tuning has no lens-shading stage. So:

1. ``linearize``: invert the tone curve (recorded per capture in
   session.json ``tone`` since v0.11.76; older captures fall back to the
   shipped finder curve, with a warning) -> values proportional to light.
2. ``calibrate``: aperture photometry of each frame's matched stars (the
   solver's own matches, with catalog magnitudes) fitted as
   ``m_cat = ZP_frame - 2.5 log10(flux) + k * rho**2`` (rho = distance from
   the frame centre over the half-diagonal): one zero point per frame
   (absorbs exposure, gain, transparency) plus a shared vignetting term.
3. ``measure_trail`` (stack trails) / ``segment_flux`` (frame-by-frame
   tracks): the light of the dash in the frame minus the steady sky (so stars
   under the trail do not count) -> the frame's magnitude (a dash collects the
   same total light over the exposure as a star would) and light-curve
   samples (position along the dash -> time, from the object's motion).
4. ``light_curve_summary``: magnitude statistics, flash period (Lomb-Scargle)
   and an IOD optical-behaviour code: S steady, F constant flash period,
   X irregular flashing.

Limits worth knowing: 8-bit steps are coarse at the bright end and anything
at 255 is clipped (the magnitude is then only an upper bound on faintness:
"at least this bright"); a crushed sky (many pixels at 0) clips the noise and
makes faint trails read slightly faint; the catalog magnitudes are in the star
database's band, which is close to but not exactly visual V, and sunlight on a
satellite is a different colour from most stars (a colour term of ~0.1-0.2
mag). Expect roughly +-0.2-0.3 mag for steady objects of magnitude 3-6.
"""
from __future__ import annotations

import json
import math
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TUNING = os.path.join(os.path.dirname(HERE), "tuning", "imx477_finder.json")
SATURATED_DN = 250

# ---------------------------------------------------------------------------
# Tone curve
# ---------------------------------------------------------------------------


def tone_from_tuning_file(path):
    with open(path) as f:
        t = json.load(f)
    for a in t.get("algorithms", []):
        if "rpi.contrast" in a:
            c = a["rpi.contrast"]
            return {"tuning_file": path, "gamma_curve": c["gamma_curve"],
                    "contrast_enhance": bool(c.get("ce_enable", 1))}
    raise ValueError(f"{path}: no rpi.contrast gamma_curve")


def tone_for_session(session, log=None):
    """The capture's tone curve (session.json ``tone``), else the shipped
    finder curve. Returns (tone dict, source string)."""
    tone = (session or {}).get("tone")
    if tone and tone.get("gamma_curve"):
        if tone.get("contrast_enhance"):
            (log or print)("WARNING: adaptive contrast was on; magnitudes are unreliable")
        return tone, "recorded in the capture"
    if log:
        log("note: this capture predates tone-curve recording; assuming the "
            "shipped finder tuning (imx477_finder.json)")
    return tone_from_tuning_file(DEFAULT_TUNING), "assumed finder tuning"


class Linearizer:
    """8-bit frame -> linear light (16-bit input units of the tone curve)."""

    def __init__(self, tone):
        g = np.asarray(tone["gamma_curve"], float)
        xin, yout = g[0::2], g[1::2]
        order = np.argsort(yout)
        self.lut = np.interp(np.arange(256) * 256.0 + 128.0, yout[order], xin[order])
        self.lut[0] = 0.0                        # black stays black

    def __call__(self, img):
        return self.lut[np.asarray(img, np.uint8)]


# ---------------------------------------------------------------------------
# Star calibration
# ---------------------------------------------------------------------------

def _rho2(x, y, w, h):
    return ((x - w / 2.0) ** 2 + (y - h / 2.0) ** 2) / ((w / 2.0) ** 2 + (h / 2.0) ** 2)


def aperture_flux(lin, raw, x, y, r_ap=3.0, r_in=5.0, r_out=8.0):
    """(flux, saturated) at (x, y) in pixel-index coordinates."""
    h, w = lin.shape
    x0, x1 = int(max(0, math.floor(x - r_out))), int(min(w, math.ceil(x + r_out) + 1))
    y0, y1 = int(max(0, math.floor(y - r_out))), int(min(h, math.ceil(y + r_out) + 1))
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None, False
    yy, xx = np.mgrid[y0:y1, x0:x1]
    d = np.hypot(xx - x, yy - y)
    ap, an = d <= r_ap, (d >= r_in) & (d <= r_out)
    if ap.sum() < 5 or an.sum() < 8:
        return None, False
    sub = lin[y0:y1, x0:x1]
    bg = float(np.median(sub[an]))
    sat = bool(np.any(raw[y0:y1, x0:x1][ap] >= SATURATED_DN))
    return float(np.sum(sub[ap] - bg)), sat


def star_measurements(lin, raw, matched):
    """[(x, y, mag_cat, flux)] for unsaturated matched stars. ``matched``
    rows are [x, y, ra, dec, mag] in the extractor convention ((0.5, 0.5) =
    centre of pixel (0, 0)), so pixel-index coordinates are x-0.5, y-0.5."""
    out = []
    for row in matched or []:
        x, y, mag = float(row[0]) - 0.5, float(row[1]) - 0.5, row[4]
        if mag is None:
            continue
        f, sat = aperture_flux(lin, raw, x, y)
        if f is not None and f > 0 and not sat:
            out.append((x, y, float(mag), f))
    return out


def calibrate(frames_stars, width, height, min_stars=5, clip=3.0, iters=5):
    """Fit ZP per frame + shared vignetting k. ``frames_stars``: {key: [(x,
    y, mag, flux)]}. Returns {"zp": {key: zp}, "k": k, "rms": mag rms,
    "n": stars used, "per_frame_rms": {key: rms}}."""
    keys = [k for k, s in frames_stars.items() if len(s) >= min_stars]
    if not keys:
        return {"zp": {}, "k": 0.0, "rms": None, "n": 0, "per_frame_rms": {}}
    rows = [(ki, x, y, m, f) for ki, k in enumerate(keys) for (x, y, m, f) in frames_stars[k]]
    rows = np.array(rows, float)
    use = np.ones(len(rows), bool)
    nk = len(keys)
    for _ in range(iters):
        r = rows[use]
        A = np.zeros((len(r), nk + 1))
        A[np.arange(len(r)), r[:, 0].astype(int)] = 1.0
        A[:, nk] = _rho2(r[:, 1], r[:, 2], width, height)
        b = r[:, 3] + 2.5 * np.log10(r[:, 4])
        sol, *_ = np.linalg.lstsq(A, b, rcond=None)
        Aall = np.zeros((len(rows), nk + 1))
        Aall[np.arange(len(rows)), rows[:, 0].astype(int)] = 1.0
        Aall[:, nk] = _rho2(rows[:, 1], rows[:, 2], width, height)
        res = rows[:, 3] + 2.5 * np.log10(rows[:, 4]) - Aall @ sol
        sig = 1.4826 * float(np.median(np.abs(res[use] - np.median(res[use])))) or 0.05
        new = np.abs(res) <= clip * sig
        if np.array_equal(new, use):
            break
        use = new
    per = {}
    for ki, k in enumerate(keys):
        m = use & (rows[:, 0] == ki)
        per[k] = float(np.sqrt(np.mean(res[m] ** 2))) if m.any() else None
    return {"zp": {k: float(sol[i]) for i, k in enumerate(keys)}, "k": float(sol[nk]),
            "rms": float(np.sqrt(np.mean(res[use] ** 2))), "n": int(use.sum()),
            "per_frame_rms": per}


def magnitude(zp, k, flux, x, y, width, height):
    if flux is None or flux <= 0:
        return None
    return zp - 2.5 * math.log10(flux) + k * _rho2(x, y, width, height)


# ---------------------------------------------------------------------------
# Trail photometry
# ---------------------------------------------------------------------------

def fit_motion(track_obs):
    """(s0, v_px_per_day, jd0) of s = s0 + v (jd - jd0) from (jd, s) pairs."""
    jd = np.array([o[0] for o in track_obs], float)
    s = np.array([o[1] for o in track_obs], float)
    jd0 = float(np.median(jd))
    v, s0 = np.polyfit(jd - jd0, s, 1)
    return float(s0), float(v), jd0


def _strip(shape, a, b, margin):
    """Pixels of the box around segment a->b (frame coords): their index
    arrays and coordinates along (s, from a) and across (o) the segment."""
    h, w = shape
    a, b = np.asarray(a, float), np.asarray(b, float)
    L = float(np.hypot(*(b - a)))
    u = (b - a) / L if L > 0 else np.array([1.0, 0.0])
    n = np.array([-u[1], u[0]])
    x0 = int(max(0, math.floor(min(a[0], b[0]) - margin)))
    x1 = int(min(w, math.ceil(max(a[0], b[0]) + margin) + 1))
    y0 = int(max(0, math.floor(min(a[1], b[1]) - margin)))
    y1 = int(min(h, math.ceil(max(a[1], b[1]) + margin) + 1))
    if x1 <= x0 or y1 <= y0:
        return None
    yy, xx = np.mgrid[y0:y1, x0:x1]
    dx, dy = xx - a[0], yy - a[1]
    return yy.ravel(), xx.ravel(), (dx * u[0] + dy * u[1]).ravel(), \
        (dx * n[0] + dy * n[1]).ravel(), L


def measure_trail(resid, raw, a, b, jd_start, jd_end, ok=None, half_width=3.0,
                  bg_gap=6.0, bg_width=4.0, end_pad=3.0, bin_px=3.0):
    """One frame's dash. ``resid`` = the frame's linear light minus the
    steady sky (stars subtracted), ``raw`` = the 8-bit frame, ``a``/``b`` =
    where the object was (frame px) at the exposure's start and end. Every
    pixel is counted once (pixels binned by their position along the dash,
    so a slanted dash on the pixel grid does not alias into a fake
    flicker). Returns {flux, flux_err, x, y (dash centre), full (wholly
    inside the frame), saturated_px, lc_jd, lc_rate (light per second, one
    sample per ``bin_px`` along the dash: about the PSF, the finest detail a
    dash can show), noise_rate} or None."""
    h, w = resid.shape
    st = _strip(resid.shape, a, b, bg_gap + bg_width + end_pad + 1)
    if st is None:
        return None
    yy, xx, s, o, L = st
    good = np.ones(len(s), bool) if ok is None else ok[yy, xx]
    along = (s >= -end_pad) & (s <= L + end_pad)
    sig = along & (np.abs(o) <= half_width) & good
    bgm = along & (np.abs(o) >= bg_gap) & (np.abs(o) <= bg_gap + bg_width) & good
    if sig.sum() < 5 or bgm.sum() < 8:
        return None
    bv = resid[yy[bgm], xx[bgm]]
    bg = float(np.median(bv))
    noise = 1.4826 * float(np.median(np.abs(bv - bg))) or 1.0
    v = resid[yy[sig], xx[sig]] - bg
    flux = float(np.sum(v))
    a, b = np.asarray(a, float), np.asarray(b, float)
    full = bool(min(a[0], b[0]) >= 3 and min(a[1], b[1]) >= 3 and max(a[0], b[0]) < w - 3
                and max(a[1], b[1]) < h - 3)
    exp_s = (jd_end - jd_start) * 86400.0
    lc_jd = lc_rate = np.array([])
    if L >= 2 * bin_px and exp_s > 0:
        nb = int(math.floor(L / bin_px))
        idx = np.floor(s[sig] / bin_px).astype(int)
        keep = (idx >= 0) & (idx < nb)
        per = np.bincount(idx[keep], weights=v[keep], minlength=nb)
        cnt = np.bincount(idx[keep], minlength=nb)
        full_bins = cnt >= 0.8 * bin_px * (2 * half_width + 1)
        centres = (np.arange(nb) + 0.5) * bin_px
        lc_jd = jd_start + (centres / L) * (jd_end - jd_start)
        lc_rate = per / bin_px * (L / exp_s)
        lc_jd, lc_rate = lc_jd[full_bins], lc_rate[full_bins]
    m = 0.5 * (a + b)
    return {"flux": flux, "flux_err": noise * math.sqrt(int(sig.sum())),
            "x": float(m[0]), "y": float(m[1]), "full": full,
            "saturated_px": int(np.sum(raw[yy[sig], xx[sig]] >= SATURATED_DN)),
            "lc_jd": lc_jd, "lc_rate": lc_rate,
            "noise_rate": noise * math.sqrt((2 * half_width + 1) / bin_px)
            * (L / exp_s if exp_s > 0 else 0),
            "speed_px_s": (L / exp_s) if exp_s > 0 else None}


def segment_flux(lin_resid, ok, raw, a, b, noise, r_ap=3.0, r_in=6.0, r_out=10.0):
    """Light within r_ap of the segment a-b (a dot when a == b) in a
    star-subtracted linear residual image; background = median of the ring
    r_in..r_out. Pixels outside ``ok`` (star cores, edges) are skipped and
    counted. Returns {flux, flux_err, saturated_px, masked_frac} or None."""
    h, w = lin_resid.shape
    a, b = np.asarray(a, float), np.asarray(b, float)
    x0 = int(max(0, math.floor(min(a[0], b[0]) - r_out)))
    x1 = int(min(w, math.ceil(max(a[0], b[0]) + r_out) + 1))
    y0 = int(max(0, math.floor(min(a[1], b[1]) - r_out)))
    y1 = int(min(h, math.ceil(max(a[1], b[1]) + r_out) + 1))
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None
    yy, xx = np.mgrid[y0:y1, x0:x1].astype(float)
    d = b - a
    L2 = float(d @ d)
    t = np.clip(((xx - a[0]) * d[0] + (yy - a[1]) * d[1]) / L2, 0, 1) if L2 > 0 else 0.0
    dist = np.hypot(xx - (a[0] + t * d[0]), yy - (a[1] + t * d[1]))
    good = ok[y0:y1, x0:x1]
    ap, ring = dist <= r_ap, (dist >= r_in) & (dist <= r_out)
    if ap.sum() < 5:
        return None
    sub = lin_resid[y0:y1, x0:x1]
    rg = sub[ring & good]
    bg = float(np.median(rg)) if rg.size >= 8 else 0.0
    use = ap & good
    return {"flux": float(np.sum(sub[use] - bg)),
            "flux_err": float(noise * math.sqrt(max(int(use.sum()), 1))),
            "saturated_px": int(np.sum(raw[y0:y1, x0:x1][ap] >= SATURATED_DN)),
            "masked_frac": float(1.0 - use.sum() / ap.sum())}


def calibrated(flux, zp, k, x, y, width, height):
    """Light on the magnitude scale: 10**(-0.4 m) for a flux measured at
    (x, y) in a frame with zero point zp (so values from different frames,
    exposures and transparency compare directly)."""
    return flux * 10.0 ** (-0.4 * (zp + k * _rho2(x, y, width, height)))


def light_curve_summary(jd, cflux, noise, min_period_s=0.15, fap_max=0.01):
    """Flash period and behaviour from light-curve samples. ``cflux`` =
    calibrated light (see ``calibrated``; any common scale works), ``noise`` =
    its typical 1-sigma error. Returns dict: n, span_s, period_s, fap,
    amplitude_mag (5-95 percentile range), behaviour ('S' steady, 'F' constant
    flash period, 'X' irregular flashing, None when too little data)."""
    jd = np.asarray(jd, float)
    c = np.asarray(cflux, float)
    ok = np.isfinite(c) & np.isfinite(jd)
    jd, c = jd[ok], c[ok]
    out = {"n": int(len(jd)), "span_s": None, "period_s": None, "fap": None,
           "amplitude_mag": None, "behaviour": None}
    if len(jd) < 3:
        return out
    t = (jd - jd.min()) * 86400.0
    span = float(t.max())
    out["span_s"] = round(span, 2)
    pos = c[c > 0]
    if len(pos) < 3:
        return out
    hi, lo = np.percentile(pos, 95), max(np.percentile(pos, 5), 1e-30)
    amp = 2.5 * math.log10(hi / lo)
    out["amplitude_mag"] = round(amp, 2)
    rel_noise = float(noise) / max(float(np.median(pos)), 1e-30)
    noise_mag = 1.0857 * rel_noise
    if len(jd) >= 20 and span > 3 * min_period_s:
        from astropy.timeseries import LombScargle
        ls = LombScargle(t, c)
        fmin, fmax = 3.0 / span, 1.0 / min_period_s
        if fmax > fmin:
            freq, power = ls.autopower(minimum_frequency=fmin, maximum_frequency=fmax,
                                       samples_per_peak=10)
            if len(power):
                i = int(np.argmax(power))
                fap = _fap(float(power[i]), len(t), span, fmin, fmax)
                out["fap"] = fap
                if fap < fap_max and amp > max(0.5, 3 * noise_mag):
                    out["period_s"] = round(1.0 / float(freq[i]), 3)
    if out["period_s"] is not None:
        out["behaviour"] = "F"
    elif amp > max(0.75, 4 * noise_mag):
        out["behaviour"] = "X"
    else:
        out["behaviour"] = "S"
    return out


def _fap(power, n, span, fmin, fmax):
    """False-alarm probability of the highest peak of a standard-normalized
    (floating-mean) Lomb-Scargle periodogram: single-frequency probability
    (1 - p)**((N - 3) / 2) over about span*(fmax - fmin) independent
    frequencies (numpy only; astropy's own needs scipy)."""
    single = max(0.0, 1.0 - power) ** (0.5 * max(n - 3, 1))
    m = max(1.0, span * (fmax - fmin))
    return float(-np.expm1(m * np.log1p(-min(single, 1 - 1e-16))))
