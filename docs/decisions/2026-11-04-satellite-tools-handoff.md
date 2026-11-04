# Satellite tools — handoff for a new chat (2026-11-04)

Purpose: everything needed to continue the satellite work in a fresh chat
without the old one. The full exchange log is
`docs/transcripts/2026-11-02-to-11-04-satellite-thread-exchanges.md`; the
thread-wide record (finder work before and alongside this) is
`docs/decisions/2026-11-04-session-record-thread.md`.

**Branch:** `claude/azimuth-on-0.11.74` (mconsidine/diofinder). Latest commit
at handoff: `6042e7c`. Device side: v0.11.75 release, OTA-able to this branch.

---

## 1. The idea and the goal

The finder (Pi Zero 2W + IMX477, 25 mm lens, ~13.5° field, 51″/px) shows many
satellites crossing its field. The user wants to:

1. Record what the finder sees (frames + exact UTC + plate solutions) with a
   button — **live capture** (built, on the device).
2. Off-device, on a Windows laptop (PyCharm, PowerShell terminal), find the
   satellite trails, **identify** them (SatChecker for the public catalog;
   Mike McCants' classified/integrated element files for the rest), measure
   **positions, magnitudes and flash behaviour**, and write **reports in the
   SeeSat-L community's formats** (IOD; also R.D.E.).
3. Derive or refine **element sets** (classic TLE plus the newer OMM formats),
   and predict **where to point next**.
4. Test the whole chain against **ground truth (the ISS)**.

Status: everything is built and unit-tested on synthetic captures; only one
real capture has been analysed so far (2026-11-02, whose only trail was an
aircraft). **Next step is the user's: OTA-update the finder, then capture
real satellite passes (fixed field, hand-tracked, an ISS pass, and a second
pass of the same object).**

---

## 2. Working conventions with this user

- Runs scripts offline on a **Windows laptop in PyCharm**: commands go in the
  PyCharm **Terminal** (Alt+F12), opened at the `diofinder` repo root; one line
  per command (PowerShell has no `\` continuation); `git pull` to update.
- Laptop packages: `pip install numpy astropy matplotlib pillow sgp4` (no
  scipy; the code avoids it — astropy's Lomb-Scargle FAP needs scipy, so the
  FAP is computed in numpy).
- OTA on the finder: `sudo /usr/local/bin/diofinder-update --ref claude/azimuth-on-0.11.74`.
- Element files the user keeps: `C:\Users\matt\Downloads\TLEs\classfd.zip`,
  `inttles.zip` (from https://mmccants.org/tles/). Site in tests: 43.95 N,
  72.667 W, 330 m (placeholder; use the real one).
- The user prefers explanations with concrete commands; asks to confirm
  assumptions; wants honest accuracy statements.

---

## 3. What exists (files and what they do)

### On the device (`diofinder/live_capture.py`, web UI)
- **Live capture** (Home page Start/Stop; Utilities → Live captures to
  download/delete). Archive `livecap_<UTC>.zip`: `session.json` (site East+,
  FOV, clock source record, `tone` = the camera's tone curve from the tuning
  file — *new, needs the OTA*), `frames.jsonl` (per frame: sensor
  exposure start/mid UTC + JD, exposure, gain, solved centre RA/Dec J2000,
  roll, mirrored, FOV, and `matched` stars `[x, y, ra, dec, mag]`), and
  `frame_NNNNNN.png` (8-bit, 960×760, Y plane after the ISP tone curve).
- Caps: `live_capture_max_mb` 4000, `live_capture_max_minutes` 60,
  `live_capture_min_free_mb` 1000, `live_capture_keep_mb` 8000 (oldest pruned).
- Clock: phone-set (Utilities → Clock) is best; SkySafari sets whole seconds;
  the archive records which (`clock_info.current()`).

### Off-device scripts (`tests/`)
| Script | Does |
|---|---|
| `satcheck_archive.py` | SatChecker FOV query per frame/group (stdlib, text listing) |
| `satcheck_overlay.py` | Per-frame TAN WCS fitted to the matched stars; SatChecker predictions drawn on frames (rails); `--stack` star-aligned stacks (satellites = dashed lines), trail detection + frame-by-frame timing, `trails.csv`, aircraft flag; `--chart` |
| `sat_tracks.py` | Frame-by-frame residual tracks (each frame minus its neighbours' sky median), fixed-pattern rejection, linking along a smooth sky path — for **hand-tracked** passes and long arcs |
| `sat_photometry.py` | Tone-curve inversion, per-frame zero point + vignetting from matched stars, dash/dot photometry against the steady sky, light curves, Lomb-Scargle period, IOD behaviour code |
| `sat_identify.py` | The main tool: trails + tracks → identification against element files (free along-track time) → refinement → photometry → reports (IOD, R.D.E., SeeSat-L draft, light curves, refined TLE/OMM, `observations.json`) |
| `sat_orbit.py` | `fit` orbits to `observations.json` (one pass: circular; two+: full 6), `predict` visible passes (one object with timing ±, or every object in a file) |
| `iss_check.py` | Ground-truth test of the whole chain on an ISS pass vs CelesTrak's current ISS elements |

Tests: `test_satcheck_overlay.py`, `test_sat_identify.py`,
`test_sat_photometry_tracks.py` (incl. a synthetic hand-tracked pass),
`test_sat_orbit.py`, `test_iss_check.py`. Full suite at handoff: all pass (676 tests collected).

### Commands (PowerShell, repo root)
```
python tests\satcheck_overlay.py livecap_X.zip --stack --chart
python tests\sat_identify.py livecap_X.zip --elements c:\users\matt\Downloads\TLEs\classfd.zip,c:\users\matt\Downloads\TLEs\inttles.zip --station 9999 --status G
python tests\sat_orbit.py fit livecap_X_ident\observations.json
python tests\sat_orbit.py predict livecap_X_ident\orbit\fit.json --site-from livecap_X_ident\observations.json --hours 24
python tests\sat_orbit.py predict c:\users\matt\Downloads\TLEs\classfd.zip --lat 43.95 --lon -72.667 --elev 330 --hours 48 --min-elevation 25 --out plan
python tests\sat_orbit.py predict c:\users\matt\Downloads\TLEs\classfd.zip --lat 43.95 --lon -72.667 --elev 330 --start 17:00 --end 06:00 --min-elevation 25 --out plan
python tests\iss_check.py livecap_X.zip
```
Useful flags — sat_identify: `--points-per-pass N` (IOD lines per track, 0 =
all), `--time-unc S`, `--status E/G/F/P/B/T`, `--unknown-desig`,
`--conditions "…"`, `--equipment "…"`, `--time-standard`, `--no-iod-mag`,
`--no-photometry`, `--no-link`, `--link-interp`, `--readout-ms`, `--window`,
`--max-offset`, `--max-cross`, `--download`, `--responses`/`--satchecker`.
sat_orbit predict: `--norad a,b`, `--name SUBSTR`, `--tables`,
`--all-orbits`, `--start`/`--end` (LOCAL time unless `--utc`/`--in-tz`; `HH:MM` allowed, an end before the start = next day), `--out-tz` (default utc; also on sat_identify and iss_check; JSON/IOD/R.D.E./TLE always UTC), `--step`. sat_orbit fit: `--track ID`,
`--start ELEMENTS`, `--circular`/`--full`, `--name`, `--norad`, `--intldesg`.

### Element sources (every script)
Files (TLE/2LE/3LE, CCSDS OMM XML/KVN/JSON/CSV), `.zip`/`.gz` of them,
folders (walked), URLs (cached 2 h in `~\.diofinder_elements`; offline → use
the older cached copy with a warning), comma-separated lists of any of these.
**Per catalog number the newest epoch wins** (`load_elements(near_jd=…)` picks
the closest epoch instead; not exposed on the CLI). Outputs: classic TLE
(Alpha-5 for 100000–339999; 9-digit numbers OMM-only), OMM KVN/XML/JSON/CSV.

---

## 4. Decisions and the reasoning behind them

1. **Identify first, derive second.** A single 10–60 s arc from one site
   fixes direction and rate well but not range, so an orbit from scratch is
   ill-conditioned. Old element sets keep their **plane** long after their
   timing drifts, so matching is on line + direction + rate with the
   along-track time free ("N s early/late"), then the match is refined (mean
   anomaly + node at the pass epoch).
2. **Orbit fitting honesty** (simulated with a real eccentric set, NOSS 3-4 A,
   e = 0.046): one pass → circular 4-element fit, period wrong by up to ~6 %
   for that eccentricity → a 4.5 % mean-motion model error is added to the
   covariance, so next-pass timing is honestly ±minutes and later passes are
   flagged "timing UNKNOWN" (past 1/6 revolution). Two passes hours apart →
   all 6 elements (revolution count by a mean-motion scan, eccentricity by
   multi-start): ~1–2 km after 1–3 days, timing ±1 s. A known set + one pass
   (`--start`) fits only node and mean longitude.
3. **Photometry from the 8-bit frames is enough** (±0.2–0.3 mag): the tone
   curve is invertible; no ALSC stage; adaptive contrast off. 12-bit raw was
   judged unnecessary: its cost is **storage/Wi-Fi (6–18 MB/frame), not CPU**.
   Middle option if ever needed: 2×2-binned 16-bit raw (~1.5 MB/frame).
   Real-sky calibration rms on 2026-11-02 was 0.33 mag (faint stars near the
   8-bit floor, catalog band, focus) — recheck on a clear focused night.
4. **IOD format verified** against G. Lewis' IOD Version 0 description (the
   user pasted it; satobs.org is blocked from the build sandbox). Angle format
   2 states its **position uncertainty in arcminutes** (a bug writing degrees
   was fixed); MX codes rounded up; behaviour codes S/F/R/I/X (periodic → F if
   flash-like else R; aperiodic → X if flash-like else I; "flash-like" =
   duty cycle < 0.3 in linear light); magnitude cols 67–70 (+045 = 4.5),
   uncertainty 72–73, flash period 75–80 (point between 77/78); sky codes
   E/G/F/P/B/T. Unknown objects `99999 99 099A` + name `unkYYMMDD` (from the
   user's SeeSat-L sample). Time uncertainty from the capture's clock record:
   NTP 0.05 s, phone 0.2 s, SkySafari 1 s, else 10 s + "do not post".
5. **R.D.E. format** also written (header, day lines, brightest/faintest
   magnitude, flash period, remark; unknown = 9900000).
6. **Aircraft**: unidentified tracks that flash like strobes (stack flash
   ratio, or a 0.5–2 s flash period with > 2 mag amplitude) get a "CHECK: may
   be an AIRCRAFT" note but are not dropped (a tumbling unknown satellite can
   look the same).
7. **Hand-tracking** (long arcs): implemented via `sat_tracks.py`; flags — keep
   `detect_max_axis_ratio` 0 (trail rejection off), short exposures
   (0.1–0.3 s), frames that don't solve are left out; solving while panning by
   hand is **untested on sky**.
8. **ISS as ground truth** (`iss_check.py`): CelesTrak elements (good to ~1 km
   and < 0.1 s near epoch) → across-track rms (astrometry), along-track offset
   in seconds (= the finder's clock error), one-pass orbit vs truth, magnitude
   vs a rough prediction (ISS std mag −1.8; often saturates → bright limit).
   Synthetic test: 0.7 s clock error recovered to 0.001 s. Use elements within
   ~2 days of the pass (reboosts).

---

## 5. Real-capture results so far

`livecap_20261102T020749Z.zip` (82 frames): per-frame WCS fits 0.64 px rms;
the documented roll convention is right; the one trail is an **aircraft**
(not STARLINK-30913: opposite direction, that satellite was in shadow, strobes
~0.6–0.7 s). `sat_identify` (2026-11-04): no match (closest candidate StarSh
99159: 4° off, 163° wrong direction, in shadow); magnitude 1.8 ± 0.3,
frame-to-frame scatter 1.2 mag, flash period 0.69 s — first classed "R", which
exposed the flash-detection weakness fixed in `2add863` (now "F" + aircraft
note). That archive predates the clock and tone recording (warnings expected).

---

## 6. Open items / next steps

1. **User:** OTA to this branch (tone-curve recording is a device change),
   set the clock from the phone, capture: a sunlit fixed-field pass, an ISS
   pass (`iss_check.py`), a hand-tracked pass, and two passes of one object
   (two-pass orbit fit). Then share `identification.txt` / `iss_report.txt`.
2. Confirm on SeeSat-L: the unknown-object convention (`99999 99 099A`) and
   R.D.E. piece numbering (A=01, skipping I and O — assumed).
3. Possible later: CCSDS OEM ephemeris input (NASA's ISS trajectory) as a
   better truth than the TLE; expose `near_jd` (closest-epoch) on the CLI;
   photometry calibration rms investigation on real data; ISS standard
   magnitude/phase model if the photometry check matters.
4. Not built: on-device satellite identification (deliberately off-device).

## 7. Commits (satellite work)

`0375fcf` live capture · `c064828` per-frame WCS + overlay · `b57f89f` stack
composite · `8e9325d` unseeable predictions hidden, trail detection ·
`b93ecda` capture time limit + clock source · `a71cdb1` sat_identify ·
`77db35b` capture size cap · `d1c8f29` photometry, IOD reports, hand-tracked
tracks, orbit fits · `e706735` IOD per spec, R.D.E., behaviour codes ·
`2add863` duty-cycle flashes, aircraft flag · `69bbe5d` predict every object
· `e19ceae` iss_check + element sources · `6042e7c` offline cache fallback.
