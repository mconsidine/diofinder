"""
eFinder web UI.

Bound to port 80 on all interfaces. No authentication: the eFinder
operates on a private network (either the user's home Wi-Fi or its
own hotspot), and the maintenance Unix socket is the trust boundary
that already exists. If the eFinder is exposed to the public internet,
the user has bigger problems than the web UI lacking auth.

The web UI is a thin client of the maintenance socket. It does not
hold any state of its own; every render is a fresh maint-socket call.
This keeps deployment simple (Flask can crash and restart without
losing eFinder state) and makes the same UI work whether you've
restarted Flask or the eFinder itself.

Pages:
  /              dashboard (auto-refreshing status, key actions)
  /polar         polar alignment workflow (multi-step)
  /config        view current configuration
  /logs          last N lines of journalctl
  /update        trigger efinder-update

The Flask templates live in webui/templates/; static assets in
webui/static/.
"""

import io
import json
import logging
import os
import subprocess
import sys

from flask import (
    Flask, render_template, redirect, url_for, request, jsonify, abort,
)

# Path setup so we can import efinder.maint
sys.path.insert(0, "/opt/efinder")
try:
    from efinder.maint import call as maint_call, MaintResponse
except ImportError:
    sys.path.insert(0, ".")
    from efinder.maint import call as maint_call, MaintResponse

log = logging.getLogger("efinder.webui")

app = Flask(__name__,
            template_folder="templates",
            static_folder="static")


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _safe_call(cmd, args=None, timeout=5.0):
    """Wrap maint_call with an error-friendly fallback. The dashboard
    must keep rendering even if the daemon is down or restarting.
    """
    try:
        return maint_call(cmd, args, timeout=timeout)
    except FileNotFoundError:
        return MaintResponse(ok=False, error="eFinder daemon socket not found "
                                              "(service may be stopped or restarting)")
    except PermissionError:
        return MaintResponse(ok=False, error="cannot access eFinder socket "
                                              "(check group membership)")
    except Exception as e:
        return MaintResponse(ok=False, error=f"{type(e).__name__}: {e}")


def _format_solution(sol):
    """Pretty form of latest_solution for the dashboard."""
    if not sol:
        return None
    if not sol.get("solved"):
        return {
            "solved": False,
            "stars": sol.get("stars", 0),
            "peak": sol.get("peak", 0),
            "noise": sol.get("noise", 0.0),
            "status": sol.get("status", 0),
        }
    ra_h = sol["ra_deg"] / 15.0
    return {
        "solved": True,
        "ra_str": _hms(ra_h),
        "dec_str": _dms(sol["dec_deg"]),
        "ra_deg": sol["ra_deg"],
        "dec_deg": sol["dec_deg"],
        "fov_deg": sol.get("fov_deg", 0.0),
        "stars": sol["stars"],
        "matches": sol.get("matches", 0),
        "peak": sol["peak"],
        "noise": sol.get("noise", 0.0),
        "solve_ms": sol["solve_ms"],
    }


def _hms(hours):
    hours = hours % 24.0
    h = int(hours); m = int((hours - h) * 60)
    s = int(round((hours - h - m / 60) * 3600))
    if s == 60: s = 0; m += 1
    if m == 60: m = 0; h = (h + 1) % 24
    return f"{h:02d}h{m:02d}m{s:02d}s"


def _dms(deg):
    sign = "+" if deg >= 0 else "-"
    a = abs(deg)
    d = int(a); m = int((a - d) * 60)
    s = int(round((a - d - m / 60) * 3600))
    if s == 60: s = 0; m += 1
    if m == 60: m = 0; d += 1
    return f"{sign}{d:02d}\u00b0{m:02d}'{s:02d}\""


# ---------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------

@app.route("/")
def dashboard():
    status = _safe_call("status")
    cal = _safe_call("calibration_status")
    exposure = _safe_call("exposure_get")

    sol = (_format_solution(status.result["solution"])
           if status.ok and status.result else None)

    return render_template(
        "dashboard.html",
        status_ok=status.ok,
        status_error=status.error if not status.ok else None,
        solution=sol,
        boresight=(status.result.get("boresight") if status.ok else None),
        fov_deg=(status.result.get("fov_deg") if status.ok else None),
        calibration=(cal.result if cal.ok else None),
        cal_error=cal.error if not cal.ok else None,
        exposure=(exposure.result if exposure.ok else None),
    )


@app.route("/api/status")
def api_status():
    """JSON endpoint for the dashboard's auto-refresh."""
    status = _safe_call("status")
    cal = _safe_call("calibration_status")
    return jsonify({
        "status": {"ok": status.ok, "result": status.result, "error": status.error},
        "calibration": {"ok": cal.ok, "result": cal.result, "error": cal.error},
    })


# ---------------------------------------------------------------------
# Boresight
# ---------------------------------------------------------------------

@app.route("/boresight/center", methods=["POST"])
def boresight_center():
    r = _safe_call("boresight_center")
    if not r.ok:
        return r.error, 500
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------
# Polar alignment
# ---------------------------------------------------------------------

@app.route("/polar")
def polar_page():
    status = _safe_call("polar_status")
    return render_template(
        "polar.html",
        ok=status.ok,
        error=status.error if not status.ok else None,
        polar=status.result if status.ok else None,
    )


@app.route("/api/polar/status")
def api_polar_status():
    r = _safe_call("polar_status")
    return jsonify({"ok": r.ok, "result": r.result, "error": r.error})


@app.route("/polar/start", methods=["POST"])
def polar_start():
    _safe_call("polar_start")
    return redirect(url_for("polar_page"))


