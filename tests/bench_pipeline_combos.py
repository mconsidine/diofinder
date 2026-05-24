#!/usr/bin/env python3
"""
eFinder four-pipeline benchmark.

Tests every combination of extractor × solver with per-step timing so you
can identify bottlenecks and choose the best configuration.

  Combo 1  cedar-detect (gRPC)       →  tetra3 Python
  Combo 2  tetra3rs.extract_centroids →  tetra3rs solve
  Combo 3  tetra3rs.extract_centroids →  tetra3 Python
  Combo 4  cedar-detect (gRPC)       →  tetra3rs solve

Combos 2 and 4 also run seeded (attitude_hint) solves so the
blind-vs-seeded speedup is visible directly in the table.

An optional --hint-sweep varies hint_uncertainty_deg from 5° down to 0.02°
on both tetra3rs combos.

A --sigma-sweep varies both cedar detect_sigma and tetra3rs sigma_threshold
to show the detection/solve trade-off.

Usage:
  # Daemon stopped, cedar-detect running:
  sudo systemctl stop efinder
  sudo systemctl start cedar-detect
  sudo /opt/efinder/venv/bin/python3 tests/bench_pipeline_combos.py

  # With a saved capture:
  sudo /opt/efinder/venv/bin/python3 tests/bench_pipeline_combos.py \\
      --image /var/lib/efinder/captures/capture_20250101_123456.png

  # Extra reps and hint sweep:
  sudo /opt/efinder/venv/bin/python3 tests/bench_pipeline_combos.py \\
      --reps 10 --hint-sweep

  # Override FOV and timeout:
  sudo /opt/efinder/venv/bin/python3 tests/bench_pipeline_combos.py \\
      --fov 14.0 --fov-err 1.5 --timeout 3000

  # Read from live daemon SHM:
  sudo /opt/efinder/venv/bin/python3 tests/bench_pipeline_combos.py \\
      --live-shm --reps 3
"""

import argparse
import sys
import time

sys.path.insert(0, '/opt/efinder')
sys.path.insert(0, '/opt/efinder/proto')

# ── ANSI helpers ──────────────────────────────────────────────────────────────
PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"
INFO = "\033[34mINFO\033[0m"
BOLD = "\033[1m"
NC   = "\033[0m"

def tag(label, msg): print(f"  [{label}] {msg}")
def sep(title):      print(f"\n{BOLD}{'='*64}\n{title}\n{'='*64}{NC}")
def hr():            print(f"  {'-'*60}")

