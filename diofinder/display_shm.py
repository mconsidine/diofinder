"""Demand-gated display frame segment (P4 — decouple the web live view from the
solve loop).

The web UI's ``/frame.jpg`` was served through the ``frame_get`` maint command:
solver copies the frame, pickles it through the reply queue, comms base64-encodes
it, the browser decodes ~1 MB of JSON — ~6 frame copies plus base64+JSON per
poll, with the copy and queue pickle stolen from the *solver* process and the
command serviced only between frames (adding up to a frame period of latency).
Every open live-view tab therefore measurably slowed the solve loop.

Instead the solver publishes the frame it already holds into a dedicated,
name-attachable SHM segment, and the web UI reads it **directly** — no maint
round-trip, no base64, no solver-thread theft. Any process can attach by name;
consistency is a best-effort **seqlock** (even/odd generation counter) with a
double-read + retry, which is strictly better than the pre-existing
direct-SHM fallback (which had no tear protection at all). A rare torn preview
frame is cosmetically harmless for a finder.

**Demand-gated**: the solver only writes the segment while a viewer is active
(``shared_cfg["display_wanted_until"]`` in the future — bumped by the web UI's
``display_start`` keepalive). With no browser open the cost is a single dict
key check on the per-frame snapshot; the ~0.73 MB copy is never paid.

Segment layout (little-endian, fixed frame size H*W):
    [0:8]   seq   uint64  — even = stable, odd = write in progress
    [8:12]  height uint32
    [12:16] width  uint32
    [16: ]  frame  uint8[H*W]
"""
from __future__ import annotations

import numpy as np
from multiprocessing import shared_memory

DISPLAY_SHM_NAME = "diofinder_display"
_HEADER_BYTES = 16


def segment_size(height: int, width: int) -> int:
    return _HEADER_BYTES + int(height) * int(width)


def _views(buf, height: int, width: int):
    seq = np.ndarray((1,), dtype=np.uint64, buffer=buf, offset=0)
    shape = np.ndarray((2,), dtype=np.uint32, buffer=buf, offset=8)
    data = np.ndarray((height, width), dtype=np.uint8, buffer=buf,
                      offset=_HEADER_BYTES)
    return seq, shape, data


def create(height: int, width: int):
    """Launcher: (re)create the display segment. Unlinks any stale segment of
    the same name first (mirrors the frame-buffer allocator). Returns the
    SharedMemory handle — the caller must keep a reference for the process
    lifetime."""
    try:
        stale = shared_memory.SharedMemory(name=DISPLAY_SHM_NAME)
        stale.close()
        stale.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return shared_memory.SharedMemory(
        name=DISPLAY_SHM_NAME, create=True, size=segment_size(height, width))


class DisplayWriter:
    """Solver-side writer. Attach best-effort (returns a no-op writer if the
    segment is absent, e.g. an older launcher), then call ``write`` only when
    a viewer is active."""

    def __init__(self, height: int, width: int):
        self.height = int(height)
        self.width = int(width)
        self._gen = 0
        self._shm = None
        self._seq = self._shape = self._data = None
        try:
            self._shm = shared_memory.SharedMemory(name=DISPLAY_SHM_NAME)
            if self._shm.size < segment_size(height, width):
                self._shm.close()
                self._shm = None
            else:
                self._seq, self._shape, self._data = _views(
                    self._shm.buf, self.height, self.width)
                self._shape[0] = self.height
                self._shape[1] = self.width
                # Resume the generation count from whatever is already
                # published, so a writer attaching to a non-fresh segment
                # never re-issues a seq a reader has already seen (a
                # repeated/backwards seq reads as "no new frame" to a
                # last-seq-tracking consumer).
                self._gen = (int(self._seq[0]) + 1) // 2
        except FileNotFoundError:
            self._shm = None
        except Exception:
            self._shm = None

    @property
    def available(self) -> bool:
        return self._shm is not None

    def write(self, frame_u8: np.ndarray) -> None:
        """Publish one frame under the seqlock. No-op if the segment is
        unavailable or the frame shape doesn't match."""
        if self._shm is None or frame_u8.shape != (self.height, self.width):
            return
        # Odd = write in progress; even = stable. A reader that sees an odd or
        # a changed seq around its data read discards and retries.
        self._seq[0] = np.uint64(2 * self._gen + 1)
        np.copyto(self._data, frame_u8)
        self._seq[0] = np.uint64(2 * self._gen + 2)
        self._gen += 1

    def close(self):
        if self._shm is not None:
            try:
                self._shm.close()
            except Exception:
                pass
            self._shm = None


class DisplayReader:
    """Web-UI-side reader. Attach best-effort (``available`` is False when the
    segment is absent — older daemon — so the caller falls back to
    ``frame_get``). ``read`` returns (frame_copy, seq) on a consistent read or
    None on a torn/empty/absent read."""

    def __init__(self, height: int, width: int):
        self.height = int(height)
        self.width = int(width)
        self._shm = None
        self._seq = self._shape = self._data = None
        try:
            from multiprocessing import resource_tracker as _rt
            self._shm = shared_memory.SharedMemory(name=DISPLAY_SHM_NAME)
            # This process only attaches; it must never unlink the daemon's
            # segment on GC (the resource_tracker would otherwise try).
            try:
                _rt.unregister(self._shm._name, "shared_memory")
            except Exception:
                pass
            if self._shm.size < segment_size(height, width):
                self._shm.close()
                self._shm = None
            else:
                self._seq, self._shape, self._data = _views(
                    self._shm.buf, self.height, self.width)
        except FileNotFoundError:
            self._shm = None
        except Exception:
            self._shm = None

    @property
    def available(self) -> bool:
        return self._shm is not None

    def read(self, after_seq: int = -1, retries: int = 6):
        """Return (frame, seq) for the newest consistently-published frame, or
        None. ``after_seq``: only return a frame with seq > after_seq (else
        None) — used to skip a frame already served."""
        if self._shm is None:
            return None
        for _ in range(max(1, retries)):
            s1 = int(self._seq[0])
            if s1 == 0 or (s1 & 1):
                # Never written, or a write is in progress.
                continue
            if s1 <= after_seq:
                return None
            h = int(self._shape[0])
            w = int(self._shape[1])
            if h != self.height or w != self.width:
                continue
            frame = self._data.copy()
            s2 = int(self._seq[0])
            if s1 == s2:
                return frame, s1
        return None

    def close(self):
        if self._shm is not None:
            try:
                self._shm.close()
            except Exception:
                pass
            self._shm = None
