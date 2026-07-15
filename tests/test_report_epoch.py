"""Reporting-epoch boundary (_report_radec): diofinder solves in J2000 but
reports JNow to SkySafari by default; report_epoch=j2000 is the passthrough
kill switch. (comms_proc imports no native wheels, so this runs headless.)"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from diofinder import comms_proc as c
from diofinder import precession as p


_SOL = {"ra_deg": 45.0, "dec_deg": 10.0}


def test_jnow_default_precesses_j2000_to_date():
    ra, dec = c._report_radec({"report_epoch": "jnow"}, dict(_SOL))
    exp = p.j2000_to_jnow(45.0, 10.0)
    assert abs(ra - exp[0]) < 1e-9 and abs(dec - exp[1]) < 1e-9
    # And it actually moved (this is the ~15-22' fix, not a no-op).
    assert abs(ra - 45.0) > 0.05


def test_missing_key_defaults_to_jnow():
    ra, _ = c._report_radec({}, dict(_SOL))
    assert abs(ra - p.j2000_to_jnow(45.0, 10.0)[0]) < 1e-9


def test_j2000_is_passthrough_kill_switch():
    ra, dec = c._report_radec({"report_epoch": "j2000"}, dict(_SOL))
    assert ra == 45.0 and dec == 10.0


def test_uses_last_solved_when_no_imu_prediction():
    # No imu_available in scfg -> _imu_predict_smoothed returns None -> the
    # sol position is what gets reported (converted).
    ra, dec = c._report_radec({"report_epoch": "j2000"}, {"ra_deg": 200.0,
                                                          "dec_deg": -30.0})
    assert ra == 200.0 and dec == -30.0
