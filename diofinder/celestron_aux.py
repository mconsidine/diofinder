"""Celestron AUX-over-TCP protocol: let SkyPortal treat diofinder as a mount.

Celestron's SkyPortal app (and SkySafari's "Celestron WiFi" scope type) does
NOT speak LX200 over WiFi — it speaks the Celestron AUX *bus* protocol framed
over TCP port 2000. The real SkyPortal WiFi module is a dumb TCP<->serial
bridge onto the mount's internal AUX bus; the app itself is bus device 0x20
and talks directly to the motor controllers. Crucially, the app keeps the
alignment model *in the app*: the "mount" is only ever asked for raw encoder
angles (24-bit fractions of a revolution), and the user's in-app star
alignment maps encoders -> sky.

That makes a plate-solving push-to finder a near-ideal Celestron mount: we
report the solved (IMU-smoothed) pointing converted to topocentric alt/az as
the two "encoder" values. Because the reported angles are *true* alt/az, the
app's alignment converges to (nearly) a pure rotation and pointing is exact
everywhere. GoTo/slew commands are acknowledged as no-ops — correct for a
device that cannot move the telescope.

Wire format (one frame)::

    0x3b | len | src | dst | cmd | data... | checksum

``len`` counts src+dst+cmd+data; ``checksum`` is the two's complement of the
sum of every byte from ``len`` through the last data byte. Replies swap
src/dst and repeat the cmd byte. Verified byte-for-byte against a captured
SkyPortal <-> NexStar Evolution session (jochym/nexstar-evo ``analysis/``
dumps); see docs/skyportal-aux.md for the decoded handshake.

This module is pure stdlib (``math``/``time``) and holds all protocol logic —
framing, the command dispatcher, and the RA/Dec -> alt/az conversion — so it
is unit-testable without sockets or hardware (tests/test_celestron_aux.py).
The TCP server / UDP discovery beacon live in comms_proc.
"""
from __future__ import annotations

import math
import time
from collections import namedtuple

# ---------------------------------------------------------------------------
# Bus device IDs
# ---------------------------------------------------------------------------
DEV_MB     = 0x01   # main board
DEV_HC     = 0x04   # NexStar hand controller
DEV_AZM    = 0x10   # azimuth / RA motor controller
DEV_ALT    = 0x11   # altitude / Dec motor controller
DEV_APP    = 0x20   # the app (SkyPortal / SkySafari / CPWI)
DEV_GPS    = 0xb0
DEV_WIFI   = 0xb5   # the WiFi module itself
DEV_BATT   = 0xb6   # Evolution battery controller
DEV_CHARGE = 0xb7   # Evolution charge port
DEV_LIGHTS = 0xbf   # Evolution tray lights

# ---------------------------------------------------------------------------
# Command IDs (MC_* target the motor controllers)
# ---------------------------------------------------------------------------
MC_GET_POSITION     = 0x01   # -> 24-bit position
MC_GOTO_FAST        = 0x02   # 24-bit target
MC_GET_MODEL        = 0x05   # -> 16-bit model id
MC_SET_POS_GUIDERATE = 0x06  # 24-bit rate (the app drives tracking with this)
MC_SET_NEG_GUIDERATE = 0x07
MC_SLEW_DONE        = 0x13   # -> 0xff done / 0x00 moving
MC_GOTO_SLOW        = 0x17   # 24-bit target (final approach)
MC_GET_MAX_SLEW_RATE = 0x21
MC_MOVE_POS         = 0x24   # 1-byte rate 0-9, 0 = stop
MC_MOVE_NEG         = 0x25
MC_ENABLE_CORDWRAP  = 0x38
MC_DISABLE_CORDWRAP = 0x39
MC_SET_CORDWRAP_POS = 0x3a
MC_GET_POS_BACKLASH = 0x40
MC_GET_APPROACH     = 0xfc
MC_SET_APPROACH     = 0xfd
DEV_GET_VER         = 0xfe

MODEL_EVOLUTION = 0x1687     # what a NexStar Evolution reports for MC_GET_MODEL

# Firmware version the motor controllers report for DEV_GET_VER. This is the
# exact value from the captured real-Evolution session (7.10.4109) — reported
# verbatim so the app enables the same feature set it did against the capture.
FW_VERSION = (0x07, 0x0a, 0x10, 0x0d)

TWO_24 = 1 << 24

