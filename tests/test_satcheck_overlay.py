"""Off-device satellite overlay (tests/satcheck_overlay.py), end to end on a
synthetic capture with a known sky->pixel mapping.

A fake frame is drawn with a satellite streak along a known path; the
archive is written by the real LiveRecorder (matched-star rows included); the
SatChecker response is faked in the structure the live service returns
(data.satellites[name].positions[{ra, dec, julian_date}]). The overlay must
put the predicted streak on the real one and rank it above a decoy."""

import json
import math
import os
import sys
import types
import zipfile

import numpy as np
import pytest

astropy = pytest.importorskip("astropy")
pytest.importorskip("matplotlib")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from astropy.wcs import WCS  # noqa: E402

from diofinder import frame_meta as fm  # noqa: E402
from diofinder import live_capture as lc  # noqa: E402
import satcheck_archive as sa  # noqa: E402
import satcheck_overlay as so  # noqa: E402

W, H = 960, 760
RA0, DEC0 = 271.43, 38.17
T0 = 1_785_000_000.0                       # unix seconds of frame 1's exposure start
EXP = 1.0


def true_wcs(mirrored=False, rot_deg=23.0):
    s = 13.54 / W
    th = math.radians(rot_deg)
    cd = np.array([[-s, 0], [0, -s]]) @ np.array(
        [[math.cos(th), -math.sin(th)], [math.sin(th), math.cos(th)]])
    if mirrored:
        cd = cd @ np.diag([-1.0, 1.0])
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [RA0, DEC0]
    w.wcs.crpix = [W / 2 + 0.5, H / 2 + 0.5]
    w.wcs.cd = cd
    return w


def jd(t):
    return t / 86400.0 + 2440587.5


def sat_track(w, x0, y0, x1, y1, t0, t1, n=21):
    """A straight pixel path (x0,y0)->(x1,y1) during [t0, t1], sampled as
    SatChecker positions (in RA/Dec through the true WCS)."""
    xs, ys = np.linspace(x0, x1, n), np.linspace(y0, y1, n)
    ra, dec = w.pixel_to_world_values(xs, ys)
    ts = np.linspace(t0, t1, n)
    return [{"ra": float(r), "dec": float(d), "julian_date": jd(t)}
            for r, d, t in zip(ra, dec, ts)]


def make_capture(tmp_path, mirrored=False, n_frames=3, unsolved=(), streak=None):
    """Record n frames through LiveRecorder. `streak` = (frame_no, (x0,y0),
    (x1,y1)) is drawn into that frame."""
    w = true_wcs(mirrored)
    rng = np.random.default_rng(3)
    scfg = {}
    rec = lc.LiveRecorder(scfg, types.SimpleNamespace(
        frame_width=W, frame_height=H, fov_deg=13.54, version="t",
        live_capture_max_mb=4000, live_capture_min_free_mb=0),
        directory=str(tmp_path), queue_max=64,
        clock=lambda: T0)
    rec.sync({"on": True, "id": "20260724T022854Z",
              "site": {"latitude_deg": 43.95, "longitude_deg": -72.667, "elevation_m": 330.0}})
    # Field stars at random pixels -> their true sky positions.
    sx, sy = rng.uniform(20, W - 20, 40), rng.uniform(20, H - 20, 40)
    sra, sdec = w.pixel_to_world_values(sx, sy)
    ring = ()
    for k in range(1, n_frames + 1):
        img = rng.normal(20, 2, (H, W)).clip(0, 255)
        for x, y in zip(sx, sy):
            img[int(y), int(x)] = 200
        if streak and streak[0] == k:
            (x0, y0), (x1, y1) = streak[1], streak[2]
            for t in np.linspace(0, 1, 800):
                img[int(round(y0 + (y1 - y0) * t)), int(round(x0 + (x1 - x0) * t))] = 90
        img = img.astype(np.uint8)
        t_start = T0 + (k - 1) * 2.0
        # frame_meta entry: readout = start + exposure; wall offset = 0.
        ring = fm.push(ring, (k, int((t_start + EXP) * 1e9), int(EXP * 1e6), 4.0, 0))
        rec.offer_frame(img, k, ring)
        if k in unsolved:
            rec.note_solution({"solved": False, "seq": k})
        else:
            # Solver convention: matched centroids (y, x) with (0.5, 0.5) = centre
            # of pixel (0, 0); astropy pixel centres are 0-based -> +0.5.
            rec.note_matches(k, [(y + 0.5, x + 0.5) for x, y in zip(sx, sy)],
                             [(r, d, 5.0) for r, d in zip(sra, sdec)])
            rec.note_solution({"solved": True, "seq": k, "center_ra_deg": RA0,
                               "center_dec_deg": DEC0, "roll_deg": 23.0,
                               "fov_deg": 13.54, "is_mirrored": mirrored})
    rec.sync({})
    z = tmp_path / "livecap_20260724T022854Z.zip"
    import time
    t = time.monotonic() + 15
    while not z.exists() and time.monotonic() < t:
        time.sleep(0.05)
    assert z.exists()
    return str(z), w


