"""Unit tests for the FrameSlots new-frame gate (seq-based).

The solver passes the seq it last handled as ``after_seq`` so it blocks for a
*genuinely new* frame instead of re-processing one it already solved. These
tests run without any camera or extractor — pure multiprocessing primitives.
"""
import threading
import time

from diofinder.frame_slots import FrameSlots


def test_first_frame_returns_with_seq():
    s = FrameSlots()
    s.publish(0)
    idx, seq = s.acquire_read_slot(after_seq=-1)
    assert idx == 0 and seq == 1


def test_new_frame_returns_immediately():
    s = FrameSlots()
    s.publish(0)
    idx, seq = s.acquire_read_slot(after_seq=-1)
    assert seq == 1
    s.publish(1)                       # a genuinely new frame
    idx, seq = s.acquire_read_slot(after_seq=seq)
    assert idx == 1 and seq == 2


def test_stale_frame_blocks_then_times_out_to_current():
    """No new frame published: the gate blocks for ~timeout, then returns the
    current (not-new) frame so housekeeping/watchdog still run on a camera
    stall — it must NOT spin or return instantly."""
    s = FrameSlots()
    s.publish(0)
    _, seq = s.acquire_read_slot(after_seq=-1)   # seq == 1
    t0 = time.monotonic()
    idx, seq2 = s.acquire_read_slot(timeout=0.15, after_seq=seq)
    elapsed = time.monotonic() - t0
    assert idx == 0 and seq2 == seq              # same frame, seq not advanced
    assert elapsed >= 0.13                        # it actually waited


def test_blocks_until_publish_wakes_it():
    """A blocked reader must wake immediately when a new frame is published."""
    s = FrameSlots()
    s.publish(0)
    _, seq = s.acquire_read_slot(after_seq=-1)   # seq == 1

    result = {}

    def reader():
        idx, sq = s.acquire_read_slot(timeout=5.0, after_seq=seq)
        result["idx"] = idx
        result["seq"] = sq

    th = threading.Thread(target=reader)
    th.start()
    time.sleep(0.1)                               # ensure the reader is blocked
    assert not result                             # still waiting, no stale return
    s.publish(2)
    th.join(timeout=2.0)
    assert result == {"idx": 2, "seq": 2}


def test_seq_is_monotonic_across_publishes():
    s = FrameSlots()
    seqs = []
    last = -1
    for i in range(5):
        s.publish(i % 3)
        idx, last = s.acquire_read_slot(after_seq=last)
        seqs.append(last)
    assert seqs == [1, 2, 3, 4, 5]
