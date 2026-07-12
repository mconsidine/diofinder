"""bg_cache.preview_background (v0.11.49): the solver-side background-preview
op that replaced the webui's reimplemented _compute_background.

The unique capability under test: temporal_median renders the solver's live
cached median stack (which exists only in the solver process), and degrades to
per-frame block_percentile when no stack is built — the same documented
degradation detect() uses. Spatial modes reconstruct per-frame so the A/B tool
compares modes without touching the live pipeline.

Pure helpers (upsample / broadcast / bilinear / per-frame) are numpy-only and
tested for shape + known values.
"""
import pathlib
import sys
import time

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from diofinder import bg_cache as bc
from diofinder.config import Config


# ----- pure reconstruction helpers -----------------------------------------

def test_fit_1d_crop_and_pad():
    a = np.arange(5, dtype=np.float32)
    assert np.array_equal(bc._fit_1d(a, 3), [0, 1, 2])          # crop
    assert np.array_equal(bc._fit_1d(a, 7), [0, 1, 2, 3, 4, 4, 4])  # edge-pad


def test_fit_to_pads_short_binned_model():
    a = np.arange(6, dtype=np.float32).reshape(2, 3)
    out = bc._fit_to(a, 3, 4)
    assert out.shape == (3, 4)
    assert out[2, 3] == a[1, 2]  # bottom-right edge replicated


def test_upsample_binned_replicates_blocks():
    a = np.array([[0, 4], [8, 12]], dtype=np.uint8)
    up = bc._upsample_binned(a, 4, 4, 2)
    assert up.shape == (4, 4)
    # each source pixel fills a 2x2 block
    assert up[0, 0] == 0 and up[1, 1] == 0
    assert up[2, 2] == 12 and up[3, 3] == 12


def test_broadcast_rows_expands_per_row_floor():
    rows = np.array([10, 20, 30], dtype=np.uint8)
    br = bc._broadcast_rows(rows, 6, 4, 2)
    assert br.shape == (6, 4)
    assert (br[0] == 10).all() and (br[1] == 10).all()   # bin=2 -> two rows each
    assert (br[2] == 20).all() and (br[4] == 30).all()


def test_bilinear_to_matches_grid_corners():
    grid = np.array([[0, 100], [200, 255]], dtype=np.uint8)
    bl = bc._bilinear_to(grid, 4, 4)
    assert bl.shape == (4, 4)
    assert bl[0, 0] == 0 and bl[0, -1] == 100
    assert bl[-1, 0] == 200 and bl[-1, -1] == 255
    # interior is a monotone blend
    assert 0 < bl[1, 1] < 255


def test_bilinear_to_handles_degenerate_1x1_grid():
    bl = bc._bilinear_to(np.array([[42]], np.uint8), 5, 7)
    assert bl.shape == (5, 7) and (bl == 42).all()


def test_per_frame_background_line_median_is_per_row():
    f = np.zeros((4, 6), np.uint8)
    f[2, :] = 50
    bg = bc._per_frame_background(f, "line_median")
    assert bg.shape == (4, 6)
    assert (bg[0] == 0).all() and (bg[2] == 50).all()


def test_per_frame_background_unknown_mode_falls_back_to_line_median():
    f = (np.ones((4, 4)) * 7).astype(np.uint8)
    bg = bc._per_frame_background(f, "not_a_mode")
    assert (bg == 7).all()


# ----- preview_background routing ------------------------------------------

def _cache():
    return bc.BackgroundCache(Config())


def _frame(cfg):
    rng = np.random.default_rng(0)
    return rng.integers(0, 60, (cfg.frame_height, cfg.frame_width)).astype(np.uint8)


def test_spatial_mode_previews_per_frame():
    cfg = Config()
    cache = _cache()
    bg, info = cache.preview_background(_frame(cfg), bg_mode="line_median")
    assert bg.shape == (cfg.frame_height, cfg.frame_width)
    assert bg.dtype == np.uint8
    assert info["requested_mode"] == "line_median"
    assert info["preview_source"] == "per-frame line_median"


def test_temporal_median_without_model_degrades_to_block():
    cfg = Config()
    cache = _cache()
    cache._model = None
    bg, info = cache.preview_background(_frame(cfg), bg_mode="temporal_median")
    assert bg.shape == (cfg.frame_height, cfg.frame_width)
    assert "no temporal-median stack" in info["preview_source"]
    assert "block_percentile" in info["preview_source"]


def test_temporal_median_with_model_renders_the_real_stack():
    if not bc.HAS_BG_IMAGE:
        return  # wheel without the image cache: nothing to render
    cfg = Config()
    cache = _cache()
    h, w, b = cfg.frame_height, cfg.frame_width, cache.bin
    stack = (np.ones((h // b, w // b)) * 33).astype(np.uint8)
    cache._model = bc.BgModel(
        noise=2.0, h=h, w=w, bin=b, epoch=time.monotonic(),
        n_frames=12, bg_image=stack)
    bg, info = cache.preview_background(_frame(cfg), bg_mode="temporal_median")
    # The preview IS the cached stack upsampled — not an approximation.
    assert (bg == 33).all()
    assert "live temporal-median stack" in info["preview_source"]
    assert "12 frames" in info["preview_source"]


def test_preview_never_mutates_cache_state():
    cfg = Config()
    cache = _cache()
    before = (cache._n_cached, cache._n_fallback, cache._frame_count(),
              cache._model)
    cache.preview_background(_frame(cfg), bg_mode="row_percentile")
    cache.preview_background(_frame(cfg), bg_mode="temporal_median")
    after = (cache._n_cached, cache._n_fallback, cache._frame_count(),
             cache._model)
    assert before == after  # read-only: no submit, no counter bump, no rebuild
