"""Unit tests for the held :CM# sync decision (_align_promote).

A sync must survive a marginal sky: the solver holds a pending align request
across frames and only fails it when the hold window expires, not on the first
frame that doesn't solve (the pre-fix behaviour, which failed the sync on a
single NoMatch frame — see the align-hold fix). These tests pin the
promote/supersede/expire transitions.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diofinder.solver_proc import _align_promote

WINDOW = 13.0


class _Req:
    """Stand-in for AlignRequest — _align_promote treats it opaquely."""
    def __init__(self, tag):
        self.tag = tag

    def __repr__(self):
        return f"_Req({self.tag})"


def test_new_request_promotes_and_sets_deadline():
    r = _Req("a")
    pending, deadline, fails = _align_promote(None, 0.0, r, now=100.0,
                                              window_s=WINDOW)
    assert pending is r
    assert deadline == 100.0 + WINDOW
    assert fails == []


def test_bad_frame_holds_pending_no_reply():
    # No new request, pending still inside its window -> held, nothing emitted.
    r = _Req("a")
    pending, deadline, fails = _align_promote(r, 113.0, None, now=105.0,
                                              window_s=WINDOW)
    assert pending is r
    assert deadline == 113.0
    assert fails == []


def test_expiry_fails_and_clears():
    r = _Req("a")
    pending, deadline, fails = _align_promote(r, 113.0, None, now=113.5,
                                              window_s=WINDOW)
    assert pending is None
    assert len(fails) == 1
    req, msg = fails[0]
    assert req is r
    assert "window" in msg


def test_new_request_supersedes_in_flight():
    old = _Req("old")
    new = _Req("new")
    pending, deadline, fails = _align_promote(old, 113.0, new, now=101.0,
                                              window_s=WINDOW)
    assert pending is new
    assert deadline == 101.0 + WINDOW      # fresh window for the new sync
    assert len(fails) == 1
    req, msg = fails[0]
    assert req is old
    assert "superseded" in msg


def test_new_request_not_immediately_expired():
    # A freshly promoted request must never be failed on the same call, even if
    # the caller passed a stale deadline in.
    new = _Req("new")
    pending, deadline, fails = _align_promote(None, 0.0, new, now=1000.0,
                                              window_s=WINDOW)
    assert pending is new
    assert fails == []
