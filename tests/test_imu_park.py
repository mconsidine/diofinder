"""Unit test for the IMU park switch (imu_enabled) — no hardware.

`imu_enabled=False` must stop the reader thread from sampling/publishing and
drop `imu_available` (the same end state as unplugging the BNO055), then resume
cleanly — re-probing the device — when toggled back on. The I2C layer is faked
so the loop's park/resume decisions run without a sensor.
"""
import threading
import time
import types

from diofinder import imu_proc


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class _FakeBus:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_park_switch_stops_and_resumes(monkeypatch):
    buses = []

    def fake_probe(smbus2, restore=False):
        b = _FakeBus()
        buses.append(b)
        return b, 0x28, False

    monkeypatch.setattr(imu_proc, "_import_smbus2",
                        lambda: types.SimpleNamespace())
    monkeypatch.setattr(imu_proc, "_probe_and_init", fake_probe)
    monkeypatch.setattr(imu_proc, "_read_quaternion",
                        lambda bus, addr: (1.0, 0.0, 0.0, 0.0))

    shared = {"imu_enabled": True, "imu_poll_hz": 20, "imu_hunt_filter": False}
    stop = threading.Event()
    th = threading.Thread(target=imu_proc.imu_thread, args=(shared,),
                          kwargs={"stop_event": stop}, daemon=True)
    th.start()
    try:
        # Running: attaches, marks available, publishes.
        assert _wait(lambda: shared.get("imu_available") is True)
        assert _wait(lambda: "imu" in shared)

        # Park: available drops, the bus is closed, publishing stops.
        shared["imu_enabled"] = False
        assert _wait(lambda: shared.get("imu_available") is False)
        assert _wait(lambda: buses and buses[0].closed)
        snap = shared.get("imu")            # tuple carries a changing timestamp
        time.sleep(0.2)
        assert shared.get("imu") == snap    # no new samples while parked

        # Resume: re-probes a fresh bus and comes back available.
        shared["imu_enabled"] = True
        assert _wait(lambda: shared.get("imu_available") is True)
        assert len(buses) >= 2
    finally:
        stop.set()
        th.join(timeout=3.0)
    assert not th.is_alive()
