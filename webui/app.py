"""
diofinder web UI.

Pipeline: sycamore star_detect (matched_filter gate) + olive-solve (tetra3).
"""

import io
import json
import logging
import math
import os
import pathlib
import re as _re
import subprocess
import sys
import threading
import zipfile
from datetime import datetime

from flask import (
    Flask, render_template, redirect, url_for, request, jsonify, abort,
    send_file,
)

sys.path.insert(0, "/opt/diofinder")
try:
    from diofinder.maint import call as maint_call, MaintResponse
except ImportError:
    sys.path.insert(0, ".")
    from diofinder.maint import call as maint_call, MaintResponse

from diofinder import bg_modes as bg_modes_mod

log = logging.getLogger("diofinder.webui")

app = Flask(__name__,
            template_folder="templates",
            static_folder="static")

app.jinja_env.filters['log10'] = \
    lambda x: math.log10(float(x)) if float(x) > 0 else -3


def _shm_frame_fallback(height, width):
    """Direct SHM read of the first openable slot - the pre-frame_get path.

    Kept ONLY as a fallback for when the daemon is down (so the live view can
    still show something): the read ignores the FrameSlots protocol, so the
    frame may be TORN (camera writing that slot concurrently). Callers label
    frames from this path unsynced."""
    import numpy as np
    from multiprocessing import shared_memory, resource_tracker as _rt
    from diofinder.frame_slots import SHM_PREFIX, NUM_BUFFERS
    for i in range(NUM_BUFFERS):
        try:
            shm = shared_memory.SharedMemory(name=f"{SHM_PREFIX}_{i}",
                                             create=False)
            try:
                _rt.unregister(shm._name, "shared_memory")
            except Exception:
                pass
            frame = np.ndarray((height, width), dtype=np.uint8,
                               buffer=shm.buf).copy()
            shm.close()
            return frame
        except Exception:
            continue
    return None


def _daemon_frame(after_seq=-1, timeout=8.0, meta_out=None):
    """Newest camera frame via the daemon's frame_get maint command
    (FrameSlots-bracketed in the solver - can never be torn by a concurrent
    camera write).

    Returns (frame u8 ndarray | None, seq, synced). after_seq >= 0 waits
    solver-side for a frame NEWER than that sequence - chain it for strictly
    consecutive burst frames. Falls back to the direct-SHM read (seq -1,
    synced False) when the daemon is unreachable.

    meta_out: optional dict; when given, its "meta" key receives the frame's
    exact capture metadata (SensorTimestamp / actual exposure / actual gain,
    from frame_get's "meta" field) or None on older daemons."""
    import base64
    import numpy as np
    r = _safe_call("frame_get", {"after_seq": int(after_seq)}, timeout=timeout)
    if r.ok and r.result and r.result.get("data_b64"):
        try:
            shape = r.result.get("shape") or [0, 0]
            buf = base64.b64decode(r.result["data_b64"])
            fr = np.frombuffer(buf, dtype=np.uint8)
            if fr.size == int(shape[0]) * int(shape[1]) and fr.size > 0:
                if meta_out is not None:
                    meta_out["meta"] = r.result.get("meta")
                return (fr.reshape(int(shape[0]), int(shape[1])).copy(),
                        int(r.result.get("seq", -1)), True)
        except Exception:
            pass
    try:
        ecfg = _load_cfg_cached()
        h, w = ecfg.frame_height, ecfg.frame_width
    except Exception:
        h, w = 760, 960
    return _shm_frame_fallback(h, w), -1, False


# Direct display-SHM reader for the live view (P4): no maint round-trip, no
# base64, no solver-thread theft. Lazily attached; falls back to frame_get
# when the segment is absent (older daemon) or a read is torn.
_display_reader = {"r": None, "hw": None, "ino": None,
                   "last_seq": -1, "last_new": 0.0}
_display_lock = threading.Lock()
_display_keepalive = {"ts": 0.0}
_DISPLAY_KEEPALIVE_S = 2.0
# A fast-path frame whose seq hasn't advanced in this long is treated as
# stale and the poll is served by frame_get instead. Comfortably above the
# longest sane frame period (multi-second manual exposures) so a slow but
# healthy writer never trips it.
_DISPLAY_STALE_S = 15.0


def _live_frame(timeout=3.0):
    """Newest frame for the web LIVE VIEW, fast path first.

    Sends a throttled ``display_start`` keepalive (so the solver keeps the
    display segment warm), reads the segment DIRECTLY, and falls back to the
    ``frame_get`` maint path (the authoritative FrameSlots-bracketed read) on
    any miss. Returns (frame u8 | None, seq, synced). Bursts / debug bundles
    deliberately keep using ``_daemon_frame`` — the display segment is a
    best-effort preview, not a strictly-consecutive source.

    The daemon unlinks and RECREATES the segment on every restart
    (``ExecStartPre`` rm + ``display_shm.create``), which orphans a
    long-lived reader: it would keep returning the last frame ever written
    to the old mapping — with a valid, never-advancing seq — forever. Three
    guards keep the view live across daemon restarts without restarting the
    webui (the frozen-live-view-until-webui-restart bug):

    * the /dev/shm inode of the segment is checked each poll (one stat);
      a change, or the file disappearing, drops the reader so the next
      poll re-attaches to the daemon's CURRENT segment;
    * a frame whose seq hasn't advanced in ``_DISPLAY_STALE_S`` is served
      via frame_get instead (covers a live segment whose writer stopped);
    * a failed attach is no longer sticky — when the webui boots before
      the daemon has created the segment, the attach retries every poll
      (cheap: one failed stat) instead of permanently disabling the fast
      path with only ``hw`` recorded.
    """
    import time as _t
    from diofinder import display_shm
    now = _t.monotonic()
    if now - _display_keepalive["ts"] > _DISPLAY_KEEPALIVE_S:
        _safe_call("display_start", timeout=0.5)
        _display_keepalive["ts"] = now
    try:
        ecfg = _load_cfg_cached()
        hw = (ecfg.frame_height, ecfg.frame_width)
    except Exception:
        hw = (760, 960)
    with _display_lock:
        try:
            ino = os.stat(
                "/dev/shm/" + display_shm.DISPLAY_SHM_NAME).st_ino
        except OSError:
            ino = None
        reader = _display_reader["r"]
        if (reader is None or _display_reader["hw"] != hw
                or _display_reader["ino"] != ino):
            # (Re)attach: reader missing, frame geometry changed, or the
            # daemon recreated the segment (inode changed / file gone).
            if reader is not None:
                try:
                    reader.close()
                except Exception:
                    pass
                _display_reader["r"] = None
            reader = None
            if ino is not None:
                try:
                    cand = display_shm.DisplayReader(hw[0], hw[1])
                except Exception:
                    cand = None
                if cand is not None and cand.available:
                    _display_reader.update(
                        r=cand, hw=hw, ino=ino, last_seq=-1, last_new=now)
                    reader = cand
                elif cand is not None:
                    cand.close()
        if reader is not None:
            got = reader.read()
            if got is not None:
                frame, seq = got
                if seq != _display_reader["last_seq"]:
                    _display_reader["last_seq"] = seq
                    _display_reader["last_new"] = now
                    return frame, seq, True
                if now - _display_reader["last_new"] <= _DISPLAY_STALE_S:
                    return frame, seq, True
                # Same seq past the stale window: the writer stopped (or
                # this mapping is orphaned in a way the inode check can't
                # see). Serve the authoritative maint path until the seq
                # advances again.
    # Fall back to the maint path (also covers the first frame or two before
    # the solver has armed the segment).
    return _daemon_frame(timeout=timeout)


def _safe_call(cmd, args=None, timeout=15.0):
    """Call the maintenance daemon; return MaintResponse(ok=False) on any connection error."""
    try:
        return maint_call(cmd, args, timeout=timeout)
    except FileNotFoundError:
        return MaintResponse(ok=False, error="diofinder daemon socket not found")
    except PermissionError:
        return MaintResponse(ok=False, error="cannot access diofinder socket")
    except Exception as e:
        return MaintResponse(ok=False, error=f"{type(e).__name__}: {e}")


_cfg_cache = {"cfg": None, "mtime": None, "path": None}


def _load_cfg_cached():
    """load_config(), re-parsed only when the conf file's mtime changes.

    /frame.jpg is polled continuously; reading+parsing the file per request
    was measurable CPU-1 load for a value that almost never changes."""
    from diofinder.config import load_config, DEFAULT_CONFIG_PATH
    path = os.environ.get("DIOFINDER_CONFIG", DEFAULT_CONFIG_PATH)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    if (_cfg_cache["cfg"] is None or _cfg_cache["mtime"] != mtime
            or _cfg_cache["path"] != path):
        _cfg_cache["cfg"] = load_config()
        _cfg_cache["mtime"] = mtime
        _cfg_cache["path"] = path
    return _cfg_cache["cfg"]


def _tuning_profile():
    """Active libcamera tuning profile name from the *effective* config.

    Returns 'finder' / 'scientific' / 'standard', or '' when the path is blank
    or unrecognised — never mislabel an unknown/failed lookup as 'standard'
    (the old home/camera inline logic did, and home_page additionally called an
    unimported load_config(), so its badge always fell through to 'standard')."""
    try:
        base = os.path.basename(_load_cfg_cached().camera_tuning_file or "")
    except Exception:
        return ""
    if "finder" in base:
        return "finder"
    if "scientific" in base:
        return "scientific"
    if base == "imx477.json":
        return "standard"
    return ""


def _format_solution(sol):
    """Reshape a raw latest_solution dict into a template-friendly form."""
    if not sol:
        return None
    if not sol.get("solved"):
        return {
            "solved": False,
            "stars":  sol.get("stars", 0),
            "peak":   sol.get("peak",  0),
            "noise":  sol.get("noise", 0.0),
            "status": sol.get("status", 0),
        }
    # Show the REPORTED epoch (JNow by default) so the page matches SkySafari;
    # the daemon adds report_ra_deg/dec_deg. Fall back to the raw J2000
    # ra_deg/dec_deg for an older daemon that doesn't send them.
    _rra = sol.get("report_ra_deg", sol["ra_deg"])
    _rdec = sol.get("report_dec_deg", sol["dec_deg"])
    ra_h = _rra / 15.0
    return {
        "solved":    True,
        "ra_str":    _hms(ra_h),
        "dec_str":   _dms(_rdec),
        "ra_deg":    _rra,
        "dec_deg":   _rdec,
        "epoch":     sol.get("report_epoch", "j2000"),
        "fov_deg":   sol.get("fov_deg",  0.0),
        "roll_deg":  sol.get("roll_deg", 0.0),
        "stars":     sol["stars"],
        "matches":   sol.get("matches", 0),
        "peak":      sol["peak"],
        "noise":     sol.get("noise", 0.0),
        "solve_ms":  sol["solve_ms"],
        "star_name":    sol.get("star_name"),
        "star_desig":   sol.get("star_desig"),
        "star_mag":     sol.get("star_mag"),
        "star_sep_deg": sol.get("star_sep_deg"),
        "dso_m":        sol.get("dso_m"),
        "dso_name":     sol.get("dso_name"),
        "dso_mag":      sol.get("dso_mag"),
        "dso_type":     sol.get("dso_type"),
        "dso_sep_deg":  sol.get("dso_sep_deg"),
    }


def _hms(hours):
    """Format fractional hours as HHhMMmSSs."""
    hours = hours % 24.0
    h = int(hours); m = int((hours - h) * 60)
    s = int(round((hours - h - m / 60) * 3600))
    if s == 60: s = 0; m += 1
    if m == 60: m = 0; h = (h + 1) % 24
    return f"{h:02d}h{m:02d}m{s:02d}s"


def _dms(deg):
    """Format decimal degrees as ±DD°MM'SS\"."""
    sign = "+" if deg >= 0 else "-"
    a = abs(deg)
    d = int(a); m = int((a - d) * 60)
    s = int(round((a - d - m / 60) * 3600))
    if s == 60: s = 0; m += 1
    if m == 60: m = 0; d += 1
    return f"{sign}{d:02d}°{m:02d}'{s:02d}\""  # noqa: Q000


@app.route("/status")
def dashboard():
    """Retired: the Status page is merged into Home. Kept as a permanent
    redirect so old bookmarks and url_for('dashboard') references resolve."""
    return redirect(url_for("home_page"))


@app.route("/")
@app.route("/home")
def home_page():
    """Merged Home: live view + pointing + everyday controls (novice), with the
    expert tier expanding to detection/background tuning, boresight, calibration
    and IMU. Stands alongside Status/Camera during the page merge."""
    status   = _safe_call("status")
    seeing   = _safe_call("seeing_get")
    sparams  = _safe_call("solver_params_get")
    exposure = _safe_call("exposure_get")
    version  = _safe_call("version")
    cal      = _safe_call("calibration_status")
    sol = (_format_solution(status.result["solution"])
           if status.ok and status.result else None)
    tuning_profile = _tuning_profile()
    return render_template(
        "home.html",
        status_ok=status.ok,
        status_error=status.error if not status.ok else None,
        solution=sol,
        boresight=(status.result.get("boresight") if status.ok else None),
        calibration=(cal.result if cal.ok else None),
        cal_error=cal.error if not cal.ok else None,
        imu=(status.result.get("imu") if status.ok else None),
        seeing=(seeing.result if seeing.ok else None),
        solver_params=(sparams.result if sparams.ok else None),
        exposure=(exposure.result if exposure.ok else None),
        tuning_profile=tuning_profile,
        test_mode=(status.result.get("test_mode", True) if status.ok else True),
        version=(version.result.get("version") if version.ok else None),
        wheels=(version.result.get("wheels") if version.ok else None),
    )


