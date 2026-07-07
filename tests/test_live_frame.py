"""webui _live_frame: display-SHM fast path must survive daemon restarts.

The daemon unlinks and RECREATES /dev/shm/diofinder_display on every restart
(ExecStartPre rm + display_shm.create). The webui's long-lived DisplayReader
kept its mapping to the OLD segment: `available` stayed True and read() kept
returning the last frame ever written there — with a valid, never-advancing
seq — so the live view froze on one frame until diofinder-webui itself was
restarted (observed live on v0.11.41 boot). A second, quieter defect: an
attach attempted before the daemon created the segment recorded `hw` with an
unavailable reader and never retried, permanently disabling the fast path.

These tests pin the fixed behavior with a real display segment and stubbed
maint calls — no daemon required. Skipped when Flask (a webui-only dep) is
absent.
"""
import numpy as np
import pytest

pytest.importorskip("flask")

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from webui import app as app_mod  # noqa: E402
from diofinder import display_shm  # noqa: E402

H, W = 12, 20
FALLBACK = ("fallback-sentinel", -1, False)


class _Cfg:
    frame_height = H
    frame_width = W


@pytest.fixture
def patched(monkeypatch):
    """Stub the daemon-facing calls and reset the module reader state."""
    monkeypatch.setattr(app_mod, "_safe_call",
                        lambda *a, **k: app_mod.MaintResponse(
                            ok=False, error="stub"))
    monkeypatch.setattr(app_mod, "_daemon_frame", lambda **k: FALLBACK)
    monkeypatch.setattr(app_mod, "_load_cfg_cached", lambda: _Cfg())
    app_mod._display_reader.update(
        r=None, hw=None, ino=None, last_seq=-1, last_new=0.0)
    app_mod._display_keepalive["ts"] = 0.0
    yield
    r = app_mod._display_reader["r"]
    if r is not None:
        r.close()
    app_mod._display_reader.update(
        r=None, hw=None, ino=None, last_seq=-1, last_new=0.0)


def _segment_with_frame(value):
    """Create the display segment and publish one uniform frame into it."""
    shm = display_shm.create(H, W)
    w = display_shm.DisplayWriter(H, W)
    w.write(np.full((H, W), value, dtype=np.uint8))
    w.close()
    return shm


def test_fast_path_serves_published_frame(patched):
    shm = _segment_with_frame(37)
    try:
        frame, seq, synced = app_mod._live_frame()
        assert synced and seq > 0
        assert int(frame[0, 0]) == 37
    finally:
        shm.close()
        shm.unlink()


def test_daemon_restart_reattaches_instead_of_freezing(patched):
    """THE frozen-live-view bug: segment recreated under an attached reader."""
    shm1 = _segment_with_frame(37)
    frame, _seq, _ = app_mod._live_frame()
    assert int(frame[0, 0]) == 37  # attached to segment #1

    # Daemon restart: old segment unlinked, new one created + written.
    shm1.close()
    shm1.unlink()
    shm2 = _segment_with_frame(99)
    try:
        frame, seq, synced = app_mod._live_frame()
        assert synced and int(frame[0, 0]) == 99, \
            "reader must re-attach to the recreated segment, not serve " \
            "the orphaned mapping's frozen frame"
    finally:
        shm2.close()
        shm2.unlink()


def test_failed_attach_is_not_sticky(patched):
    """Webui boots before the daemon: attach must retry once the segment
    appears, not permanently record hw with an unavailable reader."""
    assert app_mod._live_frame() == FALLBACK  # no segment yet
    shm = _segment_with_frame(55)
    try:
        frame, _seq, synced = app_mod._live_frame()
        assert synced and int(frame[0, 0]) == 55
    finally:
        shm.close()
        shm.unlink()


def test_segment_gone_drops_reader_and_falls_back(patched):
    """ExecStartPre removed the file and the daemon hasn't recreated it yet."""
    shm = _segment_with_frame(37)
    frame, _seq, _ = app_mod._live_frame()
    assert int(frame[0, 0]) == 37
    shm.close()
    shm.unlink()
    assert app_mod._live_frame() == FALLBACK
    assert app_mod._display_reader["r"] is None


def test_frozen_seq_past_stale_window_falls_back(patched):
    """Same segment, writer stopped: serve frame_get once the seq is stale."""
    shm = _segment_with_frame(37)
    try:
        frame, _seq, _ = app_mod._live_frame()
        assert int(frame[0, 0]) == 37
        # Within the stale window the cached frame keeps being served.
        assert int(app_mod._live_frame()[0][0, 0]) == 37
        # Age the last seq-advance past the stale threshold.
        app_mod._display_reader["last_new"] -= (
            app_mod._DISPLAY_STALE_S + 1.0)
        assert app_mod._live_frame() == FALLBACK
    finally:
        shm.close()
        shm.unlink()


def test_writer_resuming_restores_fast_path(patched):
    shm = _segment_with_frame(37)
    try:
        app_mod._live_frame()
        app_mod._display_reader["last_new"] -= (
            app_mod._DISPLAY_STALE_S + 1.0)
        assert app_mod._live_frame() == FALLBACK  # stale -> fallback
        w = display_shm.DisplayWriter(H, W)
        w.write(np.full((H, W), 88, dtype=np.uint8))
        w.close()
        frame, _seq, synced = app_mod._live_frame()
        assert synced and int(frame[0, 0]) == 88
    finally:
        shm.close()
        shm.unlink()
