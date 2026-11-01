"""The IMU reader must not re-read its settings from shared_cfg every sample.

Each shared_cfg access is a Manager IPC round-trip on CPU 0. The reader used to
issue ~7 gets per sample (140/s at 20 Hz); settings are now snapshotted at most
once per _SETTINGS_TTL_S. No hardware: the I2C layer is faked.
"""
import threading
import time
import types

from diofinder import imu_proc


class _CountingDict(dict):
    """Stand-in for a Manager DictProxy that counts RPC-equivalent calls."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.reads = 0
        self.writes = 0

    def get(self, *a, **k):
        self.reads += 1
        return super().get(*a, **k)

    def copy(self):
        self.reads += 1
        return dict(self)

    def __setitem__(self, k, v):
        self.writes += 1
        super().__setitem__(k, v)


def test_settings_read_at_most_once_per_ttl(monkeypatch):
    monkeypatch.setattr(imu_proc, "_import_smbus2",
                        lambda: types.SimpleNamespace())
    monkeypatch.setattr(imu_proc, "_probe_and_init",
                        lambda smbus2, restore=False: (object(), 0x28, False))
    monkeypatch.setattr(imu_proc, "_read_quaternion",
                        lambda bus, addr: (1.0, 0.0, 0.0, 0.0))

    shared = _CountingDict(imu_enabled=True, imu_poll_hz=50,
                           imu_hunt_filter=False)
    stop = threading.Event()
    th = threading.Thread(target=imu_proc.imu_thread, args=(shared,),
                          kwargs={"stop_event": stop}, daemon=True)
    th.start()
    try:
        end = time.monotonic() + 3.0
        while "imu" not in shared and time.monotonic() < end:
            time.sleep(0.01)
        assert "imu" in shared
        r0, w0 = shared.reads, shared.writes
        time.sleep(1.0)                      # ~50 samples at 50 Hz
        samples = shared.writes - w0
        reads = shared.reads - r0
    finally:
        stop.set()
        th.join(timeout=3.0)
    assert samples >= 20
    # <= one settings snapshot per second (+1 for a TTL boundary), not per sample.
    assert reads <= 2, (reads, samples)


def test_settings_change_applies_within_ttl(monkeypatch):
    monkeypatch.setattr(imu_proc, "_import_smbus2",
                        lambda: types.SimpleNamespace())
    monkeypatch.setattr(imu_proc, "_probe_and_init",
                        lambda smbus2, restore=False: (types.SimpleNamespace(
                            close=lambda: None), 0x28, False))
    monkeypatch.setattr(imu_proc, "_read_quaternion",
                        lambda bus, addr: (1.0, 0.0, 0.0, 0.0))
    shared = {"imu_enabled": True, "imu_poll_hz": 20, "imu_hunt_filter": False}
    stop = threading.Event()
    th = threading.Thread(target=imu_proc.imu_thread, args=(shared,),
                          kwargs={"stop_event": stop}, daemon=True)
    th.start()
    try:
        end = time.monotonic() + 3.0
        while shared.get("imu_available") is not True and time.monotonic() < end:
            time.sleep(0.01)
        shared["imu_enabled"] = False
        t0 = time.monotonic()
        while shared.get("imu_available") is not False and time.monotonic() - t0 < 3.0:
            time.sleep(0.01)
        assert shared.get("imu_available") is False
        assert time.monotonic() - t0 < imu_proc._SETTINGS_TTL_S + 0.6
    finally:
        stop.set()
        th.join(timeout=3.0)
