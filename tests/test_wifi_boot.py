"""Boot-network policy (scripts/diofinder-wifi-boot) + its callers ap.sh and
station.sh, exercised against a fake nmcli that keeps profile state in JSON
and records every call. No NetworkManager, radio, or root needed for the
helper tests; the ap.sh/station.sh tests need root (the scripts check EUID)
and are skipped otherwise.

The regression these guard: firstboot.sh's "is wlan0 already up?" check
never worked (awk `exit 0` in a rule still runs END's `exit 1`), so every
boot restarted the AP, and it also forced the AP's autoconnect back on,
undoing a joined network. Boot must now leave a correct connection alone."""

import json
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
HELPER = REPO / "scripts" / "diofinder-wifi-boot"
AP_SH = REPO / "scripts" / "ap.sh"
STATION_SH = REPO / "scripts" / "station.sh"

AP, ST = "diofinder-ap", "diofinder-station"

FAKE_NMCLI = textwrap.dedent(r'''
    import json, os, sys
    path = os.environ["FAKE_NM_STATE"]
    st = json.load(open(path))
    argv = sys.argv[1:]
    st["calls"].append(argv)
    # Strip global options.
    terse = False; fields = None; args = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-t": terse = True
        elif a in ("-s",): pass
        elif a in ("-f", "-g", "-w"):
            if a in ("-f", "-g"): fields = argv[i + 1]
            i += 1
        else: args.append(a)
        i += 1
    ALIAS = {"autoconnect": "connection.autoconnect",
             "ssid": "802-11-wireless.ssid"}
    def save(rc=0):
        json.dump(st, open(path, "w")); sys.exit(rc)
    if args[:2] == ["con", "show"]:
        rest = args[2:]
        if rest == ["--active"]:
            if st["active"]: print(f'{st["active"]}:wlan0')
            print("lo:lo")
        elif not rest and fields == "NAME":
            for n in st["profiles"]: print(n)
        elif not rest:
            print("NAME  AUTOCONNECT  AUTOCONNECT-PRIORITY")
            for n, p in st["profiles"].items():
                print(n, p.get("connection.autoconnect"), p.get("connection.autoconnect-priority", "0"))
        else:
            p = st["profiles"].get(rest[0])
            if p is None: save(10)
            for f in (fields or "").split(","):
                print(f"{f}:{p.get(f, '')}")
        save()
    if args[:2] == ["con", "modify"]:
        name = args[2]
        if name not in st["profiles"]: save(10)
        kv = args[3:]
        for k, v in zip(kv[0::2], kv[1::2]):
            st["profiles"][name][ALIAS.get(k, k)] = v
        save()
    if args[:2] == ["con", "add"]:
        kv = args[2:]
        d = {ALIAS.get(k, k): v for k, v in zip(kv[0::2], kv[1::2])}
        st["profiles"][d.pop("con-name")] = d
        save()
    if args[:2] == ["con", "up"]:
        if args[2] in st.get("fail_up", []): save(4)
        st["active"] = args[2]; save()
    if args[:2] == ["con", "down"]:
        st["active"] = None; save()
    save()   # radio / dev / anything else: succeed quietly
''')


def _ap(autoconnect="yes", prio="0"):
    return {"connection.autoconnect": autoconnect,
            "connection.autoconnect-priority": prio,
            "802-11-wireless.ssid": "diofinder-abcd",
            "802-11-wireless-security.psk": "12345678"}


def _st(autoconnect="yes", prio="0"):
    return {"connection.autoconnect": autoconnect,
            "connection.autoconnect-priority": prio,
            "802-11-wireless.ssid": "HomeNet"}


