"""bg_cache.resolve_effective: the one-sentence 'what is background
subtraction actually doing right now' resolver (v0.11.47).

Pure-function tests over stats() snapshots — no wheel, no camera. Pins the
full decision matrix so the UI line derives from the same rules detect()
applies and can't silently diverge.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from diofinder.bg_cache import resolve_effective


def _stats(**over):
    base = dict(enabled=True, state="STEADY", has_model=True,
                model_kind="block", model_age_s=3.2,
                served_cached=96, served_fallback=4,
                block_cache_supported=True, bg_image_supported=True,
                tophat_supported=True, active_bg_mode="block_percentile")
    base.update(over)
    return base


def test_steady_block_cache():
    r = resolve_effective(_stats(), "block_percentile", "mad")
    assert r["path"] == "cached-block" and r["reason"] is None
    assert r["effective_mode"] == "block_percentile"
    assert "block-grid temporal cache" in r["summary"]
    assert "96% cached" in r["summary"]


def test_steady_image_cache():
    r = resolve_effective(_stats(model_kind="image"), "temporal_median", "mad")
    assert r["path"] == "cached-image"
    assert r["effective_mode"] == "temporal_median"


def test_steady_row_cache():
    r = resolve_effective(_stats(model_kind="row"), "row_percentile", "mad")
    assert r["path"] == "cached-row"


def test_never_cached_mode_is_per_frame_even_when_steady():
    r = resolve_effective(_stats(), "uniform_mean", "mad")
    assert r["path"] == "per-frame"
    assert "never cached" in r["reason"]


def test_noise_mode_forces_per_frame():
    r = resolve_effective(_stats(), "block_percentile", "global_rms")
    assert r["path"] == "per-frame"
    assert "global_rms" in r["reason"]


def test_disabled_cache():
    r = resolve_effective(_stats(enabled=False), "block_percentile", "mad")
    assert r["path"] == "per-frame"
    assert "disabled" in r["reason"]


def test_warming_up_and_slewing():
    r = resolve_effective(_stats(state="WARMING_UP"), "block_percentile", "mad")
    assert r["path"] == "per-frame" and "collecting" in r["reason"]
    r = resolve_effective(_stats(state="SLEWING"), "block_percentile", "mad")
    assert r["path"] == "per-frame" and "rebuilding" in r["reason"]


def test_model_kind_mismatch_during_mode_switch():
    r = resolve_effective(_stats(model_kind="row"), "block_percentile", "mad")
    assert r["path"] == "per-frame"
    assert "rebuilding for the new mode" in r["reason"]


def test_temporal_median_degrades_to_block_percentile():
    # Off the image cache (here: old wheel), temporal_median has no per-frame
    # form — it runs as per-frame block_percentile.
    r = resolve_effective(_stats(bg_image_supported=False),
                          "temporal_median", "mad")
    assert r["path"] == "per-frame"
    assert r["effective_mode"] == "block_percentile"
    assert "wheel lacks" in r["reason"]
    # Same degradation when the cache exists but is warming up.
    r = resolve_effective(_stats(state="WARMING_UP", model_kind="image"),
                          "temporal_median", "mad")
    assert r["effective_mode"] == "block_percentile"


def test_tophat_wheel_degradation():
    r = resolve_effective(_stats(tophat_supported=False, model_kind="row"),
                          "top_hat", "mad")
    # Degrades to line_median, which is still row-cache compatible.
    assert r["effective_mode"] == "line_median"
    assert r["path"] == "cached-row"


def test_block_cache_unsupported_wheel():
    r = resolve_effective(_stats(block_cache_supported=False),
                          "block_percentile", "mad")
    assert r["path"] == "per-frame"
    assert "wheel lacks" in r["reason"]


def test_minimal_stats_from_none_bg_cache():
    # The solver returns {"enabled": False, "state": "NONE"} when bg_cache is
    # absent; the resolver must not raise.
    r = resolve_effective({"enabled": False, "state": "NONE"},
                          "block_percentile", "mad")
    assert r["path"] == "per-frame"


def test_no_serve_counters_yet():
    r = resolve_effective(_stats(served_cached=0, served_fallback=0),
                          "block_percentile", "mad")
    assert "no detections served yet" in r["summary"]  # no div-by-zero
