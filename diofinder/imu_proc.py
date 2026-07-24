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

from diofinder import imu_persist as _imu_persist

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
_REG_CALIB_STAT = 0x35  # [7:6]=sys [5:4]=gyro [3:2]=accel [1:0]=mag, each 0-3
_REG_CALIB_DATA = 0x55  # 22 bytes (0x55..0x6A): accel/mag/gyro offsets + radii

_CHIP_ID      = 0xA0
_OPR_CONFIG   = 0x00    # configuration mode (required before mode changes)
_OPR_IMUPLUS  = 0x08    # accel + gyro fusion; no magnetometer
_PWR_NORMAL   = 0x00

_POLL_HZ      = 20
_POLL_INTERVAL = 1.0 / _POLL_HZ
_PROBE_INTERVAL = 3.0   # seconds between probe attempts when device absent

# --- Unit B: BNO055 calibration-profile persistence (default off) -----------
# The chip has no flash, so its gyro/accel calibration is lost on power-down.
# When imu_persist_bno055 is enabled we restore a saved profile at init (in
# CONFIG mode) and save one back once the chip is well-calibrated AND plate
# solves confirm the IMU is tracking truth AND the scope is roughly still (so
# the brief CONFIG excursion the save needs never lands mid-slew).
_STILL_THRESH_DEG = 0.1       # per-sample rotation below this counts as "still"
_STILL_FRAMES     = 20        # ~1 s at 20 Hz of stillness before a save
_STATUS_PUBLISH_INTERVAL = 1.0  # seconds between imu_calib_status publishes
_REF_FRESH_S      = 30.0      # imu_ref newer than this ⇒ solves are live


def _quat_angle_deg(q1, q2):
    """Angle (degrees) between two orientation quaternions (w,x,y,z)."""
    if q1 is None or q2 is None:
        return 999.0
    dot = abs(q1[0]*q2[0] + q1[1]*q2[1] + q1[2]*q2[2] + q1[3]*q2[3])
    return math.degrees(2.0 * math.acos(max(-1.0, min(1.0, dot))))


# ---- P3: BNO055 fusion-hunt suppression --------------------------------------
# In IMUPLUS (no magnetometer) the BNO055 can "hunt" between two nearby
# orientations while the scope is physically stationary — observed live as the
# quaternion toggling between two fixed 16-bit states ~0.8 deg apart. Published
# raw, that square wave drives the LX200 pointing prediction and SkySafari's
# reticle oscillates between two positions. An adaptive filter suppresses it:
# a reading within _HUNT_SNAP_DEG of the last published orientation is low-pass
# blended toward (attenuating the hunt), while a larger change SNAPS through
# unfiltered so a genuine slew is tracked with no added lag and the solve-hint
# Kabsch fit — which learns from real motion only — stays unbiased. Reversible
# via imu_hunt_filter (default on); when off the raw quaternion is published
# byte-for-byte.
_HUNT_SNAP_DEG = 2.0     # change above this is real motion -> pass through
_HUNT_ALPHA    = 0.35    # blend factor for sub-snap (hunting) readings


def _nlerp(q_from, q_to, alpha):
    """Normalised linear interpolation from q_from to q_to along the short arc.

    alpha=0 -> q_from, alpha=1 -> q_to. A cheap stand-in for slerp; the inputs
    here are always < _HUNT_SNAP_DEG apart, so the small-angle error is
    negligible. Returns a unit (w,x,y,z) tuple.
    """
    dot = q_from[0]*q_to[0] + q_from[1]*q_to[1] + q_from[2]*q_to[2] \
        + q_from[3]*q_to[3]
    s = -1.0 if dot < 0.0 else 1.0          # take the shorter arc
    q = [f + alpha * (s*t - f) for f, t in zip(q_from, q_to)]
    n = math.sqrt(q[0]*q[0] + q[1]*q[1] + q[2]*q[2] + q[3]*q[3])
    if n < 1e-9:
        return tuple(q_to)
    return (q[0]/n, q[1]/n, q[2]/n, q[3]/n)


