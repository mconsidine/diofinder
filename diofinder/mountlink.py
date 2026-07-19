"""Outbound mount link — push the finder's solved position to a GoTo mount.

diofinder is a plate-solving finder; after a confident solve it knows exactly
where the scope points. This module lets it act as an **alignment source** for a
mount: it sends a *sync* (never a slew) so the mount's own pointing model is
corrected. The finder serves SkySafari over Wi-Fi and drives the mount over the
GPIO serial line at the same time (see docs/onstep-design.md).

**v1 dialect: SynScan** (SkyWatcher, e.g. the Virtuoso). The mount's wired
serial port speaks the NexStar-derived SynScan ASCII protocol; the Pi's 3.3 V
GPIO UART reaches it through a MAX232 (TTL -> true RS-232). The architecture is
dialect- and transport-pluggable — the interface a mount link exposes is:

    ping() -> bool                 # liveness handshake
    version() -> str               # for the "Test connection" button
    get_radec() -> (ra, dec)       # what the mount thinks it's pointing at (J2000)
    sync(ra_deg, dec_deg) -> (ok, detail)   # correct the mount's model

so an LX200Link (OnStepX) or AlpacaLink can drop in later behind the same shape.

**Epoch.** The finder solves in J2000/ICRS; SynScan/NexStar mounts use JNow.
The conversion happens here, once per sync, via ``diofinder.precession`` — the
same helper the LX200 comms boundary uses. Gated by the mount's configured
epoch (``jnow`` default, ``j2000`` = send raw).

**Safety.** Sync only — this module has no slew/GoTo command. Nothing here can
move the mount.
"""

from __future__ import annotations

import logging

from diofinder import precession

log = logging.getLogger("diofinder.comms")

# A full revolution in the NexStar "precise" (32-bit) angle encoding.
_REV32 = 4294967296  # 2**32


class MountError(Exception):
    """Any mount-link I/O or protocol failure (raised by transports/dialects)."""


# --------------------------------------------------------------------------- #
# NexStar / SynScan angle encoding (pure, unit-tested)
# --------------------------------------------------------------------------- #

def deg_to_nexstar_hex(angle_deg: float) -> bytes:
    """Degrees -> 8-hex-digit NexStar 'precise' fraction of a revolution.

    A full circle maps onto 2**32. Negative angles wrap (e.g. Dec -90 -> 270 ->
    0xC0000000), matching how SynScan encodes southern declinations.
    """
    frac = int(round((angle_deg % 360.0) / 360.0 * _REV32)) & 0xFFFFFFFF
    return b"%08X" % frac


def nexstar_hex_to_deg(h: bytes, signed: bool = False) -> float:
    """NexStar hex fraction -> degrees. Handles precise (8 hex) and non-precise
    (4 hex) fields by their digit count.

    ``signed=True`` folds the upper half onto negatives (for Dec: 270 -> -90),
    so a round-trip of a declination returns the original signed value.
    """
    val = int(h, 16)
    modulus = 1 << (4 * len(h))
    deg = val / float(modulus) * 360.0
    if signed and deg > 180.0:
        deg -= 360.0
    return deg


# --------------------------------------------------------------------------- #
# Serial transport (pyserial; imported lazily so tests need no hardware)
# --------------------------------------------------------------------------- #

