"""sat_identify: element-set loading, identification with a free time offset,
refinement, the output formats (TLE incl. Alpha-5, OMM KVN/XML/JSON/CSV),
IOD observations, and an end-to-end run on a synthetic live capture whose
satellite dashes follow a real element set. No network, no hardware."""
import io
import json
import math
import os
import sys
import types
import zipfile

import numpy as np
import pytest

pytest.importorskip("sgp4")
pytest.importorskip("astropy")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from astropy.wcs import WCS  # noqa: E402
from sgp4 import omm  # noqa: E402
from sgp4.api import Satrec  # noqa: E402

from diofinder import frame_meta as fm  # noqa: E402
from diofinder import live_capture as lc  # noqa: E402
import sat_identify as si  # noqa: E402

# Two objects of one NOSS triplet (0.9 deg apart, same orbit) and a GEO, from
# Mike McCants' classfd.tle of 2026-11-02 (non-standard checksum fields, as in
# that file).
CLASSFD = """NOSS 3-4 (A)
1 31701U 07027A   26305.11925817 0.00000000  00000-0  00000-0 0    01
2 31701  63.3855 334.9446 0455683  25.7901 334.2099 13.40996952    00
NOSS 3-4 (C)
1 31708U 07027C   26305.11938377 0.00000000  00000-0  00000-0 0    03
2 31708  63.3761 335.2260 0475053  24.8808 335.1192 13.40996259    04
SDS 2-2
1 20963U 90097B   26306.78449793  .00000000  00000-0  00000-0 0    06
2 20963  13.8750 333.5002 0018269 296.2162  63.6081  0.98580576 17095
"""
SITE = {"latitude_deg": 43.95, "longitude_deg": -72.667, "elevation_m": 330.0}
JD_PASS = 2461346.599073             # NOSS 3-4 (A) near zenith, 2026-11-02 02:22:40 UTC
OFFSET = 37.0                         # the object runs 37 s ahead of its elements


@pytest.fixture(scope="module")
def elems(tmp_path_factory):
    p = tmp_path_factory.mktemp("el") / "classfd.tle"
    p.write_text(CLASSFD)
    return si.load_elements([str(p)])


def _truth(el, jd, geom):
    e, r = si.propagate([el.satrec], np.asarray(jd) + OFFSET / 86400.0)
    u, _ = geom.topo_unit(r[0], np.asarray(jd) + OFFSET / 86400.0)
    return u


def _obs(el, geom, n=25, noise=0.01, seed=0):
    rng = np.random.default_rng(seed)
    jd = JD_PASS + (np.arange(n) * 0.8 - 10.0) / 86400.0
    ra, dec = si.vec_to_radec(_truth(el, jd, geom))
    return [{"jd": float(t), "ra": float(a + rng.normal(0, noise) / math.cos(math.radians(d))),
             "dec": float(d + rng.normal(0, noise))} for t, a, d in zip(jd, ra, dec)]


@pytest.fixture(scope="module")
def geom():
    return si.Geometry(SITE, JD_PASS - 700 / 86400.0, JD_PASS + 700 / 86400.0)


def test_loader_reads_tle_3le_zip_and_omm_keeping_the_newest(tmp_path, elems):
    assert {e.satrec.satnum for e in elems} == {31701, 31708, 20963}
    a = [e for e in elems if e.satrec.satnum == 31701][0]
    assert a.name == "NOSS 3-4 (A)" and a.intldesg == "07027A"
    # An older copy of 31701 in a zip, plus an OMM JSON with a newer epoch.
    old = CLASSFD.replace("26305.11925817", "26300.11925817")
    zp = tmp_path / "old.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("old.tle", old)
    rec = si.omm_record(a.satrec, "NOSS 3-4 (A) newer")
    rec["EPOCH"] = "2026-11-03T00:00:00.000000"
    jp = tmp_path / "gp.json"
    jp.write_text(json.dumps([rec]))
    got = {e.satrec.satnum: e for e in si.load_elements([str(zp), str(jp)])}
    assert got[31701].name == "NOSS 3-4 (A) newer"            # newest epoch wins
    assert got[31708].source == "old.zip:old.tle"