@app.route("/api/status")
def api_status():
    """JSON snapshot of daemon status and calibration state for auto-refresh."""
    status = _safe_call("status")
    cal    = _safe_call("calibration_status")
    return jsonify({
        "status":      {"ok": status.ok, "result": status.result,
                        "error": status.error},
        "calibration": {"ok": cal.ok,    "result": cal.result,
                        "error": cal.error},
    })


@app.route("/api/liveview_health")
def api_liveview_health():
    """Explain why the live view couldn't fetch a frame, instead of the browser
    blindly guessing "camera not running". (Distinct from diofinder.frame_health,
    which assesses a frame's exposure/content — this is about availability.)

    The live-frame path (display SHM + the frame_get fallback) is serviced by
    the SOLVER process, so a busy/behind solver — e.g. grinding on hard frames
    low on the horizon — or a long exposure looks identical to a dead camera
    from the browser's onerror handler. The solution epoch tells them apart:
    it advances while the solver is alive (every solve, plus dark heartbeats),
    so a fresh epoch means the camera is fine and the miss was transient, while
    a stale one means the solver stalled or the camera stopped delivering."""
    import time as _t
    r = _safe_call("status", timeout=1.5)
    if not r.ok or not r.result:
        return jsonify({"ok": True, "reason": _liveview_miss_reason(None, ok=False)})
    sol = (r.result.get("solution") or {})
    epoch = sol.get("epoch_monotonic")
    age = (_t.monotonic() - epoch) if epoch else None
    return jsonify({"ok": True, "reason": _liveview_miss_reason(age, ok=True)})


def _liveview_miss_reason(age, *, ok):
    """Map (daemon reachable?, solution epoch age) -> a human explanation for a
    live-view frame miss. Pure so the branch table is unit-testable.

    `ok` False = the status call itself failed (daemon down). Otherwise `age`
    is seconds since the last published solution (None = never published), the
    signal that separates a busy/behind solver from a genuinely dead camera."""
    if not ok:
        return "daemon not responding (the service may be starting or restarting)"
    if age is None:
        return ("the solver hasn't published a frame yet — starting up "
                "(can take a while in daylight)")
    if age > 15.0:
        return (f"no fresh frames for {int(age)}s — the solver has stalled or "
                "the camera stopped delivering; check the service")
    if age > 5.0:
        return (f"the solver is behind (last update {int(age)}s ago), likely "
                "grinding on hard frames — low on the horizon? The camera is "
                "fine; the view will catch up")
    return ("the frame server is momentarily busy (or a long exposure is "
            "delivering frames slowly) — camera and solver are live")


@app.route("/boresight/center", methods=["POST"])
def boresight_center():
    """Reset boresight to the frame center and redirect to dashboard."""
    r = _safe_call("boresight_center")
    if not r.ok:
        return r.error, 500
    return redirect(url_for("dashboard"))


_NEXT_ENDPOINTS = {"dashboard", "camera_page", "home_page", "utilities_page",
                   "bgtest_page"}


def _redirect_next(default):
    """Redirect to the form's `next` endpoint if it's an allow-listed page,
    else to `default`. Lets shared control forms return to whichever page
    (Status / Camera / Home) submitted them."""
    nxt = request.form.get("next", "")
    if nxt not in _NEXT_ENDPOINTS:
        nxt = default
    return redirect(url_for(nxt))


@app.route("/testmode/set", methods=["POST"])
def testmode_set():
    """Toggle test-image vs. live-camera mode; return to the submitting page."""
    raw     = request.form.get("enabled", "false").strip().lower()
    enabled = raw in ("true", "1", "yes")
    r = _safe_call("set_test_mode", {"enabled": enabled})
    if not r.ok:
        return r.error, 500
    return _redirect_next("dashboard")


# ---- Polar alignment --------------------------------------------------------

@app.route("/polar")
def polar_page():
    """Polar-alignment page: shows current status and start/cancel controls."""
    status = _safe_call("polar_status")
    return render_template(
        "polar.html",
        ok=status.ok,
        error=status.error if not status.ok else None,
        polar=status.result if status.ok else None,
    )


@app.route("/api/polar/status")
def api_polar_status():
    """JSON polar-alignment status for the polar page's auto-refresh."""
    r = _safe_call("polar_status")
    return jsonify({"ok": r.ok, "result": r.result, "error": r.error})


@app.route("/polar/start", methods=["POST"])
def polar_start():
    """Start a polar-alignment session and redirect to the polar page."""
    _safe_call("polar_start")
    return redirect(url_for("polar_page"))


@app.route("/polar/cancel", methods=["POST"])
def polar_cancel():
    """Cancel the running polar-alignment session and redirect to the polar page."""
    _safe_call("polar_cancel")
    return redirect(url_for("polar_page"))


@app.route("/polar/set-latitude", methods=["POST"])
def polar_set_latitude():
    """Persist a new observer latitude and push it to the solver, then redirect."""
    try:
        lat = float(request.form.get("latitude_deg", ""))
    except ValueError:
        return "latitude must be numeric", 400
    if not (-90.0 <= lat <= 90.0):
        return "latitude out of range", 400
    _safe_call("polar_set_latitude",
               {"latitude_deg": lat, "persist": True})
    return redirect(url_for("polar_page"))


@app.route("/bgtest")
def bgtest_page():
    """Background compensation settings page."""
    r = _safe_call("solver_params_get")
    return render_template(
        "bgtest.html",
        params=(r.result if r.ok else None),
        error=(r.error if not r.ok else None),
        bg_modes=bg_modes_mod.for_ui(),
    )


@app.route("/bgtest/set", methods=["POST"])
def bgtest_set():
    """Apply background mode from the bgtest form and redirect back."""
    mode = request.form.get("bg_mode", "row_percentile").strip().lower()
    # The single "Apply & save" button always applies live AND persists.
    params = {"detect_bg_mode": mode, "persist": True}
    if mode == "top_hat":
        try:
            params["detect_tophat_radius"] = int(request.form.get("tophat_radius", 12))
        except (ValueError, TypeError):
            params["detect_tophat_radius"] = 12
    if mode == "block_percentile":
        try:
            params["detect_bg_block_size"] = int(request.form.get("bg_block_size", 32))
        except (ValueError, TypeError):
            params["detect_bg_block_size"] = 32
    if mode == "uniform_mean":
        try:
            params["detect_uniform_filter_size"] = int(
                request.form.get("uniform_filter_size", 25))
        except (ValueError, TypeError):
            params["detect_uniform_filter_size"] = 25
    if mode in ("uniform_mean", "block_percentile"):
        nm = request.form.get("noise_mode", "mad").strip().lower()
        if nm in ("mad", "global_rms"):
            params["detect_noise_mode"] = nm
    r = _safe_call("solver_params_set", params)
    if not r.ok:
        return r.error, 500
    return _redirect_next("bgtest_page")


@app.route("/api/bgcache")
def api_bgcache():
    """Live temporal-background-cache status for the Background page."""
    r = _safe_call("bg_cache_status")
    return jsonify({"ok": r.ok, "result": r.result, "error": r.error})


def _bg_preview_arrays(mode, kind):
    """Fetch the paired frame + reconstructed background from the solver's
    bg_preview op (the single home for background-preview math — the webui no
    longer reimplements it, and this is the only way to render the live
    temporal-median stack, which exists only in the solver process).

    Returns (out_float, lo, hi, meta) or (None, 0, 0, error_string)."""
    import numpy as np
    args = {"mode": mode}
    for qname, argk in (("radius", "tophat_radius"),
                        ("block", "bg_block_size"),
                        ("window", "uniform_filter_size")):
        v = request.args.get(qname)
        if v is not None:
            try:
                args[argk] = max(1, min(400, int(v)))
            except (ValueError, TypeError):
                pass
    r = _safe_call("bg_preview", args)
    if not r.ok:
        return None, 0, 0, (r.error or "bg preview unavailable")
    res = r.result or {}
    try:
        import base64
        h, w = int(res["shape"][0]), int(res["shape"][1])
        frame = np.frombuffer(
            base64.b64decode(res["frame_b64"]), np.uint8).reshape(h, w)
        bg = np.frombuffer(
            base64.b64decode(res["bg_b64"]), np.uint8).reshape(h, w).astype(np.float32)
    except Exception as e:
        return None, 0, 0, f"malformed bg preview: {e}"
    if kind == "bg":
        out = bg
        lo, hi = float(np.percentile(out, 1)), float(np.percentile(out, 99))
    else:
        out = np.clip(frame.astype(np.float32) - bg, 0.0, None)
        lo, hi = 0.0, max(float(np.percentile(out, 99.5)), 8.0)
    return out, lo, max(hi, lo + 1.0), res


