"""Where the finder's clock got its time, for anything that depends on it.

The Pi Zero 2W has no real-time clock. The time comes from network time
(systemd-timesyncd, when the finder is on a network with internet) or from
SkySafari's LX200 time sync (``:SL``/``:SG``/``:SC`` on connect), which sets the
clock only when it is more than 10 s off, to whole seconds.

``record_sync`` stores the most recent manual check/set (SkySafari, or the
Utilities page's "set from this phone" button) in a small JSON file
(survives restarts of the service, not of a wiped card); ``current`` reports
it alongside whether network time is synchronized right now. Live captures
copy ``current()`` into ``session.json`` so satellite timing can be judged.
"""
from __future__ import annotations

import datetime as _dt
import json
import os

STATE_PATH = "/var/lib/diofinder/clock_sync.json"
# systemd-timesyncd creates this file once it has synchronized (systemd >= 239).
NTP_FLAG_PATH = "/run/systemd/timesync/synchronized"


def record_sync(source, action, drift_s, utc_set=None, path=STATE_PATH, now=None):
    """Remember a clock check/set. ``action`` is "set" (the clock was changed)
    or "checked" (it was already within tolerance; drift_s says by how much).
    Best-effort: never raises."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    rec = {
        "source": str(source),
        "action": str(action),
        "drift_s": None if drift_s is None else round(float(drift_s), 1),
        "utc_set": utc_set,
        "at_utc": now.isoformat(timespec="seconds"),
    }
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(rec, f)
        os.replace(tmp, path)
    except OSError:
        pass
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
_SOURCE_NOTE = {"skysafari": "SkySafari only corrects errors over 10 s, to whole seconds",
                "phone": "good to a fraction of a second"}


def current(path=STATE_PATH, flag_path=NTP_FLAG_PATH):
    """{ntp_synchronized, last_sync: {...} or None, summary}. ``last_sync`` is
    the most recent manual check/set (SkySafari or the web page's phone
    button)."""
    ntp = ntp_synchronized(flag_path)
    last = last_sync(path)
    if ntp:
        summary = "network time (synchronized)"
    elif last:
        src = last.get("source", "")
        drift = last.get("drift_s")
        summary = (f"{_SOURCE_LABEL.get(src, src)}, "
                   + ("set" if last.get("action") == "set" else "checked")
                   + f" at {last.get('at_utc')}"
                   + (f" (was {abs(drift):.1f} s off)" if drift is not None else "")
                   + (f"; {_SOURCE_NOTE[src]}" if src in _SOURCE_NOTE else ""))
    else:
        summary = "unknown (no network time, and not set from SkySafari or a phone)"
    return {"ntp_synchronized": ntp, "last_sync": last, "summary": summary}
