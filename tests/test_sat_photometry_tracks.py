"""sat_photometry (tone-curve inversion, star calibration, dash photometry,
light curves) and sat_tracks (frame-by-frame residual linking) — including an
end-to-end hand-tracked capture: the scope follows a satellite, so it is a dot
near the frame centre while the field sweeps across the sky, and the stack
trail finder cannot see it. No network, no hardware."""
import json
import math
import os
import sys
import types

import numpy as np
import pytest

pytest.importorskip("sgp4")
pytest.importorskip("astropy")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from astropy.wcs import WCS  # noqa: E402

from diofinder import frame_meta as fm  # noqa: E402
from diofinder import live_capture as lc  # noqa: E402
import sat_identify as si  # noqa: E402
import sat_photometry as ph  # noqa: E402
import sat_tracks as tr  # noqa: E402
import test_sat_identify as tsi  # noqa: E402

W, H = 960, 760
TONE = ph.tone_from_tuning_file(ph.DEFAULT_TUNING)
LIN = ph.Linearizer(TONE)


def to_8bit(linear):
    """Linear light -> 8-bit through the finder tone curve (what the camera
    does), so Linearizer is tested against an independent forward model."""
    g = np.asarray(TONE["gamma_curve"], float)
    y16 = np.interp(linear, g[0::2], g[1::2])
    return np.clip(np.rint(y16 / 256.0), 0, 255).astype(np.uint8)


def test_tone_curve_round_trip_and_session_recording(tmp_path):
    lin = np.linspace(0, 60000, 2000)
    back = LIN(to_8bit(lin))
    mid = (lin > 300) & (lin < 50000)
    rel = np.abs(back[mid] - lin[mid]) / lin[mid]
    assert np.median(rel) < 0.03                                 # within an 8-bit step
    info = lc.tone_info(ph.DEFAULT_TUNING)
    assert info["gamma_curve"] == TONE["gamma_curve"] and info["contrast_enhance"] is False
    assert ph.tone_for_session({"tone": info})[1] == "recorded in the capture"
    assert lc.tone_info(str(tmp_path / "missing.json")) is None


def _star_field(rng, zp=15.0, k=-0.3, n=60, bg=400.0):
    lin = np.full((H, W), bg) + rng.normal(0, 25, (H, W))
    yy, xx = np.mgrid[0:H, 0:W]
    stars = []
    for _ in range(n):
        x, y, m = rng.uniform(20, W - 20), rng.uniform(20, H - 20), rng.uniform(4.5, 7.5)
        flux = 10 ** (-0.4 * (m - zp - k * ph._rho2(x, y, W, H)))
        sl = (slice(int(y) - 6, int(y) + 7), slice(int(x) - 6, int(x) + 7))
        lin[sl] += flux / (2 * math.pi * 1.2 ** 2) * np.exp(
            -((xx[sl] - x) ** 2 + (yy[sl] - y) ** 2) / (2 * 1.2 ** 2))
        stars.append([x + 0.5, y + 0.5, 0.0, 0.0, m])
    return lin, stars


def test_calibration_recovers_zero_point_and_vignetting():
    rng = np.random.default_rng(3)
    frames = {}
    for i, zp in enumerate((15.0, 14.6, 15.3)):
        lin, stars = _star_field(rng, zp=zp)
        raw = to_8bit(lin)
        frames[f"f{i}"] = ph.star_measurements(LIN(raw), raw, stars)
    cal = ph.calibrate(frames, W, H)
    assert cal["n"] > 100 and cal["rms"] < 0.1
    zps = [cal["zp"][f"f{i}"] for i in range(3)]
    assert zps[1] - zps[0] == pytest.approx(-0.4, abs=0.05)
    assert zps[2] - zps[0] == pytest.approx(0.3, abs=0.05)
    assert cal["k"] == pytest.approx(-0.3, abs=0.1)


