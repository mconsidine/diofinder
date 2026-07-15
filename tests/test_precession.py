"""IAU 1976 precession boundary (J2000 <-> JNow). Pure math, no numpy."""
import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from diofinder import precession as p


def _sep_arcmin(ra1, dec1, ra2, dec2):
    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    c = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2)
    return math.degrees(math.acos(min(1.0, max(-1.0, c)))) * 60.0


def test_round_trip_is_identity():
    t = 0.265
    for ra, dec in [(10, 20), (180, -45), (350, 70), (83.6, 22.0), (0, 89)]:
        ra2, dec2 = p.precess(ra, dec, t, inverse=False)
        ra3, dec3 = p.precess(ra2, dec2, t, inverse=True)
        assert _sep_arcmin(ra, dec, ra3, dec3) < 1e-3, (ra, dec)


def test_shift_magnitude_is_precession_scale():
    # J2000 -> ~2026 (0.265 cy) is a ~10-25' shift depending on sky position —
    # the position dependence is the whole reason a constant boresight offset
    # can't compensate for it.
    t = 0.265
    for ra, dec in [(10, 20), (180, -45), (83.6, 22.0)]:
        ra2, dec2 = p.precess(ra, dec, t, inverse=False)
        assert 5.0 < _sep_arcmin(ra, dec, ra2, dec2) < 30.0, (ra, dec)


def test_zero_time_is_no_op():
    for ra, dec in [(123.4, -12.3), (0.0, 0.0)]:
        ra2, dec2 = p.precess(ra, dec, 0.0, inverse=False)
        assert _sep_arcmin(ra, dec, ra2, dec2) < 1e-6


def test_forward_inverse_disagree_by_direction():
    # Forward and inverse move a point in opposite directions.
    t = 0.265
    ra, dec = 45.0, 10.0
    f = p.precess(ra, dec, t, inverse=False)
    b = p.precess(ra, dec, t, inverse=True)
    # Both shift by ~one precession step, but to opposite sides -> ~2x apart.
    assert _sep_arcmin(*f, *b) > 1.5 * _sep_arcmin(ra, dec, *f)


def test_helpers_default_to_now():
    # j2000_to_jnow / jnow_to_j2000 with no t use the current epoch and invert.
    ra, dec = 200.0, 30.0
    jn = p.j2000_to_jnow(ra, dec)
    back = p.jnow_to_j2000(*jn)
    assert _sep_arcmin(ra, dec, *back) < 1e-3
    # And "now" is a plausible fraction of a century past J2000.
    assert 0.2 < p.julian_centuries_now() < 0.6