def fake_response(w, frame_no=2):
    """Satellite A crosses frame `frame_no` (streak drawn there); B crosses
    the same frame but leaves no trail (decoy); C passes outside the frame."""
    t0 = T0 + (frame_no - 1) * 2.0
    return {"data": {"satellites": {
        "STARLINK-1234 (45000)": {"positions": sat_track(w, 150, 120, 750, 600, t0, t0 + EXP)},
        "DECOY (1)": {"positions": sat_track(w, 800, 100, 300, 650, t0, t0 + EXP)},
        "FAR (2)": {"positions": sat_track(w, -900, -900, -400, -800, t0, t0 + EXP)},
    }}}


@pytest.mark.parametrize("mirrored", [False, True])
def test_overlay_lands_on_the_real_streak(tmp_path, mirrored):
    z, w = make_capture(tmp_path, mirrored, streak=(2, (150, 120), (750, 600)))
    calls = []

    def fetch(params):
        calls.append(params)
        return fake_response(w)
    res = so.analyse(z, fetch=fetch, sleep_s=0)
    assert len(calls) == 1                        # 3 frames, one window -> one query
    p = calls[0]
    assert p["latitude"] == 43.95 and p["longitude"] == -72.667 and p["elevation"] == 330.0
    assert p["async"] == "False" and p["group_by"] == "satellite"
    assert p["duration"] >= 2 * 2.0 + EXP         # spans all three exposures
    r2 = [r for r in res["results"] if r["rec"]["seq"] == 2][0]
    assert r2["wcs_source"] == "fit" and r2["wcs_rms_px"] < 0.05
    sats = {s["satellite"]: s for s in r2["sats"]}
    (x0, y0), (x1, y1) = sats["STARLINK-1234 (45000)"]["seg_px"]
    assert abs(x0 - 150) < 0.5 and abs(y0 - 120) < 0.5     # exact, mirroring and all
    assert abs(x1 - 750) < 0.5 and abs(y1 - 600) < 0.5
    assert sats["STARLINK-1234 (45000)"]["in_frame"] and sats["DECOY (1)"]["in_frame"]
    assert not sats["FAR (2)"]["in_frame"]
    # Frames 1 and 3 are outside the satellites' 1 s pass: no exposure segment.
    r1 = [r for r in res["results"] if r["rec"]["seq"] == 1][0]
    assert all("seg_px" not in s for s in r1["sats"])
    so.score_results(z, res)
    assert sats["STARLINK-1234 (45000)"]["score"] > 5 * max(1.0, sats["DECOY (1)"]["score"])


def test_unsolved_frame_gets_its_neighbours_wcs(tmp_path):
    # Between two solved frames: interpolated; at the end: borrowed.
    z, w = make_capture(tmp_path, unsolved=(2, 3), n_frames=4)
    res = so.analyse(z, fetch=lambda p: fake_response(w), sleep_s=0)
    src = {r["rec"]["seq"]: r["wcs_source"] for r in res["results"]}
    assert src[2].startswith("interp:frame_000001.png+frame_000004.png")
    z2, _ = make_capture(tmp_path / "b", unsolved=(3,))
    res = so.analyse(z2, fetch=lambda p: fake_response(w), sleep_s=0)
    r3 = [r for r in res["results"] if r["rec"]["seq"] == 3][0]
    assert r3["wcs_source"] == "borrowed:frame_000002.png"
    z, w = make_capture(tmp_path / "c", unsolved=(2,))
    res = so.analyse(z, fetch=lambda p: fake_response(w), sleep_s=0)
    r2 = [r for r in res["results"] if r["rec"]["seq"] == 2][0]
    seg = {s["satellite"]: s for s in r2["sats"]}["STARLINK-1234 (45000)"]["seg_px"]
    assert abs(seg[0][0] - 150) < 0.5