class SerialTransport:
    """A framed serial line: write a command, read the reply up to ``#``.

    NexStar/SynScan replies are all terminated by ``#``; ``read_reply`` returns
    the bytes *before* it. pyserial is imported on ``open`` so importing this
    module (and unit-testing the dialects with a fake transport) needs nothing
    installed.
    """

    def __init__(self, port: str, baud: int = 9600, timeout: float = 2.0):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._ser = None

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def open(self):
        try:
            import serial  # pyserial
        except ImportError as e:  # pragma: no cover - environment dependent
            raise MountError(f"pyserial not installed: {e}") from e
        try:
            self._ser = serial.Serial(
                self.port, self.baud, timeout=self.timeout,
                write_timeout=self.timeout)
        except Exception as e:
            self._ser = None
            raise MountError(f"cannot open {self.port} @ {self.baud}: {e}") from e

    def close(self):
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def write(self, data: bytes):
        if not self.is_open:
            raise MountError("serial port not open")
        try:
            self._ser.reset_input_buffer()  # drop any stale reply bytes
            self._ser.write(data)
        except Exception as e:
            raise MountError(f"serial write failed: {e}") from e

    def read_reply(self) -> bytes:
        """Read up to and including the ``#`` terminator; return bytes before it.

        Raises ``MountError`` on timeout (empty/partial read with no ``#``),
        which the caller treats as a dropped/unresponsive link.
        """
        if not self.is_open:
            raise MountError("serial port not open")
        try:
            raw = self._ser.read_until(b"#")
        except Exception as e:
            raise MountError(f"serial read failed: {e}") from e
        if not raw.endswith(b"#"):
            raise MountError(f"no reply (got {raw!r})")
        return raw[:-1]


# --------------------------------------------------------------------------- #
# SynScan / NexStar dialect
# --------------------------------------------------------------------------- #

class SynScanLink:
    """NexStar-derived SynScan ASCII dialect (SkyWatcher Virtuoso, AZ-GTi, ...).

    ``transport`` is any object exposing ``write(bytes)`` and
    ``read_reply() -> bytes`` (the serial transport, or a fake in tests).
    ``epoch`` is the *mount's* epoch (``jnow``/``j2000``); the finder's own
    coordinates are always J2000 and converted here.
    """

    PROTOCOL = "synscan"

    def __init__(self, transport, epoch: str = "jnow", now_fn=None):
        self._t = transport
        self.epoch = (epoch or "jnow").lower()
        # Injectable clock (Julian centuries since J2000) so the epoch
        # conversion is testable without a real clock.
        self._now_fn = now_fn or precession.julian_centuries_now

    def _command(self, payload: bytes) -> bytes:
        self._t.write(payload)
        return self._t.read_reply()

    # -- epoch helpers -----------------------------------------------------
    def _to_mount(self, ra_j2000: float, dec_j2000: float):
        if self.epoch == "j2000":
            return ra_j2000 % 360.0, dec_j2000
        return precession.j2000_to_jnow(ra_j2000, dec_j2000, self._now_fn())

    def _to_j2000(self, ra_mount: float, dec_mount: float):
        if self.epoch == "j2000":
            return ra_mount % 360.0, dec_mount
        return precession.jnow_to_j2000(ra_mount, dec_mount, self._now_fn())

    # -- protocol ----------------------------------------------------------
    def ping(self) -> bool:
        """Echo handshake (``Kx`` -> ``x#``): is anything alive on the line?"""
        try:
            return self._command(b"Kx") == b"x"
        except MountError:
            return False

    def version(self) -> str:
        """Firmware version (``V``) as a printable string, best-effort."""
        body = self._command(b"V")
        if len(body) == 2:                    # classic NexStar: 2 raw bytes
            return f"{body[0]}.{body[1]}"
        if len(body) == 6:                    # SynScan: 6 hex nibbles maj.min.pat
            try:
                return ".".join(str(int(body[i:i + 2], 16)) for i in (0, 2, 4))
            except ValueError:
                pass
        return body.decode("ascii", "replace")

    def get_radec(self):
        """Return the mount's current pointing as **J2000** (ra_deg, dec_deg)."""
        body = self._command(b"e")            # precise get RA/Dec
        try:
            ra_hex, dec_hex = body.split(b",")
        except ValueError as e:
            raise MountError(f"bad RA/Dec reply {body!r}") from e
        ra_m = nexstar_hex_to_deg(ra_hex)
        dec_m = nexstar_hex_to_deg(dec_hex, signed=True)
        return self._to_j2000(ra_m, dec_m)

    def sync(self, ra_deg: float, dec_deg: float):
        """Sync the mount to the given **J2000** position. Never slews.

        Returns ``(ok, detail)`` where detail carries the mount-epoch coords
        actually sent and the raw reply, for logging / the UI.
        """
        ra_m, dec_m = self._to_mount(ra_deg, dec_deg)
        payload = b"s" + deg_to_nexstar_hex(ra_m) + b"," + deg_to_nexstar_hex(dec_m)
        reply = self._command(payload)        # success reply is just '#' -> b''
        ok = reply == b""
        detail = {
            "sent_ra_deg": round(ra_m, 5),
            "sent_dec_deg": round(dec_m, 5),
            "epoch": self.epoch,
            "reply": reply.decode("ascii", "replace"),
        }
        if not ok:
            log.warning("Mount sync rejected: sent %s, reply %r", payload, reply)
        return ok, detail


