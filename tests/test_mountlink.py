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
    track_last_motion,
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


# ---- settle gate (settle_s) ----------------------------------------------
# The solve must come from at least settle_s (3 s) after the scope was last
# seen moving, so a sync never uses a frame from the tail of a slew.

def test_settle_blocks_solve_too_soon_after_motion():
    # Motion last seen at 998.5; solve published at 1000.0 = only 1.5 s later.
    do, why = should_sync(_sol(), 1001.0, None, _GATES, False,
                          last_motion_t=998.5)
    assert do is False and why.startswith("settling")


def test_settle_passes_once_solve_is_late_enough():
    # Motion last seen at 996.5; solve at 1000.0 = 3.5 s later.
    do, why = should_sync(_sol(), 1001.0, None, _GATES, False,
                          last_motion_t=996.5)
    assert do is True and why == "sync"


def test_settle_judges_the_solve_time_not_the_clock():
    # Plenty of wall time has passed since motion (now - 997 = 3.9 s), but the
    # newest solve (1000.0) is from only 3.0 - 0.1 s after it: still blocked,
    # because that frame may hold the end of the slew.
    do, why = should_sync(_sol(), 1000.9, None, _GATES, False,
                          last_motion_t=997.1)
    assert do is False and why.startswith("settling")


def test_settle_zero_disables_gate():
    gates = dict(_GATES, settle_s=0.0)
    do, why = should_sync(_sol(), 1001.0, None, gates, False,
                          last_motion_t=1000.5)
    assert do is True and why == "sync"


def test_settle_skipped_without_imu_signal():
    # last_motion_t None (no IMU): the settle gate can't be measured, so it
    # never blocks; the other gates still apply.
    do, why = should_sync(_sol(), 1001.0, None, _GATES, None,
                          last_motion_t=None)
    assert do is True and why == "sync"


def test_settle_default_kwarg_keeps_old_call_shape():
    do, why = should_sync(_sol(), 1001.0, None, _GATES, False)
    assert do is True and why == "sync"


def test_track_last_motion_moving_stamps_now():
    assert track_last_motion(None, True, 50.0) == 50.0
    assert track_last_motion(10.0, True, 50.0) == 50.0


def test_track_last_motion_still_keeps_reference():
    assert track_last_motion(10.0, False, 50.0) == 10.0


def test_track_last_motion_first_still_reading_starts_settle():
    # Startup / IMU just came back: don't assume it was already settled.
    assert track_last_motion(None, False, 50.0) == 50.0


def test_track_last_motion_no_imu_clears():
    assert track_last_motion(10.0, None, 50.0) is None


def test_settle_end_to_end_after_a_slew():
    # Loop cadence 2 s; slewing until t=100, still from t=102.
    lm = None
    for t, mv in [(96.0, True), (98.0, True), (100.0, True), (102.0, False)]:
        lm = track_last_motion(lm, mv, t)
    assert lm == 100.0
    # Solve published at 101.5 (tail of the slew): blocked.
    assert should_sync(_sol(epoch_monotonic=101.5), 102.0, None, _GATES,
                       False, last_motion_t=lm)[0] is False
    # Solve published at 103.2 = 3.2 s after the last motion: syncs.
    assert should_sync(_sol(epoch_monotonic=103.2), 104.0, None, _GATES,
                       False, last_motion_t=lm)[0] is True


def test_comms_imu_is_moving_reports_no_signal_as_none():
    # No IMU -> None (not False), so the settle gate knows it has nothing to
    # measure from; None stays falsy for the "slewing" gate.
    from diofinder import comms_proc
    assert comms_proc._imu_is_moving({}) is None
    assert comms_proc._imu_is_moving({"imu_available": False}) is None


def test_comms_mount_status_exposes_auto_reason():
    from diofinder import comms_proc

    class _Cfg:
        pass
    st = comms_proc._MountManager().status(_Cfg(), {})
    assert "auto_reason" in st and st["auto_reason"] is None
