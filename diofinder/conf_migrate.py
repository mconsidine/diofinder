"""One-shot config migrations + shipped-default divergence report.

`/etc/diofinder/diofinder.conf` persists across OTA updates, so when a
default changes for a CORRECTNESS reason (not taste), devices that update
in place keep the old value forever — e.g. the scientific tuning profile
(DPC deletes 1-2 px faint stars), the pre-v0.11.15 background mode, or the
pre-v0.11.17 FOV center that sat at the edge of the calibrated window.

Two tools, both side-effect-free to compute:

* ``migrate(path)`` — a **versioned, one-shot** migration: each rule rewrites
  a key **only when it still equals the old default** (i.e. the user never
  touched it — a deliberate user choice is never overridden). Applied rules
  are persisted via ``config.save_keys`` together with a ``conf_version``
  stamp, so migrations run once; anything the user changes afterwards —
  including changing a key back to the old value — sticks. Called by the
  launcher at startup (covers OTA and manual git updates alike).

* ``diff_from_default(conf_path, default_path)`` — the divergence report:
  every key whose value differs from the shipped ``diofinder.conf.default``,
  shown on the web UI's Update page and printed by ``diofinder-update`` so
  stale settings are visible instead of silent.

Migration rules are data (``MIGRATIONS``), unit-tested in
``tests/test_conf_migrate.py``.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from diofinder import config as cfg_mod

log = logging.getLogger("diofinder.conf_migrate")

# Bump when adding rules. Stored in the conf as ``conf_version``.
CONF_VERSION = 1

# (key, old_default_values, new_value, reason)
# A rule fires only when the conf's current value is IN old_default_values
# (string comparison after strip). ``None`` in old_default_values means
# "key absent from the file".
MIGRATIONS = [
    ("camera_tuning_file",
     ("/usr/share/libcamera/ipa/rpi/vc4/imx477_scientific.json",),
     "/usr/share/libcamera/ipa/rpi/vc4/imx477_finder.json",
     "finder tuning disables DPC (defect correction deletes 1-2 px faint "
     "stars) and compands faint stars through the 12->8-bit reduction"),
    ("detect_bg_mode",
     ("row_percentile",),
     "block_percentile",
     "2-D gradient-aware background at similar cost; cache-compatible "
     "(default since v0.11.15)"),
    ("fov_max_error_deg",
     ("1.0", "1.000000"),
     "0.3",
     "the lens FOV is known; a narrower blind-search window means fewer "
     "candidates per pattern and faster lost-in-space solves"),
    ("fov_deg",
     ("13.64", "13.640000"),
     "13.54",
     "measured on-sky FOV of the binned sensor mode; 13.64 sat at the edge "
     "of the calibrated +/-0.1 window (v0.11.17 recenter)"),
    ("arcsec_per_pixel",
     ("51.15", "51.150000"),
     "50.78",
     "plate scale matching the 13.54 deg recenter"),
    ("sensor_full_width",
     ("4056",),
     "2028",
     "2x2-binned full-FOV readout: same FOV/plate scale, ~4x less "
     "sensor/ISP bandwidth, better SNR (default since v0.11.15)"),
    ("sensor_full_height",
     ("3040",),
     "1520",
     "pairs with sensor_full_width"),
    ("star_db_deep",
     (None,),
     "/var/lib/diofinder/diofinder_13deg_mag85.npz",
     "lets the Bad-seeing preset use the deeper catalog when installed "
     "(missing key silently kept the preset on the standard db)"),
]


def _parse_conf(path: str) -> dict:
    """key -> raw string value (comments stripped). Last occurrence wins,
    matching load_config."""
    out = {}
    try:
        with open(path) as f:
            for line in f:
                stripped = line.split("#", 1)[0].strip()
                if ":" not in stripped:
                    continue
                k, v = stripped.split(":", 1)
                out[k.strip().lower()] = v.strip()
    except OSError:
        pass
    return out


def pending_migrations(path: Optional[str] = None) -> list:
    """Rules that would fire on this conf: [(key, current, new, reason)]."""
    p = path or os.environ.get("DIOFINDER_CONFIG", cfg_mod.DEFAULT_CONFIG_PATH)
    if not os.path.exists(p):
        return []
    conf = _parse_conf(p)
    if int(conf.get("conf_version", "0") or 0) >= CONF_VERSION:
        return []
    fires = []
    for key, olds, new, reason in MIGRATIONS:
        cur = conf.get(key)
        # Numeric-aware matching (audit 2026-07 F-L3): save_keys writes
        # floats as %.10g, so a user-persisted old default "1.0" lands in
        # the conf as "1" — which no exact-string rule matched, silently
        # skipping the migration for exactly the devices that need it.
        hit = cur in olds or (cur is None and None in olds)
        if not hit and cur is not None:
            cur_n = _norm(cur)
            hit = any(o is not None and _norm(o) == cur_n for o in olds)
        if hit:
            fires.append((key, cur, new, reason))
    return fires


def migrate(path: Optional[str] = None) -> dict:
    """Apply pending migrations once; returns {key: new_value} applied
    (empty when already at CONF_VERSION or nothing matched). Always stamps
    ``conf_version`` so the check is O(1) on every subsequent boot."""
    p = path or os.environ.get("DIOFINDER_CONFIG", cfg_mod.DEFAULT_CONFIG_PATH)
    if not os.path.exists(p):
        return {}
    conf = _parse_conf(p)
    if int(conf.get("conf_version", "0") or 0) >= CONF_VERSION:
        return {}
    fires = pending_migrations(p)
    updates = {key: new for key, _cur, new, _r in fires}
    for key, cur, new, reason in fires:
        log.warning("conf migration: %s: %s -> %s (%s)",
                    key, cur if cur is not None else "<absent>", new, reason)
    updates["conf_version"] = CONF_VERSION
    try:
        cfg_mod.save_keys(updates, path=p)
    except Exception as e:
        log.warning("conf migration could not persist: %s", e)
        return {}
    updates.pop("conf_version")
    return updates


def diff_from_default(conf_path: Optional[str] = None,
                      default_path: Optional[str] = None) -> list:
    """[(key, current, shipped_default)] for every key that differs from the
    shipped conf.default (both directions: changed values and keys the
    default file has that the conf lacks). Keys only in the live conf
    (calibration artifacts like conf_version/boresight drift) are reported
    with a '<not in default>' marker only when the default file lacks them
    entirely — they are normal and listed last."""
    p = conf_path or os.environ.get("DIOFINDER_CONFIG",
                                    cfg_mod.DEFAULT_CONFIG_PATH)
    d = default_path
    if d is None:
        # Installed checkout location first, then the repo-relative path.
        for cand in ("/opt/diofinder/etc/diofinder.conf.default",
                     os.path.join(os.path.dirname(__file__), "..",
                                  "etc", "diofinder.conf.default")):
            if os.path.exists(cand):
                d = cand
                break
    if d is None or not os.path.exists(p) or not os.path.exists(d):
        return []
    conf, dflt = _parse_conf(p), _parse_conf(d)
    diffs = []
    for key, dval in dflt.items():
        cval = conf.get(key)
        if cval is None:
            diffs.append((key, "<absent>", dval))
        elif _norm(cval) != _norm(dval):
            diffs.append((key, cval, dval))
    extras = [(k, v, "<not in default>")
              for k, v in conf.items() if k not in dflt]
    return diffs + sorted(extras)


def _norm(v: str) -> str:
    """Value-compare: numbers numerically, everything else as-is."""
    try:
        return repr(float(v))
    except (TypeError, ValueError):
        return v.strip().lower()
