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


def test_unsolved_frame_borrows_the_neighbours_wcs(tmp_path):
    z, w = make_capture(tmp_path, unsolved=(2,))
    res = so.analyse(z, fetch=lambda p: fake_response(w), sleep_s=0)
    r2 = [r for r in res["results"] if r["rec"]["seq"] == 2][0]
    assert r2["wcs_source"].startswith("borrowed:frame_00000")
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