def test_identifies_with_a_free_time_offset_and_rejects_the_sister_satellite(elems, geom):
    c = si.identify(_obs([e for e in elems if e.satrec.satnum == 31701][0], geom),
                    elems, geom)
    assert c[0]["norad"] == 31701 and si.is_match(c[0])
    assert c[0]["offset_s"] == pytest.approx(OFFSET, abs=0.5)
    assert c[0]["rms_deg"] < 0.03 and c[0]["dir_deg"] < 2
    sister = [x for x in c if x["norad"] == 31708]
    assert not sister or not si.is_match(sister[0])              # 0.9 deg off its track


def test_wrong_direction_is_not_a_match(elems, geom):
    obs = _obs([e for e in elems if e.satrec.satnum == 31701][0], geom)
    rev = [dict(o, jd=obs[-1 - i]["jd"]) for i, o in enumerate(obs)]   # same path, backwards
    rev.sort(key=lambda o: o["jd"])
    assert not any(si.is_match(c) for c in si.identify(rev, elems, geom))


def test_refine_moves_the_elements_to_the_observations(elems, geom):
    el = [e for e in elems if e.satrec.satnum == 31701][0]
    obs = _obs(el, geom)
    c = si.identify(obs, elems, geom)[0]
    rf = si.refine(c, obs, geom)
    assert rf["rms_after_deg"] < 0.02 < rf["rms_before_deg"]
    assert abs(rf["epoch_jd"] - JD_PASS) < 1e-3
    jd = np.array([o["jd"] for o in obs])
    e, r = si.propagate([rf["satrec"]], jd)                     # NO offset needed now
    u, _ = geom.topo_unit(r[0], jd)
    assert float(np.max(si._ang(u, _truth(el, jd, geom)))) < 0.03


def _pos(satrec, jd):
    e, r = si.propagate([satrec], np.array([jd]))
    assert e[0][0] == 0
    return r[0][0]


def test_every_output_format_round_trips(tmp_path, elems, geom):
    el = [e for e in elems if e.satrec.satnum == 31701][0]
    rf = si.refine(si.identify(_obs(el, geom), elems, geom)[0], _obs(el, geom), geom)
    s = rf["satrec"]
    ref = _pos(s, JD_PASS + 0.01)
    recs = [(si.omm_record(s, "NOSS 3-4 (A)"), "test")]
    si.write_tle([((s, "NOSS 3-4 (A)"), "test")], str(tmp_path / "r.tle"))
    si.write_omm_kvn(recs, str(tmp_path / "r.kvn"))
    si.write_omm_xml(recs, str(tmp_path / "r.xml"))
    si.write_omm_json(recs, str(tmp_path / "r.json"))
    si.write_omm_csv(recs, str(tmp_path / "r.csv"))
    # TLE (fixed-width fields: ~1e-4 deg angles) -> within a few km.
    back = si.load_elements([str(tmp_path / "r.tle")])[0].satrec
    assert np.linalg.norm(_pos(back, JD_PASS + 0.01) - ref) < 5.0
    # OMM forms keep full precision -> metres.
    for name in ("r.xml", "r.json", "r.csv"):
        b = si.load_elements([str(tmp_path / name)])
        assert len(b) == 1 and b[0].satrec.satnum == 31701, name
        assert np.linalg.norm(_pos(b[0].satrec, JD_PASS + 0.01) - ref) < 0.05, name
    kvn = (tmp_path / "r.kvn").read_text()
    for key in ("CCSDS_OMM_VERS = 2.0", "OBJECT_ID = 2007-027A", "MEAN_ELEMENT_THEORY = SGP4",
                "NORAD_CAT_ID = 31701", "REF_FRAME = TEME"):
        assert key in kvn
    with open(tmp_path / "r.xml") as f:
        assert omm.parse_xml(f)                                 # sgp4's own parser


def test_catalog_numbers_beyond_five_digits():
    from sgp4.api import WGS72
    s = Satrec()
    s.sgp4init(WGS72, "i", 123456, 27000.0, 0.0, 0.0, 0.0, 0.001, 0.0, 0.9, 0.0, 0.06, 0.0)
    l1, l2 = si.tle_lines(s)
    assert l1[2:7] == "C3456" and l2[2:7] == "C3456"            # Alpha-5: C = 12
    assert Satrec.twoline2rv(l1, l2).satnum == 123456
    # Beyond 339999 there is no TLE form, and SGP4's Satrec cannot even hold
    # the number: an OMM record with a 9-digit number still loads (propagated
    # under a placeholder), keeps its number, and is written as OMM only.
    rec = si.omm_record(s, "NINE DIGITS", norad=270000001)
    rec["OBJECT_ID"] = "2026-999A"
    el = si.parse_elements_text(json.dumps([rec]), "gp.json")[0]
    assert el.norad == 270000001 and el.norad_str == "270000001"
    assert si.tle_lines(el.satrec, el.norad) is None
    assert si.omm_record(el.satrec, el.name, el.norad)["NORAD_CAT_ID"] == 270000001
    buf = io.StringIO()
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        si.write_tle([((el.satrec, el.name, el.norad), "")], os.path.join(d, "x.tle"))
        assert "cannot be written as a TLE" in open(os.path.join(d, "x.tle")).read()
    del buf


