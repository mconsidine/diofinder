#!/usr/bin/env python3
"""
BNO055 IMU sensor diagnostic.

Scans /dev/i2c-1 for a BNO055, verifies the chip, and prints every
data register the sensor exposes: quaternion, Euler angles, raw accel,
linear accel, gravity vector, gyroscope, magnetometer, temperature,
calibration status, self-test result, and system status.

Usage:
    sudo /opt/diofinder/venv/bin/python3 tests/diag_bno055.py
    sudo .../diag_bno055.py --address 0x29      # alternate I2C address
    sudo .../diag_bno055.py --bus 3              # different I2C bus
    sudo .../diag_bno055.py --samples 20         # more live samples
    sudo .../diag_bno055.py --interval 0.5       # faster sampling
    sudo .../diag_bno055.py --mode imuplus       # match the diofinder daemon mode
    sudo .../diag_bno055.py --no-mode-change     # leave the sensor in whatever mode it is

Note: the diofinder daemon uses IMUPLUS (accel + gyro, no magnetometer) to
avoid magnetic interference from telescope motors.  This script defaults
to NDOF (full 9-DOF fusion) so all outputs are exercised.
"""

import argparse
import math
import sys
import time

# ---------------------------------------------------------------------------
# Register addresses (page 0)
# ---------------------------------------------------------------------------

REG_CHIP_ID         = 0x00
REG_ACC_ID          = 0x01
REG_MAG_ID          = 0x02
REG_GYR_ID          = 0x03
REG_SW_REV_ID_LSB   = 0x04
REG_SW_REV_ID_MSB   = 0x05
REG_BL_REV_ID       = 0x06
REG_PAGE_ID         = 0x07

REG_ACC_DATA_X_LSB  = 0x08   # 6 bytes: accel X, Y, Z (100 LSB per m/s²)
REG_MAG_DATA_X_LSB  = 0x0E   # 6 bytes: mag   X, Y, Z (16  LSB per µT)
REG_GYR_DATA_X_LSB  = 0x14   # 6 bytes: gyro  X, Y, Z (16  LSB per °/s)
REG_EUL_H_LSB       = 0x1A   # 6 bytes: Euler heading, roll, pitch (16 LSB per °)
REG_QUA_W_LSB       = 0x20   # 8 bytes: quaternion W, X, Y, Z (16384 LSB per unit)
REG_LIA_DATA_X_LSB  = 0x28   # 6 bytes: linear accel X, Y, Z (100 LSB per m/s²)
REG_GRV_DATA_X_LSB  = 0x2E   # 6 bytes: gravity vector X, Y, Z (100 LSB per m/s²)

REG_TEMP            = 0x34
REG_CALIB_STAT      = 0x35
REG_ST_RESULT       = 0x36
REG_SYS_CLK_STAT    = 0x38
REG_SYS_STAT        = 0x39
REG_SYS_ERR         = 0x3A
REG_UNIT_SEL        = 0x3B
REG_OPR_MODE        = 0x3D
REG_PWR_MODE        = 0x3E
REG_TEMP_SOURCE     = 0x40   # 0x00 = accelerometer (default), 0x01 = gyroscope

CHIP_ID_EXPECTED    = 0xA0
ACC_ID_EXPECTED     = 0xFB
MAG_ID_EXPECTED     = 0x32
GYR_ID_EXPECTED     = 0x0F

OPR_CONFIG          = 0x00
OPR_IMUPLUS         = 0x08   # accel + gyro fusion, no mag (diofinder daemon mode)
OPR_NDOF            = 0x0C   # full 9-DOF fusion (default for this script)

OPR_NAMES = {
    0x00: "CONFIG",
    0x01: "ACCONLY",
    0x02: "MAGONLY",
    0x03: "GYROONLY",
    0x04: "ACCMAG",
    0x05: "ACCGYRO",
    0x06: "MAGGYRO",
    0x07: "AMG (all non-fused)",
    0x08: "IMUPLUS (accel+gyro, no mag)",
    0x09: "COMPASS",
    0x0A: "M4G",
    0x0B: "NDOF_FMC_OFF",
    0x0C: "NDOF (full 9-DOF fusion)",
}

SYS_STAT_NAMES = {
    0: "Idle",
    1: "Error (see SYS_ERR below)",
    2: "Initializing peripherals",
    3: "System initialization",
    4: "Executing self-test",
    5: "Sensor fusion running",
    6: "Running without fusion",
}

