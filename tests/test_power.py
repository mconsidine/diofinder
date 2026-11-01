"""Utilities -> Power: the diofinder-power helper and the /power/<action> route.

The helper must only ever schedule `systemctl reboot|poweroff` (a few
seconds out, via systemd-run), and refuse anything else. The route must call
it through sudo with exactly the two arguments the sudoers file allows, and
report a failure instead of claiming "Rebooting…"."""

import os
import pathlib
import subprocess
import types

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
HELPER = REPO / "scripts" / "diofinder-power"


@pytest.fixture
def fake_run(tmp_path):
    rec = tmp_path / "calls.txt"
    fake = tmp_path / "systemd-run"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {rec}\n')
    fake.chmod(0o755)
    env = dict(os.environ, DIOFINDER_POWER_SYSTEMD_RUN=str(fake))
    return env, rec


@pytest.mark.parametrize("action", ["reboot", "poweroff"])
def test_helper_schedules_a_clean_systemctl_action(fake_run, action):
    env, rec = fake_run
    r = subprocess.run(["bash", str(HELPER), action], env=env,
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    args = rec.read_text().split("\n")
    assert "--on-active=3s" in args                      # response gets out first
    assert args[-3:-1] == ["/usr/bin/systemctl", action]  # and nothing else runs
    assert f"--unit=diofinder-power-{action}" in args


@pytest.mark.parametrize("bad", [[], ["halt"], ["reboot; rm -rf /"], ["--help"]])
def test_helper_refuses_anything_else(fake_run, bad):
    env, rec = fake_run
    r = subprocess.run(["bash", str(HELPER), *bad], env=env,
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 2
    assert not rec.exists(), "nothing may be scheduled for a bad argument"


def test_sudoers_allows_exactly_the_two_actions():
    lines = [l for l in (REPO / "etc/sudoers.d/diofinder-power").read_text().splitlines()
             if l.strip() and not l.startswith("#")]
    assert sorted(lines) == sorted([
        "diofinder ALL=(root) NOPASSWD: /usr/local/bin/diofinder-power reboot",
        "diofinder ALL=(root) NOPASSWD: /usr/local/bin/diofinder-power poweroff",
    ])


# ---- the web route ---------------------------------------------------------

A = pytest.importorskip("webui.app")


@pytest.fixture
def client(monkeypatch):
    calls = []
    state = {"rc": 0}

    def fake_subprocess_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, state["rc"], "", "boom" if state["rc"] else "")

    monkeypatch.setattr(A.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(A, "_load_cfg_cached",
                        lambda: types.SimpleNamespace(wifi_boot_network="saved"))
    monkeypatch.setattr(A, "_saved_network_ssid", lambda: "HomeNet")
    return A.app.test_client(), calls, state


@pytest.mark.parametrize("action,heading", [("reboot", "Rebooting"),
                                            ("poweroff", "Shutting down")])
def test_route_calls_the_helper_and_says_where_it_comes_back(client, action, heading):
    c, calls, _ = client
    r = c.post(f"/power/{action}")
    assert r.status_code == 200
    assert calls == [["sudo", "/usr/local/bin/diofinder-power", action]]
    body = r.get_data(as_text=True)
    assert heading in body and "HomeNet" in body


def test_route_reports_failure_honestly(client):
    c, _, state = client
    state["rc"] = 1
    r = c.post("/power/reboot")
    assert r.status_code == 500
    body = r.get_data(as_text=True)
    assert "didn't start" in body and "boom" in body and "Rebooting…" not in body


def test_route_rejects_other_actions_and_get(client):
    c, calls, _ = client
    assert c.post("/power/halt").status_code == 404
    assert c.get("/power/reboot").status_code == 405
    assert calls == []
