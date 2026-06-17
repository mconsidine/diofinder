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

import json
import os
import tempfile
import time
from typing import Any, Dict, Optional

# Canonical preset tables. Every value here is also an individually
# adjustable config key, so applying a preset is exactly equivalent to
# setting each of these by hand.
SEEING_PRESETS: Dict[str, Dict[str, Any]] = {
    "good": dict(
        extractor_backend="sycamore",
        detect_sigma=5.0,
        detect_kernel_sigma=1.5,
        detect_bg_mode="row_percentile",
        detect_noise_mode="mad",
        detect_uniform_filter_size=0,
        detect_max_axis_ratio=3.0,
        min_centroids=8,
        match_radius=0.01,
        match_threshold=1e-5,
        solve_timeout_ms=1500,
        auto_exposure_target_stars=20,
        auto_exposure_target_matches=10,
        auto_exposure_max_s=0.5,
        auto_exposure_max_gain=16.0,
        star_db="standard",
    ),
    "bad": dict(
        extractor_backend="sycamore",
        detect_sigma=4.0,
        detect_kernel_sigma=2.5,
        detect_bg_mode="block_percentile",
        detect_noise_mode="mad",
        detect_uniform_filter_size=0,
        detect_max_axis_ratio=5.0,
        min_centroids=5,
        match_radius=0.015,
        match_threshold=1e-5,
        solve_timeout_ms=3000,
        auto_exposure_target_stars=15,
        auto_exposure_target_matches=6,
        auto_exposure_max_s=1.0,
        auto_exposure_max_gain=16.0,
        star_db="deep",
    ),
    # "Keith" — an exact re-creation of the AstroKeith eFinder_cli "original"
    # pipeline (tetra3 get_centroids_from_image: local_mean background +
    # global-RMS noise + sigma=2, no matched filter, no temporal cache),
    # routed through the olive-solve tetra3 extractor backend rather than
    # sycamore. This is the BASELINE to improve upon, not the recommended
    # default. detect_kernel_sigma / detect_bg_mode are ignored by the tetra3
    # backend but kept here so a toggle back to good/bad fully re-tunes.
    "keith": dict(
        extractor_backend="tetra3",
        detect_sigma=2.0,
        detect_kernel_sigma=1.5,
        detect_bg_mode="uniform_mean",
        detect_noise_mode="global_rms",
        detect_uniform_filter_size=25,
        detect_max_axis_ratio=0.0,
        min_centroids=15,
        match_radius=0.01,
        match_threshold=1e-5,
        solve_timeout_ms=5000,
        auto_exposure_target_stars=20,
        auto_exposure_target_matches=10,
        auto_exposure_max_s=1.0,
        auto_exposure_max_gain=20.0,
        star_db="standard",
    ),
}

# Human-readable rationale for each key, surfaced in the UI / docs.
PRESET_RATIONALE: Dict[str, str] = {
    "extractor_backend": "Centroid extractor: sycamore (matched filter) or tetra3 (AstroKeith).",
    "detect_sigma": "Detection threshold in noise sigmas.",
    "detect_kernel_sigma": "Matched-filter kernel width; wider for bloated PSFs.",
    "detect_bg_mode": "Per-frame background model.",
    "detect_noise_mode": "Noise estimator: mad (robust) or global_rms (tetra3).",
    "detect_uniform_filter_size": "uniform_mean / tetra3 background window (px).",
    "detect_max_axis_ratio": "Trail/elongation rejection; looser when seeing smears stars.",
    "min_centroids": "Minimum stars before attempting a solve.",
    "match_radius": "Catalog-match tolerance as a fraction of FOV.",
    "match_threshold": "Max false-positive probability accepted.",
    "solve_timeout_ms": "Per-frame solve budget.",
    "auto_exposure_target_stars": "Auto-exposure target star count (lost-in-space fallback).",
    "auto_exposure_target_matches": "Auto-exposure target matched-star count while solving.",
    "auto_exposure_max_s": "Auto-exposure ceiling.",
    "auto_exposure_max_gain": "Auto-exposure gain-ladder ceiling.",
    "star_db": "standard vs. deeper-magnitude database.",
}

VALID_MODES = ("good", "bad", "keith")


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


# ===== Saved overrides ========================================================
#
# An override is an OPTIONAL, persisted, *sparse* layer that sits between the
# immutable factory preset and the user's live hand-edits:
#
#     factory preset  (SEEING_PRESETS, immutable)
#        + saved override   (only the keys it changes; this file)
#        + live hand-edits  (shared_cfg drift)
#        = active config
#
# Overrides are created by `auto_tune commit` (source "auto_tune") or by the
# user ("Update override from current", source "manual"). They are NOT applied
# automatically on a mode toggle — the caller passes use_override explicitly.
# An override may carry absolute exposure_s / gain, which the factory presets
# cannot express.

