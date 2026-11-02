"""Utilities -> Clock: set the finder's time from the phone.

The route must validate the phone time, call the existing sudo helper with
exactly one "YYYY-MM-DD HH:MM:SS" argument (the sudoers grant already exists
for comms' SkySafari sync), record the source, and report failures honestly."""
import subprocess
import time
from datetime import datetime, timezone

import pytest

A = pytest.importorskip("webui.app")


@pytest.fixture
def web(tmp_path, monkeypatch):
    from diofinder import clock_info
    monkeypatch.setattr(clock_info, "STATE_PATH", str(tmp_path / "clock.json"))
    monkeypatch.setattr(clock_info, "NTP_FLAG_PATH", str(tmp_path / "nope"))
    # record_sync/current take their paths as defaults bound at definition
    # time, so wrap them to use the temp paths.
    real_rec, real_cur = clock_info.record_sync, clock_info.current
    monkeypatch.setattr(clock_info, "record_sync",
                        lambda *a, **k: real_rec(*a, path=str(tmp_path / "clock.json"), **k))
    monkeypatch.setattr(clock_info, "current",
                        lambda *a, **k: real_cur(str(tmp_path / "clock.json"),
                                                 str(tmp_path / "nope")))
    calls, state = [], {"rc": 0}

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, state["rc"], "", "denied" if state["rc"] else "")
    monkeypatch.setattr(A.subprocess, "run", fake_run)
    return A.app.test_client(), calls, state


def test_sets_the_next_whole_phone_second_and_records_it(web):
    c, calls, _ = web
    phone_ms = int((time.time() + 42.7) * 1000)          # phone 42.7 s ahead
    r = c.post("/time/set", json={"phone_ms": phone_ms}).get_json()
    assert r["ok"], r
    assert len(calls) == 1 and calls[0][:2] == ["sudo", "/usr/local/bin/diofinder-set-time"]
    arg = calls[0][2]
    datetime.strptime(arg, "%Y-%m-%d %H:%M:%S")         # exactly one well-formed arg
    want = datetime.fromtimestamp(phone_ms / 1000 + 1, tz=timezone.utc)
    got = datetime.strptime(arg, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    assert abs((got - want).total_seconds()) <= 1.0
    assert abs(r["result"]["drift_s"] - 42.7) < 0.5
    src = c.get("/api/time").get_json()["result"]["clock"]
    assert "phone (web page), set" in src["summary"]


def test_rejects_bad_input_without_touching_the_clock(web):
    c, calls, _ = web
    assert c.post("/time/set", json={}).status_code == 400
    assert c.post("/time/set", json={"phone_ms": "soon"}).status_code == 400
    assert c.post("/time/set", json={"phone_ms": 0}).status_code == 400   # 1970
    assert c.get("/time/set").status_code == 405
    assert calls == []


def test_reports_helper_failure(web):
    c, calls, state = web
    state["rc"] = 1
    r = c.post("/time/set", json={"phone_ms": int(time.time() * 1000)})
    assert r.status_code == 500 and "denied" in r.get_json()["error"]