def _dash_frame(rng, a, b, flux, flash_period_px=None, bg=400.0):
    """Linear frame (sky already subtracted afterwards) with a dash from a to b."""
    lin = np.full((H, W), bg) + rng.normal(0, 20, (H, W))
    a, b = np.asarray(a, float), np.asarray(b, float)
    L = float(np.hypot(*(b - a)))
    yy, xx = np.mgrid[0:H, 0:W]
    for s in np.linspace(0, L, int(L * 4)):
        p = a + (b - a) * s / L
        f = flux / (L * 4)
        if flash_period_px:
            f *= 1.0 + 0.9 * math.sin(2 * math.pi * s / flash_period_px)
        sl = (slice(int(p[1]) - 5, int(p[1]) + 6), slice(int(p[0]) - 5, int(p[0]) + 6))
        lin[sl] += f / (2 * math.pi) * np.exp(-((xx[sl] - p[0]) ** 2 + (yy[sl] - p[1]) ** 2) / 2)
    return lin


def test_measure_trail_flux_and_flash_period():
    rng = np.random.default_rng(5)
    a, b = (300.0, 200.0), (520.0, 330.0)                       # slanted, 255 px
    jd0, exp = 2461346.5, 1.0
    for period_px, want in ((None, "S"), (40.0, "F")):
        lin = _dash_frame(rng, a, b, 3.0e5, period_px)
        raw = to_8bit(lin)
        resid = LIN(raw) - 400.0
        m = ph.measure_trail(resid, raw, a, b, jd0, jd0 + exp / 86400.0)
        assert m["full"] and m["saturated_px"] == 0
        assert m["flux"] == pytest.approx(3.0e5, rel=0.1)
        s = ph.light_curve_summary(m["lc_jd"], m["lc_rate"], m["noise_rate"],
                                   min_period_s=4.0 / m["speed_px_s"])
        assert s["behaviour"] == want, s
        if want == "F":
            # 40 px at 255 px/s
            assert s["period_s"] == pytest.approx(40.0 / 255.0, rel=0.05)


def test_light_curve_summary_classes():
    t = 2461346.5 + np.arange(200) * 0.05 / 86400.0
    steady = ph.light_curve_summary(t, np.full(200, 100.0) + np.random.default_rng(1).normal(0, 2, 200), 2.0)
    assert steady["behaviour"] == "S" and steady["period_s"] is None
    flash = ph.light_curve_summary(t, 100 + 90 * np.sin(2 * np.pi * np.arange(200) * 0.05 / 1.7), 2.0)
    assert flash["behaviour"] == "F" and flash["period_s"] == pytest.approx(1.7, rel=0.03)
    rng = np.random.default_rng(2)
    irregular = ph.light_curve_summary(t, 100 * np.exp(rng.normal(0, 1.0, 200)), 2.0)
    assert irregular["behaviour"] == "X"
    assert ph.light_curve_summary(t[:2], [1, 2], 1)["behaviour"] is None


def test_iod_photometry_columns_and_reporting_helpers():
    ob = {"jd": 2461346.5, "ra": 30.0, "dec": -10.0}
    line = si.iod_line(ob, "99999", "99099A", "1775", "E", 0.2, 0.01, "F", 4.53, 0.25, 2.25)
    assert line[:13] == "99999 99 099A" and line[16:20] == "1775" and line[21] == "E"
    assert line[41:43] == "27"                                   # 0.2 s
    assert line[62:64] == "16"                                   # 0.01 deg
    assert line[65] == "F" and line[66:70] == "+045" and line[71:73] == "03"
    assert line[74:80] == "002250"                               # 2.25 s, point implied
    assert si.iod_line(ob, behaviour="S", mag=-1.2, mag_unc=0.3)[66:70] == "-012"
    assert len(si.iod_line(ob, behaviour="S")) == 66             # no photometry
    # Clock record -> time uncertainty.
    phone = {"clock": {"trusted": True, "this_boot": True, "ntp_synchronized": False,
                       "last_sync": {"source": "phone", "action": "set"}}}
    assert si.clock_uncertainty(phone)[0] == si.TIME_UNC_PHONE_S
    assert si.clock_uncertainty({"clock": {"ntp_synchronized": True}})[0] == si.TIME_UNC_NTP_S
    ss = {"clock": {"trusted": True, "this_boot": True,
                    "last_sync": {"source": "skysafari", "action": "checked"}}}
    assert si.clock_uncertainty(ss)[0] == si.TIME_UNC_SKYSAFARI_S
    unk, _, postable = si.clock_uncertainty({"clock": {"trusted": False, "summary": "x"}})
    assert unk == si.TIME_UNC_UNKNOWN_S and not postable
    # Point selection: spread over the arc, partial dashes avoided.
    obs = [{"jd": 2461346.5 + i / 86400.0, "full": i not in (0, 1), "resid_deg": 0.0}
           for i in range(20)]
    sel = si.select_points(obs, 4)
    assert len(sel) == 4 and 0 not in sel and 1 not in sel and sel[-1] >= 15
    assert si.select_points(obs, 0) == list(range(20))
    assert si.unknown_name(2461344.5) == "unk261031"


