"""_liveview_miss_reason (v0.11.50): the live-view "frame unavailable" message
now explains WHY instead of always guessing "camera not running". The frame
path is solver-serviced, so a busy/behind solver or a long exposure looks
identical to a dead camera from the browser — the solution-epoch age separates
them. This pins the decision table.

(Distinct from diofinder.frame_health, which assesses a frame's exposure — this
is about frame AVAILABILITY.)
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "webui"))

from app import _liveview_miss_reason


def test_daemon_down_never_blames_camera():
    r = _liveview_miss_reason(None, ok=False)
    assert "daemon" in r.lower()
    assert "camera" not in r.lower()


def test_never_published_reads_as_startup():
    r = _liveview_miss_reason(None, ok=True)
    assert "starting up" in r


def test_fresh_solution_is_transient_not_camera_down():
    # Solves are current -> the miss was momentary frame-serving latency.
    r = _liveview_miss_reason(1.0, ok=True)
    assert "live" in r
    assert "stalled" not in r


def test_behind_solver_exonerates_the_camera():
    # 5-15s stale: the low-horizon "solver grinding" case must say the camera
    # is fine (the whole point of the fix) and report the age.
    r = _liveview_miss_reason(8.0, ok=True)
    assert "camera is fine" in r
    assert "8s" in r


def test_very_stale_flags_a_real_stall():
    r = _liveview_miss_reason(40.0, ok=True)
    assert "40s" in r
    assert "stalled" in r or "stopped delivering" in r


def test_thresholds_are_ordered_and_distinct():
    msgs = [_liveview_miss_reason(a, ok=True) for a in (1.0, 8.0, 40.0)]
    assert len(set(msgs)) == 3
