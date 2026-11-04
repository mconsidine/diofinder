"""sat_oem (CCSDS OEM ephemerides) and sat_orbit's sky-region filter.
OEMs written from a real element set in TEME, GCRF and ITRF, as KVN and XML,
read back and interpolated to well within a metre of SGP4; passes predicted
from an OEM equal those from the element set; other time systems and the
day-of-year epoch form; OEM files ignored as element sources. The sky
filter keeps whole passes in a half/quadrant of the sky, or cuts passes to
it. No network, no hardware."""
import json
import os
import sys

import numpy as np
import pytest

pytest.importorskip("sgp4")
pytest.importorskip("astropy")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import sat_identify as si  # noqa: E402
import sat_oem  # noqa: E402
import sat_orbit as so  # noqa: E402
import test_sat_identify as tsi  # noqa: E402

ELS = {e.norad: e for e in si.parse_elements_text(tsi.CLASSFD, "t")}
NOSS = ELS[31701]
SITE = dict(tsi.SITE)
JD0 = tsi.JD_PASS - 0.25
HOURS = 30.0


def _segment(frame, el=NOSS, hours=HOURS, step=60.0):
    return sat_oem.segment_from_satrec(el.satrec, el.name, sat_oem._object_id(
        el.satrec, el.name, el.norad), JD0, JD0 + hours / 24.0, step, frame)


@pytest.mark.parametrize("frame", ["TEME", "GCRF", "ITRF"])
@pytest.mark.parametrize("xml", [False, True])
def test_round_trip_interpolates_to_sgp4(tmp_path, frame, xml):
    seg = _segment(frame, hours=6)
    path = tmp_path / ("e.xml" if xml else "e.oem")
    (sat_oem.write_xml if xml else sat_oem.write_kvn)([seg], str(path), comment="test")
    assert sat_oem.is_oem(str(path))
    back = sat_oem.read_oem(str(path))
    assert len(back) == 1 and back[0]["frame"] == frame and back[0]["name"] == NOSS.name
    assert back[0]["object_id"] == "2007-027A" and len(back[0]["jd"]) == 361
    # Off the 60 s nodes (17 s in), back in TEME: within a metre of SGP4.
    jd = JD0 + (np.arange(0, 6 * 3600 - 60, 97) + 17) / 86400.0
    e, r = sat_oem.teme_positions(back, jd)
    _, r0, _ = sat_oem.states(NOSS.satrec, jd)
    assert np.all(e == 0)
    assert np.max(np.linalg.norm(r - r0, axis=1)) < 1e-3          # km
    # Outside the span: an error code, not an extrapolation.
    e, _ = sat_oem.teme_positions(back, np.array([JD0 - 0.01, JD0 + 0.5]))
    assert list(e) == [1, 1]


def test_predict_from_an_oem_equals_the_element_set(tmp_path):
    p = tmp_path / "two.oem"
    sat_oem.write_kvn([_segment("GCRF"), _segment("TEME", ELS[31708])], str(p))
    got = so.predict_oem(sat_oem.read_oem(str(p)), SITE, JD0, HOURS, 25.0)
    want = so.predict_all([NOSS, ELS[31708]], SITE, JD0, HOURS, 25.0)
    assert got and [q["max_utc"] for q in got] == [q["max_utc"] for q in want]
    assert {q["norad_str"] for q in got} == {"2007-027A", "2007-027C"}
    assert all(q["epoch_age_days"] is None for q in got)
    assert "ephemeris" in so.format_summary(got, "two.oem")
    # The CLI: an OEM given to predict, and ephem writing one.
    out = tmp_path / "o"
    start = si.so.jd_to_utc(JD0).strftime("%Y-%m-%dT%H:%M:%SZ")
    so.main(["predict", str(p), "--lat", str(SITE["latitude_deg"]), "--lon",
             str(SITE["longitude_deg"]), "--start", start, "--hours", str(HOURS),
             "--min-elevation", "25", "--out", str(out)])
    assert "covers" in (out / "predictions.txt").read_text()
    assert len(json.loads((out / "predictions.json").read_text())) == len(want)
    tle = tmp_path / "c.tle"
    tle.write_text(tsi.CLASSFD)
    so.main(["ephem", str(tle), "--norad", "31701", "--start", start, "--hours", "2",
             "--step", "30", "--frame", "itrf", "--xml", "--out", str(tmp_path / "x")])
    seg = sat_oem.read_oem(str(tmp_path / "x" / "ephemeris_oem.xml"))[0]
    assert seg["frame"] == "ITRF" and len(seg["jd"]) == 241


