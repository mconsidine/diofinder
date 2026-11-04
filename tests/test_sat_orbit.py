"""sat_orbit: circular first orbit from one pass (with an honest period
uncertainty), full 6-element fit across passes (revolution counting resolved),
pass prediction with timing uncertainty, and the CLI files. Observations are
simulated from a real eccentric element set (NOSS 3-4 (A), e = 0.046) at
0.01 deg noise. No network, no hardware."""
import json
import math
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
import sat_orbit as so  # noqa: E402
import test_sat_identify as tsi  # noqa: E402

TRUTH = {e.satrec.satnum: e for e in si.parse_elements_text(tsi.CLASSFD, "t")}[31701].satrec
SITE = dict(tsi.SITE)
NOISE = 0.01


def _passes_of(s, jd0, hours, min_el=25.0):
    jd = jd0 + np.arange(0, hours * 3600, 20) / 86400.0
    g = si.Geometry(SITE, jd[0], jd[-1])
    _, r = si.propagate([s], jd)
    _, se = g.topo_unit(r[0], jd)
    el = np.degrees(np.arcsin(se))
    return [float(jd[k]) for k in range(1, len(jd) - 1)
            if el[k] > min_el and el[k] >= el[k - 1] and el[k] >= el[k + 1]]


PASSES = _passes_of(TRUTH, tsi.JD_PASS - 0.01, 30)


def _doc(jdc, seed, span=30.0, cadence=0.8, t_unc=0.2):
    rng = np.random.default_rng(seed)
    jd = jdc + np.arange(-span / 2, span / 2, cadence) / 86400.0
    g = si.Geometry(SITE, jd[0] - 1e-3, jd[-1] + 1e-3)
    _, r = si.propagate([TRUTH], jd)
    u, _ = g.topo_unit(r[0], jd)
    ra, dec = si.vec_to_radec(u)
    obs = [{"jd": float(j), "ra": float(a + rng.normal(0, NOISE) / math.cos(math.radians(d))),
            "dec": float(d + rng.normal(0, NOISE)), "sigma_deg": NOISE}
           for j, a, d in zip(jd, ra, dec)]
    return {"format": "diofinder-sat-observations/1", "capture": f"cap{seed}", "site": SITE,
            "clock": {"time_unc_s": t_unc}, "tracks": [
                {"id": "L1", "observations": obs, "sigma_pos_deg": NOISE, "identified": None}]}


@pytest.fixture(scope="module")
def obs_files(tmp_path_factory):
    d = tmp_path_factory.mktemp("obs")
    paths = []
    for i, p in enumerate(PASSES[:2]):
        fn = d / f"obs{i}.json"
        fn.write_text(json.dumps(_doc(p, i)))
        paths.append(str(fn))
    return paths


def _pos_err_km(satrec, jd):
    _, a = si.propagate([satrec], np.array([jd]))
    _, b = si.propagate([TRUTH], np.array([jd]))
    return float(np.linalg.norm(a[0][0] - b[0][0]))


def test_one_pass_gives_a_circular_orbit_with_an_honest_period(obs_files):
    assert (PASSES[1] - PASSES[0]) * 24 > 3          # a later, separate pass
    res = so.fit(so.load_passes(obs_files[:1]), log=lambda *a: None)
    assert res["circular"]
    assert max(res["rms_deg"].values()) < 0.03       # fits the pass itself
    true_period = 2 * math.pi / TRUTH.no_kozai
    err_s = abs(res["period_min"] - true_period) * 60
    assert err_s < 3 * res["sigma"]["period_s"]       # the model error is in the sigma
    assert res["sigma"]["period_s"] > 60             # ...which is minutes, not seconds
    assert abs(math.degrees(res["p"][1] - TRUTH.inclo)) < 1.0