def test_roll_fallback_is_close_for_a_non_mirrored_frame():
    # Without matched stars the centre/roll/FOV path is used. On our synthetic
    # convention (roll = North CCW from up) it reproduces the true mapping.
    w = true_wcs(False, rot_deg=23.0)
    approx = so.roll_wcs({"center_ra_deg": RA0, "center_dec_deg": DEC0,
                          "roll_deg": 23.0, "fov_deg": 13.54}, W, H)
    ra, dec = w.pixel_to_world_values(np.array([100.0, 800]), np.array([50.0, 700]))
    x, y = approx.world_to_pixel_values(ra, dec)
    assert np.allclose(x, [100, 800], atol=0.5) and np.allclose(y, [50, 700], atol=0.5)


def test_grouping_splits_on_time_and_pointing():
    def fr(t, ra):
        return {"file": f"f{t}", "jd_start": jd(T0 + t), "exposure_s": 1.0,
                "solved": True, "center_ra_deg": ra, "center_dec_deg": 10.0}
    frames = [fr(0, 100), fr(2, 100), fr(70, 100), fr(72, 105)]
    g = so.group_frames(frames, range(4), window_s=60, radius_deg=8.6)
    assert [x["idx"] for x in g] == [[0, 1], [2], [3]]


def test_interp_track_across_ra_zero():
    track = [(1.0, 359.0, 0.0), (2.0, 1.0, 0.0)]
    ra, dec = so.interp_track(track, 1.5)
    assert abs(((ra + 180) % 360) - 180) < 1e-6 and abs(dec) < 1e-9
    assert so.interp_track(track, 0.5) is None


def test_parse_satellites_matches_the_service_structure():
    resp = {"data": {"satellites": {"ISS (25544)": {"positions": [
        {"ra": 2.0, "dec": 3.0, "julian_date": 2.0},
        {"ra": 1.0, "dec": 2.0, "julian_date": 1.0}]}}}}
    assert sa.parse_satellites(resp) == {"ISS (25544)": [(1.0, 1.0, 2.0), (2.0, 2.0, 3.0)]}
    assert sa.summarize(resp) == ["ISS (25544)"]


def test_cli_writes_images_report_and_reuses_cached_responses(tmp_path, monkeypatch):
    z, w = make_capture(tmp_path, streak=(2, (150, 120), (750, 600)))
    resp_file = tmp_path / "cache.json"
    # Seed the cache with the response for the query the tool will make.
    res = so.analyse(z, fetch=lambda p: fake_response(w), sleep_s=0)
    resp_file.write_text(json.dumps(res["responses"]))
    out = tmp_path / "out"
    so.main([z, "--out", str(out), "--responses", str(resp_file), "--offline", "--chart"])
    files = sorted(os.listdir(out))
    assert "frame_000002_sats.png" in files and "chart_1.png" in files
    assert "report.csv" in files
    rows = (out / "report.csv").read_text().splitlines()
    hit = [r for r in rows if r.startswith("frame_000002.png") and "STARLINK" in r]
    assert hit and ",True," in hit[0]


# ---- stacked composite (--stack) -------------------------------------------

DRIFT_DEG = 0.08                     # field drift per frame (~6 px), exaggerated
N_DRIFT = 6


def wcs_at(k, mirrored=False):
    """True mapping of frame k (1-based): the pointing drifts in RA and the
    field rotates slightly, as on a non-tracking mount (sped up)."""
    w = true_wcs(mirrored, rot_deg=23.0 + 0.05 * (k - 1))
    w.wcs.crval = [RA0 + DRIFT_DEG * (k - 1), DEC0]
    return w