def _hunt_filter(state, q, snap_deg=_HUNT_SNAP_DEG, alpha=_HUNT_ALPHA):
    """Attenuate BNO055 two-state hunting in the published quaternion.

    ``state`` is a mutable dict carrying the last published quaternion under
    key 'q'. A reading beyond ``snap_deg`` of it is real motion and is passed
    through unchanged (no lag); a nearer reading is blended toward, damping the
    stationary two-state oscillation. Pure/deterministic — unit-tested without
    hardware.
    """
    last = state.get("q")
    if last is None or _quat_angle_deg(q, last) > snap_deg:
        state["q"] = q          # first sample or real motion — snap through
        return q
    q_pub = _nlerp(last, q, alpha)
    state["q"] = q_pub
    return q_pub


def _plate_solves_confirm(shared_cfg):
    """True when plate solves are live (imu_ref freshly re-anchored) — the
    signal that the IMU is being validated against solved truth this session."""
    ref = shared_cfg.get("imu_ref")
    try:
        return (ref is not None and len(ref) >= 5
                and (time.monotonic() - float(ref[4])) < _REF_FRESH_S)
    except (TypeError, ValueError, IndexError):
        return False


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

def _restore_bno055_profile(bus, addr):
    """Write a saved calibration blob to the chip. MUST be called in CONFIG
    mode. Returns True if a profile was restored. Best-effort — never raises."""
    try:
        blob = _imu_persist.load_bno055_profile()
        if blob is None:
            return False
        bus.write_i2c_block_data(addr, _REG_CALIB_DATA, blob)
        time.sleep(0.010)
        log.info("BNO055 calibration profile restored from disk")
        return True
    except Exception as e:
        log.debug("BNO055 profile restore failed: %s", e)
        return False


def _save_bno055_profile(bus, addr, status):
    """Read the 22-byte calibration blob and persist it. The offsets are only
    reliably READable in CONFIG mode, so this makes a brief CONFIG excursion
    (~30 ms) and returns to IMUPLUS. Best-effort — never raises. Callers gate
    this on the scope being still so the excursion never lands mid-slew.
    Returns True on a successful save."""
    try:
        _write(bus, addr, _REG_OPR_MODE, _OPR_CONFIG)
        time.sleep(0.025)
        try:
            blob = bus.read_i2c_block_data(
                addr, _REG_CALIB_DATA, _imu_persist.BNO055_CALIB_LEN)
        finally:
            _write(bus, addr, _REG_OPR_MODE, _OPR_IMUPLUS)
            time.sleep(0.025)
        _imu_persist.save_bno055_profile(blob, status=status)
        log.info("BNO055 calibration profile saved (gyro/accel calibrated)")
        return True
    except Exception as e:
        log.debug("BNO055 profile save failed: %s", e)
        return False


def _probe_and_init(smbus2, restore=False):
    """Try both I2C addresses.  Return (bus, addr, restored) on success or
    (None, None, False).  When ``restore`` is set and a saved BNO055 profile
    exists, it is written back in CONFIG mode before entering IMUPLUS."""
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
            # Restore a saved calibration profile while still in CONFIG mode
            # (calib registers are only writable there), before entering IMUPLUS.
            restored = _restore_bno055_profile(bus, addr) if restore else False
            # Switch to IMUPLUS (accel + gyro fusion, magnetometer off)
            _write(bus, addr, _REG_OPR_MODE, _OPR_IMUPLUS)
            time.sleep(0.025)   # datasheet: ≥7 ms after mode change

            log.info("BNO055 detected at I2C 0x%02x — IMUPLUS mode active", addr)
            return bus, addr, restored

        except Exception as e:
            log.debug("BNO055 probe 0x%02x: %s", addr, e)
            try:
                if bus:
                    bus.close()
            except Exception:
                pass

    return None, None, False


# ---- Daemon thread ------------------------------------------------------------

