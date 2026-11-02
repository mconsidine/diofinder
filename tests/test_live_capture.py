"""Live capture: the solver-side recorder, the comms commands, the web routes,
and the off-device SatChecker query builder. No hardware: the recorder runs
against a plain dict standing in for shared_cfg and a temp directory."""

import json
import os
import pathlib
import sys
import time
import types
import zipfile

import numpy as np
import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from diofinder import live_capture as lc  # noqa: E402


# ---- pure helpers ----------------------------------------------------------

def test_session_id_and_archive_name():
    import datetime as dt
    sid = lc.new_session_id(dt.datetime(2026, 11, 2, 1, 30, 45, tzinfo=dt.timezone.utc))
    assert sid == "20261102T013045Z" and lc.valid_session_id(sid)
    assert lc.archive_name(sid) == "livecap_20261102T013045Z.zip"
    assert lc.ARCHIVE_RE.match(lc.archive_name(sid))
    for bad in ("", None, "../etc", "20261102T013045", "x20261102T013045Z"):
        assert not lc.valid_session_id(bad)
    for bad in ("livecap_20261102T013045Z.zip.tmp", "../livecap_20261102T013045Z.zip",
                "livecap_20261102T013045Z.partial"):
        assert not lc.ARCHIVE_RE.match(bad)


def test_julian_date_epochs():
    assert lc.unix_to_jd(0) == 2440587.5                     # 1970-01-01T00:00Z
    assert abs(lc.unix_to_jd(946728000) - 2451545.0) < 1e-9  # J2000.0 (2000-01-01T12:00Z)


def test_fov_radius_is_half_the_diagonal():
    r = lc.fov_radius_deg(13.54, 960, 760)
    assert abs(r - 0.5 * 13.54 * (960 ** 2 + 760 ** 2) ** 0.5 / 960) < 1e-12
    assert 8.5 < r < 8.7
    assert lc.fov_radius_deg(None, 960, 760) is None


_META = {"seq": 7, "sensor_timestamp_ns": 1_000_000_000,
         "exposure_us": 500_000, "gain": 4.0,
         "wall_offset_ns": 1_793_000_000 * 10 ** 9}


def test_frame_record_uses_sensor_time_and_this_frames_solution():
    sol = {"solved": True, "center_ra_deg": 10.0, "center_dec_deg": 20.0,
           "ra_deg": 10.1, "dec_deg": 20.1, "roll_deg": 5.0, "fov_deg": 13.5,
           "matches": 30, "seq": 7, "epoch_monotonic": 1.0}
    rec = lc.frame_record("frame_000001.png", 7, _META, sol, None, 0.0)
    start = 1_793_000_001.0 - 0.5            # readout start minus exposure
    assert rec["time_source"] == "sensor"
    assert rec["exposure_s"] == 0.5 and rec["gain"] == 4.0
    assert abs(rec["jd_start"] - lc.unix_to_jd(start)) < 1e-8
    assert abs(rec["jd_mid"] - lc.unix_to_jd(start + 0.25)) < 1e-8
    assert rec["exposure_start_utc"].endswith("+00:00")
    assert rec["center_ra_deg"] == 10.0 and rec["ra_deg"] == 10.1
    assert "epoch_monotonic" not in rec and "last_solved" not in rec


def test_frame_record_unsolved_carries_last_solve_and_handover_time():
    last = {"seq": 3, "center_ra_deg": 1.0, "center_dec_deg": 2.0}
    rec = lc.frame_record("f.png", 9, None, {"solved": False, "seq": 9}, last, 1e9)
    assert rec["solved"] is False and rec["last_solved"] == last
    assert rec["time_source"] == "handover" and "jd_start" not in rec
    rec2 = lc.frame_record("f.png", 9, None, None, None, 1e9)
    assert rec2["solved"] is False and "last_solved" not in rec2


# ---- the recorder -----------------------------------------------------------

def _cfg(**kw):
    base = dict(frame_width=64, frame_height=48, fov_deg=13.54, version="test",
                live_capture_max_mb=4000.0, live_capture_min_free_mb=0.0)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _wait(pred, timeout=10.0):
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _ctl(sid="20261102T013045Z", on=True):
    return {"on": on, "id": sid, "test_mode": False,
            "site": {"latitude_deg": 44.5, "longitude_deg": -72.7, "elevation_m": 300.0}}