def make_drift_capture(tmp_path, unsolved=()):
    """Frames 1..N_DRIFT with fixed stars on the sky (so they move in pixels)
    and one satellite on a fixed sky path leaving a dash in every exposure.
    Returns (zip path, satellite track as SatChecker positions)."""
    rng = np.random.default_rng(5)
    w1 = wcs_at(1)
    sra, sdec = w1.pixel_to_world_values(rng.uniform(60, W - 60, 40),
                                         rng.uniform(60, H - 60, 40))
    # Satellite: a straight pixel path in frame 1's sky over the whole capture.
    t_end = T0 + (N_DRIFT - 1) * 2.0 + EXP
    track = sat_track(w1, 120, 600, 820, 140, T0, t_end, n=61)
    scfg = {}
    rec = lc.LiveRecorder(scfg, types.SimpleNamespace(
        frame_width=W, frame_height=H, fov_deg=13.54, version="t",
        live_capture_max_mb=4000, live_capture_min_free_mb=0),
        directory=str(tmp_path), queue_max=64, clock=lambda: T0)
    rec.sync({"on": True, "id": "20260724T022854Z",
              "site": {"latitude_deg": 43.95, "longitude_deg": -72.667, "elevation_m": 330.0}})
    ring = ()
    for k in range(1, N_DRIFT + 1):
        wk = wcs_at(k)
        img = rng.normal(20, 2, (H, W)).clip(0, 255)
        sx, sy = wk.world_to_pixel_values(sra, sdec)
        for x, y in zip(sx, sy):
            if 1 <= x < W - 1 and 1 <= y < H - 1:
                img[int(round(y)) - 1:int(round(y)) + 2, int(round(x)) - 1:int(round(x)) + 2] = 200
        t_start = T0 + (k - 1) * 2.0
        p0 = so.interp_track([(q["julian_date"], q["ra"], q["dec"]) for q in track], jd(t_start))
        p1 = so.interp_track([(q["julian_date"], q["ra"], q["dec"]) for q in track], jd(t_start + EXP))
        (x0, x1), (y0, y1) = wk.world_to_pixel_values(np.array([p0[0], p1[0]]),
                                                      np.array([p0[1], p1[1]]))
        for t in np.linspace(0, 1, 400):
            xi, yi = int(round(x0 + (x1 - x0) * t)), int(round(y0 + (y1 - y0) * t))
            if 0 <= xi < W and 0 <= yi < H:
                img[yi, xi] = 90
        ring = fm.push(ring, (k, int((t_start + EXP) * 1e9), int(EXP * 1e6), 4.0, 0))
        rec.offer_frame(img.astype(np.uint8), k, ring)
        if k in unsolved:
            rec.note_solution({"solved": False, "seq": k})
            continue
        ok = (sx > 0) & (sx < W) & (sy > 0) & (sy < H)
        rec.note_matches(k, [(y + 0.5, x + 0.5) for x, y in zip(sx[ok], sy[ok])],
                         [(r, d, 5.0) for r, d in zip(sra[ok], sdec[ok])])
        c = wk.pixel_to_world_values((W - 1) / 2, (H - 1) / 2)
        rec.note_solution({"solved": True, "seq": k, "center_ra_deg": float(c[0]),
                           "center_dec_deg": float(c[1]), "roll_deg": 23.0,
                           "fov_deg": 13.54})
    rec.sync({})
    z = tmp_path / "livecap_20260724T022854Z.zip"
    import time
    t = time.monotonic() + 20
    while not z.exists() and time.monotonic() < t:
        time.sleep(0.05)
    assert z.exists()
    decoy = sat_track(w1, 820, 600, 120, 140, T0, t_end, n=61)
    return str(z), {"data": {"satellites": {"STARLINK-1234 (45000)": {"positions": track},
                                            "DECOY (1)": {"positions": decoy}}}}, sra, sdec


