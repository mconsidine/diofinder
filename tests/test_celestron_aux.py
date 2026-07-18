#!/usr/bin/env python3
"""
Unit tests for the Celestron AUX protocol layer (diofinder/celestron_aux.py)
and its comms_proc wiring — framing, checksums, the SkyPortal handshake
dispatcher, 24-bit encoder encoding, and the RA/Dec -> alt/az conversion.

The frame-level expectations are taken verbatim from a captured
SkyPortal <-> NexStar Evolution session (jochym/nexstar-evo analysis dumps),
so a regression here means we no longer answer the way a real mount does.

Runnable WITHOUT picamera2 or star_detect:
    python3 -m unittest tests.test_celestron_aux -v
or directly:
    python3 tests/test_celestron_aux.py
"""
import math
import os
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "star_detect" not in sys.modules:
    _stub = types.ModuleType("star_detect")
    _stub.detect_stars = lambda *a, **k: []
    _stub.set_num_threads = lambda n: None
    sys.modules["star_detect"] = _stub

from diofinder import celestron_aux as ca
from diofinder import precession


def hx(s):
    return bytes.fromhex(s.replace(" ", ""))


# App -> mount startup sequence exactly as captured from SkyPortal connecting
# to a real NexStar Evolution (order preserved; position polls elided).
CAPTURED_STARTUP = [
    "3b 03 20 10 fe cf",           # GET_VER          (AZM)
    "3b 03 20 10 05 c8",           # GET_MODEL        (AZM)
    "3b 04 20 10 24 00 a8",        # MOVE_POS rate 0  (AZM)
    "3b 04 20 11 24 00 a7",        # MOVE_POS rate 0  (ALT)
    "3b 03 20 10 40 8d",           # GET_POS_BACKLASH (AZM)
    "3b 03 20 11 40 8c",           # GET_POS_BACKLASH (ALT)
    "3b 03 20 10 fc d1",           # GET_APPROACH     (AZM)
    "3b 03 20 11 fc d0",           # GET_APPROACH     (ALT)
    "3b 03 20 10 21 ac",           # GET_MAX_SLEW_RATE(AZM)
    "3b 03 20 10 23 aa",           # max-rate flag    (AZM)
    "3b 03 20 10 47 86",           # 0x47 probe       (AZM)
    "3b 03 20 11 47 85",           # 0x47 probe       (ALT)
    "3b 04 20 bf 10 02 0b",        # lights get       (LIGHTS)
    "3b 03 20 b7 10 16",           # charge-port mode (CHARGE)
    "3b 03 20 b6 18 0f",           # battery cutoff   (BATT)
    "3b 03 20 10 01 cc",           # GET_POSITION     (AZM)
    "3b 03 20 11 01 cb",           # GET_POSITION     (ALT)
    "3b 06 20 10 06 00 2e 4f 47",  # SET_POS_GUIDERATE(AZM) — tracking
    "3b 06 20 11 06 00 00 83 40",  # SET_POS_GUIDERATE(ALT)
    "3b 06 20 10 3a 7f ff ff 13",  # SET_CORDWRAP_POS (AZM)
    "3b 03 20 10 38 95",           # ENABLE_CORDWRAP  (AZM)
]


