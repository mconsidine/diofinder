"""Debug bundle: built in the background to a file, downloaded as a plain file.

The old route built the whole zip in web UI memory on CPU 0 and returned it
as one big response (fragile on phones). Now /debug/start runs _debug_build
in a thread, /api/debug reports progress, /debug/download/<name> serves the
finished file, and only the newest 10 bundles are kept."""
import time
import types

import pytest

A = pytest.importorskip("webui.app")


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_DEBUG_DIR", tmp_path)
    monkeypatch.setattr(A, "_debug_job", {"state": "idle", "name": None,
                                           "error": None, "started": None})
    calls = []

    def fake_build(zip_name, n_frames):
        calls.append((zip_name, n_frames))
        p = tmp_path / zip_name
        p.write_bytes(b"PK fake bundle")
        return p
    monkeypatch.setattr(A, "_debug_build", fake_build)
    return A.app.test_client(), tmp_path, calls


def _wait_done(c, timeout=5.0):
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        job = c.get("/api/debug").get_json()["result"]
        if job["state"] in ("done", "error"):
            return job
        time.sleep(0.02)
    raise AssertionError("bundle never finished")


def test_start_poll_download(web):
    c, d, calls = web
    r = c.post("/debug/start?frames=20").get_json()
    assert r["ok"] and r["result"]["state"] == "running"
    job = _wait_done(c)
    assert job["state"] == "done" and A._DEBUG_RE.match(job["name"])
    assert calls == [(job["name"], 20)]
    r = c.get("/debug/download/" + job["name"])
    assert r.status_code == 200 and r.data == b"PK fake bundle"
    assert "attachment" in r.headers["Content-Disposition"]


def test_download_rejects_other_names(web):
    c, d, _ = web
    (d / "diofinder.conf").write_text("secret")
    for bad in ("diofinder.conf", "..%2Fdiofinder.conf", "diofinder_debug_1.zip"):
        assert c.get("/debug/download/" + bad).status_code == 404


def test_build_failure_is_reported(web, monkeypatch):
    c, _, _ = web
    def boom(zip_name, n_frames):
        raise RuntimeError("camera gone")
    monkeypatch.setattr(A, "_debug_build", boom)
    c.post("/debug/start")
    job = _wait_done(c)
    assert job["state"] == "error" and "camera gone" in job["error"]


def test_legacy_collect_still_returns_the_zip(web):
    c, _, calls = web
    r = c.post("/debug/collect")
    assert r.status_code == 200 and r.data == b"PK fake bundle"
    assert calls and calls[0][1] == 12


def test_prune_keeps_newest_ten(tmp_path):
    for i in range(13):
        (tmp_path / f"diofinder_debug_202611020000{i:02d}.zip").write_bytes(b"x")
    (tmp_path / "hot_pixel_mask.npz").write_bytes(b"keep me")
    A._debug_prune(tmp_path, keep=10)
    left = sorted(p.name for p in tmp_path.glob("diofinder_debug_*.zip"))
    assert len(left) == 10 and left[0].endswith("03.zip")
    assert (tmp_path / "hot_pixel_mask.npz").exists()