def test_iod_line_columns():
    import datetime as dt
    t = dt.datetime(1999, 4, 2, 1, 23, 34, 770000, tzinfo=dt.timezone.utc)
    jd = 2440587.5 + t.timestamp() / 86400.0                     # 1999-04-02 01:23:34.770
    ob = {"jd": jd, "ra": (13 + 24.520 / 60) * 15.0, "dec": 38 + 24.50 / 60}
    line = si.iod_line(ob, "23794", "96010A", "2701", "G", 0.1, 0.005, "S")   # 0.3 arcmin
    assert line == "23794 96 010A   2701 G 19990402012334770 17 25 1324520+382450 37 S"
    neg = si.iod_line(dict(ob, dec=-(12 + 20.0 / 60)), station="9999", pos_unc_deg=0.02)
    assert neg[47:61].endswith("-122000") and neg[62:64] == "28"   # 1.2 arcmin, rounded up
    assert neg.startswith("99999")


# ---- end to end on a synthetic capture --------------------------------------

W, H = 960, 760


def _capture(tmp_path, el, geom, n=12, exp=0.8, cadence=1.2):
    """Frames whose stars are fixed on the sky and whose satellite dashes are
    drawn from the element set (OFFSET s ahead), through LiveRecorder."""
    jd_start = JD_PASS - (n * cadence / 2) / 86400.0
    mid = _truth(el, np.array([JD_PASS]), geom)[0]
    ra0, dec0 = si.vec_to_radec(mid)
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [float(ra0), float(dec0)]
    w.wcs.crpix = [W / 2 + 0.5, H / 2 + 0.5]
    s = 13.54 / W
    th = math.radians(30)
    w.wcs.cd = np.array([[-s, 0], [0, -s]]) @ np.array([[math.cos(th), -math.sin(th)],
                                                         [math.sin(th), math.cos(th)]])
    rng = np.random.default_rng(7)
    sra, sdec = w.pixel_to_world_values(rng.uniform(30, W - 30, 60), rng.uniform(30, H - 30, 60))
    t0_unix = (jd_start - 2440587.5) * 86400.0
    rec = lc.LiveRecorder({}, types.SimpleNamespace(
        frame_width=W, frame_height=H, fov_deg=13.54, version="t",
        live_capture_max_mb=4000, live_capture_min_free_mb=0),
        directory=str(tmp_path), queue_max=64, clock=lambda: t0_unix)
    rec.sync({"on": True, "id": "20261102T022230Z", "site": SITE})
    ring = ()
    for k in range(1, n + 1):
        img = rng.normal(20, 2, (H, W)).clip(0, 255)
        sx, sy = w.world_to_pixel_values(sra, sdec)
        for x, y in zip(sx, sy):
            img[int(round(y)) - 1:int(round(y)) + 2, int(round(x)) - 1:int(round(x)) + 2] = 200
        t_start = t0_unix + (k - 1) * cadence
        jd0 = jd_start + (k - 1) * cadence / 86400.0
        path = _truth(el, jd0 + np.linspace(0, exp, 40) / 86400.0, geom)
        px, py = w.world_to_pixel_values(*si.vec_to_radec(path))
        xs = np.interp(np.linspace(0, 39, 400), np.arange(40), px)
        ys = np.interp(np.linspace(0, 39, 400), np.arange(40), py)
        dash = np.zeros((H, W))
        ds = float(np.hypot(np.diff(xs), np.diff(ys)).mean())
        for x, y in zip(xs, ys):                         # PSF-blurred dash
            xi, yi = int(round(x)), int(round(y))
            for dy in (-2, -1, 0, 1, 2):
                for dx in (-2, -1, 0, 1, 2):
                    if 0 <= xi + dx < W and 0 <= yi + dy < H:
                        dash[yi + dy, xi + dx] += 35.0 * ds * math.exp(
                            -((xi + dx - x) ** 2 + (yi + dy - y) ** 2) / 2.0)
        img = np.maximum(img, img + dash)
        ring = fm.push(ring, (k, int((t_start + exp) * 1e9), int(exp * 1e6), 4.0, 0))
        rec.offer_frame(img.astype(np.uint8), k, ring)
        rec.note_matches(k, [(y + 0.5, x + 0.5) for x, y in zip(sx, sy)],
                         [(r, d, 5.0) for r, d in zip(sra, sdec)])
        rec.note_solution({"solved": True, "seq": k, "center_ra_deg": float(ra0),
                           "center_dec_deg": float(dec0), "roll_deg": 30.0, "fov_deg": 13.54})
    rec.sync({})
    z = tmp_path / "livecap_20261102T022230Z.zip"
    import time
    t = time.monotonic() + 20
    while not z.exists() and time.monotonic() < t:
        time.sleep(0.05)
    assert z.exists()
    return str(z)


