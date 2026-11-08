# Satellite thread — exchanges, 2026-11-04 to 2026-11-08

Continues `docs/transcripts/2026-11-02-to-11-04-satellite-thread-exchanges.md`, which ends with the user's save request of 2026-11-04 14:03 UTC. This file starts with the rest of Claude's reply to that request and runs to the user's request of 2026-11-08 15:18 UTC to prepare documents for a new chat. Every message between the user and Claude is included, in time order. Tool calls, command output and the contents of files created are not included; command output the user pasted into a message is kept, as part of that message. Messages sent while Claude was working are marked. Times are UTC. Companion handoff: `docs/decisions/2026-11-08-satellite-tools-handoff.md`.

---

### **Claude** — 2026-11-04 14:04:57

The exchanges file is done: 54 of your messages and 52 replies, with uploads, images and the pasted script left out. Next is the satellite handoff document, which needs the commit history.

All three files are written. Doing a final accuracy check, then committing.

All three are saved in the repo and pushed (`7762545`), and I've sent you copies.

**1. Exchanges since the satellite idea** — `docs/transcripts/2026-11-02-to-11-04-satellite-thread-exchanges.md`
- It starts at your 2 Nov 00:45 UTC message ("I've noticed a lot of satellites…") and runs to your save request, in time order with UTC timestamps: 54 of your messages and 52 replies.
- Messages you sent while I was working are included and marked, such as the satobs.org pages and the IOD and R.D.E. format descriptions you pasted.
- Left out:
  - **Files:** uploaded files and images appear only as labelled markers.
  - **Pasted script:** your ~150-line SatChecker notebook script is replaced by a one-line note; it became the basis of `satcheck_overlay.py`.
  - **Tool activity:** command output, tool calls and the code I wrote.

**2. Satellite handoff for a new chat** — `docs/decisions/2026-11-04-satellite-tools-handoff.md`
- The goal and its current status, with your working setup: Windows, PyCharm terminal, the OTA command, where your element files live.
- Every script, with copy-paste commands and the useful flags.
- The decisions and the numbers behind them: identify first, honest orbit errors, the IOD spec checks, raw capture not needed, aircraft handling, hand-tracking flags, ISS as ground truth.
- Real-capture results so far, open items with the next field captures first, and the commit list.

To continue in a new chat, point it at that file. It links to the exchanges file and the thread record.

**3. New general record for this thread** — `docs/decisions/2026-11-04-session-record-thread.md`
- It covers 20 Oct to 4 Nov:
  - Alt/Az and the RA/Dec question;
  - supply-chain pinning and v0.11.73/74;
  - the azimuth branch to v0.11.75, including the headroom monitor, networking and performance work;
  - the v0.11.76 to-do work and builds #192–#195;
  - a satellite summary, and the open items.
- I treated `docs/decisions/2026-10-31-supply-chain-pinning-and-lx200-diagnostics.md` as the earlier record for this thread. It is unchanged; the new file sits beside it, as you asked.

If you'd rather have the handoff as a shareable page outside the repo, I can publish it as one.

---

### **User** — 2026-11-04 14:12:17

I see this: (.venv) PS C:\Users\matt\Downloads\diofinder> python tests\sat_orbit.py predict https://celestrak.org/NORAD/elements/gp.php?CATNR=25544"?"FORMAT=TLE  --lat 43.95 --lon -72.667 --elev 330 --hours 48 --min-elevation 25 --out plan
no element set

---

### **Claude** — 2026-11-04 14:14:50

The URL lost its `&`. With `"?"` in its place, the address became `…?CATNR=25544?FORMAT=TLE`, which CelesTrak doesn't understand, so it sent back an error instead of element data. The script then only said "no element set".

The fix is to put the whole URL in double quotes. In PowerShell `&` has a special meaning, which is presumably why you swapped it out; quoting avoids that:

```
python tests\sat_orbit.py predict "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE" --lat 43.95 --lon -72.667 --elev 330 --hours 48 --min-elevation 25 --out plan
```

I also made the scripts handle this better. It's pushed (`282ba6c`); `git pull` to get it.

- **Clearer error:** when a URL returns no element sets, you now see what the server actually replied. If the URL contains a second `?`, you also get the hint to use `&` and quote the URL.
- **Bad replies are never cached:** an error page, or CelesTrak's "not updated since your last download" notice, could otherwise have sat in the 2-hour cache and caused the same failure on every run. Now only replies that contain element sets are kept. If a good older copy exists, it's used with a warning.

The tests pass, including a new one for these cases.

---

### **User** — 2026-11-04 14:27:07

yup, my bad.  thx

---

### **Claude** — 2026-11-04 14:27:11

No problem. It's an easy one to hit, since PowerShell treats `&` as special. It also exposed a real weakness: a bad reply could have sat in the 2-hour cache. That's fixed now.

When you have new captures, send me the `identification.txt` or `iss_report.txt` and I'll go through the results.

---

### **User** — 2026-11-04 14:32:47

I ran a stress test to see if it would predict all the elements held by the Previsat folder.  I got this, which seems unlikely: (.venv) PS C:\Users\matt\Downloads\diofinder> python tests\sat_orbit.py predict c:\users\matt\appdata\local\Astropedia\PreviSat\elem\  --lat 43.95 --lon -72.667 --elev 330 --hours 6 --min-elevation 25 --out plan                                                                                                               
21516 element sets; predicting all of them
1297 slow object(s) (< 2 rev/day: geostationary, high orbits) skipped; --all-orbits to include them
: 0 visible pass(es)
  none in the window
  highest at (UTC)          object                     number  alt  az   RA       Dec     rate    visible (UTC)        elements
Elements a few days old or more: the path across the sky stays close, but the TIME can be off by minutes (classified objects manoeuvre). Be pointed early.

---

### **Claude** — 2026-11-04 14:35:56

The 0 passes was actually correct. You ran it at about 14:30 UTC, which is 09:30 at your site, so the 6-hour window ran through daylight. Passes are only listed when the sky is dark (Sun more than 6° below the horizon) and the satellite is sunlit. The script just never told you that. It also loaded all 21,516 element sets from the PreviSat folder without complaint, so the stress part worked.

I've fixed what made it confusing; it's pushed (`bbeccd9`), so `git pull` first:

- **Darkness line:** the output now starts with the dark-sky times in the window, for example:
  `Dark sky (Sun below -6 deg) at the site, UTC: 11-04 22:10 - 11:05`
  If there are none, it prints "NO DARK SKY … start the window at dusk with --start, or give more --hours".
- **Title:** the blank ": 0 visible pass(es)" heading came from the trailing backslash on the folder path. It now shows the folder name.
- **`--sun-alt`:** sets how dark counts as dark. The default is −6; use −3 to include brighter twilight, or −12 for darker skies.
- **Speed:** satellites are now only computed at dark times. That's about twice as fast per day of window, and instant for an all-daylight window, which matters with 21,000 objects.

To try the stress test again over tonight (EST is UTC−5, so dusk around 22:00 UTC):
```
python tests\sat_orbit.py predict c:\users\matt\appdata\local\Astropedia\PreviSat\elem\ --lat 43.95 --lon -72.667 --elev 330 --start 2026-11-04T22:00:00 --hours 8 --min-elevation 25 --out plan
```
Expect a long list, probably thousands of passes, and a run of a few minutes for that many objects. Two things to check in the output:
- **Too many:** narrow it with `--name STARLINK`, `--name ISS`, a higher `--min-elevation`, or a shorter window.
- **Element ages:** a large shared folder like this can hold old sets, which make pass times less reliable.

---

