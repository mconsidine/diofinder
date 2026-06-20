"""
Lock-protected triple-buffer state for camera <-> solver handoff.

Three shared-memory frame buffers are allocated by the parent. At any time
each buffer is in one of three logical states:

  * latest_ready    : the most recent fully-written frame (for solver to take)
  * reading         : the buffer the solver is currently reading from
  * free            : the camera writes to one of the free buffers

With three buffers we always have at least one free slot available to the
camera regardless of solver state, so the camera never blocks on the solver.

We track only two integers under a lock:
  latest_ready_idx    : -1 when no frame has ever been published, else 0..N-1
  reading_idx         : -1 when solver is idle, else 0..N-1
The "writing_idx" is implicit: anything that's neither latest_ready nor
reading is free.

The lock is held only for the microseconds needed to flip these integers,
never during the frame copy itself.
"""

import multiprocessing as mp
import time

# Architectural constants -- not user-tunable.
# NUM_BUFFERS=3 is the minimum that keeps camera and solver from
# blocking each other; bumping it higher gains nothing.
NUM_BUFFERS = 3
SHM_PREFIX = "efinder_frame"


class FrameSlots:
    def __init__(self):
        self.lock = mp.Lock()
        self.cond = mp.Condition(self.lock)
        # Use Value('i', ...) so they're shared across spawn'd processes
        self.latest_ready = mp.Value("i", -1, lock=False)
        self.reading_idx = mp.Value("i", -1, lock=False)
        # Monotonic publish counter. The slot index alone can repeat across the
        # 3-buffer ring, so a reader uses this to wait for a *genuinely new*
        # frame instead of re-processing the one it already handled.
        self.seq = mp.Value("q", 0, lock=False)

    def acquire_write_slot(self) -> int:
        """Camera: pick any buffer that's neither the latest published frame
        nor the buffer the solver is currently reading.

        With NUM_BUFFERS=3 and at most 2 buffers in 'use', there's always at
        least one free buffer; this never blocks.
        """
        with self.lock:
            for i in range(NUM_BUFFERS):
                if i != self.latest_ready.value and i != self.reading_idx.value:
                    return i
        # Unreachable with NUM_BUFFERS=3
        raise RuntimeError("No free frame slot (should be impossible)")

    def publish(self, idx: int) -> None:
        """Camera: mark a slot as the newest available frame and wake solver."""
        with self.cond:
            self.latest_ready.value = idx
            self.seq.value += 1
            self.cond.notify_all()

    def acquire_read_slot(self, timeout: float = None, after_seq: int = -1):
        """Solver: block until a frame *newer than* ``after_seq`` is published,
        then take ownership. Pass the seq returned by the previous call as
        ``after_seq`` to wait for a genuinely new frame instead of re-processing
        the one already handled (a new-frame gate). Default ``after_seq=-1``
        returns as soon as any frame exists.

        On ``timeout`` (seconds) it returns the current latest frame even if it
        is not newer than ``after_seq`` — so a camera stall can't wedge the
        solver; it still runs its housekeeping and feeds the watchdog. It keeps
        waiting only while *no* frame has ever been published.

        Returns ``(idx, seq)``.
        """
        with self.cond:
            deadline = None if timeout is None else time.monotonic() + timeout
            while not (self.latest_ready.value != -1 and self.seq.value > after_seq):
                if deadline is None:
                    self.cond.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if self.latest_ready.value != -1:
                        break  # timed out: return the current (not-new) frame
                    deadline = time.monotonic() + timeout  # no frame yet: keep waiting
                    continue
                self.cond.wait(remaining)
            idx = self.latest_ready.value
            self.reading_idx.value = idx
            # Don't clear latest_ready: camera is allowed to overwrite some
            # other slot (free or the previously-reading one).
            return idx, self.seq.value

    def release_read_slot(self) -> None:
        """Solver: relinquish the buffer it was reading from."""
        with self.lock:
            self.reading_idx.value = -1
