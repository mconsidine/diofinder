"""
BNO055 absolute orientation IMU — background daemon thread.

Probes for the sensor on I2C bus 1 (GPIO 2/3, the Pi's primary I2C)
at the standard BNO055 addresses (0x28 with ADDR low, 0x29 with ADDR
high).  When found, initialises the chip in IMUPLUS mode (accelerometer +
gyroscope, magnetometer disabled) and publishes quaternion updates to
shared_cfg at ~20 Hz.

IMUPLUS mode is intentional: the magnetometer is unreliable near the
telescope's metal body and motor drives.  Gyroscope-only fusion
accumulates ~1–5°/hour of drift, but plate-solve corrections reset the
dead-reckoning reference frequently so drift never builds up.

Hot-plug: if the device is absent at startup or disappears mid-session,
the thread falls back to probing every 3 s without any error output to
the user.  shared_cfg["imu_available"] is set False so all downstream
code (comms_proc smoothing, web UI) degrades gracefully.

Requires: smbus2  (pip install smbus2)
The diofinder user must be a member of the 'i2c' group, or the service
must run as root, for /dev/i2c-1 access.
"""

import logging
import math
import threading
import time

log = logging.getLogger("diofinder.imu")

# ---- Hardware constants -------------------------------------------------------

_I2C_BUS     = 1        # /dev/i2c-1  (GPIO 2 = SDA, GPIO 3 = SCL)
_ADDR_LOW    = 0x28     # ADDR pin → GND (default)
_ADDR_HIGH   = 0x29     # ADDR pin → VCC

# BNO055 register map (subset)
_REG_CHIP_ID    = 0x00  # expected value 0xA0
_REG_OPR_MODE   = 0x3D
_REG_PWR_MODE   = 0x3E
_REG_SYS_TRIGGER = 0x3F
_REG_UNIT_SEL   = 0x3B
_REG_QUAT_W_LSB = 0x20  # 8 bytes: W_LSB W_MSB X_LSB X_MSB Y_LSB Y_MSB Z_LSB Z_MSB

_CHIP_ID      = 0xA0
_OPR_CONFIG   = 0x00    # configuration mode (required before mode changes)
_OPR_IMUPLUS  = 0x08    # accel + gyro fusion; no magnetometer
_PWR_NORMAL   = 0x00

_POLL_HZ      = 20
_POLL_INTERVAL = 1.0 / _POLL_HZ
_PROBE_INTERVAL = 3.0   # seconds between probe attempts when device absent


# ---- smbus2 soft-import -------------------------------------------------------

def _import_smbus2():
    try:
        import smbus2
        return smbus2
    except ImportError:
        return None


# ---- Low-level I2C helpers ----------------------------------------------------

def _write(bus, addr, reg, value):
    bus.write_byte_data(addr, reg, value)


def _read(bus, addr, reg):
    return bus.read_byte_data(addr, reg)


def _read_quaternion(bus, addr):
    """Read the BNO055 quaternion output registers.

    Returns a normalised (w, x, y, z) tuple, or None if the read looks
    invalid (all-zero or non-unit magnitude).
    """
    data = bus.read_i2c_block_data(addr, _REG_QUAT_W_LSB, 8)

    def s16(lo, hi):
        v = (hi << 8) | lo
        return v - 65536 if v > 32767 else v

    w = s16(data[0], data[1])
    x = s16(data[2], data[3])
    y = s16(data[4], data[5])
    z = s16(data[6], data[7])

    # BNO055 quaternion scale factor: 1 / 2^14
    scale = 1.0 / 16384.0
    qw, qx, qy, qz = w * scale, x * scale, y * scale, z * scale

    # Re-normalise (hardware integer rounding can slightly break unity)
    n = math.sqrt(qw*qw + qx*qx + qy*qy + qz*qz)
    if n < 0.5:
        return None  # garbage read (sensor still initialising)
    return (qw/n, qx/n, qy/n, qz/n)


# ---- Probe + initialise -------------------------------------------------------

def _probe_and_init(smbus2):
    """Try both I2C addresses.  Return (bus, addr) on success or (None, None)."""
    for addr in (_ADDR_LOW, _ADDR_HIGH):
        bus = None
        try:
            bus = smbus2.SMBus(_I2C_BUS)
            chip_id = _read(bus, addr, _REG_CHIP_ID)
            if chip_id != _CHIP_ID:
                bus.close()
                continue

            # Sequence from BNO055 datasheet §3.1 "Operation mode switching"
            _write(bus, addr, _REG_OPR_MODE, _OPR_CONFIG)
            time.sleep(0.025)
            _write(bus, addr, _REG_PWR_MODE, _PWR_NORMAL)
            time.sleep(0.010)
            # Metric units (m/s², deg, deg/s); we only read quaternions so
            # this doesn't matter for our use-case but set it for consistency.
            _write(bus, addr, _REG_UNIT_SEL, 0x00)
            # Switch to IMUPLUS (accel + gyro fusion, magnetometer off)
            _write(bus, addr, _REG_OPR_MODE, _OPR_IMUPLUS)
            time.sleep(0.025)   # datasheet: ≥7 ms after mode change

            log.info("BNO055 detected at I2C 0x%02x — IMUPLUS mode active", addr)
            return bus, addr

        except Exception as e:
            log.debug("BNO055 probe 0x%02x: %s", addr, e)
            try:
                if bus:
                    bus.close()
            except Exception:
                pass

    return None, None


# ---- Daemon thread ------------------------------------------------------------

def imu_thread(shared_cfg, stop_event=None):
    """
    Entry point for the IMU daemon thread.

    Publishes to shared_cfg:
        "imu_available"  bool   — True when sensor is responding
        "imu_q"          tuple  — (w, x, y, z) most recent quaternion
        "imu_t"          float  — monotonic timestamp of last successful read
    """
    smbus2 = _import_smbus2()
    if smbus2 is None:
        log.warning("smbus2 not installed — IMU support disabled. "
                    "Run: pip install smbus2")
        shared_cfg["imu_available"] = False
        return

    shared_cfg["imu_available"] = False
    bus = addr = None
    last_probe = 0.0

    while stop_event is None or not stop_event.is_set():
        # ---- Device absent: probe periodically --------------------------------
        if bus is None:
            now = time.monotonic()
            if now - last_probe < _PROBE_INTERVAL:
                time.sleep(0.1)
                continue
            last_probe = now
            bus, addr = _probe_and_init(smbus2)
            if bus is None:
                shared_cfg["imu_available"] = False
                continue
            shared_cfg["imu_available"] = True

        # ---- Device present: read at _POLL_HZ ---------------------------------
        t0 = time.monotonic()
        try:
            q = _read_quaternion(bus, addr)
            if q is not None:
                shared_cfg["imu_q"] = q
                shared_cfg["imu_t"] = t0
        except Exception as e:
            log.warning("BNO055 read error (%s) — will re-probe", e)
            try:
                bus.close()
            except Exception:
                pass
            bus = addr = None
            shared_cfg["imu_available"] = False
            last_probe = 0.0    # attempt re-probe immediately next iteration
            continue

        # Sleep for the remainder of the poll interval
        sleep_t = _POLL_INTERVAL - (time.monotonic() - t0)
        if sleep_t > 0:
            time.sleep(sleep_t)


def start_imu_thread(shared_cfg):
    """Start the IMU thread as a daemon.  Returns the thread (informational)."""
    t = threading.Thread(
        target=imu_thread,
        args=(shared_cfg,),
        name="diofinder-imu",
        daemon=True,
    )
    t.start()
    log.info("IMU thread started (will activate when BNO055 is detected)")
    return t