class TestFraming(unittest.TestCase):
    def test_build_frame_matches_captured_requests(self):
        self.assertEqual(ca.build_frame(0x20, 0x10, 0xfe),
                         hx("3b 03 20 10 fe cf"))
        self.assertEqual(ca.build_frame(0x20, 0x10, 0x3a, b"\x7f\xff\xff"),
                         hx("3b 06 20 10 3a 7f ff ff 13"))

    def test_build_frame_matches_captured_replies(self):
        self.assertEqual(
            ca.build_frame(0x10, 0x20, 0xfe, bytes([0x07, 0x0a, 0x10, 0x0d])),
            hx("3b 07 10 20 fe 07 0a 10 0d 9d"))
        self.assertEqual(ca.build_frame(0x10, 0x20, 0x05, b"\x16\x87"),
                         hx("3b 05 10 20 05 16 87 29"))

    def test_checksum_of_every_captured_frame(self):
        for s in CAPTURED_STARTUP:
            raw = hx(s)
            self.assertEqual(ca.checksum(raw[1:-1]), raw[-1], s)

    def test_parser_roundtrip_in_small_chunks_with_junk_prefix(self):
        stream = b"\x00junk" + b"".join(hx(s) for s in CAPTURED_STARTUP)
        parser = ca.FrameParser()
        frames = []
        for i in range(0, len(stream), 5):
            frames += parser.feed(stream[i:i + 5])
        self.assertEqual(len(frames), len(CAPTURED_STARTUP))
        self.assertEqual(frames[0], ca.AuxFrame(0x20, 0x10, 0xfe, b""))
        self.assertEqual(frames[-1], ca.AuxFrame(0x20, 0x10, 0x38, b""))

    def test_parser_handles_preamble_byte_inside_data(self):
        # The captured battery-status reply carries 0x3b IN its data.
        frame = ca.build_frame(0xb6, 0x20, 0x10,
                               bytes([0x02, 0x02, 0x00, 0xa0, 0x01, 0x3b]))
        parser = ca.FrameParser()
        out = parser.feed(frame + hx("3b 03 20 10 fe cf"))
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].data[-1], 0x3b)

    def test_parser_resyncs_after_corruption(self):
        good = hx("3b 03 20 10 fe cf")
        corrupted = good[:-1] + b"\x00"        # bad checksum
        parser = ca.FrameParser()
        out = parser.feed(corrupted + good)
        self.assertEqual(out, [ca.AuxFrame(0x20, 0x10, 0xfe, b"")])


class TestPositionEncoding(unittest.TestCase):
    def test_known_values(self):
        self.assertEqual(ca.deg_to_pos24(0.0), b"\x00\x00\x00")
        self.assertEqual(ca.deg_to_pos24(90.0), b"\x40\x00\x00")
        self.assertEqual(ca.deg_to_pos24(180.0), b"\x80\x00\x00")

    def test_roundtrip_and_wrap(self):
        for deg in (0.0, 12.34, 179.99, -10.0, -179.9, 359.5):
            got = ca.pos24_to_deg(ca.deg_to_pos24(deg))
            want = ((deg + 180.0) % 360.0) - 180.0
            self.assertAlmostEqual(got, want, delta=360.0 / ca.TWO_24 + 1e-9)

    def test_captured_position_decodes(self):
        # Real ALT reply "ff ff ac" = just below zero altitude.
        self.assertAlmostEqual(ca.pos24_to_deg(hx("ff ff ac")),
                               -0.0018, delta=1e-3)


class TestAltAz(unittest.TestCase):
    LAT, LON = 44.5, -73.2

    def test_polaris_sits_at_the_pole(self):
        alt, az = ca.radec_to_altaz(37.95, 89.264, self.LAT, self.LON,
                                    1752800000.0)
        self.assertAlmostEqual(alt, self.LAT, delta=1.2)
        self.assertTrue(az < 2.5 or az > 357.5, az)

    def test_meridian_transit(self):
        t = 1752800000.0
        ra = (ca.gmst_deg(t) + self.LON) % 360.0    # hour angle = 0
        alt, az = ca.radec_to_altaz(ra, 20.0, 40.0, self.LON, t)
        self.assertAlmostEqual(alt, 70.0, delta=1e-6)
        self.assertAlmostEqual(az, 180.0, delta=1e-6)

    def test_object_east_of_meridian_bears_east(self):
        t = 1752800000.0
        ra = (ca.gmst_deg(t) + self.LON + 30.0) % 360.0   # 2h east
        _alt, az = ca.radec_to_altaz(ra, 20.0, 40.0, self.LON, t)
        self.assertTrue(90.0 < az < 180.0, az)


