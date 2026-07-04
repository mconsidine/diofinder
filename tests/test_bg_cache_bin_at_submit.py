"""P5 opt-in bin-at-submit stack path. Two distinct claims:

1. FAITHFULNESS (exact): the uint16-sum storage reproduces the float
   median(bin_mean(frames)) estimator exactly — median(sums)/bin**2 ==
   median(bin_mean) since median commutes with a positive scale. This is the
   estimator the offline quantification measured.

2. AGREEMENT (approximate, NOT exact): that estimator only *approximates* the
   default bin_mean(median(frames)) — spatial-mean and temporal-median don't
   commute — so the noise scalar differs by a data-dependent few percent
   (~1-2% on real sky, more on steep synthetic structure). The test pins a
   loose bound, matching why P5 ships opt-in + A/B, not as the default.

Requires the sycamore wheel (the model builder calls star_detect); skipped
otherwise.
"""
import importlib
import sys

import numpy as np
import pytest

# A prior test (test_auto_exposure / test_auto_tune) may have installed a thin
# star_detect stub in sys.modules, which bakes HAS_CACHE=False into bg_cache
# and makes compute_block_medians_py a no-op. Evict it, bind the REAL wheel,
# and reload bg_cache so its capability probes re-run — these equivalence
# tests genuinely need the compiled extension. Skip if it isn't installed.
sys.modules.pop("star_detect", None)
star_detect = pytest.importorskip("star_detect")
if star_detect.compute_block_medians_py(np.zeros((8, 8), np.uint8), 4) is None:
    pytest.skip("star_detect stub lacks a real block-median builder",
                allow_module_level=True)
import diofinder.bg_cache as _bgc   # noqa: E402
importlib.reload(_bgc)
BackgroundCache = _bgc.BackgroundCache


class _Cfg:
    bg_cache_enabled = True
    detect_bin = 2
    bg_cache_stack = 8
    bg_cache_refresh_s = 5.0
    bg_cache_slew_deg = 0.5
    bg_cache_max_age_s = 60.0
    detect_bg_mode = "block_percentile"
    detect_bg_block_size = 32
    bg_cache_fail_invalidate = 3
    bg_cache_bin_at_submit = False
    frame_height = 120
    frame_width = 160


def _synthetic_stack(n=8, seed=0):
    rng = np.random.default_rng(seed)
    h, w = _Cfg.frame_height, _Cfg.frame_width
    # A smooth light-pollution gradient + a pedestal + a couple of stars +
    # per-frame shot noise (elevates MAD above the 0.5 floor).
    yy = np.linspace(0, 40, h)[:, None]
    xx = np.linspace(0, 25, w)[None, :]
    base = 30 + yy + xx
    for (cy, cx, amp) in [(40, 60, 120), (80, 120, 90)]:
        Y, X = np.ogrid[:h, :w]
        base = base + amp * np.exp(-((Y - cy) ** 2 + (X - cx) ** 2) / 8.0)
    frames = []
    for _ in range(n):
        f = base + rng.normal(0, 3.0, base.shape)
        frames.append(np.clip(f, 0, 255).astype(np.uint8))
    return frames


def _binmean_f(f, b=2):
    h, w = f.shape
    return (f[: (h // b) * b, : (w // b) * b].astype(np.float32)
            .reshape(h // b, b, w // b, b).mean(axis=(1, 3)))


def test_storage_faithfully_reproduces_the_proposed_estimator():
    # Claim 1 (exact): the uint16-sum P5 path == an independent float
    # median(bin_mean(frames)) reference, to float precision.
    cache = BackgroundCache(_Cfg())
    full = _synthetic_stack()
    binned = [cache._bin_sum_u16(f) for f in full]
    assert binned[0].dtype == np.uint16
    assert binned[0].shape == (_Cfg.frame_height // 2, _Cfg.frame_width // 2)

    ref = np.median(np.stack([_binmean_f(f) for f in full], 0), 0)
    p5 = np.median(np.stack(binned, 0), 0).astype(np.float32) / 4.0
    np.testing.assert_allclose(p5, ref, atol=1e-4)

    m_bin = cache._build_model(binned)
    assert m_bin.h == _Cfg.frame_height and m_bin.w == _Cfg.frame_width  # full-res


def test_bin_at_submit_agrees_with_default_within_bound():
    # Claim 2 (approximate): P5 vs the default estimator — the noise differs by
    # a bounded few percent (NOT exactly; mean/median don't commute), and the
    # u8-derived block offsets are identical. This is why it's opt-in + A/B.
    cache = BackgroundCache(_Cfg())
    full = _synthetic_stack()
    binned = [cache._bin_sum_u16(f) for f in full]
    m_full = cache._build_model(full)       # default (dtype uint8)
    m_bin = cache._build_model(binned)      # P5 (dtype uint16)

    assert m_full.noise > 0.5 and m_bin.noise > 0.5   # unfloored (real test)
    rel = abs(m_full.noise - m_bin.noise) / m_full.noise
    assert rel < 0.10, f"noise divergence {rel:.3f} exceeds the 10% A/B bound"
    # The u8 background offsets absorb the sub-DN float difference and match.
    np.testing.assert_array_equal(m_full.block_offsets, m_bin.block_offsets)


def test_bin_sum_is_exact():
    cache = BackgroundCache(_Cfg())
    f = np.arange(_Cfg.frame_height * _Cfg.frame_width, dtype=np.uint8).reshape(
        _Cfg.frame_height, _Cfg.frame_width)
    s = cache._bin_sum_u16(f)
    # First 2x2 block sum matches a hand computation.
    assert int(s[0, 0]) == int(f[0, 0]) + int(f[0, 1]) + int(f[1, 0]) + int(f[1, 1])


def test_note_bin_at_submit_flushes_and_is_idempotent():
    cache = BackgroundCache(_Cfg())
    # Seed the buffer, then flip -> buffer flushed + rebuild armed.
    with cache._frame_buf_lock:
        cache._frame_buf.append(np.zeros((120, 160), np.uint8))
    cache.note_bin_at_submit(True)
    assert cache._bin_at_submit is True
    assert cache._frame_count() == 0
    assert cache._needs_rebuild.is_set()
    # Same value again is a no-op (no exception, still set).
    cache.note_bin_at_submit(True)
    assert cache._bin_at_submit is True
