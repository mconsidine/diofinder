#!/usr/bin/env python3
"""Re-solve every frame of a debug bundle and emit a filename -> data JSON map.

The per-frame RA/Dec recorded live in older bundles (`imu.json` ref_*) is the
PREVIOUS solve at grab time — up to one frame period stale. This tool solves
each bundled frame itself, offline, and merges the bundle's exact capture
metadata (seq / SensorTimestamp-derived exposure_start_utc / actual exposure
and gain — present in bundles from v0.11.46 onward), producing one JSON packet:

    {
      "bundle": "diofinder_debug_20260709003212.zip",
      "generated_utc": "...",
      "solver": {"db": "...", "fov_estimate_deg": ..., "fov_max_error_deg": ...},
      "frames": {
        "frame_01_raw.png": {
          "solved": true, "ra_deg": ..., "dec_deg": ..., "roll_deg": ...,
          "fov_deg": ..., "matches": ..., "stars": ..., "solve_ms": ...,
          "solve_pass": "calibrated" | "loose" | null,
          "seq": ..., "exposure_start_utc": ..., "readout_start_utc": ...,
          "exposure_s": ..., "gain": ...,
          "saved_at": ..., "live_solution": {...} | null
        }, ...
      }
    }

Usage:
    python3 tests/bundle_solve.py BUNDLE.zip [--out FILE.json] [--db PATH.npz]
                                  [--sigma S] [--fov F] [--fov-err E]

The star database must be available locally (--db, or the bundle conf's
solver_db resolved under /var/lib/diofinder). Extraction mirrors the live
solver via the bundle's effective_params.json (extractor backend, sigma,
bg_mode, kernel_sigma, noise_mode, max_axis_ratio, detect_bin, max stars),
exactly like diag_solve.py --match-runtime.
"""
import argparse
import json
import pathlib
import sys
import time


# ── Pure helpers (unit-tested without tetra3/star_detect installed) ───────────

def load_bundle_meta(root: pathlib.Path) -> dict:
    """Per-frame metadata map {raw_png_name: entry} from frames.json
    (v0.11.46+), falling back to imu.json (older bundles: wall_time + imu ref
    only)."""
    fj = root / "frames.json"
    if fj.exists():
        try:
            return json.loads(fj.read_text())
        except Exception:
            pass
    out = {}
    ij = root / "imu.json"
    if ij.exists():
        try:
            for rec in json.loads(ij.read_text()):
                name = rec.get("frame")
                if name:
                    out[f"{name}_raw.png"] = {
                        "seq": rec.get("seq"),
                        "saved_at": rec.get("wall_time"),
                        "imu": rec.get("imu"),
                    }
        except Exception:
            pass
    return out


def build_record(solve_result, solve_pass, n_stars, solve_ms, meta_entry):
    """Assemble one output frame record from a tetra3 solve dict (or None),
    which pass solved it ('calibrated'/'loose'/None), the detection count,
    and the bundle's per-frame metadata entry (may be {})."""
    meta_entry = meta_entry or {}
    rec = {
        "solved": bool(solve_result and solve_result.get("RA") is not None),
        "ra_deg": None, "dec_deg": None, "roll_deg": None, "fov_deg": None,
        "matches": None,
        "stars": int(n_stars),
        "solve_ms": round(float(solve_ms), 2),
        "solve_pass": solve_pass,
        # Exact capture metadata (present in v0.11.46+ bundles)
        "seq": meta_entry.get("seq"),
        "exposure_start_utc": meta_entry.get("exposure_start_utc"),
        "readout_start_utc": meta_entry.get("readout_start_utc"),
        "exposure_s": meta_entry.get("exposure_s"),
        "gain": (meta_entry.get("capture") or {}).get("gain"),
        "saved_at": meta_entry.get("saved_at"),
        # The solve recorded live at capture time, for comparison (None in
        # older bundles or when the frame didn't solve live).
        "live_solution": meta_entry.get("solution"),
    }
    if rec["solved"]:
        rec.update(
            ra_deg=float(solve_result["RA"]),
            dec_deg=float(solve_result["Dec"]),
            roll_deg=float(solve_result.get("Roll") or 0.0),
            fov_deg=(float(solve_result["FOV"])
                     if solve_result.get("FOV") is not None else None),
            matches=(int(solve_result["Matches"])
                     if solve_result.get("Matches") is not None else None),
        )
    return rec


