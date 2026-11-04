"""Live capture — record the frames the finder sees, with exact UTC timing and
the solved image-center RA/Dec of each, into a timestamped archive.

The use case is satellite identification: satellites streak through the finder
field often, and a recording of raw frames + per-frame time + pointing is
exactly what a field-of-view pass predictor (e.g. the IAU CPS SatChecker
``/fov/satellite-passes/`` endpoint) needs. ``tests/satcheck_archive.py`` reads
an archive and queries SatChecker for each frame.

Where it runs: inside the solver process. The solver already holds every frame
it processes AND the solve result for that frame, so pairing them is exact
(matched by FrameSlots seq) and costs no extra IPC. The solver loop only does
cheap work — a dict compare per frame and, while recording, one frame copy
(~0.7 MB memcpy). PNG encoding and file writes run on a writer thread fed by a
small bounded queue; if the writer falls behind, frames are DROPPED (counted)
rather than ever slowing the solve loop.

Control (single writer each):

* ``shared_cfg["live_capture"]`` — written by comms (``live_capture_start`` /
  ``live_capture_stop``): ``{"on": bool, "id": "<UTC stamp>", "site": {...}}``.
* ``shared_cfg["live_capture_status"]`` — written here (throttled): state,
  frame/drop counts, bytes, the last archive name, and why it stopped.

On disk (``/var/lib/diofinder/live_captures/``):

* while recording: ``livecap_<id>.partial/`` holding ``frame_000001.png`` …
  and ``frames.jsonl`` (one JSON record per frame, appended + flushed);
* on stop: zipped (STORED — PNGs are already compressed) into
  ``livecap_<id>.zip`` file by file, deleting each source as it goes, so the
  peak extra disk use is one frame, not a second copy of the session. The
  zip is written as ``.zip.tmp`` and renamed, so a half-written archive is
  never mistaken for a finished one.
* a ``.partial`` directory left by a crash or power cut is finalized the next
  time the solver starts (``recover_partials``), so nothing recorded is lost.

Times: ``exposure_start_utc`` is the sensor's own first-row exposure start
(libcamera SensorTimestamp - ExposureTime, see frame_meta.py), ``exposure_mid``
adds half the actual exposure. They are only as good as the Pi's clock, which
has no RTC — the archive records whether the clock looked plausible.
Coordinates are J2000/ICRS (the solver's internal frame; no JNow conversion).
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import os
import queue
import re
import shutil
import threading
import time
import zipfile

log = logging.getLogger("diofinder.live_capture")

DEFAULT_DIR = "/var/lib/diofinder/live_captures"
FORMAT = "diofinder-livecap/1"
ARCHIVE_RE = re.compile(r"^livecap_\d{8}T\d{6}Z\.zip$")
_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")

_QUEUE_MAX = 3            # frames buffered for the writer (~0.7 MB each)
_STATUS_EVERY_S = 1.0     # status publish throttle while recording (Manager write)
_CONTROL_POLL_S = 2.0     # writer re-checks the on/off flag this often
_PNG_LEVEL = 3            # ~10% bigger than level 6, ~3x faster to encode

_SOL_KEYS = ("solved", "status", "center_ra_deg", "center_dec_deg",
             "ra_deg", "dec_deg", "roll_deg", "is_mirrored", "fov_deg",
             "stars", "matches", "peak", "solve_ms")


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------- #

def new_session_id(now=None):
    """UTC stamp used in the archive name, e.g. '20261102T013045Z'."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ")


def valid_session_id(sid):
    return isinstance(sid, str) and bool(_ID_RE.match(sid))


def archive_name(session_id):
    return f"livecap_{session_id}.zip"


def unix_to_jd(t_unix):
    """Julian Date (UTC) from a Unix timestamp."""
    return t_unix / 86400.0 + 2440587.5


def fov_radius_deg(fov_w_deg, width_px, height_px):
    """Radius of the circle that encloses the whole rectangular frame
    (half the diagonal), from the horizontal FOV. Small-angle; fine for a
    finder (the tangent-plane error is < 0.2% at 14 deg)."""
    if not fov_w_deg or not width_px:
        return None
    return 0.5 * fov_w_deg * math.hypot(width_px, height_px) / width_px


def _iso(t_unix):
    return _dt.datetime.fromtimestamp(t_unix, tz=_dt.timezone.utc).isoformat(
        timespec="milliseconds")