AuxFrame = namedtuple("AuxFrame", "src dst cmd data")


def checksum(body: bytes) -> int:
    """Two's-complement checksum over len+src+dst+cmd+data."""
    return (-sum(body)) & 0xFF


def build_frame(src: int, dst: int, cmd: int, data: bytes = b"") -> bytes:
    body = bytes([len(data) + 3, src, dst, cmd]) + data
    return b"\x3b" + body + bytes([checksum(body)])


class FrameParser:
    """Incremental AUX frame parser for a TCP byte stream.

    ``feed()`` returns complete, checksum-valid frames and buffers any tail.
    A checksum failure discards only the bogus preamble byte and rescans, so
    a corrupted frame (or a 0x3b landing mid-garbage) resynchronizes on the
    next real frame. 0x3b *inside* a frame's data is handled by the length
    field, never by scanning (real battery-status replies contain 0x3b).
    """

    MAX_BUFFER = 4096   # AUX frames are tens of bytes; anything more is junk

    def __init__(self):
        self._buf = b""

    def feed(self, data: bytes):
        self._buf += data
        frames = []
        while True:
            start = self._buf.find(b"\x3b")
            if start < 0:
                self._buf = b""
                break
            self._buf = self._buf[start:]
            if len(self._buf) < 2:
                break
            ln = self._buf[1]
            total = 3 + ln           # preamble + len + body(ln) + checksum
            if ln < 3:
                self._buf = self._buf[1:]     # implausible length: resync
                continue
            if len(self._buf) < total:
                if len(self._buf) > self.MAX_BUFFER:
                    self._buf = b""
                break
            raw = self._buf[:total]
            if checksum(raw[1:-1]) != raw[-1]:
                self._buf = self._buf[1:]     # bad checksum: resync
                continue
            self._buf = self._buf[total:]
            frames.append(AuxFrame(raw[2], raw[3], raw[4], raw[5:total - 1]))
        return frames


