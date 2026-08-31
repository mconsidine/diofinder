#!/usr/bin/env python3
"""
reproject_frame.py — turn a debug-bundle frame into a North-up image you can
lay next to a star chart.

Reads a diofinder debug bundle (the .zip the web UI's "Download debug bundle"
button produces), takes one `frame_XX_raw.png` and its `frames.json` solve
record, and writes a chart-aligned PNG: **North up, East left** (the standard
direct-sky orientation), with an optional contrast stretch so the stars are
visible and a compass + center crosshair overlay.

This is an OFF-DEVICE tool — pure stdlib + numpy + Pillow, no diofinder or
solver imports — so you run it on the laptop where you're comparing to the
chart, not on the Pi.

Usage
-----
    python3 tests/reproject_frame.py BUNDLE.zip                 # first solved frame
    python3 tests/reproject_frame.py BUNDLE.zip frame_03        # a specific frame
    python3 tests/reproject_frame.py BUNDLE.zip 3 -o out.png    # by index
    python3 tests/reproject_frame.py BUNDLE.zip --list          # list frames + solutions

Options
-------
    -o, --out FILE     output PNG (default: <frame>_northup.png in the cwd)
    --invert           rotate by +roll instead of -roll (see "sign check" below)
    --no-flip          do NOT apply the is_mirrored horizontal flip (debugging)
    --raw              skip the contrast stretch (align the raw pixels as-is)
    --no-annotate      do not draw the compass / crosshair / caption
    --list             print each frame's solve summary and exit

The transform
-------------
The solve record gives, per frame: `roll_deg` (celestial North's angle,
**counter-clockwise from image "up"/y=0**), `is_mirrored` (parity), and the
image-center `center_ra_deg`/`center_dec_deg` (all J2000/ICRS). The reported
roll is already parity-corrected, so the alignment is the same recipe for both
parities:

    1. if is_mirrored:  flip the frame left<->right
    2. rotate by -roll_deg   (Pillow rotates CCW for positive angles)

Result: North up, East left — matching a chart drawn "North up" (a planetarium
app set to no flip / direct view).

Sign check (do this once)
-------------------------
The image y-axis points *down*, so "counter-clockwise" in the solver's frame
vs. what Pillow calls a positive rotation is exactly the convention that can
flip signs between tools. Verify once on a known frame: after reprojection, a
star that is **north of the image center** (catalog Dec > `center_dec_deg`)
must land **above** the center crosshair. If it lands below, re-run with
`--invert` and use that from then on.
"""

import argparse
import io
import json
import os
import sys
import zipfile

import numpy as np

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except ImportError:
    sys.exit("error: Pillow is required (pip install Pillow)")


# ----------------------------------------------------------------------------
# Bundle reading
# ----------------------------------------------------------------------------
def load_bundle(path):
    """Return (zipfile.ZipFile, frames_map dict). frames_map may be {}."""
    zf = zipfile.ZipFile(path)
    frames = {}
    if "frames.json" in zf.namelist():
        try:
            frames = json.loads(zf.read("frames.json"))
        except Exception as e:
            print(f"warning: could not parse frames.json: {e}", file=sys.stderr)
    return zf, frames


def resolve_frame_name(zf, frames, ident):
    """Map a user identifier to a 'frame_XX_raw.png' member name."""
    names = [n for n in zf.namelist() if n.endswith("_raw.png")]
    names.sort()
    if ident is None:
        # First frame that actually solved, else the first frame.
        for n in names:
            sol = (frames.get(n) or {}).get("solution") or {}
            if sol.get("solved"):
                return n
        return names[0] if names else None
    # Accept 'frame_03', 'frame_03_raw.png', '3', or a bare index.
    if ident in zf.namelist():
        return ident
    cand = ident if ident.endswith("_raw.png") else f"{ident}_raw.png"
    if cand in zf.namelist():
        return cand
    if ident.isdigit():
        cand = f"frame_{int(ident):02d}_raw.png"
        if cand in zf.namelist():
            return cand
    for n in names:
        if ident in n:
            return n
    return None


