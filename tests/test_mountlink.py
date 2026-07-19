"""Unit tests for the outbound mount link (mountlink.py).

Covers the NexStar/SynScan angle encoding, the sync command sequence and reply
interpretation against a fake transport, epoch conversion, and the auto-push
decision matrix. No hardware / pyserial needed.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diofinder import mountlink
from diofinder.mountlink import (
    SynScanLink, MountError, deg_to_nexstar_hex, nexstar_hex_to_deg, should_sync,
)


class FakeTransport:
    """Records writes; returns queued replies (bytes before the '#')."""

    def __init__(self, replies=None):
        self.writes = []
        self._replies = list(replies or [])

    def write(self, data):
        self.writes.append(data)

    def read_reply(self):
        if not self._replies:
            raise MountError("no queued reply")
        return self._replies.pop(0)


# ---- angle encoding -------------------------------------------------------

def test_encode_known_angles():
    assert deg_to_nexstar_hex(0.0) == b"00000000"
    assert deg_to_nexstar_hex(180.0) == b"80000000"
    assert deg_to_nexstar_hex(90.0) == b"40000000"
    # Negative wraps: Dec -90 -> 270 -> 0.75 * 2**32.
    assert deg_to_nexstar_hex(-90.0) == b"C0000000"


def test_encode_decode_roundtrip():
    for ra in (0.0, 10.68, 83.63, 266.4, 359.99):
        got = nexstar_hex_to_deg(deg_to_nexstar_hex(ra))
        assert abs(got - ra) < 1e-4
    for dec in (-89.9, -41.2, 0.0, 22.01, 89.9):
        got = nexstar_hex_to_deg(deg_to_nexstar_hex(dec), signed=True)
        assert abs(got - dec) < 1e-4


def test_decode_nonprecise_field_width():
    # 4-hex 'non-precise' field: 0x8000 = half a revolution = 180 deg.
    assert abs(nexstar_hex_to_deg(b"8000") - 180.0) < 1e-3


# ---- sync sequence & reply ------------------------------------------------

def test_sync_sends_precise_command_j2000():
    # epoch=j2000 -> no precession, so the wire bytes are exact.
    t = FakeTransport(replies=[b""])          # '#' success -> empty body
    link = SynScanLink(t, epoch="j2000")
    ok, detail = link.sync(180.0, 45.0)
    assert ok is True
    assert t.writes == [b"s80000000,20000000"]   # 180deg RA, 45deg Dec
    assert detail["sent_ra_deg"] == 180.0
    assert detail["sent_dec_deg"] == 45.0
    assert detail["epoch"] == "j2000"


def test_sync_rejected_reply():
    t = FakeTransport(replies=[b"X"])         # anything but bare '#' -> reject
    ok, detail = SynScanLink(t, epoch="j2000").sync(10.0, 20.0)
    assert ok is False
    assert detail["reply"] == "X"


def test_sync_applies_precession_when_jnow():
    # Fixed t (Julian centuries) so the conversion is deterministic.
    t_now = 0.265                              # ~mid-2026
    fake = FakeTransport(replies=[b""])
    link = SynScanLink(fake, epoch="jnow", now_fn=lambda: t_now)
    link.sync(180.0, 45.0)
    # The sent bytes must differ from the raw j2000 encoding (precession moved
    # the point by ~15-20').
    assert fake.writes[0] != b"s80000000,20000000"
    assert fake.writes[0].startswith(b"s")


# ---- version / ping / get_radec ------------------------------------------

def test_ping_ok_and_fail():
    assert SynScanLink(FakeTransport(replies=[b"x"])).ping() is True
    assert SynScanLink(FakeTransport(replies=[b"y"])).ping() is False
    assert SynScanLink(FakeTransport()).ping() is False   # no reply -> False


def test_version_two_byte():
    assert SynScanLink(FakeTransport(replies=[bytes([4, 14])])).version() == "4.14"


def test_get_radec_roundtrips_through_j2000():
    # Encode a known pointing, feed it back as an 'e' reply, expect it back.
    ra, dec = 83.63, 22.01
    body = deg_to_nexstar_hex(ra) + b"," + deg_to_nexstar_hex(dec)
    link = SynScanLink(FakeTransport(replies=[body]), epoch="j2000")
    got_ra, got_dec = link.get_radec()
    assert abs(got_ra - ra) < 1e-3
    assert abs(got_dec - dec) < 1e-3


# ---- auto-push decision matrix -------------------------------------------

_GATES = {"max_age_s": 3.0, "min_matches": 8, "settle_s": 3.0,
          "deadband_arcmin": 1.0, "min_interval_s": 15.0}


def _sol(**kw):
    base = {"solved": True, "matches": 12, "ra_deg": 100.0, "dec_deg": 20.0,
            "epoch_monotonic": 1000.0}
    base.update(kw)
    return base


def test_should_sync_all_clear():
    do, why = should_sync(_sol(), now=1001.0, last_sync=None, gates=_GATES,
                          moving=False)
    assert do is True and why == "sync"


def test_should_not_sync_unsolved():
    do, _ = should_sync(_sol(solved=False), 1001.0, None, _GATES, False)
    assert do is False


def test_should_not_sync_while_moving():
    do, why = should_sync(_sol(), 1001.0, None, _GATES, moving=True)
    assert do is False and why == "slewing"


def test_should_not_sync_stale():
    do, _ = should_sync(_sol(epoch_monotonic=990.0), 1001.0, None, _GATES, False)
    assert do is False


def test_should_not_sync_weak():
    do, _ = should_sync(_sol(matches=4), 1001.0, None, _GATES, False)
    assert do is False


def test_should_not_sync_rate_limited():
    last = {"ra": 5.0, "dec": 5.0, "t": 995.0}   # 6s ago < 15s
    do, why = should_sync(_sol(), 1001.0, last, _GATES, False)
    assert do is False and why == "rate-limited"


def test_should_not_sync_in_deadband():
    # Same spot as last sync, but past the rate-limit window.
    last = {"ra": 100.0, "dec": 20.0, "t": 900.0}
    do, why = should_sync(_sol(), 1001.0, last, _GATES, False)
    assert do is False and why == "in deadband"


def test_should_sync_when_moved_far_enough():
    last = {"ra": 90.0, "dec": 20.0, "t": 900.0}   # ~9deg away, old enough
    do, why = should_sync(_sol(), 1001.0, last, _GATES, False)
    assert do is True and why == "sync"