@pytest.fixture
def nm(tmp_path):
    """Fake NetworkManager: returns an object with .state(), .set(), .run()."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (tmp_path / "fake_nmcli.py").write_text(FAKE_NMCLI)
    shim = bindir / "nmcli"
    shim.write_text(f'#!/bin/sh\nexec {sys.executable} {tmp_path / "fake_nmcli.py"} "$@"\n')
    shim.chmod(0o755)
    ip = bindir / "ip"   # station.sh/ap.sh read the wlan0 address
    ip.write_text("#!/bin/sh\necho '    inet 192.168.1.50/24 brd 192.168.1.255'\n")
    ip.chmod(0o755)
    state_path = tmp_path / "nm.json"
    conf = tmp_path / "diofinder.conf"

    class NM:
        def set(self, profiles, active=None, pref=None, fail_up=()):
            json.dump({"profiles": profiles, "active": active, "calls": [],
                       "fail_up": list(fail_up)}, open(state_path, "w"))
            if pref is None:
                conf.write_text("# no wifi_boot_network key\n")
            else:
                conf.write_text(f"lx200_port: 4060\nwifi_boot_network: {pref}\n")

        def state(self):
            return json.load(open(state_path))

        def run(self, script, *args):
            env = dict(os.environ, FAKE_NM_STATE=str(state_path),
                       DIOFINDER_CONFIG=str(conf),
                       PATH=f"{bindir}:{os.environ['PATH']}")
            return subprocess.run(["bash", str(script), *args], env=env,
                                  capture_output=True, text=True, timeout=60)

        def calls(self, verb):
            return [c for c in self.state()["calls"] if verb in c]

        def ac(self, name):
            p = self.state()["profiles"][name]
            return p["connection.autoconnect"], p["connection.autoconnect-priority"]

    return NM()


# ---- get -------------------------------------------------------------------

@pytest.mark.parametrize("pref,expected", [
    (None, "ap"), ("ap", "ap"), ("saved", "saved"),
    ("saved   # trailing comment", "saved"), ("bogus", "ap"), ("", "ap"),
])
def test_get_reads_conf_and_defaults_to_ap(nm, pref, expected):
    nm.set({AP: _ap()}, pref=pref)
    assert nm.run(HELPER, "get").stdout.strip() == expected


# ---- apply -----------------------------------------------------------------

def test_apply_ap_makes_the_ap_win_at_boot(nm):
    nm.set({AP: _ap("no"), ST: _st("yes", "0")}, pref="ap")
    r = nm.run(HELPER, "apply")
    assert r.returncode == 0, r.stdout + r.stderr
    assert nm.ac(AP) == ("yes", "10")
    assert nm.ac(ST) == ("yes", "0")          # still saved, but loses to the AP
    assert nm.state()["profiles"][AP]["connection.autoconnect-retries"] == "0"


def test_apply_saved_makes_the_saved_network_win(nm):
    nm.set({AP: _ap("yes", "10"), ST: _st("yes", "0")}, pref="saved")
    assert nm.run(HELPER, "apply").returncode == 0
    assert nm.ac(ST) == ("yes", "10")
    assert nm.ac(AP) == ("no", "0")


def test_apply_saved_without_a_saved_network_keeps_the_ap(nm):
    nm.set({AP: _ap("no")}, pref="saved")
    r = nm.run(HELPER, "apply")
    assert r.returncode == 0
    assert "no saved network yet" in r.stdout
    assert nm.ac(AP) == ("yes", "10")


def test_apply_argument_overrides_conf(nm):
    nm.set({AP: _ap(), ST: _st()}, pref="ap")
    nm.run(HELPER, "apply", "saved")
    assert nm.ac(ST) == ("yes", "10") and nm.ac(AP) == ("no", "0")


def test_apply_rejects_unknown_preference(nm):
    nm.set({AP: _ap(), ST: _st()}, pref="ap")
    r = nm.run(HELPER, "apply", "station")
    assert r.returncode == 2
    assert nm.calls("modify") == []


@pytest.mark.parametrize("pref", ["ap", "saved"])
def test_apply_never_changes_the_current_connection(nm, pref):
    nm.set({AP: _ap(), ST: _st()}, active=ST, pref=pref)
    nm.run(HELPER, "apply")
    assert nm.calls("up") == [] and nm.calls("down") == []
    assert nm.state()["active"] == ST


# ---- boot (called by firstboot.sh) ----------------------------------------

def test_boot_leaves_an_already_up_ap_alone(nm):
    """THE regression: the old check restarted the AP on every boot."""
    nm.set({AP: _ap("yes", "10"), ST: _st()}, active=AP, pref="ap")
    r = nm.run(HELPER, "boot")
    assert r.returncode == 0
    assert nm.calls("up") == [], "boot must not restart a correct connection"
    assert "leaving it" in r.stdout


def test_boot_ap_pref_repairs_a_station_left_from_old_profiles(nm):
    # The first boot after this change on a device whose profiles were left
    # in the old station.sh shape (AP autoconnect=no): NM joined the saved
    # network; preference is AP -> switch once, and fix the profiles.
    nm.set({AP: _ap("no"), ST: _st("yes")}, active=ST, pref="ap")
    r = nm.run(HELPER, "boot")
    assert "one-time repair" in r.stdout
    assert nm.state()["active"] == AP
    assert nm.ac(AP) == ("yes", "10") and nm.ac(ST) == ("yes", "0")


def test_boot_ap_pref_idle_activates_the_ap(nm):
    nm.set({AP: _ap(), ST: _st()}, active=None, pref="ap")
    nm.run(HELPER, "boot")
    assert nm.state()["active"] == AP


def test_boot_ap_up_failure_is_not_fatal(nm):
    nm.set({AP: _ap()}, active=None, pref="ap", fail_up=[AP])
    r = nm.run(HELPER, "boot")
    assert r.returncode == 0
    assert "ensure-ap will retry" in r.stdout


def test_boot_saved_pref_keeps_the_saved_network(nm):
    nm.set({AP: _ap("no"), ST: _st("yes", "10")}, active=ST, pref="saved")
    nm.run(HELPER, "boot")
    assert nm.calls("up") == [] and nm.state()["active"] == ST


def test_boot_saved_pref_idle_leaves_it_to_ensure_ap(nm):
    nm.set({AP: _ap("no"), ST: _st("yes", "10")}, active=None, pref="saved")
    r = nm.run(HELPER, "boot")
    assert nm.calls("up") == []
    assert "ensure-ap falls back" in r.stdout


def test_boot_saved_pref_leaves_a_fallback_ap_alone(nm):
    nm.set({AP: _ap("no"), ST: _st("yes", "10")}, active=AP, pref="saved")
    nm.run(HELPER, "boot")
    assert nm.calls("up") == [] and nm.ac(AP) == ("no", "0")


# ---- callers: station.sh / ap.sh (they require root) -----------------------

needs_root = pytest.mark.skipif(os.geteuid() != 0, reason="script checks EUID")


@needs_root
def test_joining_a_network_no_longer_changes_the_boot_network(nm):
    nm.set({AP: _ap("yes", "10")}, active=AP, pref="ap")
    r = nm.run(STATION_SH, "HomeNet", "password1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert nm.state()["active"] == ST                  # joined for now
    assert nm.ac(AP) == ("yes", "10")                  # AP still wins at boot
    assert nm.ac(ST) == ("yes", "0")
    assert "its own access point" in r.stdout


@needs_root
def test_joining_with_saved_pref_makes_it_the_boot_network(nm):
    nm.set({AP: _ap("yes", "10")}, active=AP, pref="saved")
    nm.run(STATION_SH, "HomeNet", "password1")
    assert nm.ac(ST) == ("yes", "10") and nm.ac(AP) == ("no", "0")


@needs_root
def test_failed_join_restores_the_ap_without_changing_the_setting(nm):
    nm.set({AP: _ap("no", "0"), ST: _st("yes", "10")}, active=ST,
           pref="saved", fail_up=[ST])
    r = nm.run(STATION_SH, "HomeNet", "password1")
    assert r.returncode == 1
    assert nm.state()["active"] == AP                  # reachable again now
    assert nm.ac(AP) == ("no", "0")                    # boot setting untouched


@needs_root
def test_ap_sh_brings_up_the_ap_without_flipping_a_saved_setting(nm):
    # What the watchdog does when the saved network drops mid-session.
    nm.set({AP: _ap("no", "0"), ST: _st("yes", "10")}, active=None, pref="saved")
    r = nm.run(AP_SH)
    assert r.returncode == 0, r.stdout + r.stderr
    assert nm.state()["active"] == AP
    assert nm.ac(AP) == ("no", "0") and nm.ac(ST) == ("yes", "10")


@needs_root
def test_ap_sh_with_ap_setting_keeps_the_ap_as_boot_network(nm):
    nm.set({AP: _ap("no", "0"), ST: _st("yes", "10")}, active=ST, pref="ap")
    nm.run(AP_SH)
    assert nm.state()["active"] == AP
    assert nm.ac(AP) == ("yes", "10") and nm.ac(ST) == ("yes", "0")


# ---- the bug itself, pinned -----------------------------------------------

def test_old_awk_check_was_always_false():
    """Documents why the old firstboot check never worked."""
    old = """awk -F: '$2=="wlan0"{exit 0} END{exit 1}'"""
    rc = subprocess.run(["bash", "-c", f"printf 'x:wlan0\\n' | {old}"]).returncode
    assert rc == 1
    assert old not in (REPO / "scripts" / "firstboot.sh").read_text()