def test_stack_aligns_stars_and_turns_the_pass_into_dashes(tmp_path):
    z, resp, sra, sdec = make_drift_capture(tmp_path, unsolved=(3,))
    res = so.analyse(z, fetch=lambda p: resp, sleep_s=0)
    assert len(res["groups"]) == 1
    # Frame 3 didn't solve: its mapping is interpolated from frames 2 and 4.
    r3 = [r for r in res["results"] if r["rec"]["seq"] == 3][0]
    assert r3["wcs_source"].startswith("interp:")
    tx, ty = wcs_at(3).world_to_pixel_values(sra[:5], sdec[:5])
    ix, iy = r3["wcs"].world_to_pixel_values(sra[:5], sdec[:5])
    assert np.allclose(tx, ix, atol=0.3) and np.allclose(ty, iy, atol=0.3)
    with zipfile.ZipFile(z) as zf:
        st = so.stack_group(zf, res["groups"][0], res)
    assert len(st["members"]) == N_DRIFT
    # Stars are fixed on the sky: aligned through the mappings they land on
    # the same reference pixel in every frame, so they vanish from `moving`
    # even though they moved ~30 px across the capture.
    w_ref = st["ref"]["wcs"]
    sx, sy = w_ref.world_to_pixel_values(sra, sdec)
    ok = (sx > 5) & (sx < W - 5) & (sy > 5) & (sy < H - 5)
    star_px = [(int(round(y)), int(round(x))) for x, y in zip(sx[ok], sy[ok])]
    assert np.mean([st["max"][p] for p in star_px]) > 150        # stacked, sharp
    assert np.mean([st["moving"][p] for p in star_px]) < 15       # and steady
    # The satellite leaves one dash per exposure, between its rails.
    sc = so.stack_scores(st, res["groups"][0], res)
    assert sc["STARLINK-1234 (45000)"] > 10 * max(1.0, sc["DECOY (1)"])
    track = [(q["julian_date"], q["ra"], q["dec"])
             for q in resp["data"]["satellites"]["STARLINK-1234 (45000)"]["positions"]]
    for k in range(1, N_DRIFT + 1):
        mid = so.interp_track(track, jd(T0 + (k - 1) * 2.0 + EXP / 2))
        x, y = w_ref.world_to_pixel_values(mid[0], mid[1])
        patch = st["moving"][int(y) - 2:int(y) + 3, int(x) - 2:int(x) + 3]
        assert patch.max() > 50, f"dash {k} missing at ({x:.0f}, {y:.0f})"
    # And the gaps between exposures stay dark.
    gap = so.interp_track(track, jd(T0 + EXP + 0.5))
    gx, gy = w_ref.world_to_pixel_values(gap[0], gap[1])
    assert st["moving"][int(gy) - 1:int(gy) + 2, int(gx) - 1:int(gx) + 2].max() < 30


def test_cli_stack_writes_composites(tmp_path):
    z, resp, *_ = make_drift_capture(tmp_path)
    res = so.analyse(z, fetch=lambda p: resp, sleep_s=0)
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps(res["responses"]))
    out = tmp_path / "out"
    so.main([z, "--out", str(out), "--responses", str(cache), "--offline", "--stack"])
    assert {"stack_1.png", "stack_1_moving.png"} <= set(os.listdir(out))


# ---- which predictions could be seen; trails nobody predicted --------------

def test_line_score_ignores_a_prediction_that_only_crosses_a_trail():
    img = np.random.default_rng(1).normal(20, 2, (H, W))
    for t in np.linspace(0, 1, 1200):                 # bright trail, top-left to bottom-right
        img[int(100 + 500 * t), int(100 + 700 * t)] = 200
    along = so.line_score(img, (100, 100), (800, 600), extend=0.0)
    across = so.line_score(img, (100, 600), (800, 100), extend=0.0)
    assert along > 100 and across < 5


def _pos_at(t, gcrs_xyz_km, site):
    """SatChecker-style altitude/azimuth/range for a geocentric position."""
    import astropy.units as u
    from astropy.coordinates import (AltAz, CartesianRepresentation, EarthLocation,
                                     GCRS, SkyCoord)
    loc = EarthLocation(lat=site["latitude_deg"] * u.deg, lon=site["longitude_deg"] * u.deg)
    c = SkyCoord(CartesianRepresentation(*(np.asarray(gcrs_xyz_km) * u.km)),
                 frame=GCRS(obstime=t)).transform_to(AltAz(obstime=t, location=loc))
    return {"altitude": float(c.alt.deg), "azimuth": float(c.az.deg),
            "range_km": float(c.distance.to(u.km).value), "julian_date": float(t.jd)}


def test_sat_properties_rate_direction_and_earth_shadow():
    from astropy.coordinates import get_sun
    from astropy.time import Time
    site = {"latitude_deg": 43.95, "longitude_deg": -72.667, "elevation_m": 330.0}
    t = Time(jd(T0), format="jd", scale="utc")
    sun = get_sun(t).cartesian.xyz.value
    sun = sun / np.linalg.norm(sun)
    resp = {"data": {"satellites": {
        "SUNLIT (1)": {"positions": [_pos_at(t, sun * 7000.0, site)]},
        "SHADOW (2)": {"positions": [_pos_at(t, -sun * 7000.0, site)]},
    }}}
    j = jd(T0)
    tracks = {"SUNLIT (1)": [(j, 100.0, 10.0), (j + 10 / 86400, 100.0, 11.0)],     # north, 0.1 deg/s
              "SHADOW (2)": [(j, 100.0, 10.0), (j + 10 / 86400, 99.0, 10.0)],      # west
              "GEO (3)": [(j, 100.0, 0.0), (j + 100 / 86400, 100.4, 0.0)]}        # 0.004 deg/s
    p = so.sat_properties(resp, site, tracks)
    assert p["SUNLIT (1)"]["lit"] is True and p["SHADOW (2)"]["lit"] is False
    assert p["GEO (3)"]["lit"] is None                            # no position data: unknown
    assert abs(p["SUNLIT (1)"]["rate_deg_s"] - 0.1) < 1e-3
    assert p["SUNLIT (1)"]["toward"] == "N" and p["SHADOW (2)"]["toward"] == "W"
    keep, hidden = so.filter_tracks(tracks, p)
    assert list(keep) == ["SUNLIT (1)"]
    assert hidden == {"unlit": ["SHADOW (2)"], "stationary": ["GEO (3)"]}
    keep, _ = so.filter_tracks(tracks, p, show_unlit=True, show_stationary=True)
    assert len(keep) == 3


