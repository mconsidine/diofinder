"""Utilities -> Clock: set the finder's time from the phone.

The route must validate the phone time, call the sudo helper in its
--offset mode (one process reads and sets the clock, so the helper's start-up
time doesn't count), record the source, and report failures honestly. The
helper script itself is run here with stand-in timedatectl / python."""
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


def test_sets_an_offset_with_half_the_round_trip_and_records_it(web):
    c, calls, _ = web
    phone_ms = int((time.time() + 42.7) * 1000)          # phone 42.7 s ahead
    r = c.post("/time/set", json={"phone_ms": phone_ms, "rtt_ms": 80}).get_json()
    assert r["ok"], r
    # One helper call in --offset mode: an absolute time would go stale while
    # sudo + timedatectl start up (the old 0.8-0.9 s lag).
    assert len(calls) == 1
    assert calls[0][:3] == ["sudo", "/usr/local/bin/diofinder-set-time", "--offset"]
    assert len(calls[0]) == 4
    off = float(calls[0][3])
    assert off == pytest.approx(42.7 + 0.04, abs=0.1)   # + half the 80 ms round trip
    assert r["result"]["drift_s"] == pytest.approx(42.74, abs=0.1)
    datetime.strptime(r["result"]["set_utc"], "%Y-%m-%d %H:%M:%S")
    src = c.get("/api/time").get_json()["result"]["clock"]
    assert "phone (web page), set" in src["summary"]


def test_round_trip_is_bounded(web):
    c, calls, _ = web
    now_ms = int(time.time() * 1000)
    assert c.post("/time/set", json={"phone_ms": now_ms, "rtt_ms": 1e9}).get_json()["ok"]
    assert float(calls[-1][3]) == pytest.approx(1.0, abs=0.1)     # capped at 2 s / 2
    assert c.post("/time/set", json={"phone_ms": now_ms, "rtt_ms": "x"}).get_json()["ok"]
    assert abs(float(calls[-1][3])) < 0.1


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


# ---- the helper script ------------------------------------------------------

import os
import pathlib

HELPER = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "diofinder-set-time"


@pytest.fixture
def fakes(tmp_path):
    log = tmp_path / "log.txt"
    tdc = tmp_path / "timedatectl"
    tdc.write_text(f'#!/bin/sh\necho "timedatectl $*" >> {log}\n')
    py = tmp_path / "python3"
    py.write_text(f'#!/bin/bash\necho "python ${{@: -1}}" >> {log}\n')   # the offset
    for f in (tdc, py):
        f.chmod(0o755)
    env = dict(os.environ, DIOFINDER_SET_TIME_TIMEDATECTL=str(tdc),
               DIOFINDER_SET_TIME_PYTHON=str(py))
    return env, log


def _run(env, *args):
    return subprocess.run(["bash", str(HELPER), *args], env=env,
                          capture_output=True, text=True, timeout=10)


def test_helper_offset_mode_sets_in_one_process_and_restores_ntp(fakes):
    env, log = fakes
    r = _run(env, "--offset", "-0.850")
    assert r.returncode == 0, r.stderr
    assert log.read_text().splitlines() == [
        "timedatectl set-ntp false", "python -0.850", "timedatectl set-ntp true"]


def test_helper_absolute_mode_unchanged(fakes):
    env, log = fakes
    assert _run(env, "2026-11-04 22:00:00").returncode == 0
    assert log.read_text().splitlines() == [
        "timedatectl set-ntp false", "timedatectl set-time 2026-11-04 22:00:00",
        "timedatectl set-ntp true"]


@pytest.mark.parametrize("bad", [["--offset", "soon"], ["--offset", "1;reboot"],
                                 ["--offset"], [], ["a", "b"]])
def test_helper_refuses_bad_arguments_without_touching_anything(fakes, bad):
    env, log = fakes
    assert _run(env, *bad).returncode == 2
    assert not log.exists()
