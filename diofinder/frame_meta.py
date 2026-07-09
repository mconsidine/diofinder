"""Per-frame capture metadata: exact exposure timing + settings, keyed by seq.

The camera process captures each frame via a picamera2 *request* and records
the libcamera metadata the ISP delivers with it:

  * ``SensorTimestamp`` — nanoseconds on the kernel CLOCK_BOOTTIME clock at
    the moment the FIRST ROW of the sensor's active array is read out
    (libcamera's definition). The first row's exposure therefore STARTED at
    ``SensorTimestamp - ExposureTime``. (Rolling shutter: later rows expose
    correspondingly later; for a finder the first-row time is the reference.)
  * ``ExposureTime`` — the exposure actually applied (µs), which may differ
    from the requested value by sensor quantization (line-period granularity).
  * ``AnalogueGain`` — the gain actually applied.

Because SensorTimestamp is on CLOCK_BOOTTIME, each entry also records the
camera process's measured ``wall_offset_ns = time_ns() - boottime_ns()`` so a
consumer can place the exposure on the wall clock:

    readout_start_wall_ns  = sensor_timestamp_ns + wall_offset_ns
    exposure_start_wall_ns = readout_start_wall_ns - exposure_us * 1000

Entries live in a small ring published to ``shared_cfg["frame_meta"]`` as a
tuple of tuples (single writer: camera_proc; one atomic Manager write per
frame, trivial at finder frame rates). Keyed by the FrameSlots seq so any
consumer holding a frame's seq (frame_get, debug bundles) can recover the
exact capture parameters for THAT frame.

Entry layout (plain tuple, pickle-friendly across Manager):
    (seq, sensor_timestamp_ns, exposure_us, gain, wall_offset_ns)
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

RING_SIZE = 8  # > NUM_BUFFERS and > any frame_get chase window


def wall_offset_ns() -> int:
    """CLOCK_REALTIME minus CLOCK_BOOTTIME, in ns (sampled now)."""
    return time.time_ns() - time.clock_gettime_ns(time.CLOCK_BOOTTIME)


def make_entry(seq: int, metadata: dict, offset_ns: int):
    """Build a ring entry from picamera2 request metadata, or None if the
    metadata lacks the timing fields (e.g. test mode / exotic pipeline)."""
    try:
        ts = int(metadata["SensorTimestamp"])
        exp = int(metadata["ExposureTime"])
    except (KeyError, TypeError, ValueError):
        return None
    try:
        gain = float(metadata.get("AnalogueGain", 0.0))
    except (TypeError, ValueError):
        gain = 0.0
    return (int(seq), ts, exp, gain, int(offset_ns))


def push(ring, entry):
    """Return a new bounded ring tuple with entry appended (oldest dropped)."""
    if entry is None:
        return ring
    ring = tuple(ring or ())
    return (ring + (tuple(entry),))[-RING_SIZE:]


def lookup(ring, seq: int):
    """Find the entry for seq in a ring; returns the meta dict used on the
    wire (frame_get reply / bundle records) or None."""
    for e in reversed(tuple(ring or ())):
        try:
            if int(e[0]) == int(seq):
                return {
                    "seq": int(e[0]),
                    "sensor_timestamp_ns": int(e[1]),
                    "exposure_us": int(e[2]),
                    "gain": float(e[3]),
                    "wall_offset_ns": int(e[4]),
                }
        except (TypeError, ValueError, IndexError):
            continue
    return None


def derive_times(meta: dict):
    """From a wire meta dict, derive wall-clock timestamps.

    Returns {exposure_start_utc, readout_start_utc, exposure_s} (ISO-8601,
    millisecond precision, UTC) or {} if meta is missing/incomplete.
    """
    if not meta:
        return {}
    try:
        readout_ns = int(meta["sensor_timestamp_ns"]) + int(meta["wall_offset_ns"])
        exp_us = int(meta["exposure_us"])
    except (KeyError, TypeError, ValueError):
        return {}
    start_ns = readout_ns - exp_us * 1000

    def _iso(ns):
        return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat(
            timespec="milliseconds")

    return {
        "exposure_start_utc": _iso(start_ns),
        "readout_start_utc": _iso(readout_ns),
        "exposure_s": exp_us / 1e6,
    }