# ----------------------------------------------------------------------------
# Formatting helpers
# ----------------------------------------------------------------------------
def fmt_ra(ra_deg):
    if ra_deg is None:
        return "?"
    h = ra_deg / 15.0
    hh = int(h)
    mm = int((h - hh) * 60)
    ss = (h - hh - mm / 60.0) * 3600
    return f"{hh:02d}h{mm:02d}m{ss:04.1f}s ({ra_deg:.4f}°)"


def fmt_dec(dec_deg):
    if dec_deg is None:
        return "?"
    sign = "-" if dec_deg < 0 else "+"
    a = abs(dec_deg)
    dd = int(a)
    mm = int((a - dd) * 60)
    ss = (a - dd - mm / 60.0) * 3600
    return f"{sign}{dd:02d}°{mm:02d}'{ss:04.1f}\" ({dec_deg:+.4f}°)"


# ----------------------------------------------------------------------------
# Image processing
# ----------------------------------------------------------------------------
def asinh_stretch(arr, black_pct=45.0, white_pct=99.8, softness=0.12):
    """arcsinh stretch a 2-D uint8/float array -> uint8, so faint stars show.

    Mirrors the bundle's arcsinh display style: subtract a background black
    point, scale to a high percentile, and asinh-compand so faint stars lift
    without blowing out the bright ones.
    """
    a = arr.astype(np.float64)
    lo = np.percentile(a, black_pct)
    a = np.clip(a - lo, 0.0, None)
    hi = np.percentile(a, white_pct)
    if hi <= 0:
        hi = a.max() or 1.0
    norm = a / hi
    out = np.arcsinh(norm / softness) / np.arcsinh(1.0 / softness)
    out = np.clip(out, 0.0, 1.0)
    return (out * 255.0 + 0.5).astype(np.uint8)


def reproject(gray_u8, roll_deg, is_mirrored, do_flip=True, invert=False):
    """Flip-if-mirrored then rotate to North-up. Returns an RGB PIL image."""
    img = Image.fromarray(gray_u8, mode="L").convert("RGB")
    if is_mirrored and do_flip:
        img = ImageOps.mirror(img)
    angle = roll_deg if invert else -roll_deg
    # Pillow rotates CCW for a positive angle; expand keeps all pixels.
    img = img.rotate(angle, expand=True, fillcolor=(0, 0, 0), resample=Image.BICUBIC)
    return img


