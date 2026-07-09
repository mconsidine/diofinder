"""frame_meta: exact per-frame capture metadata (v0.11.46).

Pins the ring bookkeeping, the seq lookup, and — most importantly — the
wall-clock derivation: exposure_start = SensorTimestamp (BOOTTIME ns, first
row readout start) + wall_offset − ExposureTime. Also covers bundle_solve's
pure record assembly and old-bundle fallback.
"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from diofinder import frame_meta
from tests.bundle_solve import build_record, load_bundle_meta


def test_make_entry_and_lookup():
    md = {"SensorTimestamp": 1_000_000_000, "ExposureTime": 500_000,
          "AnalogueGain": 4.0}
    e = frame_meta.make_entry(7, md, offset_ns=123)
    assert e == (7, 1_000_000_000, 500_000, 4.0, 123)
    ring = frame_meta.push((), e)
    got = frame_meta.lookup(ring, 7)
    assert got["seq"] == 7 and got["exposure_us"] == 500_000
    assert got["gain"] == 4.0 and got["wall_offset_ns"] == 123
    assert frame_meta.lookup(ring, 8) is None
    assert frame_meta.lookup(None, 7) is None


def test_make_entry_rejects_incomplete_metadata():
    assert frame_meta.make_entry(1, {}, 0) is None
    assert frame_meta.make_entry(1, {"SensorTimestamp": 5}, 0) is None
    # Missing gain is tolerated (0.0), timing fields are not.
    e = frame_meta.make_entry(1, {"SensorTimestamp": 5, "ExposureTime": 2}, 0)
    assert e[3] == 0.0


def test_ring_is_bounded_and_newest_wins():
    ring = ()
    for i in range(frame_meta.RING_SIZE * 2):
        ring = frame_meta.push(
            ring, frame_meta.make_entry(
                i, {"SensorTimestamp": i, "ExposureTime": 1}, 0))
    assert len(ring) == frame_meta.RING_SIZE
    assert frame_meta.lookup(ring, 0) is None            # evicted
    assert frame_meta.lookup(ring, frame_meta.RING_SIZE * 2 - 1) is not None


def test_derive_times_exact_math():
    # readout at boottime 2e9 ns, wall clock 100 s ahead of boottime,
    # exposure 0.5 s -> exposure started 1.5 s + offset on the wall clock.
    meta = {"sensor_timestamp_ns": 2_000_000_000,
            "exposure_us": 500_000,
            "wall_offset_ns": 100_000_000_000}
    t = frame_meta.derive_times(meta)
    assert t["exposure_s"] == 0.5
    # 100 s + 2 s = 102 s epoch readout; start = 101.5 s epoch
    assert t["readout_start_utc"].startswith("1970-01-01T00:01:42")
    assert t["exposure_start_utc"].startswith("1970-01-01T00:01:41.500")


def test_derive_times_tolerates_missing():
    assert frame_meta.derive_times(None) == {}
    assert frame_meta.derive_times({}) == {}
    assert frame_meta.derive_times({"exposure_us": 5}) == {}


def test_build_record_solved_with_meta():
    soln = {"RA": 3.14, "Dec": 5.6, "Roll": 45.0, "FOV": 13.5, "Matches": 18}
    meta = {"seq": 42, "exposure_start_utc": "T0", "readout_start_utc": "T1",
            "exposure_s": 0.629, "capture": {"gain": 17.0},
            "saved_at": "TS", "solution": {"solved": True, "ra_deg": 3.13}}
    rec = build_record(soln, "calibrated", 124, 17.1, meta)
    assert rec["solved"] and rec["ra_deg"] == 3.14 and rec["matches"] == 18
    assert rec["seq"] == 42 and rec["gain"] == 17.0
    assert rec["exposure_start_utc"] == "T0" and rec["exposure_s"] == 0.629
    assert rec["solve_pass"] == "calibrated"
    assert rec["live_solution"]["ra_deg"] == 3.13


def test_build_record_unsolved_no_meta():
    rec = build_record(None, None, 12, 5.0, None)
    assert rec["solved"] is False and rec["ra_deg"] is None
    assert rec["stars"] == 12 and rec["seq"] is None
    assert rec["live_solution"] is None


def test_load_bundle_meta_prefers_frames_json(tmp_path):
    (tmp_path / "frames.json").write_text(json.dumps(
        {"frame_01_raw.png": {"seq": 9, "exposure_s": 0.5}}))
    (tmp_path / "imu.json").write_text(json.dumps(
        [{"frame": "frame_01", "wall_time": "W", "imu": {}}]))
    m = load_bundle_meta(tmp_path)
    assert m["frame_01_raw.png"]["seq"] == 9


def test_load_bundle_meta_falls_back_to_imu_json(tmp_path):
    (tmp_path / "imu.json").write_text(json.dumps(
        [{"frame": "frame_02", "wall_time": "W2", "imu": {"q": [1, 0, 0, 0]}}]))
    m = load_bundle_meta(tmp_path)
    assert m["frame_02_raw.png"]["saved_at"] == "W2"
    assert m["frame_02_raw.png"]["seq"] is None
    assert load_bundle_meta(tmp_path / "nope") == {}


def test_frame_slots_publish_returns_seq():
    from diofinder.frame_slots import FrameSlots
    s = FrameSlots()
    idx = s.acquire_write_slot()
    assert s.publish(idx) == 1
    idx = s.acquire_write_slot()
    assert s.publish(idx) == 2