def test_end_to_end_capture_to_identification_and_files(tmp_path, elems, geom):
    el = [e for e in elems if e.satrec.satnum == 31701][0]
    z = _capture(tmp_path, el, geom)
    out = tmp_path / "out"
    res = si.run(z, elems, str(out), log=lambda *a: None)
    assert len(res) == 1
    r = res[0]
    assert len(r["obs"]) >= 10
    # Per-frame positions agree with the truth at their own times.
    jd = np.array([o["jd"] for o in r["obs"]])
    v = si.radec_to_vec([o["ra"] for o in r["obs"]], [o["dec"] for o in r["obs"]])
    assert float(np.median(si._ang(v, _truth(el, jd, geom)))) < 0.03
    assert r["match"]["norad"] == 31701
    assert r["match"]["offset_s"] == pytest.approx(OFFSET, abs=1.0)
    assert r["refined"]["rms_after_deg"] < 0.05
    files = set(os.listdir(out))
    assert {"observations_iod.txt", "observations.json", "identification.txt",
            "identification.json", "refined.tle", "refined_omm.kvn", "refined_omm.xml",
            "refined_omm.json", "refined_omm.csv"} <= files
    iod = (out / "observations_iod.txt").read_text().splitlines()
    assert len(iod) == 4                                         # --points-per-pass default
    assert all(ln.startswith("31701 07 027A") for ln in iod)
    assert all(ln[65] == "S" and ln[66] == "+" for ln in iod)    # steady, magnitude given
    assert all(ln[41:43] == "19" for ln in iod)                  # no clock record: 10 s
    txt = (out / "identification.txt").read_text()
    assert "MATCH NOSS 3-4 (A)" in txt and "s early" in txt and "magnitude" in txt
    p = r["photometry"]
    assert p["n_clean"] >= 10 and p["frame_scatter_mag"] < 0.1
    assert p["light_curve"]["behaviour"] == "S"
    assert {"lightcurve_S1.1.csv", "lightcurve_S1.1.png", "seesat_post.txt",
            "observations_rde.txt"} <= files
    rde = (out / "observations_rde.txt").read_text().splitlines()
    assert rde[0].startswith("9999 2611 9.911 ") and rde[0][19] == "5"   # header
    assert rde[1] == "02" and rde[-1] == "999" and len(rde) == 4 + 3
    assert all(ln.startswith("0702701 ") and ln.endswith(" S") for ln in rde[2:6])
    post = (out / "seesat_post.txt").read_text()
    assert "CLOCK NOT VERIFIED" in post and "NOSS 3-4 (A) (31701)" in post
    assert post.count("\n1 31701U") == 1 and "\n2 31701 " in post   # refined TLE
    doc = json.loads((out / "observations.json").read_text())
    assert doc["format"] == "diofinder-sat-observations/1"
    assert all(doc["site"][k] == v for k, v in SITE.items())
    tj = doc["tracks"][0]
    assert tj["identified"]["norad"] == 31701 and sum(o["reported"] for o in tj["observations"]) == 4
    assert all(o["sigma_deg"] > 0 for o in tj["observations"])
    # sat_orbit reads the same file: a one-pass circular orbit from the
    # observations alone.
    import sat_orbit
    passes = sat_orbit.load_passes([str(out / "observations.json")])
    assert passes[0].identified["norad"] == 31701
    fit = sat_orbit.fit(passes, log=lambda *a: None)
    assert fit["circular"] and max(fit["rms_deg"].values()) < 0.05