# ---------------------------------------------------------------------------
# 24-bit encoder encoding
# ---------------------------------------------------------------------------
def deg_to_pos24(deg: float) -> bytes:
    """Degrees -> big-endian 24-bit fraction of a revolution (wraps)."""
    v = int(round(deg / 360.0 * TWO_24)) % TWO_24
    return bytes([(v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF])


def pos24_to_deg(data: bytes) -> float:
    """Big-endian 24-bit fraction of a revolution -> degrees in [-180, 180)."""
    v = (data[0] << 16) | (data[1] << 8) | data[2]
    if v >= TWO_24 // 2:
        v -= TWO_24
    return v / TWO_24 * 360.0


# ---------------------------------------------------------------------------
# RA/Dec (equinox of date) -> topocentric alt/az
# ---------------------------------------------------------------------------
def gmst_deg(unix_t: float) -> float:
    """Greenwich mean sidereal time in degrees (IAU 1982 linear form).

    Accurate to well under a second of time across decades — far below a
    finder's needs (1 s of time = 15" of azimuth at the equator).
    """
    jd = unix_t / 86400.0 + 2440587.5
    return (280.46061837 + 360.98564736629 * (jd - 2451545.0)) % 360.0


def radec_to_altaz(ra_deg: float, dec_deg: float,
                   lat_deg: float, lon_deg: float,
                   unix_t: float) -> tuple:
    """(alt_deg, az_deg) for RA/Dec of date at the given site and instant.

    Azimuth is measured from North, increasing East (the encoder convention a
    real alt-az mount presents after the app's alignment; any constant offset
    or sign is absorbed by the in-app alignment anyway, but time-dependence
    must be physical — which this is). ``lon_deg`` is east-positive.
    """
    lst = gmst_deg(unix_t) + lon_deg
    h = math.radians((lst - ra_deg) % 360.0)
    phi = math.radians(lat_deg)
    dec = math.radians(dec_deg)
    sin_alt = (math.sin(phi) * math.sin(dec)
               + math.cos(phi) * math.cos(dec) * math.cos(h))
    alt = math.degrees(math.asin(max(-1.0, min(1.0, sin_alt))))
    az = math.degrees(math.atan2(
        -math.cos(dec) * math.sin(h),
        math.sin(dec) * math.cos(phi)
        - math.cos(dec) * math.cos(h) * math.sin(phi))) % 360.0
    return alt, az


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
class AuxDispatcher:
    """Answer AUX frames the way a NexStar Evolution does.

    ``get_altaz()`` -> (alt_deg, az_deg) supplies the live pointing; it is
    called once per MC_GET_POSITION. Everything else is static or a no-op
    acknowledge. Frames addressed to devices we do not emulate get NO reply —
    on the real bus an absent device is silent and the app moves on.

    Replies mirror the captured real-mount session exactly, including the
    quirk that the MC answers the app's 0x47 probe with command byte 0xf0
    (data 0x47) — SkyPortal is verified happy with that exchange, so it is
    reproduced rather than "fixed".
    """

    def __init__(self, get_altaz, model: int = MODEL_EVOLUTION,
                 fw_version=FW_VERSION):
        self._get_altaz = get_altaz
        self._model = model
        self._fw = bytes(fw_version)
        self.rx_frames = 0
        self.replies = 0
        self.last_goto = {}          # dev -> target degrees (diagnostics)

    def handle(self, frame: AuxFrame):
        """One inbound frame -> list of raw reply frames (possibly empty)."""
        self.rx_frames += 1
        reply = self._reply_for(frame)
        if reply is None:
            return []
        cmd, data = reply
        self.replies += 1
        return [build_frame(frame.dst, frame.src, cmd, data)]

    def _reply_for(self, fr: AuxFrame):
        cmd = fr.cmd
        if fr.dst in (DEV_AZM, DEV_ALT):
            if cmd == MC_GET_POSITION:
                alt, az = self._get_altaz()
                return cmd, deg_to_pos24(az if fr.dst == DEV_AZM else alt)
            if cmd == DEV_GET_VER:
                return cmd, self._fw
            if cmd == MC_GET_MODEL:
                return cmd, bytes([(self._model >> 8) & 0xFF,
                                   self._model & 0xFF])
            if cmd == MC_SLEW_DONE:
                # We cannot move the scope; report every goto complete at
                # once so the app returns to tracking display immediately.
                return cmd, b"\xff"
            if cmd in (MC_GOTO_FAST, MC_GOTO_SLOW) and len(fr.data) == 3:
                self.last_goto[fr.dst] = pos24_to_deg(fr.data)
                return cmd, b""
            if cmd == MC_GET_MAX_SLEW_RATE:
                return cmd, bytes([0x0f, 0x90, 0x11, 0x94])
            if cmd == 0x23:                       # max-rate flag (captured)
                return cmd, b"\x01"
            if cmd in (MC_GET_POS_BACKLASH, 0x41, MC_GET_APPROACH):
                return cmd, b"\x00"
            if cmd == 0x47:                       # see class docstring
                return 0xf0, b"\x47"
            # Every other command observed from the app is a setter/motion
            # command (guiderates, moves, cordwrap, approach, backlash...):
            # acknowledge with an empty-payload echo, exactly like a real MC.
            return cmd, b""
        if fr.dst == DEV_WIFI:
            if cmd == DEV_GET_VER:
                return cmd, self._fw
            return cmd, b""
        if fr.dst == DEV_BATT:
            if cmd == DEV_GET_VER:
                return cmd, self._fw
            if cmd == 0x10:      # status: flags + millivolts (captured value)
                return cmd, bytes([0x02, 0x02, 0x00, 0xa0, 0x01, 0x3b])
            if cmd == 0x18:      # low-voltage cutoff threshold (2000 mV)
                return cmd, b"\x07\xd0"
            return cmd, b""
        if fr.dst == DEV_CHARGE:
            if cmd == 0x10 and not fr.data:
                return cmd, b"\x00"
            return cmd, b""
        if fr.dst == DEV_LIGHTS:
            if cmd == 0x10 and len(fr.data) == 1:   # get brightness of pot N
                return cmd, b"\x37"
            return cmd, b""
        return None      # device not emulated: stay silent, like a real bus


def beacon_payload(version: str) -> bytes:
    """UDP discovery beacon payload (broadcast to port 55555).

    The real WiFly module broadcasts a status packet on UDP 55555 about once a
    second; SkyPortal/SkySafari auto-detect listens for it to learn the
    scope's IP on an infrastructure network (in AP mode the app just connects
    to 1.2.3.4:2000 and never needs the beacon). The homebrew adapters (g7ltt,
    HBG3) send an identity string instead and are detected fine — reproduce
    that shape.
    """
    return f"diofinder-AMW007-9.0.0.0,{version},SkyPortal-AUX".encode("ascii")