### **User** — 2026-11-04 14:39:07

for the command-line specification of start/end times and dates, can we use local time, with a flag to accept UTC?  And can we separately flag the use of local or UTC in any output/reporting?  it would be convenient to query for predictions using local time, but have the output that could be share be in UTC, for example.

---

### **Claude** — 2026-11-04 14:40:10

Now the shared time helpers in sat_identify, then wiring them into each script.

Adding the local/UTC time options to `sat_orbit predict` and `iss_check` now (the `sat_identify` half is already done).

Now iss_check:

Now the tests: existing CLI tests pass UTC starts, so they get `--utc`; plus new ones for the time handling.

Now rerun the append script without the already-applied replacements:

Argparse reads `-05:00` as a flag. Users would hit that too, so the help will suggest the `UTC-5` form (`--in-tz=-05:00` also works).

Tests pass. Here's what the real console output looks like:

Found a bug: a bare `--end 07:00` was put on today's date, not the start's (38 h instead of 14 h). Fixing it so a bare end time counts from the start date:

Now the docs, then the full suite.

Full suite is running in the background; I'll commit when it finishes.

The local/UTC changes are written and the targeted tests pass. The full test suite is still running; I'll commit and push once it's green.

**How it works**

- **Typed times are local by default.** `--start` and the new `--end` use the laptop's own time zone.
  - Add `--utc` to type UTC instead, or `--in-tz` with a zone such as `UTC-5` or `America/New_York`.
  - A time that ends in `Z` or carries an offset is read as written, whatever the flags say.
  - A plain `17:00` works. A bare `--end 06:00` that falls before the start means the next morning.
  - `--end` replaces `--hours`.
- **Printed times are UTC by default, ready to share.** `--out-tz local` (or any zone) changes this for the console and `predictions.txt`. Every time is followed by its zone, e.g. `2026-11-03 04:04:40 EST`.
  - The run now starts with a `Window: … to …` line, so you can check how your input was read.
  - With a non-UTC `--out-tz`, `predictions.csv` gets extra local-time columns and keeps the UTC ones.
- **Always UTC, whatever you choose:** `predictions.json`, IOD, R.D.E., TLE/OMM, `observations.json` and the SeeSat-L draft.
- **Other scripts:** `sat_identify` and `iss_check` also take `--out-tz`, for `identification.txt`, `iss_report.txt`, light-curve labels and the console.

**Tonight's passes, typed in local time, printed in UTC:**
```
python tests\sat_orbit.py predict c:\users\matt\Downloads\TLEs\classfd.zip --lat 43.95 --lon -72.667 --elev 330 --start 17:00 --end 06:00 --min-elevation 25 --out plan
```
Add `--out-tz local` if you'd rather read the times in local time.

Things to know:
- **Breaking change:** a `--start` you type without `Z` is now read as local time, not UTC. Add `--utc` to any saved commands that use UTC times.
- **Negative offsets:** write `--in-tz UTC-5`, or `--in-tz=-05:00` with the `=`. A bare `-05:00` gets mistaken for a flag.
- **Zone names on Windows:** names like `America/New_York` need `pip install tzdata` once. `local` and `UTC-5` work without it.

I also found and fixed a bug in my first version: a bare `--end 07:00` was placed on today's date rather than the start's, giving a 38-hour window instead of 14 hours. There's now a test for that case.

The docs are updated: `CLAUDE.md`, `tests/README.md` and the satellite handoff doc.

The local/UTC time options are committed and pushed to `claude/azimuth-on-0.11.74` (`31e1b20`). The full test suite passed (680 passed, 1 skipped). Run `git pull` on the laptop to get them.

- **Typed times:** `sat_orbit predict --start`/`--end` are local time unless you add `--utc`, or the time ends in `Z` or carries an offset.
- **Printed times:** UTC by default; `--out-tz local` switches them. `sat_identify` and `iss_check` take `--out-tz` too.
- **Always UTC:** JSON, IOD, R.D.E., TLE/OMM and the SeeSat-L draft.

Any saved command that types a UTC `--start` without `Z` now needs `--utc`.

---

### **User** — 2026-11-04 15:01:51

A slightly odd request: I'd like to be able to have the option to filter the passes to those occur fully east or west of the meridian or north or south of an east/west line passing directly overhead.  This is due to viewing restrictions at sites because of buildings, ie only the eastern sky being visible for example.  Also, I know its overkill, but I'd like to add the CDSDS OEM format in the event its ever useful.  Finally, reformat these older-version commands to reflect the new command line options: "python tests\sat_orbit.py predict c:\users\matt\Downloads\TLEs\classfd.zip --lat 43.95 --lon -72.667 --elev 330 --hours 48 --min-elevation 25 --out plan

python tests\satcheck_archive.py livecap_20261102T020749Z.zip --group 30 --out hits.json

python tests\satcheck_overlay.py livecap_20261102T020749Z.zip --stack --chart

python tests\sat_identify.py livecap_20261102T020749Z.zip --elements c:\users\matt\Downloads\TLEs\classfd.zip c:\users\matt\Downloads\TLEs\inttles.zip --station 9999 

-----------
python tests\sat_orbit.py predict c:\users\matt\appdata\local\Astropedia\PreviSat\elem\iss.gp  --lat 43.95 --lon -72.667 --elev 330 --hours 48 --min-elevation 25 --out plan    

python tests\sat_orbit.py predict https://celestrak.org/NORAD/elements/gp.php?CATNR=25544"&"FORMAT=TLE  --lat 43.95 --lon -72.667 --elev 330 --hours 48 --min-elevation 25 --out plan  

python tests\sat_orbit.py predict c:\users\matt\Downloads\TLEs\inttles.zip --lat 43.95 --lon -72.667 --elev 330 --hours 12 --min-elevation 25 --out plan                                    
"

---

### **Claude** — 2026-11-04 15:03:46

This is three pieces of work: a sky-region filter, CCSDS OEM read/write, and refreshed commands. First I'll check how astropy handles velocity transforms between frames, which OEM needs:

Astropy handles TEME, GCRS and ITRS with velocities, and the round trip is exact. Next, writing the OEM module:

Now the sky-region filter in `_passes_from` and its callers:

Cleaning up a leftover placeholder in `cmd_ephem`:

Also: PreviSat's `.gp` files are skipped when a folder is walked, and an OEM passed where elements are expected should be ignored rather than misread. Fixing both in `sat_identify`:

Smoke test: write an OEM from the NOSS element set, predict from it, and compare with predicting from the TLE directly. Then try the sky filter.

All three frames and both formats give the same passes as the TLE. Now the sky filter:

Working. One fix: part mode can leave zero-length slivers (`08:20:30 -> 08:20:30`), so I'll drop fragments under two samples.

Now the tests:

A 1.5 m error. That's not the interpolation: the node epochs are written to the millisecond while the states were sampled at the unrounded start time. Snapping the start to a whole second:

An `--out` with no file extension should be treated as a folder. Fixing that, and dropping the "elements a few days old" note when every pass came from an ephemeris:

Adding an `ephem` run from a `fit.json` to the existing CLI test:

Now the docs: the `sat_orbit` docstring, `CLAUDE.md`, `tests/README.md` and the handoff doc.

While the full suite runs: `satcheck_archive` and `satcheck_overlay` have no time options, so those two commands stay as they are.

The full test suite is still running in the background. When it finishes I'll commit and push, then give you the updated commands.

The sky filter and CCSDS OEM support are pushed as `0f0ddf1`, and the full test suite passes. Run `git pull` to get them.

