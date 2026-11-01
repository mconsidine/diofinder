"""LX200 site/time boundary: longitude sign convention (:Sg/:Gg), site
coordinate formatting (:Gt/:Gg), and the test-mode epoch parser.

LX200 (Meade, SkySafari's LX200 drivers, OnStep, AstroKeith's eFinder) sends
longitude WEST-positive; diofinder stores it East-positive (conf, Config page
"+E", horizon.local_sidereal_time_deg). Before the fix :Sg stored the raw
value, so a US site came out ~145 deg of sidereal time wrong."""

import datetime
import types

import pytest

from diofinder import horizon


# ---- pure conversions -------------------------------------------------------

@pytest.mark.parametrize("lx200,east", [
    (72.0 + 40 / 60, -(72.0 + 40 / 60)),   # Vermont, signed form "+072*40"
    (350.0, 10.0),                          # 10 E, classic unsigned 0..360
    (-10.0, 10.0),                          # 10 E, signed form "-010*00"
    (0.0, 0.0),
    (180.0, 180.0),
    (270.0, 90.0),
])
def test_lx200_longitude_to_east(lx200, east):
    assert horizon.lx200_longitude_to_east(lx200) == pytest.approx(east)


@pytest.mark.parametrize("east", [-72.6667, 10.0, 0.0, 179.5, -179.5, 180.0])
def test_longitude_round_trip(east):
    w = horizon.east_longitude_to_lx200(east)
    assert horizon.lx200_longitude_to_east(w) == pytest.approx(east)


@pytest.mark.parametrize("s,expected", [
    ("2026-07-23T01:24:00.798+00:00", datetime.datetime(2026, 7, 23, 1, 24, 0, 798000)),
    ("2026-07-23T01:24:00.798+00:00Z", datetime.datetime(2026, 7, 23, 1, 24, 0, 798000)),
    ("2026-07-23T01:24:00Z", datetime.datetime(2026, 7, 23, 1, 24, 0)),
    ("2026-07-23T01:24:00", datetime.datetime(2026, 7, 23, 1, 24, 0)),
    ("2026-07-23 01:24:00", datetime.datetime(2026, 7, 23, 1, 24, 0)),
    ("2026-07-22T21:24:00-04:00", datetime.datetime(2026, 7, 23, 1, 24, 0)),  # converted
    ("  2026-01-01T00:00:00Z  ", datetime.datetime(2026, 1, 1)),
])
def test_parse_utc_timestamp_accepts_what_diofinder_writes(s, expected):
    got = horizon.parse_utc_timestamp(s)
    assert got == expected and got.tzinfo is None


@pytest.mark.parametrize("s", [None, "", "   ", "garbage", "2026-13-01T00:00:00Z"])
def test_parse_utc_timestamp_rejects_junk(s):
    assert horizon.parse_utc_timestamp(s) is None


# ---- through the real LX200 handler ----------------------------------------

comms = pytest.importorskip("diofinder.comms_proc")   # needs numpy


@pytest.fixture
def lx(monkeypatch):
    saved = {}
    monkeypatch.setattr(comms.cfg_mod, "save_keys", lambda d: saved.update(d))
    cfg = types.SimpleNamespace(latitude_deg=0.0, longitude_deg=0.0)

    def run(cmd):
        return comms._handle_lx200_command(cmd, {}, None, {}, cfg, {}, None, None)

    return run, cfg, saved


@pytest.mark.parametrize("cmd,east", [
    (":Sg+072*40", -(72.0 + 40 / 60)),
    (":Sg072*40", -(72.0 + 40 / 60)),
    (":Sg350*00", 10.0),
    (":Sg-010*00", 10.0),
])
def test_sg_stores_east_positive(lx, cmd, east):
    run, cfg, saved = lx
    assert run(cmd) == b"1"
    assert cfg.longitude_deg == pytest.approx(east)
    assert saved["longitude_deg"] == pytest.approx(east)    # what lands in the conf


def test_gg_replies_west_positive_and_round_trips(lx):
    run, cfg, _ = lx
    run(":Sg+072*40")
    assert run(":Gg") == b"+072*40#"
    cfg.longitude_deg = 10.0               # an eastern site set on the Config page
    assert run(":Gg") == b"-010*00#"


def test_site_formatting_never_prints_60_minutes(lx):
    run, cfg, _ = lx
    cfg.latitude_deg = 43.9999
    assert run(":Gt") == b"+44*00#"
    cfg.latitude_deg = -33.5
    assert run(":Gt") == b"-33*30#"
    cfg.longitude_deg = -72.99999          # 72.99999 W
    assert run(":Gg") == b"+073*00#"


def test_sg_garbage_is_rejected_and_not_saved(lx):
    run, cfg, saved = lx
    assert run(":Sgnope") == b"0"
    assert cfg.longitude_deg == 0.0 and saved == {}


def test_test_mode_epoch_accepts_frames_json_timestamp(monkeypatch):
    """The exact value from the bench conf now pins the clock instead of
    silently falling back to the wall clock."""
    seen = {}
    real_lst = horizon.local_sidereal_time_deg

    def spy(dt, lon):
        seen["dt"], seen["lon"] = dt, lon
        return real_lst(dt, lon)

    monkeypatch.setattr(comms._horizon, "local_sidereal_time_deg", spy)
    cfg = types.SimpleNamespace(
        latitude_deg=0.0, longitude_deg=0.0,
        test_latitude_deg=43.95, test_longitude_deg=-72.6667,
        test_epoch_utc="2026-07-23T01:24:00.798+00:00Z")
    sol = {"solved": True, "ra_deg": 280.0, "dec_deg": 38.8}
    out = comms._report_altaz({"test_mode": True}, sol, cfg)
    assert out is not None
    assert seen["dt"] == datetime.datetime(2026, 7, 23, 1, 24, 0, 798000)
    assert seen["lon"] == pytest.approx(-72.6667)
