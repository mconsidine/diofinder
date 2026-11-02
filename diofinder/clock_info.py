"""Where the finder's clock got its time, for anything that depends on it.

The Pi Zero 2W has no real-time clock. The time comes from network time
(systemd-timesyncd, when the finder is on a network with internet) or from
SkySafari's LX200 time sync (``:SL``/``:SG``/``:SC`` on connect), which sets the
clock (whole seconds) when it is more than ~2 s off and network time isn't synchronized.

``record_sync`` stores the most recent manual check/set (SkySafari, or the
Utilities page's "set from this phone" button) in a small JSON file, tagged
with the kernel boot id; ``current`` reports it alongside whether network time
is synchronized right now. A record from an EARLIER boot says nothing about
the clock now (the Pi lost its time when it powered off), so it is reported
as history, never as the current source. Live captures copy ``current()``
into ``session.json`` so satellite timing can be judged.

Last-known time: while the clock is trusted (network time, or set from
SkySafari / a phone this boot) comms saves it every few minutes
(``save_last_known``); at the next start the launcher moves the clock forward
to it (``restore_at_start``) instead of leaving it at the image build date.
That keeps logs and files in order; it is NOT accurate time (the finder was
off for an unknown while), and ``current`` says so.
"""
from __future__ import annotations

import datetime as _dt
import json
import os

STATE_PATH = "/var/lib/diofinder/clock_sync.json"
LAST_KNOWN_PATH = "/var/lib/diofinder/clock_last_known.json"
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
# systemd-timesyncd creates this file once it has synchronized (systemd >= 239).
NTP_FLAG_PATH = "/run/systemd/timesync/synchronized"


def boot_id(path=BOOT_ID_PATH):
    try:
        with open(path) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _write_json(path, obj):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def record_sync(source, action, drift_s, utc_set=None, path=STATE_PATH, now=None,
                boot=None):
    """Remember a clock check/set. ``action`` is "set" (the clock was changed),
    "checked" (it was already within tolerance; drift_s says by how much) or
    "restored" (moved forward to the last known time at start).
    Best-effort: never raises."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    rec = {
        "source": str(source),
        "action": str(action),
        "drift_s": None if drift_s is None else round(float(drift_s), 1),
        "utc_set": utc_set,
        "at_utc": now.isoformat(timespec="seconds"),
        "boot_id": boot if boot is not None else boot_id(),
    }
    _write_json(path, rec)
    return rec


def last_sync(path=STATE_PATH):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def ntp_synchronized(flag_path=NTP_FLAG_PATH):
    return os.path.exists(flag_path)


_SOURCE_LABEL = {"skysafari": "SkySafari", "phone": "a phone (web page)"}
_SOURCE_NOTE = {"skysafari": "SkySafari sets whole seconds, to within about 2 s",
                "phone": "good to a fraction of a second"}


def this_boot(rec, boot=None):
    """True if a sync record was made since the finder last started up.
    Records without a boot id (older software) count as earlier boots."""
    b = boot if boot is not None else boot_id()
    return bool(rec) and b is not None and rec.get("boot_id") == b


def current(path=STATE_PATH, flag_path=NTP_FLAG_PATH, boot=None):
    """{ntp_synchronized, last_sync, this_boot, trusted, summary}.

    ``last_sync`` is the most recent manual check/set/restore record;
    ``this_boot`` says whether it was made since the finder started (only
    then does it describe the clock now). ``trusted``: network time is
    synchronized, or SkySafari / a phone set or checked the clock this boot."""
    ntp = ntp_synchronized(flag_path)
    last = last_sync(path)
    now_boot = this_boot(last, boot)
    src = (last or {}).get("source", "")
    action = (last or {}).get("action")
    trusted = ntp or (now_boot and action in ("set", "checked")
                      and src in _SOURCE_LABEL)
    if ntp:
        summary = "network time (synchronized)"
    elif now_boot and action == "restored":
        summary = (f"NOT SET since start-up: moved forward to the last known time "
                   f"({last.get('utc_set')}) only. The finder was off for an "
                   "unknown while, so it is behind; connect SkySafari or use "
                   "Utilities -> Clock")
    elif now_boot and last:
        drift = last.get("drift_s")
        summary = (f"{_SOURCE_LABEL.get(src, src)}, "
                   + ("set" if action == "set" else "checked")
                   + f" at {last.get('at_utc')}"
                   + (f" (was {abs(drift):.1f} s off)" if drift is not None else "")
                   + (f"; {_SOURCE_NOTE[src]}" if src in _SOURCE_NOTE else ""))
    elif last:
        summary = ("NOT SET since start-up (no network time; last set by "
                   f"{_SOURCE_LABEL.get(src, src)} at {last.get('at_utc')}, "
                   "before the finder was last switched off)")
    else:
        summary = "unknown (no network time, and not set from SkySafari or a phone)"
    return {"ntp_synchronized": ntp, "last_sync": last, "this_boot": now_boot,
            "trusted": bool(trusted), "summary": summary}


# --------------------------------------------------------------------------- #
# Setting the clock, and the last-known time across power cycles
# --------------------------------------------------------------------------- #

SET_TIME_HELPER = "/usr/local/bin/diofinder-set-time"


def set_clock(utc_epoch_s, run=None):
    """Set the system clock to ``utc_epoch_s``. Directly when this process
    may (diofinder.service grants CAP_SYS_TIME: its NoNewPrivileges=true
    blocks sudo), else through the sudo helper (whole seconds). Returns
    "direct" or "helper"; raises RuntimeError on failure."""
    import time as _time
    try:
        _time.clock_settime(_time.CLOCK_REALTIME, float(utc_epoch_s))
        return "direct"
    except (PermissionError, AttributeError, OSError):
        pass
    import subprocess
    run = run or subprocess.run
    arg = _dt.datetime.fromtimestamp(round(float(utc_epoch_s)), _dt.timezone.utc) \
        .strftime("%Y-%m-%d %H:%M:%S")
    r = run(["sudo", SET_TIME_HELPER, arg], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or f"exit {r.returncode}").strip())
    return "helper"


def save_last_known(path=None, now_s=None):
    """Remember the current (trusted) time. Callers check ``trusted`` first."""
    import time as _time
    path = path or LAST_KNOWN_PATH
    t = float(now_s if now_s is not None else _time.time())
    return _write_json(path, {"utc_epoch": t, "utc": _dt.datetime.fromtimestamp(
        t, _dt.timezone.utc).isoformat(timespec="seconds")})


def restore_at_start(path=None, now_s=None, setter=None,
                     state_path=None, flag_path=None, margin_s=5.0):
    """At start-up: if the clock is behind the last known time (it restarted
    from the image build date), move it forward to that time. Never moves
    the clock back, and does nothing once network time is synchronized.
    Returns the record written, or None."""
    import time as _time
    path, state_path = path or LAST_KNOWN_PATH, state_path or STATE_PATH
    if ntp_synchronized(flag_path or NTP_FLAG_PATH):
        return None
    try:
        with open(path) as f:
            saved = float(json.load(f)["utc_epoch"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    now = float(now_s if now_s is not None else _time.time())
    if saved <= now + margin_s:
        return None
    (setter or set_clock)(saved)
    when = _dt.datetime.fromtimestamp(saved, _dt.timezone.utc).isoformat(timespec="seconds")
    return record_sync("restored", "restored", saved - now, utc_set=when, path=state_path)
