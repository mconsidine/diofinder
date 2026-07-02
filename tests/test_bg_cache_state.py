"""Unit tests for BackgroundCache.state().

Focus: the consumer must drop to per-frame detection whenever the live
temporal model is stale — including while a rebuild is *pending* (the no-IMU
slew recovery path), not just while ``_slewing`` is set. Regression guard for
the "stale background suppresses detection after a slew, solver never
re-acquires" bug.

bg_cache.py does ``import star_detect`` (the aarch64-only sycamore wheel)
unconditionally, so we stub it before importing the module — these tests
exercise pure state logic and need no real extractor.
"""
import importlib.util
import os
import sys
import time
import types
from types import SimpleNamespace


def _load_bg_cache():
    # Ensure star_detect exposes the FULL cached API before bg_cache is loaded.
    # Augment rather than replace: another test (e.g. test_auto_exposure) may
    # have already installed a thinner star_detect stub, and if it lacks
    # detect_stars_with_cache / compute_row_medians_py then bg_cache's
    # module-level HAS_CACHE probes False, BackgroundCache disables itself, and
    # note_motion / note_solve_result early-return — silently breaking the slew
    # state tests when the suite runs in order.
    sd = sys.modules.get("star_detect")
    if sd is None:
        sd = types.ModuleType("star_detect")
        sys.modules["star_detect"] = sd
    if not hasattr(sd, "detect_stars"):
        sd.detect_stars = lambda *a, **k: []
    if not hasattr(sd, "detect_stars_with_cache"):
        sd.detect_stars_with_cache = lambda *a, **k: []
    if not hasattr(sd, "compute_row_medians_py"):
        sd.compute_row_medians_py = lambda *a, **k: None
    if not hasattr(sd, "compute_block_medians_py"):
        sd.compute_block_medians_py = lambda *a, **k: None
    path = os.path.join(os.path.dirname(__file__), "..", "diofinder", "bg_cache.py")
    spec = importlib.util.spec_from_file_location("diofinder_bg_cache_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass introspection can resolve the module.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


bc = _load_bg_cache()


def _make_cache():
    cfg = SimpleNamespace(
        bg_cache_enabled=True,
        detect_bin=2,
        bg_cache_stack=8,
        bg_cache_refresh_s=5.0,
        bg_cache_slew_deg=0.5,
        bg_cache_max_age_s=60.0,
        detect_bg_mode="row_percentile",
        detect_bg_block_size=0,
        bg_cache_fail_invalidate=3,
    )
    return bc.BackgroundCache(cfg)


def _fake_model(cache, age_s=0.0):
    # state() only reads model.epoch and that the model exists.
    return bc.BgModel(
        noise=2.0, h=760, w=960, bin=cache.bin,
        epoch=time.monotonic() - age_s, n_frames=8,
        pose_quat=(1.0, 0.0, 0.0, 0.0),
    )


def test_warming_up_without_model():
    c = _make_cache()
    assert c.state() is bc.CacheState.WARMING_UP


def test_steady_with_fresh_model():
    c = _make_cache()
    c._model = _fake_model(c)
    assert c.state() is bc.CacheState.STEADY


def test_pending_rebuild_forces_per_frame():
    """A pending rebuild must drop the consumer to per-frame, not keep serving
    the stale model. This is the core of the no-IMU slew-recovery fix."""
    c = _make_cache()
    c._model = _fake_model(c)
    assert c.state() is bc.CacheState.STEADY
    c._needs_rebuild.set()                 # invalidation requested
    assert c.state() is bc.CacheState.SLEWING
    c._needs_rebuild.clear()               # worker published the fresh model
    assert c.state() is bc.CacheState.STEADY


def test_fail_streak_invalidates_without_imu():
    """End-to-end IMU-less path: consecutive solve failures reach the
    invalidate threshold, set the rebuild flag, and state() reflects it."""
    c = _make_cache()
    c._model = _fake_model(c)
    for _ in range(c._fail_invalidate - 1):
        c.note_solve_result(None, False)
        assert c.state() is bc.CacheState.STEADY        # not yet at threshold
    c.note_solve_result(None, False)                    # crosses threshold
    assert c._needs_rebuild.is_set()
    assert c.state() is bc.CacheState.SLEWING


def test_camera_epoch_change_flushes_and_invalidates():
    """An exposure/gain change (camera_settings_epoch bump) must flush the
    frame buffer and mark the model stale — frames captured at the old
    setting don't share the new frames' background pedestal."""
    import numpy as np
    c = _make_cache()
    c._model = _fake_model(c)
    c.note_camera_settings(1)                    # first sight: adopt silently
    c._frame_buf.append(np.zeros((4, 4), np.uint8))
    assert c.state() is bc.CacheState.STEADY
    c.note_camera_settings(1)                    # unchanged -> no-op
    assert c.state() is bc.CacheState.STEADY
    assert len(c._frame_buf) == 1
    c.note_camera_settings(2)                    # exposure/gain changed
    assert len(c._frame_buf) == 0                # stack flushed
    assert c._needs_rebuild.is_set()
    assert c.state() is bc.CacheState.SLEWING    # per-frame until refilled
    c.note_camera_settings(None)                 # missing key -> no-op
    assert c._camera_epoch == 2


def test_build_model_noise_is_not_quantized():
    """Regression for the 0.50 <-> 1.48 noise flip: the old uint8 casts
    quantized the MAD to whole DN so noise could only be 1.4826*k (k int,
    floored at 0.5). The float pipeline must resolve intermediate values and
    respond monotonically to the frame noise level."""
    import numpy as np
    c = _make_cache()                            # bin=2
    rng = np.random.default_rng(42)

    def frames(sigma):
        return [np.clip(rng.normal(30.0, sigma, (96, 96)), 0, 255)
                .astype(np.uint8) for _ in range(8)]

    lo = c._build_model(frames(6.0)).noise
    hi = c._build_model(frames(12.0)).noise
    assert hi > lo > 0.5
    # Neither value may sit on the old integer-MAD lattice {1.4826*k}.
    for v in (lo, hi):
        k = round(v / 1.4826)
        assert abs(v - 1.4826 * k) > 0.05, f"noise {v} still quantized"


def test_fail_streak_does_not_invalidate_with_imu():
    """When the IMU is feeding, a run of solve failures must NOT invalidate the
    model — note_motion owns slew detection, and dropping the cache on a
    stationary faint-signal scene would replace the √N-reduced background with
    noisier per-frame detection (the observed faint-sky fallback trap)."""
    c = _make_cache()
    c._model = _fake_model(c)
    c.note_motion((1.0, 0.0, 0.0, 0.0))                 # IMU now feeding, still
    for _ in range(c._fail_invalidate + 2):             # well past the threshold
        c.note_solve_result(None, False)
    assert not c._needs_rebuild.is_set()
    assert c.state() is bc.CacheState.STEADY


def test_slewing_flag_and_model_age():
    c = _make_cache()
    c._model = _fake_model(c)
    c._slewing = True
    assert c.state() is bc.CacheState.SLEWING
    c._slewing = False
    assert c.state() is bc.CacheState.STEADY
    c._model = _fake_model(c, age_s=c.max_age_s + 1.0)
    assert c.state() is bc.CacheState.SLEWING


def _quat_about_x(deg):
    import math
    h = math.radians(deg) / 2.0
    return (math.cos(h), math.sin(h), 0.0, 0.0)


def test_imu_slew_does_not_latch_after_stop():
    """Regression for the SLEWING deadlock: aiming the finder (a slew that ends
    at a new resting pose far from the model's build pose) must NOT latch
    _slewing forever. Once consecutive observations are stable again, _slewing
    clears even though the new pose differs from model.pose_quat."""
    c = _make_cache()
    c._model = _fake_model(c)                      # pose_quat = identity
    c.note_motion((1.0, 0.0, 0.0, 0.0))           # establish motion ref, still
    assert not c._slewing
    far = _quat_about_x(5.0)                        # 5 deg >> 0.5 deg threshold
    c.note_motion(far)                             # moving
    assert c._slewing and c._needs_rebuild.is_set()
    c.note_motion(far)                             # stopped at the new pose
    assert not c._slewing                          # <-- would latch True before fix
    assert c._needs_rebuild.is_set()              # still wants a rebuild here
    # And once the worker rebuilds at the new pose (clears the flag), STEADY.
    c._needs_rebuild.clear()
    assert c.state() is bc.CacheState.STEADY


def test_solve_driven_slew_without_imu():
    """No-IMU path: consecutive solved poses drive the same rate-based logic."""
    c = _make_cache()
    c._model = _fake_model(c)
    c.note_solve_result((1.0, 0.0, 0.0, 0.0), True)   # establish ref
    assert not c._slewing
    c.note_solve_result(_quat_about_x(5.0), True)     # moved
    assert c._slewing
    c.note_solve_result(_quat_about_x(5.0), True)     # stopped
    assert not c._slewing


def test_imu_precedence_over_solve():
    """When the IMU is feeding, solved poses must not also drive motion (the two
    quats are in different frames; mixing them produced bogus slews)."""
    c = _make_cache()
    c._model = _fake_model(c)
    c.note_motion((1.0, 0.0, 0.0, 0.0))               # IMU now feeding
    ref = c._motion_ref_quat
    c.note_solve_result(_quat_about_x(30.0), True)    # different frame; ignored
    assert c._motion_ref_quat == ref
    assert not c._slewing
