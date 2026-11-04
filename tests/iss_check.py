#!/usr/bin/env python3
"""Check the whole satellite pipeline against the ISS (off-device).

Capture an ISS pass with the finder (Home -> Live capture), download the
archive, and run this. It assumes the capture contains the ISS and that the
laptop is online: the current ISS element set comes from CelesTrak (updated
several times a day, and good to about a kilometre near its epoch; the ISS
is the best-tracked object there is), so it serves as GROUND TRUTH for:

1. **Astrometry** (the per-frame WCS, centroiding, trail detection): how far
   the measured positions lie ACROSS the true track. Expect about a pixel
   (0.015 deg) when everything works.
2. **Timing** (sensor timestamps + the finder's clock): how far ALONG the
   track the positions lie, converted to seconds. That is your clock error
   (plus the element set's own timing error, normally < 0.1 s for the ISS):
   a phone-set clock should give a few tenths of a second at most.
3. **Orbit from scratch**: sat_orbit's one-pass circular fit, made WITHOUT
   the ISS elements, compared with them: period, inclination, node, where it
   puts the ISS later, and the timing of the next visible passes. The ISS
   orbit is nearly circular (e ~ 0.0005), so the circular model itself is
   right and the comparison measures what one pass really determines.
4. **Photometry** (rough): the measured magnitude against a simple
   prediction (ISS standard magnitude -1.8 at 1000 km, half lit, diffuse
   sphere phase law, +-0.5 mag). The ISS often saturates the camera; then
   the measurement is only a bright limit.

Everything sat_identify normally writes lands in --out too (IOD, R.D.E.,
light curves...), plus iss_report.txt / iss_report.json, iss_residuals.png
and iss_sky.png.

Needs: numpy, astropy, matplotlib, Pillow, sgp4; internet for the CelesTrak
download (cached for 2 hours, as CelesTrak asks), or --tle FILE for an
element set you saved yourself (any element source sat_identify accepts).

Example:
  python tests/iss_check.py livecap_20261108T231500Z.zip
  python tests/iss_check.py livecap_...zip --tle iss.tle --out iss_out
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sat_identify as si  # noqa: E402
import sat_orbit as so  # noqa: E402

ISS_NORAD = 25544
CELESTRAK_ISS = "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE"
ISS_STD_MAG = -1.8          # at 1000 km, half illuminated (Heavens-Above convention)
TLE_MAX_AGE_DAYS = 3.0      # beyond this a reboost may have moved it


# --------------------------------------------------------------------------- #
# Comparisons
# --------------------------------------------------------------------------- #

def along_cross(obs, satrec, geom):
    """Per observation: along-track offset (deg and s; positive = the
    measured position is AHEAD of where the elements put the ISS at that
    timestamp) and cross-track offset (deg), plus the predicted rate."""
    jd = np.array([o["jd"] for o in obs])
    v = si.radec_to_vec([o["ra"] for o in obs], [o["dec"] for o in obs])
    _, r = si.propagate([satrec], jd)
    _, r2 = si.propagate([satrec], jd + 1.0 / 86400.0)
    u, _ = geom.topo_unit(r[0], jd)
    u2, _ = geom.topo_unit(r2[0], jd + 1.0 / 86400.0)
    rate = si._ang(u, u2)                                   # deg/s
    t = u2 - u
    t = t - np.sum(t * u, -1, keepdims=True) * u
    t /= np.linalg.norm(t, axis=-1, keepdims=True)
    c = np.cross(u, t)
    d = v - u
    along = np.degrees(np.sum(d * t, -1))
    cross = np.degrees(np.sum(d * c, -1))
    return {"jd": jd, "along_deg": along, "cross_deg": cross, "rate_deg_s": rate,
            "along_s": along / np.maximum(rate, 1e-6), "track": u}


def robust(x):
    x = np.asarray(x, float)
    med = float(np.median(x))
    return med, 1.4826 * float(np.median(np.abs(x - med)))


def predicted_magnitude(satrec, geom, jd):
    """Rough ISS magnitude: standard magnitude, range and a diffuse-sphere
    phase law. None when in shadow."""
    _, r = si.propagate([satrec], np.array([jd]))
    g = geom.sat_gcrs(r[0][0])
    o = geom.observer(np.array([jd]))[0]
    sun = so._sun_dirs(np.array([jd]))[0]
    if not si.sunlit(g, jd):
        return None, None
    to_obs = o - g
    rng = float(np.linalg.norm(to_obs))
    phase = math.acos(max(-1.0, min(1.0, float(np.dot(sun, to_obs / rng)))))
    f = (math.sin(phase) + (math.pi - phase) * math.cos(phase)) / math.pi
    f90 = 1.0 / math.pi
    if f <= 0:
        return None, math.degrees(phase)
    return ISS_STD_MAG + 5 * math.log10(rng / 1000.0) - 2.5 * math.log10(f / f90), \
        math.degrees(phase)


def compare_orbit(res, truth, site, jd_end, hours=24.0, min_elevation=20.0):
    """sat_orbit fit result vs the truth element set."""
    ep = res["epoch_jd"]
    pt = so.move_epoch(truth, ep)
    p = res["p"]
    period_fit = so.TWO_PI / p[0]
    period_true = so.TWO_PI / truth.no_kozai
    dlam = (p[5] - pt[5] + math.pi) % so.TWO_PI - math.pi
    out = {"period_min_fit": period_fit, "period_min_true": period_true,
           "period_error_s": (period_fit - period_true) * 60.0,
           "period_sigma_s": res["sigma"]["period_s"],
           "inclination_error_deg": math.degrees(p[1] - truth.inclo),
           "inclination_sigma_deg": res["sigma"]["inclination_deg"],
           "node_error_deg": math.degrees((p[2] - pt[2] + math.pi) % so.TWO_PI - math.pi),
           "node_sigma_deg": res["sigma"]["node_deg"],
           "timing_at_epoch_s": math.degrees(dlam) / math.degrees(p[0]) * 60.0,
           "position_error_km": {}}
    for label, dt_d in (("at the pass", 0.0), ("+1 orbit", period_true / 1440.0),
                        ("+6 h", 0.25), ("+24 h", 1.0)):
        jd = np.array([ep + dt_d])
        _, a = si.propagate([res["satrec"]], jd)
        _, b = si.propagate([truth], jd)
        out["position_error_km"][label] = float(np.linalg.norm(a[0][0] - b[0][0]))
    mine = so.predict(res["satrec"], site, jd_end + 0.005, hours, min_elevation,
                      fit_result=res)
    true = so.predict(truth, site, jd_end + 0.005, hours, min_elevation)
    rows = []
    for t in true:
        near = min(mine, key=lambda m: abs(m["max_jd"] - t["max_jd"]), default=None)
        if near is not None and abs(near["max_jd"] - t["max_jd"]) * 24 < 1.0:
            rows.append({"true_max_utc": t["max_utc"], "pred_max_utc": near["max_utc"],
                         "error_s": (near["max_jd"] - t["max_jd"]) * 86400.0,
                         "sigma_s": near.get("timing_sigma_s"),
                         "timing_unknown": near.get("timing_unknown"),
                         "true_max_alt": t["max_alt"], "pred_max_alt": near["max_alt"]})
        else:
            rows.append({"true_max_utc": t["max_utc"], "pred_max_utc": None,
                         "true_max_alt": t["max_alt"]})
    out["next_passes"] = rows
    return out


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

def _verdict(value, good, ok):
    a = abs(value)
    return "excellent" if a <= good else ("good" if a <= ok else "POOR")


def run(archive, tle_sources, out, station="9999", readout_ms=0.0, log=print,
        hours=24.0, min_elevation=20.0):
    os.makedirs(out, exist_ok=True)
    elems = [e for e in si.load_elements(tle_sources) if e.norad == ISS_NORAD]
    if not elems:
        raise SystemExit("no ISS (25544) element set in " + ", ".join(map(str, tle_sources)))
    iss = elems[0]
    log(f"ISS element set: {iss.name} epoch {si._utc(iss.epoch_jd)} ({iss.source})")
    results = si.run(archive, [iss], out, readout_ms=readout_ms, station=station, log=log)
    hits = [r for r in results if r.get("match") and r["match"]["norad"] == ISS_NORAD]
    report = {"archive": os.path.basename(archive), "tle_epoch_utc": si._utc(iss.epoch_jd),
              "tle_source": iss.source, "tracks_found": len(results),
              "iss_tracks": [r["id"] for r in hits]}
    lines = [f"ISS check of {os.path.basename(archive)}",
             f"ground truth: {iss.name}, epoch {si._utc(iss.epoch_jd)} ({iss.source})"]
    if not hits:
        lines.append("NO ISS TRAIL FOUND among %d track(s)." % len(results))
        for r in results:
            c = (r.get("candidates") or [None])[0]
            lines.append(f"  track {r['id']}: {len(r['obs'])} frames"
                         + (f"; ISS was {c['cross_deg']:.2f} deg across, {c['offset_s']:+.1f} s, "
                            f"direction {c['dir_deg']:.0f} deg off" if c else "; ISS not close"))
        lines.append("Check: was the ISS in the field and sunlit, did the frames solve, "
                     "is the clock wildly off (> 10 min)?")
        _write(out, lines, report, log)
        return report
    # One object, possibly split over several stack groups: merge.
    obs = sorted({round(o["jd"], 7): o for r in hits for o in r["obs"]}.values(),
                 key=lambda o: o["jd"])
    with open(os.path.join(out, "observations.json")) as f:
        doc = json.load(f)
    site = doc["site"]
    clock = doc.get("clock") or {}
    jd0, jd1 = obs[0]["jd"], obs[-1]["jd"]
    age = (jd0 - iss.epoch_jd)
    report.update(n_obs=len(obs), start_utc=si._utc(jd0), end_utc=si._utc(jd1),
                  arc_s=(jd1 - jd0) * 86400.0, tle_age_days=age, clock=clock)
    geom = si.Geometry(site, jd0 - 0.01, jd1 + 0.01)
    ac = along_cross(obs, iss.satrec, geom)
    t_off, t_jit = robust(ac["along_s"])
    c_med, c_sc = robust(ac["cross_deg"])
    c_rms = float(np.sqrt(np.mean(ac["cross_deg"] ** 2)))
    plate = 13.54 / 960.0
    report["astrometry"] = {"cross_track_rms_deg": c_rms, "cross_track_median_deg": c_med,
                            "cross_track_scatter_deg": c_sc,
                            "cross_track_rms_px": c_rms / plate}
    report["timing"] = {"offset_s": t_off, "jitter_s": t_jit,
                        "finder_clock_error_s": -t_off,
                        "rate_deg_s_median": float(np.median(ac["rate_deg_s"]))}
    lines += ["",
              f"ISS seen in {len(obs)} frames, {si._utc(jd0)} -> {si._utc(jd1)[11:]}, "
              f"arc {report['arc_s']:.1f} s, rate {report['timing']['rate_deg_s_median']:.2f} "
              f"deg/s (tracks {', '.join(report['iss_tracks'])})",
              f"element set age at the pass: {age:+.2f} d"
              + ("   ** older than %g d: a reboost may have moved the ISS **" % TLE_MAX_AGE_DAYS
                 if abs(age) > TLE_MAX_AGE_DAYS else ""),
              f"capture clock: {clock.get('note', '?')}",
              "",
              "1. ASTROMETRY (across the track)",
              f"   rms {c_rms:.4f} deg = {c_rms * 60:.2f} arcmin = {c_rms / plate:.2f} px; "
              f"median {c_med:+.4f} deg, scatter {c_sc:.4f} deg  -> "
              f"{_verdict(c_rms, 0.02, 0.05)}",
              "2. TIMING (along the track)",
              f"   positions are {t_off:+.3f} s {'ahead of' if t_off > 0 else 'behind'} the "
              f"ISS elements (scatter {t_jit:.3f} s)",
              f"   -> the finder clock was about {abs(t_off):.2f} s "
              f"{'BEHIND' if t_off > 0 else 'AHEAD'} (ISS element timing is good to "
              f"< 0.1 s near its epoch)  -> {_verdict(t_off, 0.2, 1.0)}"]
    # 3. Orbit from scratch.
    track = {"id": "ISS", "observations": [dict(o, sigma_deg=max(c_sc, 0.005))
                                           for o in obs],
             "sigma_pos_deg": max(c_sc, 0.005), "identified": None}
    pdoc = {"capture": report["archive"], "site": site, "clock": clock}
    try:
        fit = so.fit([so.Pass(pdoc, track)], log=lambda *a: None)
        cmp_ = compare_orbit(fit, iss.satrec, site, jd1, hours, min_elevation)
        report["orbit_from_scratch"] = cmp_
        pe = cmp_["position_error_km"]
        lines += ["3. ORBIT FROM THIS PASS ALONE (circular fit, no ISS elements used)",
                  f"   period {cmp_['period_min_fit']:.3f} min vs {cmp_['period_min_true']:.3f}"
                  f" true: error {cmp_['period_error_s']:+.1f} s "
                  f"({cmp_['period_error_s'] / (cmp_['period_min_true'] * 60) * 100:+.2f} %), "
                  f"fit sigma {cmp_['period_sigma_s']:.0f} s",
                  f"   inclination {cmp_['inclination_error_deg']:+.3f} deg "
                  f"(sigma {cmp_['inclination_sigma_deg']:.3f}), node "
                  f"{cmp_['node_error_deg']:+.3f} deg (sigma {cmp_['node_sigma_deg']:.3f})",
                  "   position error: " + ", ".join(f"{k} {v:.0f} km" for k, v in pe.items())]
        for row in cmp_["next_passes"]:
            if row.get("pred_max_utc"):
                unk = "  (fit says: timing unknown)" if row.get("timing_unknown") else (
                    f"  (fit sigma {row['sigma_s']:.0f} s)" if row.get("sigma_s") else "")
                lines.append(f"   next pass {row['true_max_utc']}: predicted "
                             f"{row['error_s']:+.0f} s off{unk}")
            else:
                lines.append(f"   next pass {row['true_max_utc']}: the fit misses it")
    except SystemExit as e:
        lines.append(f"3. ORBIT FROM THIS PASS ALONE: fit failed ({e})")
    # 4. Photometry.
    ph = {}
    for r in hits:
        if r.get("photometry"):
            ph = r["photometry"]
            break
    jmid = obs[len(obs) // 2]["jd"]
    pm, phase = predicted_magnitude(iss.satrec, geom, jmid)
    report["photometry"] = {"measured": ph.get("mag"), "measured_err": ph.get("mag_err"),
                            "bright_limit": bool(ph.get("bright_limit")),
                            "saturated_frames": ph.get("saturated_frames"),
                            "predicted_rough": pm, "phase_deg": phase}
    if ph.get("mag") is not None and pm is not None:
        lim = " (saturated: brighter than this)" if ph.get("bright_limit") else ""
        lines.append(f"4. PHOTOMETRY: measured {ph['mag']:+.1f}{lim}, rough prediction "
                     f"{pm:+.1f} (phase {phase:.0f} deg, +-0.5 mag); difference "
                     f"{ph['mag'] - pm:+.1f}")
    elif pm is None:
        lines.append("4. PHOTOMETRY: the elements put the ISS in Earth's shadow at mid-pass")
    else:
        lines.append("4. PHOTOMETRY: no magnitude measured")
    _plots(out, ac, obs)
    _write(out, lines, report, log)
    return report


def _write(out, lines, report, log):
    with open(os.path.join(out, "iss_report.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(out, "iss_report.json"), "w") as f:
        json.dump(report, f, indent=1, default=si._json_default)
    for ln in lines:
        log(ln)


def _plots(out, ac, obs):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return
    t = (ac["jd"] - ac["jd"][0]) * 86400.0
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(8, 5), sharex=True, dpi=100)
    a1.plot(t, ac["along_s"], "o", ms=4)
    a1.axhline(float(np.median(ac["along_s"])), color="C3", lw=1)
    a1.set_ylabel("along track (s)\n+ = ahead of elements")
    a2.plot(t, ac["cross_deg"] * 60.0, "o", ms=4, color="C2")
    a2.axhline(0, color="0.5", lw=0.8)
    a2.set_ylabel("across track (arcmin)")
    a2.set_xlabel(f"seconds after {si._utc(ac['jd'][0])}")
    fig.suptitle("ISS: measured positions vs CelesTrak elements", fontsize=10)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "iss_residuals.png"))
    plt.close(fig)
    ra_t, dec_t = si.vec_to_radec(ac["track"])
    fig, ax = plt.subplots(figsize=(6, 5), dpi=100)
    ax.plot(ra_t, dec_t, "-", color="0.6", label="elements (at each frame's time)")
    ax.plot([o["ra"] for o in obs], [o["dec"] for o in obs], "o", ms=4, color="C0",
            label="measured")
    ax.invert_xaxis()
    ax.set_xlabel("RA (deg, J2000)")
    ax.set_ylabel("Dec (deg)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "iss_sky.png"))
    plt.close(fig)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("archive")
    ap.add_argument("--tle", nargs="*", default=[CELESTRAK_ISS],
                    help="ISS element source(s): file, .zip, folder, URL or comma list "
                         "(default: CelesTrak, downloaded; newest epoch wins)")
    ap.add_argument("--out", help="output folder (default <archive>_iss)")
    ap.add_argument("--station", default="9999")
    ap.add_argument("--readout-ms", type=float, default=0.0)
    ap.add_argument("--hours", type=float, default=24.0,
                    help="how far ahead to compare predicted passes (24)")
    ap.add_argument("--min-elevation", type=float, default=20.0)
    a = ap.parse_args(argv)
    out = a.out or os.path.splitext(a.archive)[0] + "_iss"
    run(a.archive, a.tle, out, a.station, a.readout_ms, hours=a.hours,
        min_elevation=a.min_elevation)
    print(f"results in {out}")


if __name__ == "__main__":
    main()