## Sky-region filter (`sat_orbit predict`)
- **`--sky east`** (or `west`, `north`, `south`) keeps only passes whose whole visible part lies on that side. East/west are split by the meridian; north/south by the east–west line through the zenith.
- **Quadrants:** use `ne`, `nw`, `se`, `sw`, or a comma list such as `--sky east,north`.
- **`--sky-mode part`** keeps passes that are only partly in that part of the sky, cut to the part that is. The default is `whole`, which is the "fully" you asked for. For a partly blocked sky, `part` usually turns up more usable passes.

On the NOSS test case, passes go nearly over the pole, so `--sky east` with whole passes kept none. `--sky-mode part` kept three. Each part pass shows its own start, end and highest point.

## CCSDS OEM
- **`sat_orbit.py ephem`** writes an OEM file of state vectors (position and velocity) every `--step` seconds, 60 by default. It works from any element source or from a `fit.json`.
  - Frames: TEME (the default), GCRF, EME2000, ICRF or ITRF.
  - Formats: the text form (`.oem`), or XML with `--xml`.
  - It takes the same `--start`/`--end`/`--hours` options as `predict`.
- **`predict` also reads OEM files.** It lists passes only within the period the file covers. I checked this by writing OEMs in TEME, GCRF and ITRF, as text and XML: passes predicted from them matched those from the original TLE exactly.
- **Not included:** covariance blocks are neither written nor read. An OEM can't be used as `--elements` for `sat_identify`, which needs element sets, so OEM files are skipped there.
- **Possible next step:** NASA publishes the ISS trajectory as an OEM, which would be a more accurate reference than the TLE for `iss_check`. I haven't connected it.

I also made folder scans pick up PreviSat's `.gp` files; before this they were skipped. A `.gp` file named directly on the command line was already read.

## Your commands, updated
Without `--start`, the window starts now. Times you type are now local.
```
python tests\sat_orbit.py predict c:\users\matt\Downloads\TLEs\classfd.zip --lat 43.95 --lon -72.667 --elev 330 --start 17:00 --hours 48 --min-elevation 25 --out plan

python tests\satcheck_archive.py livecap_20261102T020749Z.zip --group 30 --out hits.json

python tests\satcheck_overlay.py livecap_20261102T020749Z.zip --stack --chart

python tests\sat_identify.py livecap_20261102T020749Z.zip --elements c:\users\matt\Downloads\TLEs\classfd.zip,c:\users\matt\Downloads\TLEs\inttles.zip --station 9999

-----------
python tests\sat_orbit.py predict c:\users\matt\appdata\local\Astropedia\PreviSat\elem\iss.gp --lat 43.95 --lon -72.667 --elev 330 --start 17:00 --hours 48 --min-elevation 25 --out plan

python tests\sat_orbit.py predict "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE" --lat 43.95 --lon -72.667 --elev 330 --start 17:00 --hours 48 --min-elevation 25 --out plan

python tests\sat_orbit.py predict c:\users\matt\Downloads\TLEs\inttles.zip --lat 43.95 --lon -72.667 --elev 330 --start 18:00 --end 06:00 --min-elevation 25 --out plan
```
- `satcheck_archive` and `satcheck_overlay` have no time options, so those two are unchanged.
- In `sat_identify`, your space-separated `--elements` still works; the comma form is the same thing.
- For the URL, quoting the whole thing is cleaner than the `"&"` trick.
- Add `--out-tz local` to any `predict` to read times in local time; leave it off for UTC to share.
- Add `--sky east` (or `--sky east --sky-mode part`) for your building-limited site.
- An OEM for the ISS overnight, saved into the `plan` folder:
  ```
  python tests\sat_orbit.py ephem "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE" --start 17:00 --hours 12 --frame GCRF --out plan
  ```

---

### **User** — 2026-11-04 17:13:04

I have not run this yet, but will in a moment.  However, on the prior version I noticed this sort of output: (.venv) PS C:\Users\matt\Downloads\diofinder> python tests\sat_orbit.py predict c:\users\matt\Downloads\TLEs\classfd.zip --lat 43.95 --lon -72.667 --elev 330 --start 17:00 --end 21:00 --min-elevation 25 --out plan                                                                                                             
436 element sets; predicting all of them
45 slow object(s) (< 2 rev/day: geostationary, high orbits) skipped; --all-orbits to include them
Window: 2026-11-04 22:00:00 UTC to 2026-11-05 02:00:00 UTC (4 h)
Dark sky (Sun below -6 deg) at the site: 11-04 22:07 - 02:00 UTC
classfd.zip: 173 visible pass(es)
  highest at                    object                     number  alt  az   RA       Dec     rate    visible              elements
  2026-11-04 22:07:30 UTC       Trumpet 2                   23609   33  318   209.93  +55.82   0.00/s  22:07:30-23:45:50  +508.2 d
  2026-11-04 22:07:30 UTC       Trumpet 3                   25034   57   45   352.10  +59.20   0.00/s  22:07:30-02:00:10  +580.4 d
  2026-11-04 22:07:30 UTC       USA 184                     29249   49   56     5.28  +51.54   0.00/s  22:07:30-00:13:00  +612.5 d
  2026-11-04 22:07:30 UTC       SDS 3F5                     32378   52  260   259.97  +28.06   0.01/s  22:07:30-22:37:10  +508.1 d
  2026-11-04 22:07:30 UTC       STSS Demo 2                 35938   76  221   292.46  +32.88   0.28/s  22:07:30-22:12:50  +5.2 d
  2026-11-04 22:07:30 UTC       ISON 64800                  90103   57   43   350.92  +60.19   0.00/s  22:07:30-02:00:10  +2478.2 d
  2026-11-04 22:09:40 UTC       USA 294                     45110   55   83   350.07  +37.77   2.28/s  22:09:20-22:10:00  +1172.1 d
  2026-11-04 22:09:50 UTC       USA 455 StarSh              62354   56  115   336.84  +23.78   0.62/s  22:07:40-22:12:00  +41.8 d
  2026-11-04 22:11:40 UTC       USA 352                     59288   78  160   309.23  +31.89   0.99/s  22:10:00-22:13:20  +237.1 d
  2026-11-04 22:11:40 UTC       StarSh                      99254   82   32   310.77  +50.25   1.07/s  22:10:10-22:13:20  +8.2 d
  2026-11-04 22:12:20 UTC       USA 294                     45110   55   78   351.85  +40.55   2.28/s  22:12:00-22:12:40  +1172.1 d
  2026-11-04 22:12:40 UTC       NOSS 3-5 (B)                37391   65  126   327.03  +26.39   0.41/s  22:09:20-22:16:30  +5.1 d
  2026-11-04 22:12:50 UTC       NOSS 3-5 (A)                37386   64  121   329.08  +27.33   0.41/s  22:09:30-22:16:30  +5.1 d
  2026-11-04 22:15:00 UTC       USA 294                     45110   55   73   353.79  +43.31   2.28/s  22:14:40-22:15:20  +1172.1 d
  2026-11-04 22:15:00 UTC       USA 422 StarSh              61645   32  306   218.33  +46.37   0.42/s  22:13:30-22:16:20  +6.2 d
  2026-11-04 22:16:40 UTC       USA 644 StarSh              A0658   45   56    13.79  +50.51   0.53/s  22:14:50-22:18:40  +8.2 d
  2026-11-04 22:17:40 UTC       USA 294                     45110   55   68   355.92  +46.03   2.28/s  22:17:10-22:18:00  +1172.1 d
  2026-11-04 22:19:40 UTC       USA 446 StarSh              62345   71  124   325.08  +31.25   0.69/s  22:17:30-22:22:00  +9.8 d
  2026-11-04 22:20:10 UTC       USA 294                     45110   56  104   344.88  +28.31   2.29/s  22:19:50-22:20:40  +1172.1 d
  2026-11-04 22:22:50 UTC       USA 294                     45110   57   99   346.30  +31.18   2.29/s  22:22:30-22:23:10  +1172.1 d
  2026-11-04 22:24:50 UTC       USA 430 StarSh              61653   26  306   214.60  +43.11   0.37/s  22:24:10-22:25:30  +6.2 d   Why does something like "USA 294" show up so many times?