def _record(rec, scfg, n, solve=lambda i: True):
    frame = np.zeros((48, 64), np.uint8)
    ring = ()
    from diofinder import frame_meta as fm
    for i in range(1, n + 1):
        frame[:] = i
        ring = fm.push(ring, (i, 10 ** 9 * i, 250_000, 2.0, 1_793_000_000 * 10 ** 9))
        rec.offer_frame(frame, i, ring)
        if solve(i):
            rec.note_solution({"solved": True, "seq": i, "center_ra_deg": 100.0 + i,
                               "center_dec_deg": 20.0, "fov_deg": 13.5})
        else:
            rec.note_solution({"solved": False, "seq": i})


def test_record_and_close_a_session(tmp_path):
    scfg = {}
    rec = lc.LiveRecorder(scfg, _cfg(), directory=str(tmp_path), queue_max=64)
    rec.sync(_ctl())
    assert rec.active
    _record(rec, scfg, 5, solve=lambda i: i != 4)
    rec.sync(_ctl(on=False))
    zpath = tmp_path / "livecap_20261102T013045Z.zip"
    assert _wait(zpath.exists), scfg.get("live_capture_status")
    assert _wait(lambda: scfg["live_capture_status"]["state"] == "idle")
    assert not (tmp_path / "livecap_20261102T013045Z.partial").exists()
    with zipfile.ZipFile(zpath) as zf:
        names = zf.namelist()
        assert names[:2] == ["session.json", "frames.jsonl"]
        assert [n for n in names if n.endswith(".png")] == [
            f"frame_{i:06d}.png" for i in range(1, 6)]
        recs = [json.loads(l) for l in zf.read("frames.jsonl").decode().splitlines()]
        sess = json.loads(zf.read("session.json"))
        from PIL import Image
        import io
        img = np.array(Image.open(io.BytesIO(zf.read("frame_000003.png"))))
    # Each frame paired with ITS OWN solve (matched by seq), sensor-timed.
    assert [r["seq"] for r in recs] == [1, 2, 3, 4, 5]
    assert recs[2]["center_ra_deg"] == 103.0 and recs[2]["time_source"] == "sensor"
    assert recs[3]["solved"] is False and recs[3]["last_solved"]["center_ra_deg"] == 103.0
    assert img.shape == (48, 64) and int(img[0, 0]) == 3
    assert sess["frames"] == 5 and sess["observer"]["latitude_deg"] == 44.5
    assert sess["observer"]["elevation_m"] == 300.0
    assert sess["camera"]["fov_radius_deg"] > 6.7
    assert scfg["live_capture_status"]["last_archive"] == zpath.name


def test_frame_without_a_published_solution_is_still_recorded(tmp_path):
    scfg = {}
    rec = lc.LiveRecorder(scfg, _cfg(), directory=str(tmp_path), queue_max=64)
    rec.sync(_ctl())
    frame = np.zeros((48, 64), np.uint8)
    rec.offer_frame(frame, 1)          # no solution ever published for seq 1
    rec.offer_frame(frame, 2)          # flushes seq 1 with no solution
    rec.note_solution({"solved": False, "seq": 2})
    rec.sync({})                       # control cleared -> stop
    z = tmp_path / "livecap_20261102T013045Z.zip"
    assert _wait(z.exists)
    with zipfile.ZipFile(z) as zf:
        recs = [json.loads(l) for l in zf.read("frames.jsonl").decode().splitlines()]
    assert [r["seq"] for r in recs] == [1, 2] and recs[0]["solved"] is False


def test_same_session_never_restarts_and_new_one_waits_for_finalize(tmp_path):
    scfg = {}
    rec = lc.LiveRecorder(scfg, _cfg(), directory=str(tmp_path), queue_max=64)
    rec.sync(_ctl())
    rec.sync(_ctl(on=False))
    rec.sync(_ctl())                   # same id switched on again: ignored
    assert not rec.active
    assert _wait(lambda: rec._finalize_sid is None
                 and scfg["live_capture_status"]["state"] == "idle")
    rec.sync(_ctl(sid="20261102T020000Z"))
    assert rec.active and rec._sid == "20261102T020000Z"


