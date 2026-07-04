"""Display-SHM segment (P4): seqlock writer/reader round-trip + graceful
absence. No daemon required — creates a throwaway segment in this process.
"""
import numpy as np
import pytest

from diofinder import display_shm


@pytest.fixture
def segment():
    H, W = 12, 20
    shm = display_shm.create(H, W)
    try:
        yield H, W
    finally:
        shm.close()
        try:
            shm.unlink()
        except FileNotFoundError:
            pass


def test_write_then_read_roundtrip(segment):
    H, W = segment
    w = display_shm.DisplayWriter(H, W)
    r = display_shm.DisplayReader(H, W)
    assert w.available and r.available
    # Nothing published yet.
    assert r.read() is None
    f1 = (np.arange(H * W, dtype=np.uint8) % 251).reshape(H, W)
    w.write(f1)
    got = r.read()
    assert got is not None
    fr, seq = got
    np.testing.assert_array_equal(fr, f1)
    assert seq % 2 == 0 and seq > 0
    w.close()
    r.close()


def test_after_seq_skips_already_seen(segment):
    H, W = segment
    w = display_shm.DisplayWriter(H, W)
    r = display_shm.DisplayReader(H, W)
    f1 = np.full((H, W), 5, np.uint8)
    w.write(f1)
    _, seq = r.read()
    assert r.read(after_seq=seq) is None            # not newer -> None
    w.write(np.full((H, W), 9, np.uint8))
    fr2, seq2 = r.read(after_seq=seq)               # newer -> served
    assert seq2 > seq
    assert int(fr2[0, 0]) == 9
    w.close()
    r.close()


def test_absent_segment_is_graceful():
    # No segment created -> writer no-ops, reader reports unavailable.
    w = display_shm.DisplayWriter(8, 8)
    r = display_shm.DisplayReader(8, 8)
    assert not w.available
    assert not r.available
    w.write(np.zeros((8, 8), np.uint8))   # must not raise
    assert r.read() is None


def test_wrong_shape_write_is_noop(segment):
    H, W = segment
    w = display_shm.DisplayWriter(H, W)
    r = display_shm.DisplayReader(H, W)
    w.write(np.zeros((H + 1, W), np.uint8))   # shape mismatch -> ignored
    assert r.read() is None
    w.close()
    r.close()