---

### **Claude** — 2026-11-04 17:13:23

The "+1172.1 d" in the last column is the giveaway: that USA 294 element set is about three years old. Let me reproduce it with the real file to confirm what SGP4 is doing:

The download is blocked from here, so I'll simulate instead: a typical low-orbit element set, propagated three years past its epoch.

Adding the sanity check to `si.propagate`, so identification, orbit fits and predictions all get it, and to `sat_oem.states`:

Testing with the simulated decayed object (the n=15.9 case above, where SGP4 says "fine" at +600 days):

Both pass. Now the full suite, since `propagate` is shared by identification and orbit fitting. I'll update the docs while it runs.

The full suite is running in the background; I'll commit and push once it passes.

USA 294 showed up so often because its element set is 1172 days old (the "+1172.1 d" in the last column). SGP4 was asked to propagate it more than three years past its epoch and returned an impossible orbit, which the old code listed as a real pass every 2.5 minutes. That's fixed and pushed (`2cdea76`; the full test suite passes), so run `git pull` before your next try.

**Why it happened.** SGP4 includes air drag, so it shrinks a low orbit over time. Past the point where the orbit should have decayed, it first reports an error (which the code already respected). Later on it goes back to "no error" while giving physically impossible positions: inside the Earth at 30 km/s, or far beyond the Moon. I couldn't download `classfd.zip` from here, so I reproduced this with a simulated old low-orbit element set rather than USA 294 itself. Your listing matches it: the same object every ~2.5 min at 2.3°/s, which nothing in orbit can do.

**What changed:**
- **Impossible positions are rejected** in all the scripts. A computed position counts as impossible if it is:
  - below 80 km altitude,
  - moving fast enough to escape the Earth, or
  - on an orbit whose size is far off the one in the element set.

  Normal element sets aren't affected; that is covered by the tests.
- **Old element sets are skipped** when you predict a whole file. A new `--max-age-days` option sets the limit (default 30; `0` keeps everything), and the console prints how many were skipped. With 30-day-old elements, a low orbit's timing can already be minutes off. If you ask for one object with `--norad`, it is still predicted, with a warning that it's old.
- **classfd keeps many old sets**, e.g. the Trumpet and ISON entries at 500–2500 days. Those are high orbits, so their positions aren't nonsense, but their timing can be off by a lot. They will now be skipped by default; add `--max-age-days 0` (or e.g. `365`) if you want them back.
- **Two smaller fixes:**
  - Slow objects now show their rate to three decimals (e.g. `0.004/s` rather than `0.00/s`).
  - With windows longer than one night, a pass can no longer join the end of one night to the start of the next.

The OEM and `--sky` work from the previous commit is unchanged, and the commands I gave you last time still work as written.

---

### **User** — 2026-11-04 17:38:15

I would like a document that is restricted to the use of the diofinder for identifying satellites.  Please write a users document that also includes examples of complete workflows and identifies all the test scripts and command line options.

---

### **Claude** — 2026-11-04 17:38:26

I'll write this as a user guide in the repo (`docs/satellite-user-guide.md`), so it stays versioned with the scripts it documents. First I'll collect the exact options from each script's own `--help`, so the guide matches the code:

`sat_tracks`, `sat_photometry` and `sat_oem` are libraries with no command line. Next, the outputs each tool writes, the package requirements, and the satellite tests:

Two more sources: the capture tips already in the handoff doc and the to-do file, so the guide's field advice matches what was worked out earlier.

Checking a couple of web-UI details the guide will reference: how the site elevation is set, and the Clock card's labels.

I have what I need. Writing the guide:

Before committing, checking a few claims in the guide against the code. First, how `--track FILE:ID` is parsed, since Windows paths contain a colon:

Adding pointers to the guide from `CLAUDE.md`, `tests/README.md` and the handoff doc:

The example commands parse and run as written. (The test site gets no passes from the three-object test file tonight; that is expected.) Committing:

I've written the satellite user guide; it's attached above and pushed to the repo as `docs/satellite-user-guide.md` (`a1712bc`). It covers only satellite identification. I took the option tables from each script's own `--help`, so they match the code as it is now.

What's in it:

- **Overview** of how the finder and the laptop tools fit together.
- **What you need:** the over-the-air update, the pip packages, `tzdata` on Windows, element files, and which steps need internet.
- **Setting up the finder:** site and elevation, the clock (with the time uncertainty each clock source gives), exposure, and the capture size and time limits.
- **Capturing:** fixed-field, hand-tracked and ISS passes, and downloading the archives.
- **Seven complete workflows**, as one-line PowerShell commands with your paths:
  1. Plan a night: tonight's passes, with sky-region and time-zone options.
  2. Quick look at what SatChecker expected in a capture.
  3. Identify everything and write the reports, with a checklist to go through before posting to SeeSat-L.
  4. ISS ground-truth check.
  5. Hand-tracked pass.
  6. Unknown object: one-pass orbit, then predict, capture a second pass and fit both.
  7. Export an OEM ephemeris.
- **Command reference:** a table of every option for `satcheck_archive`, `satcheck_overlay`, `sat_identify`, `iss_check` and `sat_orbit fit` / `predict` / `ephem`.
- **Reference sections:** times and time zones, element sources, every output file, accuracy and limits, and a troubleshooting table drawn from the problems you've actually hit.
- **Modules and tests:** the three library modules (no command line of their own) and the eight test files, with what each covers and the command to run them.

The guide lists the hand-tracked mode as not yet tried on the sky, because it isn't. The guide is also linked from `CLAUDE.md`, `tests/README.md` and the satellite handoff doc, with a note to keep it up to date when options change.

If you'd like a shareable copy outside the repo, I can also make it a Claude Doc.

---

### **User** — 2026-11-04 17:48:56

what is the cmd to update everything via OTA on the device now?

---

### **Claude** — 2026-11-04 17:49:05

To update everything (the code from the branch, the olive-solve and sycamore wheels, and the star database), run this on the finder:

```
sudo /usr/local/bin/diofinder-update --ref claude/azimuth-on-0.11.74 --with-db
```

- **Without `--with-db`**, the script still updates the code and the wheels. It only reports whether a newer star database exists.
- **The star database** is a large download. A new one can also cause the FOV calibration to re-run; it finds the value again on its own as it solves. If you'd rather skip that, leave off `--with-db`.
- **At the end of the run**, the script lists each wheel as old → new. If you see "NOT refreshed", tell me.

Almost all of the recent satellite work runs on your laptop, so `git pull` there. The only change on the finder is that captures now record the camera's tone curve, which magnitudes use. That makes this update worth doing before your next capture.

---

### **User** — 2026-11-04 17:51:33

isnt elevation available from skysafari?

---

### **Claude** — 2026-11-04 17:51:52

No. SkySafari knows your elevation, but the LX200 protocol it uses to talk to the finder has no command for it. The only site and time commands are:

| Command | Sends |
|---|---|
| `:St` | latitude |
| `:Sg` | longitude |
| `:SG` | UTC offset |
| `:SL` | local time |
| `:SC` | date |