SYS_ERR_NAMES = {
    0:  "No error",
    1:  "Peripheral initialization error",
    2:  "System initialization error",
    3:  "Self-test failed",
    4:  "Register map value out of range",
    5:  "Register map address out of range",
    6:  "Register map write error",
    7:  "Low-power mode unavailable for selected op mode",
    8:  "Accelerometer power mode unavailable",
    9:  "Fusion algorithm configuration error",
    10: "Sensor configuration error",
}

# ---------------------------------------------------------------------------
# Low-level I2C helpers
# ---------------------------------------------------------------------------

def _r(bus, addr, reg):
    return bus.read_byte_data(addr, reg)

def _w(bus, addr, reg, val):
    bus.write_byte_data(addr, reg, val)

def _rblock(bus, addr, reg, n):
    return bus.read_i2c_block_data(addr, reg, n)

def _s16(lo, hi):
    v = (hi << 8) | lo
    return v - 65536 if v > 32767 else v

def _xyz(data, scale):
    return (
        _s16(data[0], data[1]) / scale,
        _s16(data[2], data[3]) / scale,
        _s16(data[4], data[5]) / scale,
    )

# ---------------------------------------------------------------------------
# Snapshot read
# ---------------------------------------------------------------------------

def read_snapshot(bus, addr):
    """Read all sensor outputs and status registers.  Returns a dict."""
    d = {}

    d['chip_id'] = _r(bus, addr, REG_CHIP_ID)
    d['acc_id']  = _r(bus, addr, REG_ACC_ID)
    d['mag_id']  = _r(bus, addr, REG_MAG_ID)
    d['gyr_id']  = _r(bus, addr, REG_GYR_ID)
    d['sw_rev']  = _r(bus, addr, REG_SW_REV_ID_LSB) | (_r(bus, addr, REG_SW_REV_ID_MSB) << 8)
    d['bl_rev']  = _r(bus, addr, REG_BL_REV_ID)

    d['opr_mode']     = _r(bus, addr, REG_OPR_MODE) & 0x0F
    d['pwr_mode']     = _r(bus, addr, REG_PWR_MODE) & 0x03
    d['sys_stat']     = _r(bus, addr, REG_SYS_STAT)
    d['sys_err']      = _r(bus, addr, REG_SYS_ERR)
    d['sys_clk_stat'] = _r(bus, addr, REG_SYS_CLK_STAT)

    st = _r(bus, addr, REG_ST_RESULT)
    d['st_mcu'] = bool(st & 0x08)
    d['st_gyr'] = bool(st & 0x04)
    d['st_mag'] = bool(st & 0x02)
    d['st_acc'] = bool(st & 0x01)

    cal = _r(bus, addr, REG_CALIB_STAT)
    d['cal_sys'] = (cal >> 6) & 0x03
    d['cal_gyr'] = (cal >> 4) & 0x03
    d['cal_acc'] = (cal >> 2) & 0x03
    d['cal_mag'] = (cal >> 0) & 0x03

    t = _r(bus, addr, REG_TEMP)
    d['temp_c'] = t if t < 128 else t - 256

    d['acc']   = _xyz(_rblock(bus, addr, REG_ACC_DATA_X_LSB,  6), 100.0)
    d['mag']   = _xyz(_rblock(bus, addr, REG_MAG_DATA_X_LSB,  6),  16.0)
    d['gyr']   = _xyz(_rblock(bus, addr, REG_GYR_DATA_X_LSB,  6),  16.0)
    d['euler'] = _xyz(_rblock(bus, addr, REG_EUL_H_LSB,       6),  16.0)
    d['lia']   = _xyz(_rblock(bus, addr, REG_LIA_DATA_X_LSB,  6), 100.0)
    d['grv']   = _xyz(_rblock(bus, addr, REG_GRV_DATA_X_LSB,  6), 100.0)

    raw = _rblock(bus, addr, REG_QUA_W_LSB, 8)
    qw = _s16(raw[0], raw[1]) / 16384.0
    qx = _s16(raw[2], raw[3]) / 16384.0
    qy = _s16(raw[4], raw[5]) / 16384.0
    qz = _s16(raw[6], raw[7]) / 16384.0
    d['quat'] = (qw, qx, qy, qz)

    return d

# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

_G  = "\033[32m"   # green
_Y  = "\033[33m"   # yellow
_R  = "\033[31m"   # red
_NC = "\033[0m"    # reset

