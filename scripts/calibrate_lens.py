#!/usr/bin/env python3
"""
calibrate_lens.py — OFF-DEVICE lens distortion calibration helper.

This is a developer-box tool, NOT installed on the Pi. Given a directory of
solved-frame PNGs (e.g. the captures written when save_solved_frames=true,
copied off the device), it extracts star centroids and runs tetra3rs'
calibrate_camera to fit the SIP distortion, then prints a radial-k-equivalent
coefficient and the config line to set in diofinder.conf.

Why off-device: the calibration fit is heavier than the Pi Zero 2W wants to do
live, and tetra3rs (the pip package with calibrate_camera) is a dev-box
dependency, not part of the on-device wheel set. Run it once after changing the
lens, then copy the printed `distortion:` value into the device config.

Usage:
    python3 scripts/calibrate_lens.py --images /path/to/frames/ \
        --db /path/to/gaia_db.bin --fov 13.6

Install the dependency first:
    pip install tetra3rs numpy pillow

The script is deliberately simple and well-commented; tune the few constants
near the top to your rig if needed.
"""
import argparse
import glob
import os
import sys

# --- Tunables --------------------------------------------------------------
DEFAULT_FOV_DEG = 13.6      # nominal horizontal FOV; refined by the solve
SIGMA = 5.0                 # extraction threshold in noise sigmas
MIN_STARS = 8               # skip frames with fewer detected stars


def _die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def _import_tetra3rs():
    """Guarded import with a clear install message."""
    try:
        import tetra3  # the tetra3rs PyPI package imports as `tetra3`
        return tetra3
    except ImportError:
        _die(
            "ERROR: tetra3rs is not installed.\n"
            "  This is an OFF-DEVICE helper. Install on your dev box with:\n"
            "      pip install tetra3rs numpy pillow\n"
            "  (tetra3rs is the PyPI wheel; it imports as `tetra3`.)")


def _load_png(path):
    import numpy as np
    from PIL import Image
    img = Image.open(path).convert("L")
    return np.asarray(img, dtype=np.uint8)


def main():
    ap = argparse.ArgumentParser(
        description="Off-device lens distortion calibration from solved PNGs.")
    ap.add_argument("--images", required=True,
                    help="Directory of solved-frame PNGs (or a glob).")
    ap.add_argument("--db", required=True,
                    help="tetra3rs solver database (.bin) for the solve.")
    ap.add_argument("--fov", type=float, default=DEFAULT_FOV_DEG,
                    help=f"Nominal horizontal FOV in degrees (default {DEFAULT_FOV_DEG}).")
    ap.add_argument("--sigma", type=float, default=SIGMA,
                    help=f"Extraction sigma (default {SIGMA}).")
    ap.add_argument("--max-images", type=int, default=20,
                    help="Cap the number of frames used (default 20).")
    args = ap.parse_args()

    tetra3 = _import_tetra3rs()
    import numpy as np

    # Collect frames.
    if os.path.isdir(args.images):
        paths = sorted(glob.glob(os.path.join(args.images, "*.png")))
    else:
        paths = sorted(glob.glob(args.images))
    if not paths:
        _die(f"No PNGs found at {args.images!r}")
    paths = paths[: args.max_images]
    print(f"Using {len(paths)} frame(s) for calibration.")

    if not os.path.exists(args.db):
        _die(f"Database not found: {args.db}")

    # tetra3rs API surface varies a little across versions; we use the public
    # extract + calibrate entry points and degrade with a clear message if the
    # installed version differs.
    try:
        from tetra3 import calibrate_camera  # re-exported helper
    except Exception:
        calibrate_camera = getattr(tetra3, "calibrate_camera", None)
    if calibrate_camera is None:
        _die("Installed tetra3rs lacks calibrate_camera; upgrade with "
             "`pip install -U tetra3rs`.")

    # Load the solver DB once.
    try:
        db = tetra3.SolverDatabase.load(args.db) \
            if hasattr(tetra3, "SolverDatabase") else None
    except Exception as e:
        print(f"warning: could not preload database object ({e}); "
              "passing the path directly.")
        db = None

    images = []
    for p in paths:
        try:
            images.append(_load_png(p))
        except Exception as e:
            print(f"  skip {os.path.basename(p)}: {e}")
    if not images:
        _die("No readable images.")

    print("Running calibrate_camera (this can take a while)…")
    try:
        # The exact signature depends on the tetra3rs version. The common form
        # takes the list of images, a database, and an FOV estimate, and fits
        # SIP distortion across all solvable frames.
        result = calibrate_camera(
            images,
            database=(db if db is not None else args.db),
            fov_estimate=args.fov,
            sigma=args.sigma,
            min_stars=MIN_STARS,
        )
    except TypeError:
        # Fallback to a positional-only call if keywords differ.
        result = calibrate_camera(images, args.db, args.fov)
    except Exception as e:
        _die(f"calibrate_camera failed: {e}\n"
             "Check the tetra3rs version and its calibrate_camera signature; "
             "this helper targets tetra3rs >= 0.4.")

    # Pull a radial-k-equivalent out of the result. tetra3rs returns SIP
    # polynomial coefficients; the dominant low-order radial term is the most
    # useful single number for diofinder.conf's scalar `distortion` field.
    radial_k = _extract_radial_k(result)
    rms = getattr(result, "rms_arcsec", None) or _maybe(result, "rms")

    print("\n=== Calibration result ===")
    if rms is not None:
        print(f"  residual RMS: {rms:.2f} arcsec")
    if radial_k is None:
        print("  Could not derive a scalar radial-k from the SIP fit.")
        print("  Inspect the full result object for the polynomial terms:")
        print(f"  {result!r}")
    else:
        print(f"  radial-k equivalent: {radial_k:.6f}")
        print("\nSet this on the device in /etc/diofinder/diofinder.conf:")
        print(f"    distortion: {radial_k:.6f}")
        print("Then: sudo systemctl restart diofinder")


def _maybe(obj, name):
    return getattr(obj, name, None)


def _extract_radial_k(result):
    """Best-effort scalar radial coefficient from a tetra3rs calibrate result.

    Tries a few attribute shapes so this keeps working across minor version
    changes. Returns None if nothing recognizable is present.
    """
    # Direct radial coefficient, if the result exposes one.
    for attr in ("radial_k", "distortion", "k1"):
        v = getattr(result, attr, None)
        if isinstance(v, (int, float)):
            return float(v)
    # A distortion sub-object with a radial term.
    dist = getattr(result, "distortion", None)
    if dist is not None:
        for attr in ("k1", "radial_k", "k"):
            v = getattr(dist, attr, None)
            if isinstance(v, (int, float)):
                return float(v)
    # A camera_model with a distortion field.
    cam = getattr(result, "camera_model", None) or getattr(result, "camera", None)
    if cam is not None:
        return _extract_radial_k(cam)
    return None


if __name__ == "__main__":
    main()