def test_link_ignores_noise_and_fixed_pattern():
    rng = np.random.default_rng(1)
    n = 60
    T = 2461346.5 + np.arange(n) * 0.5 / 86400
    src = []
    for k in range(n):
        t = k * 0.5
        bl = []
        if k % 7 != 3:                                           # missed now and then
            bl.append(dict(ra=100 + 0.6 * t + 0.002 * t * t + rng.normal(0, 0.01),
                           dec=20 + 0.3 * t + rng.normal(0, 0.01), jd=T[k], flux=50,
                           x=480 + rng.normal(0, 20), y=380 + rng.normal(0, 20), length=3,
                           file=f"f{k}"))
        for _ in range(5):
            bl.append(dict(ra=90 + rng.uniform(0, 40), dec=10 + rng.uniform(0, 25), jd=T[k],
                           flux=20, x=rng.uniform(0, W), y=rng.uniform(0, H), length=3,
                           file=f"f{k}"))
        bl.append(dict(ra=95 + 0.6 * t, dec=15, jd=T[k], flux=80, x=100.2, y=50.1,
                       length=3, file=f"f{k}"))                  # a hot pixel
        src.append(bl)
    tr.drop_fixed_pattern(src, n)
    assert not any(b["x"] == 100.2 for bl in src for b in bl)
    tracks = tr.link(src, T)
    assert len(tracks) == 1 and len(tracks[0]["points"]) == 51
    assert tracks[0]["rms_deg"] < 0.03


# ---- end to end: a hand-tracked pass --------------------------------------

