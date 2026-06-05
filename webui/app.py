"""
eFinder web UI.

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

app.jinja_env.filters['log10'] = \
    lambda x: math.log10(float(x)) if float(x) > 0 else -3


def _safe_call(cmd, args=None, timeout=15.0):
    """Call the maintenance daemon; return MaintResponse(ok=False) on any connection error."""
    try:
        return maint_call(cmd, args, timeout=timeout)
    except FileNotFoundError:
        return MaintResponse(ok=False, error="eFinder daemon socket not found")
    except PermissionError:
        return MaintResponse(ok=False, error="cannot access eFinder socket")
    except Exception as e:
        return MaintResponse(ok=False, error=f"{type(e).__name__}: {e}")


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
    ra_h = sol["ra_deg"] / 15.0
    return {
        "solved":    True,
        "ra_str":    _hms(ra_h),
        "dec_str":   _dms(sol["dec_deg"]),
        "ra_deg":    sol["ra_deg"],
        "dec_deg":   sol["dec_deg"],
        "fov_deg":   sol.get("fov_deg",  0.0),
        "roll_deg":  sol.get("roll_deg", 0.0),
        "stars":     sol["stars"],
        "matches":   sol.get("matches", 0),
        "peak":      sol["peak"],
        "noise":     sol.get("noise", 0.0),
        "solve_ms":  sol["solve_ms"],
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


@app.route("/")
def dashboard():
    """Main dashboard: current solution, boresight, calibration, and IMU status."""
    status = _safe_call("status")
    cal    = _safe_call("calibration_status")

    sol = (_format_solution(status.result["solution"])
           if status.ok and status.result else None)

    with _focus_lock:
        committed_focus = _focus_state["committed_score"]

    return render_template(
        "dashboard.html",
        status_ok=status.ok,
        status_error=status.error if not status.ok else None,
        solution=sol,
        boresight=(status.result.get("boresight") if status.ok else None),
        fov_deg=(status.result.get("fov_deg") if status.ok else None),
        calibration=(cal.result if cal.ok else None),
        cal_error=cal.error if not cal.ok else None,
        committed_focus=committed_focus,
        imu=(status.result.get("imu") if status.ok else None),
        solver_backend="sycamore",
        test_mode=(
            status.result.get("test_mode", True)
            if status.ok else True),
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


@app.route("/boresight/center", methods=["POST"])
def boresight_center():
    """Reset boresight to the frame center and redirect to dashboard."""
    r = _safe_call("boresight_center")
    if not r.ok:
        return r.error, 500
    return redirect(url_for("dashboard"))


@app.route("/testmode/set", methods=["POST"])
def testmode_set():
    """Toggle test-image vs. live-camera mode and redirect to dashboard."""
    raw     = request.form.get("enabled", "false").strip().lower()
    enabled = raw in ("true", "1", "yes")
    r = _safe_call("set_test_mode", {"enabled": enabled})
    if not r.ok:
        return r.error, 500
    return redirect(url_for("dashboard"))


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
    )


@app.route("/bgtest/set", methods=["POST"])
def bgtest_set():
    """Apply background mode from the bgtest form and redirect back."""
    mode = request.form.get("bg_mode", "row_percentile").strip().lower()
    persist = request.form.get("persist") == "1"
    params = {"detect_bg_mode": mode, "persist": persist}
    if mode == "top_hat":
        try:
            params["detect_tophat_radius"] = int(request.form.get("tophat_radius", 12))
        except (ValueError, TypeError):
            params["detect_tophat_radius"] = 12
    r = _safe_call("solver_params_set", params)
    if not r.ok:
        return r.error, 500
    return redirect(url_for("bgtest_page"))


@app.route("/api/bgcache")
def api_bgcache():
    """Live temporal-background-cache status for the Background page."""
    r = _safe_call("bg_cache_status")
    return jsonify({"ok": r.ok, "result": r.result, "error": r.error})


@app.route("/calibration/reset", methods=["POST"])
def calibration_reset():
    """Reset the FOV rolling-window calibration and redirect to the dashboard."""
    r = _safe_call("calibration_reset")
    if not r.ok:
        return r.error, 500
    return redirect(url_for("dashboard"))


@app.route("/camera")
def camera_page():
    """Camera and solver settings page."""
    exposure      = _safe_call("exposure_get")
    solver_params = _safe_call("solver_params_get")
    return render_template(
        "camera.html",
        exposure=(exposure.result if exposure.ok else None),
        solver_params=(solver_params.result if solver_params.ok else None),
    )


@app.route("/exposure/set", methods=["POST"])
def exposure_set():
    """Apply exposure and optional gain from the camera-settings form."""
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
    return redirect(url_for("camera_page"))


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
    if "detect_sigma" in data or "solve_timeout_ms" in data:
        pargs = {"persist": False}
        if "detect_sigma" in data:
            try:
                pargs["detect_sigma"] = float(data["detect_sigma"])
            except (ValueError, TypeError) as e:
                errors.append(f"detect_sigma invalid: {e}")
        if "solve_timeout_ms" in data:
            try:
                pargs["solve_timeout_ms"] = int(data["solve_timeout_ms"])
            except (ValueError, TypeError) as e:
                errors.append(f"solve_timeout_ms invalid: {e}")
        if len(pargs) > 1:
            r = _safe_call("solver_params_set", pargs)
            if r.ok:
                applied.update({k: v for k, v in pargs.items() if k != "persist"})
            else:
                errors.append(f"solver_params: {r.error}")
    if errors:
        return jsonify({"ok": False, "errors": errors, "applied": applied}), 400
    return jsonify({"ok": True, "applied": applied})


@app.route("/solver/params/set", methods=["POST"])
def solver_params_set():
    """Apply detect_sigma and solve_timeout_ms from the solver-settings form."""
    persist = request.form.get("persist") == "on"
    pargs   = {"persist": persist}
    if request.form.get("detect_sigma"):
        try:
            pargs["detect_sigma"] = float(request.form["detect_sigma"])
        except ValueError:
            return "detect_sigma must be numeric", 400
    if request.form.get("solve_timeout_ms"):
        try:
            pargs["solve_timeout_ms"] = int(request.form["solve_timeout_ms"])
        except ValueError:
            return "solve_timeout_ms must be integer", 400
    r = _safe_call("solver_params_set", pargs)
    if not r.ok:
        return r.error, 400
    return redirect(url_for("camera_page"))


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
                result["mode"] = "ap" if wlan_con == "efinder-ap" else "station"
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
            if ssid and ssid not in seen and not ssid.startswith("efinder-"):
                seen.add(ssid)
                networks.append({"ssid": ssid, "signal": signal})
        networks.sort(key=lambda n: n["signal"], reverse=True)
        return networks
    except Exception:
        return []


_wifi_lock         = threading.Lock()
_wifi_connect_proc = None


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
        _wifi_connect_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    return redirect(url_for("wifi_connecting", ssid=ssid))


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
    """Show the most recent n lines (10–500) from the efinder.service journal."""
    n = int(request.args.get("n", 100))
    n = max(10, min(n, 500))
    try:
        out = subprocess.check_output(
            ["journalctl", "--system",
             "-u", "efinder.service",
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

@app.route("/update", methods=["GET", "POST"])
def update_page():
    """OTA update page: GET shows current version; POST fires efinder-update in the background.

    An optional 'ref' form field updates to a specific branch/tag instead of the
    latest release (runs `efinder-update --ref <ref>`)."""
    if request.method == "POST":
        cmd = ["sudo", "/usr/local/bin/efinder-update"]
        ref = (request.form.get("ref") or "").strip()
        if ref:
            # Branch/tag names: letters, digits, and ./_/-, with slashes for
            # namespaced branches (e.g. claude/my-branch). Reject anything else.
            if not _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,200}", ref):
                return "invalid branch/tag name", 400
            cmd += ["--ref", ref]
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError:
            return "efinder-update not installed", 500
        return render_template("update_running.html")
    version = _safe_call("version")
    return render_template(
        "update.html",
        version=(
            version.result.get("version") if version.ok else "unknown"),
    )


# ---- Config view ------------------------------------------------------------

CONFIG_PATH = os.environ.get("EFINDER_CONFIG", "/etc/efinder/efinder.conf")

_CONFIG_SECTIONS = [
    ("Camera", [
        ("frame_width",               "Frame width",          "Sensor ROI width in pixels."),
        ("frame_height",              "Frame height",         "Sensor ROI height in pixels."),
        ("exposure_s",                "Exposure (s)",         "Initial exposure; auto-exposure adjusts this at runtime."),
        ("gain",                      "Gain",                 "Analog gain. Higher = more sensitive but noisier."),
        ("auto_exposure_enabled",     "Auto-exposure",        "Adaptively adjust exposure to reach the target star count."),
        ("auto_exposure_target_stars","Target stars",         "Desired star count when auto-exposure is on."),
        ("auto_exposure_min_s",       "Auto-exp min (s)",     "Minimum exposure floor."),
        ("auto_exposure_max_s",       "Auto-exp max (s)",     "Maximum exposure ceiling."),
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
    ]),
    ("Plate Solving (olive-solve)", [
        ("solver_db",        "Star database",      "Path to a tetra3 .npz database compatible with olive-solve."),
        ("min_centroids",    "Min stars",          "Minimum detected stars required to attempt a solve."),
        ("solve_timeout_ms", "Solve timeout (ms)", "Hard timeout per solve attempt."),
        ("match_threshold",  "Match threshold",    "Max false-positive probability (1e-5 default)."),
        ("match_radius",     "Match radius",       "Max centroid-catalog distance as fraction of FOV."),
    ]),
    ("Boresight", [
        ("boresight_x", "Boresight X (px)", "Telescope axis X in pixels."),
        ("boresight_y", "Boresight Y (px)", "Telescope axis Y in pixels."),
    ]),
    ("Communications (LX200)", [
        ("lx200_port",             "LX200 port",         "TCP port for the LX200 server."),
        ("lx200_client_timeout_s", "Client timeout (s)", "Disconnect idle LX200 clients."),
    ]),
    ("CPU Affinity", [
        ("cpu_camera", "Camera CPU",  "Core for camera_proc. Also shared with solver rayon threads."),
        ("cpu_solver", "Solver CPU",  "Primary core for solver_proc; rayon also uses cpu_camera."),
        ("cpu_comms",  "Comms CPU",   "Core for comms_proc and web UI."),
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
    from efinder.config import Config, load_config as _load_config
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

    return render_template(
        "config.html",
        path=CONFIG_PATH,
        sections=sections,
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


@app.route("/frame.jpg")
def frame_jpg():
    """Serve the current camera frame as a histogram-stretched JPEG with boresight overlay."""
    import time as _time
    import numpy as np
    from multiprocessing import shared_memory, resource_tracker as _rt
    from PIL import Image, ImageDraw

    try:
        from efinder.config import load_config
        ecfg = load_config()
        width, height = ecfg.frame_width, ecfg.frame_height
    except Exception:
        width, height = 960, 760

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

    from efinder.frame_slots import SHM_PREFIX, NUM_BUFFERS
    frame = None
    for i in range(NUM_BUFFERS):
        try:
            shm = shared_memory.SharedMemory(name=f"{SHM_PREFIX}_{i}", create=False)
            try:
                _rt.unregister(shm._name, "shared_memory")
            except Exception:
                pass
            frame = np.ndarray(
                (height, width), dtype=np.uint8, buffer=shm.buf,
            ).copy()
            shm.close()
            break
        except Exception:
            continue

    if frame is None:
        return "camera not running", 503, {"Content-Type": "text/plain"}

    sky     = float(np.percentile(frame, 50))
    signal  = np.clip(frame.astype(np.float32) - sky, 0.0, None)
    white   = max(float(np.percentile(signal, 99.9)), 20.0)
    stretched = np.clip(signal / white * 255.0, 0, 255).astype(np.uint8)

    img  = Image.fromarray(stretched, mode="L").convert("RGB")
    draw = ImageDraw.Draw(img)
    r    = 28
    draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                 outline=(255, 80, 80), width=2)
    gap = 6
    draw.line([cx - r - gap, cy, cx - r - 1, cy],   fill=(255, 120, 120), width=1)
    draw.line([cx + r + 1,   cy, cx + r + gap, cy],  fill=(255, 120, 120), width=1)
    draw.line([cx, cy - r - gap, cx, cy - r - 1],   fill=(255, 120, 120), width=1)
    draw.line([cx, cy + r + 1,   cx, cy + r + gap],  fill=(255, 120, 120), width=1)

    try:
        r_half = round(1800.0 / ecfg.arcsec_per_pixel)
        r_one  = round(3600.0 / ecfg.arcsec_per_pixel)
    except Exception:
        r_half, r_one = 35, 71
    draw.ellipse([cx - r_half, cy - r_half, cx + r_half, cy + r_half],
                 outline=(255, 120, 120), width=1)
    draw.ellipse([cx - r_one,  cy - r_one,  cx + r_one,  cy + r_one],
                 outline=(255, 120, 120), width=1)

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
        from efinder.config import load_config
        ecfg = load_config()
        width, height = ecfg.frame_width, ecfg.frame_height
    except Exception:
        width, height = 960, 760

    from efinder.frame_slots import SHM_PREFIX, NUM_BUFFERS
    frame = None
    for i in range(NUM_BUFFERS):
        try:
            shm = shared_memory.SharedMemory(name=f"{SHM_PREFIX}_{i}", create=False)
            try:
                _rt.unregister(shm._name, "shared_memory")
            except Exception:
                pass
            frame = np.ndarray((height, width), dtype=np.uint8,
                               buffer=shm.buf).copy()
            shm.close()
            break
        except Exception:
            continue

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
        from efinder.config import load_config as _lcfg
        ecfg = _lcfg()
        W, H = ecfg.frame_width, ecfg.frame_height
        arcsec_px = ecfg.arcsec_per_pixel
    except Exception:
        W, H, arcsec_px = 960, 760, 50.8

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:

        # ── Config file ──────────────────────────────────────────────────────
        conf_path = os.environ.get("EFINDER_CONFIG", "/etc/efinder/efinder.conf")
        try:
            zf.write(conf_path, "efinder.conf")
        except Exception as e:
            zf.writestr("efinder.conf", f"# could not read: {e}\n")

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

        # ── Journal ──────────────────────────────────────────────────────────
        try:
            j = subprocess.run(
                ["journalctl", "-u", "efinder", "-n", "300", "--no-pager"],
                capture_output=True, text=True, timeout=10,
            )
            zf.writestr("journal.txt", j.stdout + (j.stderr or ""))
        except Exception as e:
            zf.writestr("journal.txt", f"error collecting journal: {e}\n")

        # ── Camera frames from SHM ───────────────────────────────────────────
        from PIL import Image, ImageDraw
        try:
            from efinder.frame_slots import SHM_PREFIX, NUM_BUFFERS
        except Exception:
            SHM_PREFIX, NUM_BUFFERS = "efinder_frame", 3

        bs_r = _safe_call("status")
        bs   = bs_r.result.get("boresight") if bs_r.ok and bs_r.result else None
        cx   = int(round(bs["x"])) if bs else W // 2
        cy   = int(round(bs["y"])) if bs else H // 2

        def _capture_frame():
            for i in range(NUM_BUFFERS):
                try:
                    shm = shared_memory.SharedMemory(
                        name=f"{SHM_PREFIX}_{i}", create=False)
                    frame = np.ndarray(
                        (H, W), dtype=np.uint8, buffer=shm.buf).copy()
                    shm.close()
                    return frame
                except Exception:
                    continue
            return None

        def _save_frame_pair(zf, idx, frame):
            # Raw grayscale PNG
            raw_buf = io.BytesIO()
            Image.fromarray(frame, mode="L").save(raw_buf, format="PNG")
            zf.writestr(f"frame_{idx:02d}_raw.png", raw_buf.getvalue())
            # Arcsinh-stretched display JPEG with boresight overlay
            sky   = float(np.median(frame))
            x     = np.clip(frame.astype(np.float32) - sky, 0.0, None)
            beta  = max(1.0, sky * 0.1)
            xs    = np.arcsinh(x / beta)
            scale = float(np.percentile(xs, 99.9)) or float(xs.max()) or 1.0
            disp  = np.clip(xs / scale * 255.0, 0, 255).astype(np.uint8)
            img   = Image.fromarray(disp, mode="L").convert("RGB")
            draw  = ImageDraw.Draw(img)
            r = 28
            draw.ellipse([cx-r, cy-r, cx+r, cy+r], outline=(255, 80, 80), width=2)
            try:
                r_half = round(1800.0 / arcsec_px)
                r_one  = round(3600.0 / arcsec_px)
                draw.ellipse([cx-r_half, cy-r_half, cx+r_half, cy+r_half],
                             outline=(255, 120, 120), width=1)
                draw.ellipse([cx-r_one,  cy-r_one,  cx+r_one,  cy+r_one],
                             outline=(255, 120, 120), width=1)
            except Exception:
                pass
            disp_buf = io.BytesIO()
            img.save(disp_buf, format="JPEG", quality=85)
            zf.writestr(f"frame_{idx:02d}_display.jpg", disp_buf.getvalue())

        frames_saved = 0
        import time as _time
        first_frame = None
        for attempt in range(2):
            if attempt > 0:
                # Wait long enough for the camera to deliver a genuinely new
                # frame (exposure may be up to ~1 s, plus camera overhead).
                _time.sleep(max(1.2, ecfg.exposure_s + 0.4))
            f = _capture_frame()
            if f is not None:
                # Skip second frame if it is identical to the first (can
                # happen at slow frame rates when two SHM reads hit the
                # same slot).
                if attempt > 0 and first_frame is not None:
                    import hashlib
                    if hashlib.md5(f.tobytes()).digest() == hashlib.md5(first_frame.tobytes()).digest():
                        f = None
                if f is not None:
                    try:
                        _save_frame_pair(zf, attempt + 1, f)
                        frames_saved += 1
                        if first_frame is None:
                            first_frame = f
                    except Exception:
                        pass

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
        if status.ok and status.result:
            r = status.result
            lines.append(f"test_mode: {r.get('test_mode')}")
            lines.append(f"fov_deg: {r.get('fov_deg')}")
            sol = r.get("solution") or {}
            lines.append(f"last_solve_status: {sol.get('status')}")
            lines.append(f"last_solve_ms: {sol.get('solve_ms')}")
            imu = r.get("imu") or {}
            lines.append(f"imu_active: {imu.get('active')}")
        zf.writestr("capture_info.txt", "\n".join(lines) + "\n")

    # Save a copy to disk so scp/curl also works
    out_dir = pathlib.Path("/var/lib/efinder")
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


@app.route("/healthz")
def healthz():
    """200 ok if the daemon socket responds to ping, else 503."""
    r = _safe_call("ping", timeout=2.0)
    if r.ok:
        return "ok\n", 200
    return f"daemon unreachable: {r.error}\n", 503


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80, debug=False, threaded=True)
