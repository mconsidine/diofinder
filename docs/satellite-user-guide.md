# Identifying satellites with diofinder: user guide

This guide covers one use of diofinder: recording satellites that cross the
finder's field, then identifying them, measuring them, reporting them,
fitting orbits and predicting the next passes. Everything else the finder
does (plate solving for SkySafari, the IMU, the mount link) is documented in
`CLAUDE.md` and `docs/`.

Contents

1. [How it fits together](#1-how-it-fits-together)
2. [What you need](#2-what-you-need)
3. [Setting up the finder](#3-setting-up-the-finder)
4. [Capturing](#4-capturing)
5. [Complete workflows](#5-complete-workflows)
6. [Command reference](#6-command-reference)
7. [Times and time zones](#7-times-and-time-zones)
8. [Element sources](#8-element-sources)
9. [Output files](#9-output-files)
10. [Accuracy and limits](#10-accuracy-and-limits)
11. [Troubleshooting](#11-troubleshooting)
12. [Library modules and automated tests](#12-library-modules-and-automated-tests)

---

## 1. How it fits together

```
 ON THE FINDER                          ON YOUR LAPTOP (off-device, tests/)
 ─────────────                          ─────────────────────────────────────
 Utilities → Clock   (set time)
 Home → Start/Stop live capture ──►  livecap_<UTC>.zip
 Utilities → Live captures (download)      │
                                           ├─► satcheck_archive.py   quick SatChecker listing
                                           ├─► satcheck_overlay.py   predictions drawn on frames, stacks, trails
                                           ├─► sat_identify.py       identify, measure, report  ──► observations.json
                                           │                                                        │
                                           └─► iss_check.py          ISS ground-truth check        ▼
                                                                              sat_orbit.py fit  ──► fit.json
 element files (classfd.zip, inttles.zip, CelesTrak, PreviSat...) ──────►   sat_orbit.py predict / ephem
```

- **The finder records; the laptop analyses.** The finder writes every frame,
  its exact UTC exposure time, its plate solution and the stars it matched.
  All identification work happens afterwards on a computer.
- **SatChecker** (IAU CPS, online) knows the public catalog. **Element files**
  you supply also cover what SatChecker doesn't: Mike McCants' classified
  (`classfd`) and integrated (`inttles`) files, CelesTrak, or a PreviSat folder.
- **Old element sets still identify objects.** An orbit's plane changes
  slowly even when its timing has drifted by minutes, so each trail is
  matched by its line, direction and rate, with the along-track time left
  free. The report then says, for example, "41 s early".

---

## 2. What you need

**Finder software.** A version with live capture (v0.11.76 base or later).
To update over the air from the finder's console:

```
sudo /usr/local/bin/diofinder-update --ref claude/azimuth-on-0.11.74
```

Captures made before this update have no tone-curve record. Their
magnitudes are computed assuming the shipped `imx477_finder.json` tone curve.

**Laptop.** Python 3.10 or later, with a copy of the diofinder repository.
Run every command from the repository root; on Windows that is the PyCharm
Terminal (Alt+F12).

```
pip install numpy astropy matplotlib pillow sgp4
```

- On **Windows**, also run `pip install tzdata` if you want to use time-zone
  names such as `America/New_York`.
- Plain offsets such as `UTC-5`, and `local` / `utc`, work without it.
- scipy is not needed.

**Element files** (optional but recommended): save
<https://mmccants.org/tles/classfd.zip> and
<https://mmccants.org/tles/inttles.zip>, or let `sat_identify.py --download`
fetch them. CelesTrak `gp.php` URLs can be given directly (section 8).

**Internet**, only for:
- SatChecker queries (`satcheck_archive`, `satcheck_overlay`,
  `sat_identify --satchecker`);
- `--download` and element URLs;
- `iss_check` with its default CelesTrak source.

Everything else works offline.

**Command syntax.** The examples use Windows PowerShell paths (`tests\...`).
On Linux or macOS write `python3 tests/...` with forward slashes. PowerShell
has no line continuation, so each command is one line.

---

## 3. Setting up the finder

Do these once (site) or before each session (clock).

1. **Site.** Config page → *Observer Location*:
   - *Latitude* (+N)
   - *Longitude* (**+E**; west longitudes are negative, e.g. −72.667)
   - *Elevation (m)*

   The site is stored in every capture and every calculation uses it. If it
   is unset (0, 0), the Home page warns when you start a capture.
2. **Clock.** Utilities → *Clock* → **Set finder time from this device**.
   - The finder has no clock battery.
   - A 1 s clock error moves a low satellite about 0.5° along its track, so
     the clock is the main limit on accuracy along the track.
   - The capture records where its time came from, and the reports use that
     record:

   | Time source | Uncertainty used |
   |---|---|
   | Network time (NTP) | 0.05 s |
   | Phone, set this boot | 0.2 s |
   | SkySafari (whole seconds) | 1 s |
   | Unknown | 10 s, plus a "do not post" warning |
3. **Detection.** Keep trail rejection off (`detect_max_axis_ratio` 0, the
   default): trailed stars must still be detected while the scope moves.
4. **Exposure.** Short exposures (0.2–0.3 s) keep a fast satellite's dash
   inside the frame and give more points per pass. Use 0.1–0.3 s when
   hand-tracking.
5. **Capture limits** (Config page, restart to change):

   | Setting | Default | Meaning |
   |---|---|---|
   | `live_capture_max_minutes` | 60 | A capture stops by itself after this long (0 = no limit) |
   | `live_capture_max_mb` | 4000 | A capture stops at this size |
   | `live_capture_min_free_mb` | 1000 | A capture stops when free space falls below this |
   | `live_capture_keep_mb` | 8000 | Oldest finished captures are deleted to stay under this (the newest is always kept) |

   A frame is about 200 KB.

---

## 4. Capturing

1. Point the finder where the satellite will pass (section 5.1 tells you
   when and where), and check that it solves.
2. Home page → **Start live capture**. Press again to stop.
   - The recording becomes `livecap_<UTC start>.zip`.
   - Dark frames (no signal) are never recorded.
   - If the finder can't write frames fast enough, frames are dropped
     (and counted), never delayed.
3. Utilities → **Live captures** → download the archive to the laptop. Old
   archives can be deleted there too.

Three kinds of capture are useful:

| Kind | How | Best for |
|---|---|---|
| **Fixed field** | Scope still; the satellite crosses the field as dashes, one per exposure | Most passes; the stacking tools find these trails |
| **Hand-tracked** | Follow the satellite by hand so it stays near the centre while the stars stream | Long arcs (many degrees), better orbits. **Not yet tested on sky**; frames that don't plate-solve are left out |
| **ISS pass** | Fixed field or tracked, ISS sunlit, ≥ 30° up, in darkness | The ground-truth check (5.4) |

Capturing **two passes of the same object** a few hours apart, or on
consecutive nights, gives a full six-element orbit (5.6).

---

## 5. Complete workflows

The examples assume archives in the repository folder and element files in
`C:\Users\matt\Downloads\TLEs\`. Use your own paths and site.

### 5.1 Plan tonight: what will pass, and where?

All objects in two element files, tonight from 17:00 to 06:00 local time,
at least 25° up. Times are printed in UTC, ready to share:

```
python tests\sat_orbit.py predict C:\Users\matt\Downloads\TLEs\classfd.zip,C:\Users\matt\Downloads\TLEs\inttles.zip --lat 43.95 --lon -72.667 --elev 330 --start 17:00 --end 06:00 --min-elevation 25 --out plan
```

Variations:

- `--out-tz local`: read the times in local time instead.
- `--sky east`: only passes wholly in the eastern sky, e.g. when buildings
  hide the west. Other values are `west`, `north`, `south`, `ne`, `nw`, `se`,
  `sw`, or a comma list such as `east,north`. Add `--sky-mode part` to keep
  the part of each pass that is in that sky.
- `--name NOSS` or `--norad 37386,37391`: only some objects.
- `--tables`: a pointing table (alt/az/RA/Dec every `--step` s) under each
  pass.
- `--max-age-days 0`: include element sets older than 30 days, which are
  skipped by default (section 10).
- `--all-orbits`: include geostationary and other objects under 2 rev/day.

One object, from a CelesTrak URL. Quote the URL in PowerShell:

```
python tests\sat_orbit.py predict "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE" --lat 43.95 --lon -72.667 --elev 330 --start 17:00 --hours 48 --min-elevation 25 --out plan
```

Output:
- the console listing, also saved as `plan\predictions.txt`;
- `plan\predictions.csv` (spreadsheet);
- `plan\predictions.json` (UTC, all details).

The console first prints:
- the window it understood;
- when the sky is dark at the site (Sun below `--sun-alt`, −6° by default);
- for whole files, how many slow or old element sets it skipped.

### 5.2 Quick look: what did SatChecker expect in my capture?

```
python tests\satcheck_archive.py livecap_20261102T020749Z.zip --group 30 --out hits.json
python tests\satcheck_overlay.py livecap_20261102T020749Z.zip --stack --chart
```

- `satcheck_archive` lists, per frame or group, the public satellites that
  crossed the field. Run it with `--dry-run` first to see the queries
  without sending them.
- `satcheck_overlay` writes its results to `livecap_..._sats\`:
  - Each frame with a satellite in it, with the predicted streak drawn using
    the frame's own WCS (fitted to its matched stars).
  - With `--stack`, star-aligned stacks of each query group, in which a
    satellite is a dashed line. Trails no prediction explains are outlined
    red; strobe-like ones are flagged "flashing → aircraft?".
  - `trails.csv`, `report.csv`.
  - With `--chart`, sky charts.
  - `satchecker_responses.json`. Re-runs reuse it with `--responses ...`;
    add `--offline` to send no queries at all.
- `--window 600` stacks ten-minute stretches (default 60 s).

### 5.3 Identify everything and write reports

```
python tests\sat_identify.py livecap_20261102T020749Z.zip --elements C:\Users\matt\Downloads\TLEs\classfd.zip,C:\Users\matt\Downloads\TLEs\inttles.zip --station 9999 --status G --window 600
```

Add the SatChecker answers from 5.2, so that public objects are identified
too:

```
python tests\sat_identify.py livecap_20261102T020749Z.zip --elements C:\Users\matt\Downloads\TLEs\classfd.zip,C:\Users\matt\Downloads\TLEs\inttles.zip --responses livecap_20261102T020749Z_sats\satchecker_responses.json --station 9999
```

Results go in `livecap_..._ident\`. Read **`identification.txt`** first. For
each track it shows:
- the best candidates: name, catalog number, element-set age, "N s
  early/late", rms, cross-track offset, rate and direction agreement, sunlit
  or not;
- the magnitude ± and the flash period;
- the IOD behaviour code: S steady, F flashing with a regular period, R
  regular variation, I irregular, X irregular flashes;
- the refined elements.

An unidentified, strobing track carries **"CHECK: may be an AIRCRAFT"**.

**Before posting to SeeSat-L:**

1. Use your real IOD station number (`--station`; 9999 is a placeholder).
2. Describe the sky: `--status E|G|F|P|B|T`, `--conditions "limiting mag 5, light haze"`.
3. Describe your equipment: `--equipment "diofinder, 25 mm lens, IMX477"`.
4. Check the clock line. If the time source was unknown, don't post.
5. Review `seesat_post.txt`, the draft message. It holds a header, the IOD
   lines, the object's name (or `unkYYMMDD`) and the refined TLE.

Other reports:
- `observations_iod.txt`: the IOD lines alone. `--points-per-pass` sets how
  many per track (4; 0 = every frame); `--no-iod-mag` leaves out magnitudes.
- `observations_rde.txt`: R.D.E. format.
- Refined elements: `refined.tle`, `refined_omm.kvn/.xml/.json/.csv`.

### 5.4 Ground truth: check the whole chain on an ISS pass

```
python tests\iss_check.py livecap_<ISS pass>.zip
```

The truth is CelesTrak's current ISS elements, downloaded and cached for
2 h. To use saved elements instead: `--tle iss.tle`. `iss_report.txt` gives:

1. **Astrometry**: across-track rms. About 1 px (0.014°) is expected.
2. **Timing**: the along-track offset in seconds, which is your finder's
   clock error. It says "BEHIND" or "AHEAD".
3. **Orbit from this pass alone**: a fit made without the ISS elements,
   compared with them. It gives period, inclination, node, km off at +1
   orbit / +6 h / +24 h, and next-pass timing.
4. **Photometry**: the measured magnitude against a rough prediction. The
   ISS often saturates, so the result is a bright limit.

Use elements from within a day or two of the pass: reboosts move the ISS.
`--out-tz local` gives the report's times in local time.

### 5.5 A hand-tracked pass

Capture as in section 4 (short exposures, steady push), then run 5.3
unchanged. Frame-by-frame tracks are found automatically, and a long linked
track replaces the stack trails it continues.
- `--link-interp`: lets frames that didn't solve use an interpolated WCS.
- `--no-link`: uses only the stack trails.

This mode has been tested only on simulated captures; solving while panning
by hand is unproven.

### 5.6 An unknown object: orbit from one pass, then two

1. **Identify and measure** (5.3). The unidentified track is in
   `observations.json`, e.g. track `L1`.
2. **One-pass orbit** (circular, four elements, honest about its timing):

   ```
   python tests\sat_orbit.py fit livecap_A_ident\observations.json --track L1 --name UNK261104
   ```

   This writes `livecap_A_ident\orbit\`: `fit.json` (with covariance),
   `fit.tle`, `fit_omm.json/.xml/.kvn`.
3. **When will it come back?**

   ```
   python tests\sat_orbit.py predict livecap_A_ident\orbit\fit.json --site-from livecap_A_ident\observations.json --start 17:00 --hours 36 --min-elevation 20
   ```

   Each pass is given with a timing ± (1 σ). Beyond about 1/6 of a
   revolution, a one-pass orbit marks passes **"timing UNKNOWN"**: the path
   across the sky is a rough guide, but the time is not. Be pointed early.
4. **Capture the second pass**, identify it (5.3), then fit both passes. All
   six elements are fitted and the revolution count between them is
   resolved:

   ```
   python tests\sat_orbit.py fit livecap_A_ident\observations.json livecap_B_ident\observations.json --track livecap_A_ident\observations.json:L1 livecap_B_ident\observations.json:L1 --name UNK261104
   ```

   When both passes use the same track id, `--track L1` alone also works.
   Timing is now ±1 s or so, and positions are good to a few km for days.
   Predict as in step 3 from the new `fit.json`.

If the object is identified but its elements are stale, refine them:
`sat_orbit.py fit ... --start classfd.zip --norad 37386`. With one pass, only
the node and mean longitude are fitted.

### 5.7 Export an ephemeris (CCSDS OEM)

State vectors for other software (GMAT, STK, Orekit...):

```
python tests\sat_orbit.py ephem livecap_A_ident\orbit\fit.json --start 17:00 --hours 12 --frame GCRF --out plan
python tests\sat_orbit.py ephem C:\Users\matt\Downloads\TLEs\classfd.zip --norad 37386,37391 --start 17:00 --hours 12 --xml --out plan
```

`predict` also reads an OEM, in either format (KVN `.oem` or XML), and lists
passes only inside the span it covers:

```
python tests\sat_orbit.py predict plan\ephemeris.oem --lat 43.95 --lon -72.667 --start 17:00 --hours 12
```

---

## 6. Command reference

All tools live in `tests\`. `python tests\<tool>.py --help` prints the same
options.

### 6.1 `satcheck_archive.py ARCHIVE`

Lists the public satellites SatChecker predicts in each frame or group.
Standard library only; needs internet.

| Option | Default | Meaning |
|---|---|---|
| `--dry-run` | | Print the query URLs only |
| `--group N` | 1 | One query spans N consecutive frames (fewer calls) |
| `--every N` | 1 | Query only every Nth frame or group |
| `--lat` / `--lon` / `--elevation` | from archive | Override the site (longitude **East-positive**) |
| `--radius DEG` | half-diagonal | Field radius |
| `--sleep S` | 1.0 | Pause between requests (be polite) |
| `--out FILE` | | Write every query and response (JSON) |
| `--base-url URL` | satchecker.cps.iau.org | Another SatChecker server |

### 6.2 `satcheck_overlay.py ARCHIVE`

Draws SatChecker predictions on the frames, stacks frames, and finds trails.

| Option | Default | Meaning |
|---|---|---|
| `--out DIR` | `<archive>_sats` | Output folder |
| `--frames LIST` | all | 1-based frame numbers, e.g. `5,10-20` |
| `--window S` | 60 | Most seconds of frames per query and stack group (600 for long stacks) |
| `--responses FILE` | `<out>\satchecker_responses.json` | Read/write the SatChecker answers |
| `--offline` | | Never query; use only `--responses` |
| `--all-frames` | | Draw every frame, not only those with a satellite |
| `--chart` | | Also draw sky charts |
| `--stack` | | Star-aligned stacks (satellites become dashed lines), trail detection, `trails.csv` |
| `--show-unlit` | hidden | Also show satellites in Earth's shadow |
| `--show-stationary` | hidden | Also show near-stationary objects |
| `--min-rate DEG/S` | 0.02 | Below this an object counts as stationary |
| `--sat-frame icrs\|jnow` | icrs | Try `jnow` if every overlay is offset by the same ~0.3° |
| `--flip-roll` | | Only for old captures without matched stars: roll-convention check |
| `--base-url` / `--sleep` | | As in 6.1 |

### 6.3 `sat_identify.py ARCHIVE`

The main tool: identify, refine, measure, report.

| Option | Default | Meaning |
|---|---|---|
| `--elements SRC ...` | | Element sources (section 8); several, separated by spaces or commas |
| `--download [NAME\|URL ...]` | classfd inttles | Download element files first |
| `--cache DIR` | `<out>\elements` | Where downloads go |
| `--out DIR` | `<archive>_ident` | Output folder |
| `--window S` | 600 | Seconds of frames stacked together |
| `--responses FILE` | | SatChecker answers from `satcheck_overlay` |
| `--satchecker` | | Also query SatChecker for groups not in `--responses` |
| `--max-offset S` | 600 | Largest element-set timing error searched |
| `--max-cross DEG` | 1.0 | Largest cross-track offset accepted |
| `--readout-ms MS` | 0 | Rolling-shutter readout time of a frame |
| `--station NNNN` | 9999 | Your IOD station number (9999 = placeholder) |
| `--status E\|G\|F\|P\|B\|T` | G | IOD sky condition |
| `--conditions TEXT` | | Conditions line for `seesat_post.txt` |
| `--equipment TEXT` | | Equipment line for `seesat_post.txt` |
| `--time-standard N` | 1 | R.D.E. time standard code |
| `--points-per-pass N` | 4 | IOD lines per track (0 = every frame) |
| `--time-unc S` | from clock record | Override the time uncertainty |
| `--unknown-desig D` | 99099A | Designator for unidentified objects (`''` = blank) |
| `--no-iod-mag` | | Leave magnitude and flash period out of the IOD lines |
| `--out-tz ZONE` | utc | Zone for `identification.txt`, light curves, console |
| `--no-photometry` | | Skip magnitudes and light curves |
| `--no-link` | | Stack trails only (no frame-by-frame tracks) |
| `--link-interp` | | Frame-by-frame tracks may use unsolved frames' interpolated WCS |

### 6.4 `iss_check.py ARCHIVE`

The ISS ground-truth check. Internet is needed for the default element
source.

| Option | Default | Meaning |
|---|---|---|
| `--tle SRC ...` | CelesTrak ISS URL | ISS element source(s) (section 8) |
| `--out DIR` | `<archive>_iss` | Output folder |
| `--station NNNN` | 9999 | IOD station number |
| `--readout-ms MS` | 0 | Rolling-shutter readout time |
| `--hours H` | 24 | How far ahead to compare predicted passes |
| `--min-elevation DEG` | 20 | For that comparison |
| `--out-tz ZONE` | utc | Zone of the times in `iss_report.txt` |

### 6.5 `sat_orbit.py fit OBSERVATIONS.json ...`

Fits an orbit to one or more passes.

| Option | Default | Meaning |
|---|---|---|
| `--track ID ...` | all | Track ids (`L1`, `S2.1`) or `FILE:ID` when several files |
| `--start SRC` | | Begin from a known element set (any source; `--norad` picks one) |
| `--norad N` | | Catalog number (selects in `--start`; written out) |
| `--name TEXT` | identified, else `unkYYMMDD` | Object name |
| `--intldesg D` | identified, else 99099A | International designator |
| `--circular` / `--full` | automatic | Force 4 (circular) or all 6 elements |
| `--out DIR` | `<first obs folder>\orbit` | Output folder |

One pass gives a circular fit; two or more passes give all six elements.

### 6.6 `sat_orbit.py predict ELEMENTS`

`ELEMENTS` is a `fit.json`, a CCSDS OEM, or any element source (section 8).

| Option | Default | Meaning |
|---|---|---|
| `--lat` / `--lon` / `--elev` | | Site (longitude **East-positive**); or |
| `--site-from OBS.json` | | the site of an `observations.json` |
| `--start T` | now | Window start, **local time** unless `--utc` (section 7) |
| `--end T` | | Window end (replaces `--hours`) |
| `--hours H` | 24 | Window length |
| `--in-tz ZONE` | local | Zone of `--start`/`--end` |
| `--utc` | | Same as `--in-tz utc` |
| `--out-tz ZONE` | utc | Zone of printed / txt / csv times |
| `--min-elevation DEG` | 20 | Lowest altitude counted |
| `--sun-alt DEG` | −6 | The sky is dark with the Sun below this (−12 darker, −3 brighter twilight) |
| `--sky REGION` | all | `east`, `west`, `north`, `south`, `ne`/`nw`/`se`/`sw`, or a comma list |
| `--sky-mode whole\|part` | whole | Keep passes wholly in `--sky`, or cut them to it |
| `--norad LIST` | all | Only these catalog numbers |
| `--name TEXT` | all | Only names containing this |
| `--max-age-days D` | 30 | Skip element sets further than D days from the window (0 = keep all; one object: a warning instead) |
| `--all-orbits` | | Include objects under 2 rev/day (GEO, high orbits) |
| `--tables` | | Pointing tables under each pass in a multi-object listing |
| `--step S` | 30 | Pointing-table step |
| `--out DIR` | | Also write `predictions.txt/.json/.csv` |

Only passes that can be seen are listed: the satellite is sunlit, the site
is dark, and the satellite is above `--min-elevation`.

### 6.7 `sat_orbit.py ephem ELEMENTS`

Writes a CCSDS OEM ephemeris.

| Option | Default | Meaning |
|---|---|---|
| `--norad` / `--name` | | Select objects (at most 100 per file) |
| `--start` / `--end` / `--hours` / `--in-tz` / `--utc` | | Time window (as `predict`) |
| `--step S` | 60 | Seconds between state vectors |
| `--frame F` | TEME | `TEME` (SGP4's own), `GCRF` / `EME2000` / `ICRF` (inertial J2000 axes), `ITRF` (Earth-fixed) |
| `--xml` | KVN text | OEM XML instead |
| `--out FILE\|DIR` | `ephemeris.oem` | Output file, or a folder (no extension) |

---

## 7. Times and time zones

- **Typed times are local** (this computer's zone) unless:
  - you add `--utc` or `--in-tz ZONE`, or
  - the time itself carries `Z` or an offset (`2026-11-04T23:00Z`,
    `2026-11-04T18:00-05:00`).
- **Accepted forms:** `2026-11-04T18:00`, `"2026-11-04 18:00"`, or just
  `18:00` (today).
- **`--end`:** a bare `HH:MM` counts from the start's date, and an end
  earlier than the start means the next morning. So
  `--start 17:00 --end 06:00` is tonight.
- **Printed times are UTC by default,** so reports can be shared as they are.
  `--out-tz local`, `--out-tz UTC-5` or `--out-tz America/New_York` change
  that. Every time is printed with its zone (`2026-11-03 04:04:40 EST`).
- **Always UTC:** IOD, R.D.E., the SeeSat-L draft, TLE/OMM, OEM, and all
  JSON files.
- **Negative offsets:** write `UTC-5`, or `--in-tz=-05:00` with the `=`. A
  bare `-05:00` is mistaken for an option.

---

## 8. Element sources

Every option that takes elements (`--elements`, `--start`, `--tle`, the
`predict` / `ephem` argument) accepts any of:

- **Files:** TLE / 2LE / 3LE; CCSDS OMM in XML, KVN, JSON or CSV (CelesTrak
  GP and Space-Track forms); PreviSat `.gp` files.
- **Archives:** `.zip` or `.gz` of those files.
- **Folders:** walked, including sub-folders.
- **URLs:** downloaded and cached for 2 h in `~\.diofinder_elements`, as
  CelesTrak asks.
  - Offline, an older cached copy is used, with a warning.
  - A reply with no element sets is never cached.
  - In PowerShell, **quote URLs** that contain `&`.
- **Comma-separated lists** of any of these:
  `classfd.zip,inttles.zip,C:\TLEs\extra\`.

When the same catalog number appears more than once, **the newest epoch
wins**. CCSDS OEM files are not element sets; `predict` reads them directly.

---

## 9. Output files

| Tool | File | Contents |
|---|---|---|
| finder | `livecap_<UTC>.zip` | `session.json` (site, field, clock record, tone curve), `frames.jsonl` (per frame: exposure UTC/JD, gain, solved RA/Dec, roll, matched stars), `frame_NNNNNN.png` |
| satcheck_archive | `--out` JSON | Every query and SatChecker response |
| satcheck_overlay | `frame_NNNNNN_sats.png` | Frame with predicted streaks |
| | `stack_<n>_moving.png`, `stack_<n>.png` | Stacks: moving objects only / with stars |
| | `chart_<n>.png` | Sky chart (`--chart`) |
| | `report.csv`, `trails.csv` | Per (frame, satellite) scores; detected trails and their matches |
| | `satchecker_responses.json` | Answers, reusable with `--responses` |
| sat_identify | `identification.txt/.json` | Candidates and results per track |
| | `observations_iod.txt`, `observations_rde.txt` | Reports in IOD and R.D.E. formats |
| | `seesat_post.txt` | Draft SeeSat-L message |
| | `observations.json` | Positions with sigmas, site, clock (input to `sat_orbit fit`) |
| | `lightcurve_<track>.csv/.png` | Magnitude per frame / along the dashes |
| | `refined.tle`, `refined_omm.kvn/.xml/.json/.csv` | Refined elements |
| iss_check | `iss_report.txt/.json`, `iss_residuals.png`, `iss_sky.png` | Plus all sat_identify outputs |
| sat_orbit fit | `fit.json`, `fit.tle`, `fit_omm.json/.xml/.kvn` | Fitted orbit (JSON includes covariance) |
| sat_orbit predict | `predictions.txt/.csv/.json` | Visible passes |
| sat_orbit ephem | `ephemeris.oem` / `ephemeris_oem.xml` | CCSDS OEM state vectors |

---

## 10. Accuracy and limits

- **Across the track:** about 1 px (51″). Each frame's WCS is fitted to its
  own matched stars.
- **Along the track:** set by the clock. Each 0.1 s of clock error is about
  0.05° at 0.5°/s. Set the clock from your phone before capturing.
- **Magnitudes:** about ±0.2–0.3 mag, including a 0.15 mag colour term. A
  saturated object (often the ISS) gives a bright limit only.
- **One pass, one site:** direction and rate are measured well, distance is
  not. So:
  - Identification works well.
  - A one-pass orbit is circular, and its period can be off by several
    percent.
  - Next-pass timing is ± minutes, and later passes are "timing UNKNOWN".
- **Two passes:** all six elements; timing about ±1 s, a few km after 1–3
  days.
- **Element-set age:**
  - The path across the sky stays close for weeks; the timing drifts
    (minutes for low orbits, and classified objects manoeuvre).
  - Far past its epoch, SGP4 can produce impossible states, such as a fake
    object "passing" every few minutes. These are now rejected.
  - Whole-file predictions skip sets more than 30 days from the window
    (`--max-age-days`).
- **Not identified ≠ unknown satellite.** Check the aircraft flag, and
  whether the object was sunlit.
- **SatChecker positions** come from public TLEs: often several arcminutes
  off along the track, more for Starlink.

---

## 11. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `no element set` from a URL | Quote the whole URL in PowerShell; use `&` between parameters, not a second `?` (the error message says so) |
| `NO DARK SKY ... no pass can be listed` | The window is in daylight at the site. Start at dusk (`--start 17:00`) or widen it |
| `0 visible pass(es)` with `--sky` | Whole passes rarely stay on one side of the meridian; try `--sky-mode part` |
| An object listed every few minutes at > 2°/s | A very old element set. Fixed: such states are rejected, and old sets are skipped by default. `git pull` |
| `capture clock: unknown` / "do not post" | The finder's time wasn't set this boot. Use Utilities → Clock before capturing |
| No tone curve in the archive | Captured before the OTA. Magnitudes assume the shipped curve |
| `unknown time zone` on Windows | `pip install tzdata`, or use `UTC-5` / `local` |
| `argument --in-tz: expected one argument` | Write `UTC-5` or `--in-tz=-05:00` |
| Every overlay offset by the same ~0.3° | `satcheck_overlay --sat-frame jnow` |
| Hand-tracked frames missing | Frames that don't plate-solve are left out; try `--link-interp`, shorter exposures, a steadier push |

---

## 12. Library modules and automated tests

These modules in `tests\` are used by the tools above and have no command
line of their own:

| Module | Does |
|---|---|
| `sat_tracks.py` | Frame-by-frame tracks: each frame minus its neighbours' steady sky, fixed-pattern rejection, linking along a smooth sky path |
| `sat_photometry.py` | Tone-curve linearisation, per-frame zero point and vignetting from the matched stars, dash/dot photometry, light curves, flash period, behaviour codes |
| `sat_oem.py` | CCSDS OEM read/write (KVN and XML), frame conversion (TEME / GCRF / EME2000 / ICRF / ITRF), interpolation |

The automated tests run without hardware or network, on simulated captures
drawn from real element sets:

| Test file | Covers |
|---|---|
| `test_live_capture.py` | The finder's recorder: archive contents, size and time limits, recovery after a power cut, web routes |
| `test_clock_info.py` | Clock source records (network, phone, SkySafari), restore at start |
| `test_satcheck_overlay.py` | Per-frame WCS, predicted streaks, stacking, trail detection and timing |
| `test_sat_identify.py` | End to end: a simulated capture with a 37 s timing error, identification (and rejecting the sister satellite), refinement, every output format, IOD columns, element loading |
| `test_sat_photometry_tracks.py` | Tone round trip, calibration, dash flux and flash period, light-curve classes, IOD columns, clock codes, a hand-tracked pass |
| `test_sat_orbit.py` | One-pass circular fit with honest period error, two-pass recovery, prediction vs truth, CLI files, whole-file prediction, dark-sky text, time zones, decayed-orbit rejection |
| `test_sat_oem.py` | OEM round trips in three frames and both formats, predictions from an OEM, time systems, sky-region filter |
| `test_iss_check.py` | A simulated ISS pass with a 0.7 s clock error (recovered), element sources (folders, lists, .gz, KVN, newest wins), URL cache and offline fallback |

Run them from the repository root (`pip install pytest` first):

```
python -m pytest -q tests\test_sat_identify.py tests\test_sat_orbit.py tests\test_sat_oem.py tests\test_iss_check.py tests\test_sat_photometry_tracks.py tests\test_satcheck_overlay.py tests\test_live_capture.py tests\test_clock_info.py
```
