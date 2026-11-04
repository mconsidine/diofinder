"""Utilities -> Site: set latitude / longitude / elevation from the web UI.

SkySafari sends latitude and longitude over LX200 (:St/:Sg) but the protocol
has no elevation command, so elevation could only be set by editing the conf.
Covered: the parser (numbers or hemisphere letters, range checks, 0..360
longitude folded), the site_get / site_set maint commands (persisted, live
cfg updated, latitude pushed to the solver), the web routes, and the card on
the Utilities page."""
import types

import pytest

from diofinder import comms_proc as cp
from diofinder import horizon


def test_parse_site_numbers_letters_and_ranges():
    p = horizon.parse_site
    assert p({"latitude_deg": "43.95", "longitude_deg": -72.667, "elevation_m": "330"}) == \
        {"latitude_deg": 43.95, "longitude_deg": -72.667, "elevation_m": 330.0}
    assert p({"latitude_deg": "33.9S", "longitude_deg": "151.2 E"}) == \
        {"latitude_deg": -33.9, "longitude_deg": 151.2}
    assert p({"longitude_deg": "72.667W"}) == {"longitude_deg": -72.667}
    assert p({"longitude_deg": "287.333"})["longitude_deg"] == pytest.approx(-72.667)
    assert p({"elevation_m": "330 m"}) == {"elevation_m": 330.0}
    assert p({"latitude_deg": "", "elevation_m": None}) == {}
    for bad in ({"latitude_deg": "91"}, {"longitude_deg": "400"},
                {"elevation_m": "12000"}, {"latitude_deg": "north"},
                {"longitude_deg": "-72W"}):
        with pytest.raises(ValueError):
            p(bad)


@pytest.fixture
def ctx(monkeypatch):
    saved, solver = [], []
    monkeypatch.setattr(cp.cfg_mod, "save_keys", lambda upd, **kw: saved.append(dict(upd)))
    monkeypatch.setattr(cp, "_call_solver",
                        lambda op, args, q, rq, **kw: solver.append((op, args)))
    c = types.SimpleNamespace(cfg=types.SimpleNamespace(latitude_deg=0.0, longitude_deg=0.0,
                                                        elevation_m=0.0),
                              solver_cmd_q=None, solver_cmd_reply_q=None, shared_cfg={})
    return c, saved, solver


def _maint(ctx, cmd, args=None):
    return cp._handle_maint_command(cp.MaintRequest(cmd=cmd, args=args or {}), ctx)


def test_site_set_persists_updates_live_cfg_and_pushes_latitude(ctx):
    import time
    c, saved, solver = ctx
    r = _maint(c, "site_get")
    assert r.ok and r.result["set"] is False
    r = _maint(c, "site_set", {"latitude_deg": "43.95", "longitude_deg": "72.667W",
                               "elevation_m": "330"})
    assert r.ok, r.error
    assert r.result == {"latitude_deg": 43.95, "longitude_deg": -72.667,
                        "elevation_m": 330.0, "set": True}
    assert saved == [{"latitude_deg": 43.95, "longitude_deg": -72.667, "elevation_m": 330.0}]
    assert (c.cfg.latitude_deg, c.cfg.longitude_deg, c.cfg.elevation_m) == \
        (43.95, -72.667, 330.0)
    for _ in range(50):                          # the solver push is on a thread
        if solver:
            break
        time.sleep(0.01)
    assert solver == [(cp.SOLVER_OP_POLAR_SET_LATITUDE, {"latitude_deg": 43.95})]
    # Elevation alone: nothing else touched, no solver call.
    solver.clear()
    assert _maint(c, "site_set", {"elevation_m": 412}).ok
    assert saved[-1] == {"elevation_m": 412.0} and c.cfg.latitude_deg == 43.95
    time.sleep(0.05)
    assert solver == []


def test_site_set_refuses_bad_or_empty_input_and_writes_nothing(ctx):
    c, saved, _ = ctx
    r = _maint(c, "site_set", {"latitude_deg": "95"})
    assert not r.ok and "latitude" in r.error
    r = _maint(c, "site_set", {})
    assert not r.ok
    assert saved == [] and c.cfg.latitude_deg == 0.0


A = pytest.importorskip("webui.app")


@pytest.fixture
def web(monkeypatch):
    calls = []

    def fake_safe_call(cmd, args=None, timeout=15.0):
        calls.append((cmd, args))
        if cmd == "site_set" and args.get("latitude_deg") == "95":
            return types.SimpleNamespace(ok=False, result=None, error="latitude 95.0 is outside")
        return types.SimpleNamespace(ok=True, error=None, result={
            "latitude_deg": 43.95, "longitude_deg": -72.667, "elevation_m": 330.0, "set": True})
    monkeypatch.setattr(A, "_safe_call", fake_safe_call)
    return A.app.test_client(), calls


def test_web_routes(web):
    c, calls = web
    assert c.get("/api/site").get_json()["result"]["elevation_m"] == 330.0
    r = c.post("/site/set", json={"latitude_deg": "43.95", "longitude_deg": "",
                                  "elevation_m": "330"})
    assert r.status_code == 200 and r.get_json()["ok"]
    assert calls[-1] == ("site_set", {"latitude_deg": "43.95", "elevation_m": "330"})
    r = c.post("/site/set", json={"latitude_deg": "95"})
    assert r.status_code == 400 and "outside" in r.get_json()["error"]


def test_utilities_page_has_the_site_card(web):
    c, _ = web
    body = c.get("/utilities").get_data(as_text=True)
    assert 'id="site-card"' in body and 'id="site-el"' in body