def imu_thread(shared_cfg, stop_event=None):
    """
    Entry point for the IMU daemon thread.

    Publishes to shared_cfg:
        "imu_available"  bool   — True when sensor is responding
        "imu"            tuple  — ((w, x, y, z) quaternion, monotonic read
                                  timestamp) as ONE atomic composite key:
                                  halves the 20 Hz write RPCs and the pair
                                  can never tear between two Manager writes
                                  (audit 2026-07 P7). Readers unpack via
                                  imu_math.get_imu_qt (falls back to the
                                  legacy split imu_q / imu_t keys).
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
    # Unit B calibration-profile state (per successful attach).
    profile_saved = True        # set False on attach when persistence is on
    still_count = 0
    last_q = None
    last_status_pub = 0.0
    hunt_state = {}             # P3: BNO055 fusion-hunt suppression filter state

    while stop_event is None or not stop_event.is_set():
        # ---- Park switch: shed the entire IMU load without unplugging ---------
        # imu_enabled=False stops sampling/publishing and drops imu_available —
        # the same end state as pulling the sensor: the pointing path reports
        # solves only, and the solve hint / slew detection fall back to their
        # non-IMU behavior. Live-toggled from the Camera page; re-enabling
        # re-probes (bus set None here forces the probe branch below).
        if not shared_cfg.get("imu_enabled", True):
            if bus is not None:
                try:
                    bus.close()
                except Exception:
                    pass
                bus = addr = None
                last_probe = 0.0
            if shared_cfg.get("imu_available", False):
                shared_cfg["imu_available"] = False
            time.sleep(0.3)
            continue

        persist = bool(shared_cfg.get("imu_persist_bno055", False))
        # ---- Device absent: probe periodically --------------------------------
        if bus is None:
            now = time.monotonic()
            if now - last_probe < _PROBE_INTERVAL:
                time.sleep(0.1)
                continue
            last_probe = now
            bus, addr, restored = _probe_and_init(smbus2, restore=persist)
            if bus is None:
                shared_cfg["imu_available"] = False
                continue
            shared_cfg["imu_available"] = True
            # A saved profile already exists (restored) → nothing to re-save.
            # Only a device with no usable profile saves one once it calibrates.
            profile_saved = (not persist) or restored
            still_count = 0
            last_q = None
            hunt_state.clear()

        # ---- Device present: read at _POLL_HZ ---------------------------------
        t0 = time.monotonic()
        try:
            q = _read_quaternion(bus, addr)
            if q is not None:
                # P3: suppress fusion hunting before publishing so it never
                # reaches the LX200 pointing prediction (reversible; off ->
                # raw passthrough).
                if shared_cfg.get("imu_hunt_filter", True):
                    q = _hunt_filter(hunt_state, q)
                else:
                    hunt_state.clear()   # re-enable snaps cleanly next time
                shared_cfg["imu"] = (q, t0)
                # Stillness tracker (for the Unit B save gate).
                if _quat_angle_deg(q, last_q) < _STILL_THRESH_DEG:
                    still_count += 1
                else:
                    still_count = 0
                last_q = q
            # ---- Unit B: publish calib status + save profile once good -------
            if persist and (t0 - last_status_pub) >= _STATUS_PUBLISH_INTERVAL:
                last_status_pub = t0
                try:
                    st = _imu_persist.decode_calib_status(
                        _read(bus, addr, _REG_CALIB_STAT))
                    shared_cfg["imu_calib_status"] = st
                    if (not profile_saved and st["gyro"] >= 3
                            and st["accel"] >= 3 and still_count >= _STILL_FRAMES
                            and _plate_solves_confirm(shared_cfg)):
                        if _save_bno055_profile(bus, addr, st):
                            profile_saved = True
                except Exception as e:
                    log.debug("BNO055 calib status/save skipped: %s", e)
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

        # Sleep for the remainder of the poll interval. The rate is live-tunable
        # (imu_poll_hz): the pointing consumers (SkySafari ~4 Hz polls, solves
        # ~2-3 Hz) don't benefit from 20 Hz oversampling, so a lower rate cuts
        # the per-sample Manager-dict IPC on the shared CPU 0 proportionally.
        try:
            hz = float(shared_cfg.get("imu_poll_hz", _POLL_HZ))
        except (TypeError, ValueError):
            hz = _POLL_HZ
        hz = min(50.0, max(1.0, hz))
        sleep_t = (1.0 / hz) - (time.monotonic() - t0)
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
