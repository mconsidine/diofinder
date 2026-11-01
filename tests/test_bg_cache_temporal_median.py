"""The bg_cache temporal median (sorting network) must equal np.median exactly.

bg_cache imports star_detect at module load; stub it like the other bg_cache
tests so this runs without the aarch64 wheel.
"""
import sys
import types

import numpy as np
import pytest

if "star_detect" not in sys.modules:
    sys.modules["star_detect"] = types.ModuleType("star_detect")

from diofinder import bg_cache  # noqa: E402


@pytest.mark.parametrize("n", list(range(1, 17)) + [24, 32, 33])
@pytest.mark.parametrize("dtype,hi", [(np.uint8, 256), (np.uint16, 1021)])
def test_matches_np_median(n, dtype, hi):
    rng = np.random.default_rng(n)
    frames = [rng.integers(0, hi, (37, 53), dtype=dtype) for _ in range(n)]
    ref = np.median(np.stack(frames), axis=0).astype(np.float32)
    got = bg_cache._temporal_median_f32(frames)
    assert got.dtype == np.float32
    np.testing.assert_array_equal(got, ref)


def test_inputs_not_mutated():
    rng = np.random.default_rng(0)
    frames = [rng.integers(0, 256, (16, 16), dtype=np.uint8) for _ in range(8)]
    before = [f.copy() for f in frames]
    bg_cache._temporal_median_f32(frames)
    for f, b in zip(frames, before):
        np.testing.assert_array_equal(f, b)


@pytest.mark.parametrize("n", range(2, 33))
def test_network_sorts_all_zero_one_inputs(n):
    # 0-1 principle: a comparator network sorts every input iff it sorts every
    # binary input. Exhaustive for small n, random sample above.
    net = bg_cache._batcher_network(n)
    rng = np.random.default_rng(n)
    cases = (range(1 << n) if n <= 12 else rng.integers(0, 1 << n, 4000))
    for v in cases:
        a = [(int(v) >> k) & 1 for k in range(n)]
        for i, j in net:
            if a[i] > a[j]:
                a[i], a[j] = a[j], a[i]
        assert a == sorted(a)
