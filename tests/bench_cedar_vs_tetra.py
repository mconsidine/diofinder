#!/usr/bin/env python3
"""Side-by-side timing: cedar (tetra3 Python) vs tetra hybrid (cedar-detect + tetra3rs).

Run on device:
    sudo /opt/efinder/venv/bin/python3 tests/bench_cedar_vs_tetra.py

Prints a table of N solve iterations for both backends.
Requires efinder daemon NOT to be running (both use cedar-detect SHM).
If the daemon is running, stop it first:
    sudo systemctl stop efinder
And ensure cedar-detect is still running:
    sudo systemctl start cedar-detect
"""
import sys, time
sys.path.insert(0, '/opt/efinder')
sys.path.insert(0, '/opt/efinder/proto')

import grpc
import numpy as np
import cedar_detect_pb2 as pb
import cedar_detect_pb2_grpc as pb_grpc
import tetra3rs
from efinder.config import load_config
from efinder.calibration import FovCalibrator

N = 20  # iterations per backend

cfg = load_config()
w, h = cfg.frame_width, cfg.frame_height
shared_cfg = {}
cal = FovCalibrator(cfg, shared_cfg)
fov_est = cal.get_fov_estimate()
fov_err = max(cal.get_fov_max_error(), cfg.fov_max_error_deg)

print(f"Frame: {w}x{h}  fov_est={fov_est:.4f}  fov_err={fov_err:.4f}")
print()

channel = grpc.insecure_channel(cfg.cedar_detect_socket)
stub = pb_grpc.CedarDetectStub(channel)

def get_cedar_centroids(reopen=False):
    req = pb.CentroidsRequest(
        input_image=pb.Image(
            width=w, height=h,
            shmem_name='efinder_frame_0', reopen_shmem=reopen,
        ),
        sigma=cfg.detect_sigma,
        detect_hot_pixels=cfg.detect_hot_pixels,
        use_binned_for_star_candidates=cfg.detect_use_binned,
        return_binned=False,
    )
    t = time.monotonic()
    resp = stub.ExtractCentroids(req, timeout=2.0)
    return resp, (time.monotonic() - t) * 1000.0

# Warm-up
get_cedar_centroids(reopen=True)

# --- Cedar backend (tetra3 Python solver) ---
import tetra3 as t3_lib
t3 = t3_lib.Tetra3(cfg.tetra3_db)

print(f"{'='*60}")
print(f"CEDAR backend (cedar-detect + tetra3 Python)  N={N}")
print(f"{'='*60}")
cedar_times, cedar_ext, cedar_slv = [], [], []
for i in range(N):
    resp, ext_ms = get_cedar_centroids()
    centroids_cedar = np.array(
        [[c.centroid_position.y, c.centroid_position.x]
         for c in resp.star_candidates],
        dtype=np.float32,
    )
    t_slv = time.monotonic()
    soln = t3.solve_from_centroids(
        centroids_cedar,
        (h, w),
        fov_estimate=fov_est,
        fov_max_error=fov_err,
        solve_timeout=cfg.solve_timeout_ms,
        match_threshold=cfg.match_threshold,
        match_radius=cfg.match_radius,
        return_matches=False,
    )
    slv_ms = (time.monotonic() - t_slv) * 1000.0
    total_ms = ext_ms + slv_ms
    solved = soln.get('status') == 1 if soln else False
    cedar_times.append(total_ms)
    cedar_ext.append(ext_ms)
    cedar_slv.append(slv_ms)
    status = 'OK' if solved else 'FAIL'
    print(f"  [{i+1:2d}] {status}  ext={ext_ms:.0f}ms  slv={slv_ms:.0f}ms  total={total_ms:.0f}ms")

print(f"  avg: ext={sum(cedar_ext)/N:.1f}ms  slv={sum(cedar_slv)/N:.1f}ms  "
      f"total={sum(cedar_times)/N:.1f}ms")
print()

# --- Tetra hybrid backend (cedar-detect + tetra3rs) ---
db = tetra3rs.SolverDatabase.load_from_file(cfg.tetra3rs_db)
print(f"{'='*60}")
print(f"TETRA hybrid (cedar-detect + tetra3rs)  N={N}")
print(f"DB: {db.num_stars} stars  {db.num_patterns} patterns")
print(f"{'='*60}")
tetra_times, tetra_ext, tetra_slv = [], [], []
last_quat = None
for i in range(N):
    resp, ext_ms = get_cedar_centroids()
    centroids_tetra = np.array(
        [[c.centroid_position.x - w / 2.0,
          c.centroid_position.y - h / 2.0]
         for c in resp.star_candidates],
        dtype=np.float64,
    )
    hint_label = 'seeded' if last_quat is not None else 'blind '
    t_slv = time.monotonic()
    result = db.solve_from_centroids(
        centroids_tetra,
        fov_estimate_deg=fov_est,
        fov_max_error_deg=fov_err,
        image_width=w, image_height=h,
        match_radius=cfg.match_radius,
        match_threshold=cfg.match_threshold,
        solve_timeout_ms=cfg.solve_timeout_ms,
        attitude_hint=last_quat,
        hint_uncertainty_deg=0.1,
        strict_hint=False,
    )
    slv_ms = (time.monotonic() - t_slv) * 1000.0
    total_ms = ext_ms + slv_ms
    if result is not None:
        last_quat = result.quaternion
        status = 'OK'
    else:
        status = 'FAIL'
    tetra_times.append(total_ms)
    tetra_ext.append(ext_ms)
    tetra_slv.append(slv_ms)
    print(f"  [{i+1:2d}] {status} ({hint_label})  ext={ext_ms:.0f}ms  slv={slv_ms:.0f}ms  total={total_ms:.0f}ms")

print(f"  avg: ext={sum(tetra_ext)/N:.1f}ms  slv={sum(tetra_slv)/N:.1f}ms  "
      f"total={sum(tetra_times)/N:.1f}ms")
print()

# --- Summary ---
print(f"{'='*60}")
print(f"SUMMARY")
print(f"  Cedar:        avg total = {sum(cedar_times)/N:.1f}ms")
print(f"  Tetra hybrid: avg total = {sum(tetra_times)/N:.1f}ms")
print(f"  Speedup: {sum(cedar_times)/N / (sum(tetra_times)/N):.2f}x" if sum(tetra_times) > 0 else "")
