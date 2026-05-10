"""
Comms worker process.

Pinned to its dedicated CPU. Two server endpoints:

  1. LX200 TCP server on cfg.lx200_port (default 4060) -- talks to
     SkySafari and any other LX200 client. Handles :GR/:GD pointing
     queries and the :Sr/:Sd/:CM# boresight alignment workflow.

  2. Maintenance Unix socket at /run/efinder/maint.sock -- accepts
     newline-delimited JSON requests for inspection, calibration,
     boresight management, and exposure tuning. Used by the
     efinder-ctl shell tool and (eventually) the web UI.

Both servers share the same shared dicts (latest_solution,
shared_cfg) and the same queues to the solver and camera. The
maintenance socket runs in its own thread so a slow LX200 client
doesn't delay maintenance commands and vice versa.
"""

import itertools
import json
import logging
import os
import socket
import threading
import time
from queue import Empty

from efinder import config as cfg_mod
from efinder.align import AlignRequest, AlignResult, CommsAlignState
from efinder.maint import MaintRequest, MaintResponse, SOCKET_PATH
from efinder.worker_cmds import (
    SolverCmd, CameraCmd,
    SOLVER_OP_CALIBRATION_STATUS, SOLVER_OP_CALIBRATION_RESET,
    SOLVER_OP_POLAR_START, SOLVER_OP_POLAR_STATUS,
    SOLVER_OP_POLAR_CANCEL, SOLVER_OP_POLAR_SET_LATITUDE,
    CAMERA_OP_GET_EXPOSURE, CAMERA_OP_SET_EXPOSURE, CAMERA_OP_SET_GAIN,
)

log = logging.getLogger("efinder.comms")

_request_id_seq = itertools.count(1)

# Locks serializing maintenance calls to each worker. Per-worker because
# the solver and camera have independent reply queues and there's no
# need to serialize across them.
_solver_call_lock = threading.Lock()
_camera_call_lock = threading.Lock()


def _pin_to_cpu(cpu: int) -> None:
    try:
        os.sched_setaffinity(0, {cpu})
        log.info("Pinned to CPU %d", cpu)
    except Exception as e:
        log.warning("Could not pin to CPU %d: %s", cpu, e)


# ----------------------------------------------------------------------
# LX200 RA/Dec formatting (unchanged from prior version)
# ----------------------------------------------------------------------

def _format_ra(ra_hours: float) -> str:
    ra_hours = ra_hours % 24.0
    h = int(ra_hours); m_full = (ra_hours - h) * 60.0
    m = int(m_full); s = int(round((m_full - m) * 60.0))
    if s == 60: s = 0; m += 1
    if m == 60: m = 0; h = (h + 1) % 24
    return f"{h:02d}:{m:02d}:{s:02d}#"


def _format_dec(dec_deg: float) -> str:
    sign = "+" if dec_deg >= 0 else "-"
    a = abs(dec_deg); d = int(a)
    m_full = (a - d) * 60.0; m = int(m_full)
    s = int(round((m_full - m) * 60.0))
    if s == 60: s = 0; m += 1
    if m == 60: m = 0; d += 1
    return f"{sign}{d:02d}*{m:02d}:{s:02d}#"


# ----------------------------------------------------------------------
# Worker call helpers (used by both LX200 alignment and maint socket)
# ----------------------------------------------------------------------

