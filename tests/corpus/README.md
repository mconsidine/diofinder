# Regression Corpus Layout and Labeling Convention

This directory is the **regression corpus** for the diofinder replay harness
(`tests/replay_corpus.py`).  Drop PNG frames here to build a labeled dataset
that makes solver/extractor/preset changes measurable A/B instead of on-sky
anecdote.

---

## Where frames come from

The diofinder daemon saves frames automatically when:

- `save_failed_frames: true` — saves frames that produced TooFew / NoMatch
  status (label `failed_TooFew`, `failed_NoMatch`)
- `save_solved_frames: true` — saves successfully solved frames (label `solved`)

Saved frames live at `/var/lib/diofinder/captures/` on the Pi, named:

```
{YYYYMMDDTHHMMSS_mmm}_{label}.png
```

Copy them off the device with:

```bash
scp diofinder@diofinder.local:/var/lib/diofinder/captures/*.png tests/corpus/
```

Or into a labeled subdirectory:

```bash
scp diofinder@diofinder.local:/var/lib/diofinder/captures/\*solved\*.png \
    tests/corpus/clear_dark/
```

Frames are raw 8-bit greyscale, 960×760 (the exact bytes the solver receives).

---

## Labeling convention

The harness derives a label for each frame using this priority order:

### 1. Subdirectory name (recommended for curated corpora)

Organize frames into subdirectories whose names describe the sky condition or
capture scenario.  The subdirectory name becomes the label.

```
tests/corpus/
  clear_dark/          <- label: "clear_dark"
    20240601T220000_solved.png
    20240601T220030_solved.png
  moonlit/             <- label: "moonlit"
    20240615T235000_failed_NoMatch.png
  thin_cloud/          <- label: "thin_cloud"
    20240710T230000_failed_TooFew.png
  defocused/           <- label: "defocused"
    focus_test_1.png
```

**This is the preferred layout** because the label is independent of the
filename and survives renaming.

### 2. Filename suffix (auto-labeled from the daemon's naming convention)

A flat directory of files named `{ts}_{label}.png` works out of the box:

```
tests/corpus/
  20240601T220000_solved.png         <- label: "solved"
  20240601T221500_failed_NoMatch.png <- label: "failed_NoMatch"
  20240601T221800_failed_TooFew.png  <- label: "failed_TooFew"
```

The harness splits on the first `_` in the filename stem; everything after
becomes the label.

### 3. "unlabeled" fallback

Files with no underscore in the stem (e.g. `frame001.png`) get the label
`unlabeled`.

---

## Mixed layouts

A corpus directory can have both subdirectories and root-level PNGs.
Subdirectory files use the subdirectory name; root-level PNGs use the
filename-suffix rule.

---

## Suggested scenario categories

| Subdirectory name | Scenario |
|---|---|
| `clear_dark`       | Clear sky, dark site, nominal conditions |
| `moonlit`          | Full or near-full moon, elevated background |
| `thin_cloud`       | Patchy cloud, variable background |
| `heavy_cloud`      | Dense cloud, few or no stars expected |
| `defocused`        | Intentionally or accidentally out of focus |
| `gradient`         | Strong sky-glow gradient (light pollution) |
| `high_altitude`    | Near zenith, stellar density varies |
| `near_horizon`     | Low altitude, atmospheric extinction |
| `tracking_sequence`| Sequential frames for hint / tracking validation |

These are suggestions, not requirements.  Use whatever category names make
sense for your test campaign.

---

## Running the harness

```bash
# Full run on this corpus, both presets
python3 tests/replay_corpus.py \
    --corpus tests/corpus/ \
    --database /var/lib/diofinder/default_database.npz

# Quick run: first 20 frames, good preset only, write CSV
python3 tests/replay_corpus.py \
    --corpus tests/corpus/ \
    --database /var/lib/diofinder/default_database.npz \
    --presets good \
    --limit 20 \
    --csv /tmp/results.csv

# Sweep background modes across both presets
python3 tests/replay_corpus.py \
    --corpus tests/corpus/ \
    --database /var/lib/diofinder/default_database.npz \
    --bg-modes row_percentile,block_percentile,uniform_mean \
    --csv /tmp/bg_sweep.csv
```

See `tests/replay_corpus.py --help` for all flags.

---

## CSV output columns

| Column | Description |
|---|---|
| `frame`      | Absolute path to the PNG |
| `label`      | Derived label (subdir name, filename suffix, or "unlabeled") |
| `preset`     | Seeing preset name (`good` or `bad`) |
| `bg_mode`    | Background mode actually used |
| `n_stars`    | Extracted centroid count |
| `solved`     | `True`/`False` |
| `extract_ms` | Extraction wall-clock time in ms |
| `solve_ms`   | Solve wall-clock time in ms (0 if not attempted) |
| `ra`         | Right ascension in degrees (blank if not solved) |
| `dec`        | Declination in degrees (blank if not solved) |
| `fov`        | Solved FOV in degrees (blank if not solved) |
| `matches`    | Catalog star matches (blank if not solved) |

---

## Notes

- **No manifest database** — adding or removing frames requires only moving
  files into the right subdirectory.  The harness re-discovers on every run.
- Frames are always processed in sorted order within each subdirectory, so
  results are deterministic across runs with the same corpus and wheels.
- The `.gitignore` at the repo root excludes `*.npz` and large image files.
  PNGs in the corpus are not committed to git by default — they live only on
  your local machine or shared storage.