def frame_record(fname, seq, meta, sol, last_solved, saved_unix):
    """Build one frames.jsonl record.

    * ``meta`` — the frame_meta wire dict for this seq (or None);
    * ``sol``  — the solution published for THIS frame (or None if none was);
    * ``last_solved`` — the most recent solved record (for frames that did not
      solve themselves, so a satellite trail that broke the solve still has a
      nearby pointing), or None;
    * ``saved_unix`` — wall time the solver handed the frame over, used as the
      time fallback when the sensor metadata is missing (marked as such).
    """
    rec = {"file": fname, "seq": seq}
    t_start = exp_s = None
    if meta:
        try:
            readout_ns = int(meta["sensor_timestamp_ns"]) + int(meta["wall_offset_ns"])
            exp_us = int(meta["exposure_us"])
            t_start = (readout_ns - exp_us * 1000) / 1e9
            exp_s = exp_us / 1e6
            rec["gain"] = float(meta.get("gain", 0.0))
        except (KeyError, TypeError, ValueError):
            t_start = None
    if t_start is not None:
        t_mid = t_start + exp_s / 2.0
        rec.update({
            "time_source": "sensor",
            "exposure_start_utc": _iso(t_start),
            "exposure_mid_utc": _iso(t_mid),
            "exposure_s": exp_s,
            "jd_start": round(unix_to_jd(t_start), 10),
            "jd_mid": round(unix_to_jd(t_mid), 10),
        })
    else:
        # No sensor timing (test mode / older picamera2): the hand-over time is
        # AFTER the exposure ended — good to about one frame period only.
        rec.update({
            "time_source": "handover",
            "saved_utc": _iso(saved_unix),
            "jd_saved": round(unix_to_jd(saved_unix), 10),
        })
    if sol is not None:
        rec.update({k: sol[k] for k in _SOL_KEYS if k in sol})
        if sol.get("matched"):
            rec["matched"] = sol["matched"]
    else:
        rec["solved"] = False
    if not rec.get("solved") and last_solved:
        rec["last_solved"] = last_solved
    return rec


def matched_rows(matched_centroids, matched_stars):
    """olive-solve's matched pairs -> [x, y, ra, dec, mag] rows (centroids come
    as (y, x) in the solver's pixel coordinates, stars as (RA, Dec, mag) J2000).
    Enough to fit a frame's exact pixel<->sky mapping offline. [] if absent."""
    if matched_centroids is None or matched_stars is None:
        return []
    rows = []
    try:
        for (yy, xx), st in zip(matched_centroids, matched_stars):
            rows.append([round(float(xx), 3), round(float(yy), 3),
                         round(float(st[0]), 7), round(float(st[1]), 7),
                         round(float(st[2]), 2) if len(st) > 2 else None])
    except (TypeError, ValueError, IndexError):
        return []
    return rows


def zip_dir_streaming(src_dir, dest_zip, delete_sources=True):
    """Zip every file in src_dir (flat) into dest_zip, STORED, removing each
    source right after it is added. Writes dest_zip + '.tmp' then renames.
    Returns the number of files archived."""
    tmp = dest_zip + ".tmp"
    names = sorted(os.listdir(src_dir))
    # session.json first, frames.jsonl next, then the frames in order.
    names.sort(key=lambda n: (n != "session.json", n != "frames.jsonl", n))
    n = 0
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED,
                         allowZip64=True) as zf:
        for name in names:
            path = os.path.join(src_dir, name)
            if not os.path.isfile(path):
                continue
            zf.write(path, arcname=name)
            n += 1
            if delete_sources:
                os.remove(path)
    os.replace(tmp, dest_zip)
    if delete_sources:
        shutil.rmtree(src_dir, ignore_errors=True)
    return n


def list_archives(directory=DEFAULT_DIR):
    """Finished archives, newest first: [{name, bytes, mtime}]."""
    out = []
    try:
        for name in os.listdir(directory):
            if ARCHIVE_RE.match(name):
                st = os.stat(os.path.join(directory, name))
                out.append({"name": name, "bytes": st.st_size,
                            "mtime": st.st_mtime})
    except FileNotFoundError:
        pass
    out.sort(key=lambda a: a["name"], reverse=True)
    return out