diofinder already handles all of these. Latitude and longitude from SkySafari are saved into the config each time it connects (`diofinder/comms_proc.py`, the `:St` / `:Sg` branches), and the time commands set the clock. Elevation has no LX200 command, so it has to be entered by hand.

Enter it once on the Config page → Observer Location → Elevation (m). It's saved, so you only need to change it at a new site.

It's worth setting, but it isn't critical. For a satellite 500 km away, a 330 m error in your height moves its apparent position by up to about 0.04° (around 2–3 px at 51″/px). It's largest when the satellite is low; overhead, the shift is mostly along the line of sight. That's well inside the identification tolerance, but similar to the measurement accuracy, so leaving it at 0 would add a small error to the IOD positions and orbit fits.

---

### **User** — 2026-11-04 17:53:15

for other settings that read "absent" when there is a default value available, why is the default not used?

---

### **Claude** — 2026-11-04 17:53:18

I'll find where "absent" is shown and why it isn't falling back to the default.

"<absent>" comes from the config-migration and diff code. Let me see where it's shown:

The default is used. "<absent>" on the Update page (and in the `diofinder-update` output) is misleading: it only means the key isn't written in your `/etc/diofinder/diofinder.conf`. It doesn't mean the setting has no value.

**Why keys are missing from your file.** That file is kept across updates, so a key added in a later release is never written into it. When a key is missing, the finder uses the built-in default from `diofinder/config.py`.

**Why they're listed anyway.** The report compares your file with the shipped `etc/diofinder.conf.default`, not with what the finder actually uses. Any key not in your file is listed as "<absent>", even when the built-in value it falls back to is exactly the shipped default.

I compared all 111 shipped keys with the built-in defaults. 107 match, so for those an "<absent>" row is cosmetic: the finder is already running the shipped default. Four differ:

| Key | Shipped default | Built-in, used when absent | Effect |
|---|---|---|---|
| `camera_tuning_file` | `imx477_finder.json` | `imx477_scientific.json` | The only one that matters. The finder profile keeps faint stars that the scientific profile's defect correction can delete. It's probably present on your device, since it has been in the conf a long time. If it isn't, the migration that switches to the finder profile won't fire, because it only fires when the key is set to the scientific file. |
| `star_db_deep` | `.../diofinder_13deg_mag85.npz` | empty | The "Bad" seeing preset uses the standard star database instead of the deeper one. |
| `fov_calibrated` | `true` | `false` | It re-learns the field of view from the first ~30 solves. Harmless. |
| `conf_version` | `1` | `0` | Bookkeeping; set on the first start. |

If one of these four shows as "<absent>" on your Update page, the device really is running a value different from a fresh image's.

Two possible fixes:
1. **Make the report honest.** Show missing keys as "not set — using X", and drop them entirely when that built-in value equals the shipped default. Then the list only shows real differences.
2. **Close the gaps.** Make the built-in defaults for `camera_tuning_file` and `star_db_deep` match the shipped file. Or have the update write any missing key whose built-in value differs.

Both are small changes on the finder side, so they'd need an OTA update. Do you want me to do both?

---

### **User** — 2026-11-04 17:57:43

yes.  and elevation cannot be set from the webui.

---

### **Claude** — 2026-11-04 17:57:47

I'll start with elevation, since that's a bug. The Config page lists the row, so something must be rejecting the save.

The Config page is a read-only viewer. Latitude is set by a separate form; let me look at that:

A missing tuning file falls back safely, so changing the built-in default to `imx477_finder.json` is safe. Next: how the maint-command tests build their context, and the Utilities page route:

I have what I need. Last check: that the deep star database is only used when the file exists, so its built-in default can safely become the shipped path.

Now the report: a missing key is compared with the built-in default the finder actually uses. It's listed only when that value differs from the shipped one, and is labelled honestly.

Updating that test, plus new cases for missing keys that match or differ from the built-in default:

Now the maint commands, placed next to `polar_set_latitude`:

Now the web routes, next to the Clock routes:

Now the Site card on the Utilities page, after Clock:

Updating the Config page's descriptions to point at the new card:

Now the tests for the site work:

All pass. Full suite in the background while I update the docs:

The user guide I wrote said to set the site on the Config page, but that page is view-only. Correcting it along with the other docs:

700 passed. Committing:

Both changes are made and pushed (`c016901`), and the full test suite passes. They take effect on the finder after the OTA command from before:

```
sudo /usr/local/bin/diofinder-update --ref claude/azimuth-on-0.11.74 --with-db
```

**Elevation.** You were right: no web page could set it. The Config page only shows settings, and the only edit form for the site was a latitude box on the hidden Polar page. There is now a **Site** card on the Utilities page, under Clock, with latitude, longitude and elevation and a **Save site** button.
- Longitude can be typed as `-72.667` or `72.667W`, and latitude as `43.95N`. Bad values are refused with a message.
- The site is saved to the config, used straight away, and recorded by the next live capture.
- SkySafari still overwrites latitude and longitude each time it connects, but it can't touch elevation.
- Live captures and Alt/Az use a new site at once. The plate-solver process reads its own copy of the site at start-up, so only the polar-alignment latitude reaches it immediately, as with SkySafari today. The opt-in horizon-reject and IMU solve-calibration features see the change after the next restart.

**"<absent>" in the settings report** (Update page, and the end of `diofinder-update`):
- **The report.** A key missing from your config is listed only if the value it falls back to differs from the shipped default, and then reads `<not set: using X>`. Missing keys that already run the shipped value no longer appear.
- **The gaps.** The built-in defaults for the camera tuning file and the deep star database now match the shipped file. A config missing either key now runs the same as a fresh image. A test checks that the built-in and shipped defaults stay the same.
- **Still different:** `fov_calibrated` keeps a built-in `false` on purpose. A config without it re-learns the field of view instead of assuming it's correct. `conf_version` is set on the first start, so it's never missing in practice.

I also corrected the satellite user guide, which wrongly said to set the site on the Config page, and added an "elevation is 0" row to its troubleshooting table.

---

### **User** — 2026-11-04 18:23:58

The setting of the time on the device leaves a delta of 0.8 or 0.9 relative to the phone.  I guess there's no good way to sync them exactly, correct?

---

### **Claude** — 2026-11-04 18:24:07

That 0.8–0.9 s is too large to be network delay, and it's very close to the time a command takes to start up on a Pi Zero. I'll check how the time is actually set:

Now the web route. It uses the offset (with half the Wi-Fi round trip added) and no longer waits for a whole second:

Confirmed (2 s, or 10 s after a phone set). Updating the card text, then the route tests:

That failure is in my stand-in `python3` (it logged the wrong argument), not in the helper. Fixing the stand-in:

The tests pass. Updating `CLAUDE.md` and the sudoers comment, then running the full suite:

No, that gap wasn't a real limit. It was a bug, and it's fixed and pushed (`636bfbf`). The full test suite passes, but I haven't run the new setter on a Pi, so check it after the update.

**Cause.** The web page sent the phone's time correctly. The finder then waited for the next whole second and handed that time, as text, to a chain of programs (sudo, a shell script, then `timedatectl` twice). On a Pi Zero that chain takes most of a second just to start. By the time the clock was set, the second it named had already passed, so the finder ended up about 0.8 s behind. The Clock card's displayed difference already allows for the Wi-Fi round trip, so the 0.8–0.9 s you saw was real.