def test_writer_behind_drops_frames_instead_of_blocking(tmp_path):
    scfg = {}
    rec = lc.LiveRecorder(scfg, _cfg(), directory=str(tmp_path), queue_max=1)
    rec.sync(_ctl())
    t = time.monotonic()
    _record(rec, scfg, 30)             # far faster than PNG encoding
    assert time.monotonic() - t < 1.0  # the solver loop never waited
    rec.sync({})
    assert _wait((tmp_path / "livecap_20261102T013045Z.zip").exists)
    st = scfg["live_capture_status"]
    assert st["dropped"] > 0 and st["frames"] + st["dropped"] == 30


def test_size_limit_stops_from_the_writer_thread_without_deadlock(tmp_path):
    scfg = {}
    rec = lc.LiveRecorder(scfg, _cfg(live_capture_max_mb=1e-6), directory=str(tmp_path))
    rec.sync(_ctl())
    _record(rec, scfg, 6)              # writer hits the cap on frame 1
    z = tmp_path / "livecap_20261102T013045Z.zip"
    assert _wait(z.exists)
    assert _wait(lambda: scfg["live_capture_status"]["state"] == "idle")
    st = scfg["live_capture_status"]
    assert "size limit" in st["stop_reason"] and not rec.active


def test_writer_notices_stop_while_no_frames_arrive(tmp_path, monkeypatch):
    monkeypatch.setattr(lc, "_CONTROL_POLL_S", 0.05)
    scfg = {"live_capture": _ctl()}
    rec = lc.LiveRecorder(scfg, _cfg(), directory=str(tmp_path), queue_max=64)
    rec.sync(scfg["live_capture"])
    _record(rec, scfg, 1)
    scfg["live_capture"] = _ctl(on=False)   # dark sky: no sync() calls now
    assert _wait((tmp_path / "livecap_20261102T013045Z.zip").exists)


def test_recover_partials_zips_a_crashed_session(tmp_path):
    d = tmp_path / "livecap_20261101T220000Z.partial"
    d.mkdir()
    (d / "frames.jsonl").write_text('{"seq": 1}\n')
    (d / "frame_000001.png").write_bytes(b"png")
    (tmp_path / "unrelated.partial").mkdir()
    assert lc.recover_partials(str(tmp_path)) == ["livecap_20261101T220000Z.zip"]
    with zipfile.ZipFile(tmp_path / "livecap_20261101T220000Z.zip") as zf:
        assert "recovered" in json.loads(zf.read("session.json"))["stop_reason"]
        assert set(zf.namelist()) == {"session.json", "frames.jsonl", "frame_000001.png"}
    assert not d.exists() and (tmp_path / "unrelated.partial").exists()
    assert [a["name"] for a in lc.list_archives(str(tmp_path))] == [
        "livecap_20261101T220000Z.zip"]


def test_solution_tap_passes_through_and_feeds_recorder():
    seen = []

    class Rec:
        active = True

        def note_solution(self, d):
            seen.append(d)
    store = {}
    tap = lc.SolutionTap(store, Rec())
    tap.update({"seq": 1, "solved": True})
    assert store == {"seq": 1, "solved": True} and seen == [{"seq": 1, "solved": True}]
    assert tap.get("seq") == 1             # other methods pass through
    Rec.active = False
    tap.update({"seq": 2})
    assert store["seq"] == 2 and len(seen) == 1


# ---- comms commands ---------------------------------------------------------