def test_detect_trails_ignores_stars_and_finds_a_dashed_line():
    rng = np.random.default_rng(2)
    img = np.clip(rng.normal(3, 1.5, (H, W)), 0, None)
    for x, y in rng.uniform(30, [W - 30, H - 30], (60, 2)):      # round star residuals
        img[int(y) - 1:int(y) + 2, int(x) - 1:int(x) + 2] = 60
    assert so.detect_trails(img) == []
    for k in range(6):                                            # 6 dashes, gaps between
        for t in np.linspace(0, 1, 120):
            s = k * 2 + t
            img[int(round(500 - 30 * s)), int(round(100 + 60 * s))] = 80
    tr = so.detect_trails(img)
    assert len(tr) == 1 and tr[0]["n_dashes"] == 6
    assert abs(tr[0]["dash_px"] - math.hypot(60, 30)) < 8
    assert tr[0]["flash_ratio"] < 1.5
    rate = so.trail_rate_deg_s(tr[0], 13.54 / W, 1.0)
    assert abs(rate / (math.hypot(60, 30) * 13.54 / W) - 1) < 0.05


def test_flash_ratio_spots_strobes():
    img = np.zeros((H, W))
    img[400, 100:700] = 50
    assert so._flash_ratio(img, (100, 400), (699, 400), 10) < 1.2
    for x in range(100, 700, 20):                                 # a 4-px knot every 20 px
        img[399:402, x:x + 4] = 180                               # (like the real strobes)
    assert so._flash_ratio(img, (100, 400), (699, 400), 10) >= so.FLASH_RATIO


def test_trail_in_the_stack_is_matched_timed_and_checked_for_direction(tmp_path):
    z, resp, *_ = make_drift_capture(tmp_path)
    res = so.analyse(z, fetch=lambda p: resp, sleep_s=0)
    g = res["groups"][0]
    with zipfile.ZipFile(z) as zf:
        st = so.stack_group(zf, g, res)
        trails = so.trail_kinematics(zf, st, res, so.detect_trails(st["moving"]))
    assert len(trails) == 1
    t = trails[0]
    assert t["n_seen"] == N_DRIFT
    # True motion: 830 px in 11 s in frame 1's pixels (the reference frame is
    # within a few px of that).
    assert abs(abs(t["px_per_s"]) - math.hypot(700, 460) / 11.0) < 5
    so.match_trails(trails, st, g, res)
    assert t["satellite"] == "STARLINK-1234 (45000)"
    # Same line, but predicted to run the other way: not a match.
    track = g["tracks"]["STARLINK-1234 (45000)"]
    rev = [(track[i][0], track[-1 - i][1], track[-1 - i][2]) for i in range(len(track))]
    g2 = dict(g, tracks={"BACKWARDS (9)": rev})
    so.match_trails(trails, st, g2, res)
    assert t["satellite"] is None
    assert t["rejected"] == [("BACKWARDS (9)", "moves the opposite way")]
    assert so.trail_label(t) == "unidentified"


def test_cli_stack_writes_trails_csv(tmp_path):
    z, resp, *_ = make_drift_capture(tmp_path)
    res = so.analyse(z, fetch=lambda p: resp, sleep_s=0)
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps(res["responses"]))
    out = tmp_path / "out"
    so.main([z, "--out", str(out), "--responses", str(cache), "--offline", "--stack"])
    rows = (out / "trails.csv").read_text().splitlines()
    assert rows[0].startswith("stack,") and len(rows) == 2
    assert "STARLINK-1234 (45000)" in rows[1]
    rep = (out / "report.csv").read_text().splitlines()[0]
    assert "rate_deg_s,toward,lit" in rep