def prune_archives(directory=DEFAULT_DIR, keep_mb=8000.0, protect=None):
    """Delete the OLDEST finished archives until the total is at most
    ``keep_mb`` (0 or less = no limit). ``protect`` (an archive name, e.g.
    the one just written) and the newest archive are never deleted, even if
    one alone is over the limit. Returns the names deleted."""
    if not keep_mb or keep_mb <= 0:
        return []
    arcs = list_archives(directory)                    # newest first
    total = sum(a["bytes"] for a in arcs) / 1e6
    gone = []
    for a in reversed(arcs[1:]):                       # oldest first, skip newest
        if total <= keep_mb:
            break
        if a["name"] == protect:
            continue
        try:
            os.remove(os.path.join(directory, a["name"]))
        except OSError as e:
            log.warning("Could not delete old live capture %s: %s", a["name"], e)
            continue
        total -= a["bytes"] / 1e6
        gone.append(a["name"])
        log.info("Deleted old live capture %s (keeping captures under %.0f MB)",
                 a["name"], keep_mb)
    return gone


def tone_info(tuning_path):
    """The camera's brightness transform, copied from the libcamera tuning
    file the camera loaded, so offline photometry can undo it exactly
    (``tests/sat_photometry.py``): the contrast (gamma) curve as flat
    [in, out, in, out, ...] 16-bit points, whether adaptive contrast is on,
    and the black level. None if the file can't be read."""
    try:
        with open(tuning_path) as f:
            t = json.load(f)
    except (OSError, ValueError, TypeError):
        return None
    algos = t.get("algorithms", []) if isinstance(t, dict) else []
    out = {"tuning_file": tuning_path}
    for a in algos:
        if not isinstance(a, dict) or len(a) != 1:
            continue
        (k, v), = a.items()
        if k == "rpi.contrast" and isinstance(v, dict):
            out["gamma_curve"] = v.get("gamma_curve")
            out["contrast_enhance"] = bool(v.get("ce_enable", 1))
        elif k == "rpi.black_level" and isinstance(v, dict):
            out["black_level"] = v.get("black_level")
        elif k == "rpi.alsc":
            out["lens_shading_correction"] = True
    out.setdefault("lens_shading_correction", False)
    return out if out.get("gamma_curve") else None


def free_mb(directory):
    try:
        st = os.statvfs(directory)
        return st.f_bavail * st.f_frsize / 1e6
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# Recorder (solver-process side)
# --------------------------------------------------------------------------- #