def test_two_passes_pin_the_orbit_down(obs_files):
    res = so.fit(so.load_passes(obs_files), log=lambda *a: None)
    assert not res["circular"]
    true_period = 2 * math.pi / TRUTH.no_kozai
    assert abs(res["period_min"] - true_period) * 60 < 1.0
    assert res["sigma"]["period_s"] < 1.0
    assert math.hypot(res["p"][3], res["p"][4]) == pytest.approx(TRUTH.ecco, abs=0.005)
    for days in (0.5, 1.0, 3.0):
        assert _pos_err_km(res["satrec"], PASSES[0] + days) < 30.0, days


def test_prediction_matches_the_truth_and_reports_uncertainty(obs_files):
    res = so.fit(so.load_passes(obs_files), log=lambda *a: None)
    start = PASSES[1] + 0.05
    got = so.predict(res["satrec"], SITE, start, hours=30, min_elevation=25, fit_result=res)
    want = so.predict(TRUTH, SITE, start, hours=30, min_elevation=25)
    assert got and len(got) == len(want)
    for a, b in zip(got, want):
        assert abs(a["max_jd"] - b["max_jd"]) * 86400 <= 20     # 10 s grid
        assert a["max_alt"] == pytest.approx(b["max_alt"], abs=1.0)
        assert a["timing_sigma_s"] is not None and a["timing_sigma_s"] < 10
        assert not a["timing_unknown"]
        assert a["table"][0]["alt"] >= 25 and len(a["table"]) >= 3
    # One pass only: the next passes are flagged as timing-unknown, not
    # given a falsely precise time.
    one = so.fit(so.load_passes(obs_files[:1]), log=lambda *a: None)
    far = so.predict(one["satrec"], SITE, PASSES[0] + 0.5, hours=24, min_elevation=25,
                     fit_result=one)
    assert far and all(p["timing_unknown"] for p in far)
    assert "timing UNKNOWN" in so.format_passes(far, "x")


def test_start_from_a_known_element_set(obs_files, tmp_path):
    p = tmp_path / "old.tle"
    p.write_text(tsi.CLASSFD)
    start = [e for e in si.load_elements([str(p)]) if e.norad == 31701][0]
    res = so.fit(so.load_passes(obs_files[:1]), start=start,
                 log=lambda *a: None)
    # Starting from the real shape, even one pass predicts a day ahead well
    # (the element set is the object's, only its timing was off).
    assert max(res["rms_deg"].values()) < 0.03
    assert _pos_err_km(res["satrec"], PASSES[0] + 1.0) < 200


def test_cli_fit_and_predict_files(obs_files, tmp_path):
    out = tmp_path / "orbit"
    so.main(["fit", *obs_files, "--out", str(out), "--name", "TEST OBJ"])
    doc = json.loads((out / "fit.json").read_text())
    assert doc["format"] == "diofinder-sat-orbit/1" and doc["norad"] == 99999
    assert doc["intldesg"] == "99099A" and doc["tle"][0].startswith("1 99999U 99099A")
    tle = (out / "fit.tle").read_text()
    assert "TEST OBJ" in tle and "\n1 99999U" in tle
    back = si.load_elements([str(out / "fit_omm.json")])[0]
    assert _pos_err_km(back.satrec, PASSES[0] + 1.0) < 30
    so.main(["predict", str(out / "fit.json"), "--site-from", obs_files[0],
             "--start", si.so.jd_to_utc(PASSES[1] + 0.05).strftime("%Y-%m-%dT%H:%M:%S"),
             "--utc", "--hours", "30", "--min-elevation", "25", "--out", str(out)])
    pj = json.loads((out / "predictions.json").read_text())
    assert pj and pj[0]["timing_sigma_s"] < 10
    assert "Visible passes of TEST OBJ" in (out / "predictions.txt").read_text()
    # The fit as a CCSDS OEM ephemeris (GCRF), and passes predicted from it.
    import sat_oem
    so.main(["ephem", str(out / "fit.json"), "--start",
             si.so.jd_to_utc(PASSES[1] + 0.05).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "--hours", "30", "--frame", "GCRF", "--out", str(out / "fit.oem")])
    seg = sat_oem.read_oem(str(out / "fit.oem"))[0]
    assert seg["name"] == "TEST OBJ" and seg["object_id"] == "1999-099A"
    assert any("fit 1 sigma" in c for c in seg["comments"])
    so.main(["predict", str(out / "fit.oem"), "--site-from", obs_files[0],
             "--start", si.so.jd_to_utc(PASSES[1] + 0.05).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "--hours", "30", "--min-elevation", "25", "--out", str(out / "p2")])
    p2 = json.loads((out / "p2" / "predictions.json").read_text())
    assert [q["max_utc"] for q in p2] == [q["max_utc"] for q in pj]