# ── Args ──────────────────────────────────────────────────────────────────────
p = argparse.ArgumentParser(description=__doc__,
                             formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("--image", metavar="PNG",
               help="Path to a sky PNG (grayscale or RGB) to use as the test frame")
p.add_argument("--live-shm", action="store_true",
               help="Read from live efinder_frame_0 SHM (daemon must be running)")
p.add_argument("--reps", type=int, default=5,
               help="Timed repetitions per combination (default 5)")
p.add_argument("--timeout", type=int, default=None,
               help="Solve timeout in ms (overrides config)")
p.add_argument("--sigma", type=float, default=None,
               help="Override cedar detect_sigma AND tetra3rs sigma_threshold")
p.add_argument("--fov", type=float, default=None,
               help="Override FOV estimate in degrees")
p.add_argument("--fov-err", type=float, default=None,
               help="Override FOV max error in degrees")
p.add_argument("--hint-sweep", action="store_true",
               help="Sweep hint_uncertainty_deg for tetra3rs combos after main table")
p.add_argument("--sigma-sweep", action="store_true",
               help="Sweep sigma values for both extractors after main table")
p.add_argument("--skip-cedar-detect", action="store_true",
               help="Skip combos 1 and 4 (cedar-detect gRPC not available)")
p.add_argument("--skip-tetra-extract", action="store_true",
               help="Skip combos 2 and 3 (tetra3rs extraction not needed)")
args = p.parse_args()

# ── Stage 0: Imports & config ─────────────────────────────────────────────────
sep("Stage 0: Config & library imports")

try:
    from efinder.config import load_config
    from efinder.calibration import FovCalibrator
    cfg        = load_config()
    shared_cfg = {}
    cal        = FovCalibrator(cfg, shared_cfg)
    fov_est    = args.fov    if args.fov    is not None else cal.get_fov_estimate()
    fov_err    = (args.fov_err if args.fov_err is not None
                  else max(cal.get_fov_max_error(), cfg.fov_max_error_deg))
    timeout_ms = args.timeout if args.timeout is not None else cfg.solve_timeout_ms
    sigma_cd   = args.sigma if args.sigma is not None else cfg.detect_sigma
    sigma_t3   = args.sigma if args.sigma is not None else 5.0  # tetra3rs default
    tag(PASS, f"Config: {cfg.summary()}")
    tag(INFO, f"FOV: {fov_est:.4f}° ± {fov_err:.4f}°  timeout: {timeout_ms}ms")
    tag(INFO, f"cedar sigma: {sigma_cd:.1f}  tetra3rs sigma: {sigma_t3:.1f}")
except Exception as e:
    tag(FAIL, f"Config: {e}")
    sys.exit(1)

w, h = cfg.frame_width, cfg.frame_height

try:
    import numpy as np
    tag(PASS, f"numpy {np.__version__}")
except Exception as e:
    tag(FAIL, f"numpy: {e}"); sys.exit(1)

# cedar-detect gRPC
cd_ok   = False
stub    = None
pb      = None
pb_grpc = None
if not args.skip_cedar_detect:
    try:
        import grpc
        import cedar_detect_pb2 as pb
        import cedar_detect_pb2_grpc as pb_grpc
        channel = grpc.insecure_channel(cfg.cedar_detect_socket)
        stub    = pb_grpc.CedarDetectStub(channel)
        tag(PASS, f"cedar-detect gRPC  ({cfg.cedar_detect_socket})")
        cd_ok = True
    except Exception as e:
        tag(FAIL, f"cedar-detect gRPC: {e}")

# tetra3rs extraction + solving
t3rs_ok   = False
t3rs_extr = None   # module-level extract_centroids
db_rs     = None   # SolverDatabase
if not args.skip_tetra_extract:
    try:
        import tetra3rs as _t3rs
        t3rs_extr = _t3rs.extract_centroids
        db_rs     = _t3rs.SolverDatabase.load_from_file(cfg.tetra3rs_db)
        tag(PASS, f"tetra3rs {_t3rs.__version__}  db={cfg.tetra3rs_db}")
        tag(INFO, f"  DB: {db_rs.num_stars} stars  {db_rs.num_patterns} patterns  "
                  f"fov=[{db_rs.min_fov_deg:.2f}, {db_rs.max_fov_deg:.2f}]°")
        if not (db_rs.min_fov_deg <= fov_est <= db_rs.max_fov_deg):
            tag(WARN, f"  fov_est {fov_est:.3f}° outside DB range — tetra3rs will fail!")
        t3rs_ok = True
    except Exception as e:
        tag(FAIL, f"tetra3rs: {e}")

# tetra3 Python (cedar-solve)
t3py_ok = False
t3py    = None
try:
    import tetra3 as _t3
    t3py = _t3.Tetra3(cfg.tetra3_db)
    tag(PASS, f"tetra3 Python  db={cfg.tetra3_db}")
    t3py_ok = True
except Exception as e:
    tag(FAIL, f"tetra3 Python: {e}")

if not t3py_ok and not t3rs_ok:
    tag(FAIL, "No solver available — aborting")
    sys.exit(1)

# ── Stage 1: Frame source → SHM + raw numpy array ─────────────────────────────
sep("Stage 1: Frame source")

from multiprocessing import shared_memory
from multiprocessing import resource_tracker as _rt

OWN_SHM_NAME = "efinder_bench_frame"

def _borrow_shm(name: str) -> shared_memory.SharedMemory:
    shm = shared_memory.SharedMemory(name=name, create=False)
    try:
        _rt.unregister(shm._name, 'shared_memory')
    except Exception:
        pass
    return shm
own_shm  = None
shm_slot = None   # SHM name for cedar-detect gRPC
raw_frame = None  # numpy uint8 (h, w) for tetra3rs extraction

def _cleanup():
    global own_shm
    if own_shm is not None:
        try: own_shm.close(); own_shm.unlink()
        except Exception: pass
        own_shm = None

def _make_own_shm(frame):
    try:
        _s = shared_memory.SharedMemory(name=OWN_SHM_NAME, create=False)
        _s.close(); _s.unlink()
    except Exception:
        pass
    shm = shared_memory.SharedMemory(name=OWN_SHM_NAME, create=True, size=frame.nbytes)
    np.ndarray(frame.shape, dtype=np.uint8, buffer=shm.buf)[:] = frame
    return shm

if args.live_shm:
    shm_slot = "efinder_frame_0"
    try:
        _s   = _borrow_shm(shm_slot)
        raw_frame = np.array(np.ndarray((h, w), dtype=np.uint8, buffer=_s.buf))
        _s.close()
        tag(PASS, f"Live SHM {shm_slot}: peak={raw_frame.max()}  mean={raw_frame.mean():.1f}")
    except Exception as e:
        tag(FAIL, f"Cannot attach live SHM: {e}"); sys.exit(1)

elif args.image:
    try:
        from PIL import Image
        img = Image.open(args.image).convert("L")
        if img.size != (w, h):
            tag(INFO, f"Resizing {img.size} → {(w, h)}")
            img = img.resize((w, h), Image.LANCZOS)
        raw_frame = np.array(img, dtype=np.uint8)
        own_shm   = _make_own_shm(raw_frame)
        shm_slot  = OWN_SHM_NAME
        tag(PASS, f"Test image: {args.image}")
        tag(INFO, f"  peak={raw_frame.max()}  mean={raw_frame.mean():.1f}  "
                  f"p95={int(np.percentile(raw_frame, 95))}")
    except Exception as e:
        tag(FAIL, f"Cannot load image: {e}"); sys.exit(1)

else:
    # Auto-detect live SHM first
    live_ok = False
    for slot in ("efinder_frame_0", "efinder_frame_1", "efinder_frame_2"):
        try:
            _s   = _borrow_shm(slot)
            raw_frame = np.array(np.ndarray((h, w), dtype=np.uint8, buffer=_s.buf))
            _s.close()
            shm_slot = slot
            live_ok  = True
            tag(PASS, f"Found live SHM: {slot}  peak={raw_frame.max()}  mean={raw_frame.mean():.1f}")
            break
        except Exception:
            continue
    if not live_ok:
        tag(WARN, "No live SHM — pass --image for a real sky capture")
        tag(INFO, "Generating synthetic star field (blind solve will fail; "
                  "timing and extraction counts are still valid)")
        raw_frame = np.zeros((h, w), dtype=np.uint8)
        rng = np.random.default_rng(42)
        for _ in range(50):
            cy, cx = int(rng.integers(15, h-15)), int(rng.integers(15, w-15))
            br = int(rng.integers(100, 230))
            ys, xs = np.ogrid[-6:7, -6:7]
            patch = (br * np.exp(-(ys**2+xs**2)/5.0)).astype(np.uint8)
            y0,y1 = max(0,cy-6), min(h,cy+7)
            x0,x1 = max(0,cx-6), min(w,cx+7)
            raw_frame[y0:y1,x0:x1] = np.maximum(raw_frame[y0:y1,x0:x1], patch[:y1-y0,:x1-x0])
        own_shm  = _make_own_shm(raw_frame)
        shm_slot = OWN_SHM_NAME
        tag(INFO, f"Synthetic: peak={raw_frame.max()}")

if raw_frame.max() < 20:
    tag(WARN, f"Peak pixel={raw_frame.max()} < 20 — solver_proc would skip this frame. "
              "Check exposure/camera mode.")

# ── Helper: cedar-detect gRPC extraction ─────────────────────────────────────
_cd_opened: set = set()

def cedar_extract(sigma: float = sigma_cd) -> "tuple[list, float, dict]":
    """Returns (centroids_raw_resp, extract_ms, stats)."""
    reopen = shm_slot not in _cd_opened
    if reopen:
        _cd_opened.add(shm_slot)
    req = pb.CentroidsRequest(
        input_image=pb.Image(width=w, height=h,
                             shmem_name=shm_slot, reopen_shmem=reopen),
        sigma=sigma,
        detect_hot_pixels=cfg.detect_hot_pixels,
        use_binned_for_star_candidates=cfg.detect_use_binned,
        return_binned=False,
    )
    t0   = time.monotonic()
    resp = stub.ExtractCentroids(req, timeout=5.0)
    ms   = (time.monotonic() - t0) * 1000.0
    stats = {
        "n": len(resp.star_candidates),
        "peak": int(resp.peak_star_pixel) if resp.peak_star_pixel else 0,
        "noise": float(resp.noise_estimate),
    }
    return resp, ms, stats


def cedar_centroids_for_t3py(resp) -> np.ndarray:
    """[y, x] image-coord array for tetra3 Python."""
    return np.array([[c.centroid_position.y, c.centroid_position.x]
                     for c in resp.star_candidates], dtype=np.float32)


def cedar_centroids_for_t3rs(resp) -> np.ndarray:
    """Center-relative [x, y] array for tetra3rs."""
    return np.array([[c.centroid_position.x - w/2.0,
                      c.centroid_position.y - h/2.0]
                     for c in resp.star_candidates], dtype=np.float64)

# ── Helper: tetra3rs native extraction ────────────────────────────────────────
def t3rs_extract(sigma: float = sigma_t3) -> "tuple[object, float, dict]":
    """Returns (ExtractionResult, extract_ms, stats)."""
    t0 = time.monotonic()
    result = t3rs_extr(
        raw_frame,
        sigma_threshold=sigma,
        min_pixels=3,
        max_pixels=10000,
        local_bg_block_size=64,
        max_elongation=3.0,
    )
    ms    = (time.monotonic() - t0) * 1000.0
    stats = {
        "n":      len(result.centroids),
        "bg":     result.background_mean,
        "bg_sig": result.background_sigma,
        "thresh": result.threshold,
    }
    return result, ms, stats


def t3rs_centroids_for_t3py(extr_result) -> np.ndarray:
    """Convert tetra3rs Centroid list → [y, x] image-coord for tetra3 Python."""
    return np.array([[c.y + h/2.0, c.x + w/2.0]
                     for c in extr_result.centroids], dtype=np.float32)

# ── Helper: solve functions ────────────────────────────────────────────────────
def solve_t3py(centroids_yx: np.ndarray) -> "tuple[bool, float, dict]":
    """Run tetra3 Python solve. Returns (solved, solve_ms, info)."""
    t0 = time.monotonic()
    soln = t3py.solve_from_centroids(
        centroids_yx,
        (h, w),
        fov_estimate=fov_est,
        fov_max_error=fov_err,
        solve_timeout=timeout_ms,
        match_threshold=cfg.match_threshold,
        match_radius=cfg.match_radius,
        return_matches=False,
    )
    ms     = (time.monotonic() - t0) * 1000.0
    solved = bool(soln and soln.get("status") == 1)
    info   = {
        "ra":  soln.get("RA")      if soln else None,
        "dec": soln.get("Dec")     if soln else None,
        "fov": soln.get("FOV")     if soln else None,
        "m":   soln.get("Matches") if soln else 0,
        "status": soln.get("status") if soln else None,
    }
    return solved, ms, info


def solve_t3rs(centroids, hint=None, hint_unc_deg=0.1,
               strict=False, t_ms=None) -> "tuple[bool, float, object]":
    """Run tetra3rs solve. centroids can be Centroid list or Nx2 ndarray."""
    if t_ms is None:
        t_ms = timeout_ms
    t0 = time.monotonic()
    result = db_rs.solve_from_centroids(
        centroids,
        fov_estimate_deg=fov_est,
        fov_max_error_deg=fov_err,
        image_width=w,
        image_height=h,
        match_radius=cfg.match_radius,
        match_threshold=cfg.match_threshold,
        solve_timeout_ms=t_ms,
        attitude_hint=hint,
        hint_uncertainty_deg=hint_unc_deg if hint is not None else None,
        strict_hint=strict,
    )
    ms = (time.monotonic() - t0) * 1000.0
    return result is not None, ms, result

# ── Warm-up ───────────────────────────────────────────────────────────────────
sep("Warm-up")
if cd_ok:
    try:
        _cd_opened.clear()  # force reopen
        _, wu_ms, st = cedar_extract()
        tag(PASS, f"cedar-detect warm-up: {wu_ms:.0f}ms  stars={st['n']}  "
                  f"peak={st['peak']}  noise={st['noise']:.2f}")
        if st["n"] < cfg.min_centroids:
            tag(WARN, f"  {st['n']} stars < min_centroids={cfg.min_centroids}; "
                      f"solves will likely fail. Try lower --sigma (current {sigma_cd:.1f}).")
    except Exception as e:
        tag(FAIL, f"cedar-detect warm-up failed: {e}")
        cd_ok = False

if t3rs_ok:
    try:
        _, wu_ms, st = t3rs_extract()
        tag(PASS, f"tetra3rs extract warm-up: {wu_ms:.0f}ms  stars={st['n']}  "
                  f"bg={st['bg']:.1f}  σ={st['bg_sig']:.2f}  thresh={st['thresh']:.1f}")
        if st["n"] < cfg.min_centroids:
            tag(WARN, f"  {st['n']} stars < min_centroids={cfg.min_centroids}; "
                      f"try lower --sigma (current t3rs sigma={sigma_t3:.1f}).")
    except Exception as e:
        tag(FAIL, f"tetra3rs extract warm-up failed: {e}")
        t3rs_ok = False

# ── Timing helpers ────────────────────────────────────────────────────────────
def _stats(vals):
    if not vals:
        return "n/a"
    return (f"avg={sum(vals)/len(vals):.1f}  "
            f"min={min(vals):.1f}  "
            f"max={max(vals):.1f}")

# Storage for summary table
summary = {}

# ── Combo 1: cedar-detect → tetra3 Python ────────────────────────────────────
sep(f"Combo 1: cedar-detect  →  tetra3 Python   (N={args.reps})")
if not cd_ok or not t3py_ok:
    tag(WARN, "Skipped (cedar-detect or tetra3 Python unavailable)")
    summary[1] = None
else:
    ext_ms_c1, slv_ms_c1, solved_c1 = [], [], 0
    for i in range(args.reps):
        try:
            resp, e_ms, est = cedar_extract()
            c_yx = cedar_centroids_for_t3py(resp)
            if len(c_yx) < cfg.min_centroids:
                tag(WARN, f"  [{i+1:2d}] only {len(c_yx)} stars — skipping solve")
                continue
            ok, s_ms, info = solve_t3py(c_yx)
            ext_ms_c1.append(e_ms); slv_ms_c1.append(s_ms)
            if ok:
                solved_c1 += 1
                tag(PASS, f"  [{i+1:2d}] ext={e_ms:.0f}ms  slv={s_ms:.0f}ms  "
                           f"total={e_ms+s_ms:.0f}ms  "
                           f"RA={info['ra']:.4f}  Dec={info['dec']:.4f}  "
                           f"fov={info['fov']:.4f}°  m={info['m']}")
            else:
                tag(FAIL, f"  [{i+1:2d}] ext={e_ms:.0f}ms  slv={s_ms:.0f}ms  "
                           f"NO MATCH  status={info['status']}")
        except Exception as e:
            tag(FAIL, f"  [{i+1:2d}] exception: {e}")

    if ext_ms_c1:
        avg_e = sum(ext_ms_c1)/len(ext_ms_c1)
        avg_s = sum(slv_ms_c1)/len(slv_ms_c1)
        print(f"\n  Solved: {solved_c1}/{len(ext_ms_c1)}")
        print(f"  Extract: {_stats(ext_ms_c1)} ms")
        print(f"  Solve:   {_stats(slv_ms_c1)} ms")
        print(f"  Total:   avg={avg_e+avg_s:.1f}ms")
        summary[1] = {"solved": solved_c1, "n": len(ext_ms_c1),
                      "avg_ext": avg_e, "avg_slv": avg_s, "hint": "—"}

# ── Combo 2: tetra3rs extract → tetra3rs solve ────────────────────────────────
sep(f"Combo 2: tetra3rs extract  →  tetra3rs solve   (N={args.reps})")
if not t3rs_ok:
    tag(WARN, "Skipped (tetra3rs unavailable)")
    summary[2] = None
else:
    ext_ms_c2, slv_ms_c2_blind, slv_ms_c2_seed = [], [], []
    solved_blind_c2 = solved_seed_c2 = 0
    last_quat_c2 = None

    for i in range(args.reps):
        try:
            extr, e_ms, est = t3rs_extract()
            if est["n"] < cfg.min_centroids:
                tag(WARN, f"  [{i+1:2d}] only {est['n']} stars — skipping solve")
                continue
            ext_ms_c2.append(e_ms)

            # Blind solve — pass Centroid list directly (native tetra3rs format)
            ok_b, s_ms_b, res_b = solve_t3rs(extr.centroids, hint=None)
            slv_ms_c2_blind.append(s_ms_b)
            if ok_b:
                solved_blind_c2 += 1
                last_quat_c2 = res_b.quaternion
                tag(PASS, f"  [{i+1:2d}] ext={e_ms:.0f}ms  "
                           f"blind={s_ms_b:.0f}ms  n={est['n']:3d}  "
                           f"RA={res_b.ra_deg:.4f}  Dec={res_b.dec_deg:.4f}  "
                           f"fov={res_b.fov_deg:.4f}°  m={res_b.num_matches}")
            else:
                tag(FAIL, f"  [{i+1:2d}] ext={e_ms:.0f}ms  blind={s_ms_b:.0f}ms  NO MATCH")

            # Seeded solve (uses quaternion from previous success)
            if last_quat_c2 is not None:
                ok_s, s_ms_s, res_s = solve_t3rs(extr.centroids, hint=last_quat_c2,
                                                   hint_unc_deg=0.1)
                slv_ms_c2_seed.append(s_ms_s)
                if ok_s:
                    solved_seed_c2 += 1
                    last_quat_c2 = res_s.quaternion
                    tag(PASS, f"  [{i+1:2d}]               seeded={s_ms_s:.0f}ms  "
                               f"m={res_s.num_matches}")
                else:
                    tag(FAIL, f"  [{i+1:2d}]               seeded={s_ms_s:.0f}ms  NO MATCH")
        except Exception as e:
            tag(FAIL, f"  [{i+1:2d}] exception: {e}")

    if ext_ms_c2:
        avg_e  = sum(ext_ms_c2)/len(ext_ms_c2)
        avg_sb = sum(slv_ms_c2_blind)/len(slv_ms_c2_blind) if slv_ms_c2_blind else 0
        avg_ss = sum(slv_ms_c2_seed)/len(slv_ms_c2_seed)   if slv_ms_c2_seed   else None
        print(f"\n  Blind  solved: {solved_blind_c2}/{len(ext_ms_c2)}")
        if slv_ms_c2_seed:
            print(f"  Seeded solved: {solved_seed_c2}/{len(slv_ms_c2_seed)}")
        print(f"  Extract:      {_stats(ext_ms_c2)} ms")
        print(f"  Solve blind:  {_stats(slv_ms_c2_blind)} ms")
        if slv_ms_c2_seed:
            print(f"  Solve seeded: {_stats(slv_ms_c2_seed)} ms")
        summary[2] = {"solved": solved_blind_c2, "n": len(ext_ms_c2),
                      "avg_ext": avg_e, "avg_slv": avg_sb,
                      "avg_slv_seed": avg_ss, "hint": "0.1°"}

# ── Combo 3: tetra3rs extract → tetra3 Python ────────────────────────────────
sep(f"Combo 3: tetra3rs extract  →  tetra3 Python   (N={args.reps})")
if not t3rs_ok or not t3py_ok:
    tag(WARN, "Skipped (tetra3rs or tetra3 Python unavailable)")
    summary[3] = None
else:
    ext_ms_c3, slv_ms_c3, solved_c3 = [], [], 0
    for i in range(args.reps):
        try:
            extr, e_ms, est = t3rs_extract()
            # Convert tetra3rs Centroids → [y, x] image coords for tetra3 Python
            c_yx = t3rs_centroids_for_t3py(extr)
            if len(c_yx) < cfg.min_centroids:
                tag(WARN, f"  [{i+1:2d}] only {est['n']} stars — skipping solve")
                continue
            ok, s_ms, info = solve_t3py(c_yx)
            ext_ms_c3.append(e_ms); slv_ms_c3.append(s_ms)
            if ok:
                solved_c3 += 1
                tag(PASS, f"  [{i+1:2d}] ext={e_ms:.0f}ms  slv={s_ms:.0f}ms  "
                           f"total={e_ms+s_ms:.0f}ms  "
                           f"RA={info['ra']:.4f}  Dec={info['dec']:.4f}  "
                           f"fov={info['fov']:.4f}°  m={info['m']}")
            else:
                tag(FAIL, f"  [{i+1:2d}] ext={e_ms:.0f}ms  slv={s_ms:.0f}ms  "
                           f"NO MATCH  status={info['status']}")
        except Exception as e:
            tag(FAIL, f"  [{i+1:2d}] exception: {e}")

    if ext_ms_c3:
        avg_e = sum(ext_ms_c3)/len(ext_ms_c3)
        avg_s = sum(slv_ms_c3)/len(slv_ms_c3)
        print(f"\n  Solved: {solved_c3}/{len(ext_ms_c3)}")
        print(f"  Extract: {_stats(ext_ms_c3)} ms")
        print(f"  Solve:   {_stats(slv_ms_c3)} ms")
        print(f"  Total:   avg={avg_e+avg_s:.1f}ms")
        summary[3] = {"solved": solved_c3, "n": len(ext_ms_c3),
                      "avg_ext": avg_e, "avg_slv": avg_s, "hint": "—"}

# ── Combo 4: cedar-detect → tetra3rs ─────────────────────────────────────────
sep(f"Combo 4: cedar-detect  →  tetra3rs solve   (N={args.reps})")
if not cd_ok or not t3rs_ok:
    tag(WARN, "Skipped (cedar-detect or tetra3rs unavailable)")
    summary[4] = None
else:
    ext_ms_c4, slv_ms_c4_blind, slv_ms_c4_seed = [], [], []
    solved_blind_c4 = solved_seed_c4 = 0
    last_quat_c4 = None

    for i in range(args.reps):
        try:
            resp, e_ms, est = cedar_extract()
            if est["n"] < cfg.min_centroids:
                tag(WARN, f"  [{i+1:2d}] only {est['n']} stars — skipping solve")
                continue
            c_xy = cedar_centroids_for_t3rs(resp)
            ext_ms_c4.append(e_ms)

            # Blind solve
            ok_b, s_ms_b, res_b = solve_t3rs(c_xy, hint=None)
            slv_ms_c4_blind.append(s_ms_b)
            if ok_b:
                solved_blind_c4 += 1
                last_quat_c4 = res_b.quaternion
                tag(PASS, f"  [{i+1:2d}] ext={e_ms:.0f}ms  "
                           f"blind={s_ms_b:.0f}ms  n={est['n']:3d}  "
                           f"RA={res_b.ra_deg:.4f}  Dec={res_b.dec_deg:.4f}  "
                           f"fov={res_b.fov_deg:.4f}°  m={res_b.num_matches}")
            else:
                tag(FAIL, f"  [{i+1:2d}] ext={e_ms:.0f}ms  blind={s_ms_b:.0f}ms  NO MATCH")

            # Seeded solve
            if last_quat_c4 is not None:
                ok_s, s_ms_s, res_s = solve_t3rs(c_xy, hint=last_quat_c4, hint_unc_deg=0.1)
                slv_ms_c4_seed.append(s_ms_s)
                if ok_s:
                    solved_seed_c4 += 1
                    last_quat_c4 = res_s.quaternion
                    tag(PASS, f"  [{i+1:2d}]               seeded={s_ms_s:.0f}ms  "
                               f"m={res_s.num_matches}")
                else:
                    tag(FAIL, f"  [{i+1:2d}]               seeded={s_ms_s:.0f}ms  NO MATCH")
        except Exception as e:
            tag(FAIL, f"  [{i+1:2d}] exception: {e}")

    if ext_ms_c4:
        avg_e  = sum(ext_ms_c4)/len(ext_ms_c4)
        avg_sb = sum(slv_ms_c4_blind)/len(slv_ms_c4_blind) if slv_ms_c4_blind else 0
        avg_ss = sum(slv_ms_c4_seed)/len(slv_ms_c4_seed)   if slv_ms_c4_seed   else None
        print(f"\n  Blind  solved: {solved_blind_c4}/{len(ext_ms_c4)}")
        if slv_ms_c4_seed:
            print(f"  Seeded solved: {solved_seed_c4}/{len(slv_ms_c4_seed)}")
        print(f"  Extract:      {_stats(ext_ms_c4)} ms")
        print(f"  Solve blind:  {_stats(slv_ms_c4_blind)} ms")
        if slv_ms_c4_seed:
            print(f"  Solve seeded: {_stats(slv_ms_c4_seed)} ms")
        summary[4] = {"solved": solved_blind_c4, "n": len(ext_ms_c4),
                      "avg_ext": avg_e, "avg_slv": avg_sb,
                      "avg_slv_seed": avg_ss, "hint": "0.1°"}

# ── Summary table ─────────────────────────────────────────────────────────────
sep("Summary")
print(f"  {'#':<2}  {'Pipeline':<38}  {'extract':>8}  {'solve(blind)':>13}  "
      f"{'total(blind)':>13}  {'solve(seed)':>12}  {'solved':>8}")
hr()
names = {
    1: "cedar-detect   → tetra3 Python",
    2: "tetra3rs-extr  → tetra3rs",
    3: "tetra3rs-extr  → tetra3 Python",
    4: "cedar-detect   → tetra3rs",
}
for n, label in names.items():
    s = summary.get(n)
    if s is None:
        print(f"  {n:<2}  {label:<38}  {'—':>8}  {'—':>13}  {'—':>13}  {'—':>12}  {'skipped':>8}")
        continue
    ext    = f"{s['avg_ext']:.1f}ms"
    slv_b  = f"{s['avg_slv']:.1f}ms"
    tot_b  = f"{s['avg_ext']+s['avg_slv']:.1f}ms"
    slv_s  = f"{s['avg_slv_seed']:.1f}ms" if s.get("avg_slv_seed") else "—"
    slv_rt = f"{s['solved']}/{s['n']}"
    print(f"  {n:<2}  {label:<38}  {ext:>8}  {slv_b:>13}  {tot_b:>13}  {slv_s:>12}  {slv_rt:>8}")
hr()
print(f"  Frame: {w}x{h}  FOV: {fov_est:.3f}°±{fov_err:.3f}°  timeout: {timeout_ms}ms  reps: {args.reps}")

# ── Hint-uncertainty sweep ────────────────────────────────────────────────────
if args.hint_sweep and t3rs_ok:
    sep("Hint-uncertainty sweep  (tetra3rs combos 2 & 4)")
    print("  Requires at least one successful blind solve above to seed the quaternion.\n")

    blind_quat = None
    blind_cxy  = None  # for combo-4 style (cedar centroids)
    blind_cobj = None  # for combo-2 style (Centroid objects)

    # Get a fresh blind solve to seed from
    try:
        if cd_ok:
            resp, e_ms, est = cedar_extract()
            if est["n"] >= cfg.min_centroids:
                c_xy = cedar_centroids_for_t3rs(resp)
                ok, _, res = solve_t3rs(c_xy, hint=None, t_ms=timeout_ms*3)
                if ok:
                    blind_quat = res.quaternion
                    blind_cxy  = c_xy
        if blind_quat is None and t3rs_ok:
            extr, e_ms, est = t3rs_extract()
            if est["n"] >= cfg.min_centroids:
                ok, _, res = solve_t3rs(extr.centroids, hint=None, t_ms=timeout_ms*3)
                if ok:
                    blind_quat = res.quaternion
                    blind_cobj = extr.centroids
                    # also make numpy array version
                    blind_cxy  = np.array([[c.x, c.y] for c in extr.centroids],
                                          dtype=np.float64)
    except Exception as e:
        tag(WARN, f"Could not get seed quaternion: {e}")

    if blind_quat is None:
        tag(WARN, "No successful blind solve — cannot run hint sweep")
    else:
        HINT_REPS = max(3, args.reps)
        uncertainties = [5.0, 2.0, 1.0, 0.5, 0.2, 0.1, 0.05, 0.02]

        for strict in (False, True):
            label = "strict_hint=True " if strict else "strict_hint=False"
            print(f"\n  {label}  ({HINT_REPS} reps each)\n")
            print(f"  {'unc_deg':>8}  {'c2-t3rs(nat)':>14}  {'c4-cd+t3rs':>12}  "
                  f"{'c2-solved':>10}  {'c4-solved':>10}")
            hr()

            for unc in uncertainties:
                # Combo 2 style: Centroid objects (or numpy) → tetra3rs
                times_c2, ok_c2 = [], 0
                centroids_src_c2 = blind_cobj if blind_cobj is not None else blind_cxy
                for _ in range(HINT_REPS):
                    try:
                        ok, ms, _ = solve_t3rs(centroids_src_c2, hint=blind_quat,
                                                hint_unc_deg=unc, strict=strict)
                        times_c2.append(ms)
                        if ok: ok_c2 += 1
                    except Exception:
                        pass

                # Combo 4 style: numpy center-relative → tetra3rs
                times_c4, ok_c4 = [], 0
                if blind_cxy is not None:
                    for _ in range(HINT_REPS):
                        try:
                            ok, ms, _ = solve_t3rs(blind_cxy, hint=blind_quat,
                                                    hint_unc_deg=unc, strict=strict)
                            times_c4.append(ms)
                            if ok: ok_c4 += 1
                        except Exception:
                            pass

                avg_c2 = f"{sum(times_c2)/len(times_c2):.1f}ms" if times_c2 else "—"
                avg_c4 = f"{sum(times_c4)/len(times_c4):.1f}ms" if times_c4 else "—"
                sol_c2 = f"{ok_c2}/{HINT_REPS}"
                sol_c4 = f"{ok_c4}/{HINT_REPS}" if times_c4 else "—"
                print(f"  {unc:>8.2f}  {avg_c2:>14}  {avg_c4:>12}  "
                      f"{sol_c2:>10}  {sol_c4:>10}")

# ── Sigma sweep ───────────────────────────────────────────────────────────────
if args.sigma_sweep:
    sep("Sigma sweep  —  star count vs detection threshold")
    print("  Shows how sigma affects star yield for each extractor.\n")
    print(f"  {'sigma':>6}  {'cedar stars':>12}  {'cedar_ms':>9}  "
          f"{'t3rs stars':>11}  {'t3rs_ms':>8}  {'t3rs_bg':>8}")
    hr()

    for sig in [3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 12.0]:
        cd_n = "—"; cd_ms = "—"
        tr_n = "—"; tr_ms = "—"; tr_bg = "—"

        if cd_ok:
            try:
                resp, e_ms, est = cedar_extract(sigma=sig)
                cd_n  = str(est["n"])
                cd_ms = f"{e_ms:.0f}ms"
            except Exception as e:
                cd_n = f"ERR: {e}"

        if t3rs_ok:
            try:
                extr, e_ms, est = t3rs_extract(sigma=sig)
                tr_n  = str(est["n"])
                tr_ms = f"{e_ms:.0f}ms"
                tr_bg = f"{est['bg']:.1f}"
            except Exception as e:
                tr_n = f"ERR: {e}"

        min_mark = " ← " if (
            (cd_ok and cd_n.isdigit() and int(cd_n) >= cfg.min_centroids) or
            (t3rs_ok and tr_n.isdigit() and int(tr_n) >= cfg.min_centroids)
        ) else ""
        print(f"  {sig:>6.1f}  {cd_n:>12}  {cd_ms:>9}  "
              f"{tr_n:>11}  {tr_ms:>8}  {tr_bg:>8}{min_mark}")

    print(f"\n  min_centroids = {cfg.min_centroids}  (marked with ← above)")

_cleanup()
print()
