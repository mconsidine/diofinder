#!/usr/bin/env python3
"""
eFinder full-pipeline solve diagnostic.

Exercises every layer from frame SHM → cedar-detect centroid extraction →
cedar backend (tetra3 Python) + tetra hybrid backend (tetra3rs), with
detailed per-step timing and failure attribution.

Stages:
  0. Config + all library imports
  1. Frame into SHM (live daemon SHM | test PNG | synthetic)
  2. cedar-detect ExtractCentroids (timed warm-up + N reps)
  3. Cedar backend  — tetra3 Python solver   (N reps, normal + 3× timeout)
  4. Tetra backend  — tetra3rs              (N reps: blind, then seeded)
  5. Summary table

Requires cedar-detect to be running; the efinder daemon may or may not be.

Typical usage:
  # Stop the efinder daemon so SHM is stable, then run:
  sudo systemctl stop efinder
  sudo systemctl start cedar-detect
  sudo /opt/efinder/venv/bin/python3 tests/diag_solve.py

  # With a saved failed/solved capture:
  sudo /opt/efinder/venv/bin/python3 tests/diag_solve.py \\
      --image /var/lib/efinder/captures/capture_20250101_123456_tetra_failed.png

  # Extended timeouts:
  sudo /opt/efinder/venv/bin/python3 tests/diag_solve.py --timeout 5000

  # Read live SHM from the running daemon instead:
  sudo /opt/efinder/venv/bin/python3 tests/diag_solve.py --live-shm

  # Override FOV for a solve test (e.g. if config FOV is wrong):
  sudo /opt/efinder/venv/bin/python3 tests/diag_solve.py --fov 14.0 --fov-err 2.0
"""

import argparse
import math
import sys
import time

sys.path.insert(0, '/opt/efinder')
sys.path.insert(0, '/opt/efinder/proto')

# ── ANSI helpers ──────────────────────────────────────────────────────────────
PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"
INFO = "\033[34mINFO\033[0m"

def tag(label, msg): print(f"  [{label}] {msg}")
def sep(title):      print(f"\n{'='*62}\n{title}\n{'='*62}")