def annotate(img, frame, sol, applied):
    """Draw compass (N up, E left), a center crosshair, and a caption."""
    red = (220, 40, 40)
    d = ImageDraw.Draw(img)
    w, h = img.size
    cx, cy = w // 2, h // 2

    # Center crosshair = the image-center sky point (center_ra/dec).
    r = max(6, w // 60)
    d.line([(cx - r, cy), (cx + r, cy)], fill=red, width=2)
    d.line([(cx, cy - r), (cx, cy + r)], fill=red, width=2)

    # Compass: North up, East left (post-alignment, non-mirrored sky).
    m = max(24, w // 20)
    d.line([(cx, m + 18), (cx, m)], fill=red, width=3)          # N arrow (up)
    d.polygon([(cx - 5, m + 6), (cx + 5, m + 6), (cx, m - 2)], fill=red)
    d.line([(m + 18, cy), (m, cy)], fill=red, width=3)          # E arrow (left)
    d.polygon([(m + 6, cy - 5), (m + 6, cy + 5), (m - 2, cy)], fill=red)

    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", max(12, w // 55))
    except Exception:
        font = ImageFont.load_default()
    d.text((cx + 6, m - 4), "N", fill=red, font=font)
    d.text((m + 2, cy - 20), "E", fill=red, font=font)

    lines = [
        f"{frame}",
        f"center RA  {fmt_ra(sol.get('center_ra_deg'))}",
        f"center Dec {fmt_dec(sol.get('center_dec_deg'))}",
        f"roll {sol.get('roll_deg')}°   mirrored {bool(sol.get('is_mirrored'))}"
        f"   FOV {sol.get('fov_deg')}°",
        f"applied: {applied}   (J2000/ICRS; North up, East left)",
    ]
    y = h - (len(lines) * (max(12, w // 55) + 4)) - 8
    for ln in lines:
        d.text((8, y), ln, fill=red, font=font)
        y += max(12, w // 55) + 4
    return img


# ----------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Reproject a debug-bundle frame to North-up for chart comparison.")
    ap.add_argument("bundle", help="debug bundle .zip")
    ap.add_argument("frame", nargs="?", default=None,
                    help="frame id (frame_03, frame_03_raw.png, 3); "
                         "default = first solved frame")
    ap.add_argument("-o", "--out", default=None, help="output PNG path")
    ap.add_argument("--invert", action="store_true",
                    help="rotate by +roll instead of -roll (sign check)")
    ap.add_argument("--no-flip", dest="flip", action="store_false",
                    help="do not apply the is_mirrored horizontal flip")
    ap.add_argument("--raw", action="store_true",
                    help="skip the contrast stretch")
    ap.add_argument("--no-annotate", dest="annotate", action="store_false",
                    help="do not draw compass / crosshair / caption")
    ap.add_argument("--list", action="store_true",
                    help="list frames and their solve summary, then exit")
    args = ap.parse_args(argv)

    if not os.path.exists(args.bundle):
        sys.exit(f"error: no such file: {args.bundle}")
    zf, frames = load_bundle(args.bundle)

    if args.list:
        names = sorted(n for n in zf.namelist() if n.endswith("_raw.png"))
        if not names:
            sys.exit("no frame_*_raw.png members in this bundle")
        for n in names:
            sol = (frames.get(n) or {}).get("solution") or {}
            if sol.get("solved"):
                print(f"{n}: solved  RA_center={sol.get('center_ra_deg')}  "
                      f"Dec_center={sol.get('center_dec_deg')}  "
                      f"roll={sol.get('roll_deg')}  mirrored={sol.get('is_mirrored')}")
            else:
                print(f"{n}: {sol.get('status', 'no solution')}")
        return 0

    name = resolve_frame_name(zf, frames, args.frame)
    if name is None:
        sys.exit("error: could not find a matching frame (try --list)")

    sol = (frames.get(name) or {}).get("solution") or {}
    roll = sol.get("roll_deg")
    if roll is None:
        sys.exit(f"error: {name} has no solve record with roll_deg in frames.json "
                 f"(unsolved frame, or a bundle predating the solve fields). "
                 f"Run with --list to see what's available.")
    if "center_ra_deg" not in sol or "is_mirrored" not in sol:
        print("warning: this bundle predates center_ra_deg/is_mirrored — assuming "
              "not mirrored; upgrade the device to v0.11.67+ for full metadata.",
              file=sys.stderr)
    is_mirrored = bool(sol.get("is_mirrored", False))

    gray = np.array(Image.open(io.BytesIO(zf.read(name))).convert("L"))
    if not args.raw:
        gray = asinh_stretch(gray)

    img = reproject(gray, float(roll), is_mirrored,
                    do_flip=args.flip, invert=args.invert)

    applied = []
    if is_mirrored and args.flip:
        applied.append("h-flip")
    applied.append(f"rotate {'+' if args.invert else '-'}{roll}°")
    applied_str = " then ".join(applied)

    if args.annotate:
        img = annotate(img, name, sol, applied_str)

    out = args.out or (os.path.splitext(os.path.basename(name))[0] + "_northup.png")
    img.save(out)
    print(f"wrote {out}  ({img.size[0]}x{img.size[1]})  [{applied_str}]")
    print(f"  center RA/Dec (J2000): {fmt_ra(sol.get('center_ra_deg'))}  "
          f"{fmt_dec(sol.get('center_dec_deg'))}")
    print("  North up, East left. If a star north of center sits BELOW the "
          "crosshair, re-run with --invert.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
