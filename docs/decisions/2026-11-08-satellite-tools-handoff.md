# Satellite tools — handoff for a new chat (2026-11-08)

Purpose: everything a fresh chat needs to continue the satellite work. This
version **supersedes** `docs/decisions/2026-11-04-satellite-tools-handoff.md`
(kept unchanged). Read this one first, then the 11-04 one only for the
reasoning behind earlier decisions (its §4 "Decisions" and §5 "Real-capture
results" are still current and not repeated in full here).

Related files:
- Exchange logs: `docs/transcripts/2026-11-02-to-11-04-satellite-thread-exchanges.md`,
  `docs/transcripts/2026-11-04-to-11-08-satellite-thread-exchanges.md`.
- Thread record (finder work alongside this):
  `docs/decisions/2026-11-08-session-record-thread.md`.
- User guide (complete workflows and every option):
  `docs/satellite-user-guide.md`. Keep it current when options change.
- Mount-tracking assessments (nothing built): `docs/assessments/2026-11-08-satellite-tracking-*.md`.
- Developer reference: the "Live capture" section of `CLAUDE.md`.

**Branch:** `claude/azimuth-on-0.11.74` (mconsidine/diofinder), unmerged. Head
at handoff: the commit that adds this file (the one before it is `f8c5aa5`).
Device: v0.11.75 release plus OTA to this branch. Last full test run: 711
passed, 1 skipped.

---

## 1. Goal and status

The finder (Pi Zero 2W + IMX477, 25 mm lens, ~13.5° field, ~51″/px) sees many
satellites. The user wants to:

1. Record frames + exact UTC + plate solutions on the device (**live capture**:
   built).
2. On a Windows laptop, find trails, identify them (SatChecker; Mike McCants'
   `classfd.zip`/`inttles.zip`; CelesTrak), measure positions, magnitudes and
   flashes, and write SeeSat-L reports (IOD, R.D.E.) (**built**).
3. Fit or refine element sets and predict where to look next (**built**).
4. Check the whole chain against the ISS (**built**, `iss_check.py`).
5. **New, assessment only:** paste a TLE or upload a session schedule and have
   the finder drive the mount to follow passes. See §5.

Everything is unit-tested on synthetic captures. Only one real capture has
been analysed (2026-11-02; its trail was an aircraft). **The next step is
still the user's field work:** OTA, set the clock, capture a sunlit
fixed-field pass, an ISS pass, a hand-tracked pass, and two passes of one
object.

---

## 2. Working conventions with this user

- Scripts run on a **Windows laptop in PyCharm**: PowerShell terminal at the
  repo root (`C:\Users\matt\Downloads\diofinder`, a `.venv`), one line per
  command (no `\` continuation), `git pull` to update.
- Laptop packages: `pip install numpy astropy matplotlib pillow sgp4` (no
  scipy; Windows needs `pip install tzdata` for IANA zone names).
- OTA on the finder: `sudo /usr/local/bin/diofinder-update --ref claude/azimuth-on-0.11.74`
  (add `--with-db` to refresh the star database too).
- Site: 43.95 N, 72.667 W (`--lon -72.667`, East-positive), 330 m.
- Element files: `C:\Users\matt\Downloads\TLEs\classfd.zip`, `inttles.zip`;
  PreviSat folder `c:\users\matt\appdata\local\Astropedia\PreviSat\elem\`.
- Preferences: concrete copy-paste commands; honest accuracy statements;
  confirm assumptions; **UTC for anything shared** (reports, JSON, IOD), **local
  time for what the user types**; newest epoch wins for element sets; when the
  user asks for an assessment only, change no code.
- Sandbox limits: satobs.org, mmccants.org (403), manualslib.com and
  manualmachine.com are not reachable from the build sandbox. The network has
  not been widened for them; say so instead of guessing.
- Commits: end with the `Co-Authored-By` / `Claude-Session` lines the session
  supplies; no model names in repository files; push only to this branch; no
  PRs unless asked.

---

## 3. What exists

### 3.1 On the device
- **Live capture** (Home page Start/Stop; Utilities → Live captures).
  `livecap_<UTC>.zip` = `session.json` (site East+, elevation, FOV, clock
  source record, `tone` curve), `frames.jsonl` (sensor exposure start/mid UTC +
  JD, exposure, gain, J2000 centre RA/Dec, roll, mirrored, FOV, `matched`
  stars), `frame_NNNNNN.png` (8-bit, 960×760). Caps: 4000 MB / 60 min per
  capture, 1000 MB free floor, 8000 MB kept in total.
- **Site card** (Utilities → Site, new 11-04): latitude, longitude, elevation;
  numbers or N/S/E/W letters; longitudes 0..360 folded. Maint
  `site_get`/`site_set`, `/api/site`, `/site/set`, `horizon.parse_site`.
  Elevation has to be set here: LX200 has no elevation command, so SkySafari
  cannot send it.
- **Clock card** (Utilities → Clock, reworked 11-04):
  - NTP-style measurement: several `/api/time` exchanges, shortest round trip
    kept, error shown as ± half of it.
  - Set posts `{offset_s, rtt_ms}`; `diofinder-set-time --offset` reads and
    sets the clock in one process (the old whole-second form left it 0.8–0.9 s
    behind).
  - **Under network time** (`/run/systemd/timesync/synchronized`), the finder
    is the reference: the card shows the phone's error, Set is disabled and
    `/time/set` returns 409 (timesyncd would undo it).
  - **Field fact:** the user's phone runs ~1 s fast against NTP; the finder
    at home is on network time. Earlier "0.8–1 s off" reports were the phone.
- **Shipped-default report** (Update page, end of `diofinder-update`): a key
  missing from the device conf is listed only when the built-in default
  differs from the shipped one, as `<not set: using X>`. The two `config.py`
  defaults that differed (`camera_tuning_file` → `imx477_finder.json`,
  `star_db_deep`) were aligned.
- SkySafari before the first solve shows RA 0 / Dec 0 (a placeholder, not a
  bug).

### 3.2 Off-device scripts (`tests/`)
| Script | Does |
|---|---|
| `satcheck_archive.py` | SatChecker FOV query per frame/group |
| `satcheck_overlay.py` | Per-frame WCS from matched stars; predictions drawn on frames; `--stack` stacks, trail detection and timing, aircraft flag, `trails.csv`, `--chart` |
| `sat_tracks.py` | Frame-by-frame residual tracks (hand-tracked passes, long arcs) |
| `sat_photometry.py` | Tone-curve inversion, star calibration, light curves, flash period, IOD behaviour code |
| `sat_identify.py` | Trails/tracks → identification (free along-track time) → refinement → photometry → IOD, R.D.E., SeeSat-L draft, refined TLE/OMM, `observations.json` |
| `sat_orbit.py` | `fit` (one pass circular, two+ full), `predict` (one object with timing ±, or every object in a file), `ephem` (CCSDS OEM) |
| `sat_oem.py` | OEM writer/reader (new 11-04) |
| `iss_check.py` | Ground-truth test on an ISS pass |

### 3.3 Added since the 11-04 handoff
- **Times** (`31e1b20`): `--start`/`--end` are local unless `--utc` or
  `--in-tz ZONE`; `HH:MM` allowed; an end before the start = next day; a bare
  `--end HH:MM` counts from the start's date; `--end` replaces `--hours`.
  `--out-tz` (default `utc`; `local`, `UTC-5`, IANA) for printed reports, on
  `sat_orbit`, `sat_identify` and `iss_check`. JSON, IOD, R.D.E., TLE/OMM, OEM
  and the SeeSat-L draft always UTC. Write a negative offset as `UTC-5` or
  `--in-tz=-05:00` (argparse reads `-05:00` as an option).
- **Sky region** (`0f0ddf1`): `--sky east|west|north|south|ne|nw|se|sw` or a
  comma list (intersection). East/west = of the meridian; north/south = of the
  east–west line through the zenith. `--sky-mode whole` (default: the whole
  visible pass must lie there) or `part` (cut each pass to that stretch).
- **CCSDS OEM** (`0f0ddf1`): `sat_orbit ephem ELEMENTS|fit.json` writes state
  vectors every `--step` s, KVN `.oem` or `--xml`, frames TEME (default) /
  GCRF / EME2000 / ICRF (via astropy) / ITRF, UTC, start snapped to a whole
  second (millisecond-rounded epochs gave 1.5 m interpolation error), at most
  100 objects. `predict` reads OEMs (KVN/XML, several segments,
  UTC/TAI/TT/GPS, Lagrange at the file's degree, only inside its span). OEMs
  are ignored as element sources; no covariance blocks.
- **Stale and decayed sets** (`2cdea76`): SGP4 long past a decay returns err 0
  with nonsense (USA 294, a 1172-day-old set, "passed" every 2.5 min).
  `sane_states` gives err 7 below 80 km, unbound, or with a semi-major axis
  outside ×0.75–1.33 of the elements'. Whole-file `predict` skips sets more
  than `--max-age-days` (30; 0 = all) from the window; passes break at gaps in
  the dark-only grid. PreviSat `.gp` files are read in folder walks.
- **`--norad`** (`dc7aa9b`): comma list of catalog numbers, Alpha-5 accepted
  (`A0658` = 100658); a bad entry gives a plain message. Names go in
  `--name SUBSTR`. `fit` takes one number.
- **User guide** (`a1712bc`, corrected later): `docs/satellite-user-guide.md`.
- Earlier the same day: `282ba6c` (never cache a URL reply with no element
  sets), `bbeccd9` (says when the window has no dark sky; faster big files).

### 3.4 Commands (PowerShell, repo root)
```
python tests\satcheck_overlay.py livecap_X.zip --stack --chart
python tests\sat_identify.py livecap_X.zip --elements c:\users\matt\Downloads\TLEs\classfd.zip,c:\users\matt\Downloads\TLEs\inttles.zip --station 9999 --status G
python tests\sat_orbit.py fit livecap_X_ident\observations.json
python tests\sat_orbit.py predict livecap_X_ident\orbit\fit.json --site-from livecap_X_ident\observations.json --hours 24
python tests\sat_orbit.py predict c:\users\matt\Downloads\TLEs\classfd.zip --norad 51445,53323 --lat 43.95 --lon -72.667 --elev 330 --start 17:00 --hours 72 --min-elevation 25 --sky east --out-tz local --out plan
python tests\sat_orbit.py ephem c:\users\matt\Downloads\TLEs\classfd.zip --norad 51445 --start 17:00 --hours 6 --step 60 --out ephem
python tests\iss_check.py livecap_X.zip --out-tz local
```
A CelesTrak URL must be quoted whole in PowerShell:
`"https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE"`.

---

## 4. Decisions still in force (details in the 11-04 handoff §4)

1. Identify first (line + direction + rate, along-track time free), then
   refine; don't fit orbits from scratch off one arc.
2. Honest orbit errors: one pass → circular fit with a 4.5 % mean-motion model
   error ("timing UNKNOWN" past 1/6 revolution); two passes → all 6 elements.
3. 8-bit frames suffice for photometry (±0.2–0.3 mag); raw capture not needed.
4. IOD per G. Lewis' description (angle format 2 uncertainty in arcminutes);
   R.D.E. also written; unknown objects `99999 99 099A`, named `unkYYMMDD`.
5. Strobe-like unidentified tracks get an aircraft note, not dropped.
6. ISS as ground truth, using elements within ~2 days of the pass.
7. New: reject impossible SGP4 states rather than trusting err 0; skip sets
   older than 30 days in whole-file listings.
8. New: shareable output in UTC by default; typed input local by default.

---

## 5. Mount tracking from a TLE or schedule (assessment only, 11-08)

The user asked what it would take to paste one TLE, or upload a file of
element sets as a session schedule, and have the finder set up and drive the
mount through each pass. **No code was written.** Five documents:

- `docs/assessments/2026-11-08-satellite-tracking-common.md`: all mounts.
- `…-synscan.md`: SkyWatcher SynScan / the Virtuoso (the mount in use).
- `…-lx200.md`: Meade LX200 / Autostar and LX200-protocol mounts.
- `…-onstepx.md`: OnStepX controllers.
- `…-orion-teletrack.md`: Orion TeleTrack (9481 GoTo ≈ SynScan; 9441 probably
  has no computer port; manuals unread, sandbox can't reach them).

Key conclusions:
- **Satellite maths exists only on the laptop** (astropy is too heavy for the
  Pi). Phase 1 ports element parsing, SGP4 (`sgp4` package) and a light pass
  predictor to the device, plus a Satellites page (paste a TLE, upload a
  schedule, show passes).
- **The finder never moves a mount today**, by design (`mountlink.py` is
  sync-only; `docs/onstep-design.md` defers GoTo). Most of the work is a motion
  interface (`capabilities`, `goto_altaz`/`goto_radec`, `set_axis_rates`,
  `read_position`, `stop`), safety, a pointing model and a 5–10 Hz tracking
  loop. Leapfrog GoTos are the universal fallback.
- **Phase 0 is always a probe of the real mount** (version, GoTo forms, rate
  commands, top speed, rate resolution, read latency). For the Virtuoso:
  path A (hand controller NexStar-style `b`/`z`/`P` variable rate) vs path B
  (motor-controller `:` protocol via PC-direct mode or a direct cable).
  LX200's custom rates are 0.1°/s steps (coarse). OnStepX is the best suited
  (fast steppers, LX200-native, open firmware).
- **Pointing model reuses the polar alignment code** (`diofinder/polar.py`
  `fit_axis`, `decompose_alignment_error`; `polar_run.PolarAligner`, UI
  hidden): for alt-az, sweep azimuth, convert each solve to the horizon frame
  with its own timestamp, fit the axis, and read the levelling tilt; for
  equatorial builds the existing routine applies as it is, and becomes
  automatic once the finder can move the RA axis.
- Phases: 0 probe · 1 device engine + Satellites page · 2 motion + safety +
  simulator · 3 pointing model · 4 tracking loop and pass sequence · 5
  (optional) optical closed loop. Phase 1 is useful on its own (planning with
  no motion).

---

## 6. Open items / next steps

1. **User field work:** OTA; clock (network time at home, or the phone with
   its ± shown); captures of a fixed-field sunlit pass, an ISS pass, a
   hand-tracked pass, two passes of one object. Then review
   `identification.txt` / `iss_report.txt` together.
2. **If mount tracking goes ahead:** start with phase 0 (a probe script for
   the Virtuoso, results recorded in the SynScan assessment) and phase 1 (the
   device engine and Satellites page, planning only). Confirm the Virtuoso
   model (original with SynScan AZ hand controller, or GTi).
3. Confirm on SeeSat-L: the unknown-object designator; R.D.E. piece numbering.
4. Possible later: NASA's ISS OEM as truth in `iss_check`; expose `near_jd`
   (closest epoch) on the CLI; photometry calibration on real data; OEM
   covariance; an azimuth-dependent horizon mask (beyond `--sky`).

## 7. Commits since the 11-04 handoff

`282ba6c` no caching of empty URL replies · `bbeccd9` no-dark-sky message,
faster big files · `31e1b20` local-time input, `--out-tz` · `0f0ddf1` sky
filter, OEM · `2cdea76` SGP4 sanity, `--max-age-days` · `a1712bc` user guide ·
`c016901` Site card, honest default report · `636bfbf` clock offset helper ·
`ae9f67c` NTP-style ± · `e3e5dc7` under network time show the phone's error,
refuse set · `dc7aa9b` `--norad` Alpha-5 · `f8c5aa5` mount-tracking
assessments. Earlier satellite commits are listed in the 11-04 handoff §7.