def _tracked_capture(tmp_path, el, geom, n=24, exp=0.25, cadence=1.0, jitter_deg=0.25):
    """The finder follows the satellite by hand: each frame is centred on it
    (plus a wandering tracking error), so it is a dot near the centre and the
    stars move through the field."""
    rng = np.random.default_rng(11)
    jd_start = tsi.JD_PASS - (n * cadence / 2) / 86400.0
    t0_unix = (jd_start - 2440587.5) * 86400.0
    rec = lc.LiveRecorder({}, types.SimpleNamespace(
        frame_width=W, frame_height=H, fov_deg=13.54, version="t",
        live_capture_max_mb=4000, live_capture_min_free_mb=0),
        directory=str(tmp_path), queue_max=64, clock=lambda: t0_unix)
    rec.sync({"on": True, "id": "20261102T022230Z", "site": tsi.SITE})
    # Stars: a wide patch of sky around the whole pass.
    mids = tsi._truth(el, jd_start + (np.arange(n) * cadence + exp / 2) / 86400.0, geom)
    sra, sdec = [], []
    for v in mids[::3]:
        ra0, dec0 = si.vec_to_radec(v)
        for _ in range(40):
            sra.append(float(ra0) + rng.uniform(-9, 9) / math.cos(math.radians(dec0)))
            sdec.append(float(dec0) + rng.uniform(-9, 9))
    sra, sdec = np.array(sra), np.array(sdec)
    off = np.zeros(2)
    ring = ()
    yy, xx = np.mgrid[0:H, 0:W]
    for k in range(1, n + 1):
        jd0 = jd_start + (k - 1) * cadence / 86400.0
        mid = tsi._truth(el, np.array([jd0 + exp / 2 / 86400.0]), geom)[0]
        ra0, dec0 = si.vec_to_radec(mid)
        off = 0.8 * off + rng.normal(0, jitter_deg, 2)          # hand-tracking error
        w = WCS(naxis=2)
        w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
        w.wcs.crval = [float(ra0) + off[0] / math.cos(math.radians(dec0)), float(dec0) + off[1]]
        w.wcs.crpix = [W / 2 + 0.5, H / 2 + 0.5]
        s = 13.54 / W
        th = math.radians(30)
        w.wcs.cd = np.array([[-s, 0], [0, -s]]) @ np.array([[math.cos(th), -math.sin(th)],
                                                             [math.sin(th), math.cos(th)]])
        lin = np.full((H, W), 400.0) + rng.normal(0, 20, (H, W))
        sx, sy = w.world_to_pixel_values(sra, sdec)
        inside = (sx > 15) & (sx < W - 15) & (sy > 15) & (sy < H - 15)
        for x, y in zip(sx[inside], sy[inside]):
            sl = (slice(int(y) - 5, int(y) + 6), slice(int(x) - 5, int(x) + 6))
            lin[sl] += 6000 / (2 * math.pi * 1.3) * np.exp(
                -((xx[sl] - x) ** 2 + (yy[sl] - y) ** 2) / (2 * 1.3))
        # The satellite during the exposure: (nearly) a dot.
        path = tsi._truth(el, jd0 + np.linspace(0, exp, 10) / 86400.0, geom)
        px, py = w.world_to_pixel_values(*si.vec_to_radec(path))
        for x, y in zip(px, py):
            sl = (slice(int(y) - 5, int(y) + 6), slice(int(x) - 5, int(x) + 6))
            lin[sl] += 1500 / (2 * math.pi * 1.3) * np.exp(
                -((xx[sl] - x) ** 2 + (yy[sl] - y) ** 2) / (2 * 1.3))
        img = to_8bit(lin)
        t_start = t0_unix + (k - 1) * cadence
        ring = fm.push(ring, (k, int((t_start + exp) * 1e9), int(exp * 1e6), 4.0, 0))
        rec.offer_frame(img, k, ring)
        rec.note_matches(k, [(y + 0.5, x + 0.5) for x, y in zip(sx[inside], sy[inside])],
                         [(r, d, 6.0) for r, d in zip(sra[inside], sdec[inside])])
        c_ra, c_dec = w.wcs.crval
        rec.note_solution({"solved": True, "seq": k, "center_ra_deg": float(c_ra),
                           "center_dec_deg": float(c_dec), "roll_deg": 30.0, "fov_deg": 13.54})
    rec.sync({})
    z = tmp_path / "livecap_20261102T022230Z.zip"
    import time
    t = time.monotonic() + 30
    while not z.exists() and time.monotonic() < t:
        time.sleep(0.05)
    assert z.exists()
    return str(z)


def test_hand_tracked_pass_is_linked_identified_and_measured(tmp_path, ):
    elems_path = tmp_path / "classfd.tle"
    elems_path.write_text(tsi.CLASSFD)
    elems = si.load_elements([str(elems_path)])
    geom = si.Geometry(tsi.SITE, tsi.JD_PASS - 700 / 86400.0, tsi.JD_PASS + 700 / 86400.0)
    el = [e for e in elems if e.satrec.satnum == 31701][0]
    z = _tracked_capture(tmp_path, el, geom)
    out = tmp_path / "out"
    res = si.run(z, elems, str(out), log=lambda *a: None)
    linked = [r for r in res if r["kind"] == "linked"]
    assert len(linked) == 1, [(r["id"], len(r["obs"])) for r in res]
    r = linked[0]
    assert len(r["obs"]) >= 18                                   # most of 24 frames
    jd = np.array([o["jd"] for o in r["obs"]])
    v = si.radec_to_vec([o["ra"] for o in r["obs"]], [o["dec"] for o in r["obs"]])
    assert float(np.median(si._ang(v, tsi._truth(el, jd, geom)))) < 0.03
    assert r["match"]["norad"] == 31701
    assert r["match"]["offset_s"] == pytest.approx(tsi.OFFSET, abs=1.0)
    # The arc is long: ~10 deg across many fields.
    assert float(si._ang(v[0], v[-1])) > 5.0
    p = r["photometry"]
    assert p["mag"] is not None and p["frame_scatter_mag"] < 0.2
    assert p["light_curve"]["sampling"] == "one value per frame"
    doc = json.loads((out / "observations.json").read_text())
    assert doc["tracks"][0]["kind"] == "linked"