def _ok(cond):   return f"{_G}PASS{_NC}" if cond else f"{_R}FAIL{_NC}"
def _flag(cond): return f"{_G}OK{_NC}"   if cond else f"{_Y}WARN{_NC}"

def _bar(n):
    return f"{'█' * n}{'░' * (3 - n)}  {n}/3"

def _sep(title=""):
    w = 62
    if title:
        pad = w - len(title) - 4
        return f"{'─'*2}  {title}  {'─'*pad}"
    return "─" * w

def _clock_stretch_indicators(d):
    """Return list of register names whose bit 7 is stuck high by the BCM2835 bug."""
    hits = []
    for name, actual, expected in [
        ("MAG_ID",  d['mag_id'],  MAG_ID_EXPECTED),
        ("GYR_ID",  d['gyr_id'],  GYR_ID_EXPECTED),
        ("SW_REV_MSB", (d['sw_rev'] >> 8) & 0xFF, (d['sw_rev'] >> 8) & 0x7F),
    ]:
        if (actual & 0x80) and not (expected & 0x80):
            hits.append(f"{name}: 0x{actual:02X} (expected 0x{expected:02X}, bit 7 stuck high)")
    return hits


def print_static(d):
    print()
    print(_sep("CHIP IDENTIFICATION"))
    print(f"  Chip ID      0x{d['chip_id']:02X}  expect 0xA0  {_ok(d['chip_id'] == CHIP_ID_EXPECTED)}")
    print(f"  Accel ID     0x{d['acc_id']:02X}  expect 0xFB  {_ok(d['acc_id']  == ACC_ID_EXPECTED)}")
    print(f"  Mag ID       0x{d['mag_id']:02X}  expect 0x32  {_ok(d['mag_id']  == MAG_ID_EXPECTED)}")
    print(f"  Gyro ID      0x{d['gyr_id']:02X}  expect 0x0F  {_ok(d['gyr_id']  == GYR_ID_EXPECTED)}")
    print(f"  SW rev       0x{d['sw_rev']:04X}")
    print(f"  BL rev       0x{d['bl_rev']:02X}")

    stretch = _clock_stretch_indicators(d)
    if stretch:
        print()
        print(_sep("CLOCK STRETCHING DETECTED"))
        print(f"  {_Y}The BCM2835 I2C master has a hardware bug: it releases SCL before the{_NC}")
        print(f"  {_Y}slave finishes clock-stretching, sampling bit 7 incorrectly.{_NC}")
        print(f"  {_Y}The BNO055 is a heavy clock-stretcher; these registers show the symptom:{_NC}")
        for h in stretch:
            print(f"    {h}")
        print(f"\n  Fix: add the following line to /boot/firmware/config.txt and reboot:")
        print(f"    dtparam=i2c_arm_baudrate=50000")
        print(f"\n  At 50 kHz the BNO055 never needs to stretch the clock.")
        print(f"  Until then, occasional corrupt sensor reads (wrong values, glitches)")
        print(f"  in accel/gyro/mag data and temperature are expected.")

    print()
    print(_sep("SYSTEM STATUS"))
    mode_str = OPR_NAMES.get(d['opr_mode'], f"UNKNOWN 0x{d['opr_mode']:02X}")
    pwr_str  = {0: "NORMAL", 1: "LOW", 2: "SUSPEND"}.get(d['pwr_mode'], f"0x{d['pwr_mode']:02X}")
    ss_str   = SYS_STAT_NAMES.get(d['sys_stat'], f"0x{d['sys_stat']:02X}")
    se_str   = SYS_ERR_NAMES.get(d['sys_err'],  f"0x{d['sys_err']:02X}")
    print(f"  Op mode      0x{d['opr_mode']:02X}  {mode_str}")
    print(f"  Power mode   0x{d['pwr_mode']:02X}  {pwr_str}")
    print(f"  Sys status   {d['sys_stat']}     {ss_str}  {_flag(d['sys_stat'] in (5, 6))}")
    print(f"  Sys error    {d['sys_err']}     {se_str}  {_flag(d['sys_err'] == 0)}")
    clk = "external crystal" if (d['sys_clk_stat'] & 0x01) else "internal RC oscillator"
    print(f"  Clock        {clk}")

    print()
    print(_sep("SELF-TEST"))
    print(f"  MCU          {_ok(d['st_mcu'])}")
    print(f"  Gyroscope    {_ok(d['st_gyr'])}")
    print(f"  Magnetometer {_ok(d['st_mag'])}")
    print(f"  Accelerometer{_ok(d['st_acc'])}")

    print()
    print(_sep("CALIBRATION  (0 = none, 3 = fully calibrated)"))
    print(f"  System       {_bar(d['cal_sys'])}")
    print(f"  Gyroscope    {_bar(d['cal_gyr'])}")
    print(f"  Accelerometer{_bar(d['cal_acc'])}")
    print(f"  Magnetometer {_bar(d['cal_mag'])}")
    if d['cal_gyr'] < 3:
        print(f"\n  {_Y}Tip{_NC}  Leave sensor motionless for ~2 s to calibrate gyro.")
    if d['cal_mag'] < 3:
        print(f"  {_Y}Tip{_NC}  Slow figure-8 motions calibrate the magnetometer.")
    if d['cal_acc'] < 3:
        print(f"  {_Y}Tip{_NC}  Place sensor on each of its 6 faces to calibrate accel.")


