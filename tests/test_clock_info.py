"""clock_info: which time source describes the clock NOW, the SkySafari
threshold, and the last-known-time save/restore across power cycles."""
import datetime as dt
import json
import types

import pytest

from diofinder import clock_info as ci
from diofinder import comms_proc as cp

NOW = dt.datetime(2026, 11, 2, 2, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def paths(tmp_path):
    return {"state": str(tmp_path / "sync.json"), "flag": str(tmp_path / "ntp"),
            "known": str(tmp_path / "known.json")}


def test_a_record_from_an_earlier_boot_is_history_not_the_source(paths):
    ci.record_sync("skysafari", "set", 3.0, path=paths["state"], now=NOW, boot="A")
    cur = ci.current(paths["state"], paths["flag"], boot="A")
    assert cur["this_boot"] and cur["trusted"] and cur["summary"].startswith("SkySafari, set")
    cur = ci.current(paths["state"], paths["flag"], boot="B")        # after a power cycle
    assert not cur["this_boot"] and not cur["trusted"]
    assert cur["summary"].startswith("NOT SET since start-up")
    open(paths["flag"], "w").close()                                  # network time
    cur = ci.current(paths["state"], paths["flag"], boot="B")
    assert cur["trusted"] and cur["summary"] == "network time (synchronized)"


def test_record_without_boot_id_counts_as_an_earlier_boot(paths):
    with open(paths["state"], "w") as f:
        json.dump({"source": "phone", "action": "set", "at_utc": "x"}, f)   # older software
    assert not ci.current(paths["state"], paths["flag"], boot="A")["trusted"]


def test_restore_moves_forward_only_and_says_it_is_not_accurate(paths):
    set_to = []
    ci.save_last_known(paths["known"], now_s=2_000_000_000.0)
    # Clock behind (restarted at the build date): moved forward.
    rec = ci.restore_at_start(paths["known"], now_s=1_990_000_000.0, setter=set_to.append,
                              state_path=paths["state"], flag_path=paths["flag"])
    assert set_to == [2_000_000_000.0] and rec["action"] == "restored"
    cur = ci.current(paths["state"], paths["flag"], boot=rec["boot_id"])
    assert not cur["trusted"] and "NOT SET since start-up: moved forward" in cur["summary"]
    # Clock already ahead: untouched (never moves back).
    assert ci.restore_at_start(paths["known"], now_s=2_000_000_100.0, setter=set_to.append,
                               state_path=paths["state"], flag_path=paths["flag"]) is None
    # Network time synchronized: untouched.
    open(paths["flag"], "w").close()
    assert ci.restore_at_start(paths["known"], now_s=1.0, setter=set_to.append,
                               state_path=paths["state"], flag_path=paths["flag"]) is None
    assert len(set_to) == 1
    # Nothing saved yet: nothing to do.
    assert ci.restore_at_start(paths["known"] + "x", now_s=1.0, setter=set_to.append,
                               flag_path=paths["flag"] + "x") is None


def test_set_clock_falls_back_to_the_helper_without_the_capability(monkeypatch):
    def denied(*a):
        raise PermissionError("no CAP_SYS_TIME")
    import time
    monkeypatch.setattr(time, "clock_settime", denied)
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    assert ci.set_clock(NOW.timestamp() + 0.4, run=run) == "helper"
    assert calls == [["sudo", ci.SET_TIME_HELPER, "2026-11-02 02:00:00"]]
    with pytest.raises(RuntimeError, match="denied"):
        ci.set_clock(NOW.timestamp(), run=lambda c, **k: types.SimpleNamespace(
            returncode=1, stdout="", stderr="denied"))


def test_skysafari_threshold():
    assert cp._skysafari_threshold({"ntp_synchronized": True}) is None
    assert cp._skysafari_threshold({}) == 2.0
    phone = {"this_boot": True, "last_sync": {"source": "phone", "action": "set"}}
    assert cp._skysafari_threshold(phone) == 10.0
    assert cp._skysafari_threshold(dict(phone, this_boot=False)) == 2.0


def test_skysafari_sync_sets_a_clock_3s_off_and_keeps_one_1s_off(monkeypatch):
    sets, recs, saves = [], [], []
    monkeypatch.setattr(ci, "current", lambda *a, **k: {"ntp_synchronized": False})
    monkeypatch.setattr(ci, "set_clock", lambda t: sets.append(t) or "direct")
    monkeypatch.setattr(ci, "record_sync", lambda *a, **k: recs.append(a))
    monkeypatch.setattr(ci, "save_last_known", lambda *a, **k: saves.append(1))
    now = dt.datetime(2026, 11, 2, 2, 0, 0)

    class FakeDT(dt.datetime):
        @classmethod
        def utcnow(cls):
            return now
    monkeypatch.setattr(cp.datetime, "datetime", FakeDT)
    # SkySafari: local 21:00:03 at UTC-5 -> 02:00:03 UTC, 3 s ahead.
    cp._sync_clock("21:00:03", "5", "11/01/26")
    assert recs[-1][:2] == ("skysafari", "set") and len(sets) == 1
    assert sets[0] == dt.datetime(2026, 11, 2, 2, 0, 3, tzinfo=dt.timezone.utc).timestamp()
    cp._sync_clock("21:00:01", "5", "11/01/26")                 # 1 s: as good as it gets
    assert recs[-1][:2] == ("skysafari", "checked") and len(sets) == 1
    assert len(saves) == 2


def test_set_clock_offset_uses_the_helpers_offset_mode(monkeypatch):
    import time as _time
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    assert ci.set_clock_offset(-0.8514, run=run, direct=False) == "helper"
    assert calls == [["sudo", ci.SET_TIME_HELPER, "--offset", "-0.851"]]

    def no_cap(*a):
        raise PermissionError("no CAP_SYS_TIME")
    monkeypatch.setattr(_time, "clock_settime", no_cap)
    assert ci.set_clock_offset(42.0, run=run) == "helper"          # falls back
    assert calls[-1][-1] == "+42.000"
    with pytest.raises(RuntimeError):
        ci.set_clock_offset(1.0, direct=False, run=lambda c, **k: types.SimpleNamespace(
            returncode=1, stdout="", stderr="denied"))