def test_comms_start_stop_status(tmp_path, monkeypatch):
    from diofinder import comms_proc as cp
    monkeypatch.setattr(cp.cfg_mod, "load_config", lambda *a, **k: types.SimpleNamespace(
        latitude_deg=44.5, longitude_deg=-72.7, elevation_m=300.0))
    monkeypatch.setattr(cp._horizon, "clock_is_plausible", lambda *a, **k: True)
    ctx = types.SimpleNamespace(cfg=types.SimpleNamespace(live_capture_dir=str(tmp_path)),
                                shared_cfg={"test_mode": False})
    r = cp._live_capture_cmd("live_capture_start", ctx)
    ctl = ctx.shared_cfg["live_capture"]
    assert r.ok and ctl["on"] and lc.valid_session_id(ctl["id"])
    assert ctl["site"] == {"latitude_deg": 44.5, "longitude_deg": -72.7, "elevation_m": 300.0}
    assert r.result["waiting"] and not r.result["warnings"]
    # Idempotent: a second start keeps the same session.
    assert cp._live_capture_cmd("live_capture_start", ctx).result["id"] == ctl["id"]
    ctx.shared_cfg["live_capture_status"] = {"state": "recording", "id": ctl["id"]}
    assert cp._live_capture_cmd("live_capture_status", ctx).result["recording"]
    r = cp._live_capture_cmd("live_capture_stop", ctx)
    assert r.ok and ctx.shared_cfg["live_capture"]["on"] is False
    # While the solver is still zipping, a new start is refused, not queued.
    ctx.shared_cfg["live_capture_status"] = {"state": "finalizing"}
    r = cp._live_capture_cmd("live_capture_start", ctx)
    assert not r.ok and "still being saved" in r.error


def test_comms_warns_about_clock_and_site(tmp_path, monkeypatch):
    from diofinder import comms_proc as cp
    monkeypatch.setattr(cp.cfg_mod, "load_config", lambda *a, **k: types.SimpleNamespace(
        latitude_deg=0.0, longitude_deg=0.0, elevation_m=0.0))
    monkeypatch.setattr(cp._horizon, "clock_is_plausible", lambda *a, **k: False)
    ctx = types.SimpleNamespace(cfg=types.SimpleNamespace(live_capture_dir=str(tmp_path)),
                                shared_cfg={})
    w = " ".join(cp._live_capture_cmd("live_capture_start", ctx).result["warnings"])
    assert "clock" in w and "location" in w


def test_comms_test_mode_never_records_null_island(tmp_path, monkeypatch):
    from diofinder import comms_proc as cp
    monkeypatch.setattr(cp.cfg_mod, "load_config", lambda *a, **k: types.SimpleNamespace(
        latitude_deg=0.0, longitude_deg=0.0, elevation_m=0.0,
        test_latitude_deg=0.0, test_longitude_deg=0.0))
    monkeypatch.setattr(cp._horizon, "clock_is_plausible", lambda *a, **k: True)
    ctx = types.SimpleNamespace(cfg=types.SimpleNamespace(live_capture_dir=str(tmp_path)),
                                shared_cfg={"test_mode": True})
    r = cp._live_capture_cmd("live_capture_start", ctx)
    site = ctx.shared_cfg["live_capture"]["site"]
    assert (site["latitude_deg"], site["longitude_deg"]) == (40.0, -75.0)
    assert site["source"] == "test mode (placeholder)"
    assert not any("location" in w for w in r.result["warnings"])


# ---- web routes -------------------------------------------------------------

A = pytest.importorskip("webui.app")


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_load_cfg_cached",
                        lambda: types.SimpleNamespace(live_capture_dir=str(tmp_path)))
    calls = []

    def fake_safe_call(cmd, args=None, timeout=15.0):
        calls.append(cmd)
        return types.SimpleNamespace(ok=True, result={"on": cmd.endswith("start")}, error=None)
    monkeypatch.setattr(A, "_safe_call", fake_safe_call)
    return A.app.test_client(), tmp_path, calls


def test_web_toggle_routes_to_comms(web):
    c, _, calls = web
    assert c.post("/livecap/toggle", json={"action": "start"}).get_json()["result"]["on"]
    assert c.post("/livecap/toggle", json={"action": "stop"}).get_json()["ok"]
    assert c.post("/livecap/toggle", json={"action": "rm"}).status_code == 400
    assert calls == ["live_capture_start", "live_capture_stop"]


