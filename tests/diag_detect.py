#!/usr/bin/env python3
"""
eFinder detect diagnostic — exercises cedar-detect centroid extraction
in isolation, with timing at every step.

Stages:
  0. Config + library imports
  1. cedar-detect gRPC connectivity
  2. Frame source  (live SHM from running daemon  |  test PNG  |  synthetic)
  3. ExtractCentroids — timing, star count, peak pixel, noise
  4. Sigma sweep — find the sigma that yields the most usable stars

Works with or without the efinder daemon running:
  - Daemon running  → reads frame from efinder_frame_0 SHM (non-destructive)
  - Daemon stopped  → requires --image or generates a synthetic star field
                      (synthetic will extract stars but not solve)

Usage:
  # With daemon running (read live frame):
  sudo /opt/efinder/venv/bin/python3 tests/diag_detect.py

  # With daemon stopped, using a saved capture:
  sudo /opt/efinder/venv/bin/python3 tests/diag_detect.py \\
      --image /var/lib/efinder/captures/capture_20250101_123456_tetra_failed.png

  # Override sigma / repetition count:
  sudo /opt/efinder/venv/bin/python3 tests/diag_detect.py --sigma 5.0 --reps 10

  # Disable hot-pixel removal:
  sudo /opt/efinder/venv/bin/python3 tests/diag_detect.py --no-hot-pixels
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

def tag(label, msg):   print(f"  [{label}] {msg}")
def sep(title):        print(f"\n{'='*62}\n{title}\n{'='*62}")

# ── Args ──────────────────────────────────────────────────────────────────────
p = argparse.ArgumentParser(description=__doc__,
                             formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("--image", metavar="PNG",
               help="Path to a grayscale (or RGB) PNG to use as the test frame")
p.add_argument("--sigma", type=float, default=None,
               help="Override detect_sigma from config")
p.add_argument("--reps", type=int, default=6,
               help="Number of timed ExtractCentroids calls (default 6)")
p.add_argument("--no-hot-pixels", dest="hot_pixels", action="store_false",
               default=None, help="Disable hot-pixel removal")
p.add_argument("--hot-pixels", dest="hot_pixels", action="store_true",
               help="Force hot-pixel removal on (overrides config)")
p.add_argument("--sigma-sweep", action="store_true",
               help="After main test, sweep sigma 3–12 to find the best setting")
p.add_argument("--binned", action="store_true",
               help="Set use_binned_for_star_candidates=True")
args = p.parse_args()

# ── Stage 0: Config & imports ─────────────────────────────────────────────────
sep("Stage 0: Config & library imports")

try:
    from efinder.config import load_config
    cfg = load_config()
    tag(PASS, f"Config loaded from {cfg.__class__.__module__}")
    tag(INFO, cfg.summary())
except Exception as e:
    tag(FAIL, f"Config load: {e}")
    sys.exit(1)

w, h     = cfg.frame_width, cfg.frame_height
sigma    = args.sigma if args.sigma is not None else cfg.detect_sigma
hot      = cfg.detect_hot_pixels if args.hot_pixels is None else args.hot_pixels
binned   = args.binned or cfg.detect_use_binned

tag(INFO, f"Frame: {w}x{h}  sigma: {sigma:.1f}  hot_pixels: {hot}  binned: {binned}")
tag(INFO, f"cedar_detect_socket: {cfg.cedar_detect_socket}")
tag(INFO, f"min_centroids: {cfg.min_centroids}")

try:
    import numpy as np
    tag(PASS, f"numpy {np.__version__}")
except Exception as e:
    tag(FAIL, f"numpy: {e}")
    sys.exit(1)

try:
    import grpc
    tag(PASS, f"grpc {grpc.__version__}")
except Exception as e:
    tag(FAIL, f"grpc: {e}")
    sys.exit(1)

try:
    import cedar_detect_pb2 as pb
    import cedar_detect_pb2_grpc as pb_grpc
    tag(PASS, "cedar_detect protobuf")
except Exception as e:
    tag(FAIL, f"cedar_detect protobuf: {e}")
    sys.exit(1)

# ── Stage 1: cedar-detect connectivity ───────────────────────────────────────
sep(f"Stage 1: cedar-detect connectivity  ({cfg.cedar_detect_socket})")

channel = grpc.insecure_channel(cfg.cedar_detect_socket)
stub    = pb_grpc.CedarDetectStub(channel)

try:
    grpc.channel_ready_future(channel).result(timeout=3.0)
    tag(PASS, "Channel connected")
except Exception as e:
    tag(WARN, f"Channel not immediately ready: {e!r}  "
              "(will still attempt RPC — server may answer)")

# ── Stage 2: Frame source ─────────────────────────────────────────────────────
sep("Stage 2: Frame source")

from multiprocessing import shared_memory
from multiprocessing import resource_tracker as _rt

OWN_SHM_NAME = "efinder_diag_frame"

def _borrow_shm(name: str) -> shared_memory.SharedMemory:
    """Attach to an existing SHM segment without taking ownership.
    Unregisters from the resource tracker so Python does not warn about
    a 'leaked' segment that belongs to another process (the daemon)."""
    shm = shared_memory.SharedMemory(name=name, create=False)
    try:
        _rt.unregister(shm._name, 'shared_memory')
    except Exception:
        pass
    return shm
own_shm  = None
shm_slot = None

def _cleanup():
    if own_shm is not None:
        try:
            own_shm.close()
            own_shm.unlink()
        except Exception:
            pass

def _make_own_shm(frame: "np.ndarray") -> shared_memory.SharedMemory:
    # Remove stale segment from a previous crashed run, if any
    try:
        _s = shared_memory.SharedMemory(name=OWN_SHM_NAME, create=False)
        _s.close(); _s.unlink()
    except Exception:
        pass
    shm = shared_memory.SharedMemory(name=OWN_SHM_NAME, create=True, size=frame.nbytes)
    buf = np.ndarray(frame.shape, dtype=np.uint8, buffer=shm.buf)
    np.copyto(buf, frame)
    return shm

if args.image:
    try:
        from PIL import Image
        img = Image.open(args.image).convert("L")
        if img.size != (w, h):
            tag(INFO, f"Resizing {img.size} → {(w, h)}")
            img = img.resize((w, h), Image.LANCZOS)
        frame = np.array(img, dtype=np.uint8)
        own_shm  = _make_own_shm(frame)
        shm_slot = OWN_SHM_NAME
        tag(PASS, f"Test image: {args.image}")
        tag(INFO, f"  peak={frame.max()}  mean={frame.mean():.1f}  "
                  f"p95={int(np.percentile(frame, 95))}  "
                  f"nonzero={np.count_nonzero(frame)}")
    except Exception as e:
        tag(FAIL, f"Could not load image: {e}")
        sys.exit(1)

else:
    # Try to attach to daemon's live SHM first
    daemon_shm_ok = False
    for slot_name in ("efinder_frame_0", "efinder_frame_1", "efinder_frame_2"):
        try:
            _s = _borrow_shm(slot_name)
            _arr = np.ndarray((h, w), dtype=np.uint8, buffer=_s.buf)
            pk  = int(_arr.max())
            mn  = float(_arr.mean())
            p95 = int(np.percentile(_arr, 95))
            _s.close()
            shm_slot = slot_name
            daemon_shm_ok = True
            tag(PASS, f"Live SHM: {slot_name}  peak={pk}  mean={mn:.1f}  p95={p95}")
            if pk < 20:
                tag(WARN, f"Peak={pk} < 20 — solver_proc would skip this frame entirely. "
                          "Check exposure/gain or camera mode (test_mode may be on).")
            break
        except Exception:
            continue

    if not daemon_shm_ok:
        tag(WARN, "No live efinder SHM found — creating synthetic star field")
        tag(INFO, "  (Centroids will be extracted but a blind plate solve is unlikely to succeed)")
        frame = np.zeros((h, w), dtype=np.uint8)
        rng = np.random.default_rng(42)
        for _ in range(40):
            cy  = int(rng.integers(15, h - 15))
            cx  = int(rng.integers(15, w - 15))
            br  = int(rng.integers(100, 230))
            ys, xs = np.ogrid[-6:7, -6:7]
            patch  = (br * np.exp(-(ys**2 + xs**2) / 5.0)).astype(np.uint8)
            y0, y1 = max(0, cy - 6), min(h, cy + 7)
            x0, x1 = max(0, cx - 6), min(w, cx + 7)
            frame[y0:y1, x0:x1] = np.maximum(
                frame[y0:y1, x0:x1], patch[:y1 - y0, :x1 - x0])
        own_shm  = _make_own_shm(frame)
        shm_slot = OWN_SHM_NAME
        tag(INFO, f"Synthetic: 40 Gaussian stars  peak={frame.max()}")

# ── Stage 3: ExtractCentroids timing ─────────────────────────────────────────
sep(f"Stage 3: ExtractCentroids  sigma={sigma:.1f}  hot={hot}  reps={args.reps}")

def _req(reopen: bool, sig: float = sigma) -> "pb.CentroidsRequest":
    return pb.CentroidsRequest(
        input_image=pb.Image(
            width=w, height=h,
            shmem_name=shm_slot,
            reopen_shmem=reopen,
        ),
        sigma=sig,
        detect_hot_pixels=hot,
        use_binned_for_star_candidates=binned,
        return_binned=False,
    )

# Warm-up: reopen_shmem=True so cedar-detect maps the segment
tag(INFO, "Warm-up (reopen_shmem=True) …")
try:
    t0   = time.monotonic()
    resp = stub.ExtractCentroids(_req(reopen=True), timeout=5.0)
    wu   = (time.monotonic() - t0) * 1000.0
    tag(PASS, f"Warm-up: {wu:.0f}ms  stars={len(resp.star_candidates)}  "
              f"peak={resp.peak_star_pixel}  noise={resp.noise_estimate:.2f}")
except Exception as e:
    tag(FAIL, f"ExtractCentroids warm-up failed: {e}")
    _cleanup()
    sys.exit(1)

print()
times, last_resp = [], None
for i in range(args.reps):
    try:
        t0   = time.monotonic()
        resp = stub.ExtractCentroids(_req(reopen=False), timeout=5.0)
        ms   = (time.monotonic() - t0) * 1000.0
        n    = len(resp.star_candidates)
        pk   = int(resp.peak_star_pixel) if resp.peak_star_pixel else 0
        ns   = float(resp.noise_estimate)
        times.append(ms)
        last_resp = resp
        status = PASS if n >= cfg.min_centroids else WARN
        tag(status, f"[{i+1:2d}] {ms:6.1f}ms  stars={n:3d}  peak={pk:3d}  noise={ns:.2f}")
    except Exception as e:
        tag(FAIL, f"[{i+1:2d}] RPC failed: {e}")

if times:
    avg = sum(times) / len(times)
    print(f"\n  Timing:  avg={avg:.1f}ms  min={min(times):.1f}ms  max={max(times):.1f}ms  "
          f"({len(times)}/{args.reps} succeeded)")

if last_resp is not None:
    n_stars = len(last_resp.star_candidates)
    if n_stars < cfg.min_centroids:
        print()
        tag(WARN, f"Only {n_stars} stars detected on last call; need {cfg.min_centroids} to solve.")
        tag(WARN, f"  Try: lower --sigma (current {sigma:.1f}), "
                  "brighter exposure, or --no-hot-pixels if sky is dark.")
    if last_resp.peak_star_pixel and int(last_resp.peak_star_pixel) < 20:
        tag(WARN, "Peak pixel < 20 — solver_proc will discard this frame without calling cedar-detect. "
                  "Increase exposure or check that the camera is sending real frames.")

# ── Stage 4: Sigma sweep ──────────────────────────────────────────────────────
if args.sigma_sweep:
    sep("Stage 4: Sigma sweep (3 → 12)  — find best detection threshold")
    print(f"  {'sigma':>6}  {'stars':>5}  {'peak':>5}  {'noise':>7}  {'time_ms':>8}")
    for sig in [3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 12.0]:
        try:
            t0   = time.monotonic()
            resp = stub.ExtractCentroids(_req(reopen=False, sig=sig), timeout=5.0)
            ms   = (time.monotonic() - t0) * 1000.0
            n    = len(resp.star_candidates)
            pk   = int(resp.peak_star_pixel) if resp.peak_star_pixel else 0
            ns   = float(resp.noise_estimate)
            marker = " ← min_centroids met" if n >= cfg.min_centroids else ""
            print(f"  {sig:6.1f}  {n:5d}  {pk:5d}  {ns:7.2f}  {ms:8.1f}{marker}")
        except Exception as e:
            print(f"  {sig:6.1f}  ERROR: {e}")

_cleanup()
print()