**Fix.**
- The setter now receives the *difference* between the phone and the finder, not a clock time. It reads and adjusts the clock in a single step, so start-up delays no longer count.
- It also adds half the Wi-Fi round trip that the Clock card already measures. That's the time the phone's timestamp spent in transit.
- It no longer waits for a whole second.

**What to expect.** After the OTA update, the Clock card should show the finder within a few hundredths of a second of the phone, set by Wi-Fi jitter. That is roughly 10–20 times better than SkySafari, which only works in whole seconds.

The reports still assume ±0.2 s for a phone-set clock. That stays a safe margin until real captures show what the clock actually achieves; the ISS check measures it directly.

I also corrected the Clock card's text. It said SkySafari only fixes errors over 10 s; in fact it fixes errors of 2 s or more, or 10 s once the phone has set the clock.

To get it on the finder:

```
sudo /usr/local/bin/diofinder-update --ref claude/azimuth-on-0.11.74
```

After it restarts, reload the Utilities page and press **Set finder time from this device**, then watch the delta.

---

### **User** — 2026-11-04 18:33:30

Still seeing it off by 0.8-1s

---

### **Claude** — 2026-11-04 18:33:41

Then my diagnosis was wrong or incomplete. Before changing anything else, I'll check whether the Clock card's own measurement can be trusted. If the `/api/time` request is slow on the Pi, both the displayed delta and the round-trip correction I just added would be biased.

The `/api/time` timestamp is taken before the file reads, which cost about a millisecond, so it's fine. Updating the route tests for the new `offset_s` form; the old `phone_ms` form still works:

My edit script failed partway, and the file was left unchanged; the second step looked for a test the first step had already removed. Re-running without it:

My first fix addressed only part of the problem; this second one, pushed as `ae9f67c`, covers the rest. I tested it in a browser here, not on your Pi or phone.

**What was still wrong:** the Clock card measured the difference from one request, assuming the Wi-Fi delay was the same both ways. When a phone is in Wi-Fi power-save mode, replies to it can be held back for a large part of a second. A single request then misreads that delay as a clock error of up to half of it. My previous fix made this worse: it added half the round trip to the setting, so a delayed reply also pushed the clock off.

**What it does now** (the same idea NTP uses):
- **Several samples.** The card makes several quick requests and trusts only the one with the shortest round trip. It uses 3 for the display and 8 just before setting.
- **An honest ±.** The difference is shown as, for example, "−0.02 s ± 0.03 s vs this device". The ± is half that shortest round trip, and the true difference lies within it. A big ± means "the Wi-Fi was slow this time", not "the clock is off".
- **Setting:** the measured difference is applied in one step.

**Browser test:** I ran the web UI against two identical clocks and delayed most replies by 0.2–0.9 s.
- The card read −0.33 s ± 0.33 s, consistent with the true value of 0.
- Pressing Set moved the clock by 0.0001 s.
- With the old single-request method, readings like −0.33 s showed with no ±, which looked like a real error.

**On the finder:**
1. Update:
   ```
   sudo /usr/local/bin/diofinder-update --ref claude/azimuth-on-0.11.74
   ```
2. Reload Utilities, and check the card now shows a **±**. If it doesn't, the update didn't take.
3. Press Set. The message should read "moved by … (measured ± …)".
4. Watch a few readings afterwards. They should sit near 0, within their ±.

If it still reads 0.8 s or more with a small ±, the difference is real. Then check the phone's own clock against time.is: phones set from the mobile network can themselves be off by about a second. Send me the card's reading with its ± and the set message, and I'll take it from there.

---

### **User** — 2026-11-04 19:41:29

I'm not seeing a +/-

---

### **Claude** — 2026-11-04 19:41:37

If the card doesn't show a ±, the browser isn't running the new page. That means either the update didn't install it or the phone is showing a cached copy. I'll rule out my side first: is the change in the pushed branch, and does the file here still have it? (The tooling flagged that `utilities.html` changed on disk.)

`\u00b1` (the ± escape) appears zero times in both the working file and the pushed branch. Let me look at what actually landed:

Nothing on my side explains it. The pushed branch has the ± code (`ae9f67c`), and the update script stops with an error rather than silently keeping old code. So the finder is most likely still on the earlier commit, or the phone is showing a cached copy of the page.

Note the timing: the ± change went out *after* your "still 0.8–1 s" message. An update you ran before then got only the first fix.

**1. Check what the finder is running.** Open the **Update** page, which shows "Running commit". Or run on the finder:

```
git -C /opt/diofinder log --oneline -1
```

You should see `ae9f67c` (or later). If you don't, update again:

```
sudo /usr/local/bin/diofinder-update --ref claude/azimuth-on-0.11.74
```

Check that it ends with "Update to claude/azimuth-on-0.11.74 complete", not a FAIL line.

**2. Reload the page properly on the phone.** Close the Utilities tab and open it fresh, or open it in a private/incognito tab. Phone browsers sometimes restore an old copy of the page without fetching it again.

When it's the new page you'll see:
- the Finder line reading like `(+0.02 s ± 0.03 s vs this device)`, with two decimals and a ±;
- the Clock card's text mentioning "the ± shown";
- after pressing Set, a message like "moved by … (measured ± …)".

If `log` shows `ae9f67c` and a fresh private tab still has no ±, tell me what the Finder line says word for word. Then it's something about the page itself, and I'll dig in.

---

### **User** — 2026-11-04 19:47:05

Ok, its working.  I got "moved by+0.98 s (measures +/- 0.03 s)" but the finder line is saying (-1.00  s +/- 0.02 s vs this device)"

---

### **Claude** — 2026-11-04 19:47:34

The two numbers together say the move worked and then something put the clock back. Set measured the finder 0.98 s behind (± 0.03) and moved it forward 0.98 s. A moment later the card reads −1.00 ± 0.02 again.

The likeliest cause is **network time**. If the finder is on your home Wi-Fi with internet, it keeps its clock synchronized to internet time servers (NTP). The set routine has to pause that service to change the clock, and turns it back on at the end. When it comes back, it promptly steps the clock back to network time and undoes the phone's correction.

If that's what's happening, the finder's clock is right, and it's your **phone** that is about 1 s fast. That would also explain the 0.8–1 s you've seen all along. My first explanation, that starting the setting programs takes most of a second, describes a real delay, but it probably wasn't the cause: network time would have undone any setting anyway. Phones that take their time from the mobile network instead of the internet can be off by around a second.

Two quick checks:
1. Look at the **Source** line on the Clock card. If it says "network time (synchronized)", that's confirmed.
2. Open **time.is** on the phone. It shows how far the phone's clock is off. If it says about 1 s ahead, the mystery is solved.

If both confirm it, there's nothing to fix: in the field, without internet, the finder will keep the phone's time.

It would help if the page said so. I can change the Clock card so that, when the finder has network time:
- the Set button warns that network time is more accurate and would undo the setting;
- the reading is labelled as the phone's error, not the finder's.

Want me to add that once you've confirmed the Source line?

---

### **User** — 2026-11-04 19:48:26

Yes, "network time"

---

### **Claude** — 2026-11-04 19:48:31

Confirmed, so I'll add it. The server should also refuse a phone set while network time is synchronized, matching the rule SkySafari's sync already follows. First the current card:

Now a browser check with network time switched on. I'll make the browser's clock run 1 s fast, as your phone's does:

The Clock card change is done and pushed as `e3e5dc7`. The full test suite passes, and I checked the page in a browser with a clock running 1 s fast.

**While the finder has network time**, the Clock card now treats the finder as the correct clock:
- The Finder line says "network time".
- The device line shows the phone's own error, e.g. "1.00 ± 0.02 s ahead of network time".
- **Set** is greyed out, with a note explaining that a phone setting would be undone straight away. The finder refuses a phone setting during this time too; SkySafari's time sync already followed the same rule.