def test_web_download_and_delete_only_finished_archives(web):
    c, d, _ = web
    good = d / "livecap_20261102T013045Z.zip"
    good.write_bytes(b"PK zip")
    (d / "livecap_20261102T013045Z.partial").mkdir()
    r = c.get("/livecap/download/" + good.name)
    assert r.status_code == 200 and r.data == b"PK zip"
    assert "attachment" in r.headers["Content-Disposition"]
    for bad in ("livecap_20261102T013045Z.partial", "..%2Fsecret.zip", "x.zip"):
        assert c.get("/livecap/download/" + bad).status_code == 404
        assert c.post("/livecap/delete/" + bad).status_code == 404
    assert c.get("/livecap/delete/" + good.name).status_code == 405
    assert c.post("/livecap/delete/" + good.name).get_json()["ok"]
    assert not good.exists()


# ---- off-device SatChecker helper ------------------------------------------

def test_satcheck_queries_from_a_real_archive(tmp_path):
    scfg = {}
    rec = lc.LiveRecorder(scfg, _cfg(), directory=str(tmp_path), queue_max=64)
    rec.sync(_ctl())
    _record(rec, scfg, 4, solve=lambda i: i != 2)
    rec.sync({})
    z = tmp_path / "livecap_20261102T013045Z.zip"
    assert _wait(z.exists)
    sys.path.insert(0, str(REPO / "tests"))
    import satcheck_archive as sa
    session, frames = sa.load_archive(str(z))
    qs = sa.build_queries(session, frames)
    assert len(qs) == 4
    p = qs[0]["params"]
    assert p["latitude"] == 44.5 and p["longitude"] == -72.7 and p["elevation"] == 300.0
    assert p["ra"] == 101.0 and p["duration"] == 0.25 and p["async"] == "False"
    assert abs(float(p["start_time_jd"]) - frames[0]["jd_start"]) < 1e-7
    assert qs[1]["pointing_source"] == "last_solved" and qs[1]["params"]["ra"] == 101.0
    g = sa.build_queries(session, frames, group=2)
    assert len(g) == 2 and g[0]["frames"] == ["frame_000001.png", "frame_000002.png"]
    assert g[0]["params"]["duration"] > 0.25
    assert sa.summarize({"data": {"satellites": {"STARLINK-1234 (45000)": {
        "positions": [{"ra": 1.0, "dec": 2.0, "julian_date": 2460000.5}]}}}}) == [
        "STARLINK-1234 (45000)"]
    assert sa.parse_satellites({"data": []}) == {} and sa.parse_satellites(None) == {}


def test_matched_stars_ride_with_their_own_frame_only(tmp_path):
    scfg = {}
    rec = lc.LiveRecorder(scfg, _cfg(), directory=str(tmp_path), queue_max=64)
    rec.sync(_ctl())
    frame = np.zeros((48, 64), np.uint8)
    rec.offer_frame(frame, 1)
    rec.note_matches(1, [(10.5, 20.5), (30.0, 40.0)], [(100.0, 20.0, 3.1), (101.0, 21.0, 4.2)])
    rec.note_solution({"solved": True, "seq": 1, "center_ra_deg": 100.5, "center_dec_deg": 20.5})
    rec.offer_frame(frame, 2)
    rec.note_matches(99, [(1.0, 1.0)], [(1.0, 1.0, 1.0)])    # another frame's: ignored
    rec.note_solution({"solved": True, "seq": 2, "center_ra_deg": 100.5, "center_dec_deg": 20.5})
    rec.sync({})
    z = tmp_path / "livecap_20261102T013045Z.zip"
    assert _wait(z.exists)
    with zipfile.ZipFile(z) as zf:
        recs = [json.loads(l) for l in zf.read("frames.jsonl").decode().splitlines()]
        sess = json.loads(zf.read("session.json"))
    # (y, x) from the solver -> [x, y, ra, dec, mag] rows.
    assert recs[0]["matched"] == [[20.5, 10.5, 100.0, 20.0, 3.1], [40.0, 30.0, 101.0, 21.0, 4.2]]
    assert "matched" not in recs[1]
    assert sess["camera"]["matched_columns"] == ["x", "y", "ra_deg", "dec_deg", "mag"]


# ---- clock source + time limit ---------------------------------------------