def make_link(protocol: str, transport, epoch: str = "jnow", now_fn=None):
    """Construct a mount-link dialect by name. Future: ``lx200``/``alpaca``."""
    p = (protocol or "synscan").lower()
    if p == "synscan":
        return SynScanLink(transport, epoch=epoch, now_fn=now_fn)
    raise MountError(f"unknown mount protocol: {protocol!r}")


# --------------------------------------------------------------------------- #
# Auto-push policy (pure, unit-tested) — mirrors _auto_exposure_decision
# --------------------------------------------------------------------------- #

def should_sync(solution, now, last_sync, gates, moving):
    """Decide whether an auto-mode sync should fire this cycle.

    Pure so it is testable without hardware. Returns ``(do_sync, reason)`` —
    ``reason`` is a short string for the status line / logs whether or not it
    fires.

    * ``solution``  — the latest solution dict (``solved``, ``matches``,
      ``ra_deg``, ``dec_deg``, ``epoch_monotonic``).
    * ``now``       — monotonic clock (same domain as ``epoch_monotonic`` and
      ``last_sync['t']``).
    * ``last_sync`` — ``{'ra','dec','t'}`` of the previous accepted sync, or
      ``None``.
    * ``gates``     — dict: ``max_age_s``, ``min_matches``, ``settle_s``,
      ``deadband_arcmin``, ``min_interval_s``.
    * ``moving``    — True if the mount/scope is slewing (IMU rate gate).
    """
    if not solution or not solution.get("solved"):
        return False, "no solution"
    if moving:
        return False, "slewing"

    age = now - solution.get("epoch_monotonic", 0.0)
    if age > gates["max_age_s"]:
        return False, f"stale ({age:.1f}s)"
    if solution.get("matches", 0) < gates["min_matches"]:
        return False, f"weak ({solution.get('matches', 0)} matches)"

    ra = solution.get("ra_deg")
    dec = solution.get("dec_deg")
    if ra is None or dec is None:
        return False, "no coords"

    if last_sync is not None:
        if now - last_sync["t"] < gates["min_interval_s"]:
            return False, "rate-limited"
        sep_arcmin = _angular_sep_arcmin(ra, dec, last_sync["ra"], last_sync["dec"])
        if sep_arcmin < gates["deadband_arcmin"]:
            return False, "in deadband"

    return True, "sync"


def _angular_sep_arcmin(ra1, dec1, ra2, dec2):
    """Great-circle separation in arcminutes (small-angle-safe haversine)."""
    import math
    r1, d1 = math.radians(ra1), math.radians(dec1)
    r2, d2 = math.radians(ra2), math.radians(dec2)
    dd = d2 - d1
    dr = r2 - r1
    a = (math.sin(dd / 2) ** 2
         + math.cos(d1) * math.cos(d2) * math.sin(dr / 2) ** 2)
    return math.degrees(2 * math.asin(min(1.0, math.sqrt(a)))) * 60.0