**In the field, without internet**, the card works as before: the difference is measured with a ±, and Set adjusts the finder from the phone.

Your setup was never wrong. Your phone runs about 1 s fast, and it was probably the cause of the 0.8–1 s readings from the start. It's worth knowing for satellite work: when you set the finder from that phone in the field, it will inherit the phone's 1 s error. That's about 0.5° along the track for a low satellite. Two ways to avoid it:
- Fix the phone's clock. Check it on time.is; turning automatic date and time off and on, or switching it to use internet time, often helps.
- Set the finder from the phone shortly after the phone has synced to internet time.

The ISS check measures whatever clock error remains directly from a capture.

To get the change on the finder:

```
sudo /usr/local/bin/diofinder-update --ref claude/azimuth-on-0.11.74
```

---

### **User** — 2026-11-04 19:54:56

When I now connect with skysafari, the default position - without any solving - is now 90 E and Altitude 0.  It used to be that it was pointing down.  What could have changed???

---

### **Claude** — 2026-11-04 19:55:04

Two recent changes touch what SkySafari sees before a solve: the new site can now be set from the web UI, and the clock is now on network time. I'll check what `:GR/:GD` and `:GA/:GZ` send when nothing has solved:

Nothing in the code changed. What's different is the time of day you connected.

**Why:** until the first solve, the finder reports RA 0h, Dec 0° (zero is its placeholder when there's no position yet). That isn't a direction relative to the finder; it's a fixed point on the sky, the spring equinox point, which rises and sets like a star. SkySafari draws it wherever that point is at the moment, using the phone's own clock and location.

From your site today, that point:

| Time (EST) | Altitude | Azimuth |
|---|---|---|
| 14:30 | −4.5° | 85° |
| 15:00 | **+0.9°** | **91°** |
| 16:00 | +12° | 101° |
| 21:00 | +46° | 182° (south) |
| 05:00–13:00 | below the horizon | |

A point on the celestial equator always rises due east (azimuth 90°). Around 15:00 today it is rising, so you'd see exactly 90° E, 0° up. Earlier connections in the morning or at midday found it below the horizon, which looked like "pointing down". It moves about 4 minutes earlier each day.

None of the recent changes (the site card, the clock, the settings report) touch what is sent before the first solve. Once the finder solves, the real position replaces it.

**Possible improvement:** if you'd like an unmistakable "no position yet" signal, I could make the finder report the point **straight down** (the nadir) until the first solve. With the site and clock both set (they are now), that's easy to compute, and SkySafari would always show the reticle under your feet before a solve, whatever the time. Without a site and clock it would fall back to today's RA 0 / Dec 0. Want that?

---

### **User** — 2026-11-04 19:57:47

Ok, my mistake.  Yes, it is at RA 0 Dec 0  which happens to be at 90, 0 right now.  Never mind.

---

### **Claude** — 2026-11-04 19:57:49

Yes, that's what's happening: RA 0, Dec 0 happens to sit at 90° E, 0° altitude right now. The finder is fine, so I'll leave it as it is.

If you'd ever like a clearer "no position yet" signal, the straight-down option is still available.

---

### **User** — 2026-11-04 22:13:36

will the command line option for --norad accept a list of names, eg --norad 51445,53323, etc?

---

### **Claude** — 2026-11-04 22:16:39

Passing. Updating the user guide's option tables to match, then committing:

Yes. In `sat_orbit predict` and `sat_orbit ephem`, `--norad` takes a comma-separated list of catalog numbers, with or without spaces after the commas. It takes numbers only, not names; to match names use `--name` (e.g. `--name StarSh`).

```
python tests\sat_orbit.py predict c:\users\matt\Downloads\TLEs\classfd.zip --norad 51445,53323 --lat 43.95 --lon -72.667 --elev 330 --start 17:00 --end 06:00 --min-elevation 25 --out plan
```

While checking this I found a gap and fixed it (`dc7aa9b`, pushed; `git pull` on the laptop). The full test suite passes.

- **Alpha-5 numbers now work.** Your earlier listing showed `USA 644 StarSh` as `A0658`. That's the Alpha-5 form element files use above 99999 (A0658 = 100658). Typing `--norad A0658` used to crash with a traceback; now it's accepted, so `--norad 51445,A0658` works.
- **Bad entries get a plain message.** Something like `--norad NOSS` now says it isn't a catalog number instead of crashing.
- **`sat_orbit fit` takes a single number,** since it fits one object. It accepts Alpha-5 too.

The user guide's option tables are updated to match.

---

### **User** — 2026-11-04 23:01:53

What should this command look like if I want to see local times and only view east of the meridian

---

### **Claude** — 2026-11-04 23:02:13

Add `--out-tz local` and `--sky east` to your previous command:

```
python tests\sat_orbit.py predict c:\users\matt\Downloads\TLEs\classfd.zip --norad 51445,53323 --lat 43.95 --lon -72.667 --elev 330 --start 17:00 --end 06:00 --min-elevation 25 --sky east --out-tz local --out plan
```

- **`--out-tz local`** prints the times in your computer's own time zone. Without it they're in UTC.
- **`--sky east`** lists only passes that stay entirely east of the meridian. If none do, add **`--sky-mode part`** to get each pass cut to just the part that's east of the meridian.
- **`--start 17:00 --end 06:00`** are already read as local time, so no change is needed there.

`predictions.txt` and `predictions.csv` in `plan` use the same local times. `predictions.json` always stays in UTC.

---

### **User** — 2026-11-04 23:04:32

And what if I want 3 days of predictions?

---

### **Claude** — 2026-11-04 23:04:37

Swap `--end 06:00` for `--hours 72`:

```
python tests\sat_orbit.py predict c:\users\matt\Downloads\TLEs\classfd.zip --norad 51445,53323 --lat 43.95 --lon -72.667 --elev 330 --start 17:00 --hours 72 --min-elevation 25 --sky east --out-tz local --out plan
```

- **Window:** this runs from 17:00 today for 72 hours. If you give `--end`, it overrides `--hours`, so leave `--end` out here.
- **Daytime:** only passes in a dark sky are listed, so the three days come out as three nights. The line at the top of the output shows each dark stretch.
- **Accuracy:** the timing of passes further out is less reliable when the element sets are a few days old. Each pass shows its element-set age in the last column.

---

### **User** — 2026-11-08 14:32:40

For this question, I do not want any code changes - I only want an assessment of what would be required to answer it.  Specifically - if I wanted to have a feature where I could either cut-and-paste 1 TLE or uploads a file with a schedule of TLEs to track for a session and then have the diofinder set and direct the mount, what would be needed beyond what exists now?

---

### **Claude** — 2026-11-08 14:33:20

Assessment only, so no code changes. First a look at what the mount link can do today and what the design docs already planned, so the gap analysis rests on the actual code:

Short answer: the satellite **maths** largely exists, but on your laptop, not the finder. The **mount** side is almost entirely missing. Today the finder can only *sync* the mount, never move it, and that was a deliberate safety decision (`docs/onstep-design.md` puts GoTo out of scope, and `mountlink.py` has no motion command at all). Moving the mount to follow a satellite is the bulk of the work, and part of it depends on what your mount's firmware accepts.

## What already exists and can be reused

| Piece | Where | Reusable for this? |
|---|---|---|
| Reading element sets (TLE/3LE/OMM, zip, folders, newest epoch wins, Alpha-5) | `tests/sat_identify.py` | Yes, the parsing logic |
| SGP4 propagation with the sanity checks (decayed/old sets rejected) | `tests/sat_identify.py` | Yes |
| Pass prediction (sunlit, dark sky, min elevation, sky region, local/UTC times) | `tests/sat_orbit.py` | The logic, yes; it needs a device version (below) |
| Site, elevation, accurate clock (NTP, phone set with ±) | finder | Yes; tracking depends on it |
| Plate solving and live capture of frames | finder | Yes: for aligning the mount, and to record each pass automatically |
| Serial link to the Virtuoso (MAX3232, `SerialTransport`, a lock-protected `_MountManager`) | `diofinder/mountlink.py`, `comms_proc.py` | Yes, the transport |
| SynScan commands: version, read RA/Dec, **sync** | `mountlink.py` | Only the start; no slew, rate or stop commands |

## What's missing

**1. A satellite engine that runs on the Pi.**
- The laptop tools rely on astropy, which is too heavy for 512 MB and isn't installed on the finder. Neither is `sgp4`.
- The device needs a lean version: `sgp4` plus numpy, a simple rotation from SGP4's frame to the local horizon, and a low-precision Sun position. About 0.01° accuracy is plenty for pointing.
- That adds one new dependency (`sgp4`, which has ARM wheels), pinned and hash-checked like the other wheels.
- Size: moderate. It's a port of logic that already exists and is tested.

**2. Input and planning in the web UI** (a new "Satellites" page).
- Paste one TLE, or upload a file (TLE/3LE/OMM/zip), stored on the finder. In the field there's no internet, so upload from the phone is the main route.
- The finder computes upcoming passes for the session: local times, maximum altitude, direction, sky region. You pick which to track. For a file, it builds a queue in time order and flags overlapping passes, with a rule for which wins (your priority order, or the higher pass).
- A "schedule file" format needs defining. Simplest: the element file, plus optionally a list of catalog numbers and a time window. The finder works out the passes itself.
- This phase is useful even before any mount motion: a pass list on the phone in the field.

**3. Mount motion: the largest piece, and new ground.**
- The mount link needs `goto_altaz`, `set_axis_rates`, `read_position` and `stop`. None of these exist.
- There are two ways to drive a SkyWatcher mount:
  - **A. Through the hand controller's command set** (the NexStar-style protocol `mountlink.py` already uses): a GoTo by alt/az to put the scope ahead of the satellite, then variable-rate commands on each axis, updated several times a second, with position reads as feedback. This is simpler and reuses the current link.
  - **B. Through the motor controller's own protocol** (the `:`-commands INDI's SkyWatcher drivers use): direct, precise speed control on each axis, better for smooth fast tracking. It needs the hand controller in pass-through mode or a direct connection, and it's more work.