def print_sample(d, n=None):
    label = f"SAMPLE {n}" if n is not None else "SENSOR DATA"
    print()
    print(_sep(label))

    print(f"  Temperature  {d['temp_c']} °C")

    qw, qx, qy, qz = d['quat']
    qmag = math.sqrt(qw*qw + qx*qx + qy*qy + qz*qz)
    print(f"\n  Quaternion   w={qw:+.4f}  x={qx:+.4f}  y={qy:+.4f}  z={qz:+.4f}   |q|={qmag:.4f}")
    if abs(qmag - 1.0) > 0.05:
        print(f"  {_Y}WARN{_NC}  |q| deviates from 1.0 — sensor may still be initialising")

    hdg, roll, pitch = d['euler']
    print(f"  Euler        heading={hdg:7.2f}°  roll={roll:7.2f}°  pitch={pitch:7.2f}°")

    ax, ay, az = d['acc']
    amag = math.sqrt(ax*ax + ay*ay + az*az)
    print(f"\n  Accel raw    x={ax:+7.3f}  y={ay:+7.3f}  z={az:+7.3f}  m/s²   |a|={amag:.2f}")

    lx, ly, lz = d['lia']
    print(f"  Linear accel x={lx:+7.3f}  y={ly:+7.3f}  z={lz:+7.3f}  m/s²  (gravity subtracted)")

    gx, gy, gz = d['grv']
    gmag = math.sqrt(gx*gx + gy*gy + gz*gz)
    print(f"  Gravity vec  x={gx:+7.3f}  y={gy:+7.3f}  z={gz:+7.3f}  m/s²   |g|={gmag:.2f}")

    wx, wy, wz = d['gyr']
    print(f"\n  Gyroscope    x={wx:+7.2f}  y={wy:+7.2f}  z={wz:+7.2f}  °/s")

    mx, my, mz = d['mag']
    mmag = math.sqrt(mx*mx + my*my + mz*mz)
    earth_ok = 20.0 <= mmag <= 80.0
    mag_note = "" if earth_ok else f"  {_Y}(Earth field is typically 25–65 µT){_NC}"
    print(f"\n  Magnetometer x={mx:+8.3f}  y={my:+8.3f}  z={mz:+8.3f}  µT   |B|={mmag:.1f}{mag_note}")

    print(f"\n  Calibration  sys={d['cal_sys']}/3  gyr={d['cal_gyr']}/3  acc={d['cal_acc']}/3  mag={d['cal_mag']}/3")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="BNO055 IMU sensor diagnostic — presence check + all registers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--bus",     type=int,   default=1,
                    help="I2C bus number (default: 1 → /dev/i2c-1)")
    ap.add_argument("--address", default=None,
                    help="Force I2C address, e.g. 0x28 or 0x29 (default: auto-detect both)")
    ap.add_argument("--samples", type=int,   default=5,
                    help="Number of live data samples to print (default: 5)")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="Seconds between samples (default: 1.0)")
    ap.add_argument("--mode",    choices=["ndof", "imuplus", "current"], default="ndof",
                    help="Op mode to set before sampling: ndof (default), imuplus, current")
    ap.add_argument("--no-mode-change", action="store_true",
                    help="Do not change the current operation mode")
    args = ap.parse_args()

    # ---- smbus2 import -------------------------------------------------------
    try:
        import smbus2
    except ImportError:
        print("ERROR: smbus2 not installed.")
        print("       /opt/diofinder/venv/bin/pip install smbus2")
        sys.exit(1)

    # ---- I2C address(es) to probe -------------------------------------------
    if args.address:
        try:
            addrs = [int(args.address, 0)]
        except ValueError:
            print(f"ERROR: invalid address '{args.address}' — use e.g. 0x28")
            sys.exit(1)
    else:
        addrs = [0x28, 0x29]

    # ---- scan for the sensor ------------------------------------------------
    print(f"\nScanning I2C bus {args.bus} (/dev/i2c-{args.bus}) for BNO055 ...")

    bus = None
    addr = None
    for a in addrs:
        try:
            b = smbus2.SMBus(args.bus)
            cid = b.read_byte_data(a, REG_CHIP_ID)
            if cid == CHIP_ID_EXPECTED:
                print(f"  {_G}FOUND{_NC}  BNO055 at 0x{a:02X}  (chip_id=0x{cid:02X})")
                bus = b
                addr = a
                break
            else:
                print(f"  SKIP   0x{a:02X} responded with chip_id=0x{cid:02X} (not BNO055)")
                b.close()
        except OSError as e:
            print(f"  SKIP   0x{a:02X}: {e}")

    if bus is None:
        print(f"\n{_R}ERROR: BNO055 not found.{_NC}")
        print("\nChecklist:")
        print(f"  1. I2C wiring: GPIO 2 (pin 3) = SDA, GPIO 3 (pin 5) = SCL  (for bus 1)")
        print(f"  2. Power: VCC → 3.3 V (pin 1 or 17), GND → GND (pin 6/9/…)")
        print(f"  3. I2C enabled: sudo raspi-config → Interface Options → I2C → Enable")
        print(f"  4. Bus present: ls /dev/i2c-{args.bus}")
        print(f"  5. Scan: sudo i2cdetect -y {args.bus}")
        sys.exit(1)

    try:
        # ---- ensure page 0 ---------------------------------------------------
        _w(bus, addr, REG_PAGE_ID, 0x00)
        time.sleep(0.010)

        # ---- print chip info and current status ------------------------------
        snap = read_snapshot(bus, addr)
        print_static(snap)

        # ---- mode change -----------------------------------------------------
        if not args.no_mode_change and args.mode != "current":
            target = OPR_NDOF if args.mode == "ndof" else OPR_IMUPLUS
            if snap['opr_mode'] != target:
                tname = OPR_NAMES.get(target, f"0x{target:02X}")
                print(f"\nSwitching to {tname} mode ...")
                _w(bus, addr, REG_OPR_MODE, OPR_CONFIG)
                time.sleep(0.025)
                # Use gyroscope as temperature source.  The accelerometer source
                # has a known firmware quirk on many BNO055 modules: bit 7 of the
                # temperature register is stuck high before the accel temperature
                # compensation initialises, producing readings ~128 °C too cold
                # (e.g. 25 °C reads as -103 °C).  The gyroscope source is clean.
                _w(bus, addr, REG_TEMP_SOURCE, 0x01)
                time.sleep(0.010)
                _w(bus, addr, REG_OPR_MODE, target)
                # Temperature sensor (gyro source) needs ~3 s to output a valid
                # reading after mode change; other sensors are ready in < 100 ms.
                time.sleep(3.0)
                print("  Done.")
            else:
                cname = OPR_NAMES.get(snap['opr_mode'], "?")
                print(f"\nAlready in {cname} — no mode change needed.")

        cur_mode = _r(bus, addr, REG_OPR_MODE) & 0x0F
        if cur_mode == OPR_IMUPLUS:
            print(f"\n{_Y}Note:{_NC} IMUPLUS mode — magnetometer is disabled.  "
                  f"Mag readings and heading will be near zero.")
            print("       Re-run without --mode imuplus to see full 9-DOF output.")
        elif cur_mode == OPR_NDOF:
            print(f"\nNote: NDOF mode active — all 9 DOF fused.  "
                  f"(The diofinder daemon uses IMUPLUS to avoid motor interference.)")

        # ---- live samples ----------------------------------------------------
        if args.samples > 0:
            print(f"\nPrinting {args.samples} sample(s) at {args.interval:.1f}s intervals ...")
            for i in range(1, args.samples + 1):
                snap = read_snapshot(bus, addr)
                print_sample(snap, n=i)
                if i < args.samples:
                    time.sleep(args.interval)

        print()
        print(_sep("DONE"))
        print()

    finally:
        bus.close()


if __name__ == "__main__":
    main()