def test_predict_every_object_in_a_file(tmp_path):
    p = tmp_path / "classfd.tle"
    p.write_text(tsi.CLASSFD)
    els = si.load_elements([str(p)])                      # 2 NOSS + 1 geostationary
    start = PASSES[0] - 0.02
    got = so.predict_all(els, SITE, start, hours=30, min_elevation=25)
    assert got and all(a["max_jd"] <= b["max_jd"] for a, b in zip(got, got[1:]))
    assert {p["norad"] for p in got} <= {31701, 31708}      # GEO skipped by default
    one = so.predict(TRUTH, SITE, start, hours=30, min_elevation=25)
    mine = [p for p in got if p["norad"] == 31701]
    assert [p["max_utc"] for p in mine] == [p["max_utc"] for p in one]
    assert all(p["epoch_age_days"] is not None and p["rate_deg_s_at_max"] > 0.1 for p in mine)
    txt = so.format_summary(got, "classfd")
    assert "NOSS 3-4 (A)" in txt and "31701" in txt
    out = tmp_path / "plan"
    so.main(["predict", str(p), "--lat", str(SITE["latitude_deg"]),
             "--lon", str(SITE["longitude_deg"]), "--start",
             si.so.jd_to_utc(start).strftime("%Y-%m-%dT%H:%M:%SZ"), "--hours", "30",
             "--min-elevation", "25", "--name", "noss", "--out", str(out)])
    rows = (out / "predictions.csv").read_text().splitlines()
    assert len(rows) == 1 + len(got) and "NOSS" in rows[1]


def test_daylight_window_says_so(tmp_path, capsys):
    p = tmp_path / "classfd.tle"
    p.write_text(tsi.CLASSFD)
    # 2026-11-02 15:00-19:00 UTC is daytime at the test site (10:00-14:00 EST).
    so.main(["predict", str(tmp_path) + os.sep, "--lat", str(SITE["latitude_deg"]),
             "--lon", str(SITE["longitude_deg"]), "--start", "2026-11-02T15:00:00",
             "--hours", "4", "--utc"])
    out = capsys.readouterr().out
    assert "NO DARK SKY" in out and "--start" in out
    assert out.count(": 0 visible pass(es)") == 1 and "\n: 0" not in out   # titled
    so.main(["predict", str(p), "--lat", str(SITE["latitude_deg"]), "--lon",
             str(SITE["longitude_deg"]), "--start", "2026-11-02T20:00:00", "--hours", "12",
             "--in-tz", "utc"])
    assert "Dark sky (Sun below -6 deg)" in capsys.readouterr().out


def _dtz(hours):
    import datetime as dt
    return dt.timezone(dt.timedelta(hours=hours))


