#!/usr/bin/env python3
"""Compare solve_from_centroids with varying hint_uncertainty_deg values.

Run on device:
    sudo /opt/efinder/venv/bin/python3 tests/bench_tetra_hints.py

The script pulls a live frame from cedar-detect via SHM slot 0, then
runs solve_from_centroids multiple times with different hint tightness
values. The first call is always blind (no quaternion); subsequent calls
use the quaternion returned by the blind solve.

This replicates the behaviour difference between the old db.solve() API
(which used ra_hint_deg/dec_hint_deg/search_radius_deg and achieved ~6ms
seeded solves) and the current solve_from_centroids API.

Note: db.solve() does not exist in the current tetra3rs version.
"""
import sys, time, math
sys.path.insert(0, '/opt/efinder')
sys.path.insert(0, '/opt/efinder/proto')

import grpc
import numpy as np
import cedar_detect_pb2 as pb
import cedar_detect_pb2_grpc as pb_grpc
import tetra3rs
from efinder.config import load_config
from efinder.calibration import FovCalibrator

cfg = load_config()
w, h = cfg.frame_width, cfg.frame_height
shared_cfg = {}
cal = FovCalibrator(cfg, shared_cfg)

print(f"Frame: {w}x{h}")
print(f"Config fov: {cfg.fov_deg:.4f} deg  fov_max_error: {cfg.fov_max_error_deg} deg")
print(f"Calibrator: fov={cal.get_fov_estimate():.4f}  max_error={cal.get_fov_max_error():.4f}")
print()

# --- Get centroids from cedar-detect via SHM ---
channel = grpc.insecure_channel(cfg.cedar_detect_socket)
stub = pb_grpc.CedarDetectStub(channel)

req = pb.CentroidsRequest(
    input_image=pb.Image(
        width=w, height=h,
        shmem_name='efinder_frame_0', reopen_shmem=True,
    ),
    sigma=cfg.detect_sigma,
    detect_hot_pixels=cfg.detect_hot_pixels,
    use_binned_for_star_candidates=cfg.detect_use_binned,
    return_binned=False,
)
stub.ExtractCentroids(req, timeout=2.0)  # warm-up
t0 = time.monotonic()
resp = stub.ExtractCentroids(req, timeout=2.0)
extract_ms = (time.monotonic() - t0) * 1000.0
stars = resp.star_candidates
print(f"Cedar extract: {extract_ms:.1f}ms  n={len(stars)}")

centraloids = np.array(
    [[c.centroid_position.x - w / 2.0,
      c.centroid_position.y - h / 2.0]
     for c in stars],
    dtype=np.float64,
)

# --- Load database ---
db = tetra3rs.SolverDatabase.load_from_file(cfg.tetra3rs_db)
print(f"DB: {db.num_stars} stars  {db.num_patterns} patterns  "
      f"fov=[{db.min_fov_deg:.1f},{db.max_fov_deg:.1f}] deg")
print()

fov_est = cal.get_fov_estimate()
fov_err = max(cal.get_fov_max_error(), cfg.fov_max_error_deg)

# --- Blind solve (no hint) ---
print("=" * 60)
print("BLIND solve (no attitude hint)")
t1 = time.monotonic()
result_blind = db.solve_from_centroids(
    centroids,
    fov_estimate_deg=fov_est,
    fov_max_error_deg=fov_err,
    image_width=w, image_height=h,
    match_radius=cfg.match_radius,
    match_threshold=cfg.match_threshold,
    solve_timeout_ms=3000,
)
blind_ms = (time.monotonic() - t1) * 1000.0
print(f"  {blind_ms:.1f}ms -> {result_blind}")

if result_blind is None:
    print("Blind solve failed — cannot test seeded solves.")
    sys.exit(1)

quat = result_blind.quaternion
print(f"  RA={result_blind.ra_deg:.4f} Dec={result_blind.dec_deg:.4f} "
      f"Roll={result_blind.roll_deg:.2f} matches={result_blind.num_matches}")
print()

# --- Seeded solves at different hint_uncertainty_deg values ---
for uncertainty in [5.0, 2.0, 1.0, 0.5, 0.2, 0.1, 0.05]:
    times = []
    matches_list = []
    for _ in range(5):
        t = time.monotonic()
        r = db.solve_from_centroids(
            centroids,
            fov_estimate_deg=fov_est,
            fov_max_error_deg=fov_err,
            image_width=w, image_height=h,
            match_radius=cfg.match_radius,
            match_threshold=cfg.match_threshold,
            solve_timeout_ms=3000,
            attitude_hint=quat,
            hint_uncertainty_deg=uncertainty,
            strict_hint=False,
        )
        times.append((time.monotonic() - t) * 1000.0)
        matches_list.append(r.num_matches if r is not None else 0)
    solved = sum(1 for m in matches_list if m > 0)
    print(f"  hint={uncertainty:5.2f} deg: "
          f"avg={sum(times)/len(times):.1f}ms "
          f"min={min(times):.1f}ms "
          f"solved={solved}/5 "
          f"matches={matches_list}")

print()
print("Note: db.solve() (old RA/Dec hint API) is not available in")
print("current tetra3rs. solve_from_centroids with attitude_hint is")
print("the equivalent.")
