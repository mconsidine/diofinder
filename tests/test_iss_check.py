"""iss_check: a synthetic capture of an ISS pass, drawn from an ISS-like
element set with the finder's clock 0.7 s behind, is analysed against that
element set: across-track error ~0, timing offset +0.7 s recovered, a
one-pass circular orbit compared with the truth. Also the element-source
handling every script shares (folders, comma lists, .gz, KVN, newest
epoch wins). No network."""
import gzip
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

from sgp4.api import Satrec, WGS72  # noqa: E402
from sgp4 import exporter  # noqa: E402

import iss_check as ic  # noqa: E402
import sat_identify as si  # noqa: E402
import test_sat_identify as tsi  # noqa: E402

CLOCK_BEHIND_S = 0.7


def _iss_satrec(epoch_jd, node_deg=120.0, mo_deg=0.0):
    s = Satrec()
    s.sgp4init(WGS72, "i", 25544, epoch_jd - 2433281.5, 3e-4, 0.0, 0.0, 0.0005,
               math.radians(100.0), math.radians(51.64), math.radians(mo_deg),
               15.5 * 2 * math.pi / 1440.0, math.radians(node_deg))
    s.intldesg = "98067A"
    s.classification = "U"
    return s


def _tle_file(path, satrec):
    l1, l2 = exporter.export_tle(satrec)
    path.write_text(f"ISS (ZARYA)\n{l1}\n{l2}\n")
    return si.load_elements([str(path)])[0]


def _find_pass(el, start_jd, min_el=35.0, max_el=60.0):
    jd = start_jd + np.arange(0, 2 * 86400, 10) / 86400.0
    g = si.Geometry(tsi.SITE, jd[0], jd[-1])
    _, r = si.propagate([el.satrec], jd)
    _, se = g.topo_unit(r[0], jd)
    el_deg = np.degrees(np.arcsin(se))
    for k in range(1, len(jd) - 1):
        if min_el <= el_deg[k] <= max_el and el_deg[k] >= el_deg[k - 1] and \
                el_deg[k] >= el_deg[k + 1]:
            return float(jd[k])
    raise AssertionError("no suitable pass")


@pytest.fixture(scope="module")
def iss_case(tmp_path_factory):
    d = tmp_path_factory.mktemp("iss")
    el = _tle_file(d / "iss.tle", _iss_satrec(tsi.JD_PASS))
    jd_pass = _find_pass(el, tsi.JD_PASS)
    geom = si.Geometry(tsi.SITE, jd_pass - 700 / 86400.0, jd_pass + 700 / 86400.0)
    z = tsi._capture(d, el, geom, n=12, exp=0.3, cadence=0.5, jd_pass=jd_pass,
                     offset=CLOCK_BEHIND_S)
    return d, z, el


def test_iss_pass_against_its_elements(iss_case):
    d, z, el = iss_case
    out = d / "out"
    rep = ic.run(z, [str(d / "iss.tle")], str(out), log=lambda *a: None, hours=12)
    assert rep["iss_tracks"], rep
    assert rep["astrometry"]["cross_track_rms_deg"] < 0.02
    assert rep["timing"]["offset_s"] == pytest.approx(CLOCK_BEHIND_S, abs=0.15)
    assert rep["timing"]["finder_clock_error_s"] == pytest.approx(-CLOCK_BEHIND_S, abs=0.15)
    orb = rep["orbit_from_scratch"]
    assert abs(orb["inclination_error_deg"]) < 3.0
    assert abs(orb["period_error_s"]) < 3 * orb["period_sigma_s"]
    txt = (out / "iss_report.txt").read_text()
    assert "ASTROMETRY" in txt and "BEHIND" in txt and "ORBIT FROM THIS PASS" in txt
    for f in ("iss_report.json", "iss_residuals.png", "iss_sky.png", "observations_iod.txt"):
        assert (out / f).exists(), f


def test_no_iss_in_the_capture_is_reported(iss_case, tmp_path):
    d, z, el = iss_case
    # Elements for an ISS on the other side of the Earth: no match.
    other = _tle_file(tmp_path / "far.tle", _iss_satrec(tsi.JD_PASS, node_deg=300.0,
                                                        mo_deg=180.0))
    del other
    rep = ic.run(z, [str(tmp_path / "far.tle")], str(tmp_path / "o"), log=lambda *a: None)
    assert not rep["iss_tracks"]
    assert "NO ISS TRAIL FOUND" in (tmp_path / "o" / "iss_report.txt").read_text()


def test_element_sources_folders_lists_gz_kvn_newest_wins(tmp_path):
    old = _iss_satrec(tsi.JD_PASS - 5)
    new = _iss_satrec(tsi.JD_PASS - 1)
    (tmp_path / "a").mkdir()
    l1, l2 = exporter.export_tle(old)
    (tmp_path / "a" / "old.tle").write_text(f"ISS OLD\n{l1}\n{l2}\n")
    rec = si.omm_record(new, "ISS NEW")
    si.write_omm_kvn([(rec, "")], str(tmp_path / "a" / "new.kvn"))
    l1, l2 = exporter.export_tle(tsi.si.load_elements(
        [str(tmp_path / "a" / "old.tle")])[0].satrec)
    with gzip.open(tmp_path / "b.tle.gz", "wt") as f:
        f.write(tsi.CLASSFD)
    got = {e.norad: e for e in si.load_elements([str(tmp_path / "a")])}
    assert got[25544].name == "ISS NEW"                         # KVN, newer epoch
    got = {e.norad: e for e in si.load_elements(
        [f"{tmp_path / 'a' / 'old.tle'},{tmp_path / 'b.tle.gz'}"])}
    assert set(got) == {25544, 31701, 31708, 20963} and got[25544].name == "ISS OLD"
    near = {e.norad: e for e in si.load_elements([str(tmp_path / "a")],
                                                 near_jd=tsi.JD_PASS - 5)}
    assert near[25544].name == "ISS OLD"                        # closest epoch instead
    with pytest.raises(SystemExit):
        si.load_elements([str(tmp_path / "missing.tle")])
    assert json.dumps(si.expand_element_sources([str(tmp_path / "a")]))   # plain paths


def test_url_cache_fresh_reuse_and_offline_fallback(tmp_path, monkeypatch):
    import time
    import urllib.request
    calls = []

    class _Resp:
        def __init__(self, data):
            self.data = data

        def read(self):
            return self.data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def online(req, timeout=60):
        calls.append(req.full_url)
        return _Resp(tsi.CLASSFD.encode())

    url = "https://example.invalid/gp.php?CATNR=31701&FORMAT=TLE"
    monkeypatch.setattr(urllib.request, "urlopen", online)
    p1 = si.fetch_url(url, str(tmp_path), log=lambda *a: None)
    p2 = si.fetch_url(url, str(tmp_path), log=lambda *a: None)
    assert p1 == p2 and len(calls) == 1                  # fresh copy reused, no download
    old = time.time() - 3 * 3600
    os.utime(p1, (old, old))                             # now older than 2 h

    def offline(req, timeout=60):
        raise OSError("no network")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    msgs = []
    assert si.fetch_url(url, str(tmp_path), log=msgs.append) == p1   # stale copy used
    assert any("cached copy" in m for m in msgs)
    with pytest.raises(SystemExit):
        si.fetch_url(url + "&x=1", str(tmp_path), log=lambda *a: None)  # nothing cached