@app.route("/bg.jpg")
def bg_jpg():
    """Render the computed background, or the background-subtracted frame, for a
    chosen bg mode. The background is reconstructed by the solver's bg_preview
    op (webui no longer reimplements it); temporal_median shows the live cached
    median stack. Compare methods / tune sizes visually."""
    import numpy as np
    from PIL import Image
    mode = request.args.get("mode", "line_median")
    kind = request.args.get("kind", "sub")
    out, lo, hi, meta = _bg_preview_arrays(mode, kind)
    if out is None:
        return meta, 503, {"Content-Type": "text/plain"}
    disp = np.clip((out - lo) / (hi - lo) * 255.0, 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(disp, mode="L").save(buf, format="JPEG", quality=80)
    buf.seek(0)
    return send_file(buf, mimetype="image/jpeg")


@app.route("/api/bgpreview")
def api_bgpreview():
    """Preview metadata only (no image bytes) so the Background page can label
    what the /bg.jpg render is actually showing — e.g. the live temporal-median
    stack's frame count/age, or the per-frame degradation when none is built."""
    mode = request.args.get("mode", "line_median")
    r = _safe_call("bg_preview", {"mode": mode})
    if not r.ok:
        return jsonify({"ok": False, "error": r.error})
    res = dict(r.result or {})
    res.pop("frame_b64", None)
    res.pop("bg_b64", None)
    return jsonify({"ok": True, "result": res})


# ---- Seeing presets ---------------------------------------------------------

@app.route("/seeing", methods=["POST"])
def seeing_set():
    """Apply a Good/Bad seeing preset (form 'mode') and redirect back.

    A plain toggle loads the factory preset; pass use_override=1 to apply the
    saved override for that mode instead.
    """
    mode = request.form.get("mode", "good").strip().lower()
    args = {"mode": mode}
    if request.form.get("use_override") in ("1", "true", "on", "yes"):
        args["use_override"] = True
    r = _safe_call("seeing_set", args)
    if not r.ok:
        return r.error, 500
    return _redirect_next("dashboard")


@app.route("/seeing/override/save", methods=["POST"])
def seeing_override_save():
    """Save the current effective settings as the override for a mode."""
    args = {}
    mode = (request.form.get("mode") or "").strip().lower()
    if mode:
        args["mode"] = mode
    r = _safe_call("seeing_override_save", args)
    if not r.ok:
        return r.error, 500
    nxt = request.form.get("next") or "config_page"
    if nxt not in ("dashboard", "config_page", "camera_page"):
        nxt = "config_page"
    return redirect(url_for(nxt))


@app.route("/seeing/override/clear", methods=["POST"])
def seeing_override_clear():
    """Delete the saved override for a mode (revert to factory)."""
    args = {}
    mode = (request.form.get("mode") or "").strip().lower()
    if mode:
        args["mode"] = mode
    r = _safe_call("seeing_override_clear", args)
    if not r.ok:
        return r.error, 500
    nxt = request.form.get("next") or "config_page"
    if nxt not in ("dashboard", "config_page", "camera_page"):
        nxt = "config_page"
    return redirect(url_for(nxt))


@app.route("/api/seeing")
def api_seeing():
    """JSON seeing mode + preset table + drift + lineage for live UI updates."""
    r = _safe_call("seeing_get")
    return jsonify({"ok": r.ok, "result": r.result, "error": r.error})


# ---- Hot-pixel mask ---------------------------------------------------------

@app.route("/hotpixel/capture", methods=["POST"])
def hotpixel_capture():
    """Capture a dark frame and build the hot-pixel mask (cap the lens first)."""
    try:
        frames = int(request.form.get("frames", 16))
    except (ValueError, TypeError):
        frames = 16
    # Optional fixed worst-case capture point (exposure + gain). When the form
    # supplies these, the daemon snapshots the live setting, captures at the
    # requested point, and restores — so the mask covers the worst case the
    # finder will run at regardless of the current exposure/gain.
    args = {"frames": frames}
    for key in ("exposure_s", "gain"):
        val = request.form.get(key, "")
        if val:
            try:
                args[key] = float(val)
            except (ValueError, TypeError):
                pass
    # Stretching the camera to a long exposure + settle takes longer than the
    # default; give the call extra head-room.
    r = _safe_call("dark_capture", args, timeout=90.0)
    if not r.ok:
        return r.error, 500
    nxt = request.form.get("next", "")
    return redirect(url_for("utilities_page" if nxt == "utilities" else "camera_page"))


@app.route("/hotpixel/clear", methods=["POST"])
def hotpixel_clear():
    """Clear the hot-pixel mask and redirect back (Camera or Utilities)."""
    r = _safe_call("hot_pixel_clear")
    if not r.ok:
        return r.error, 500
    nxt = request.form.get("next", "")
    return redirect(url_for("utilities_page" if nxt == "utilities" else "camera_page"))


@app.route("/utilities")
def utilities_page():
    """Novice-safe maintenance utilities (no Expert mode required): capture a
    dark frame, and grab support archives. Tuning/diagnostics stay in Expert."""
    hotpix  = _safe_call("hot_pixel_status")
    version = _safe_call("version")
    seeing  = _safe_call("seeing_get")
    return render_template(
        "utilities.html",
        hotpix=(hotpix.result if hotpix.ok else None),
        version=(version.result.get("version") if version.ok else None),
        seeing=(seeing.result if seeing.ok else None),
    )


@app.route("/api/hotpixel")
def api_hotpixel():
    """JSON hot-pixel mask status (count, mtime)."""
    r = _safe_call("hot_pixel_status")
    return jsonify({"ok": r.ok, "result": r.result, "error": r.error})


# ---- Offline auto-tune sweep ------------------------------------------------

@app.route("/autotune/start", methods=["POST"])
def autotune_start():
    """Kick off the background auto-tune sweep (point at a star field first)."""
    args = {}
    mode = (request.form.get("mode") or "").strip().lower()
    if mode:
        args["mode"] = mode
    if request.form.get("commit") in ("1", "true", "on", "yes"):
        args["commit"] = True
    for key, cast in (("frames_per_point", int), ("time_budget_s", float),
                      ("match_rate_floor", float)):
        val = request.form.get(key)
        if val:
            try:
                args[key] = cast(val)
            except (ValueError, TypeError):
                pass
    r = _safe_call("auto_tune", args)
    return jsonify({"ok": r.ok, "result": r.result, "error": r.error})


@app.route("/api/autotune")
def api_autotune():
    """JSON auto-tune progress / result for the Camera-page poller."""
    r = _safe_call("auto_tune_status")
    return jsonify({"ok": r.ok, "result": r.result, "error": r.error})


@app.route("/autotune/cancel", methods=["POST"])
def autotune_cancel():
    """Request cancellation of an in-progress auto-tune sweep."""
    r = _safe_call("auto_tune_cancel")
    return jsonify({"ok": r.ok, "result": r.result, "error": r.error})


@app.route("/autotune/apply", methods=["POST"])
def autotune_apply():
    """Apply the last (dry-run) auto-tune winner live + save it as the override."""
    r = _safe_call("auto_tune_apply_last")
    return jsonify({"ok": r.ok, "result": r.result, "error": r.error})


# ── Experimental A/B card: tracking mode + P5 bin-at-submit ──────────────────
# Both are live-mutable solver toggles that had no webui control (CLI-only).
# The tracking A/B reuses tests/ab_tracking.run_ab in a webui-side background
# thread (it drives the maint socket and restores tracking_enabled on exit).
_ab_lock = threading.Lock()
_ab = {"running": False, "progress": "", "result": None, "error": None,
       "started": None, "window": 30.0}


def _ab_worker(window):
    from tests.ab_tracking import run_ab
    from diofinder.maint import call as maint_call

    def prog(msg):
        with _ab_lock:
            _ab["progress"] = msg
    try:
        res = run_ab(window, 3.0, 15.0, maint_call=maint_call, progress=prog)
        with _ab_lock:
            _ab["result"] = res
            _ab["error"] = res.get("error") or res.get("aborted")
    except Exception as e:   # never leave the card stuck "running"
        with _ab_lock:
            _ab["error"] = str(e)
    finally:
        with _ab_lock:
            _ab["running"] = False
            _ab["progress"] = "done"


@app.route("/abtracking/start", methods=["POST"])
def abtracking_start():
    """Run the FULL-vs-TRACKING A/B on the live sky (background, ~70 s)."""
    try:
        window = float(request.form.get("window", 30.0))
    except (TypeError, ValueError):
        window = 30.0
    window = max(5.0, min(120.0, window))
    with _ab_lock:
        if _ab["running"]:
            return jsonify({"ok": False,
                            "error": "an A/B run is already in progress"}), 409
        _ab.update(running=True, progress="starting…", result=None, error=None,
                   window=window, started=datetime.now().strftime("%H:%M:%S"))
    threading.Thread(target=_ab_worker, args=(window,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/abtracking")
def api_abtracking():
    """Tracking-A/B progress / result for the Camera-page poller."""
    with _ab_lock:
        return jsonify(dict(_ab))


@app.route("/api/tracking")
def api_tracking():
    """Live tracking-mode status (state + counters) for the card readout."""
    r = _safe_call("tracking_status")
    return jsonify({"ok": r.ok, "result": r.result, "error": r.error})


@app.route("/tracking/set", methods=["POST"])
def tracking_set():
    """Toggle live tracking mode (optionally persist)."""
    enabled = request.form.get("enabled") in ("1", "true", "on", "yes")
    persist = request.form.get("persist") in ("1", "true", "on", "yes")
    r = _safe_call("solver_params_set",
                   {"tracking_enabled": enabled, "persist": persist})
    return jsonify({"ok": r.ok, "result": r.result, "error": r.error})


@app.route("/bincache/set", methods=["POST"])
def bincache_set():
    """Toggle the P5 bin-at-submit bg_cache stack path (optionally persist)."""
    enabled = request.form.get("enabled") in ("1", "true", "on", "yes")
    persist = request.form.get("persist") in ("1", "true", "on", "yes")
    r = _safe_call("solver_params_set",
                   {"bg_cache_bin_at_submit": enabled, "persist": persist})
    return jsonify({"ok": r.ok, "result": r.result, "error": r.error})


@app.route("/calibration/reset", methods=["POST"])
def calibration_reset():
    """Reset the FOV rolling-window calibration and redirect to the dashboard."""
    r = _safe_call("calibration_reset")
    if not r.ok:
        return r.error, 500
    return redirect(url_for("dashboard"))


@app.route("/advanced")
def camera_page():
    """Camera and solver settings page (nav label: Advanced)."""
    exposure      = _safe_call("exposure_get")
    solver_params = _safe_call("solver_params_get")
    hotpix        = _safe_call("hot_pixel_status")
    seeing        = _safe_call("seeing_get")
    tuning_profile = _tuning_profile()
    return render_template(
        "camera.html",
        exposure=(exposure.result if exposure.ok else None),
        solver_params=(solver_params.result if solver_params.ok else None),
        tuning_profile=tuning_profile,
        hotpix=(hotpix.result if hotpix.ok else None),
        seeing=(seeing.result if seeing.ok else None),
        bursts=_list_bursts(),
        bg_modes=bg_modes_mod.for_ui(),
    )


@app.route("/autoexposure/set", methods=["POST"])
def autoexposure_set():
    """Toggle the auto-exposure controller (applies live and persists)."""
    enabled = request.form.get("enabled", "false").strip().lower() in ("true", "1", "on")
    r = _safe_call("auto_exposure_set", {"enabled": enabled, "persist": True})
    if not r.ok:
        return r.error, 500
    return _redirect_next("camera_page")


@app.route("/tuning/set", methods=["POST"])
def tuning_set():
    """Switch the libcamera tuning profile (persists; needs a service restart)."""
    profile = request.form.get("profile", "scientific").strip().lower()
    r = _safe_call("tuning_set", {"profile": profile})
    if not r.ok:
        return r.error, 500
    return redirect(url_for("camera_page"))


@app.route("/exposure/set", methods=["POST"])
def exposure_set():
    """Apply exposure and optional gain from the camera-settings form."""
    # The single "Apply & save" button always applies live AND persists.
    persist = True
    try:
        s = float(request.form.get("exposure_s", ""))
        r = _safe_call("exposure_set", {"exposure_s": s, "persist": persist})
        if not r.ok:
            return r.error, 400
    except ValueError:
        return "exposure must be numeric", 400
    if request.form.get("gain"):
        try:
            g = float(request.form.get("gain"))
            r = _safe_call("gain_set", {"gain": g, "persist": persist})
            if not r.ok:
                return r.error, 400
        except ValueError:
            return "gain must be numeric", 400
    return _redirect_next("camera_page")


@app.route("/api/camera/state")
def api_camera_state():
    """Live exposure/gain + solver/match params, so the Camera page can keep its
    controls in sync after auto-exposure, auto-tune, or a seeing-preset change."""
    out = {}
    for cmd in ("exposure_get", "solver_params_get", "match_params_get"):
        r = _safe_call(cmd)
        if r.ok and isinstance(r.result, dict):
            out.update(r.result)
    return jsonify(out)


@app.route("/api/camera/set", methods=["POST"])
def api_camera_set():
    """JSON API for live camera/solver parameter changes without a page reload."""
    data   = request.get_json(silent=True) or {}
    errors = []
    applied = {}
    if "exposure_s" in data:
        try:
            s = float(data["exposure_s"])
            r = _safe_call("exposure_set", {"exposure_s": s, "persist": False})
            if r.ok:
                applied["exposure_s"] = s
            else:
                errors.append(f"exposure: {r.error}")
        except (ValueError, TypeError) as e:
            errors.append(f"exposure_s invalid: {e}")
    if "gain" in data:
        try:
            g = float(data["gain"])
            r = _safe_call("gain_set", {"gain": g, "persist": False})
            if r.ok:
                applied["gain"] = g
            else:
                errors.append(f"gain: {r.error}")
        except (ValueError, TypeError) as e:
            errors.append(f"gain invalid: {e}")
    _solver_float_keys = ("detect_sigma", "detect_kernel_sigma",
                          "detect_max_axis_ratio", "fov_max_error_deg",
                          "imu_rate_gate_dps")
    _solver_int_keys = ("solve_timeout_ms", "min_centroids", "max_solve_stars")
    _solver_extra = ("detect_local_noise", "extractor_backend",
                     "detect_bg_mode", "detect_noise_mode")
    if any(k in data for k in _solver_float_keys + _solver_int_keys + _solver_extra):
        pargs = {"persist": False}
        for k in _solver_float_keys:
            if k in data:
                try:
                    pargs[k] = float(data[k])
                except (ValueError, TypeError) as e:
                    errors.append(f"{k} invalid: {e}")
        for k in _solver_int_keys:
            if k in data:
                try:
                    pargs[k] = int(data[k])
                except (ValueError, TypeError) as e:
                    errors.append(f"{k} invalid: {e}")
        if "detect_local_noise" in data:
            pargs["detect_local_noise"] = bool(data["detect_local_noise"])
        # String-valued solver knobs (validated daemon-side).
        for k in ("extractor_backend", "detect_bg_mode", "detect_noise_mode"):
            if k in data:
                pargs[k] = str(data[k])
        if len(pargs) > 1:
            r = _safe_call("solver_params_set", pargs)
            if r.ok:
                applied.update({k: v for k, v in pargs.items() if k != "persist"})
            else:
                errors.append(f"solver_params: {r.error}")
    if "match_radius" in data or "match_threshold" in data:
        margs = {"persist": False}
        for k in ("match_radius", "match_threshold"):
            if k in data:
                try:
                    margs[k] = float(data[k])
                except (ValueError, TypeError) as e:
                    errors.append(f"{k} invalid: {e}")
        if len(margs) > 1:
            r = _safe_call("match_params_set", margs)
            if r.ok:
                applied.update({k: v for k, v in margs.items() if k != "persist"})
            else:
                errors.append(f"match_params: {r.error}")
    if errors:
        return jsonify({"ok": False, "errors": errors, "applied": applied}), 400
    return jsonify({"ok": True, "applied": applied})


@app.route("/solver/params/set", methods=["POST"])
def solver_params_set():
    """Apply detect_sigma and solve_timeout_ms from the solver-settings form."""
    # The single "Apply & save" button always applies live AND persists.
    persist = True
    pargs   = {"persist": persist}
    if request.form.get("extractor_backend"):
        pargs["extractor_backend"] = request.form["extractor_backend"].strip().lower()
    if request.form.get("detect_sigma"):
        try:
            pargs["detect_sigma"] = float(request.form["detect_sigma"])
        except ValueError:
            return "detect_sigma must be numeric", 400
    if request.form.get("detect_kernel_sigma"):
        try:
            pargs["detect_kernel_sigma"] = float(request.form["detect_kernel_sigma"])
        except ValueError:
            return "detect_kernel_sigma must be numeric", 400
    if request.form.get("detect_max_axis_ratio") is not None and \
            request.form.get("detect_max_axis_ratio") != "":
        try:
            pargs["detect_max_axis_ratio"] = float(request.form["detect_max_axis_ratio"])
        except ValueError:
            return "detect_max_axis_ratio must be numeric", 400
    if "detect_local_noise" in request.form:
        pargs["detect_local_noise"] = request.form.get(
            "detect_local_noise", "false").strip().lower() in ("true", "1", "on")
    if "star_name_brightest" in request.form:
        pargs["star_name_brightest"] = request.form.get(
            "star_name_brightest", "false").strip().lower() in ("true", "1", "on")
    if "star_name_whole_fov" in request.form:
        pargs["star_name_whole_fov"] = request.form.get(
            "star_name_whole_fov", "false").strip().lower() in ("true", "1", "on")
    if "star_name_dso" in request.form:
        pargs["star_name_dso"] = request.form.get(
            "star_name_dso", "false").strip().lower() in ("true", "1", "on")
    if request.form.get("min_centroids"):
        try:
            pargs["min_centroids"] = int(request.form["min_centroids"])
        except ValueError:
            return "min_centroids must be integer", 400
    if request.form.get("solve_timeout_ms"):
        try:
            pargs["solve_timeout_ms"] = int(request.form["solve_timeout_ms"])
        except ValueError:
            return "solve_timeout_ms must be integer", 400
    if request.form.get("max_solve_stars"):
        try:
            pargs["max_solve_stars"] = int(request.form["max_solve_stars"])
        except ValueError:
            return "max_solve_stars must be integer", 400
    if request.form.get("fov_max_error_deg"):
        try:
            pargs["fov_max_error_deg"] = float(request.form["fov_max_error_deg"])
        except ValueError:
            return "fov_max_error_deg must be numeric", 400
    if request.form.get("imu_rate_gate_dps") not in (None, ""):
        try:
            pargs["imu_rate_gate_dps"] = float(request.form["imu_rate_gate_dps"])
        except ValueError:
            return "imu_rate_gate_dps must be numeric", 400
    r = _safe_call("solver_params_set", pargs)
    if not r.ok:
        return r.error, 400
    # Optional match params on the same form.
    margs = {"persist": persist}
    if request.form.get("match_radius"):
        try:
            margs["match_radius"] = float(request.form["match_radius"])
        except ValueError:
            return "match_radius must be numeric", 400
    if request.form.get("match_threshold"):
        try:
            margs["match_threshold"] = float(request.form["match_threshold"])
        except ValueError:
            return "match_threshold must be numeric", 400
    if len(margs) > 1:
        r = _safe_call("match_params_set", margs)
        if not r.ok:
            return r.error, 400
    return _redirect_next("camera_page")


# ---- Wi-Fi ------------------------------------------------------------------

def _get_wifi_status():
    """Query nmcli for the active wlan0 connection name, mode (ap/station), SSID, and IP."""
    result = {"mode": "disconnected", "ssid": None, "connection": None,
              "ip": None}
    try:
        active = subprocess.check_output(
            ["nmcli", "-t", "-f", "NAME,DEVICE", "con", "show", "--active"],
            text=True, errors="replace", timeout=5,
        )
        for line in active.splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and parts[1] == "wlan0":
                wlan_con = parts[0]
                result["connection"] = wlan_con
                result["mode"] = "ap" if wlan_con == "diofinder-ap" else "station"
                try:
                    ssid_out = subprocess.check_output(
                        ["nmcli", "-t", "-s", "-f",
                         "802-11-wireless.ssid", "con", "show", wlan_con],
                        text=True, errors="replace", timeout=3,
                    )
                    m = _re.search(r"802-11-wireless\.ssid:(.*)", ssid_out)
                    result["ssid"] = m.group(1).strip() if m else wlan_con
                except Exception:
                    result["ssid"] = wlan_con
                break
    except Exception:
        pass
    try:
        ip_out = subprocess.check_output(
            ["ip", "-4", "addr", "show", "wlan0"],
            text=True, errors="replace", timeout=3,
        )
        m = _re.search(r"inet (\S+)", ip_out)
        if m:
            result["ip"] = m.group(1)
    except Exception:
        pass
    return result


def _scan_networks():
    """Return visible Wi-Fi networks as [{ssid, signal}] sorted by signal, excluding our own AP."""
    try:
        out = subprocess.check_output(
            ["nmcli", "-t", "-f", "SSID,SIGNAL",
             "dev", "wifi", "list", "--rescan", "no"],
            text=True, errors="replace", timeout=5,
        )
        seen     = set()
        networks = []
        for line in out.splitlines():
            parts  = line.split(":")
            ssid   = parts[0].strip() if parts else ""
            try:
                signal = int(parts[1].strip()) if len(parts) > 1 else 0
            except ValueError:
                signal = 0
            if ssid and ssid not in seen and not ssid.startswith("diofinder-"):
                seen.add(ssid)
                networks.append({"ssid": ssid, "signal": signal})
        networks.sort(key=lambda n: n["signal"], reverse=True)
        return networks
    except Exception:
        return []


_wifi_lock         = threading.Lock()
_wifi_connect_proc = None
# station.sh output is captured here so a failed AP->station switch is
# diagnosable (the script prints WHY nmcli could not associate). The password
# is never echoed by station.sh/nmcli, so the log is safe to surface.
_WIFI_LOG = "/var/lib/diofinder/wifi-connect.log"


@app.route("/wifi")
def wifi_page():
    """Wi-Fi management page: current status and list of visible networks."""
    status   = _get_wifi_status()
    networks = _scan_networks()
    return render_template("wifi.html", status=status, networks=networks)


@app.route("/wifi/ap", methods=["POST"])
def wifi_ap():
    """Switch wlan0 to access-point mode via ap.sh and redirect to the Wi-Fi page."""
    try:
        subprocess.run(["sudo", "/usr/local/bin/ap.sh"],
                       timeout=30, capture_output=True)
    except Exception:
        pass
    return redirect(url_for("wifi_page"))


@app.route("/wifi/station", methods=["POST"])
def wifi_station():
    """Start connecting wlan0 to the given SSID via station.sh and redirect to the connecting page."""
    global _wifi_connect_proc
    ssid     = request.form.get("ssid",     "").strip()
    password = request.form.get("password", "").strip()
    if not ssid:
        return "SSID required", 400
    cmd = ["sudo", "/usr/local/bin/station.sh", ssid, password]
    with _wifi_lock:
        if _wifi_connect_proc and _wifi_connect_proc.poll() is None:
            _wifi_connect_proc.terminate()
        # Capture stdout+stderr to a log instead of discarding them, so a failed
        # switch (wrong key, out of range, sudo/PATH/nmcli error) is visible on
        # the connecting page rather than just a silent revert to AP.
        try:
            out = open(_WIFI_LOG, "w")
            out.write("# connecting to SSID: %s\n" % ssid)
            out.flush()
        except OSError:
            out = subprocess.DEVNULL
        _wifi_connect_proc = subprocess.Popen(
            cmd,
            stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return redirect(url_for("wifi_connecting", ssid=ssid))


@app.route("/api/wifi/log")
def api_wifi_log():
    """Tail of the last AP->station attempt's log (plain text; password-free)."""
    try:
        with open(_WIFI_LOG) as f:
            return f.read()[-4000:], 200, {"Content-Type": "text/plain"}
    except OSError:
        return "", 200, {"Content-Type": "text/plain"}


@app.route("/wifi/connecting")
def wifi_connecting():
    """Show the connecting spinner page while station.sh runs in the background."""
    ssid = request.args.get("ssid", "")
    return render_template("wifi_connecting.html", ssid=ssid)


@app.route("/api/wifi/status")
def api_wifi_status():
    """JSON current wlan0 status for the connecting-page auto-refresh."""
    return jsonify(_get_wifi_status())


@app.route("/api/wifi/scan")
def api_wifi_scan():
    """Trigger an nmcli rescan and return the updated network list as JSON."""
    try:
        subprocess.run(["nmcli", "dev", "wifi", "rescan"],
                       timeout=10, capture_output=True)
    except Exception:
        pass
    return jsonify({"networks": _scan_networks()})


# ---- Logs -------------------------------------------------------------------

@app.route("/logs")
def logs():
    """Show the most recent n lines (10–500) from the diofinder.service journal."""
    n = int(request.args.get("n", 100))
    n = max(10, min(n, 500))
    try:
        out = subprocess.check_output(
            ["journalctl", "--system",
             "-u", "diofinder.service",
             "-n", str(n), "--no-pager", "-o", "short-precise"],
            text=True, errors="replace",
            stderr=subprocess.STDOUT, timeout=5.0,
        )
    except subprocess.CalledProcessError as e:
        out = f"journalctl failed: {e}"
    except FileNotFoundError:
        out = "journalctl not found on this system"
    except subprocess.TimeoutExpired:
        out = "journalctl timed out"
    return render_template("logs.html", logs=out, n=n)


# ---- Update -----------------------------------------------------------------

UPDATE_LOG = "/var/lib/diofinder/last-update.log"


def _running_commit():
    """One-line description of the git checkout the daemon code runs from
    (`/opt/diofinder`), e.g. '1ef350d (claude/friendly-lamport-ff02lf) webui: …'.
    Returns None if it can't be read."""
    try:
        out = subprocess.run(
            ["git", "-c", "safe.directory=/opt/diofinder", "-C", "/opt/diofinder",
             "log", "-1", "--format=%h (%D) %s"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return None


@app.route("/update", methods=["GET", "POST"])
def update_page():
    """OTA update page: GET shows current version + running commit; POST fires
    diofinder-update in the background, capturing its output to a log.

    An optional 'ref' form field updates to a specific branch/tag instead of the
    latest release (runs `diofinder-update --ref <ref>`)."""
    if request.method == "POST":
        cmd = ["sudo", "/usr/local/bin/diofinder-update"]
        ref = (request.form.get("ref") or "").strip()
        if ref:
            # Branch/tag names: letters, digits, and ./_/-, with slashes for
            # namespaced branches (e.g. claude/my-branch). Reject anything else.
            if not _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,200}", ref):
                return "invalid branch/tag name", 400
            cmd += ["--ref", ref]
        # Capture output to a log so a failed update (bad ref, dirty tree,
        # fast-forward conflict) is VISIBLE on the Update page instead of
        # vanishing into /dev/null. diofinder-update is detached (it restarts
        # this very webui), so it keeps writing the log across the restart.
        try:
            logf = open(UPDATE_LOG, "w")
            logf.write(f"$ {' '.join(cmd)}\n"
                       f"started: {datetime.now().isoformat(timespec='seconds')}\n\n")
            logf.flush()
            out = logf
        except OSError:
            out = subprocess.DEVNULL
        # diofinder-update-launcher puts the updater in its OWN transient
        # systemd unit: outside this webui unit's ProtectSystem=full sandbox
        # (so the wrapper-script and systemd-unit resync actually happen) and
        # outside its cgroup (so restarting the webui mid-update no longer
        # kills the updater before it prints completion + the wheel summary).
        # It must be a separate sudo-authorized SCRIPT, not `sudo systemd-run
        # ...` inlined here: sudo authorizes on the command it's asked to run
        # (systemd-run), not on diofinder-update appearing later as one of
        # systemd-run's own arguments, so a grant scoped to diofinder-update
        # alone silently never covers this call — see
        # diofinder-update-launcher for the full explanation. Fallback to a
        # plain detached Popen (sandboxed, but the update itself still
        # succeeds) if the launcher isn't installed yet or sudo denies it.
        run_cmd = ["sudo", "/usr/local/bin/diofinder-update-launcher"] + cmd[2:]
        use_launcher = True
        try:
            r = subprocess.run(run_cmd, capture_output=True, text=True,
                               timeout=15)
            if r.returncode != 0:
                use_launcher = False
        except Exception:
            use_launcher = False
        try:
            if not use_launcher:
                subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        except FileNotFoundError:
            return "diofinder-update not installed", 500
        return render_template("update_running.html", ref=ref or "latest release")
    version = _safe_call("version")
    # Shipped-default divergence: /etc conf persists across OTA updates, so
    # settings can silently rot (old tuning profile, old background mode...).
    # Show every key that differs from the shipped conf.default so stale
    # values are at least VISIBLE next to the update button.
    try:
        from diofinder.conf_migrate import diff_from_default
        conf_diffs = diff_from_default()
    except Exception:
        conf_diffs = []
    return render_template(
        "update.html",
        version=(version.result.get("version") if version.ok else "unknown"),
        wheels=(version.result.get("wheels") if version.ok else None),
        running=_running_commit(),
        conf_diffs=conf_diffs,
    )


@app.route("/api/update/log")
def api_update_log():
    """Tail of the last diofinder-update run + terminal state, for the running page."""
    try:
        with open(UPDATE_LOG) as f:
            text = f.read()
    except OSError:
        text = ""
    low = text.lower()
    done = ("complete" in low) or ("error" in low)
    ok = done and ("complete" in low) and ("error" not in low)
    return jsonify({"log": text, "done": done, "ok": ok,
                    "commit": _running_commit()})


# ---- Factory reset ------------------------------------------------------------

FACTORY_RESET_LOG = "/var/lib/diofinder/last-factory-reset.log"


@app.route("/factory_reset", methods=["POST"])
def factory_reset():
    """Restore diofinder.conf to the shipped default and restart the service.

    Fires diofinder-factory-reset in the background via
    diofinder-factory-reset-launcher (detached in its own systemd unit, same
    reasoning as /update: the script restarts this very webui, so running it
    inside the request's own cgroup would kill it before it finishes) and
    shows a page that polls the log until done.
    """
    cmd = ["sudo", "/usr/local/bin/diofinder-factory-reset"]
    if request.form.get("clear_overrides"):
        cmd.append("--clear-overrides")
    if request.form.get("clear_hot_pixel_mask"):
        cmd.append("--clear-hot-pixel-mask")
    try:
        logf = open(FACTORY_RESET_LOG, "w")
        logf.write(f"$ {' '.join(cmd)}\n"
                   f"started: {datetime.now().isoformat(timespec='seconds')}\n\n")
        logf.flush()
        out = logf
    except OSError:
        out = subprocess.DEVNULL
    # See diofinder-update-launcher for why this must be a separate
    # sudo-authorized script rather than `sudo systemd-run ...` inlined here.
    run_cmd = ["sudo", "/usr/local/bin/diofinder-factory-reset-launcher"] + cmd[2:]
    use_launcher = True
    try:
        r = subprocess.run(run_cmd, capture_output=True, text=True,
                           timeout=15)
        if r.returncode != 0:
            use_launcher = False
    except Exception:
        use_launcher = False
    try:
        if not use_launcher:
            subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT,
                             start_new_session=True)
    except FileNotFoundError:
        return "diofinder-factory-reset not installed", 500
    return render_template("factory_reset_running.html")


@app.route("/api/factory_reset/log")
def api_factory_reset_log():
    """Tail of the last factory-reset run + terminal state, for the running page."""
    try:
        with open(FACTORY_RESET_LOG) as f:
            text = f.read()
    except OSError:
        text = ""
    low = text.lower()
    done = ("complete" in low) or ("error" in low)
    ok = done and ("complete" in low) and ("error" not in low)
    return jsonify({"log": text, "done": done, "ok": ok})



# ---- Config view ------------------------------------------------------------

CONFIG_PATH = os.environ.get("DIOFINDER_CONFIG", "/etc/diofinder/diofinder.conf")

_CONFIG_SECTIONS = [
    ("Camera", [
        ("frame_width",               "Frame width",          "Sensor ROI width in pixels."),
        ("frame_height",              "Frame height",         "Sensor ROI height in pixels."),
        ("camera_tuning_file",        "Tuning file",          "libcamera tuning profile; the IMX477 scientific profile disables ISP processing that corrupts photometry."),
        ("exposure_s",                "Exposure (s)",         "Exposure time per frame."),
        ("gain",                      "Gain",                 "Analog gain. Higher = more sensitive but noisier."),
        ("auto_exposure_enabled",     "Auto-exposure",        "Adjust exposure toward the target star count (toggle on Camera page)."),
        ("auto_exposure_target_stars","Target stars",         "Desired star count when auto-exposure is on."),
        ("auto_exposure_min_s",       "Auto-exp min (s)",     "Minimum exposure floor."),
        ("auto_exposure_max_s",       "Auto-exp max (s)",     "Maximum exposure ceiling."),
        ("sensor_full_width",         "Sensor full width",    "Full sensor readout width (forces full-FOV ISP downscale)."),
        ("sensor_full_height",        "Sensor full height",   "Full sensor readout height."),
    ]),
    ("Optics / FOV", [
        ("fov_deg",                      "Field of view (°)",   "Horizontal FOV. Self-calibrates from solved frames."),
        ("arcsec_per_pixel",             "Plate scale (\"/px)", "Arcseconds per pixel. Used for display."),
        ("distortion",                   "Distortion",          "Barrel/pincushion coefficient. 0 = fit per-solve."),
        ("fov_calibrated",               "FOV calibrated",      "True once calibrator converged."),
        ("fov_calibrated_stddev",        "Cal. stddev (°)",     "Stddev threshold for declaring FOV stable."),
        ("fov_calibrated_max_error_deg", "Cal. FOV tolerance",  "FOV search window after calibration."),
        ("fov_max_error_deg",            "Uncal. FOV tolerance","FOV search window before calibration."),
    ]),
    ("Observer Location", [
        ("latitude_deg",  "Latitude (°)",  "Observer latitude (+N)."),
        ("longitude_deg", "Longitude (°)", "Observer longitude (+E)."),
    ]),
    ("Star Detection", [
        ("detect_sigma", "Detection sigma",
         "Threshold in units of background sigma passed to sycamore star_detect."),
        ("detect_bin",                "Detection binning",    "1 = full-res, 2 = 2x2-binned (restart to apply)."),
        ("detect_kernel_sigma",       "Kernel sigma",         "Matched-filter PSF width (sycamore >= 0.12); wider for bad seeing."),
        ("detect_max_axis_ratio",     "Max axis ratio",       "Trail rejection; 0 = off, else 1.5–10.0."),
        ("detect_local_noise",        "Local noise",          "Per-window noise estimate in the matched filter (sycamore >= 0.12)."),
        ("detect_bg_mode",            "Background mode",      "Per-frame background compensation (see Background page)."),
        ("detect_tophat_radius",      "Top-hat radius",       "Structuring-element radius for top_hat mode."),
        ("detect_bg_block_size",      "Block size",           "Tile side for block_percentile; 0 = sycamore default."),
        ("detect_uniform_filter_size","Uniform window",       "Window side for uniform_mean; 0 = sycamore default."),
        ("detect_noise_mode",         "Noise estimator",      "mad (robust) or global_rms (tetra3-compatible)."),
        ("bg_cache_enabled",          "Temporal cache",       "Median-stack recent frames into a background model."),
        ("bg_cache_stack",            "Cache stack",          "Frames median-stacked per rebuild."),
        ("bg_cache_refresh_s",        "Cache refresh (s)",    "Minimum interval between rebuilds."),
        ("bg_cache_slew_deg",         "Cache slew (deg)",     "IMU angle that invalidates the cache."),
        ("bg_cache_max_age_s",        "Cache max age (s)",    "Rebuild if the model is older than this."),
    ]),
    ("Seeing", [
        ("seeing_mode",  "Seeing mode",  "Good/Bad night preset (toggle above)."),
        ("star_db_deep", "Deep database", "Optional deeper-magnitude database for the Light-pollution preset; empty = unset."),
    ]),
    ("Plate Solving (olive-solve)", [
        ("solver_db",        "Star database",      "Path to a tetra3 .npz database compatible with olive-solve."),
        ("min_centroids",    "Min stars",          "Minimum detected stars required to attempt a solve."),
        ("max_solve_stars",           "Max solve stars",      "Cap on centroids passed to the solver."),
        ("solve_timeout_ms", "Solve timeout (ms)", "Hard timeout per solve attempt."),
        ("match_threshold",  "Match threshold",    "Max false-positive probability (1e-5 default)."),
        ("match_radius",     "Match radius",       "Max centroid-catalog distance as fraction of FOV."),
    ]),
    ("Aim point", [
        ("boresight_x", "Aim point X (px)", "Telescope optical-axis X in pixels."),
        ("boresight_y", "Aim point Y (px)", "Telescope optical-axis Y in pixels."),
    ]),
    ("Communications (LX200)", [
        ("lx200_port",             "LX200 port",         "TCP port for the LX200 server."),
        ("lx200_client_timeout_s", "Client timeout (s)", "Disconnect idle LX200 clients."),
    ]),
    ("CPU Affinity", [
        ("cpu_camera", "Camera CPU",  "Core for camera_proc. Also shared with solver rayon threads."),
        ("cpu_solver", "Solver CPU",  "Primary core for solver_proc; rayon also uses cpu_camera."),
        ("cpu_solver_aux", "Solver aux CPU", "Third solver core (freed by comms moving to CPU 0)."),
        ("cpu_comms",  "Comms CPU",   "Core for comms_proc, web UI, and IMU thread (shares CPU 0 with the kernel)."),
    ]),
    ("Watchdog", [
        ("watchdog_enabled",   "Watchdog",          "Restart the service if the solver stops publishing."),
        ("watchdog_timeout_s", "Watchdog timeout (s)", "Staleness before the solver is considered hung."),
    ]),
    ("Diagnostics", [
        ("save_solved_frames",     "Save solved frames", "Write PNG for every successful solve."),
        ("save_failed_frames",     "Save failed frames", "Write PNG for every failed solve."),
        ("failed_frames_dir",      "Captures dir",       "Directory for saved frame PNGs."),
        ("log_solve_stats_every_n","Log stats every N",  "Print solve performance stats every N solves."),
    ]),
    ("Shutdown", [
        ("shutdown_grace_s", "Grace period (s)", "Seconds between SIGTERM and SIGKILL."),
    ]),
]


def _fmt_val(v):
    """Format a config value for display (booleans → lowercase, floats → %g)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


@app.route("/config")
def config_page():
    """Config viewer: all settings with their defaults and current runtime values."""
    from diofinder.config import Config, load_config as _load_config
    defaults = Config()
    cfg_ok    = True
    cfg_error = None
    try:
        cfg = _load_config()
    except Exception as e:
        cfg       = defaults
        cfg_ok    = False
        cfg_error = str(e)

    sections = []
    for section_name, keys in _CONFIG_SECTIONS:
        rows = []
        for key, label, description in keys:
            val         = getattr(cfg,      key, None)
            default_val = getattr(defaults, key, None)
            rows.append({
                "key":         key,
                "label":       label,
                "description": description,
                "value":       _fmt_val(val)         if val         is not None else "",
                "default":     _fmt_val(default_val) if default_val is not None else "",
                "modified":    val != default_val,
            })
        sections.append({"name": section_name, "rows": rows})

    rt = _safe_call("status")
    runtime = rt.result if rt.ok else None
    seeing = _safe_call("seeing_get")
    sp = _safe_call("solver_params_get")
    mp = _safe_call("match_params_get")

    return render_template(
        "config.html",
        path=CONFIG_PATH,
        sections=sections,
        seeing=(seeing.result if seeing.ok else None),
        solver_params=(sp.result if sp.ok else None),
        match_params=(mp.result if mp.ok else None),
        cfg_ok=cfg_ok,
        cfg_error=cfg_error,
        daemon_ok=rt.ok,
        runtime_test_mode=(runtime.get("test_mode")  if runtime else None),
        runtime_imu=(runtime.get("imu")              if runtime else None),
        runtime_fov=(runtime.get("fov_deg")          if runtime else None),
        runtime_boresight=(runtime.get("boresight")  if runtime else None),
        runtime_lat=cfg.latitude_deg,
        runtime_lon=cfg.longitude_deg,
    )


# ---- Live frame view --------------------------------------------------------

# Boresight rarely changes (only on alignment).  Caching it avoids a
# synchronous maint-socket round-trip on every /frame.jpg request.
_bs_cache: dict = {"cx": None, "cy": None, "ts": 0.0}
_BS_CACHE_TTL = 5.0  # seconds

# Boresight reticle: three rings whose DIAMETERS are 0.5deg / 2deg / 4deg — the
# classic Telrad pattern. All three are angular (radius_px = half-diameter in
# arcsec / arcsec_per_pixel), so they read as true on-sky rulers regardless of
# the calibrated plate scale. The 5px-FWHM focus circle is unrelated (Focus page).
_RETICLE_RING_DIAMETERS_DEG = (0.5, 2.0, 4.0)
# Fixed-pixel fallback radii (~0.5/2/4deg diameter at the nominal 50.8"/px), used
# only if the plate scale is missing/invalid.
_RETICLE_FALLBACK_RADII_PX = (18, 71, 142)


def _draw_boresight_reticle(draw, cx, cy, arcsec_per_pixel, ds=1):
    """Draw the Telrad-style boresight reticle (three angular rings + a centre
    crosshair) onto an ImageDraw at (cx, cy). ``ds`` is the image downsample
    factor so radii scale with a half-res render. Ring radii come from
    ``_RETICLE_RING_DIAMETERS_DEG`` via ``arcsec_per_pixel``; a fixed-pixel
    fallback is used if the scale is missing/invalid. Shared by /frame.jpg and
    the debug-bundle display JPGs so the two never drift."""
    try:
        aps = float(arcsec_per_pixel)
        if aps <= 0:
            raise ValueError
        radii = [max(1, int(round(d * 3600.0 / 2.0 / aps)) // ds)
                 for d in _RETICLE_RING_DIAMETERS_DEG]
    except Exception:
        radii = [max(1, r // ds) for r in _RETICLE_FALLBACK_RADII_PX]
    r_inner = radii[0]
    # Innermost ring is brighter/thicker — it marks the boresight; the crosshair
    # ticks flank it so the exact centre is unambiguous at a glance.
    draw.ellipse([cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner],
                 outline=(255, 80, 80), width=2)
    gap = 6
    draw.line([cx - r_inner - gap, cy, cx - r_inner - 1, cy], fill=(255, 120, 120), width=1)
    draw.line([cx + r_inner + 1,   cy, cx + r_inner + gap, cy], fill=(255, 120, 120), width=1)
    draw.line([cx, cy - r_inner - gap, cx, cy - r_inner - 1], fill=(255, 120, 120), width=1)
    draw.line([cx, cy + r_inner + 1,   cx, cy + r_inner + gap], fill=(255, 120, 120), width=1)
    for rr in radii[1:]:
        draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                     outline=(255, 120, 120), width=1)


@app.route("/frame.jpg")
def frame_jpg():
    """Serve the current camera frame as a histogram-stretched JPEG with boresight overlay."""
    import time as _time
    import numpy as np
    from multiprocessing import shared_memory, resource_tracker as _rt
    from PIL import Image, ImageDraw

    try:
        ecfg = _load_cfg_cached()
        width, height = ecfg.frame_width, ecfg.frame_height
    except Exception:
        ecfg, width, height = None, 960, 760

    now = _time.monotonic()
    if now - _bs_cache["ts"] > _BS_CACHE_TTL or _bs_cache["cx"] is None:
        bs_r = _safe_call("status", timeout=0.5)
        bs   = bs_r.result.get("boresight") if bs_r.ok and bs_r.result else None
        if bs:
            _bs_cache["cx"] = int(round(bs["x"]))
            _bs_cache["cy"] = int(round(bs["y"]))
            _bs_cache["ts"] = now
    cx = _bs_cache["cx"] if _bs_cache["cx"] is not None else width  // 2
    cy = _bs_cache["cy"] if _bs_cache["cy"] is not None else height // 2

    # Live view: direct display-SHM read (P4), falling back to frame_get.
    frame, _seq, _synced = _live_frame(timeout=3.0)
    if frame is None:
        return "camera not running", 503, {"Content-Type": "text/plain"}

    # Render at half resolution by default: the stretch (percentile passes)
    # and JPEG encode are ~4x cheaper and the result is indistinguishable on
    # a phone/laptop view. ?full=1 returns native resolution. This format may
    # evolve (e.g. quality/scale knobs) as the UI grows.
    ds = 1 if request.args.get("full") in ("1", "true", "yes", "on") else 2
    work = frame[::ds, ::ds] if ds > 1 else frame

    if request.args.get("sub") in ("1", "true", "yes", "on"):
        # Detection view: a cheap per-row-median flat-field subtraction so the
        # live overlay shows stars popping on a flat background — a rough,
        # mode-agnostic visualization for the status page (polled, downsampled).
        # The accurate, mode-specific, temporal-median-aware A/B lives on the
        # Background page (the solver bg_preview op); this overlay deliberately
        # does NOT reimplement the detector's per-mode background.
        row_med = np.median(work.astype(np.float32), axis=1)[:, None]
        signal = np.clip(work.astype(np.float32) - row_med, 0.0, None)
    else:
        sky     = float(np.percentile(work, 50))
        signal  = np.clip(work.astype(np.float32) - sky, 0.0, None)
    white   = max(float(np.percentile(signal, 99.9)), 20.0)
    stretched = np.clip(signal / white * 255.0, 0, 255).astype(np.uint8)

    # Overlay geometry in downsampled coordinates.
    cx //= ds
    cy //= ds
    img  = Image.fromarray(stretched, mode="L").convert("RGB")
    draw = ImageDraw.Draw(img)
    _draw_boresight_reticle(
        draw, cx, cy, getattr(ecfg, "arcsec_per_pixel", None), ds=ds)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    buf.seek(0)
    return (
        buf.read(), 200,
        {"Content-Type": "image/jpeg",
         "Cache-Control": "no-store, no-cache"},
    )


# ---- Focus ------------------------------------------------------------------

_focus_lock  = threading.Lock()
_focus_state = {
    "score":           None,
    "session_max":     None,
    "committed_score": None,
    "cx": None, "cy": None,
}


def _read_focus_data():
    """Read one frame and compute a Laplacian-variance focus score around the brightest star."""
    import numpy as np
    from multiprocessing import shared_memory, resource_tracker as _rt
    from scipy.ndimage import laplace as nd_laplace
    from PIL import Image

    try:
        from diofinder.config import load_config
        ecfg = load_config()
        width, height = ecfg.frame_width, ecfg.frame_height
    except Exception:
        width, height = 960, 760

    frame, _seq, _synced = _daemon_frame(timeout=3.0)
    if frame is None:
        return None

    HALF   = 30
    search = frame[HALF:height - HALF, HALF:width - HALF]
    idx    = np.unravel_index(search.argmax(), search.shape)
    cy     = int(idx[0]) + HALF
    cx     = int(idx[1]) + HALF
    patch  = frame[cy - HALF:cy + HALF, cx - HALF:cx + HALF].astype(np.float32)
    score  = float(nd_laplace(patch).var())

    patch_img = Image.fromarray(
        patch.clip(0, 255).astype(np.uint8), mode="L")
    patch_img = patch_img.resize(
        (patch_img.width * 4, patch_img.height * 4),
        resample=Image.NEAREST,
    )
    buf = io.BytesIO()
    patch_img.save(buf, format="JPEG", quality=85)
    patch_bytes = buf.getvalue()

    return {"score": score, "cx": cx, "cy": cy,
            "patch_bytes": patch_bytes}


@app.route("/focus")
def focus_page():
    """Focus assistant page."""
    with _focus_lock:
        committed = _focus_state["committed_score"]
    exposure = _safe_call("exposure_get")
    return render_template(
        "focus.html",
        committed_score=committed,
        exposure=(exposure.result if exposure.ok else None),
    )


@app.route("/api/focus")
def api_focus():
    """JSON focus score and session maximum for the focus page's live polling."""
    data = _read_focus_data()
    if data is None:
        return jsonify({"error": "camera not running"}), 503
    score = data["score"]
    with _focus_lock:
        _focus_state["score"] = score
        _focus_state["cx"]    = data["cx"]
        _focus_state["cy"]    = data["cy"]
        _focus_state["patch_bytes"] = data["patch_bytes"]
        prev_max = _focus_state["session_max"]
        if prev_max is None or score > prev_max:
            _focus_state["session_max"] = score
        session_max = _focus_state["session_max"]
    pct = int(round(score / session_max * 100)) if session_max else 0
    return jsonify({
        "score":       round(score, 1),
        "session_max": round(session_max, 1) if session_max else 0,
        "pct":         pct,
    })


@app.route("/focus/patch.jpg")
def focus_patch_jpg():
    """Serve a 4× zoomed JPEG crop of the brightest star for the focus assistant."""
    with _focus_lock:
        patch_bytes = _focus_state.get("patch_bytes")
    if patch_bytes is None:
        data = _read_focus_data()
        if data is None:
            return "camera not running", 503
        patch_bytes = data["patch_bytes"]
        with _focus_lock:
            _focus_state["patch_bytes"] = patch_bytes
    return (patch_bytes, 200,
            {"Content-Type": "image/jpeg",
             "Cache-Control": "no-store, no-cache"})


@app.route("/focus/commit", methods=["POST"])
def focus_commit():
    """Save the current focus score as the committed reference and redirect to dashboard."""
    with _focus_lock:
        _focus_state["committed_score"] = _focus_state.get("score")
    return redirect(url_for("dashboard"))


@app.route("/focus/reset", methods=["POST"])
def focus_reset():
    """Reset the session-maximum focus score (returns 204 No Content)."""
    with _focus_lock:
        _focus_state["session_max"] = None
    return ("", 204)


# ---- Health -----------------------------------------------------------------

@app.route("/debug/collect", methods=["POST"])
def debug_collect():
    """Build and return a timestamped debug ZIP (frames + config + journal + status)."""
    import numpy as np
    from multiprocessing import shared_memory

    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    zip_name = f"diofinder_debug_{ts}.zip"

    try:
        from diofinder.config import load_config as _lcfg
        ecfg = _lcfg()
        W, H = ecfg.frame_width, ecfg.frame_height
        arcsec_px = ecfg.arcsec_per_pixel
    except Exception:
        W, H, arcsec_px = 960, 760, 50.8

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:

        # ── Config file ──────────────────────────────────────────────────────
        conf_path = os.environ.get("DIOFINDER_CONFIG", "/etc/diofinder/diofinder.conf")
        try:
            zf.write(conf_path, "diofinder.conf")
        except Exception as e:
            zf.writestr("diofinder.conf", f"# could not read: {e}\n")

        # ── Daemon status (JSON) ─────────────────────────────────────────────
        status = _safe_call("status")
        cal    = _safe_call("calibration_status")
        try:
            zf.writestr("status.json", json.dumps({
                "status":      {"ok": status.ok, "result": status.result,
                                "error": status.error},
                "calibration": {"ok": cal.ok,    "result": cal.result,
                                "error": cal.error},
            }, indent=2, default=str))
        except Exception as e:
            zf.writestr("status.json", json.dumps({"error": str(e)}))

        # ── Effective runtime conditions (so the bundle fully reproduces a
        #    live solve) ──────────────────────────────────────────────────────
        # The raw frame PNGs carry none of the detection/solve conditions the
        # live solver applied; diofinder.conf only carries the persisted file,
        # not live shared_cfg overrides or the calibrated (vs. loose) FOV
        # tolerance. Dump the *effective* knobs so diag_solve.py --match-runtime
        # can re-create the exact pipeline on another Pi Zero.
        try:
            sp  = _safe_call("solver_params_get")
            mp  = _safe_call("match_params_get")
            bgc = _safe_call("bg_cache_status")
            see = _safe_call("seeing_get")
            ver = _safe_call("version")
            hpx = _safe_call("hot_pixel_status")
            trk = _safe_call("tracking_status")
            calres = cal.result if cal.ok and cal.result else {}
            eff = {
                # Release + wheel versions: the wheels update independently of
                # the code, and a stale olive-solve wheel has masqueraded as an
                # application regression before. Now every bundle records them.
                "version":       ver.result if ver.ok else {"error": ver.error},
                # Mask repair rewrites pixels BEFORE detection; a poisoned
                # mask (195k px, uncapped capture) made every solve fail
                # while being invisible in the bundle. Record it always.
                "hot_pixel":     hpx.result if hpx.ok else {"error": hpx.error},
                "solver_params": sp.result if sp.ok else {"error": sp.error},
                "match_params":  mp.result if mp.ok else {"error": mp.error},
                "bg_cache":      bgc.result if bgc.ok else {"error": bgc.error},
                "seeing":        see.result if see.ok else {"error": see.error},
                # Tracking state machine + failure counters (audit F-L6): a
                # FULL/TRACKING flap is invisible without these.
                "tracking":      trk.result if trk.ok else {"error": trk.error},
                # The FOV estimate + tolerance ACTUALLY used by the live solve
                # (calibrated tightens fov_max_error well below the loose conf
                # value). These are the usual runtime-vs-diagnostic mismatch.
                "fov_estimate_deg":   calres.get("committed_fov") or calres.get("fov_estimate"),
                "fov_max_error_deg":  calres.get("fov_max_error_deg"),
                "fov_calibrated":     calres.get("calibrated"),
            }
            zf.writestr("effective_params.json",
                        json.dumps(eff, indent=2, default=str))
        except Exception as e:
            zf.writestr("effective_params.json", json.dumps({"error": str(e)}))

        # ── Journal ──────────────────────────────────────────────────────────
        try:
            j = subprocess.run(
                ["journalctl", "-u", "diofinder", "-n", "300", "--no-pager"],
                capture_output=True, text=True, timeout=10,
            )
            zf.writestr("journal.txt", j.stdout + (j.stderr or ""))
        except Exception as e:
            zf.writestr("journal.txt", f"error collecting journal: {e}\n")

        # ── Camera frames from SHM ───────────────────────────────────────────
        from PIL import Image, ImageDraw
        try:
            from diofinder.frame_slots import SHM_PREFIX, NUM_BUFFERS
        except Exception:
            SHM_PREFIX, NUM_BUFFERS = "diofinder_frame", 3

        bs_r = _safe_call("status")
        bs   = bs_r.result.get("boresight") if bs_r.ok and bs_r.result else None
        cx   = int(round(bs["x"])) if bs else W // 2
        cy   = int(round(bs["y"])) if bs else H // 2

        _burst = {"last_seq": -1, "synced": True}

        def _capture_frame():
            # Chain after_seq: the burst is then STRICTLY CONSECUTIVE
            # solver frames (the temporal-cache reconstruction premise)
            # and can never be torn. Direct-SHM fallback (daemon down) is
            # labeled unsynced in the bundle metadata.
            mo = {}
            fr, seq, synced = _daemon_frame(after_seq=_burst["last_seq"],
                                            meta_out=mo)
            if fr is not None and synced:
                _burst["last_seq"] = seq
            elif fr is not None:
                _burst["synced"] = False
            return fr, (seq if synced else None), mo.get("meta")

        def _solution_for_seq(seq, budget_s):
            """Poll status until the published solution is for THIS frame
            (matching seq) — the exact per-frame solve result, not the
            previous frame's. Returns the solution dict or None (older
            daemon without seq tagging, or the solve never landed)."""
            import time as _t
            deadline = _t.monotonic() + budget_s
            while _t.monotonic() < deadline:
                sr = _safe_call("status", timeout=1.0)
                sol = (sr.result or {}).get("solution") if sr.ok else None
                if sol and sol.get("seq") is not None:
                    if int(sol["seq"]) >= int(seq):
                        # ">" means the solver already moved on and this
                        # frame's result was overwritten — report honestly.
                        return sol if int(sol["seq"]) == int(seq) else None
                elif sol is not None:
                    return None      # daemon predates seq tagging
                _t.sleep(0.1)
            return None

        def _save_frame_pair(zf, idx, frame, with_display=True):
            # Raw grayscale PNG — the solver's food; saved for EVERY frame so an
            # offline diag_solve.py --bundle can measure a real solve rate and
            # reconstruct the temporal background cache (needs >= the cache stack
            # size of consecutive frames).
            raw_buf = io.BytesIO()
            Image.fromarray(frame, mode="L").save(raw_buf, format="PNG")
            zf.writestr(f"frame_{idx:02d}_raw.png", raw_buf.getvalue())
            # Arcsinh-stretched display JPEG with boresight overlay. Large and
            # human-only, so only the first few frames get one (keeps the bundle
            # email-friendly).
            if not with_display:
                return
            sky   = float(np.median(frame))
            x     = np.clip(frame.astype(np.float32) - sky, 0.0, None)
            beta  = max(1.0, sky * 0.1)
            xs    = np.arcsinh(x / beta)
            scale = float(np.percentile(xs, 99.9)) or float(xs.max()) or 1.0
            disp  = np.clip(xs / scale * 255.0, 0, 255).astype(np.uint8)
            img   = Image.fromarray(disp, mode="L").convert("RGB")
            draw  = ImageDraw.Draw(img)
            _draw_boresight_reticle(draw, cx, cy, arcsec_px, ds=1)
            disp_buf = io.BytesIO()
            img.save(disp_buf, format="JPEG", quality=85)
            zf.writestr(f"frame_{idx:02d}_display.jpg", disp_buf.getvalue())

        # How many frames to grab. A burst (not a single snapshot) is what makes
        # the bundle diagnosable offline: solving is stochastic frame-to-frame,
        # so a real solve RATE needs several frames, and replaying the sycamore
        # temporal cache needs >= bg_cache_stack consecutive frames. Default 12
        # (> the default stack of 8, with margin); override with ?frames=N.
        try:
            n_frames = int(request.args.get("frames")
                           or request.form.get("frames") or 12)
        except (ValueError, TypeError):
            n_frames = 12
        n_frames = max(1, min(30, n_frames))
        # Cap the display JPGs (large, human-only) regardless of frame count.
        n_display = min(2, n_frames)

        frames_saved = 0
        frame_imu = []    # per-frame IMU snapshot, sampled at capture time
        frames_map = {}   # filename -> exact per-frame record (frames.json)
        from diofinder import frame_meta as _fm
        import time as _time, hashlib
        seen_hashes = set()
        # Bounded wall-clock budget so a slow camera (long exposure) can't hang
        # the download button. Each iteration costs ~2x per_frame_s -- the
        # after_seq wait for a genuinely new frame PLUS the per-frame solution
        # poll (v0.11.46) -- so the budget is sized to that, not to one exposure
        # period. The old n_frames*per_frame_s+5 budget assumed ~1 period/frame
        # and ran out at ~5-6 of 12 frames once the solve poll was added (and a
        # redundant trailing sleep doubled the real per-frame cost again).
        per_frame_s = max(0.3, ecfg.exposure_s + 0.2)
        deadline = _time.monotonic() + min(
            90.0, n_frames * (2.0 * per_frame_s + 1.5) + 5.0)
        while frames_saved < n_frames and _time.monotonic() < deadline:
            f, f_seq, f_meta = _capture_frame()
            advanced = False
            if f is not None:
                # Dedup identical SHM reads (slow frame rates re-read one slot);
                # only genuinely new frames advance the burst.
                h = hashlib.md5(f.tobytes()).digest()
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    idx = frames_saved + 1
                    try:
                        _save_frame_pair(zf, idx, f, with_display=(idx <= n_display))
                        frames_saved += 1
                        advanced = True
                        # This frame's OWN solve result (matched by seq), not
                        # the previous frame's — poll while the PNG for the
                        # next frame would otherwise just be sleeping.
                        sol = (_solution_for_seq(f_seq, budget_s=per_frame_s + 1.0)
                               if f_seq is not None else None)
                        # Snapshot the IMU output as close to this frame as we
                        # can (maint round-trip; IMU runs at 20 Hz).
                        si = _safe_call("status")
                        rec = {
                            "frame": f"frame_{idx:02d}",
                            "wall_time": datetime.now().isoformat(timespec="milliseconds"),
                            "seq": f_seq,
                            "imu": (si.result.get("imu")
                                    if si.ok and si.result else None),
                        }
                        frame_imu.append(rec)
                        fname = f"frame_{idx:02d}_raw.png"
                        entry = {
                            "seq": f_seq,
                            "saved_at": rec["wall_time"],
                            "capture": f_meta,           # raw sensor metadata
                            "imu": rec["imu"],
                        }
                        # Wall-clock exposure timing derived from the sensor's
                        # own per-frame metadata (SensorTimestamp).
                        entry.update(_fm.derive_times(f_meta))
                        if sol is not None:
                            entry["solution"] = {
                                k: sol.get(k) for k in (
                                    "solved", "status", "ra_deg", "dec_deg",
                                    "roll_deg", "fov_deg", "stars", "matches",
                                    "peak", "solve_ms", "seq")
                                if k in sol}
                        else:
                            entry["solution"] = None
                        frames_map[fname] = entry
                    except Exception:
                        pass
            if not advanced and frames_saved < n_frames:
                # No new frame this pass (a dup SHM re-read, or the unsynced
                # direct-SHM fallback where after_seq can't pace us): back off
                # so we don't busy-loop. When synced, _capture_frame's after_seq
                # already blocks until the next frame, so a just-saved frame
                # needs no extra sleep -- that redundant sleep is what halved
                # the burst count.
                _time.sleep(per_frame_s)

        # ── System summary ───────────────────────────────────────────────────
        lines = [
            f"diofinder debug bundle",
            f"collected: {datetime.now().isoformat(timespec='seconds')}",
            "=" * 52,
        ]
        try:
            lines.append("hostname: " +
                subprocess.check_output(["hostname"], text=True).strip())
        except Exception:
            pass
        try:
            with open("/etc/os-release") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME"):
                        lines.append("OS: " + line.split("=", 1)[1].strip().strip('"'))
                        break
        except Exception:
            pass
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("Model"):
                        lines.append("hardware: " + line.split(":", 1)[1].strip())
                        break
        except Exception:
            pass
        lines.append(f"frames_in_bundle: {frames_saved}")
        # False = at least one frame came from the direct-SHM fallback
        # (daemon down): frames may be torn / non-consecutive.
        lines.append(f"frames_synced: {_burst['synced']}")
        if status.ok and status.result:
            r = status.result
            lines.append(f"test_mode: {r.get('test_mode')}")
            lines.append(f"fov_deg: {r.get('fov_deg')}")
            sol = r.get("solution") or {}
            lines.append(f"last_solve_status: {sol.get('status')}")
            lines.append(f"last_solve_ms: {sol.get('solve_ms')}")
            imu = r.get("imu") or {}
            lines.append(f"imu_active: {imu.get('active')}")
        # Per-frame IMU output (quaternion + post-solve attitude reference),
        # so the bundle records the actual attitude at each frame's capture.
        if frame_imu:
            lines.append("-" * 52)
            for rec in frame_imu:
                imu = rec.get("imu") or {}
                lines.append(
                    f"{rec['frame']} @ {rec['wall_time']}  "
                    f"available={imu.get('available')} active={imu.get('active')} "
                    f"q={imu.get('q')} age_s={imu.get('age_s')} "
                    f"ref(ra={imu.get('ref_ra_deg')},dec={imu.get('ref_dec_deg')},"
                    f"roll={imu.get('ref_roll_deg')})")
        zf.writestr("capture_info.txt", "\n".join(lines) + "\n")
        # Structured per-frame IMU for offline analysis / replay.
        zf.writestr("imu.json", json.dumps(frame_imu, indent=2))
        # frames.json: filename -> exact per-frame record — seq, the sensor's
        # own capture metadata (SensorTimestamp/actual exposure/actual gain),
        # derived wall-clock exposure_start_utc, and THIS frame's solve
        # result (matched by seq). The authoritative frame->data map;
        # tests/bundle_solve.py merges/refreshes it offline.
        zf.writestr("frames.json", json.dumps(frames_map, indent=2))

    # Save a copy to disk so scp/curl also works
    out_dir = pathlib.Path("/var/lib/diofinder")
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / zip_name
    try:
        zip_path.write_bytes(buf.getvalue())
    except Exception:
        pass

    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=zip_name,
    )


_BGRUN_DIR = pathlib.Path("/var/lib/diofinder/bg_runs")
_bgrun_lock = threading.Lock()
_bgrun = {
    "running": False, "phase": "idle", "progress": 0, "message": "",
    "n_frames": 0, "table": None, "zip_name": None, "error": None,
    "started": None, "finished": None,
}


def _bgrun_set(**kw):
    with _bgrun_lock:
        _bgrun.update(kw)


def _bgrun_snapshot():
    with _bgrun_lock:
        return dict(_bgrun)


def _bgrun_capture(frames_dir, n_target, max_seconds):
    """Grab up to n_target distinct SHM frames, saving each as a PNG.
    Returns (list_of_frames, peak). Skips frames identical to the previous one
    so the per-mode comparison isn't run on duplicates."""
    import time as _t
    import numpy as np
    from multiprocessing import shared_memory, resource_tracker as _rt
    from PIL import Image
    from diofinder.config import load_config
    from diofinder.frame_slots import SHM_PREFIX, NUM_BUFFERS

    ecfg = load_config()
    W, H = ecfg.frame_width, ecfg.frame_height

    _cap = {"last_seq": -1}

    def read_latest():
        # frame_get with after_seq chaining: distinct, untorn, consecutive
        # frames (replaces the identical-frame dedupe below as the primary
        # freshness mechanism; the dedupe stays for the SHM fallback path).
        fr, seq, synced = _daemon_frame(after_seq=_cap["last_seq"])
        if fr is not None and synced:
            _cap["last_seq"] = seq
        return fr

    frames_dir.mkdir(parents=True, exist_ok=True)
    frames, peak, last = [], 0, None
    t0 = _t.monotonic()
    while len(frames) < n_target and (_t.monotonic() - t0) < max_seconds:
        fr = read_latest()
        if fr is None:
            _t.sleep(0.3)
            continue
        if last is not None and np.array_equal(fr, last):
            _t.sleep(0.12)
            continue
        last = fr
        peak = max(peak, int(fr.max()))
        Image.fromarray(fr, mode="L").save(str(frames_dir / f"frame_{len(frames):02d}.png"))
        frames.append(fr)
        _bgrun_set(n_frames=len(frames),
                   progress=int(5 + 35 * len(frames) / max(1, n_target)),
                   message=f"captured {len(frames)}/{n_target} frames (peak={peak})")
        _t.sleep(0.25)
    return frames, peak


def _bgrun_agreement(base, other, tol=1.5):
    """(base_only, mode_only) star counts vs the row_percentile baseline."""
    import numpy as np
    B = np.array([(x, y) for (x, y, *_) in (base or [])]) if base else np.zeros((0, 2))
    O = np.array([(x, y) for (x, y, *_) in (other or [])]) if other else np.zeros((0, 2))
    used = set()
    matched = 0
    for bx, by in B:
        if len(O) == 0:
            break
        d2 = (O[:, 0] - bx) ** 2 + (O[:, 1] - by) ** 2
        for j in np.argsort(d2):
            j = int(j)
            if j in used:
                continue
            if d2[j] <= tol * tol:
                used.add(j)
                matched += 1
            break
    return len(B) - matched, len(O) - matched


def _bgrun_solve(raw, max_c, min_c):
    """Solve one frame's centroids on the live daemon. raw is star_detect's
    [(x, y, ...), ...]; the solver wants [[row, col], ...] = [[y, x], ...]."""
    if not raw or len(raw) < min_c:
        return False, 0
    cents = [[float(s[1]), float(s[0])] for s in raw[:max_c]]
    r = _safe_call("solve_centroids", {"centroids": cents}, timeout=25.0)
    if r.ok and r.result and r.result.get("solved"):
        return True, int(r.result.get("matches", 0) or 0)
    return False, 0


def _bgrun_worker(n_frames, max_seconds, solve_frames):
    import time as _t
    import inspect as _inspect
    try:
        import numpy as np
        import star_detect as sd
        from diofinder.config import load_config
        cfg = load_config()
        # Use the LIVE effective detection params (shared_cfg over the config
        # file) so "A/B matches" reflects what the live solver actually does —
        # not a stale config-file sigma. Falls back to the file if the daemon
        # is unreachable.
        sp = _safe_call("solver_params_get")
        spd = sp.result if sp.ok and sp.result else {}
        sigma        = float(spd.get("detect_sigma", cfg.detect_sigma))
        kernel_sigma = float(spd.get("detect_kernel_sigma",
                                     getattr(cfg, "detect_kernel_sigma", 1.5)))
        noise_mode   = spd.get("detect_noise_mode", cfg.detect_noise_mode)
        _mar         = float(spd.get("detect_max_axis_ratio",
                                     getattr(cfg, "detect_max_axis_ratio", 0.0)))
        max_axis_ratio = float("inf") if _mar <= 0.0 else _mar
        backend      = str(spd.get("extractor_backend",
                                   getattr(cfg, "extractor_backend", "sycamore")))
        det_bin = cfg.detect_bin          # restart-only -> file value == live
        th_radius = cfg.detect_tophat_radius
        block_size = getattr(cfg, 'detect_bg_block_size', 0) or 32
        uniform_size = getattr(cfg, 'detect_uniform_filter_size', 0) or 25
        max_c = cfg.max_solve_stars
        min_c = cfg.min_centroids
        ex = _safe_call("exposure_get")
        exd = ex.result if ex.ok and ex.result else {}
        exposure_s = exd.get("exposure_s")
        gain = exd.get("gain")
        # The sweep extracts with sycamore; when the live solver is on the
        # tetra3/Legacy backend this A/B does NOT represent it (tetra3 ignores
        # bg_mode and the matched filter). Flag it loudly.
        backend_warn = (
            "NOTE: live extractor_backend=tetra3 (Legacy) — this A/B uses "
            "sycamore and does NOT reflect the live pipeline. Use "
            "diag_solve.py --bundle to evaluate Legacy." if backend == "tetra3"
            else "")
        try:
            sd.set_num_threads(2)
        except Exception:
            pass
        _params = _inspect.signature(sd.detect_stars).parameters
        has_tophat = "tophat_radius" in _params
        has_kernel = "kernel_sigma" in _params
        has_noise  = "noise_mode" in _params
        has_mar    = "max_axis_ratio" in _params
        modes = ["row_percentile", "column_percentile", "row_column_percentile",
                 "line_median", "block_percentile", "uniform_mean"]
        if has_tophat:
            modes.append("top_hat")

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = _BGRUN_DIR / ts
        frames_dir = run_dir / "frames"

        _bgrun_set(phase="capturing", progress=5, message="capturing frames…")
        frames, peak = _bgrun_capture(frames_dir, n_frames, max_seconds)
        if not frames:
            raise RuntimeError("no frames captured — is the camera running?")
        if peak < 20:
            _bgrun_set(message=f"warning: dim frames (peak={peak}); results may be poor")

        def extract(frame, mode):
            # Match the live sycamore detection knobs (kernel/noise/trail),
            # capability-probed so it still runs on older wheels.
            kw = dict(sigma=sigma, bin=det_bin, centroid_full_res=True, bg_mode=mode)
            if has_kernel:
                kw["kernel_sigma"] = kernel_sigma
            if has_noise:
                kw["noise_mode"] = noise_mode
            if has_mar and max_axis_ratio != float("inf"):
                kw["max_axis_ratio"] = max_axis_ratio
            if mode == "top_hat" and has_tophat:
                kw["tophat_radius"] = th_radius
            elif mode == "block_percentile" and block_size:
                kw["bg_block_size"] = block_size
            elif mode == "uniform_mean" and uniform_size:
                kw["uniform_filter_size"] = uniform_size
            return sd.detect_stars(frame, **kw)

        table, base_last = [], None
        for mi, mode in enumerate(modes):
            _bgrun_set(phase="analyzing",
                       progress=45 + int(45 * mi / max(1, len(modes))),
                       message=f"mode {mode} ({mi + 1}/{len(modes)})…")
            counts, times, last = [], [], None
            for fr in frames:
                t0 = _t.perf_counter()
                raw = extract(fr, mode)
                times.append((_t.perf_counter() - t0) * 1000.0)
                counts.append(len(raw) if raw else 0)
                last = raw
            times.sort()
            if mode == "row_percentile":
                base_last = last
            bo, mo = _bgrun_agreement(base_last, last)
            solved_n = solved_att = nmatch = 0
            for fr in frames[:solve_frames]:
                solved_att += 1
                ok, nm = _bgrun_solve(extract(fr, mode), max_c, min_c)
                if ok:
                    solved_n += 1
                    nmatch = max(nmatch, nm)
            table.append({
                "mode": mode,
                "stars": round(sum(counts) / len(counts), 1) if counts else 0,
                "p50_ms": round(times[len(times) // 2], 1) if times else 0,
                "base_only": bo, "mode_only": mo,
                "solved": f"{solved_n}/{solved_att}",
                "nmatch": nmatch,
            })

        report = _bgrun_report(table, len(frames), peak, {
            "sigma": sigma, "kernel_sigma": kernel_sigma, "noise_mode": noise_mode,
            "max_axis_ratio": ("off" if max_axis_ratio == float("inf")
                               else round(max_axis_ratio, 2)),
            "bin": det_bin, "backend": backend, "tophat_radius": th_radius,
            "exposure_s": exposure_s, "gain": gain, "warn": backend_warn,
        })
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "report.txt").write_text(report)

        _bgrun_set(phase="zipping", progress=92, message="packaging results…")
        zip_name = f"bg_ab_{ts}.zip"
        _bgrun_make_zip(_BGRUN_DIR / zip_name, frames_dir, report, len(frames), peak)

        _bgrun_set(phase="done", progress=100, running=False, finished=ts,
                   table=table, zip_name=zip_name, backend=backend,
                   warn=backend_warn,
                   message=(f"done — {len(frames)} frames (peak={peak})"
                            + (f"  ·  {backend_warn}" if backend_warn else "")))
    except Exception as e:
        log.exception("bgrun worker failed")
        _bgrun_set(phase="error", running=False, error=str(e), message=f"error: {e}")


def _bgrun_report(table, n_frames, peak, p):
    lines = [
        "diofinder background-mode A/B with solve (on-device)",
        f"created : {datetime.now().isoformat(timespec='seconds')}",
        f"frames  : {n_frames} (peak={peak})   exposure={p.get('exposure_s')}s   "
        f"gain={p.get('gain')}",
        f"detect  : backend={p.get('backend')}   sigma={p.get('sigma')}   "
        f"kernel_sigma={p.get('kernel_sigma')}   noise_mode={p.get('noise_mode')}   "
        f"max_axis_ratio={p.get('max_axis_ratio')}   bin={p.get('bin')}   "
        f"tophat_radius={p.get('tophat_radius')}",
        "(detection params are the LIVE effective values, not the config file)",
    ]
    if p.get("warn"):
        lines += ["", "*** " + p["warn"] + " ***"]
    lines += [
        "=" * 74,
        f"  {'mode':>14}  {'stars':>6}  {'p50ms':>6}  {'base_only':>9}  "
        f"{'mode_only':>9}  {'solved':>7}  {'Nmatch':>6}",
        "  " + "-" * 70,
    ]
    for r in table:
        lines.append(
            f"  {r['mode']:>14}  {r['stars']:>6}  {r['p50_ms']:>6}  "
            f"{r['base_only']:>9}  {r['mode_only']:>9}  {r['solved']:>7}  "
            f"{r['nmatch']:>6}")
    lines += [
        "",
        "solved/Nmatch are from the LIVE daemon solver (resident database, no",
        "second copy loaded). base_only/mode_only are star counts vs the",
        "row_percentile baseline. The raw frames are in frames/ for off-device",
        "re-analysis: python3 tests/diag_background.py --solve --image frames/...",
    ]
    return "\n".join(lines) + "\n"


def _bgrun_make_zip(zip_path, frames_dir, report, n_frames, peak):
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
        for png in sorted(frames_dir.glob("*.png")):
            zf.write(str(png), f"frames/{png.name}")
        zf.writestr("report.txt", report)
        conf = os.environ.get("DIOFINDER_CONFIG", "/etc/diofinder/diofinder.conf")
        try:
            zf.write(conf, "diofinder.conf")
        except Exception as e:
            zf.writestr("diofinder.conf", f"# could not read: {e}\n")
        st = _safe_call("status")
        cal = _safe_call("calibration_status")
        zf.writestr("status.json", json.dumps({
            "status": {"ok": st.ok, "result": st.result, "error": st.error},
            "calibration": {"ok": cal.ok, "result": cal.result, "error": cal.error},
        }, indent=2, default=str))
        try:
            j = subprocess.run(["journalctl", "-u", "diofinder", "-n", "200", "--no-pager"],
                               capture_output=True, text=True, timeout=10)
            zf.writestr("journal.txt", (j.stdout or "") + (j.stderr or ""))
        except Exception as e:
            zf.writestr("journal.txt", f"error: {e}\n")
        zf.writestr("NOTES.txt",
            "diofinder background A/B (capture + solve) bundle\n"
            f"created: {datetime.now().isoformat(timespec='seconds')}\n"
            f"frames : {n_frames} (peak={peak})\n\n"
            "frames/      raw parked-mount burst PNGs\n"
            "report.txt   per-mode A/B table (detection + solve rate)\n"
            "diofinder.conf device config at capture time\n"
            "status.json  daemon status + calibration\n"
            "journal.txt  recent service log\n")


@app.route("/bgtest/run", methods=["POST"])
def bgtest_run():
    """Start a capture + per-mode A/B (with live-solver solve) in the background."""
    with _bgrun_lock:
        if _bgrun["running"]:
            return jsonify({"ok": False, "error": "a run is already in progress"}), 409
        _bgrun.update(running=True, phase="starting", progress=0, message="starting…",
                      table=None, zip_name=None, error=None, n_frames=0, finished=None,
                      started=datetime.now().strftime("%Y%m%d_%H%M%S"))
    try:
        n = max(4, min(40, int(request.values.get("n_frames", 12))))
        secs = max(5.0, min(180.0, float(request.values.get("seconds", 30))))
        solve_frames = max(1, min(20, int(request.values.get("solve_frames", 5))))
    except (ValueError, TypeError):
        n, secs, solve_frames = 12, 30.0, 5
    threading.Thread(target=_bgrun_worker, args=(n, secs, solve_frames),
                     daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/bgtest/run")
def bgtest_run_status():
    return jsonify(_bgrun_snapshot())


@app.route("/bgtest/run/download")
def bgtest_run_download():
    s = _bgrun_snapshot()
    name = os.path.basename(request.args.get("name") or s.get("zip_name") or "")
    if not name:
        return "no run available yet", 404
    path = _BGRUN_DIR / name
    if not path.exists():
        return "results file not found", 404
    return send_file(str(path), mimetype="application/zip",
                     as_attachment=True, download_name=name)



# ── Tune-from-burst: hindsight detection sweep over a saved burst archive ─────
#
# Replays a saved bg_ab_*.zip burst through the LIVE solver (solve_centroids,
# resident DB — no second copy, so it is memory-safe on the Pi) across a grid of
# detection params, and reports the combination that solves the most frames.
# This is the on-device, button-driven sibling of tests/replay_corpus.py. It
# sweeps EXTRACTION params (bg_mode × sigma × kernel); solving uses the live
# solver's current geometry (fov tolerance, match radius). For an offline sweep
# that also varies fov/max_stars, use tests/replay_corpus.py on the same zip.

_TUNE_SIGMAS  = (4.0, 5.0, 6.0)
_TUNE_KERNELS = (1.5, 2.5)
_TUNE_BG_MODES = ("row_percentile", "block_percentile", "line_median")

_tune_lock = threading.Lock()
_tune = {
    "running": False, "phase": "idle", "progress": 0, "message": "",
    "zip": None, "table": None, "winner": None, "error": None,
    "started": None, "finished": None,
}


def _tune_set(**kw):
    with _tune_lock:
        _tune.update(kw)


def _tune_snapshot():
    with _tune_lock:
        return dict(_tune)


def _list_bursts():
    """Newest-first burst archives available for hindsight tuning."""
    out = []
    try:
        for p in sorted(_BGRUN_DIR.glob("bg_ab_*.zip"), reverse=True):
            try:
                out.append({"name": p.name, "size": p.stat().st_size})
            except OSError:
                continue
    except Exception:
        pass
    return out


def _tune_load_frames(zip_path):
    """Load every PNG frame from a burst zip into uint8 numpy arrays."""
    import io
    import zipfile
    import numpy as np
    from PIL import Image
    frames = []
    with zipfile.ZipFile(str(zip_path)) as zf:
        for m in sorted(zf.namelist()):
            if not m.lower().endswith(".png"):
                continue
            try:
                img = Image.open(io.BytesIO(zf.read(m))).convert("L")
                frames.append(np.ascontiguousarray(np.asarray(img, dtype=np.uint8)))
            except Exception:
                continue
    return frames


def _tune_worker(zip_name, sigmas, kernels, bg_modes):
    import inspect as _inspect
    try:
        import star_detect as sd
        from diofinder.config import load_config
        cfg = load_config()
        det_bin    = cfg.detect_bin
        max_c      = cfg.max_solve_stars
        min_c      = cfg.min_centroids
        block_size = getattr(cfg, "detect_bg_block_size", 0) or 32
        try:
            sd.set_num_threads(2)
        except Exception:
            pass
        params     = _inspect.signature(sd.detect_stars).parameters
        has_kernel = "kernel_sigma" in params
        has_block  = "bg_block_size" in params
        # On wheels without kernel_sigma support, sweeping kernels is redundant.
        if not has_kernel:
            kernels = [kernels[0]]

        _tune_set(phase="loading", progress=3, message=f"loading {zip_name}…")
        frames = _tune_load_frames(_BGRUN_DIR / zip_name)
        if not frames:
            raise RuntimeError(f"no PNG frames found in {zip_name}")

        combos = [(bm, sg, kn) for bm in bg_modes for sg in sigmas for kn in kernels]
        table = []
        for ci, (bm, sg, kn) in enumerate(combos):
            _tune_set(phase="analyzing",
                      progress=int(5 + 90 * ci / max(1, len(combos))),
                      message=f"combo {ci + 1}/{len(combos)}: "
                              f"bg={bm} σ={sg} k={kn}")
            kw = dict(sigma=sg, bin=det_bin, centroid_full_res=True, bg_mode=bm)
            if bm == "block_percentile" and has_block and block_size:
                kw["bg_block_size"] = block_size
            if has_kernel:
                kw["kernel_sigma"] = kn
            solved, matches, stars = 0, [], []
            for fr in frames:
                try:
                    raw = sd.detect_stars(fr, **kw)
                except Exception:
                    raw = []
                stars.append(len(raw) if raw else 0)
                ok, nm = _bgrun_solve(raw, max_c, min_c)
                if ok:
                    solved += 1
                    matches.append(nm)
            n = len(frames)
            stars.sort()
            table.append({
                "bg_mode": bm, "sigma": sg, "kernel": kn if has_kernel else None,
                "frames": n, "solved": solved,
                "rate": (solved / n) if n else 0.0,
                "median_stars": stars[len(stars) // 2] if stars else 0,
                "mean_matches": (sum(matches) / len(matches)) if matches else 0.0,
            })

        # Winner: highest solve rate, then most mean matches, then fewer stars
        # (cheaper extraction / less crowding).
        winner = max(
            table,
            key=lambda r: (r["rate"], r["mean_matches"], -r["median_stars"]),
        ) if table else None
        if winner and winner["solved"] == 0:
            winner = None
        _tune_set(phase="done", progress=100, running=False,
                  table=table, winner=winner,
                  finished=datetime.now().strftime("%Y%m%d_%H%M%S"),
                  message=("done" if winner else
                           "done — no combination solved any frame"))
    except Exception as e:
        log.exception("tune worker failed")
        _tune_set(phase="error", running=False, error=str(e),
                  message=f"error: {e}")


@app.route("/tune/start", methods=["POST"])
def tune_start():
    """Kick off a hindsight detection sweep over a saved burst archive."""
    name = os.path.basename(request.form.get("zip") or "")
    if not name or not (_BGRUN_DIR / name).exists():
        return jsonify({"ok": False, "error": "burst archive not found"}), 404
    with _tune_lock:
        if _tune["running"]:
            return jsonify({"ok": False, "error": "a tune is already running"}), 409
        _tune.update(running=True, phase="starting", progress=0,
                     message="starting…", zip=name, table=None, winner=None,
                     error=None, finished=None,
                     started=datetime.now().strftime("%Y%m%d_%H%M%S"))

    def _floats(spec, default):
        vals = []
        for tok in (spec or "").split(","):
            tok = tok.strip()
            if tok:
                try:
                    vals.append(float(tok))
                except ValueError:
                    pass
        return vals or list(default)

    sigmas   = _floats(request.form.get("sigmas"), _TUNE_SIGMAS)
    kernels  = _floats(request.form.get("kernels"), _TUNE_KERNELS)
    bg_modes = [m.strip() for m in (request.form.get("bg_modes") or "").split(",")
                if m.strip()] or list(_TUNE_BG_MODES)
    threading.Thread(target=_tune_worker, args=(name, sigmas, kernels, bg_modes),
                     daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/tune")
def api_tune():
    """Tune-from-burst progress / winner for the Dashboard poller."""
    return jsonify(_tune_snapshot())


@app.route("/tune/apply", methods=["POST"])
def tune_apply():
    """Persist the winning detection combo live (solver_params_set, persist),
    optionally saving it as a tuned seeing override (source=replay)."""
    s = _tune_snapshot()
    w = s.get("winner")
    if not w:
        return jsonify({"ok": False, "error": "no winner to apply"}), 400
    pargs = {"persist": True, "detect_bg_mode": w["bg_mode"],
             "detect_sigma": float(w["sigma"])}
    if w.get("kernel") is not None:
        pargs["detect_kernel_sigma"] = float(w["kernel"])
    r = _safe_call("solver_params_set", pargs)
    if not r.ok:
        return jsonify({"ok": False, "error": r.error}), 400
    mode = (request.form.get("mode") or "").strip().lower()
    if mode in ("good", "bad"):
        r2 = _safe_call("seeing_override_save", {"mode": mode, "source": "replay"})
        if not r2.ok:
            return jsonify({"ok": False, "error": r2.error}), 400
    return jsonify({"ok": True, "applied": pargs})


@app.route("/healthz")
def healthz():
    """200 ok if the daemon socket responds to ping, else 503."""
    r = _safe_call("ping", timeout=2.0)
    if r.ok:
        return "ok\n", 200
    return f"daemon unreachable: {r.error}\n", 503


if __name__ == "__main__":
    try:
        os.sched_setaffinity(0, {_load_cfg_cached().cpu_comms})
    except Exception as e:
        log.warning("Could not pin webui CPU affinity: %s", e)
    app.run(host="0.0.0.0", port=80, debug=False, threaded=True)