- **Things to measure on your mount first** (I can't confirm them from here):
  - whether the Virtuoso's firmware accepts alt/az GoTo and variable-rate commands;
  - its maximum slew speed, and how fine its rate control is at low speeds;
  - how long a command and a position read take;
  - which Virtuoso exactly: the original with the SynScan AZ hand controller, or the Wi-Fi GTi.
- A small probe script run once against the mount answers all of this and decides between A and B. It should come first.
- Speeds required: a low satellite moves up to about 1–1.5°/s across the sky. At 9600 baud, a cycle of two rate commands plus one position read takes some tens of milliseconds, so about 5–10 updates a second is realistic.

**4. Alignment for alt/az control.**
- Today's sync hands RA/Dec to the mount and lets its own pointing model handle it. Commanding axis speeds directly needs a mapping from mount positions to true altitude and azimuth.
- At minimum that's an azimuth offset, an altitude offset and a two-number levelling tilt. All of these can be fitted automatically from a few plate solves at different positions, which the finder is good at. Size: moderate.

**5. The tracking loop.**
- A loop that predicts where the satellite will be, sets each axis's speed to follow it, and corrects drift from the position reads. Plus the pass sequence:
  - slew to a point ahead of the satellite;
  - wait for it;
  - track, then stop;
  - move on to the next pass in the queue.
- **Overhead passes:** an alt/az mount can't follow a pass that goes near the zenith, because the azimuth speed shoots up there; a pass peaking at 85° needs roughly 10°/s in azimuth. The planner must check each pass against the mount's speed limit and either cap the altitude (accept a gap) or flag the pass.
- **Timing errors in the element sets:** old or classified elements can be seconds to minutes off along the track. That needs **nudge buttons** in the UI (±0.5 s, ±5 s) to shift the timing while tracking. The most capable version would detect the satellite in the live frames and steer to centre it. That's research-grade on a Pi Zero (the laptop's track finder takes about 0.4 s per frame), so it's best left optional and later.
- Plate solving while the scope moves at 1°/s will mostly fail (star trails), so tracking must rely on the mount's own position feedback, not solves.
- The automatic sync loop has to be paused during a pass. A live capture can start and stop with each pass, so each pass is recorded for the identify/IOD tools.

**6. Safety.** This is required before anything moves the mount.
- Altitude limits (never below a chosen angle) and an azimuth cable-wrap limit.
- A speed cap.
- A large **STOP** on every page.
- A dead-man watchdog: if the control loop stalls or the phone disconnects, the mount stops.
- An explicit "arm" step before the first motion of a session.
- Defined behaviour if SkySafari is connected at the same time.
- Your design doc deliberately left motion as "a separate decision"; this is that decision.

**7. Testing.**
- A **mount simulator** (a fake serial device that answers like the SynScan) so the whole loop can be tested without hardware, like the rest of the suite.
- Then bench tests indoors with the real mount (rates and limits, with no sky needed).
- Then sky tests: ISS passes first, since the ISS is bright and its elements are accurate.

## Suggested order

1. **Probe the mount** (small): find out what commands and speeds it supports. This decides A or B, and whether the idea works at all with this mount.
2. **Satellites page, planning only** (moderate): paste/upload, pass list in local time, the queue. Useful immediately.
3. **Motion foundations** (moderate to large): the motion commands, limits, STOP and watchdog, the simulator, and alignment from plate solves.
4. **Pass tracking** (large): the tracking loop, the overhead-pass check, timing nudges, automatic capture per pass, the queue.
5. **Optional, later:** optical closed-loop tracking on the satellite itself.

## Decisions for you

1. Are you willing to have the finder move the mount at all, given the safety items above?
2. Which exact mount and hand controller (original Virtuoso with the SynScan AZ hand controller, or the GTi)?
3. For overhead passes, accept an altitude cap or a gap?
4. Is manual timing nudging enough, or is optical tracking a goal?

I can save this assessment as a design document in `docs/` if you want it kept with the project.

---

### **User** — 2026-11-08 15:14:42

save this as a design doc in docs/ as a synscan specific assessment.  then create assessments for lx200 and onstepx mounts.  if a skywatcher mount like orion teletrack would be different, create an assessment for that as well.  note that a polar alignment routine was already developed at one point and elements of that could be potentially used to get at adjustments for "true" point changes.

---

### **Claude** — 2026-11-08 15:15:06

I'll write four documents: SynScan, LX200, OnStepX, and Orion TeleTrack if it turns out to differ. First I need two facts: what the existing polar alignment routine actually computes, and what the Orion TeleTrack is.

I'll read the TeleTrack manual directly. It decides whether that mount needs its own document or is just a SynScan variant:

---

### **User** — 2026-11-08 15:18:46

the context for this chat is starting to get filled. when you are finished, create the documents necessary for new chat in this project to  be able to access all the work.  I'll probably pick up the satellite specific features in that thread.

*(sent while Claude was working)*

---