def _wait_for_reply(reply_q, request_id, timeout_s=5.0):
    """Block until a reply with the matching request_id arrives.

    Replies for other request_ids are discarded -- if a different
    caller posted a request, they're responsible for waiting on it.
    In our setup every caller does its own send-then-wait so this
    is fine.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        remaining = max(0.05, deadline - time.monotonic())
        try:
            reply = reply_q.get(timeout=remaining)
        except Empty:
            continue
        if reply.request_id == request_id:
            return reply
        log.debug("Discarding stale reply id=%s (waiting for %s)",
                  reply.request_id, request_id)
    return None


def _call_solver(op, args, solver_cmd_q, solver_cmd_reply_q, timeout_s=5.0):
    rid = next(_request_id_seq)
    with _solver_call_lock:
        solver_cmd_q.put(SolverCmd(op=op, args=args or {}, request_id=rid))
        return _wait_for_reply(solver_cmd_reply_q, rid, timeout_s=timeout_s)


def _call_camera(op, args, camera_cmd_q, camera_cmd_reply_q, timeout_s=2.0):
    rid = next(_request_id_seq)
    with _camera_call_lock:
        camera_cmd_q.put(CameraCmd(op=op, args=args or {}, request_id=rid))
        return _wait_for_reply(camera_cmd_reply_q, rid, timeout_s=timeout_s)


# ----------------------------------------------------------------------
# Boresight alignment workflow (LX200 :CM#)
# ----------------------------------------------------------------------

def _do_alignment(align_state, cfg, shared_cfg,
                  align_request_q, align_response_q):
    req = align_state.build_request()
    if req is None:
        return "no align target#"

    try:
        while True:
            align_response_q.get_nowait()
    except Empty:
        pass

    align_request_q.put(req)
    log.info("Alignment requested: RA=%.4f Dec=%.4f",
             req.target_ra_deg, req.target_dec_deg)

    deadline = time.monotonic() + CommsAlignState.DEFAULT_TIMEOUT_S
    result = None
    while time.monotonic() < deadline:
        try:
            candidate = align_response_q.get(timeout=0.5)
            if candidate.completed_at >= req.requested_at:
                result = candidate; break
        except Empty:
            continue

    align_state.reset()

    if result is None:
        log.warning("Alignment timed out waiting for solver")
        return "align timeout#"
    if not result.success:
        log.warning("Alignment failed: %s", result.error_message)
        return f"align fail: {result.error_message}#"

    cfg.boresight_y = result.boresight_y
    cfg.boresight_x = result.boresight_x
    shared_cfg["boresight_y"] = result.boresight_y
    shared_cfg["boresight_x"] = result.boresight_x

    try:
        cfg_mod.save_keys({
            "boresight_y": result.boresight_y,
            "boresight_x": result.boresight_x,
        })
    except Exception as e:
        log.warning("Could not persist boresight to config file: %s", e)

    log.info("Alignment complete: boresight=(%.2f, %.2f)",
             result.boresight_y, result.boresight_x)
    return "M31 EX GAL MAG 3.5 SZ178.0'#"


# ----------------------------------------------------------------------
# LX200 command handler
# ----------------------------------------------------------------------

def _handle_lx200_command(cmd, latest_solution, align_state, cfg, shared_cfg,
                          align_request_q, align_response_q, ctx=None):
    if cmd == ":GR":
        sol = dict(latest_solution)
        return _format_ra(sol["ra_deg"] / 15.0).encode("ascii")
    if cmd == ":GD":
        sol = dict(latest_solution)
        return _format_dec(sol["dec_deg"]).encode("ascii")

    # Mount status. SkySafari sends this immediately on connect; if it gets
    # no response (missing '#') it blocks waiting forever.
    # Format: <MountType><Tracking><AlignmentStar># where
    #   A=AltAz P=Polar G=GermanPolar  1=tracking 0=not  2=2-star aligned
    # We present as a fixed alt-az mount that is always tracking. This
    # satisfies SkySafari's init handshake; it does not affect :GR/:GD.
    if cmd == ":GW":
        return b"AT2#"

    if cmd in (":GVN", ":GVP"):
        return f"eFinder {cfg.version}#".encode("ascii")
    # Other firmware-version queries (:GVF firmware string, :GVD date)
    if cmd.startswith(":GV"):
        return f"eFinder {cfg.version}#".encode("ascii")

    if cmd.startswith(":Sr"):
        ok = align_state.set_target_ra(cmd[3:])
        return b"1" if ok else b"0"
    if cmd.startswith(":Sd"):
        ok = align_state.set_target_dec(cmd[3:])
        return b"1" if ok else b"0"
    if cmd == ":CM":
        reply = _do_alignment(align_state, cfg, shared_cfg,
                              align_request_q, align_response_q)
        return reply.encode("ascii")

    # :St sets site latitude in LX200 high-precision form sDD*MM (or sDD*MM:SS).
    # Parse and propagate to the polar aligner if we have a maint context.
    if cmd.startswith(":St"):
        try:
            from efinder.align import _parse_dec_dms  # same DMS format
            lat = _parse_dec_dms(cmd[3:])
            if ctx is not None:
                cfg.latitude_deg = lat
                cfg_mod.save_keys({"latitude_deg": lat})
                # Propagate to the live solver-side PolarSession. Done in a
                # daemon thread so the LX200 handler returns immediately to
                # SkySafari without waiting for _solver_call_lock (which may
                # be held by a concurrent maint call). Longer timeout gives
                # the solver time to finish its current solve/gRPC call first.
                def _push_lat(l=lat):
                    _call_solver(SOLVER_OP_POLAR_SET_LATITUDE,
                                 {"latitude_deg": l},
                                 ctx.solver_cmd_q, ctx.solver_cmd_reply_q,
                                 timeout_s=10.0)
                threading.Thread(target=_push_lat, daemon=True).start()
                log.info("Latitude from LX200 :St -> %.4f", lat)
            return b"1"
        except Exception as e:
            log.warning("Could not parse :St latitude %r: %s", cmd[3:], e)
            return b"0"
    if cmd.startswith(":Sg"):
        try:
            from efinder.align import _parse_dec_dms
            # LX200 longitude format: 'sDDD*MM' or 'sDDD*MM:SS' (same DMS
            # shape as Dec; sign indicates east/west). Some clients send
            # longitude as 0-360 instead of +/-180; we accept both.
            lon = _parse_dec_dms(cmd[3:])
            cfg.longitude_deg = lon
            cfg_mod.save_keys({"longitude_deg": lon})
            log.info("Longitude from LX200 :Sg -> %.4f", lon)
            return b"1"
        except Exception as e:
            log.warning("Could not parse :Sg longitude %r: %s", cmd[3:], e)
            return b"0"

    # :Gt / :Gg -- SkySafari sends these immediately on connect to read the
    # mount's stored site location. Returning a real value satisfies its
    # initialisation handshake and may determine whether it sends :St/:Sg
    # back with its GPS coordinates.
    if cmd == ":Gt":
        lat = cfg.latitude_deg or 0.0
        sign = "+" if lat >= 0 else "-"
        a = abs(lat)
        d = int(a)
        m = int(round((a - d) * 60.0))
        return f"{sign}{d:02d}*{m:02d}#".encode("ascii")

    if cmd == ":Gg":
        lon = cfg.longitude_deg or 0.0
        sign = "+" if lon >= 0 else "-"
        a = abs(lon)
        d = int(a)
        m = int(round((a - d) * 60.0))
        return f"{sign}{d:03d}*{m:02d}#".encode("ascii")

    if cmd.startswith(":SG") or cmd.startswith(":SL"):
        return b"1"
    if cmd.startswith(":SC"):
        return b"1Updating Planetary Data#                              #"

    # Slew / motion commands -- we don't move anything, just acknowledge.
    if cmd == ":MS":
        return b"0"   # 0 = slewing (accepted); SkySafari expects this
    if cmd.startswith(":M") or cmd.startswith(":R") or cmd == ":Q":
        return b""    # no-reply motion/rate commands; empty is correct

    # Tracking rate: report sidereal.
    if cmd == ":GT":
        return b"60.0#"

    # Slew rate: report maximum.
    if cmd == ":Gr":
        return b"4#"

    # Time/date queries: return plausible static values. SkySafari uses its
    # own GPS/clock; it sends these to sync the mount, not to read from it.
    if cmd in (":GS", ":GL"):
        return b"00:00:00#"
    if cmd == ":GC":
        return b"01/01/00#"
    if cmd == ":GG":
        return b"+00#"

    # Altitude/azimuth: not computed; return zero.
    if cmd in (":GA", ":GZ"):
        return b"+00*00#"

    # Any other unrecognised command: return an empty-but-terminated response
    # so the client's '#'-delimited parser can move on instead of blocking.
    log.debug("Unhandled LX200 command: %r", cmd)
    return b"#"


# ----------------------------------------------------------------------
# Maintenance command handler
# ----------------------------------------------------------------------

def _handle_maint_command(req: MaintRequest, ctx) -> MaintResponse:
    """Dispatch a maintenance command. ctx bundles the shared state
    and queues so we don't pass 8 args.
    """
    cmd = req.cmd
    args = req.args

    try:
        # ---- Read-only inspection ----
        if cmd == "ping":
            return MaintResponse(ok=True, result={"pong": True})

        if cmd == "version":
            return MaintResponse(ok=True, result={
                "version": ctx.cfg.version,
            })

        if cmd == "status":
            sol = dict(ctx.latest_solution)
            return MaintResponse(ok=True, result={
                "solution": sol,
                "boresight": {
                    "y": ctx.shared_cfg.get("boresight_y", ctx.cfg.boresight_y),
                    "x": ctx.shared_cfg.get("boresight_x", ctx.cfg.boresight_x),
                },
                "fov_deg": ctx.shared_cfg.get("fov_deg", ctx.cfg.fov_deg),
                "config_summary": ctx.cfg.summary(),
            })

        # ---- Boresight ----
        if cmd == "boresight_show":
            return MaintResponse(ok=True, result={
                "y": ctx.shared_cfg.get("boresight_y", ctx.cfg.boresight_y),
                "x": ctx.shared_cfg.get("boresight_x", ctx.cfg.boresight_x),
            })

        if cmd == "boresight_center":
            new_y = ctx.cfg.frame_height / 2.0
            new_x = ctx.cfg.frame_width / 2.0
            ctx.cfg.boresight_y = new_y
            ctx.cfg.boresight_x = new_x
            ctx.shared_cfg["boresight_y"] = new_y
            ctx.shared_cfg["boresight_x"] = new_x
            cfg_mod.save_keys({"boresight_y": new_y, "boresight_x": new_x})
            return MaintResponse(ok=True, result={"y": new_y, "x": new_x})

        if cmd == "boresight_set":
            try:
                new_y = float(args["y"]); new_x = float(args["x"])
            except (KeyError, ValueError, TypeError) as e:
                return MaintResponse(ok=False,
                                     error=f"boresight_set requires numeric y, x: {e}")
            if not (0 <= new_y <= ctx.cfg.frame_height) or not (0 <= new_x <= ctx.cfg.frame_width):
                return MaintResponse(ok=False,
                                     error="y/x outside frame bounds")
            ctx.cfg.boresight_y = new_y
            ctx.cfg.boresight_x = new_x
            ctx.shared_cfg["boresight_y"] = new_y
            ctx.shared_cfg["boresight_x"] = new_x
            cfg_mod.save_keys({"boresight_y": new_y, "boresight_x": new_x})
            return MaintResponse(ok=True, result={"y": new_y, "x": new_x})

        # ---- Calibration ----
        if cmd == "calibration_status":
            reply = _call_solver(SOLVER_OP_CALIBRATION_STATUS, {},
                                 ctx.solver_cmd_q, ctx.solver_cmd_reply_q)
            if reply is None:
                return MaintResponse(ok=False, error="solver did not respond in time")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            return MaintResponse(ok=True, result=reply.result)

        if cmd == "calibration_reset":
            reply = _call_solver(SOLVER_OP_CALIBRATION_RESET, {},
                                 ctx.solver_cmd_q, ctx.solver_cmd_reply_q)
            if reply is None:
                return MaintResponse(ok=False, error="solver did not respond in time")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            return MaintResponse(ok=True, result=reply.result)

        # ---- Polar alignment ----
        if cmd == "polar_start":
            reply = _call_solver(SOLVER_OP_POLAR_START, {},
                                 ctx.solver_cmd_q, ctx.solver_cmd_reply_q)
            if reply is None:
                return MaintResponse(ok=False, error="solver did not respond in time")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            return MaintResponse(ok=True, result=reply.result)

        if cmd == "polar_status":
            reply = _call_solver(SOLVER_OP_POLAR_STATUS, {},
                                 ctx.solver_cmd_q, ctx.solver_cmd_reply_q)
            if reply is None:
                return MaintResponse(ok=False, error="solver did not respond in time")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            result = reply.result or {}
            # If the solver's PolarSession doesn't have latitude yet (async
            # :St push still in-flight, or first boot before SkySafari has
            # connected), fall back to the value in cfg, which is updated
            # synchronously in the :St handler and persisted to disk.
            if not result.get("latitude_deg") and ctx.cfg.latitude_deg:
                result = {**result, "latitude_deg": ctx.cfg.latitude_deg}
            return MaintResponse(ok=True, result=result)

        if cmd == "polar_cancel":
            reply = _call_solver(SOLVER_OP_POLAR_CANCEL, {},
                                 ctx.solver_cmd_q, ctx.solver_cmd_reply_q)
            if reply is None:
                return MaintResponse(ok=False, error="solver did not respond in time")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            return MaintResponse(ok=True, result=reply.result)

        if cmd == "polar_set_latitude":
            try:
                lat = float(args["latitude_deg"])
            except (KeyError, ValueError, TypeError) as e:
                return MaintResponse(ok=False,
                                     error=f"polar_set_latitude requires numeric latitude_deg: {e}")
            persist = bool(args.get("persist", True))  # default True for latitude
            reply = _call_solver(SOLVER_OP_POLAR_SET_LATITUDE,
                                 {"latitude_deg": lat},
                                 ctx.solver_cmd_q, ctx.solver_cmd_reply_q)
            if reply is None:
                return MaintResponse(ok=False, error="solver did not respond in time")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            ctx.cfg.latitude_deg = lat
            if persist:
                cfg_mod.save_keys({"latitude_deg": lat})
            return MaintResponse(ok=True, result={
                **reply.result, "persisted": persist,
            })

        # ---- Camera ----
        if cmd == "exposure_get":
            reply = _call_camera(CAMERA_OP_GET_EXPOSURE, {},
                                 ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
            if reply is None:
                return MaintResponse(ok=False, error="camera did not respond in time")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            return MaintResponse(ok=True, result=reply.result)

        if cmd == "exposure_set":
            try:
                new_s = float(args["exposure_s"])
            except (KeyError, ValueError, TypeError) as e:
                return MaintResponse(ok=False,
                                     error=f"exposure_set requires numeric exposure_s: {e}")
            persist = bool(args.get("persist", False))
            reply = _call_camera(CAMERA_OP_SET_EXPOSURE, {"exposure_s": new_s},
                                 ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
            if reply is None:
                return MaintResponse(ok=False, error="camera did not respond in time")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            if persist:
                cfg_mod.save_keys({"exposure_s": new_s})
            return MaintResponse(ok=True, result={
                **reply.result, "persisted": persist,
            })

        if cmd == "gain_set":
            try:
                new_g = float(args["gain"])
            except (KeyError, ValueError, TypeError) as e:
                return MaintResponse(ok=False,
                                     error=f"gain_set requires numeric gain: {e}")
            persist = bool(args.get("persist", False))
            reply = _call_camera(CAMERA_OP_SET_GAIN, {"gain": new_g},
                                 ctx.camera_cmd_q, ctx.camera_cmd_reply_q)
            if reply is None:
                return MaintResponse(ok=False, error="camera did not respond in time")
            if not reply.ok:
                return MaintResponse(ok=False, error=reply.error)
            if persist:
                cfg_mod.save_keys({"gain": new_g})
            return MaintResponse(ok=True, result={
                **reply.result, "persisted": persist,
            })

        return MaintResponse(ok=False, error=f"unknown command: {cmd!r}")

    except Exception as e:
        log.exception("Maintenance command %r failed", cmd)
        return MaintResponse(ok=False, error=f"{type(e).__name__}: {e}")


class _MaintContext:
    """Tiny bag of shared state for the maintenance dispatcher."""
    def __init__(self, *, cfg, latest_solution, shared_cfg,
                 solver_cmd_q, solver_cmd_reply_q,
                 camera_cmd_q, camera_cmd_reply_q):
        self.cfg = cfg
        self.latest_solution = latest_solution
        self.shared_cfg = shared_cfg
        self.solver_cmd_q = solver_cmd_q
        self.solver_cmd_reply_q = solver_cmd_reply_q
        self.camera_cmd_q = camera_cmd_q
        self.camera_cmd_reply_q = camera_cmd_reply_q


def _handle_maint_client(client, ctx):
    """Handle one maintenance socket connection in its own thread."""
    client.settimeout(15.0)
    try:
        buf = b""
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                if not line.strip():
                    continue
                try:
                    req = MaintRequest.decode(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    client.sendall(MaintResponse(
                        ok=False,
                        error=f"bad request: {e}").encode())
                    continue
                resp = _handle_maint_command(req, ctx)
                client.sendall(resp.encode())
    except socket.timeout:
        pass
    except Exception as e:
        log.warning("Maint client error: %s", e)
    finally:
        try: client.close()
        except Exception: pass


def _serve_maint_socket(ctx, socket_path=None):
    """Bind the Unix socket and serve clients, one thread per connection."""
    if socket_path is None:
        socket_path = SOCKET_PATH
    # Ensure the socket directory exists. /run/efinder is created by
    # systemd RuntimeDirectory= but if we're started by hand this won't
    # exist; create it best-effort.
    sock_dir = os.path.dirname(socket_path)
    try:
        os.makedirs(sock_dir, exist_ok=True)
    except Exception as e:
        log.warning("Could not ensure %s exists: %s", sock_dir, e)

    # Remove any stale socket from a prior run
    try:
        if os.path.exists(socket_path):
            os.unlink(socket_path)
    except Exception as e:
        log.warning("Could not remove stale socket %s: %s", socket_path, e)

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(socket_path)
    # Permissive enough for the efinder group to use efinder-ctl.
    try:
        os.chmod(socket_path, 0o660)
    except Exception as e:
        log.warning("Could not chmod socket: %s", e)
    sock.listen(64)
    log.info("Maintenance socket listening at %s", socket_path)

    while True:
        try:
            client, _ = sock.accept()
        except Exception as e:
            log.error("Maint accept failed: %s; retrying in 1s", e)
            time.sleep(1); continue
        t = threading.Thread(
            target=_handle_maint_client, args=(client, ctx), daemon=True)
        t.start()


def _serve_lx200(latest_solution, shared_cfg, cfg,
                 align_request_q, align_response_q, ctx):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", cfg.lx200_port))
    sock.listen(8)
    log.info("LX200 server listening on :%d", cfg.lx200_port)

    while True:
        client, addr = sock.accept()
        # Disable Nagle so small LX200 responses (5-15 bytes) are sent
        # immediately rather than waiting to be batched.  Without this,
        # the OS can hold a ":GR#" reply for tens of milliseconds hoping
        # for more data to combine with; SkySafari interprets the delay
        # as a connection problem and disconnects.
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # TCP keepalives let the kernel detect a dead connection (dropped
        # WiFi, phone out of range) without waiting for lx200_client_timeout_s.
        # After 10 s idle: send a probe every 5 s, drop after 3 failures
        # (worst-case detection ~25 s vs the default 30 s recv timeout).
        client.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        try:
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        except AttributeError:
            pass  # TCP_KEEPIDLE etc. are Linux-only; SO_KEEPALIVE still helps
        client.settimeout(cfg.lx200_client_timeout_s)
        log.info("LX200 client from %s", addr)
        align_state = CommsAlignState()
        try:
            buf = b""
            while True:
                chunk = client.recv(256)
                if not chunk:
                    break
                buf += chunk
                while b"#" in buf:
                    raw, _, buf = buf.partition(b"#")
                    cmd = raw.decode("ascii", errors="ignore").strip()
                    if not cmd.startswith(":"):
                        continue
                    reply = _handle_lx200_command(
                        cmd, latest_solution, align_state, cfg,
                        shared_cfg, align_request_q, align_response_q, ctx)
                    if reply:
                        client.sendall(reply)
        except socket.timeout:
            log.info("LX200 client %s timed out", addr)
        except Exception as e:
            log.warning("LX200 client %s error: %s", addr, e)
        finally:
            try: client.close()
            except Exception: pass


def comms_main(latest_solution, shared_cfg,
               align_request_q, align_response_q,
               solver_cmd_q, solver_cmd_reply_q,
               camera_cmd_q, camera_cmd_reply_q,
               cfg):
    logging.basicConfig(
        level=os.environ.get("EFINDER_LOGLEVEL", "INFO"),
        format="comms %(levelname)s %(message)s",
    )
    _pin_to_cpu(cfg.cpu_comms)

    # Maintenance socket runs in a daemon thread alongside the
    # LX200 main thread.
    ctx = _MaintContext(
        cfg=cfg, latest_solution=latest_solution, shared_cfg=shared_cfg,
        solver_cmd_q=solver_cmd_q, solver_cmd_reply_q=solver_cmd_reply_q,
        camera_cmd_q=camera_cmd_q, camera_cmd_reply_q=camera_cmd_reply_q,
    )
    maint_thread = threading.Thread(
        target=_serve_maint_socket, args=(ctx,),
        name="efinder-maint", daemon=True)
    maint_thread.start()

    # LX200 server runs in the main thread.
    while True:
        try:
            _serve_lx200(latest_solution, shared_cfg, cfg,
                         align_request_q, align_response_q, ctx)
        except Exception as e:
            log.error("LX200 server crashed: %s; restarting in 2s", e)
            time.sleep(2)