# ── Main (heavy imports deferred so the helpers stay unit-testable) ───────────

def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bundle", help="debug bundle .zip")
    ap.add_argument("--out", help="output JSON path "
                                  "(default: <bundle>_solutions.json)")
    ap.add_argument("--db", help="star database .npz (default: bundle conf's "
                                 "solver_db under /var/lib/diofinder)")
    ap.add_argument("--sigma", type=float, help="override detection sigma")
    ap.add_argument("--fov", type=float, help="override FOV estimate (deg)")
    ap.add_argument("--fov-err", type=float,
                    help="override calibrated FOV tolerance (deg)")
    args = ap.parse_args(argv)

    import os
    import tempfile
    import zipfile

    import numpy as np
    from PIL import Image as PILImage

    bp = pathlib.Path(args.bundle)
    if not bp.exists():
        print(f"FAIL: bundle not found: {bp}"); return 1
    tmp = tempfile.TemporaryDirectory(prefix="diofinder_bundle_")
    with zipfile.ZipFile(str(bp)) as zfh:
        zfh.extractall(tmp.name)
    root = pathlib.Path(tmp.name)

    conf = root / "diofinder.conf"
    if conf.exists():
        os.environ["DIOFINDER_CONFIG"] = str(conf)
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from diofinder.config import load_config
    cfg = load_config()

    eff = {}
    effp = root / "effective_params.json"
    if effp.exists():
        try:
            eff = json.loads(effp.read_text())
        except Exception as e:
            print(f"WARN: could not parse effective_params.json: {e}")
    sp = eff.get("solver_params") or {}
    mp = eff.get("match_params") or {}

    sigma = args.sigma if args.sigma is not None else \
        float(sp.get("detect_sigma", cfg.detect_sigma))
    kernel_sigma = float(sp.get("detect_kernel_sigma", cfg.detect_kernel_sigma))
    noise_mode = sp.get("detect_noise_mode", cfg.detect_noise_mode)
    bg_mode = sp.get("detect_bg_mode", cfg.detect_bg_mode)
    backend = sp.get("extractor_backend", cfg.extractor_backend)
    min_c = int(sp.get("min_centroids", cfg.min_centroids))
    max_c = int(sp.get("max_solve_stars", cfg.max_solve_stars))
    timeout = int(sp.get("solve_timeout_ms", cfg.solve_timeout_ms))
    det_bin = int(cfg.detect_bin)
    mar = float(sp.get("detect_max_axis_ratio", cfg.detect_max_axis_ratio) or 0.0)
    max_axis_ratio = float("inf") if mar <= 0.0 else mar
    fov = args.fov if args.fov is not None else \
        float(eff.get("fov_estimate_deg") or cfg.fov_deg)
    fov_err = args.fov_err if args.fov_err is not None else \
        float(eff.get("fov_max_error_deg") or cfg.fov_calibrated_max_error_deg)
    loose_err = max(float(cfg.fov_max_error_deg), fov_err)

    if args.db:
        db_path = pathlib.Path(args.db)
    else:
        db_path = pathlib.Path(
            cfg.solver_db if str(cfg.solver_db).startswith("/")
            else f"/var/lib/diofinder/{cfg.solver_db}.npz")
    if not db_path.exists():
        print(f"FAIL: star database not found: {db_path} (use --db)"); return 1

    import tetra3
    import star_detect as _sd
    _sd.set_num_threads(2)
    print(f"db={db_path}  backend={backend}  sigma={sigma}  bin={det_bin}  "
          f"bg_mode={bg_mode}  FOV={fov:.2f}±{fov_err:.2f} "
          f"(loose ±{loose_err:.2f})")
    t3 = tetra3.Tetra3(str(db_path))

    def extract(arr):
        if backend == "tetra3" and hasattr(t3, "get_centroids_from_image_fast"):
            opts = dict(downsample=1, sigma=float(sigma),
                        bg_sub_mode="local_mean",
                        sigma_mode=("global_root_square"
                                    if noise_mode == "global_rms"
                                    else "local_median_abs"),
                        binary_open=True, min_area=5, max_area=100)
            if max_axis_ratio != float("inf"):
                opts["max_axis_ratio"] = max_axis_ratio
            yx = t3.get_centroids_from_image_fast(arr, **opts)
            cent = (np.asarray(yx, dtype=np.float64)
                    if yx is not None and len(yx) else None)
        else:
            raw = _sd.detect_stars(arr, sigma=sigma, bin=det_bin,
                                   centroid_full_res=True, bg_mode=bg_mode,
                                   noise_mode=noise_mode,
                                   kernel_sigma=kernel_sigma,
                                   max_axis_ratio=max_axis_ratio)
            cent = (np.array([[s[1], s[0]] for s in raw], dtype=np.float64)
                    if raw else None)
        n = len(cent) if cent is not None else 0
        if cent is not None and len(cent) > max_c:
            cent = cent[:max_c]
        return cent, n

    meta_map = load_bundle_meta(root)
    frames = sorted(root.glob("frame_*_raw.png"))
    if not frames:
        print("FAIL: no frame_*_raw.png in bundle"); return 1

    out_frames = {}
    for p in frames:
        arr = np.array(PILImage.open(p).convert("L"), dtype=np.uint8)
        t0 = time.monotonic()
        cent, n = extract(arr)
        soln, solve_pass = None, None
        if cent is not None and n >= min_c:
            soln = t3.solve_from_centroids(
                cent, arr.shape, fov_estimate=fov, fov_max_error=fov_err,
                solve_timeout=timeout)
            if soln and soln.get("RA") is not None:
                solve_pass = "calibrated"
            elif loose_err > fov_err:
                # The live solver's escape hatch: retry with the loose window.
                soln = t3.solve_from_centroids(
                    cent, arr.shape, fov_estimate=fov,
                    fov_max_error=loose_err, solve_timeout=timeout)
                if soln and soln.get("RA") is not None:
                    solve_pass = "loose"
        ms = (time.monotonic() - t0) * 1000
        rec = build_record(soln, solve_pass, n, ms, meta_map.get(p.name))
        out_frames[p.name] = rec
        print(f"  {p.name}: stars={n:4d}  "
              + (f"RA={rec['ra_deg']:.4f}  Dec={rec['dec_deg']:+.4f}  "
                 f"matches={rec['matches']}  [{solve_pass}]"
                 if rec["solved"] else "UNSOLVED")
              + (f"  exp_start={rec['exposure_start_utc']}"
                 if rec.get("exposure_start_utc") else ""))

    from datetime import datetime, timezone
    packet = {
        "bundle": bp.name,
        "generated_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "solver": {"db": str(db_path), "backend": backend, "sigma": sigma,
                   "detect_bin": det_bin, "bg_mode": bg_mode,
                   "fov_estimate_deg": fov, "fov_max_error_deg": fov_err},
        "frames": out_frames,
    }
    out_path = pathlib.Path(args.out) if args.out else \
        bp.with_name(bp.stem + "_solutions.json")
    out_path.write_text(json.dumps(packet, indent=2) + "\n")
    n_ok = sum(1 for r in out_frames.values() if r["solved"])
    print(f"\n{n_ok}/{len(out_frames)} solved -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