def test_command_line_times_local_by_default_utc_on_request(monkeypatch):
    import datetime as dt
    import time
    if not hasattr(time, "tzset"):
        pytest.skip("needs time.tzset (not on Windows)")
    monkeypatch.setenv("TZ", "EST5EDT")
    time.tzset()
    try:
        utc = si.parse_time("2026-11-04T23:00:00Z")
        assert si.parse_time("2026-11-04T18:00") == pytest.approx(utc, abs=1e-8)  # local EST
        assert si.parse_time("2026-11-04 23:00", "utc") == pytest.approx(utc, abs=1e-8)
        assert si.parse_time("2026-11-04T18:00-05:00", "utc") == pytest.approx(utc, abs=1e-8)
        assert si.parse_time("2026-11-04T19:00", "-04:00") == pytest.approx(utc, abs=1e-8)
        try:
            assert si.parse_time("2026-11-04T18:00", "America/New_York") == \
                pytest.approx(utc, abs=1e-8)
        except SystemExit:
            pass                                    # no tz database installed here
        today = dt.datetime.now().astimezone().date()
        t = si.so.jd_to_utc(si.parse_time("21:30")).astimezone()
        assert (t.date(), t.hour, t.minute) == (today, 21, 30)
        # --end as a bare time earlier than --start: the next morning.
        _, hours = so.window("2026-11-04T17:00", "06:00", 24.0, "local")
        assert hours == pytest.approx(13.0, abs=1e-6)
        assert so.window("2025-06-01T17:00", "07:00")[1] == pytest.approx(14.0)  # not today
        assert so.window("2025-06-01T01:00", "07:00")[1] == pytest.approx(6.0)
        assert so.window("2026-11-04T17:00", "2026-11-05T01:30")[1] == pytest.approx(8.5)
        with pytest.raises(SystemExit):
            so.window("2026-11-04T17:00", "2026-11-04T16:00")
        with pytest.raises(SystemExit):
            si.parse_time("tonight")
        # Output zones: UTC by default, local / offset on request.
        assert si.when(utc, "utc", ms=False) == "2026-11-04 23:00:00 UTC"
        assert si.when(utc, "local", ms=False) == "2026-11-04 18:00:00 EST"
        assert si.when(utc, "-05:00", ms=False) == "2026-11-04 18:00:00 UTC-05:00"
        assert si.when(utc, "UTC+5:30", ms=False) == "2026-11-05 04:30:00 UTC+05:30"
        with pytest.raises(SystemExit):
            si.set_output_tz("Mars/Olympus")
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
        si.set_output_tz("utc")


def test_local_input_utc_output_and_json_stays_utc(tmp_path):
    p = tmp_path / "classfd.tle"
    p.write_text(tsi.CLASSFD)
    t0 = si.so.jd_to_utc(PASSES[0] - 0.02)
    args = ["predict", str(p), "--norad", "31701", "--lat", str(SITE["latitude_deg"]),
            "--lon", str(SITE["longitude_deg"]), "--hours", "30", "--min-elevation", "25"]
    local = t0.astimezone(_dtz(-5)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        so.main(args + ["--start", local, "--in-tz", "UTC-5", "--out", str(tmp_path / "u")])
        txt_u = (tmp_path / "u" / "predictions.txt").read_text()
        so.main(args + ["--start", t0.strftime("%Y-%m-%dT%H:%M:%S"), "--utc",
                        "--out-tz=-05:00", "--out", str(tmp_path / "l")])
        txt_l = (tmp_path / "l" / "predictions.txt").read_text()
    finally:
        si.set_output_tz("utc")
    ju = json.loads((tmp_path / "u" / "predictions.json").read_text())
    jl = json.loads((tmp_path / "l" / "predictions.json").read_text())
    assert ju and [q["max_utc"] for q in ju] == [q["max_utc"] for q in jl]   # same window
    assert all(q["max_utc"].endswith(" UTC") and q["start_jd"] < q["max_jd"] < q["end_jd"]
               for q in jl)                                            # JSON: always UTC
    first = si.so.jd_to_utc(ju[0]["max_jd"])
    assert first.strftime("%H:%M:%S") in txt_u and "UTC-05:00" not in txt_u
    assert first.astimezone(_dtz(-5)).strftime("%H:%M:%S") in txt_l
    assert "UTC-05:00" in txt_l and "Window: " in txt_l
    rows = (tmp_path / "l" / "predictions.csv").read_text().splitlines()
    assert rows[0].startswith("max_time,start_time,end_time,max_utc")
    assert "UTC-05:00" in rows[1] and " UTC," in rows[1]
    assert (tmp_path / "u" / "predictions.csv").read_text().startswith("max_utc,")