class LiveRecorder:
    """Owns one recording session at a time. Thread-safety: ``sync`` /
    ``offer_frame`` / ``note_solution`` are called from the solver loop thread;
    everything that touches disk runs on the writer thread."""

    def __init__(self, shared_cfg, cfg, directory=DEFAULT_DIR,
                 png_level=_PNG_LEVEL, clock=time.time, queue_max=_QUEUE_MAX):
        self._scfg = shared_cfg
        self._cfg = cfg
        self.directory = directory
        self._png_level = png_level
        self._clock = clock
        self._q = queue.Queue(maxsize=queue_max)
        self._lock = threading.Lock()
        self.active = False          # read by the solver loop each frame
        self._sid = None
        self._done_ids = set()       # sessions finished/stopped (never restart)
        self._control = None
        self._pending = None         # (seq, frame_copy, meta, saved_unix)
        self._last_solved = None
        self._frames = 0
        self._dropped = 0
        self._bytes = 0
        self._started_unix = None
        self._stop_reason = None
        self._last_archive = None
        self._error = None
        self._state = "idle"
        self._last_status_t = 0.0
        self._thread = None
        self._finalize_sid = None    # set by _request_stop; the writer zips it
        self._matches = None         # (seq, rows) from note_matches
        self.max_mb = float(getattr(cfg, "live_capture_max_mb", 4000.0))
        self.min_free_mb = float(getattr(cfg, "live_capture_min_free_mb", 1000.0))
        self.max_minutes = float(getattr(cfg, "live_capture_max_minutes", 60.0))
        self.keep_mb = float(getattr(cfg, "live_capture_keep_mb", 8000.0))

    # ---- solver-loop side (cheap) -----------------------------------------
    def start_thread(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="live-capture")
            self._thread.start()

    def sync(self, control):
        """Per-frame: compare the comms control dict with our state (no IPC —
        the caller passes the value from its per-frame shared_cfg snapshot)."""
        control = control or {}
        sid = control.get("id")
        if control.get("on") and valid_session_id(sid):
            # Never start while the previous session is still being zipped:
            # the counters and status belong to it until it is closed.
            if (not self.active and sid not in self._done_ids
                    and self._finalize_sid is None):
                self._begin(sid, control)
        elif self.active:
            self._request_stop("stopped by user")

    def offer_frame(self, frame, seq, meta_ring=None):
        """Hand the current frame over (copy). Flushes the previous pending
        frame if its solution never arrived."""
        if not self.active:
            return
        import numpy as np
        from diofinder import frame_meta as _fm
        meta = _fm.lookup(meta_ring, seq) if meta_ring else None
        prev, self._pending = self._pending, (
            int(seq), np.copy(frame), meta, self._clock())
        if prev is not None:
            self._enqueue(prev, None)

    def note_solution(self, sol):
        """Called with every dict the solver publishes to latest_solution."""
        if not self.active or not isinstance(sol, dict):
            return
        if sol.get("solved") and sol.get("center_ra_deg") is not None:
            self._last_solved = {
                "seq": sol.get("seq"),
                "center_ra_deg": sol.get("center_ra_deg"),
                "center_dec_deg": sol.get("center_dec_deg"),
                "roll_deg": sol.get("roll_deg"),
                "fov_deg": sol.get("fov_deg"),
                "utc": _iso(self._clock()),
            }
        p = self._pending
        if p is not None and sol.get("seq") == p[0]:
            self._pending = None
            m = self._matches
            if m is not None and m[0] == p[0]:
                sol = dict(sol, matched=m[1])
            self._matches = None
            self._enqueue(p, sol)

    def note_matches(self, seq, matched_centroids, matched_stars):
        """The solver's matched star pairs for frame ``seq`` (olive-solve
        ``return_matches``: centroids as (y, x) in the solver's pixel
        coordinates, stars as (RA, Dec, mag) J2000). Stored as
        [x, y, ra, dec, mag] rows so a WCS can be fitted to each frame
        offline — the exact pixel<->sky mapping, with no roll/mirror
        convention to get wrong. Must be called before the frame's solution
        is published (it rides along with it)."""
        if not self.active:
            return
        rows = matched_rows(matched_centroids, matched_stars)
        if rows:
            self._matches = (int(seq), rows)

    # ---- internals ---------------------------------------------------------
    def _session_dir(self, sid):
        return os.path.join(self.directory, f"livecap_{sid}.partial")

    def _begin(self, sid, control):
        with self._lock:
            self._sid = sid
            self._control = dict(control)
            self._frames = self._dropped = self._bytes = 0
            self._started_unix = self._clock()
            self._stop_reason = None
            self._error = None
            self._last_solved = None
            self._pending = None
            self._state = "recording"
            self.active = True
        try:
            os.makedirs(self._session_dir(sid), exist_ok=True)
        except OSError as e:
            self._fail(f"cannot create capture folder: {e}")
            return
        self.start_thread()
        log.info("Live capture started: %s", sid)
        self._publish_status(force=True)

    def _enqueue(self, pending, sol):
        try:
            # last_solved is captured NOW: by the time the writer gets to this
            # frame, later solves will have moved it on.
            self._q.put_nowait((self._last_solved, pending, sol, self._sid))
        except queue.Full:
            self._dropped += 1

    def _request_stop(self, reason):
        if not self.active:
            return
        with self._lock:
            self.active = False
            self._stop_reason = self._stop_reason or reason
            self._state = "finalizing"
            sid = self._sid
            self._done_ids.add(sid)
            p, self._pending = self._pending, None
            if p is not None:
                # Non-blocking: this can run on the writer thread itself (size
                # or disk limit), which must never wait on its own queue.
                try:
                    self._q.put_nowait((self._last_solved, p, None, sid))
                except queue.Full:
                    self._dropped += 1
            # The writer zips the session once every queued frame is written.
            self._finalize_sid = sid
        self.start_thread()
        self._publish_status(force=True)

    def _fail(self, msg):
        log.warning("Live capture: %s", msg)
        self._error = msg
        self._request_stop("error: " + msg)

    def _publish_status(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_status_t < _STATUS_EVERY_S:
            return
        self._last_status_t = now
        st = self.status()
        try:
            self._scfg["live_capture_status"] = st
        except Exception:
            pass

    def status(self):
        el = (self._clock() - self._started_unix) if self._started_unix else 0.0
        return {
            "state": self._state, "id": self._sid,
            "frames": self._frames, "dropped": self._dropped,
            "bytes": self._bytes,
            "started_utc": _iso(self._started_unix) if self._started_unix else None,
            "elapsed_s": round(el, 1) if self._state == "recording" else None,
            "stop_reason": self._stop_reason,
            "last_archive": self._last_archive,
            "error": self._error,
            "t": self._clock(),
        }

    # ---- writer thread -----------------------------------------------------
    def _run(self):
        last_poll = time.monotonic()
        while True:
            try:
                item = self._q.get(timeout=1.0)
            except queue.Empty:
                item = None
            try:
                if item is not None:
                    last_solved, pending, sol, sid = item
                    self._write_frame(sid, pending, sol, last_solved)
                if self._finalize_sid is not None and self._q.empty():
                    sid, self._finalize_sid = self._finalize_sid, None
                    self._finalize(sid)
                # While recording, notice a stop even when no frames arrive
                # (dark frames skip the solver path that calls sync()).
                now = time.monotonic()
                if self.active and self._over_time():
                    self._request_stop(f"time limit ({self.max_minutes:g} min) reached")
                if self.active and now - last_poll >= _CONTROL_POLL_S:
                    last_poll = now
                    try:
                        ctl = self._scfg.get("live_capture") or {}
                    except Exception:
                        ctl = {}
                    if not ctl.get("on") or ctl.get("id") != self._sid:
                        self._request_stop("stopped by user")
                if self.active:
                    self._publish_status()
            except Exception as e:      # never let the writer die
                log.warning("live capture writer error: %s", e)
                self._error = str(e)

    def _over_time(self):
        """True once a time limit is set (> 0) and the session has run it out,
        so a forgotten capture doesn't run on until the size cap."""
        return (self.max_minutes > 0 and self._started_unix is not None
                and self._clock() - self._started_unix >= self.max_minutes * 60.0)

    def _write_frame(self, sid, pending, sol, last_solved=None):
        from PIL import Image
        seq, frame, meta, saved_unix = pending
        d = self._session_dir(sid)
        if not os.path.isdir(d):
            return
        if meta is None:
            # The camera publishes frame_meta just AFTER the frame itself, so
            # the solver's snapshot can miss it; the ring holds 8, look again.
            try:
                from diofinder import frame_meta as _fm
                meta = _fm.lookup(self._scfg.get("frame_meta"), seq)
            except Exception:
                meta = None
        idx = self._frames + 1
        fname = f"frame_{idx:06d}.png"
        path = os.path.join(d, fname)
        Image.fromarray(frame, mode="L").save(path, format="PNG",
                                              compress_level=self._png_level)
        size = os.path.getsize(path)
        rec = frame_record(fname, seq, meta, sol, last_solved, saved_unix)
        with open(os.path.join(d, "frames.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")
        self._frames = idx
        self._bytes += size
        if self._bytes / 1e6 >= self.max_mb:
            self._request_stop(f"size limit ({self.max_mb:.0f} MB) reached")
        elif self._over_time():
            self._request_stop(f"time limit ({self.max_minutes:g} min) reached")
        else:
            fm = free_mb(self.directory)
            if fm is not None and fm < self.min_free_mb:
                self._request_stop(f"disk nearly full ({fm:.0f} MB free)")

    def _session_info(self, sid, ended_unix):
        ctl = self._control or {}
        site = ctl.get("site") or {}
        w = int(getattr(self._cfg, "frame_width", 960))
        h = int(getattr(self._cfg, "frame_height", 760))
        fov = (self._last_solved or {}).get("fov_deg") or getattr(self._cfg, "fov_deg", None)
        try:
            from diofinder import horizon as _hz
            plausible = bool(_hz.clock_is_plausible())
        except Exception:
            plausible = None
        return {
            "format": FORMAT,
            "id": sid,
            "started_utc": _iso(self._started_unix) if self._started_unix else None,
            "ended_utc": _iso(ended_unix),
            "frames": self._frames,
            "dropped": self._dropped,
            "stop_reason": self._stop_reason,
            "observer": {
                "latitude_deg": site.get("latitude_deg"),
                "longitude_deg": site.get("longitude_deg"),
                "longitude_convention": "east-positive",
                "elevation_m": site.get("elevation_m"),
            },
            "camera": {
                "width_px": w, "height_px": h,
                "matched_columns": ["x", "y", "ra_deg", "dec_deg", "mag"],
                "fov_width_deg": fov,
                "fov_radius_deg": fov_radius_deg(fov, w, h),
                "pixel_origin": "top-left; (0.5, 0.5) is the centre of pixel "
                                "(0, 0) (sycamore extractor convention, used "
                                "by the matched x, y)",
            },
            "coordinates": "J2000/ICRS; center_ra_deg/center_dec_deg = image "
                           "centre, ra_deg/dec_deg = aim point (boresight)",
            "time_reference": "exposure_start_utc = sensor first-row exposure "
                              "start (rolling shutter: later rows are later); "
                              "only as accurate as the Pi clock",
            "clock_plausible": plausible,
            # Where the time came from when recording started (network time or
            # SkySafari's sync, which sets whole seconds only).
            "clock": ctl.get("clock"),
            # The brightness transform (tone curve) in force, for photometry.
            "tone": tone_info(getattr(self._cfg, "camera_tuning_file", "") or ""),
            "test_mode": bool(ctl.get("test_mode", False)),
            "version": getattr(self._cfg, "version", None),
        }

    def _finalize(self, sid):
        d = self._session_dir(sid)
        ended = self._clock()
        try:
            if os.path.isdir(d):
                with open(os.path.join(d, "session.json"), "w") as f:
                    json.dump(self._session_info(sid, ended), f, indent=2)
                dest = os.path.join(self.directory, archive_name(sid))
                n = zip_dir_streaming(d, dest)
                self._last_archive = os.path.basename(dest)
                log.info("Live capture %s closed: %d frames (%d dropped) -> %s",
                         sid, self._frames, self._dropped, dest)
                prune_archives(self.directory, self.keep_mb,
                               protect=self._last_archive)
                if n <= 2 and self._frames == 0:
                    log.info("Live capture %s recorded no frames", sid)
        except Exception as e:
            self._error = f"could not write archive: {e}"
            log.warning("Live capture %s: %s", sid, self._error)
        with self._lock:
            self._state = "idle"
        self._publish_status(force=True)


def recover_partials(directory=DEFAULT_DIR, keep_mb=None):
    """Zip any ``.partial`` session left by a crash / power cut, then (when
    ``keep_mb`` is given) apply the size cap. Returns the archive names
    written. Safe to call at every solver start."""
    out = []
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return out
    for name in sorted(names):
        m = re.match(r"^livecap_(\d{8}T\d{6}Z)\.partial$", name)
        if not m:
            continue
        src = os.path.join(directory, name)
        dest = os.path.join(directory, archive_name(m.group(1)))
        try:
            if not os.path.exists(os.path.join(src, "session.json")):
                with open(os.path.join(src, "session.json"), "w") as f:
                    json.dump({"format": FORMAT, "id": m.group(1),
                               "stop_reason": "recovered after an unexpected "
                                              "stop (crash or power loss)"},
                              f, indent=2)
            zip_dir_streaming(src, dest)
            out.append(os.path.basename(dest))
            log.info("Recovered unfinished live capture -> %s", dest)
        except Exception as e:
            log.warning("Could not recover live capture %s: %s", name, e)
    if keep_mb is not None:
        prune_archives(directory, keep_mb)
    return out


class SolutionTap:
    """Wraps the solver's latest_solution proxy so every published dict is also
    seen by the recorder (no change to the many publish sites). Only
    ``update`` is intercepted; everything else passes through."""

    def __init__(self, proxy, recorder, history=None):
        self._proxy = proxy
        self._rec = recorder
        self._history = history      # optional deque of recent seq-tagged publishes

    def update(self, d=None, **kw):
        self._proxy.update(d or {}, **kw)
        if self._history is not None and isinstance(d, dict) and d.get("seq") is not None:
            self._history.append(d)
        if self._rec.active:
            try:
                self._rec.note_solution(d)
            except Exception:
                pass

    def __getattr__(self, name):
        return getattr(self._proxy, name)