OVERRIDES_PATH = "/var/lib/efinder/seeing_overrides.json"


def _all_preset_keys() -> set:
    keys: set = set()
    for preset in SEEING_PRESETS.values():
        keys.update(preset.keys())
    return keys


# Keys an override may carry: every preset key plus absolute camera exposure /
# gain (which the factory presets do not contain).
OVERRIDE_KEYS = frozenset(_all_preset_keys() | {"exposure_s", "gain"})


def load_overrides(path: str = OVERRIDES_PATH) -> Dict[str, Any]:
    """Return the full {mode: entry} override map; {} if absent / unreadable."""
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {}


def get_override(mode: str, path: str = OVERRIDES_PATH) -> Optional[Dict[str, Any]]:
    """Return the override entry for ``mode`` ({"values", "saved_at", "source"})
    or None."""
    entry = load_overrides(path).get(mode)
    return entry if isinstance(entry, dict) and entry.get("values") else None


def _atomic_write_json(path: str, data: Any) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".seeing_ovr_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def save_override(mode: str, values: Dict[str, Any], *, source: str = "manual",
                  path: str = OVERRIDES_PATH,
                  now: Optional[float] = None) -> Dict[str, Any]:
    """Persist a sparse override for ``mode``. Only OVERRIDE_KEYS are kept.

    Raises ValueError for an unknown mode or an empty/ineligible value set.
    Returns the stored entry.
    """
    if not is_valid_mode(mode):
        raise ValueError(f"unknown seeing mode {mode!r}")
    clean = {k: v for k, v in values.items() if k in OVERRIDE_KEYS}
    if not clean:
        raise ValueError("no override-eligible keys in values")
    data = load_overrides(path)
    entry = {"values": clean,
             "saved_at": float(now if now is not None else time.time()),
             "source": str(source)}
    data[mode] = entry
    _atomic_write_json(path, data)
    return entry


def clear_override(mode: str, path: str = OVERRIDES_PATH) -> bool:
    """Delete the override for ``mode``. Returns True if one was removed."""
    data = load_overrides(path)
    if mode in data:
        del data[mode]
        _atomic_write_json(path, data)
        return True
    return False


def merged_preset(mode: str, cfg, *, use_override: bool,
                  path: str = OVERRIDES_PATH):
    """Return ``(applied_values, override_applied)``.

    The factory preset (star_db resolved), with the saved override overlaid when
    ``use_override`` is true and one exists. exposure_s / gain may appear in the
    result (override-only keys); the caller routes those to the camera.
    """
    base = apply_preset(mode, cfg)
    if not use_override:
        return base, False
    ov = get_override(mode, path)
    if not ov:
        return base, False
    merged = dict(base)
    vals = ov.get("values", {})
    for k, v in vals.items():
        merged[k] = v
    if "star_db" in vals:
        merged["star_db"] = resolve_star_db(merged["star_db"], cfg)
    return merged, True


def _override_meta(ov: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not ov:
        return None
    return {"saved_at": ov.get("saved_at"),
            "source": ov.get("source", "manual"),
            "values": dict(ov.get("values", {}))}


def overrides_summary(path: str = OVERRIDES_PATH) -> Dict[str, Any]:
    """Return ``{mode: override_meta_or_None}`` for every valid mode, for the UI."""
    return {m: _override_meta(get_override(m, path)) for m in VALID_MODES}


def classify_lineage(mode: str, cfg, effective: Dict[str, Any],
                     path: str = OVERRIDES_PATH) -> Dict[str, Any]:
    """Classify the active config's lineage for ``mode`` as factory / tuned /
    custom.

    ``effective`` is the caller-assembled map of current values (preset keys via
    ``effective_values`` plus, when relevant, exposure_s / gain from the camera).

      * tuned   — an override exists and every override key matches effective.
      * factory — every factory preset key matches effective (no override match).
      * custom  — anything else (hand-edits on top); ``drift`` lists the keys
                  differing from the factory preset.

    Returns ``{"source", "override", "drift"}``.
    """
    ov = get_override(mode, path) if is_valid_mode(mode) else None
    meta = _override_meta(ov)

    if ov:
        cmp = dict(ov.get("values", {}))
        if "star_db" in cmp:
            cmp["star_db"] = resolve_star_db(cmp["star_db"], cfg)
        if cmp and all(_approx_equal(effective.get(k), v) for k, v in cmp.items()):
            return {"source": "tuned", "override": meta, "drift": {}}

    drift = drift_from_preset(mode, cfg,
                              {k: effective.get(k) for k in effective})
    if not drift:
        return {"source": "factory", "override": meta, "drift": {}}
    return {"source": "custom", "override": meta, "drift": drift}
