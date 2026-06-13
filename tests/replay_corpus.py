#!/usr/bin/env python3
"""
replay_corpus.py — Off-device regression-corpus replay harness.

Runs the diofinder detect+solve pipeline on a directory of saved PNG frames,
sweeping over seeing presets (and optionally bg_modes), and reports solve rate,
star count, and timing per preset.

Usage:
  python3 tests/replay_corpus.py --corpus /path/to/corpus --database /path/to/db.npz
  python3 tests/replay_corpus.py --corpus /path/to/corpus --presets good,bad --csv out.csv
  python3 tests/replay_corpus.py --corpus /path/to/corpus --bg-modes row_percentile,block_percentile
  python3 tests/replay_corpus.py --corpus /path/to/corpus --limit 20

Corpus layout (documented in tests/corpus/README.md):
  corpus/frame.png              -> label "unlabeled"
  corpus/20240101T120000_solved.png -> label "solved" (from filename suffix)
  corpus/good_dark/frame.png    -> label "good_dark" (from subdirectory name)
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Graceful dependency checking — print a clear message and exit non-zero
# rather than spewing a traceback when wheels are missing.
# ---------------------------------------------------------------------------

def _die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _check_deps():
    """Import all required libraries; report exactly which are missing."""
    missing = []
    for pkg, install_hint in [
        ("numpy",      "pip install numpy"),
        ("PIL",        "pip install Pillow"),
    ]:
        try:
            __import__(pkg)
        except ImportError:
            missing.append((pkg, install_hint))

    if missing:
        lines = ["Required packages are not installed:"]
        for pkg, hint in missing:
            lines.append(f"  {pkg}  ->  {hint}")
        _die("\n".join(lines))


def _import_solver_libs():
    """Import star_detect and tetra3; return (star_detect_module, tetra3_module)
    or print a clear error and exit."""
    import_errors = []

    sd = None
    try:
        import star_detect as _sd
        sd = _sd
    except ImportError as e:
        import_errors.append(
            f"star_detect (sycamore): {e}\n"
            "  Install: build the sycamore-extract wheel for your platform and\n"
            "  copy it into your venv, or use the on-device wheel from the Pi.\n"
            "  See: sycamore-extract/scripts/build_cross.sh"
        )

    t3 = None
    try:
        import tetra3 as _t3
        t3 = _t3
    except ImportError as e:
        import_errors.append(
            f"tetra3 (olive-solve): {e}\n"
            "  Install: build the olive-solve wheel for your platform.\n"
            "  See: docs/scripts-and-tests-guide.md"
        )

    if import_errors:
        lines = [
            "Cannot run the detect+solve pipeline — solver wheels not installed:",
            "",
        ]
        lines += import_errors
        lines += [
            "",
            "To run a quick syntax/import check without the wheels:",
            "  python3 -m py_compile tests/replay_corpus.py",
            "  python3 tests/replay_corpus.py --help",
            "",
            "To run the full harness, install the wheels on a box that has them",
            "(the on-device Pi venv at /opt/efinder/venv/ works).",
        ]
        _die("\n".join(lines))

    return sd, t3


# ---------------------------------------------------------------------------
# Capability probing (mirrors efinder/bg_cache.py exactly)
# ---------------------------------------------------------------------------

def _probe_capabilities(sd) -> Dict[str, bool]:
    """Return a dict of sycamore capability flags.  Mirrors bg_cache.py so
    the harness degrades identically to the daemon on older wheels."""
    import inspect

    def _supports(fn_name: str, param: str) -> bool:
        fn = getattr(sd, fn_name, None)
        if fn is None:
            return False
        try:
            return param in inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return param != "tophat_radius"

    return {
        "has_tophat":         _supports("detect_stars", "tophat_radius"),
        "has_kernel_sigma":   _supports("detect_stars", "kernel_sigma"),
        "has_local_noise":    _supports("detect_stars", "local_noise"),
        "has_noise_mode":     _supports("detect_stars", "noise_mode"),
        "has_bg_block_size":  _supports("detect_stars", "bg_block_size"),
        "has_uniform_filter": _supports("detect_stars", "uniform_filter_size"),
    }


# ---------------------------------------------------------------------------
# Corpus discovery
# ---------------------------------------------------------------------------

def _label_from_filename(path: Path) -> str:
    """Parse label from {utc-timestamp}_{label}.png filename convention.
    Falls back to 'unlabeled'."""
    stem = path.stem  # e.g. "20240101T120000_solved" or "myframe"
    if "_" in stem:
        # Label is everything after the first underscore
        return stem.split("_", 1)[1]
    return "unlabeled"


def _discover_frames(corpus_dir: Path, limit: Optional[int]) -> List[Tuple[Path, str]]:
    """Walk corpus_dir and return [(path, label), ...].

    Label priority:
      1. Parent subdirectory name, if corpus_dir contains subdirectories
         (e.g. corpus/good_dark/frame.png -> label "good_dark").
      2. Filename suffix after the first underscore
         (e.g. 20240101_solved.png -> label "solved").
      3. "unlabeled".

    Only .png files are collected.  Files in the corpus_dir root (no subdir)
    use rule 2/3; files in immediate subdirectories use rule 1.
    """
    frames: List[Tuple[Path, str]] = []

    corpus_dir = corpus_dir.resolve()
    if not corpus_dir.exists():
        _die(f"Corpus directory does not exist: {corpus_dir}")
    if not corpus_dir.is_dir():
        _die(f"--corpus must be a directory: {corpus_dir}")

    # Check whether there are any immediate subdirectories containing PNGs.
    # If so, treat subdir names as labels (rule 1).  Otherwise treat all PNGs
    # in the flat directory using rule 2/3.
    subdirs_with_pngs = [
        p for p in sorted(corpus_dir.iterdir())
        if p.is_dir() and any(p.glob("*.png"))
    ]
    root_pngs = sorted(corpus_dir.glob("*.png"))

    if subdirs_with_pngs:
        # Labeled subdir layout: collect from subdirs first, then any
        # root-level PNGs under rule 2/3.
        for subdir in sorted(subdirs_with_pngs):
            for png in sorted(subdir.glob("*.png")):
                frames.append((png, subdir.name))
    # Also collect root-level PNGs (flat or mixed layout)
    for png in root_pngs:
        frames.append((png, _label_from_filename(png)))

    if not frames:
        _die(
            f"No PNG files found under {corpus_dir}.\n"
            "  Expected either:\n"
            "    corpus/{label}/frame.png  (labeled subdirectory layout)\n"
            "    corpus/{ts}_{label}.png   (flat filename layout)\n"
            "  See tests/corpus/README.md for the full labeling convention."
        )

    if limit and limit > 0:
        frames = frames[:limit]

    return frames


# ---------------------------------------------------------------------------
# Preset resolution
# ---------------------------------------------------------------------------

def _resolve_presets(preset_names: List[str]) -> Dict[str, Dict[str, Any]]:
    """Return {name: preset_dict} from SEEING_PRESETS for the requested names.

    Imports efinder.seeing.SEEING_PRESETS so the harness always stays in sync
    with the daemon's preset table.  Falls back to an embedded copy if the
    efinder package is not installed (dev-box use)."""
    try:
        import sys as _sys
        # Add the repo root to sys.path if efinder is not installed globally
        _repo = Path(__file__).resolve().parent.parent
        if str(_repo) not in _sys.path:
            _sys.path.insert(0, str(_repo))
        from efinder.seeing import SEEING_PRESETS
    except ImportError:
        # Last-resort fallback: hard-coded copy of the preset table.
        # This will drift — the canonical source is efinder/seeing.py.
        SEEING_PRESETS = {
            "good": dict(
                detect_sigma=5.0,
                detect_kernel_sigma=1.5,
                detect_bg_mode="row_percentile",
                detect_max_axis_ratio=3.0,
                min_centroids=8,
                match_radius=0.01,
                match_threshold=1e-5,
                solve_timeout_ms=1500,
                auto_exposure_target_stars=20,
                auto_exposure_max_s=0.5,
                star_db="standard",
            ),
            "bad": dict(
                detect_sigma=4.0,
                detect_kernel_sigma=2.5,
                detect_bg_mode="block_percentile",
                detect_max_axis_ratio=5.0,
                min_centroids=5,
                match_radius=0.015,
                match_threshold=1e-5,
                solve_timeout_ms=3000,
                auto_exposure_target_stars=15,
                auto_exposure_max_s=1.0,
                star_db="deep",
            ),
        }
        import warnings
        warnings.warn(
            "efinder package not found; using built-in preset table copy.\n"
            "  Install the efinder package or run from the repo root so that\n"
            "  efinder/seeing.py is importable.",
            stacklevel=2,
        )

    unknown = [n for n in preset_names if n not in SEEING_PRESETS]
    if unknown:
        _die(
            f"Unknown preset(s): {unknown}\n"
            f"  Available: {sorted(SEEING_PRESETS.keys())}"
        )

    return {name: dict(SEEING_PRESETS[name]) for name in preset_names}


# ---------------------------------------------------------------------------
# Config defaults (mirrors efinder/config.py — used when preset lacks a key)
# ---------------------------------------------------------------------------

_CFG_DEFAULTS = dict(
    fov_deg=13.5,
    fov_max_error_deg=1.0,
    detect_bin=2,
    detect_tophat_radius=12,
    detect_bg_block_size=0,
    detect_uniform_filter_size=0,
    detect_noise_mode="mad",
    frame_width=960,
    frame_height=760,
    distortion=0.0,
    max_solve_stars=50,
)


def _cfg(preset: Dict[str, Any], key: str) -> Any:
    """Look up a key from preset first, then from _CFG_DEFAULTS."""
    return preset.get(key, _CFG_DEFAULTS.get(key))


# ---------------------------------------------------------------------------
# Per-frame detect + solve
# ---------------------------------------------------------------------------

def _detect(sd, image_u8, preset: Dict[str, Any], bg_mode_override: Optional[str],
            caps: Dict[str, bool]) -> Tuple[List, float]:
    """Run star_detect.detect_stars on image_u8 with settings from preset.

    Returns (centroids, extract_ms).
    centroids: list of (x, y, brightness, peak) in (x,y)-origin-at-top-left coords.
    """
    import numpy as np

    bg_mode = bg_mode_override or _cfg(preset, "detect_bg_mode") or "row_percentile"
    sigma   = float(_cfg(preset, "detect_sigma") or 5.0)
    det_bin = int(_cfg(preset, "detect_bin") or 2)
    tophat_radius  = int(_cfg(preset, "detect_tophat_radius") or 12)
    bg_block_size  = int(_cfg(preset, "detect_bg_block_size") or 0)
    uniform_size   = int(_cfg(preset, "detect_uniform_filter_size") or 0)
    noise_mode     = str(_cfg(preset, "detect_noise_mode") or "mad")
    kernel_sigma   = float(_cfg(preset, "detect_kernel_sigma") or 1.5)
    local_noise    = bool(_cfg(preset, "detect_local_noise") if "detect_local_noise" in preset
                          else True)
    max_axis_ratio_raw = float(_cfg(preset, "detect_max_axis_ratio") or 0.0)
    max_axis_ratio = float("inf") if max_axis_ratio_raw == 0.0 else max_axis_ratio_raw

    # Handle top_hat fallback exactly as bg_cache.py does
    if bg_mode == "top_hat" and not caps["has_tophat"]:
        bg_mode = "line_median"

    kw: Dict[str, Any] = dict(
        sigma=sigma,
        bin=det_bin,
        centroid_full_res=True,
        bg_mode=bg_mode,
        max_axis_ratio=max_axis_ratio,
    )

    # top_hat needs its radius parameter
    if bg_mode == "top_hat" and caps["has_tophat"]:
        kw["tophat_radius"] = tophat_radius

    # block_percentile tile size
    if bg_mode == "block_percentile" and caps["has_bg_block_size"] and bg_block_size:
        kw["bg_block_size"] = bg_block_size

    # uniform_mean filter size
    if bg_mode == "uniform_mean" and caps["has_uniform_filter"] and uniform_size:
        kw["uniform_filter_size"] = uniform_size

    # noise_mode (non-default only)
    if caps["has_noise_mode"] and noise_mode and noise_mode != "mad":
        kw["noise_mode"] = noise_mode

    # sycamore >= 0.12 kwargs (capability-probed)
    if caps["has_kernel_sigma"]:
        kw["kernel_sigma"] = kernel_sigma
    if caps["has_local_noise"]:
        kw["local_noise"] = local_noise

    t0 = time.perf_counter()
    centroids = sd.detect_stars(image_u8, **kw)
    extract_ms = (time.perf_counter() - t0) * 1000.0

    return centroids, extract_ms


def _solve(t3_instance, centroids_xy, preset: Dict[str, Any],
           fov_deg: Optional[float]) -> Tuple[bool, float, Optional[Dict]]:
    """Plate-solve from (x,y) centroids.

    Swaps (x,y) -> (row,col) = (y,x) as float64 exactly as solver_proc.py does.
    Returns (solved: bool, solve_ms: float, result_dict_or_None).
    """
    import numpy as np

    # Swap x,y -> row,col (y,x) and cast to float64 (required by the Rust binding)
    if centroids_xy:
        cents = np.array([[c[1], c[0]] for c in centroids_xy], dtype=np.float64)
    else:
        cents = np.zeros((0, 2), dtype=np.float64)

    fov_est   = fov_deg if fov_deg is not None else float(_CFG_DEFAULTS["fov_deg"])
    fov_err   = float(_CFG_DEFAULTS["fov_max_error_deg"])
    timeout   = int(_cfg(preset, "solve_timeout_ms") or 1500)
    threshold = float(_cfg(preset, "match_threshold") or 1e-5)
    radius    = float(_cfg(preset, "match_radius") or 0.01)
    distortion = float(_CFG_DEFAULTS["distortion"])
    h = int(_CFG_DEFAULTS["frame_height"])
    w = int(_CFG_DEFAULTS["frame_width"])

    t0 = time.perf_counter()
    try:
        soln = t3_instance.solve_from_centroids(
            cents, (h, w),
            fov_estimate=fov_est,
            fov_max_error=fov_err,
            solve_timeout=timeout,
            match_threshold=threshold,
            match_radius=radius,
            distortion=distortion,
            return_matches=False,
        )
    except Exception as e:
        solve_ms = (time.perf_counter() - t0) * 1000.0
        return False, solve_ms, {"error": str(e)}
    solve_ms = (time.perf_counter() - t0) * 1000.0

    solved = bool(soln is not None and soln.get("RA") is not None)
    return solved, solve_ms, soln


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

def _percentile(values: List[float], p: float) -> Optional[float]:
    """Return the p-th percentile of values (0–100), or None if empty."""
    if not values:
        return None
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(len(s) * p / 100)))
    return s[idx]


def _format_stat(v: Optional[float]) -> str:
    if v is None:
        return "   N/A"
    return f"{v:6.1f}"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _print_table(rows: List[Dict[str, Any]], labels: List[str]) -> None:
    """Print aggregated results to stdout in a readable table."""
    # Group rows by (preset_name, bg_mode)
    from collections import defaultdict
    groups: Dict[Tuple[str, str], List[Dict]] = defaultdict(list)
    for r in rows:
        groups[(r["preset"], r["bg_mode"])].append(r)

    # Per-(preset,bg_mode) aggregate
    print()
    print("=" * 90)
    print("  RESULTS BY PRESET")
    print("=" * 90)
    header = (
        f"{'Preset':<14} {'BgMode':<22} {'Frames':>6} {'Solved':>6} "
        f"{'Rate%':>6} {'Stars p50':>9} {'Stars p90':>9} "
        f"{'ExtMs p50':>9} {'ExtMs p90':>9} "
        f"{'SlvMs p50':>9} {'SlvMs p90':>9}"
    )
    print(header)
    print("-" * 90)
    for (preset_name, bg_mode), group in sorted(groups.items()):
        n_frames = len(group)
        n_solved = sum(1 for r in group if r["solved"])
        rate = 100.0 * n_solved / n_frames if n_frames else 0.0
        star_counts  = [r["n_stars"]   for r in group]
        extract_mss  = [r["extract_ms"] for r in group]
        solve_mss    = [r["solve_ms"]   for r in group if r["solved"]]

        print(
            f"  {preset_name:<12} {bg_mode:<22} {n_frames:>6} {n_solved:>6} "
            f"{rate:>5.1f}% "
            f"{_format_stat(_percentile(star_counts, 50)):>9} "
            f"{_format_stat(_percentile(star_counts, 90)):>9} "
            f"{_format_stat(_percentile(extract_mss, 50)):>9} "
            f"{_format_stat(_percentile(extract_mss, 90)):>9} "
            f"{_format_stat(_percentile(solve_mss, 50)):>9} "
            f"{_format_stat(_percentile(solve_mss, 90)):>9}"
        )

    # Per-label breakdown (if more than one label present)
    unique_labels = sorted(set(r["label"] for r in rows))
    if len(unique_labels) > 1:
        print()
        print("=" * 90)
        print("  RESULTS BY LABEL  (across all presets)")
        print("=" * 90)
        lheader = (
            f"{'Label':<20} {'Preset':<14} {'BgMode':<22} "
            f"{'Frames':>6} {'Solved':>6} {'Rate%':>6}"
        )
        print(lheader)
        print("-" * 90)
        label_groups: Dict[Tuple[str, str, str], List[Dict]] = defaultdict(list)
        for r in rows:
            label_groups[(r["label"], r["preset"], r["bg_mode"])].append(r)
        for (label, preset_name, bg_mode), group in sorted(label_groups.items()):
            n_frames = len(group)
            n_solved = sum(1 for r in group if r["solved"])
            rate = 100.0 * n_solved / n_frames if n_frames else 0.0
            print(
                f"  {label:<18} {preset_name:<14} {bg_mode:<22} "
                f"{n_frames:>6} {n_solved:>6} {rate:>5.1f}%"
            )

    print()


def _write_csv(rows: List[Dict[str, Any]], csv_path: str) -> None:
    """Write per-frame rows to a CSV file."""
    fields = [
        "frame", "label", "preset", "bg_mode",
        "n_stars", "solved", "extract_ms", "solve_ms",
        "ra", "dec", "fov", "matches",
    ]
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"Per-frame CSV written to: {csv_path}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Replay-corpus harness: run the diofinder detect+solve pipeline "
            "on a directory of saved PNG frames and measure solve rate, star "
            "count, and timing per seeing preset."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 tests/replay_corpus.py --corpus /var/lib/efinder/captures \\
      --database /var/lib/efinder/default_database.npz

  python3 tests/replay_corpus.py --corpus corpus/ --presets good \\
      --bg-modes row_percentile,block_percentile --csv results.csv

  python3 tests/replay_corpus.py --corpus corpus/ --limit 30

Corpus layout:
  corpus/label_subdir/frame.png     (subdir name becomes label)
  corpus/{ts}_{label}.png           (filename suffix becomes label)
  corpus/frame.png                  (label = "unlabeled")
See tests/corpus/README.md for the full labeling convention.
        """,
    )
    parser.add_argument(
        "--corpus", required=True,
        help="Directory of PNG frames (flat or label-subdirs; recurses one level).",
    )
    parser.add_argument(
        "--database",
        default="/var/lib/efinder/default_database.npz",
        help="Path to tetra3 .npz solver database "
             "(default: /var/lib/efinder/default_database.npz).",
    )
    parser.add_argument(
        "--presets", default="good,bad",
        help="Comma-separated list of seeing presets to run "
             "(default: 'good,bad'). Must match keys in efinder.seeing.SEEING_PRESETS.",
    )
    parser.add_argument(
        "--bg-modes", default="",
        help=(
            "Optional comma-separated list of background modes to additionally "
            "sweep, overriding each preset's detect_bg_mode.  "
            "Example: --bg-modes row_percentile,block_percentile,uniform_mean"
        ),
    )
    parser.add_argument(
        "--csv",
        default="",
        help="Path for per-frame CSV output (optional).",
    )
    parser.add_argument(
        "--fov", type=float, default=None,
        help="FOV estimate in degrees (default: from config, 13.5).",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Cap number of frames for a quick run (0 = no limit).",
    )

    args = parser.parse_args()

    # Check basic deps (numpy, PIL) before doing anything
    _check_deps()

    import numpy as np
    from PIL import Image

    # Import solver libs — clear error if missing
    sd, t3_mod = _import_solver_libs()

    # Probe capabilities once
    caps = _probe_capabilities(sd)

    # Print version info so results are reproducible
    sd_version = getattr(sd, "__version__", None) or getattr(sd, "version", "unknown")
    t3_version = getattr(t3_mod, "__version__", None) or getattr(t3_mod, "version", "unknown")
    active_caps = [k for k, v in caps.items() if v]
    print(f"star_detect version : {sd_version}")
    print(f"tetra3 version      : {t3_version}")
    print(f"Active capabilities : {', '.join(active_caps) if active_caps else 'none'}")

    # Resolve presets
    preset_names = [p.strip() for p in args.presets.split(",") if p.strip()]
    if not preset_names:
        _die("--presets must be a non-empty comma-separated list.")
    presets = _resolve_presets(preset_names)

    # Optional bg_mode sweep
    bg_modes_sweep = [m.strip() for m in args.bg_modes.split(",") if m.strip()]

    # Discover frames
    corpus_path = Path(args.corpus)
    frames = _discover_frames(corpus_path, args.limit or None)
    print(f"Corpus              : {corpus_path} ({len(frames)} frames)")

    # Load database
    db_path = args.database
    if not os.path.exists(db_path):
        # Try expanding bare name as /var/lib/efinder/<name>.npz
        candidate = f"/var/lib/efinder/{db_path}.npz"
        if os.path.exists(candidate):
            db_path = candidate
        else:
            _die(
                f"Solver database not found: {args.database}\n"
                "  Pass --database /path/to/db.npz or copy a database from the Pi.\n"
                "  On the Pi, databases live in /var/lib/efinder/*.npz."
            )

    print(f"Database            : {db_path}")
    print(f"Presets             : {', '.join(preset_names)}")
    if bg_modes_sweep:
        print(f"BgMode sweep        : {', '.join(bg_modes_sweep)}")
    print()

    try:
        solver = t3_mod.Tetra3(db_path)
    except Exception as e:
        _die(
            f"Failed to load database {db_path}:\n  {e}\n"
            "  Ensure the file is a valid tetra3 .npz database."
        )

    # Build the list of (preset_name, bg_mode_override) combinations to run
    combos: List[Tuple[str, Optional[str]]] = []
    for pname in preset_names:
        if bg_modes_sweep:
            for bm in bg_modes_sweep:
                combos.append((pname, bm))
        else:
            combos.append((pname, None))

    # Run
    all_rows: List[Dict[str, Any]] = []
    total_combos = len(frames) * len(combos)
    done = 0

    for png_path, label in frames:
        try:
            img = Image.open(png_path).convert("L")
            image_u8 = np.asarray(img, dtype=np.uint8)
            if not image_u8.flags["C_CONTIGUOUS"]:
                image_u8 = np.ascontiguousarray(image_u8)
        except Exception as e:
            print(f"  SKIP {png_path.name}: could not load image: {e}")
            continue

        for pname, bg_override in combos:
            preset = presets[pname]
            effective_bg = bg_override or _cfg(preset, "detect_bg_mode") or "row_percentile"
            min_centroids = int(_cfg(preset, "min_centroids") or 8)

            try:
                centroids, extract_ms = _detect(sd, image_u8, preset, bg_override, caps)
            except Exception as e:
                print(f"  DETECT ERROR {png_path.name} preset={pname} bg={effective_bg}: {e}")
                all_rows.append(dict(
                    frame=str(png_path), label=label,
                    preset=pname, bg_mode=effective_bg,
                    n_stars=0, solved=False,
                    extract_ms=0.0, solve_ms=0.0,
                    ra=None, dec=None, fov=None, matches=0,
                ))
                done += 1
                continue

            n_stars = len(centroids)

            if n_stars < min_centroids:
                all_rows.append(dict(
                    frame=str(png_path), label=label,
                    preset=pname, bg_mode=effective_bg,
                    n_stars=n_stars, solved=False,
                    extract_ms=extract_ms, solve_ms=0.0,
                    ra=None, dec=None, fov=None, matches=0,
                ))
                done += 1
                continue

            # Cap centroid list exactly as solver_proc does
            max_stars = int(_CFG_DEFAULTS["max_solve_stars"])
            if n_stars > max_stars:
                # take brightest (sorted brightest-first by sycamore already)
                centroids = centroids[:max_stars]

            solved, solve_ms, soln = _solve(solver, centroids, preset, args.fov)

            ra  = soln.get("RA")  if soln and solved else None
            dec = soln.get("Dec") if soln and solved else None
            fov = soln.get("FOV") if soln and solved else None
            matches = int(soln.get("Matches", 0) or 0) if soln and solved else 0

            all_rows.append(dict(
                frame=str(png_path), label=label,
                preset=pname, bg_mode=effective_bg,
                n_stars=n_stars, solved=solved,
                extract_ms=extract_ms, solve_ms=solve_ms,
                ra=ra, dec=dec, fov=fov, matches=matches,
            ))
            done += 1

            status = "SOLVED" if solved else "FAILED"
            print(
                f"  [{done:>4}/{total_combos}] "
                f"{png_path.name:<35} label={label:<14} "
                f"preset={pname:<6} bg={effective_bg:<22} "
                f"stars={n_stars:>3} {status} "
                f"ext={extract_ms:5.1f}ms"
                + (f" slv={solve_ms:5.0f}ms" if solved else "")
            )

    # Summary table
    if all_rows:
        unique_labels = sorted(set(r["label"] for r in all_rows))
        _print_table(all_rows, unique_labels)

    # CSV output
    if args.csv:
        _write_csv(all_rows, args.csv)

    if not all_rows:
        print("No frames were processed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
