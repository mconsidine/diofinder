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
             "--hours", "30", "--min-elevation", "25", "--out", str(out)])
    pj = json.loads((out / "predictions.json").read_text())
    assert pj and pj[0]["timing_sigma_s"] < 10
    assert "Visible passes of TEST OBJ" in (out / "predictions.txt").read_text()