class TestDispatcher(unittest.TestCase):
    def setUp(self):
        self.altaz = (45.0, 90.0)
        self.d = ca.AuxDispatcher(lambda: self.altaz)

    def _one(self, req_hex):
        frames = ca.FrameParser().feed(hx(req_hex))
        self.assertEqual(len(frames), 1)
        replies = self.d.handle(frames[0])
        self.assertLessEqual(len(replies), 1)
        return replies[0] if replies else None

    def test_startup_sequence_gets_valid_replies(self):
        """Every captured startup command must draw exactly one reply whose
        frame is checksum-valid, addressed back to the app from the queried
        device."""
        for s in CAPTURED_STARTUP:
            req = ca.FrameParser().feed(hx(s))[0]
            replies = self.d.handle(req)
            self.assertEqual(len(replies), 1, s)
            raw = replies[0]
            self.assertEqual(raw[0], 0x3b)
            self.assertEqual(ca.checksum(raw[1:-1]), raw[-1], s)
            self.assertEqual(raw[2], req.dst, s)      # reply src = queried dev
            self.assertEqual(raw[3], ca.DEV_APP, s)

    def test_exact_static_replies_match_the_capture(self):
        self.assertEqual(self._one("3b 03 20 10 fe cf"),
                         hx("3b 07 10 20 fe 07 0a 10 0d 9d"))
        self.assertEqual(self._one("3b 03 20 10 05 c8"),
                         hx("3b 05 10 20 05 16 87 29"))
        self.assertEqual(self._one("3b 03 20 10 21 ac"),
                         hx("3b 07 10 20 21 0f 90 11 94 64"))
        self.assertEqual(self._one("3b 03 20 10 23 aa"),
                         hx("3b 04 10 20 23 01 a8"))
        # The 0x47 probe is answered with command byte 0xf0, like the mount.
        self.assertEqual(self._one("3b 03 20 10 47 86"),
                         hx("3b 04 10 20 f0 47 95"))

    def test_positions_reflect_the_pointing_callback(self):
        azm = self._one("3b 03 20 10 01 cc")
        alt = self._one("3b 03 20 11 01 cb")
        self.assertEqual(azm[5:8], ca.deg_to_pos24(90.0))   # az -> AZM axis
        self.assertEqual(alt[5:8], ca.deg_to_pos24(45.0))   # alt -> ALT axis

    def test_goto_is_acked_and_slew_reports_done(self):
        ack = self._one("3b 06 20 10 02 02 0b 4f " +
                        format(ca.checksum(hx("06 20 10 02 02 0b 4f")), "02x"))
        self.assertEqual(ack[4], ca.MC_GOTO_FAST)
        self.assertEqual(len(ack), 6)                       # empty-payload ack
        self.assertIn(ca.DEV_AZM, self.d.last_goto)
        done = self._one("3b 03 20 10 13 ba")
        self.assertEqual(done[5], 0xff)

    def test_unknown_setter_is_acked_unknown_device_is_silent(self):
        ack = self._one("3b 04 20 11 fd 01 " +
                        format(ca.checksum(hx("04 20 11 fd 01")), "02x"))
        self.assertEqual(ack[4], ca.MC_SET_APPROACH)
        gps = ca.build_frame(0x20, ca.DEV_GPS, ca.DEV_GET_VER)
        self.assertEqual(self.d.handle(ca.FrameParser().feed(gps)[0]), [])

    def test_evolution_peripherals_answer(self):
        batt = self._one("3b 03 20 b6 10 17")
        self.assertEqual(batt[5:11], bytes([0x02, 0x02, 0x00, 0xa0, 0x01, 0x3b]))
        cutoff = self._one("3b 03 20 b6 18 0f")
        self.assertEqual(cutoff[5:7], b"\x07\xd0")
        lights = self._one("3b 04 20 bf 10 02 0b")
        self.assertEqual(lights[5], 0x37)
        charge = self._one("3b 03 20 b7 10 16")
        self.assertEqual(charge[5], 0x00)


class TestCommsWiring(unittest.TestCase):
    def test_report_altaz_uses_site_and_precession(self):
        from diofinder.comms_proc import _report_altaz

        class _Cfg:
            latitude_deg = 44.5
            longitude_deg = -73.2

        sol = {"ra_deg": 250.0, "dec_deg": 36.5}
        alt, az = _report_altaz({}, sol, _Cfg())     # empty scfg: no IMU path
        ra_now, dec_now = precession.j2000_to_jnow(250.0, 36.5)
        want_alt, want_az = ca.radec_to_altaz(
            ra_now, dec_now, 44.5, -73.2, time.time())
        self.assertAlmostEqual(alt, want_alt, delta=0.05)
        self.assertAlmostEqual(az % 360.0, want_az % 360.0, delta=0.05)


if __name__ == "__main__":
    unittest.main(verbosity=2)