@app.route("/polar/cancel", methods=["POST"])
def polar_cancel():
    _safe_call("polar_cancel")
    return redirect(url_for("polar_page"))


@app.route("/polar/set-latitude", methods=["POST"])
def polar_set_latitude():
    try:
        lat = float(request.form.get("latitude_deg", ""))
    except ValueError:
        return "latitude must be numeric", 400
    if not (-90.0 <= lat <= 90.0):
        return "latitude out of range", 400
    _safe_call("polar_set_latitude",
               {"latitude_deg": lat, "persist": True})
    return redirect(url_for("polar_page"))


# ---------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------

@app.route("/calibration/reset", methods=["POST"])
def calibration_reset():
    r = _safe_call("calibration_reset")
    if not r.ok:
        return r.error, 500
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------
# Exposure
# ---------------------------------------------------------------------

@app.route("/exposure/set", methods=["POST"])
def exposure_set():
    persist = request.form.get("persist") == "on"
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
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------

@app.route("/logs")
def logs():
    n = int(request.args.get("n", 100))
    n = max(10, min(n, 500))
    try:
        out = subprocess.check_output(
            ["journalctl", "--system",
             "-u", "efinder.service",
             "-u", "cedar-detect.service",
             "-n", str(n), "--no-pager", "-o", "short-precise"],
            text=True, stderr=subprocess.STDOUT, timeout=5.0,
        )
    except subprocess.CalledProcessError as e:
        out = f"journalctl failed: {e}"
    except FileNotFoundError:
        out = "journalctl not found on this system"
    except subprocess.TimeoutExpired:
        out = "journalctl timed out"
    return render_template("logs.html", logs=out, n=n)


# ---------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------

@app.route("/update", methods=["GET", "POST"])
def update_page():
    if request.method == "POST":
        # Fire-and-forget; the user will see the new version after the
        # service restarts. We don't block on this -- efinder-update
        # restarts the service which will momentarily bring this Flask
        # app down (since they share systemd lifecycle? No, Flask is its
        # own service). Either way, fire and redirect.
        try:
            subprocess.Popen(
                ["sudo", "/usr/local/bin/efinder-update"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError:
            return "efinder-update not installed", 500
        return render_template("update_running.html")
    version = _safe_call("version")
    return render_template(
        "update.html",
        version=(version.result.get("version") if version.ok else "unknown"),
    )


# ---------------------------------------------------------------------
# Config view (read-only for v1)
# ---------------------------------------------------------------------

CONFIG_PATH = os.environ.get("EFINDER_CONFIG", "/etc/efinder/efinder.conf")


@app.route("/config")
def config_page():
    try:
        with open(CONFIG_PATH) as f:
            content = f.read()
    except Exception as e:
        content = f"Error reading {CONFIG_PATH}: {e}"
    return render_template("config.html",
                           path=CONFIG_PATH, content=content)


# ---------------------------------------------------------------------
# Live frame view
# ---------------------------------------------------------------------

@app.route("/frame.jpg")
def frame_jpg():
    """Current camera frame as JPEG with a red boresight circle overlaid.

    Reads directly from the eFinder's shared-memory triple buffer (the
    same /dev/shm segments that camera_proc writes and solver_proc reads).
    No round-trip through the daemon is needed for the pixel data; only
    the boresight coordinates come from the maintenance socket.

    Returns 503 if the eFinder daemon is not running (SHM not present).
    """
    import numpy as np
    from multiprocessing import shared_memory
    from PIL import Image, ImageDraw

    try:
        from efinder.config import load_config
        ecfg = load_config()
        width, height = ecfg.frame_width, ecfg.frame_height
    except Exception:
        width, height = 960, 760

    # Boresight from the live daemon (may differ from config if recently aligned).
    bs_r = _safe_call("status")
    bs = bs_r.result.get("boresight") if bs_r.ok and bs_r.result else None
    cx = int(round(bs["x"])) if bs else width // 2
    cy = int(round(bs["y"])) if bs else height // 2

    # Read from the first accessible SHM slot.  We .copy() immediately so
    # the buffer is detached before the camera can overwrite it.
    from efinder.frame_slots import SHM_PREFIX, NUM_BUFFERS
    frame = None
    for i in range(NUM_BUFFERS):
        try:
            shm = shared_memory.SharedMemory(name=f"{SHM_PREFIX}_{i}")
            frame = np.ndarray(
                (height, width), dtype=np.uint8, buffer=shm.buf,
            ).copy()
            shm.close()
            break
        except Exception:
            continue

    if frame is None:
        return "camera not running", 503, {"Content-Type": "text/plain"}

    img = Image.fromarray(frame, mode="L").convert("RGB")
    draw = ImageDraw.Draw(img)

    r = 28  # circle radius in pixels (~6 % of frame width)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                 outline=(220, 0, 0), width=2)
    # Short tick marks extending outward from the circle
    gap = 6
    draw.line([cx - r - gap, cy, cx - r - 1, cy], fill=(220, 0, 0), width=1)
    draw.line([cx + r + 1, cy, cx + r + gap, cy], fill=(220, 0, 0), width=1)
    draw.line([cx, cy - r - gap, cx, cy - r - 1], fill=(220, 0, 0), width=1)
    draw.line([cx, cy + r + 1, cx, cy + r + gap], fill=(220, 0, 0), width=1)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    buf.seek(0)
    return (
        buf.read(), 200,
        {"Content-Type": "image/jpeg", "Cache-Control": "no-store, no-cache"},
    )


# ---------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------

@app.route("/healthz")
def healthz():
    r = _safe_call("ping", timeout=2.0)
    if r.ok:
        return "ok\n", 200
    return f"daemon unreachable: {r.error}\n", 503


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80, debug=False)