def test_clock_info_records_and_summarizes(tmp_path):
    from diofinder import clock_info as ci
    state, flag = str(tmp_path / "clock.json"), str(tmp_path / "synchronized")
    c = ci.current(state, flag)
    assert not c["ntp_synchronized"] and c["last_sync"] is None
    assert c["summary"].startswith("unknown")
    ci.record_sync("skysafari", "checked", 6.4, path=state)
    c = ci.current(state, flag)
    assert c["last_sync"]["action"] == "checked" and c["last_sync"]["drift_s"] == 6.4
    assert "SkySafari, checked" in c["summary"] and "6.4 s off" in c["summary"]
    ci.record_sync("phone", "set", -0.31, utc_set="2026-11-02 20:00:00", path=state)
    assert "phone (web page), set" in ci.current(state, flag)["summary"]
    open(flag, "w").close()
    assert ci.current(state, flag)["summary"] == "network time (synchronized)"


def test_session_json_carries_the_clock_source(tmp_path):
    scfg = {}
    rec = lc.LiveRecorder(scfg, _cfg(), directory=str(tmp_path), queue_max=64)
    ctl = dict(_ctl(), clock={"ntp_synchronized": False, "summary": "SkySafari, set"})
    rec.sync(ctl)
    _record(rec, scfg, 1)
    rec.sync({})
    z = tmp_path / "livecap_20261102T013045Z.zip"
    assert _wait(z.exists)
    with zipfile.ZipFile(z) as zf:
        assert json.loads(zf.read("session.json"))["clock"]["summary"] == "SkySafari, set"


def test_time_limit_stops_a_forgotten_capture(tmp_path):
    t = [1000.0]
    scfg = {}
    rec = lc.LiveRecorder(scfg, _cfg(live_capture_max_minutes=1.0), directory=str(tmp_path),
                          queue_max=64, clock=lambda: t[0])
    rec.sync(_ctl())
    _record(rec, scfg, 2)                     # recorded within the limit
    t[0] = 1000.0 + 61.0                      # a minute later, no more frames
    z = tmp_path / "livecap_20261102T013045Z.zip"
    assert _wait(z.exists)                    # writer noticed on its own
    assert "time limit (1 min)" in scfg["live_capture_status"]["stop_reason"]
    assert not rec.active


def test_zero_minutes_means_no_time_limit(tmp_path):
    rec = lc.LiveRecorder({}, _cfg(live_capture_max_minutes=0), directory=str(tmp_path),
                          clock=lambda: 1e12)
    rec._started_unix = 0.0
    assert not rec._over_time()


# ---- size cap -------------------------------------------------------------

def _arc(d, sid, mb):
    p = d / lc.archive_name(sid)
    with open(p, "wb") as f:
        f.truncate(int(mb * 1e6))
    return p.name


def test_prune_deletes_oldest_and_keeps_newest_and_protected(tmp_path):
    a = _arc(tmp_path, "20261101T010000Z", 3)
    b = _arc(tmp_path, "20261101T020000Z", 3)
    c = _arc(tmp_path, "20261101T030000Z", 3)
    assert lc.prune_archives(str(tmp_path), keep_mb=0) == []          # 0 = keep all
    assert lc.prune_archives(str(tmp_path), keep_mb=6.5, protect=a) == [b]
    assert sorted(x["name"] for x in lc.list_archives(str(tmp_path))) == [a, c]
    # The newest alone over the limit is still kept.
    assert lc.prune_archives(str(tmp_path), keep_mb=1) == [a]
    assert [x["name"] for x in lc.list_archives(str(tmp_path))] == [c]


def test_recover_partials_applies_the_cap(tmp_path):
    _arc(tmp_path, "20261101T010000Z", 3)
    _arc(tmp_path, "20261101T020000Z", 3)
    part = tmp_path / "livecap_20261101T030000Z.partial"
    part.mkdir()
    (part / "frame_000001.png").write_bytes(b"x" * 100)
    assert lc.recover_partials(str(tmp_path), keep_mb=3.5) == [lc.archive_name("20261101T030000Z")]
    names = [x["name"] for x in lc.list_archives(str(tmp_path))]
    assert names == [lc.archive_name("20261101T030000Z"), lc.archive_name("20261101T020000Z")]