def test_other_time_systems_and_day_of_year_epochs():
    text = """CCSDS_OEM_VERS = 2.0
CREATION_DATE = 2026-11-04T00:00:00
ORIGINATOR = test

META_START
OBJECT_NAME = X
OBJECT_ID = 2026-001A
CENTER_NAME = EARTH
REF_FRAME = EME2000
TIME_SYSTEM = TAI
START_TIME = 2026-308T00:00:37
STOP_TIME = 2026-308T00:02:37
META_STOP
COMMENT a comment
2026-308T00:00:37 7000 0 0 0 7.5 0
2026-11-04T00:01:37.000 6999 450 0 -0.1 7.5 0
2026-308T00:02:37 6996 900 0 -0.2 7.5 0

COVARIANCE_START
EPOCH = 2026-308T00:00:37
COV_REF_FRAME = EME2000
1
0 1
COVARIANCE_STOP
"""
    seg = sat_oem.parse_kvn(text)[0]
    utc0 = si.parse_time("2026-11-04T00:00:00Z")
    assert seg["jd"][0] == pytest.approx(utc0, abs=1e-8)            # TAI - 37 s
    assert seg["start"] == pytest.approx(utc0, abs=1e-8)
    assert seg["frame"] == "EME2000" and seg["comments"] == ["a comment"]
    assert len(seg["jd"]) == 3 and seg["degree"] == 7
    with pytest.raises(SystemExit):
        sat_oem.parse_kvn(text.replace("EME2000", "MOD"))


def test_oem_files_are_not_element_sets(tmp_path):
    (tmp_path / "a.tle").write_text(tsi.CLASSFD)
    sat_oem.write_xml([_segment("TEME", hours=1)], str(tmp_path / "b.xml"))
    sat_oem.write_kvn([_segment("TEME", hours=1)], str(tmp_path / "c.kvn"))
    got = si.load_elements([str(tmp_path)])
    assert {e.norad for e in got} == {31701, 31708, 20963}
    assert ".gp" in si.ELEMENT_SUFFIXES


def test_sky_halves():
    assert so.parse_sky("east") == ("east",)
    assert so.parse_sky("NE") == ("north", "east")
    assert so.parse_sky("south, west") == ("south", "west")
    assert so.parse_sky(None) is None and so.parse_sky("all") is None
    for bad in ("east,west", "up", "n,s"):
        with pytest.raises(SystemExit):
            so.parse_sky(bad)
    az = np.array([10.0, 100.0, 190.0, 280.0])
    assert list(so.in_sky(az, ("east",))) == [True, True, False, False]
    assert list(so.in_sky(az, ("north",))) == [True, False, False, True]
    assert list(so.in_sky(az, ("south", "west"))) == [False, False, True, False]


def test_sky_filter_whole_passes_and_parts():
    sky = so.Sky(SITE, JD0, 48.0)
    every = so.predict(NOSS.satrec, SITE, JD0, 48.0, 25.0, sky=sky)
    assert len(every) >= 3
    for halves in (("east",), ("west",), ("north",), ("south",), ("north", "east")):
        whole = so.predict(NOSS.satrec, SITE, JD0, 48.0, 25.0, sky=sky, region=halves)
        part = so.predict(NOSS.satrec, SITE, JD0, 48.0, 25.0, sky=sky, region=halves,
                          region_whole=False)
        # Whole passes are unfiltered passes, unchanged, lying entirely in the region.
        assert {q["max_utc"] for q in whole} <= {q["max_utc"] for q in every}
        for q in whole + part:
            azs = [t["az"] for t in q["table"]] + [q["max_az"]]
            assert np.all(so.in_sky(np.array(azs), halves)), (halves, azs)
            assert q["end_jd"] > q["start_jd"]
        assert len(part) >= len(whole)
    # Every pass is in some half: east/west whole + those crossing the meridian.
    e = so.predict(NOSS.satrec, SITE, JD0, 48.0, 25.0, sky=sky, region=("east",))
    w = so.predict(NOSS.satrec, SITE, JD0, 48.0, 25.0, sky=sky, region=("west",))
    assert len(e) + len(w) <= len(every)
    assert so.sky_text(("east",)) == "Sky: only passes entirely east of the meridian"
