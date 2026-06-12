"""
Seeing presets: one-tap "Good" / "Bad" night tuning.

A single toggle that re-tunes the whole detection + solve pipeline for the
observing conditions. "Good" assumes steady, dark skies (tight kernel, low
sigma, fast timeouts, cheap per-row background). "Bad" assumes poor seeing /
light pollution / bloated PSFs (wider matched-filter kernel, looser trail
rejection, 2-D block background, longer exposure and solve budget, and an
optional deeper-magnitude database so faint-but-real stars still solve).

Each preset is a flat dict of config keys. ``apply_preset`` returns the
subset of keys/values to push; the caller (comms_proc) is responsible for
routing each key through the correct channel (shared_cfg write, camera RPC,
solver DB switch) and persisting via ``config.save_keys``.

This module is intentionally dependency-free (no numpy, no star_detect, no
picamera2) so it imports and unit-tests anywhere.
"""
from __future__ import annotations

from typing import Any, Dict

# Canonical preset tables. Every value here is also an individually
# adjustable config key, so applying a preset is exactly equivalent to
# setting each of these by hand.
SEEING_PRESETS: Dict[str, Dict[str, Any]] = {
    "good": dict(
        detect_sigma=5.0,
        detect_kernel_sigma=1.5,
        detect_bg_mode="row_percentile",
        detect_max_axis_ratio=3.0,
        min_centroids=8,
        match_radius=0.01,
        match_threshold=1e-5,
        solve_timeout_ms=1500,
        auto_exposure_target_stars=20,
        auto_exposure_max_s=0.5,
        star_db="standard",
    ),
    "bad": dict(
        detect_sigma=4.0,
        detect_kernel_sigma=2.5,
        detect_bg_mode="block_percentile",
        detect_max_axis_ratio=5.0,
        min_centroids=5,
        match_radius=0.015,
        match_threshold=1e-5,
        solve_timeout_ms=3000,
        auto_exposure_target_stars=15,
        auto_exposure_max_s=1.0,
        star_db="deep",
    ),
}

# Human-readable rationale for each key, surfaced in the UI / docs.
PRESET_RATIONALE: Dict[str, str] = {
    "detect_sigma": "Detection threshold in noise sigmas.",
    "detect_kernel_sigma": "Matched-filter kernel width; wider for bloated PSFs.",
    "detect_bg_mode": "Per-frame background model.",
    "detect_max_axis_ratio": "Trail/elongation rejection; looser when seeing smears stars.",
    "min_centroids": "Minimum stars before attempting a solve.",
    "match_radius": "Catalog-match tolerance as a fraction of FOV.",
    "match_threshold": "Max false-positive probability accepted.",
    "solve_timeout_ms": "Per-frame solve budget.",
    "auto_exposure_target_stars": "Auto-exposure target star count.",
    "auto_exposure_max_s": "Auto-exposure ceiling.",
    "star_db": "standard vs. deeper-magnitude database.",
}

VALID_MODES = ("good", "bad")


def is_valid_mode(mode: str) -> bool:
    return mode in VALID_MODES


def resolve_star_db(token: str, cfg) -> str:
    """Map a preset star_db token ("standard"/"deep") to an actual db name.

    "standard" -> cfg.solver_db's standard value (cfg.star_db_standard if set,
                  else the current cfg.solver_db is treated as the standard).
    "deep"     -> cfg.star_db_deep, but ONLY if it is a non-empty path that
                  exists on disk; otherwise falls back to the standard db so a
                  missing deep catalog never breaks solving.

    A token that is not "standard"/"deep" is returned unchanged (allows an
    explicit db name to pass through).
    """
    import os

    standard = getattr(cfg, "star_db_standard", "") or cfg.solver_db
    if token == "standard":
        return standard
    if token == "deep":
        deep = (getattr(cfg, "star_db_deep", "") or "").strip()
        if deep and _db_exists(deep):
            return deep
        return standard
    return token


def _db_exists(name: str) -> bool:
    """True if the db name resolves to a file on disk.

    Mirrors solver_proc._solver_db_path: bare names live under
    /var/lib/efinder/<name>.npz, absolute paths are used verbatim.
    """
    import os

    if os.path.isabs(name):
        path = name
    else:
        path = f"/var/lib/efinder/{name}.npz"
    return os.path.exists(path)


def apply_preset(mode: str, cfg) -> Dict[str, Any]:
    """Return the flat dict of config keys to apply for ``mode``.

    star_db is resolved to a concrete database name here (handling the deep-db
    existence check). Every other key is copied verbatim from the table. The
    caller decides how to route/persist each key; this function performs no
    side effects.

    Raises ValueError for an unknown mode.
    """
    if not is_valid_mode(mode):
        raise ValueError(f"unknown seeing mode {mode!r}; expected one of {VALID_MODES}")
    preset = dict(SEEING_PRESETS[mode])
    preset["star_db"] = resolve_star_db(preset.get("star_db", "standard"), cfg)
    return preset


def effective_values(cfg, shared_cfg=None) -> Dict[str, Any]:
    """Return the current effective value of every preset-controlled key.

    Reads shared_cfg (live overrides) first, falling back to cfg. Used by
    ``seeing_get`` so the UI can show which keys have drifted away from the
    applied preset after individual edits.
    """
    keys = set()
    for preset in SEEING_PRESETS.values():
        keys.update(preset.keys())
    out: Dict[str, Any] = {}
    for key in sorted(keys):
        if key == "star_db":
            # star_db lives as a config-only key; report the raw cfg value.
            out[key] = getattr(cfg, "solver_db", "")
            continue
        if shared_cfg is not None and key in shared_cfg:
            out[key] = shared_cfg[key]
        else:
            out[key] = getattr(cfg, key, None)
    return out


def drift_from_preset(mode: str, cfg, shared_cfg=None) -> Dict[str, Any]:
    """Return {key: {"preset": p, "effective": e}} for keys whose effective
    value differs from the named preset. Empty dict means no drift.

    star_db is compared against the resolved (concrete) preset db name.
    """
    if not is_valid_mode(mode):
        return {}
    preset = apply_preset(mode, cfg)
    eff = effective_values(cfg, shared_cfg)
    drift: Dict[str, Any] = {}
    for key, pval in preset.items():
        cur = eff.get(key)
        if not _approx_equal(cur, pval):
            drift[key] = {"preset": pval, "effective": cur}
    return drift


def _approx_equal(a: Any, b: Any) -> bool:
    """Tolerant equality: floats compared with a small relative epsilon."""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        if a == b:
            return True
        scale = max(abs(a), abs(b), 1e-12)
        return abs(a - b) <= 1e-6 * scale
    return a == b
