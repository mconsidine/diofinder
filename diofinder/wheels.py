"""Installed wheel versions for the solver stack.

The olive-solve (``tetra3``) and sycamore (``star_detect``) wheels are
refreshed independently of the application code (image build / OTA update /
manual pip), so which versions a device actually runs is a diagnostic
essential: a v0.11.15 no-solve report turned out to be current code running a
pre-2026-06-20 olive-solve wheel (no blind-hint fallback), and nothing in the
logs or debug bundle recorded it. These helpers put the versions in the solver
startup log, the ``version`` maint command, the web UI, and debug bundles.
"""
from __future__ import annotations

from importlib import metadata

# label -> installed distribution name
WHEEL_DISTS = {
    "olive_solve": "tetra3",       # solver (mconsidine/olive-solve releases)
    "sycamore": "star_detect",     # extractor (mconsidine/sycamore-extract)
}


def wheel_versions() -> dict:
    """{"olive_solve": "0.1.5", "sycamore": "0.13.0"}; None when not installed."""
    out = {}
    for label, dist in WHEEL_DISTS.items():
        try:
            out[label] = metadata.version(dist)
        except Exception:
            out[label] = None
    return out


def wheel_versions_str() -> str:
    """Compact single-line form for log banners: "olive-solve 0.1.5, sycamore 0.13.0"."""
    v = wheel_versions()
    return "olive-solve %s, sycamore %s" % (
        v.get("olive_solve") or "?", v.get("sycamore") or "?")