# ── Args ──────────────────────────────────────────────────────────────────────
p = argparse.ArgumentParser(description=__doc__,
                             formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("--image", metavar="PNG",
               help="Path to test PNG (grayscale or RGB)")
p.add_argument("--live-shm", action="store_true",
               help="Read from live efinder_frame_0 SHM (daemon must be running)")
p.add_argument("--timeout", type=int, default=None,
               help="Solve timeout in ms (overrides config)")
p.add_argument("--sigma", type=float, default=None,
               help="Override detect_sigma")
p.add_argument("--fov", type=float, default=None,
               help="Override FOV estimate (degrees)")
p.add_argument("--fov-err", type=float, default=None,
               help="Override FOV max error (degrees)")
p.add_argument("--reps", type=int, default=5,
               help="Solve repetitions per backend (default 5)")
p.add_argument("--extended-timeout", action="store_true",
               help="Also try each backend with 3× the timeout when normal fails")
args = p.parse_args()

# ── Stage 0: Config & imports ─────────────────────────────────────────────────
sep("Stage 0: Config & library imports")

try:
    from efinder.config import load_config
    from efinder.calibration import FovCalibrator
    cfg        = load_config()
    shared_cfg = {}
    cal        = FovCalibrator(cfg, shared_cfg)
    fov_est    = args.fov    if args.fov    is not None else cal.get_fov_estimate()
    fov_err    = args.fov_err if args.fov_err is not None else max(
                     cal.get_fov_max_error(), cfg.fov_max_error_deg)
    timeout_ms = args.timeout if args.timeout is not None else cfg.solve_timeout_ms
    sigma      = args.sigma   if args.sigma   is not None else cfg.detect_sigma
    tag(PASS, f"Config: {cfg.summary()}")
    tag(INFO, f"FOV estimate : {fov_est:.4f}°  max_error : {fov_err:.4f}°")
    tag(INFO, f"Solve timeout: {timeout_ms} ms")
    tag(INFO, f"Detect sigma : {sigma:.1f}  hot_pixels: {cfg.detect_hot_pixels}")
except Exception as e:
    tag(FAIL, f"Config load: {e}")
    sys.exit(1)

w, h = cfg.frame_width, cfg.frame_height

try:
    import numpy as np
    tag(PASS, f"numpy {np.__version__}")
except Exception as e:
    tag(FAIL, f"numpy: {e}"); sys.exit(1)

try:
    import grpc
    import cedar_detect_pb2 as pb
    import cedar_detect_pb2_grpc as pb_grpc
    tag(PASS, "grpc + cedar_detect protobuf")
except Exception as e:
    tag(FAIL, f"gRPC / protobuf: {e}"); sys.exit(1)

cedar_ok = False
t3 = None
try:
    import tetra3 as t3_lib
    t3 = t3_lib.Tetra3(cfg.tetra3_db)
    tag(PASS, f"tetra3 Python  db={cfg.tetra3_db}")
    cedar_ok = True
except Exception as e:
    tag(FAIL, f"tetra3 Python: {e}")

tetra_ok = False
db_rs = None
try:
    import tetra3rs
    db_rs = tetra3rs.SolverDatabase.load_from_file(cfg.tetra3rs_db)
    tag(PASS, f"tetra3rs  db={cfg.tetra3rs_db}")
    tag(INFO, f"  DB: {db_rs.num_stars} stars  {db_rs.num_patterns} patterns  "
              f"fov=[{db_rs.min_fov_deg:.2f}, {db_rs.max_fov_deg:.2f}]°")
    # FOV range sanity check
    if not (db_rs.min_fov_deg <= fov_est <= db_rs.max_fov_deg):
        tag(WARN, f"  fov_estimate ({fov_est:.3f}°) is OUTSIDE the DB range "
                  f"[{db_rs.min_fov_deg:.2f}, {db_rs.max_fov_deg:.2f}]° "
                  "— tetra3rs will not solve. Correct fov_deg in config.")
    tetra_ok = True
except Exception as e:
    tag(FAIL, f"tetra3rs: {e}")

if not cedar_ok and not tetra_ok:
    tag(FAIL, "No solver backend available — cannot continue")
    sys.exit(1)

# ── Stage 1: Frame into SHM ───────────────────────────────────────────────────
sep("Stage 1: Frame source → SHM")

from multiprocessing import shared_memory
from multiprocessing import resource_tracker as _rt

OWN_SHM_NAME = "efinder_diag_frame"

def _borrow_shm(name: str) -> shared_memory.SharedMemory:
    shm = shared_memory.SharedMemory(name=name, create=False)
    try:
        _rt.unregister(shm._name, 'shared_memory')
    except Exception:
        pass
    return shm
own_shm  = None
shm_slot = None

def _cleanup():
    global own_shm
    if own_shm is not None:
        try:
            own_shm.close()
            own_shm.unlink()
        except Exception:
            pass
        own_shm = None

def _make_own_shm(frame):
    try:
        _s = shared_memory.SharedMemory(name=OWN_SHM_NAME, create=False)
        _s.close(); _s.unlink()
    except Exception:
        pass
    shm = shared_memory.SharedMemory(name=OWN_SHM_NAME, create=True, size=frame.nbytes)
    buf = np.ndarray(frame.shape, dtype=np.uint8, buffer=shm.buf)
    np.copyto(buf, frame)
    return shm

if args.live_shm:
    shm_slot = "efinder_frame_0"
    try:
        _s   = _borrow_shm(shm_slot)
        _arr = np.ndarray((h, w), dtype=np.uint8, buffer=_s.buf)
        pk   = int(_arr.max())
        mn   = float(_arr.mean())
        _s.close()
        tag(PASS, f"Live SHM {shm_slot}: peak={pk}  mean={mn:.1f}")
        if pk < 20:
            tag(WARN, f"Peak={pk} < 20 — solver_proc discards this frame; "
                      "exposure may be too low or camera is in bad state")
    except Exception as e:
        tag(FAIL, f"Cannot attach to live SHM: {e}")
        sys.exit(1)

elif args.image:
    try:
        from PIL import Image
        img = Image.open(args.image).convert("L")
        if img.size != (w, h):
            tag(INFO, f"Resizing {img.size} → {(w, h)}")
            img = img.resize((w, h), Image.LANCZOS)
        frame   = np.array(img, dtype=np.uint8)
        own_shm = _make_own_shm(frame)
        shm_slot = OWN_SHM_NAME
        tag(PASS, f"Test image: {args.image}")
        tag(INFO, f"  peak={frame.max()}  mean={frame.mean():.1f}  "
                  f"p95={int(np.percentile(frame, 95))}  "
                  f"nonzero={np.count_nonzero(frame)}")
    except Exception as e:
        tag(FAIL, f"Could not load image: {e}")
        sys.exit(1)

else:
    # Try live SHM automatically; fall back to synthetic
    live_ok = False
    for slot in ("efinder_frame_0", "efinder_frame_1", "efinder_frame_2"):
        try:
            _s   = _borrow_shm(slot)
            _arr = np.ndarray((h, w), dtype=np.uint8, buffer=_s.buf)
            pk   = int(_arr.max())
            mn   = float(_arr.mean())
            _s.close()
            shm_slot = slot
            live_ok  = True
            tag(PASS, f"Found live SHM: {slot}  peak={pk}  mean={mn:.1f}")
            if pk < 20:
                tag(WARN, f"Peak={pk} < 20 — solver would skip this frame")
            break
        except Exception:
            continue

    if not live_ok:
        tag(WARN, "No live SHM found — generating synthetic star field")
        tag(INFO, "  (A blind plate-solve on a synthetic random field will fail)")
        tag(INFO, "  Pass --image <path> to use a real sky capture instead")
        frame = np.zeros((h, w), dtype=np.uint8)
        rng   = np.random.default_rng(42)
        for _ in range(50):
            cy, cx = int(rng.integers(15, h - 15)), int(rng.integers(15, w - 15))
            br     = int(rng.integers(100, 230))
            ys, xs = np.ogrid[-6:7, -6:7]
            patch  = (br * np.exp(-(ys**2 + xs**2) / 5.0)).astype(np.uint8)
            y0, y1 = max(0, cy - 6), min(h, cy + 7)
            x0, x1 = max(0, cx - 6), min(w, cx + 7)
            frame[y0:y1, x0:x1] = np.maximum(
                frame[y0:y1, x0:x1], patch[:y1 - y0, :x1 - x0])
        own_shm  = _make_own_shm(frame)
        shm_slot = OWN_SHM_NAME
        tag(INFO, f"Synthetic: 50 Gaussian stars  peak={frame.max()}")

# ── Stage 2: ExtractCentroids ─────────────────────────────────────────────────
sep(f"Stage 2: Cedar-detect ExtractCentroids  sigma={sigma:.1f}")

channel = grpc.insecure_channel(cfg.cedar_detect_socket)
stub    = pb_grpc.CedarDetectStub(channel)

def _req(reopen: bool) -> "pb.CentroidsRequest":
    return pb.CentroidsRequest(
        input_image=pb.Image(
            width=w, height=h, shmem_name=shm_slot, reopen_shmem=reopen),
        sigma=sigma,
        detect_hot_pixels=cfg.detect_hot_pixels,
        use_binned_for_star_candidates=cfg.detect_use_binned,
        return_binned=False,
    )

# Warm-up
try:
    t0   = time.monotonic()
    resp = stub.ExtractCentroids(_req(reopen=True), timeout=5.0)
    wu   = (time.monotonic() - t0) * 1000.0
    tag(PASS, f"Warm-up (reopen=True): {wu:.0f}ms  "
              f"stars={len(resp.star_candidates)}  "
              f"peak={resp.peak_star_pixel}  noise={resp.noise_estimate:.2f}")
except Exception as e:
    tag(FAIL, f"ExtractCentroids warm-up: {e}")
    _cleanup()
    sys.exit(1)

# Timed reps
print()
ext_times = []
for i in range(args.reps):
    try:
        t0   = time.monotonic()
        resp = stub.ExtractCentroids(_req(reopen=False), timeout=5.0)
        ms   = (time.monotonic() - t0) * 1000.0
        n    = len(resp.star_candidates)
        pk   = int(resp.peak_star_pixel) if resp.peak_star_pixel else 0
        ns   = float(resp.noise_estimate)
        ext_times.append(ms)
        status = PASS if n >= cfg.min_centroids else WARN
        tag(status, f"[{i+1:2d}] {ms:6.1f}ms  stars={n:3d}  peak={pk:3d}  noise={ns:.2f}")
    except Exception as e:
        tag(FAIL, f"[{i+1:2d}] RPC failed: {e}")

if ext_times:
    avg_ext = sum(ext_times) / len(ext_times)
    print(f"\n  Timing:  avg={avg_ext:.1f}ms  min={min(ext_times):.1f}ms  "
          f"max={max(ext_times):.1f}ms  ({len(ext_times)}/{args.reps} ok)")

# Final extraction — keep the result for the solve stages
t0          = time.monotonic()
resp        = stub.ExtractCentroids(_req(reopen=False), timeout=5.0)
extract_ms  = (time.monotonic() - t0) * 1000.0
n_stars     = len(resp.star_candidates)
peak_pixel  = int(resp.peak_star_pixel) if resp.peak_star_pixel else 0
noise       = float(resp.noise_estimate)

# Build centroid arrays for both backends
# cedar / tetra3 Python: [row=y, col=x]
centroids_cedar = np.array(
    [[c.centroid_position.y, c.centroid_position.x]
     for c in resp.star_candidates],
    dtype=np.float32)
# tetra3rs: center-relative [x_from_center, y_from_center]
centroids_tetra = np.array(
    [[c.centroid_position.x - w / 2.0,
      c.centroid_position.y - h / 2.0]
     for c in resp.star_candidates],
    dtype=np.float64)

if n_stars < cfg.min_centroids:
    print()
    tag(WARN, f"Only {n_stars} stars extracted (need {cfg.min_centroids}); "
              "solve stages will be skipped.")
    tag(WARN, f"  Suggestions:")
    tag(WARN, f"    • Lower detect_sigma (currently {sigma:.1f}) — try 4–6")
    tag(WARN, f"    • Increase exposure_s (currently {cfg.exposure_s}s)")
    tag(WARN, f"    • Check camera mode: maint status → test_mode should be False for live sky")
    if peak_pixel < 20:
        tag(WARN, f"    • Peak={peak_pixel} < 20: solver_proc itself would reject this frame "
                  "before even calling cedar-detect")
    _cleanup()
    sys.exit(0)

# ── Stage 3: Cedar backend (tetra3 Python) ────────────────────────────────────
sep(f"Stage 3: Cedar backend  (tetra3 Python)  N={args.reps}  timeout={timeout_ms}ms")

if not cedar_ok:
    tag(WARN, "Skipped — tetra3 Python not available (see Stage 0 errors)")
else:
    def _cedar_solve(t_ms):
        t0 = time.monotonic()
        soln = t3.solve_from_centroids(
            centroids_cedar,
            (h, w),
            fov_estimate=fov_est,
            fov_max_error=fov_err,
            solve_timeout=t_ms,
            match_threshold=cfg.match_threshold,
            match_radius=cfg.match_radius,
            return_matches=False,
        )
        return soln, (time.monotonic() - t0) * 1000.0

    cedar_times, cedar_solved = [], 0
    for i in range(args.reps):
        try:
            soln, slv_ms = _cedar_solve(timeout_ms)
        except Exception as e:
            tag(FAIL, f"[{i+1:2d}] solve raised: {e}")
            continue
        cedar_times.append(slv_ms)
        solved = soln and soln.get("status") == 1
        if solved:
            cedar_solved += 1
            ra  = soln.get("RA",  "?")
            dec = soln.get("Dec", "?")
            fov = soln.get("FOV", "?")
            m   = soln.get("Matches", "?")
            tag(PASS, f"[{i+1:2d}] {slv_ms:7.1f}ms  SOLVED  "
                      f"RA={ra:.4f} Dec={dec:.4f} FOV={fov:.4f}° matches={m}")
        else:
            status = soln.get("status") if soln else "None"
            tag(FAIL, f"[{i+1:2d}] {slv_ms:7.1f}ms  NO MATCH  status={status}")

    if cedar_times:
        avg = sum(cedar_times) / len(cedar_times)
        print(f"\n  Results: {cedar_solved}/{args.reps} solved")
        print(f"  Timing (solve only):  avg={avg:.1f}ms  min={min(cedar_times):.1f}ms  "
              f"max={max(cedar_times):.1f}ms")
        print(f"  Total per frame (ext + slv):  {extract_ms:.1f} + {avg:.1f} = "
              f"{extract_ms + avg:.1f}ms avg")

    if cedar_solved == 0 and args.extended_timeout:
        ext_timeout = timeout_ms * 3
        tag(WARN, f"No normal-timeout solves; retrying at {ext_timeout}ms …")
        try:
            soln, slv_ms = _cedar_solve(ext_timeout)
        except Exception as e:
            tag(FAIL, f"Extended-timeout solve raised: {e}")
            soln = None
        if soln and soln.get("status") == 1:
            tag(PASS, f"SOLVED at {ext_timeout}ms timeout ({slv_ms:.0f}ms). "
                      "Consider raising solve_timeout_ms in config.")
        else:
            tag(FAIL, f"Still no match at {ext_timeout}ms. "
                      "Check FOV estimate, match_radius, or image quality.")
    elif cedar_solved == 0:
        print()
        tag(WARN, "All cedar solves failed. Possible causes:")
        tag(WARN, f"  • FOV estimate {fov_est:.3f}° ±{fov_err:.3f}° may be wrong — "
                  "check fov_deg in config")
        tag(WARN, f"  • Too few stars ({n_stars}) or bad centroids "
                  "— try lower sigma or different image")
        tag(WARN, f"  • Timeout {timeout_ms}ms too short — try --timeout 5000 "
                  "or --extended-timeout")
        tag(WARN,  "  • tetra3 DB missing or wrong — check tetra3_db path in config")

# ── Stage 4: Tetra3rs backend ─────────────────────────────────────────────────
sep(f"Stage 4: Tetra3rs backend  N={args.reps}  timeout={timeout_ms}ms")

if not tetra_ok:
    tag(WARN, "Skipped — tetra3rs not available (see Stage 0 errors)")
else:
    last_quat    = None
    tetra_times  = []
    tetra_solved = 0

    for i in range(args.reps):
        hint_label = "seeded" if last_quat is not None else "blind "
        t0 = time.monotonic()
        try:
            result = db_rs.solve_from_centroids(
                centroids_tetra,
                fov_estimate_deg=fov_est,
                fov_max_error_deg=fov_err,
                image_width=w,
                image_height=h,
                match_radius=cfg.match_radius,
                match_threshold=cfg.match_threshold,
                solve_timeout_ms=timeout_ms,
                attitude_hint=last_quat,
                hint_uncertainty_deg=0.1,
                strict_hint=False,
            )
        except Exception as e:
            tag(FAIL, f"[{i+1:2d}] solve raised: {e}")
            continue
        slv_ms = (time.monotonic() - t0) * 1000.0
        tetra_times.append(slv_ms)
        if result is not None:
            tetra_solved += 1
            last_quat = result.quaternion
            tag(PASS, f"[{i+1:2d}] {slv_ms:7.1f}ms  SOLVED ({hint_label})  "
                      f"RA={result.ra_deg:.4f} Dec={result.dec_deg:.4f}  "
                      f"FOV={result.fov_deg:.4f}°  matches={result.num_matches}")
        else:
            tag(FAIL, f"[{i+1:2d}] {slv_ms:7.1f}ms  NO MATCH ({hint_label})")

    if tetra_times:
        avg = sum(tetra_times) / len(tetra_times)
        print(f"\n  Results: {tetra_solved}/{args.reps} solved")
        print(f"  Timing (solve only):  avg={avg:.1f}ms  min={min(tetra_times):.1f}ms  "
              f"max={max(tetra_times):.1f}ms")
        print(f"  Total per frame (ext + slv):  {extract_ms:.1f} + {avg:.1f} = "
              f"{extract_ms + avg:.1f}ms avg")

    if tetra_solved == 0 and args.extended_timeout:
        ext_timeout = timeout_ms * 3
        tag(WARN, f"No normal-timeout solves; retrying at {ext_timeout}ms …")
        try:
            t0     = time.monotonic()
            result = db_rs.solve_from_centroids(
                centroids_tetra,
                fov_estimate_deg=fov_est,
                fov_max_error_deg=fov_err,
                image_width=w, image_height=h,
                match_radius=cfg.match_radius,
                match_threshold=cfg.match_threshold,
                solve_timeout_ms=ext_timeout,
            )
            slv_ms = (time.monotonic() - t0) * 1000.0
        except Exception as e:
            tag(FAIL, f"Extended-timeout solve raised: {e}")
            result = None
        if result is not None:
            tag(PASS, f"SOLVED at {ext_timeout}ms timeout ({slv_ms:.0f}ms). "
                      "Consider raising solve_timeout_ms in config.")
        else:
            tag(FAIL, f"Still no match at {ext_timeout}ms. "
                      "Check FOV estimate vs DB range, image quality, or DB path.")
    elif tetra_solved == 0:
        print()
        tag(WARN, "All tetra3rs solves failed. Possible causes:")
        if db_rs and not (db_rs.min_fov_deg <= fov_est <= db_rs.max_fov_deg):
            tag(FAIL, f"  • fov_estimate ({fov_est:.3f}°) outside DB range "
                      f"[{db_rs.min_fov_deg:.2f}, {db_rs.max_fov_deg:.2f}]° — fix fov_deg in config")
        tag(WARN, f"  • Timeout {timeout_ms}ms too short — try --timeout 5000 "
                  "or --extended-timeout")
        tag(WARN, f"  • Wrong or corrupt tetra3rs DB — check tetra3rs_db in config")
        tag(WARN, f"  • Too few / noisy centroids ({n_stars} stars, "
                  f"peak={peak_pixel}) — check image and sigma")

# ── Stage 5: Summary ──────────────────────────────────────────────────────────
sep("Stage 5: Summary")
print(f"  Frame:       {w}x{h}  peak={peak_pixel}  noise={noise:.2f}")
print(f"  Stars:       {n_stars} detected  (min needed: {cfg.min_centroids})")
print(f"  Extract:     {extract_ms:.1f}ms  (sigma={sigma:.1f})")
print(f"  Timeout:     {timeout_ms}ms")

if cedar_ok:
    if 'cedar_times' in dir() and cedar_times:
        avg_c = sum(cedar_times)/len(cedar_times)
        print(f"  Cedar:       {cedar_solved}/{args.reps} solved  "
              f"avg_solve={avg_c:.1f}ms  total={extract_ms + avg_c:.1f}ms")
    else:
        print(f"  Cedar:       not measured")

if tetra_ok:
    if 'tetra_times' in dir() and tetra_times:
        avg_t = sum(tetra_times)/len(tetra_times)
        print(f"  Tetra3rs:    {tetra_solved}/{args.reps} solved  "
              f"avg_solve={avg_t:.1f}ms  total={extract_ms + avg_t:.1f}ms")
    else:
        print(f"  Tetra3rs:    not measured")

_cleanup()
print()
