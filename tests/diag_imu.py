#!/usr/bin/env python3
"""diag_imu.py — probe the IMU (BNO055 or BNO085) and stream live quaternions.

Validates the sensor driver end-to-end, independent of the main daemon: which
chip was detected and at what address, then a live stream of (w,x,y,z) with the
per-sample rotation angle and |q|. Use it to confirm, on real hardware, that the
chosen driver produces a valid right-handed unit quaternion and that physical
motion tracks (the one thing CI can't check for the BNO085 SH-2 path).

Run on-device as root (needs /dev/i2c-1 access):

    sudo /opt/diofinder/venv/bin/python3 /opt/diofinder/tests/diag_imu.py
    sudo .../diag_imu.py --sensor bno085        # force a driver
    sudo .../diag_imu.py --poll-hz 20 --count 0 # stream until Ctrl-C

Sanity check while watching the stream: rotate the sensor about each axis in
turn — |q| should stay ~1.000, and the d (per-sample angle) should rise with
motion and fall to ~0 when still. If the quaternion never updates or |q| is not
~1, the driver/ wiring/address is wrong (see detected line).
"""

import argparse
import math
import sys
import time

from diofinder import imu_proc


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Probe and stream IMU quaternions (BNO055 or BNO085).")
    ap.add_argument("--sensor", default="auto",
                    choices=["auto", "bno055", "bno085"],
                    help="which driver to probe (default: auto)")
    ap.add_argument("--poll-hz", type=float, default=20.0,
                    help="sample/report rate in Hz (default: 20)")
    ap.add_argument("--count", type=int, default=100,
                    help="samples to print, 0 = until Ctrl-C (default: 100)")
    args = ap.parse_args(argv)

    smbus2 = imu_proc._import_smbus2()
    if smbus2 is None:
        sys.exit("smbus2 not installed (pip install smbus2)")

    kind, bus, addr, restored = imu_proc._probe_sensor(
        smbus2, args.sensor, False, args.poll_hz)
    if bus is None:
        sys.exit(f"No IMU found (pref={args.sensor}). "
                 f"Check wiring, the I2C bus, and the address "
                 f"(BNO055 0x28/0x29, BNO085 0x4A/0x4B).")
    print(f"detected: {kind} at I2C 0x{addr:02x}")

    period = 1.0 / max(1.0, args.poll_hz)
    last = None
    n = 0
    try:
        while args.count == 0 or n < args.count:
            t0 = time.monotonic()
            if kind == "bno085":
                q = imu_proc._bno085_read_quaternion(smbus2, bus, addr)
            else:
                q = imu_proc._read_quaternion(bus, addr)
            if q is not None:
                d = imu_proc._quat_angle_deg(q, last) if last is not None else 0.0
                nrm = math.sqrt(sum(c * c for c in q))
                print(f"w={q[0]:+.4f} x={q[1]:+.4f} y={q[2]:+.4f} z={q[3]:+.4f}"
                      f"   |q|={nrm:.4f}   d={d:6.2f}deg")
                last = q
                n += 1
            sleep_t = period - (time.monotonic() - t0)
            if sleep_t > 0:
                time.sleep(sleep_t)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            bus.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
