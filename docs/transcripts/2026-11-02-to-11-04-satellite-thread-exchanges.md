# Satellite thread — exchanges, 2026-11-02 to 2026-11-04

Every message between the user and Claude in this chat from the moment the satellite idea came up (2026-11-02 00:45 UTC) to the request to save them. Tool calls, command output and the contents of files created or uploaded are not included (uploads and images are marked where they were attached). Times are UTC. Companion handoff document: `docs/decisions/2026-11-04-satellite-tools-handoff.md`.

---

### **User** — 2026-11-02 00:45:29

No, I don't need to pursue this.  There is something different I want to pursue though.  I've noticed a lot of satellites moving through the finder field of view.  It would be useful to have a "live capture" button, that just starts saving the displayed images along with the UTC date/time and RA/Dec for the center of the frame into an archive.  The button would be a toggle and when it is turned off again the archive would be closed with a timestamped filename.  The goal would be to offload it to a service or python program that could identify the satellites, e.g. like this example : https://satchecker.readthedocs.io/en/latest/notebooks/fov.html

---

### **Claude** — 2026-11-02 00:46:20

Reading the solver loop to find where each frame and its solve result meet, which is the cleanest place to record.

Design settled (record in the solver process, which already holds each frame and its solve). Quick check of PNG cost before writing the code:

Writing the recorder module now.

Fixing a deadlock risk I spotted (a stop requested from the writer thread could block on its own full queue): switching stop to a flag the writer checks.

Solver side is wired. Now the config keys, then the comms commands and the web UI.

Now the handler itself, placed just before `_handle_maint_command`:

Adding the web routes after `/api/mount`:

Now the Home page button (under the live view) and a Utilities card for the saved archives.

Now the off-device helper that turns an archive into SatChecker queries:

Now tests for the recorder, comms command, web routes and the helper:

The failures are the intended drop-when-behind behaviour (test pushes frames faster than the writer). Making the queue size injectable for tests:

Real bug caught: the writer read "last solved" at write time, by which point later frames had updated it. Capturing it when the frame is queued:

Full suite passes (579). Quick render check of the two pages with the daemon faked out, then a headless-browser click-through:

Playwright here is the Node one; running the fake server in Python and driving it from Node.

Status now updates live. Cleaning up the test server, then docs and commit.

I've built the live capture button. It's pushed to `claude/azimuth-on-0.11.74` (0375fcf) and hasn't run on the device yet.

**How it works:**
- **Home page:** a **Start live capture** button sits under the live view. While it records, a status line shows the frame count, size and elapsed time. Press **Stop live capture** and the recording is saved as `livecap_<UTC start>.zip` (e.g. `livecap_20261102T013045Z.zip`).
- **Utilities → Live captures:** lists the archives with their sizes and the card's free space, with a download link and a Delete button for each.
- **It records raw frames.** The 8-bit frame the solver sees is saved as a PNG, not the stretched image the live view shows; a raw frame keeps faint trails better.
- **Frames with no stars aren't recorded,** because the solver skips frames that dark.

**What's in each archive:**
- **`frames.jsonl`**, one line per frame:
  - the sensor's own exposure start and mid-point, as UTC and as Julian Date, plus exposure time and gain;
  - the solved RA/Dec of the frame centre (J2000), plus the aim point, roll and number of matched stars.
  - Each frame gets its own solve result. A frame that didn't solve (a bright trail can break a solve) carries the last good pointing instead, marked as such.
- **`session.json`:** your latitude, longitude and elevation, the field of view, and a radius that covers the whole frame (about 8.6°). It also records start and end times, frame counts, why it stopped, and whether the Pi's clock looked believable.
- **The PNG frames.**

**Satellite lookup:** I added an off-device script, `tests/satcheck_archive.py`. It turns an archive into SatChecker `/fov/satellite-passes/` queries, one per frame (or one per few frames with `--group N`). It uses your site, each exposure's start time and length, the frame centre and the radius. Run it with `--dry-run` first to see the queries, then without it to fetch results (`--out results.json` saves the full responses).

I couldn't open SatChecker's documentation from this session (the site is blocked here). The parameter names and units come from search snippets of those docs ([FOV endpoints](https://satchecker.readthedocs.io/en/latest/fov.html), [route docs](https://satchecker.readthedocs.io/en/latest/src.api.entrypoints.v1.routes.fov_routes.html)), and the default server address `https://satchecker.cps.iau.org` is from memory. Check one dry-run URL against the docs before relying on the results.

**Limits and costs:**
- **Disk:** about 200 KB per frame, roughly 0.7 GB per hour at one frame per second. A recording stops by itself at 4 GB, or if free space on the card drops below 1 GB.
- **Old archives aren't deleted automatically,** so remove them from Utilities once they're downloaded.
- **Solver cost:** with capture off it's nothing measurable. While recording, the solver copies each frame; PNG saving runs in the background. If saving falls behind, frames are skipped and counted rather than slowing the solves.
- **Safety:** if the finder crashes or loses power mid-recording, the frames are zipped up the next time it starts.

**Two things to set or check before a session:**
- **The clock.** The Pi has no clock battery, so connect SkySafari once to set the time. The button shows a warning if the clock looks wrong.
- **Elevation.** There's a new **Elevation (m)** setting on the Config page under Observer Location. Only these recordings use it.

**Testing:** the new tests (18) and the full suite (579 passed, 1 skipped) pass. I also clicked through the whole flow in a headless browser against a fake solver: start, live counts, stop, archive listed, downloaded, deleted. The commit message overstates both counts by one (says 19 and 580); I left it rather than force-push.

To try it on the finder: `sudo /usr/local/bin/diofinder-update --ref claude/azimuth-on-0.11.74`.

---

### **User** — 2026-11-02 01:12:41

Here is an off-device script I was working with that gives me basically what I want.  Ideally I'd want a way to align/correlate the frames with this chart after the fact to say "ah, this moving dot was this satellite ..."  Feel free to adjust, fix, mark up, evolve, etc the script, which is based on the satchecker jupiter page.

*[pasted script — a ~150-line adaptation of the SatChecker FOV notebook; not saved here (it became the basis of `tests/satcheck_overlay.py`)]*

---

### **Claude** — 2026-11-02 01:19:02

While astropy installs, wiring the matched-star recording into the solver and recorder:

Now the overlay tool — your script evolved to work per frame from an archive:

Overlay tests all pass. The remaining failure is my earlier test using a guessed response shape; making `parse_satellites` tolerate odd shapes and updating that test to the real structure:

Full suite green (587). Let me look at the actual overlay output from the synthetic capture to check it reads well:

The file holds a literal σ; switching to the Edit tool for that block.

The real trail now shows between the predicted rails, and the decoy's rails are empty. Adding a recorder unit test for the matched-star rows, then docs and commit:

I turned your script into `tests/satcheck_overlay.py`. For each frame of a live-capture zip, it draws where SatChecker says each satellite was during that exposure, directly on the frame. It's pushed to `claude/azimuth-on-0.11.74` (c064828). So far it has only been tested on synthetic frames, not on a real capture or against the live SatChecker service.

**The key change is on the finder.** While a capture is recording, every solve now also saves the stars it matched in that frame: each star's pixel position and its RA/Dec. From those, the script works out the exact sky-to-pixel mapping for each frame, including mirroring. Without them it would have to guess how the roll angle is defined, and that hasn't been checked on the sky. Nothing changes when you're not recording.

**What the script does:**
1. **Maps each frame.** It fits the mapping from that frame's matched stars. A frame that didn't solve (a bright trail can break a solve) uses the nearest solved frame within 30 s. Captures made before this update fall back to centre, roll and field of view, and are labelled "approx".
2. **Queries SatChecker by group.** Frames at the same pointing within `--window` seconds (default 60) share one query. It uses your parameters (`mid_obs_time_jd`, `duration`, `async=False`), handles the task-polling case, and saves every response so re-runs work offline.
3. **Draws the predicted streak on the frame.** Each satellite's predicted streak for that frame's exposure is shown as two thin lines (rails) either side of the path, so a real trail shows between them. A circle marks the start of the exposure, and the whole pass is dotted.
4. **Scores each prediction** by how bright the image is along it. Predictions can be off by several pixels, so it checks a little either side, and it counts in noise units. Use the score only to decide which images to look at first.

**Outputs** (in `<archive>_sats/`):
- `frame_NNNNNN_sats.png` for each frame a satellite crosses.
- `report.csv`, one row per frame and satellite: streak ends in pixels, whether it's in the frame, the score, and how the frame was mapped.
- With `--chart`, your original sky chart, now drawing the real frame outline instead of a circle and showing UTC in the title.

```
pip install astropy matplotlib pillow
python3 tests/satcheck_overlay.py livecap_….zip --chart
python3 tests/satcheck_overlay.py livecap_….zip --frames 120-180 --offline   # re-plot from saved responses
```

**Tested so far:** I recorded a synthetic capture through the finder's real recording code, drew a streak along a known path, and faked a SatChecker response in the structure your script uses. The prediction landed within half a pixel of the drawn streak, for both normal and mirrored frames, and scored far above a decoy that crossed the same frame. The image I sent shows that case. The full test suite passes (588).

**Open questions for your first real capture:**
- **Coordinate frame:** I don't know whether SatChecker returns J2000 or of-date coordinates. If every satellite looks shifted by about the same 20 px or so (about 0.3°), add `--sat-frame jnow`.
- **Timing:** the Pi's clock and the TLEs limit accuracy. A clock 1 s off moves a low-orbit satellite about 0.5°, and a stale TLE puts it ahead of or behind its real trail, so expect some slack along the track.
- **Grouping vs your 20 s window:** I couldn't reach SatChecker from here, so I don't know how densely it samples positions. If a 60 s group returns too few points for a smooth track, try `--window 20`.

To update the finder: `sudo /usr/local/bin/diofinder-update --ref claude/azimuth-on-0.11.74`. Only captures made after this update carry the matched stars.

---

### **User** — 2026-11-02 01:52:03

Wouldn't another way of approaching this be to align and stack all the images in an archive, using the start and end times of the first and last frames to be those things for the composited image?  Then, satellites would show up as a dotted or dashed line and they would ideally be bracketed by your rails.  Or have I missed your intent?  If I uploaded a current debug file would that help?

---

### **Claude** — 2026-11-02 01:52:32

You haven't missed my intent; you've added a second view of the same data. The script I wrote answers "which satellite made the streak in this particular frame?" Your stack answers "what crossed the field during this whole capture?", which is easier to take in at a glance and makes faint passes easier to see. I'd add your view as a `--stack` option rather than replace the per-frame one, since each is good at something different.

**How a stacked composite would work:**
- **Align first, then stack.** The mount doesn't track, so stars drift about 0.3 px a second, roughly 18 px over a minute. Each frame now has its own fitted sky-to-pixel mapping, so every frame can be shifted onto the first frame's pixel grid and the stars line up. That only needs numpy, and it would only stack frames at about the same pointing, the same grouping the queries already use.
- **Take the brightest value at each pixel, not the average.** Averaging N frames dims a streak N times, while keeping each pixel's brightest value keeps every streak at full strength. Subtracting the median stack (everything constant: stars, sky glow, hot pixels) leaves only what moved.
- **Satellites become dashes.** Each dash is one exposure, and the gaps are frame readouts or skipped frames. One SatChecker query covers the first frame's start to the last frame's end, as you said, and its predicted pass is drawn as rails over the whole path, so the dashes should sit between them.
- **The dashes also show the timing error.** Each predicted dash can be drawn at its own exposure time, so you can see how far the real dashes run ahead of or behind the prediction. That separates a stale TLE (or a clock that's off) from simply the wrong satellite, which the single-frame view can't do as easily.
- **Where the per-frame view still wins:** naming the exact frame and time a streak appeared, and passes too short to form more than one dash.

**On the debug bundle:** yes, it would help, though not for everything:
- **It doesn't record matched stars.** A debug bundle stores each frame's centre, roll, field of view and mirroring, but not the matched stars, so the script would fall back to the approximate mapping built from roll. That's still useful: I can check that roll and mirroring convention against your real frames by placing bright catalogue stars on the image. It has never been checked on the sky, and older captures depend on it.
- **It shows me real conditions.** I'd see real noise, PNG sizes, and how a faint trail looks after the stretch, which would let me tune the streak score and the stacking.
- **The most useful test is a live capture.** Once you've updated the finder to this branch, a live capture with a visible satellite trail would carry matched stars, so the whole chain could be tested end to end: mapping, query, rails, dashes. Trimming it to the 20–30 frames around the trail would keep it small.

If you'd like, I'll build `--stack` now against the synthetic tests and check it on your file when it arrives.

---

### **User** — 2026-11-02 01:56:08

Go ahead.

---

### **Claude** — 2026-11-02 01:56:29

Building it. One thing first, so the stars line up in the stack: frames that didn't solve currently borrow the nearest solved frame's mapping. On a non-tracking mount the stars drift a few pixels in that time, so I'll interpolate between the solved frames before and after instead.

---

### **User** — 2026-11-02 01:57:23

Doesnt a calcuation of roll come out of the plate solve?

*(sent while Claude was working)*

---

### **Claude** — 2026-11-02 01:57:41

Now a test with real drift: the field shifts about 6 px per frame (exaggerated, so misalignment would show), stars stay fixed on the sky, and a satellite leaves a dash in each exposure.

Now a look at the actual composites:

I've added `--stack` to `tests/satcheck_overlay.py`. It's pushed to `claude/azimuth-on-0.11.74` (b57f89f) and tested only on synthetic frames so far, not a real capture.

**What it does:** for each group of frames at about the same pointing, it lines the frames up on the middle one and stacks them, as you described. It writes two images:
- `stack_N.png`: the brightest value at each pixel, so you see the stars plus every satellite dash.
- `stack_N_moving.png`: the same minus a median stack, so only what changed remains. The stars and sky glow drop out and the satellite dashes stand out.

Each predicted pass is drawn as two rails along its whole track. Beside the rails, a thick dash marks where the prediction puts the satellite during each exposure. If the real dashes are consistently ahead of or behind the predicted ones, that points to a stale TLE or the Pi's clock rather than a wrong satellite.

**Alignment:** each frame is mapped onto the middle frame through the sky, using the mappings fitted from the matched stars, so stars line up even though they drift on a non-tracking mount. I also changed how frames that didn't solve get a mapping. A frame between two solved frames now gets a mapping interpolated in time between them, which follows the drift. Before, it copied the nearest frame's mapping, which would have smeared the stars in the stack; frames at the very start or end still copy the nearest one.

**Test results:** I used a synthetic capture whose field drifts about 6 px per frame (exaggerated), with a satellite leaving a dash in every exposure.
- The interpolated mapping for the unsolved frame was within 0.3 px of the true one.
- Stars stacked sharply and vanished from the "moving" image, apart from faint edges left by the resampling.
- All six dashes appeared where predicted, with dark gaps between them, and the pass scored far above a decoy.

The full suite passes (590). I sent you the moving-image composite from that test.

To stack a longer stretch, widen the grouping: `python3 tests/satcheck_overlay.py livecap_….zip --stack --window 600`.

**On roll:** yes, the plate solve does produce roll. The finder records it for every frame, along with the centre RA/Dec and a mirrored flag, so roll was never missing. What's untested is how to apply it:
- **Which way it's measured:** the code documents roll as "North's angle counter-clockwise from image up", but nobody has checked that against the real sky. That's why `reproject_frame.py` still has an `--invert` switch for a one-time sign check.
- **How mirroring combines with it:** whether a mirrored frame is flipped before or after the rotation.

If either is wrong, the rails land in the wrong place. The matched stars avoid the question: fitting each frame's mapping to its own stars handles rotation and mirroring automatically and reports how well the fit lines up (rms in pixels). Roll is still the fallback for captures without matched stars.

A real debug bundle would let me settle the roll convention by checking it against your frames. A live capture with a satellite trail, made after you update the finder to this branch, would test the whole chain end to end.

---

### **User** — 2026-11-02 02:03:34

The device is still powered up. what command in an ssh session will update it to include what you have added? it is running off of a flashed 0.11.75 card.

---

### **Claude** — 2026-11-02 02:03:40

Run this over SSH:

```
sudo /usr/local/bin/diofinder-update --ref claude/azimuth-on-0.11.74
```

That switches the card from the v0.11.75 release to the branch with today's changes and restarts the finder. Reload the web page afterwards and the **Start live capture** button appears under the live view on Home. Saved captures show up under **Utilities → Live captures**.

The update brings these changes:
- The live capture button, which also records each frame's matched stars.
- The mount settle fix and the MAX3232 wording.
- The **Elevation (m)** setting on the Config page, under Observer Location. Set it once, since it's recorded into every capture.

The satellite scripts aren't meant to run on the finder. Run them on your own computer from a copy of the repository, after downloading a capture zip from Utilities. To go back to following releases later, run a plain `sudo /usr/local/bin/diofinder-update`.

---

### **User** — 2026-11-02 02:04:47

Have you changed how zips/files are downloaded so they dont tax CPU0 as much? Or is that a later tweak?

---

### **Claude** — 2026-11-02 02:04:54

Partly. The new live-capture downloads are already light on CPU 0, but the debug bundle hasn't changed; that's still item 4 on the next-release list.

**Live-capture archives:**
- **Built off CPU 0.** The recorder runs inside the solver process (CPUs 1–3), which saves the PNGs and zips them on stop.
- **No compression work at zip time.** The zip is stored uncompressed because the PNGs are already compressed, so closing it is just file copying.
- **Streamed from the card.** The download link sends the finished zip from disk in chunks; it never loads the archive into memory. CPU 0 only pushes the bytes out over Wi-Fi, which is unavoidable and cheap.

**The debug bundle ("Save diagnostics") is unchanged:**
- **Built in the web UI's memory on CPU 0.** It grabs a burst of frames and makes JPEG previews there.
- **Sent as one large response.** That's the slow, fragile download you saw in the field.

The fix is to build the bundle to a file in the background and offer a normal download link, the same way the live captures work. That's a contained change: say if you want it in this round, or I'll leave it on the list.

---

### **User** — 2026-11-02 02:12:35

[uploaded file: livecap_20261102T020749Z.zip — contents not saved] [uploaded file: diofinder_debug_20261101210835.zip — contents not saved]

---

### **Claude** — 2026-11-02 02:12:38

Inspecting both uploads (copied into their own empty folders first):

SatChecker is blocked from this sandbox (proxy 403), so the predictions will have to come from your machine. I can still stack your capture to see whether anything moved through the field:

Your capture caught a satellite: the stacked image I sent shows it as a dotted line. I couldn't name it, because SatChecker is blocked from this sandbox, so that last step needs to run on your machine.

**What the capture shows:**
- **When and where:** it was already in the field when you pressed Start and left through the top edge at **02:08:09.3 UTC**, so the trail ending isn't the object fading. It was seen in 68 of 82 frames.
- **Path:** from RA 61.08° Dec −1.70° to RA 54.54° Dec −5.34° (J2000), about 20° up in the east-southeast (azimuth 113° to 121°).
- **Speed:** 7.5° in 20.1 s, **0.37°/s**. That's what a low-orbit satellite looks like at about 20° altitude, so it's likely Starlink or something similar.
- **Brightness:** it swings between roughly 60 and 210 from one 0.3 s exposure to the next. When bright it's a short, intense dash; when faint it's a dim full-length streak. That pattern suggests a flashing or tumbling object, such as a rocket body or a dead satellite. At 0.3 s sampling I can't pin down the flash period.

**What the capture confirms about the tools:**
- **Frame mapping:** every frame solved, and the mappings fitted from the matched stars are accurate to 0.64 px typically (2.0 px at worst).
- **Roll:** the documented convention is right. A mapping built from roll, centre and field of view matches the star-fitted one to within 3.5 px at the frame corners; the opposite sign is off by about 1000 px. So `reproject_frame.py`'s default is correct too.
- **Stacking:** on your real, drifting frames the stars cancel cleanly and the satellite comes out as one dash per exposure. Stacking all 82 frames took 8 s.

**To identify it, run this from the repo on your computer:**
```
python3 tests/satcheck_overlay.py livecap_20261102T020749Z.zip --stack --chart
```
If SatChecker has it, its rails should bracket the dotted line in `stack_1_moving.png`, and each predicted dash should sit beside a real one. A pass that clearly runs along the line but leads or trails the dashes means a stale TLE or a clock offset. `report.csv` lists every predicted crossing. Elevation was 0 in this capture; for a satellite this low it hardly matters, but set it on the Config page (330 m from your script) for future captures.

**A bug the debug bundle exposed:** all 12 frames in it have no solve result attached, even though the finder was solving at the time. With 0.3 s exposures the solver moves on before the web page asks for a frame's result, so every lookup misses. Live capture isn't affected, since it pairs results inside the solver. The fix is for the solver to keep the last few results and look each one up by frame number. I'd do it together with building the debug bundle to a file instead of in memory on CPU 0; want both in this round?

---

### **User** — 2026-11-02 02:28:11

No, keep stacking up the things to do.  I believe that satellite was STARLINK 30319.  whats the easiest way to get the livecapture script and that python script ontop my network-connected windows laptop?

---

### **Claude** — 2026-11-02 02:28:24

Noted. STARLINK 30319 is a good candidate, and its rails should bracket that dotted line if SatChecker agrees. The debug-bundle fixes are on the to-do list with the rest.

The easiest way onto a Windows laptop:

**1. Get the two scripts.** You need both `satcheck_overlay.py` and `satcheck_archive.py`, in the same folder. They don't need anything else from the diofinder repo. The simplest route is the browser: while signed in to GitHub, open each file on the branch and click **Download raw file** (the download-arrow icon at the top right of the file view):
- `https://github.com/mconsidine/diofinder/blob/claude/azimuth-on-0.11.74/tests/satcheck_overlay.py`
- `https://github.com/mconsidine/diofinder/blob/claude/azimuth-on-0.11.74/tests/satcheck_archive.py`

Save both to the same folder, e.g. `C:\satcheck`. If you'd rather keep them updatable and have Git installed:
```
git clone -b claude/azimuth-on-0.11.74 https://github.com/mconsidine/diofinder.git
```
Then use `diofinder\tests\` as the folder.

**2. Get the capture.** With the laptop on the finder's network, open **Utilities → Live captures** in the browser and click the archive name to download it into the same folder. If the finder has joined your home Wi-Fi, the laptop can reach the finder and SatChecker at the same time. In AP mode, switch the laptop back to the internet after downloading.

**3. One-time Python setup.** Install Python from python.org (tick "Add python.exe to PATH" during the install), then in a Command Prompt:
```
py -m pip install numpy astropy matplotlib pillow
```

**4. Run it** from that folder:
```
cd C:\satcheck
py satcheck_overlay.py livecap_20261102T020749Z.zip --stack --chart
```
Results go to `livecap_20261102T020749Z_sats\`. Open `stack_1_moving.png` and `report.csv` there.

---

### **User** — 2026-11-02 02:37:38

I want to run those scripts off-line from within pycharm.  How can I add the zip filename and set --stack and --chart without altering the script too much

---

### **Claude** — 2026-11-02 02:37:45

You don't need to change the script at all. PyCharm can pass the arguments for you:

1. **Run → Edit Configurations…** → **+** → **Python**.
2. **Script path:** `C:\satcheck\satcheck_overlay.py`
3. **Script parameters:** `livecap_20261102T020749Z.zip --stack --chart`
4. **Working directory:** `C:\satcheck` (the folder holding the zip), so the bare filename is found.
5. **Python interpreter:** the one you installed numpy/astropy/matplotlib/pillow into.

Then click **Run**. To try another capture, change only the parameters field.

If you'd rather put the settings in the file, change the last two lines from:
```python
if __name__ == "__main__":
    main()
```
to:
```python
if __name__ == "__main__":
    main(["livecap_20261102T020749Z.zip", "--stack", "--chart"])
```
`main()` already accepts that list, so nothing else changes. Use a full path, such as `r"C:\satcheck\livecap_20261102T020749Z.zip"`, if the working directory is somewhere else.

The first run needs internet to fetch predictions from SatChecker. It saves them to `satchecker_responses.json` in the output folder. After that you can add `--offline` and re-run with no connection, for example to try other `--frames` or `--window` settings.

---

### **User** — 2026-11-02 02:48:41

[uploaded file: report.csv — contents not saved] It ran — here's the report.csv output

---

### **Claude** — 2026-11-02 02:49:54

The report doesn't confirm STARLINK 30319, but it turns up a close match with a puzzle attached:

**No STARLINK 30319 in the prediction.** That name doesn't appear anywhere in SatChecker's 111 objects for this field and window. The nearest name is **STARLINK-30913 (58388)**, the same digits rearranged.

**STARLINK-30913 matches your trail, except for its direction:**

| | Your trail (measured) | STARLINK-30913 (predicted) |
|---|---|---|
| Column in the frame | x ≈ 652–662 | x ≈ 650–654 |
| Speed | ~26 px/s (0.37°/s) | ~25 px/s |
| Frame 1 position | y = 533 | y = 596 |
| Direction | **up** the frame, leaves the top at frame 68 | **down** the frame, leaves the bottom at frame 23 |

Same line and same speed is a strong coincidence, but the prediction runs the opposite way. A clock or stale-TLE error would shift it along the line, not reverse it. My overlay code passed a direction test on synthetic data, so I don't think it's flipping things. I can't be sure which explanation is right without the raw prediction.

**Most of the 9,102 rows are noise.** About 40 of the 51 objects predicted inside the frame barely move during the capture. Many are geostationary satellites and old rocket stages: this field (Dec −0.6°) sits right on the geostationary belt as seen from Vermont. They're far too faint for 0.3 s exposures, and their 6–7σ scores are just the scoring's baseline. Only 13 objects move fast enough to streak, and none of them scores high, because none of the predicted streaks lands on your trail. I'll add a filter to hide the near-stationary objects and calibrate the score against that baseline; both go on the to-do list.

**To settle it, could you upload these from the output folder?**
- `satchecker_responses.json` (the raw predictions). With it I can compare STARLINK-30913's predicted RA/Dec and times directly against the measured trail.
- `stack_1_moving.png`, to see where the rails fall.

Also, where did "30319" come from: another app such as Heavens-Above, or memory? If it came from an app that showed a direction of travel, that would help too.

---

### **User** — 2026-11-02 02:49:57

I dont think the predictions worked real well:

---

### **User** — 2026-11-02 02:49:57

[image attached — not saved]

---

### **Claude** — 2026-11-02 02:50:46

You're right, they didn't work well. Several separate problems show up in that image:

**1. The real trail has no matching prediction, and STARLINK-30913 is suspicious.** Its rails (yellow-green, bottom centre) start right where your trail starts, run along the same column at the same speed, and go the opposite way: down and off the bottom instead of up and off the top. A clock error or stale TLE would slide the prediction along the line; it can't reverse it. So either SatChecker's time tags for that object are reversed, or it really is a different, invisible satellite on the same line by coincidence. The coincidence seems unlikely, but I can't tell which from the image alone.

**2. Most predicted objects can't be seen.** Many are geostationary satellites and rocket stages: from Vermont this field sits on the geostationary belt, so they show as short dashes in place. They're magnitude 10–14, far too faint for a 0.3 s exposure. Others are low-orbit objects that are probably in Earth's shadow or too faint. SatChecker lists everything inside the circle whether it's visible or not.

**3. The score is fooled by tracks that cross the bright trail.** FALCON 9 R/B (69σ), FENGYUN 1C DEB (35σ), STARLINK-34739 (24σ) and STARLINK-2415 (22σ) all score high only because their rails cross your trail. The score averages brightness along the line, so one bright crossing dominates.

**Adding to the to-do list:**
- Score using the median along the line, not the mean, so a single crossing can't dominate.
- Hide objects that barely move during the capture, and those SatChecker marks as unlit, if its response includes that.
- Label each pass with the direction it was predicted to travel.

**To settle item 1, I still need:**
- **`satchecker_responses.json`** from the output folder. With it I can line up 30913's predicted RA/Dec and times against your measured trail point by point. If the times run backwards, that will show.
- **How the finder got its time that night.** Was it on your home Wi-Fi (network time), or set by SkySafari? The capture recorded "clock plausible", but that only rules out a clock that's years off, and the bundle's log is too short to show the source. A few seconds' error wouldn't reverse a track, but it does affect the along-track fit.
- **Where "STARLINK 30319" came from.** If it was an app that showed the direction of travel, compare it with the trail moving up the frame, which is toward the south-southwest on the sky.

---

### **User** — 2026-11-02 02:53:34

[uploaded file: satchecker_responses.json — contents not saved] The time came from SkySafari.  I jumbled the STARLINK name, so I think it should have been 30913

---

### **Claude** — 2026-11-02 02:55:21

I don't think the trail is STARLINK-30913, or any satellite. I think it's an aircraft. Three independent reasons:

1. **It moves the wrong way.** SatChecker's own numbers put 30913 at RA 61.14° Dec −1.51° at 02:07:47, heading toward RA 67° Dec +2°: north-east, and sinking (alt 19.5° → 17.9°). Your trail starts within about 0.6° of that spot but heads south-west and climbs (RA 61.09° → 55.28°, Dec −1.70° → −4.94°). A satellite can't run backwards along its orbit. A stale TLE or a clock error would only slide the prediction along the line, and the coordinate frames check out, so the prediction isn't wrong; the trail is something else. With 13 moving objects predicted in the field, one passing near the trail's start by chance is about a one-in-ten coincidence.

2. **Nothing at that height was sunlit.** The Sun was 47° below the horizon at 21:08 local. At its predicted position (457 km up, 1,142 km away), 30913 was deep in Earth's shadow and unlit. The same is true of any low-orbit satellite in that part of the sky. Only something thousands of kilometres up could be sunlit there, and anything that high moves far slower than 0.37°/s.

3. **The light looks like an aircraft's.** In alternate frames it's either a dim full-length streak (steady light) or a short, bright dash (a flash shorter than the 0.3 s exposure). The flashes come about every 0.6 s, roughly 100 per minute, which is the top of the 40–100 per minute required for anti-collision strobes. The steady streak would be the navigation lights. An airliner some tens of kilometres away moves at about 0.37°/s at that elevation, and transatlantic traffic heading toward New York passes south-east of Vermont, flying south-west.

So the tools did their job: the finder's measurements, the frame fitting and SatChecker's predictions all agree. Your trail simply isn't in the satellite catalogue.

The capture also shows the biggest improvement to make. Almost every prediction drawn was either unlit (in Earth's shadow) or a magnitude 10–14 object far too faint for 0.3 s exposures. Adding to the to-do list:
- **Hide satellites in Earth's shadow.** I'd use the same calculation as above; SatChecker's response doesn't say whether an object is lit. For a capture like this, that removes nearly all the low-orbit clutter.
- **Optionally hide geostationary and other near-stationary objects.**
- **Flag a detected trail that matches no prediction,** with its rate and flash pattern, as "unmatched (aircraft?)".

Good captures for testing would be at dusk or dawn, roughly 1–2 hours from sunset or sunrise, when low-orbit satellites are still sunlit. A Starlink train or the ISS would make a clear check.

---

### **User** — 2026-11-02 02:57:16

I think I know what is wrong.  The diofinder is mounted on the *side* of the telescope, not on the top.  Ie it is mounted 90 degrees clockwise from "top". So "up" in the field of view is basically "to the right".  Does that help?  I would have thought the plate solve and calculated roll would have pointed you to that, to the extent it matters.

---

### **Claude** — 2026-11-02 02:57:27

Thanks, but the mounting doesn't change the conclusion. You're right that the plate solve accounts for it, and it already did.

- **I compared in sky coordinates, not image directions.** Each frame's pixels are converted to RA/Dec using a mapping fitted to that frame's own matched stars. The camera's rotation (the solved roll of 120°, which includes your sideways mounting), any mirroring, and the slow drift are all built into that mapping. "Up the frame" was only my shorthand. The actual comparison was RA 61.09° → 55.28° and Dec −1.70° → −4.94° for the trail, against RA 61.14° → 67.81° and Dec −1.51° → +2.53° for STARLINK-30913.
- **Rotation can't reverse a direction.** Both the trail and the prediction are drawn through the same mapping on the same frame, so any rotation would turn both together. A trail and a prediction running opposite ways along the same line would stay opposite at any camera angle. A mirror flip couldn't cause it either: a wrong mirror setting would have stopped the star fit working, and the stars fit to 0.64 px.
- **The shadow check doesn't use the image at all.** It uses only SatChecker's own position for the satellite and the Sun's position. That satellite was deep in Earth's shadow, so it was unlit whichever way the camera faced.

So the trail still looks like an aircraft. Where the sideways mounting does matter is in reading the finder's images by eye: "up" in the frame isn't up on the sky. `reproject_frame.py` can rotate a frame to north-up, east-left, and we now know its roll convention is right.

---

### **User** — 2026-11-02 02:59:11

I'll agree with the aircraft idea.

---

### **Claude** — 2026-11-02 02:59:17

Agreed. For a fair test of the satellite chain, capture within about 1–2 hours of sunset or sunrise, when low-orbit satellites are still sunlit, ideally during a predicted ISS or Starlink pass.

The running list for the next round, nothing started yet:

**Satellite tools**
- Hide predictions that are in Earth's shadow (the calculation I just used).
- Optionally hide geostationary and other near-stationary objects.
- Score using the median along the line, so a track crossing a bright trail can't score high.
- Label each prediction with the direction it's expected to travel.
- Flag detected trails that match no prediction, with their speed and flash pattern, as possible aircraft.

**Finder**
- Make the debug bundle look up each frame's solve result by frame number from a short list the solver keeps. At 0.3 s exposures the current lookup misses every frame.
- Build the debug bundle to a file in the background and serve a normal download link, so it stops loading CPU 0.
- Plus the items from the v0.11.75 list, starting with requiring CPU 0 to stay busy across several readings before the headroom card says "Tight", and adding `ap-watchdog.sh` to the update refresh list.

Say when you want any of it started.

---

### **User** — 2026-11-02 12:11:40

From what you have seen, what are the biggest contributors to load on CPU 0?

---

### **Claude** — 2026-11-02 12:12:06

I haven't seen a per-process breakdown of CPU 0 yet, so this ranking comes from the code and the symptoms rather than from profiling.

**What I've actually measured:**
- **Bench, test mode:** CPU 0 peaked at about 42–48%. The highest reading came at about 1 min 30 s after start-up.
- **Field:** a single reading of 94% (around 22:40) is what turned the card to "Tight". It happened around the time of the "frame not available" and failed debug-zip glitches, though you didn't recall exactly when those occurred.

**Likely biggest contributors, in rough order:**

1. **Building the debug bundle.** It runs entirely inside the web UI on CPU 0: a 12-frame burst of PNG encodes, two stretched JPEGs and a zip, all in memory. It's the heaviest single job on that core and the most likely cause of the 94% spike. It's already on the to-do list.
2. **The live view (`/frame.jpg`).** Every 1.25 s per open page, the web UI stretches a frame and encodes a JPEG. The code itself calls this the heaviest regular web UI load on CPU 0. Two pages or tabs showing the live view double it.
3. **The shared-settings server that every process talks to.** Each read or write of the shared settings or the latest solution is a round trip to that server, which runs on CPU 0. The IMU reader alone writes 20 times a second. On top of that, each frame brings a write from the camera and a publish plus a snapshot from the solver. At 0.3 s exposures that's about 3 frames a second, plus polling from comms.
4. **SkySafari's reconnects.** Each poll is a new connection, 11–30 every 5 s in your logs. The worker pool made each one cheap, but they still cost the kernel and Python some time.
5. **The IMU reader.** It reads the BNO055 over I²C at 20 Hz in Python, inside the launcher process.
6. **Wi-Fi and the kernel.** Interrupts and the network stack, which work harder in AP mode while serving the phone and SkySafari at the same time.
7. **Web page pollers.** Status (every 3 s), sysmon, the live-capture status (every 2 s) and others, each multiplied by the number of open tabs.

**Cheap things you can try now:**
- Keep only one live-view tab open.
- Lower SkySafari's readout rate.
- Set **IMU poll rate** (`imu_poll_hz`) to 10. The docs say 5–10 makes no visible difference to pointing.

**To measure instead of guess,** run this over SSH during a normal session:
```
pidstat -t -u 5 6 | awk 'NR<4 || $9>0.5'
```
`pidstat` comes from the `sysstat` package (`sudo apt install sysstat`) if it isn't there. It lists CPU use per thread every 5 s, so the web UI, comms, the IMU thread and the settings server show up separately. Then try one 12-frame debug bundle and one browser reload while it's running. Send me the output and I can turn this into a measured ranking and pick which fix to do first.

---

### **User** — 2026-11-02 12:16:27

Do I really need two pages or tabs showing the live view?  Does that IMU reader cycle need to be that fast?  What counts as an "open" tab?  I thought as I moved between tabs only one was open at a time.

---

### **Claude** — 2026-11-02 12:17:02

You're right about moving between pages within one tab. The catch is separate browser tabs, and a second device.

**What counts as "open":** any page that's loaded in a browser, whether or not you're looking at it.
- **Same tab:** clicking Home → Camera → Utilities replaces the page, so only the page in front of you is polling. That's fine.
- **Separate browser tabs:** each loaded page keeps its timers running. The live-view code on Home and on the Advanced page doesn't pause when its tab is hidden; only the new live-capture status check does. Desktop browsers slow hidden tabs down (to about once a second, then about once a minute after five minutes) but don't stop them. Phone browsers usually freeze background tabs, especially with the screen off.
- **A second device counts too.** A laptop and a phone both on the finder means two live views.

So no, you don't need two live views. The live view appears on both Home and Advanced, but it only costs double when both are loaded at once. **Adding to the to-do list:** make every page pause its polling while its tab is hidden. It's a small change and removes the background-tab cost entirely.

**IMU rate:** no, 20 Hz is more than needed. SkySafari asks for the position about 4 times a second, the solver produces about 3 solves a second at 0.3 s exposures, and the slew detector compares samples about 0.8 s apart. 10 Hz halves the IMU's traffic through CPU 0, and 5 Hz quarters it. The cost is slightly coarser slew detection and a slower-settling fit of how the IMU is mounted relative to the camera; pointing shouldn't look any different.

There's already a control for it:
- **Advanced page** (Expert view) → IMU section → **IMU sample rate**, with choices 5, 10 or 20 Hz. It only appears while the IMU sensor is switched on.
- It takes effect immediately, no restart, and is saved to the config.

I'd start at **10 Hz** and watch whether the crosshair during slews looks any different. If it doesn't, 5 Hz is fine for a hand-moved finder.

---

### **User** — 2026-11-02 12:19:04

what is the load if my phone has a browser session open and a laptop has an ssh session, with the diofinder on a local network rather than in AP mode?

---

### **Claude** — 2026-11-02 12:19:17

I can only estimate this; I haven't measured these combinations. Roughly:

**SSH session on the laptop:** close to nothing. An idle SSH session only wakes up when you type or something prints. Leaving a live display running costs a little: `top` refreshing every few seconds might take a few percent of CPU 0, and `journalctl -f` very little unless the log is busy.

**Phone browser:** depends on which page it's showing.
- **Home or Advanced, with the live view:** this is the main cost. Every 1.25 s the web UI stretches a frame and encodes a JPEG, plus the smaller status pollers. Expect it to be the largest share of CPU 0 load.
- **A page without the live view (Utilities, Config, Wi-Fi):** only small status polls every few seconds, which is light.
- **Phone screen off or browser in the background:** phones usually freeze the page, so close to nothing.

**Home network vs AP mode:** little difference either way.
- **AP mode:** most access-point work happens inside the Wi-Fi chip's own firmware. The Pi only adds DHCP and DNS for connected devices, which is negligible.
- **Home network:** the Pi also sees your network's background chatter: other devices' broadcasts, Bonjour/mDNS, ARP. It answers mDNS for `diofinder.local`. On a typical home network that's small, and it's not worth choosing a mode for load reasons.

So in your scenario the load is mostly whatever the phone page is doing: the live view if it's on Home, very little otherwise.

To get real numbers, the headroom card on Utilities shows CPU 0 and per-process CPU live. Or, more precisely, run this over your SSH session:
```
pidstat -u 5 12
```
Run it once each with the phone on **Home**, on **Utilities**, and with the **screen off**. Comparing the three shows exactly what the live view costs.

---

### **User** — 2026-11-02 12:21:36

add pausing hidden tabs to the to-do list.  and add changing the IMU default polling rate to 10 instead of 20

---

### **Claude** — 2026-11-02 12:21:42

Both are on the list. The full to-do list now:

**Finder**
- Pause all page polling (live view and status) while a browser tab is hidden.
- Change the default IMU sample rate from 20 Hz to 10 Hz. Devices that already have a rate saved in their config will keep it.
- Make the debug bundle look up each frame's solve result by frame number from a short list the solver keeps; at 0.3 s exposures the current lookup misses every frame.
- Build the debug bundle to a file in the background and serve it as a normal download link, instead of in memory on CPU 0.
- Require CPU 0 to stay busy across several readings before the headroom card says "Tight".
- Add `ap-watchdog.sh` to the files the OTA update refreshes.
- The rest of the v0.11.75 list: cap the `bg_runs` folder, the SkySafari readout-rate hint, a test-suite CI workflow, a "promote tested image" release mode, the clock and set-time button, and the boot traceback.

**Satellite tools**
- Hide predictions that are in Earth's shadow.
- Optionally hide geostationary and other near-stationary objects.
- Score using the median along the line, not the mean.
- Label each prediction with the direction it's expected to travel.
- Flag detected trails that match no prediction, with their speed and flash pattern, as possible aircraft.

Nothing has been started. The list lives only in this conversation, so say if you'd like it saved as a file in the repo.

---

### **User** — 2026-11-02 12:31:10

save it as a file in the repo.  then build an artifact that incorporates all this and can serve as a base for the next release.  or do you have any suggestions for other things to do?

---

### **Claude** — 2026-11-02 12:31:29

Saving the list first:

Going with the quick fixes, then the build. First the four new ideas go on the list:

Doing the IMU default and the OTA list fix:

The hunting filter blends a fixed fraction per sample, so halving the sample rate would double its lag. I'll scale it so the time constant stays the same at any rate:

All pages share the header. Checking for pollers built on `setTimeout` chains instead, which the guard wouldn't catch:

Works: 4 frame requests per 5 s while visible, none while hidden, and an immediate refresh when the tab comes back. Next, the headroom card's CPU 0 rule:

Implementing the sustained CPU 0 rule, matching how RAM and swap already work:

Updating the card's explanation to match:

Last quick fix: the debug bundle's frame-to-solve lookup. Looking at how an existing solver command (`solve_stats`) is wired end to end so I can mirror it:

Adding tests for the history, the solver lookup and the comms command:

Updating the docs and marking the five items done in the to-do file:

Pushed. Now starting the untagged image build from the branch, with the same solver pins as v0.11.75 (olive-solve v0.1.11, sycamore v0.14.2):

Build #192 is queued on e5b3dd9. The last one took about 38 minutes, so I'll check back then:

I've saved the to-do list in the repo, done five of the quick fixes, and started building an image from the branch for the next release.

**To-do list:** it's in `docs/next-release-todo.md`, including your four additions (record the clock source in live captures, matched stars in debug bundles, bump the version to 0.11.76, live capture time limit). The five items below are marked done there.

**Quick fixes** (commit e5b3dd9; full test suite passes, 597):
- **Hidden tabs stop polling.** One guard in the shared page header covers every page. I checked it in a headless browser: 4 live-view requests per 5 s while visible, none while hidden, and an immediate refresh when the tab comes back.
- **IMU sample rate defaults to 10 Hz.** I also adjusted the hunting filter so its smoothing lag in seconds stays the same; at 20 Hz it behaves exactly as before. A freshly flashed card gets 10 Hz. A card updated over the network keeps the 20 Hz saved in its config until you change it on the Advanced page.
- **Headroom card:** CPU 0 must now stay at 90%+ for about 15 s before the card says "Tight", and at 98%+ for about 15 s before "Constrained". A single busy reading no longer counts.
- **Network updates now refresh `ap-watchdog.sh`.**
- **Debug bundles attach each frame's own solve result again.** The solver now keeps its last 48 results so the web UI can look each frame up afterwards. This needs the matching finder software, which this image has.

**Image build:** run #192, untagged, so the image is only a workflow artifact. It uses the same solver versions as v0.11.75 (olive-solve v0.1.11, sycamore v0.14.2): https://github.com/mconsidine/diofinder/actions/runs/41446634919. It's queued now; the last build took about 38 minutes, and I've set a check for about 45 minutes from now to confirm it and give you the download link. The artifact is kept for 14 days.

Not yet done, and still on the list:
- Building the debug bundle to a file instead of in memory.
- The satellite-tool fixes.
- The version bump, so the in-code version still says 0.11.75.

---

### **User** — 2026-11-02 13:14:30

Is there a reason to not do the remaining items once this artifact is done?  It seems like all of these are low-risk.  Thoughts?

---

### **Claude** — 2026-11-02 13:14:50

Most of them, yes, but not all. A few need hardware, field data or a decision from you, and no amount of care on my side changes that.

**Safe to do now** (contained, testable here, easy to back out):
- **Version bump to 0.11.76.**
- **SkySafari readout-rate hint** (wording only).
- **Cap the `bg_runs` folder.**
- **Live capture:** time limit, and recording the clock source (when and how the time was last set).
- **Debug bundle built to a file with a download link.** Web UI only, but it's the largest of these changes.
- **Matched stars in debug bundles.** The solver would have to return matches briefly during a bundle, the same mechanism live capture already uses.
- **Test-suite CI workflow.** A new file only. The one unknown is whether this session is allowed to push workflow files at all.
- **"Set time from this phone" button.** A helper for this already exists; it needs a narrow sudo permission like the power buttons.
- **All the satellite-tool items** (shadow filter, hiding stationary objects, median score, direction labels, flagging unmatched trails). These run on your laptop, so there's no risk to the finder at all.

**Not "just do it":**
- **Boot traceback fix:** it changes camera start-up, which I can't test without the camera. A mistake there means no frames.
- **"Promote tested image" release mode:** it changes how releases are cut. That's low risk, but I can't fully test it until we actually cut a release.
- **Restoring the clock across reboots:** needs a look at the device first.
- **FOV drift and the default-off features:** need more field data by definition.
- **IMU report smoothing:** was deliberately shelved and is a big job.
- **Test-mode defaults:** your decision, since it would publish a location.

**One process point:** with a dozen changes in one image, a problem in the field is harder to pin on one change. I'd keep one commit per item, so any single change can be backed out on its own, and you'd test the new image before it becomes a release.

The build running now is locked to its own commit, so I can start this work immediately without affecting it. Want me to go ahead with the "safe" group?

---

### **User** — 2026-11-02 13:23:10

Go ahead with the "safe" group.  If this session cannot push workflow files do not stop working on the list but move that to the "not 'just do it'" group.  For that group, it seems that only IMU report smoothing, FOV drift and restoring clock should be a priority.  I'm unclear what the "boot traceback fix" provides.  "Promote tested image" seems like a convenience.  And I think think the test-mode default should just use the current location unless it is overwritten - ie it shouldnt be 0/0.  So after the current artifact successfully completes, I would like what I just described for the "not 'just do it'" group built out into a new artifact.  Presumably *that* artifact would be the candidate for the next tagged/labeled release.  Opinion?

---

### **Claude** — 2026-11-02 13:23:37

Going ahead with the safe group now, one commit per item. Quick answers first, since they shape the second batch:

- **Boot traceback fix:** purely cosmetic. Every boot, the camera's first attempt to load the tuning file fails harmlessly, logs a scary-looking traceback, and the fallback works. Fixing it only cleans the log, so I'd drop it.
- **Promote tested image:** agreed, a convenience. Deferring it.
- **Test-mode location:** I'll make test mode use your configured site unless you've set test values yourself, with the old 40°N/75°W placeholder only if neither is set. It's never 0/0.
- **Opinion on the candidate:** yes, that second image should be the release candidate, with one caveat. IMU report smoothing changes what SkySafari sees during every slew, and it was shelved on purpose. I'd ship it behind a switch, default off, so the candidate behaves like today's finder until you've tried it and turned it on. FOV drift and the clock should be fine as normal changes once I've looked at them.

Starting with the version bump:

Next, the SkySafari readout-rate hint. Finding where the web UI already talks about SkySafari:

The most useful form of this hint is a live one: show the SkySafari connection rate on the headroom card, with advice when it's high. Wiring that:

The code is right; my test miscounted (the poll at exactly 5.0 s still falls in the window, so 21). Fixing the test:

Next, capping the Background A/B archive folder:

Next, the two live-capture items: a time limit, and recording where the clock's time came from. Looking at how the clock gets set today:

Tests for the clock record and the time limit:

Next, the bigger one: building the debug bundle to a file in the background. Looking at the current button, its script and the route:

Now the refactor:

Now the button script: start the job, poll, then download through a normal link:

Tests for the background bundle routes, pruning and the matched-star plumbing:

Checking that the real build function runs end to end after the refactor, with no daemon (it should still produce a bundle):

The full build path works (7.8 s with no camera; no leftover temp file). I'll commit the two items separately, splitting `app.py`'s changes between them:

Next, the test-suite CI workflow. Writing it, then seeing whether this session can push workflow files:

The clock summary assumed SkySafari was the only manual source; generalizing it for the phone:

Pushing, then on to the satellite-tool items. Those run on your laptop, and I can test them against your real capture and SatChecker response:

*(The session's context was compacted here; the conversation continued.)*

---

### **Claude** — 2026-11-02 13:36:42

Arrows and the drawing of unpredicted trails are in. Now wiring the CLI flags, report columns and `trails.csv` into `main()`.

Update the docstring, then run on real data.

On its own, the tool now rejects STARLINK-30913 because it moves the opposite way (predicted NE, the trail runs SW). That matches the earlier hand analysis. Let me check the image.

The label for the unidentified trail sits in the header strip and is hard to read. I'll move it beside the trail's midpoint and add an arrow for its measured direction.

The arrow fell off the top edge of the image, and the header line gets cut off. I'll place the arrow beside the trail's midpoint and split the header into two lines.

Drawing looks right. Now the tests.

Now the doc updates: the to-do file and CLAUDE.md.

Pushed. Next I'm checking the new CI workflow and image build #192.

No response requested.

---

### **User** — 2026-11-02 13:50:40

Go ahead with the "safe" group.  If this session cannot push workflow files do not stop working on the list but move that to the "not 'just do it'" group.  For that group, it seems that only IMU report smoothing, FOV drift and restoring clock should be a priority.  I'm unclear what the "boot traceback fix" provides.  "Promote tested image" seems like a convenience.  And I think think the test-mode default should just use the current location unless it is overwritten - ie it shouldnt be 0/0.  So after the current artifact successfully completes, I would like what I just described for the "not 'just do it'" group built out into a new artifact.  Presumably *that* artifact would be the candidate for the next tagged/labeled release.  Opinion?

---

### **Claude** — 2026-11-02 13:50:53

Build #192 succeeded, and the new Tests workflow passed on all three pushes. Next I'll start the release-candidate group you asked for, beginning with the test-mode location default.

Implementing the test-mode site rule. Explicit `test_*` values win, then the configured site, then a 40N/75W placeholder, and never 0/0.

Test-mode site is done and tested. Committing, then moving on to clock restore.

Clock work is nearly done: boot-aware source reporting, direct clock setting, and save/restore of the last known time. Fixing one test, which must also never touch this machine's real clock.

Clock work is pushed. Next item is FOV drift: why the committed FOV re-commits several times per session.

Three of the four items are done. Now the big one: IMU report smoothing, behind a default-off switch. Reading the shelved design first.

Writing the pure filter module now. Then I'll wire it into comms, the IMU thread (sample history only when the switch is on) and the solver (mid-exposure reference).

Now the tests: a simulated slew, parked convergence, a pan without IMU, the snap case, and the timing helpers.

Full suite passes (645). Updating the docs, then committing and starting the release-candidate image build.

Pushed. Before starting the build I'll check the release workflow's inputs so the pins match build #192.

Build #193 is queued (~30 min). I'll schedule a check-in to report when it finishes.

Both artifacts are on their way. Build #192 passed, and the release-candidate image (build #193) is building now. I'll report back when it finishes, around 16:19 UTC.

**Base image (build #192, passed):** https://github.com/mconsidine/diofinder/actions/runs/41446634919 — the download is `diofinder-sycamore-image-manual-20261102-1240`, about 1 GB, and it expires Nov 16. The safe group is finished. The test workflow pushed fine and passes on every push, so it stayed in the safe group.

**Release candidate (build #193, building):** https://github.com/mconsidine/diofinder/actions/runs/41474282361 — the four items you prioritized. All 645 tests pass.

- **Clock — an important find:** the main service runs with `NoNewPrivileges=true`, which blocks `sudo`. So SkySafari's clock sync, which went through a sudo helper, very likely never worked from the daemon. Whenever your time looked right, it probably came from network time. I couldn't confirm this from your bundle because its log was too short. The fix:
  - The service may now set the clock directly (`CAP_SYS_TIME`).
  - SkySafari now corrects the clock when it is 2 s or more off (was 10 s). It never overrides network time, and it needs 10 s to override a time set from your phone.
  - A sync record from before the last power-off no longer counts as the current time source. Live captures will say "NOT SET since start-up" instead of quoting yesterday's SkySafari sync.
  - The finder saves the time every 5 minutes while it is known to be right. At start-up it moves a clock still at the image build date forward to that saved time. This is flagged as not accurate; it only keeps logs and files in order.
- **FOV drift:** your logs showed four recalibrations in one evening (13.540 → 13.504 → 13.538 → 13.599 → 13.536). Each check jumped to the latest 30-solve median, chasing about ±0.05° of field-to-field wander. One jump landed 0.06° off, which used most of the ±0.1° search window. It now recenters on the median of the last few window medians and keeps the tight search window throughout. A real shift still corrects itself within about 150 solves.
- **Test-mode location:** uses `test_latitude_deg`/`test_longitude_deg` if you set them, otherwise your real site, otherwise 40N/75W. It is never 0/0. Old devices whose config still holds the 40/-75 placeholder get their real site.
- **Smooth crosshair (off by default):** a toggle on the Advanced page, "Smooth crosshair (experimental)". With it off, behaviour is exactly as before. With it on:
  - Each new solve pulls the crosshair part of the way instead of replacing it.
  - The IMU carries the motion between solves.
  - A consistent IMU error is learned and cancelled. In simulation, a 10% IMU error otherwise left a steady 0.2–0.3° lead.
  - Each solve is paired with the IMU reading from the middle of its exposure.
  - When parked, small solve-to-solve jitter is averaged and the crosshair always settles exactly on the solves.

  In a simulated 2°/s slew, today's path jumps 0.1° at every solve, while the smoother settles under 0.02° of error. That is simulation only. To judge it, watch SkySafari during slews and at rest with the toggle on, then off.

I suggest tagging #193 as the release once the smoother has had that A/B on sky. Because it ships off by default, the release is safe to tag even before then. The to-do list in `docs/next-release-todo.md` is updated to match.

That notification was the old check-in for build #192, which I'd already reported above (passed; download link at https://github.com/mconsidine/diofinder/actions/runs/41446634919).

One point it carried that I hadn't mentioned: a freshly flashed card gets the new 10 Hz IMU sample rate. A card updated over the air keeps its saved 20 Hz until you change it on the Advanced page. That applies to the #193 release candidate too, and its check-in is still scheduled for about 16:19 UTC.

---

### **User** — 2026-11-02 15:48:03

When this is done, I'd like a list of features that are/were included for testing and which could arguably be hidden or dropped because they unnecessarily take up resources. e.g are there bundle creation exercises that are redundant? Or things included as A/B tests that havent been decided upon one way or the other. Or, an assessment that while the webui cards/pages may look cluttered, they aren't take up resources that are better used elsewhere.  Also provide a list of any remaining items being considered or should be considered for inclusion.

---

### **Claude** — 2026-11-02 15:48:16

Build #193 is still running; its check-in is scheduled for about 16:19 UTC. Meanwhile I'll gather the facts for the assessment, starting with what runs all the time versus only when used.

Still building the inventory, and build #193 hasn't reported yet. Next: listing every default-off and experimental setting from the config, and finding which harnesses and pages duplicate each other.

I have what I need for the assessment. Checking where build #193 stands, since you asked for this once it's done.

Build #193 is still running; its scheduled check-in will report when it finishes. The assessment didn't need to wait for it.

**In short:** the web pages and their cards cost nothing unless you're looking at them. The real steady load comes from a few features that are on by default. What can be dropped is mostly things waiting on an A/B test that was never run, plus a few tools that overlap.

## 1. What costs resources even when nobody is looking

These run with no browser open. Each one is in use, so nothing here is a clear "drop".

| Item | Cost | Verdict |
|---|---|---|
| SkySafari polling | Each poll is a new connection, handled on CPU 0. This is the largest CPU 0 load while connected. | Keep. Lowering SkySafari's readout rate is the lever. |
| IMU reader (10 Hz) | One shared-memory write per sample on CPU 0. | Keep. Cards updated over the air still run at 20 Hz (see section 5). |
| Per-frame bookkeeping | Camera frame timing, solver results and the IMU reference, a few small writes per frame. | Keep: debug bundles, live capture and the smoother all need them. |
| Temporal background cache (`bg_cache_enabled`) | The biggest solver-side cost: a 100–300 ms median rebuild on the solver cores. | Keep, but it's the reason to finally test **bin-at-submit** (section 2), which would cut this. |
| Old IMU "C-matrix" fit | A tiny calculation plus one write on every solve while the scope moves. It only serves the `imu_exact_predict=false` fallback; the exact path has been the default since v0.11.23. | Remove once the smoother has a verdict. |
| Background loops (resource monitor 5 s, watchdog, auto-exposure and mount checks, clock save every 5 min, frame-health check every 10 s) | Negligible. | Keep. |

## 2. A/B tests and default-off features still waiting for a decision

None of these costs anything while off, beyond a setting check per frame. They do add clutter and code complexity. My suggestion is to run each comparison once, decide, and then remove the loser.

| Feature | Waiting since | Suggestion |
|---|---|---|
| **Smooth crosshair** (`imu_report_smoothing`) | today | Highest-value test. If it wins, also remove the old crosshair-smoothing layers it replaces (hold-on-fresh-solve and the rate filter), plus the old IMU fit above. |
| **bg-cache bin-at-submit** | v0.11.26 | Worth testing because it would *save* solver CPU and memory. Use the Experimental A/B card. |
| **ROI tracking mode** | v0.11.27 | Has an A/B harness that seems never to have been run on sky. If it isn't worth testing, drop it: it is the most complex optional code in the solver loop. |
| IMU calibration persistence (`imu_persist_bno055`) and solve-derived IMU correction (`imu_solve_cal_enabled`) | v0.11.3x | Validate or drop. The second has a reserved "write to chip" mode that was never implemented; drop that regardless. |
| `horizon_reject_enabled` | — | Only helps blind solves, and only with a correct clock. Low value; it can stay off. |
| Legacy preset / tetra3 extractor | — | A comparison baseline only. Zero cost; keep it expert-only. |
| Background modes (8; `top_hat` alone costs ~100 ms per frame) | — | Zero cost unless selected. Could trim the menu to the 3–4 you actually use. |
| Polar alignment (page already hidden from the menu) | — | Zero cost while idle. Either restore it to the menu or remove the code. |

## 3. Overlapping tools for bundles, captures and tuning

| Overlap | Assessment |
|---|---|
| **Debug bundle vs live capture** | They complement each other. The bundle records the setup (config, log, settings, status); the live capture records the timed frames. |
| **Debug bundle vs the Background page's A/B run** | Both capture a burst of frames, but the A/B run adds its own comparison report. It only runs when you press it, so it costs nothing otherwise. |
| **`/debug/collect`** (the old route that builds a bundle and returns it in one long request) | The web UI no longer calls it; only a test does. Drop it. |
| **Three tuners**: auto-tune (live sky), "Tune from burst" card (on the device, replays a saved burst), `replay_corpus.py` (off-device) | Real overlap. I'd keep auto-tune and `replay_corpus.py` and drop the "Tune from burst" card, or the reverse. None of the three costs anything while idle. |
| `save_failed_frames` / `save_solved_frames` | Off and capped at 100 MB. Live capture now covers most of what these were for. Could be dropped. |

## 4. Web UI clutter versus cost

The cards themselves cost nothing. Each page only fetches data while it is the visible tab, since this release pauses hidden tabs. The one thing worth knowing: **the Advanced page is the most expensive page to leave open.** It runs four refreshes the whole time:

- the live view every 1.25 s, which encodes a JPEG on CPU 0;
- camera state every 3 s (three internal calls);
- the background-mode summary every 3 s (a round trip to the solver);
- mount status every 4 s, even when the mount link is off.

The Home page runs two. A cheap fix I can make: refresh the background and mount lines only when their card is in use, and slow the camera-state refresh. Otherwise, just park on Home.

## 5. Items still open or worth considering

1. **On-sky A/B tests:** the smoother first, then bin-at-submit and ROI tracking. Then prune whatever loses, as above.
2. **Check that SkySafari can set the clock now.** After flashing #193, connect SkySafari with the clock wrong. The log should show `Clock synced … (direct)`, and Utilities → Clock should name SkySafari as the source.
3. **IMU rate on cards updated over the air:** they keep their saved 20 Hz. A one-time config migration could move 20 to 10 when the value is still the old default. Small and safe.
4. **Live captures are never deleted automatically.** Consider a size cap, as debug bundles and Background A/B runs already have.
5. **Mid-exposure timing for the IMU fit and solve hint.** It currently applies to the crosshair reference only. Extend it after the smoother verdict.
6. **Cheaper Advanced-page refreshes** (section 4).
7. **Deferred:** "promote tested image" release mode, and additional mount dialects (LX200, Alpaca, OnStep) from the mount design notes.
8. **Housekeeping:** CLAUDE.md still says the IMU runs at 20 Hz in two places, and the dropped boot-traceback fix should be removed from the to-do list.

Items 3 and 6 are small and low-risk, and I can do them next. The rest need sky time or a decision from you. If you want this as a shareable page or added to `docs/next-release-todo.md`, say which.

---

### **User** — 2026-11-02 16:17:32

Do items 3 and 6, and add this to the to-do doc.  I want to also explore having an off-device python script that will take a live capture bundle and derive an estimated TLE for satellite streaks or multi-dot trails built up/evident in aligned/stacked/composited frames in the bundle.  If one of the satellites was not found by satchecker then Mike McCant's classified TLE list or other TLE lists (determined by you) would be checked to see if there might be a match.  (That would have to deal with the fact that a referenced TLE would be out of date - so a calculated TLE would have to be back-propagated if that's even possible).  Please provide feedback on this idea.

---

### **Claude** — 2026-11-02 16:18:03

Item 3 is done (cards updated over the air move from 20 Hz to 10 Hz once, unless the user picked a rate). Now item 6, the cheaper Advanced-page refreshes.

Checking the change in a real (headless) browser: the live view should stop fetching once it's scrolled out of view.

Both pages now stop fetching the live view when it's scrolled away and resume on return. The Advanced page's other refreshes ran once instead of every 3 s. Running the full suite, then the docs.

Now the to-do doc: the assessment, the open items, and the TLE proposal.

Build #193 finished. I'll cancel its now-redundant check-in and grab the artifact details.

Items 3 and 6 are done, the to-do doc is updated, and the release-candidate image (build #193) finished successfully. The two new fixes landed after #193 was built, so they aren't in it. All 647 tests pass, and everything is pushed.

**Release-candidate image:** https://github.com/mconsidine/diofinder/actions/runs/41474282361. Download `diofinder-sycamore-image-manual-20261102-1543` (about 1 GB, expires Nov 16).

**What was done:**
- **Item 3 — cards updated over the air now move to the 10 Hz IMU rate.** It's a one-time config migration: a rate still at the old 20 Hz default becomes 10 Hz, and a rate you chose yourself is left alone. Migration rules now record which version added them, so this rule doesn't re-run the earlier ones.
- **Item 6 — pages only fetch what's on screen.** I checked this in a headless browser:
  - The live view on Home and Advanced: 4 frames per 5 s at the top of the page, none when scrolled away, and it resumes when you scroll back.
  - Advanced also skips the camera-state sync (now every 5 s instead of 3) and the background summary while they're off screen.
  - Mount status refreshes only every 30 s while the mount link is off.
- **`docs/next-release-todo.md`** now holds the resource assessment, the open items, and the satellite-identification proposal below.

These two fixes land in the next image build; I can start one whenever you like.

## Feedback on the TLE idea

It's worth doing, but I'd reframe it: **identify the satellite, then correct an existing element set**, rather than derive a TLE from scratch.

- **From scratch doesn't work well with this data.** A finder trail is 10–60 s from one location. That pins down the direction and angular rate well, but not the distance, so the altitude and orbit stay undetermined. If you assume a circular orbit you get a rough orbit. That's good enough to search catalogs with, not to publish as a TLE.
- **Old element sets are fine; no back-propagation needed.** You don't back-propagate the TLE you compute. Instead you take each catalog element set and propagate it to the time of your frames. SGP4, the standard propagator for TLEs, works forward or backward in time. An outdated element set mostly gets the timing wrong; the orbital plane stays good much longer. So match on the line, direction and rate, and let the timing float. The result reads like "STARLINK-xxxx, 40 s late". Then adjust that set's timing parameters (two of them) to fit your observations. That corrected set is the realistic version of "an estimated TLE".
- **Where the extra matches would come from:**
  - SatChecker covers only the public catalog, so Mike McCants' classified file (`classfd.tle`) is the main source of new identifications.
  - CelesTrak's supplemental data comes from the satellite operators themselves (Starlink, for example) and is more accurate than the regular catalog.
  - Space-Track needs an account.
  - I couldn't reach any of these from this sandbox, so the download URLs need confirming on your laptop.
- **Write the observations in the standard IOD format as well.** Then existing tools can pick up from there, such as Bill Gray's Find_Orb and sat_id, Scott Campbell's satfit, and the SeeSat-L observers. That's better than building our own orbit fitter.
- **The clock sets the accuracy.** Across the trail a position is good to about 1 px (51″). Along it, timing dominates: 0.1 s of clock error at 0.5°/s is about 3.5 px. So set the clock from your phone first; exposure times already come from the sensor.

**Proposed order:**
1. Per-frame RA/Dec observations of each trail, written as IOD and JSON.
2. Identification against downloaded element files, with the timing left free.
3. Correcting the best match's element set to the observations.
4. Optionally, the rough circular-orbit estimate from the arc alone.

To validate it I need a capture with real sunlit satellite trails; dusk, 1–2 hours after sunset, is best. Your 2 November capture's only trail turned out to be an aircraft. Should I start on steps 1–2?

---

### **User** — 2026-11-02 16:28:31

start the next image build

---

### **Claude** — 2026-11-02 16:31:14

I started the next image build (#194): https://github.com/mconsidine/diofinder/actions/runs/41481832699

It's the same as #193 plus the two new fixes: cards updated over the air move to the 10 Hz IMU rate, and pages only fetch what's on screen. It uses the same solver versions (olive-solve v0.1.11, sycamore v0.14.2) and has no release tag, so it will be a downloadable file on the run page only.

Builds take about 30 minutes. I've scheduled a check for about 17:07 UTC, and I'll post the download name and link then.

---

### **User** — 2026-11-02 16:36:46

[uploaded file: inttles_1.zip — contents not saved] [uploaded file: classfd_1.zip — contents not saved] Ok, I'm on board with the satellite id approach. I'd also like the TLEs generated in the newer format, which I understand increases the field for the satellite number (?).  Ie I'd like to have them generated in the older standard format as well as *whatever* newer formats are used/proposed.  McCant's website is here https://mmccants.org/tles/  The link for the classified zip file ishttps://mmccants.org/tles/classfd.zip and the link for some integrated TLEs is https://mmccants.org/tles/inttles.zip  I am attaching current versions of those for reference.

---

### **Claude** — 2026-11-02 16:37:49

Let me look at the two files and check which libraries are available.

`sgp4` 2.27 is installed and already handles the Alpha-5 numbering. Next I'll survey what's in the two files (orbit types, epochs) to pick realistic test objects.

Fixing two sign errors I spotted on review (the early/late wording and the refinement's time shift), and a leftover in `trail_observations`.

Now a synthetic check: simulate a pass of a real `classfd` object with its timing off by 37 s, then confirm `identify` finds it among all 436 sets, recovers the 37 s, and that refinement fits it.

You've hit your 5-hour limit · resets 5:10pm (UTC)

---

### **User** — 2026-11-02 17:11:41

I hit my usage limit while you were working, but it has reset now. Please continue from where you left off.

*(sent while Claude was working)*

---

### **Claude** — 2026-11-02 17:11:51

Two test issues: `sgp4`'s OMM exporter fails on an empty international designator, and my test's hand-computed Julian date was a day off. Fixing both.

All 8 pass, including the end-to-end run. Next, a smoke test on your real 2 November capture against your two McCants files. The trail is the aircraft, so it should come back unmatched.

Pushed. Checking on image build #194, whose check-in may have fired while you were paused.

No response requested.

---

### **User** — 2026-11-02 17:49:45

I hit my usage limit while you were working, but it has reset now. Please continue from where you left off.

---

### **Claude** — 2026-11-02 19:24:28

No response requested.

The satellite identification script is done, tested and pushed (`tests/sat_identify.py`), and image build #194 finished successfully. The full test suite (655) passes. I haven't yet checked the CI run on this latest push.

**Image #194** (the release candidate plus the IMU-rate migration and on-screen-only polling): https://github.com/mconsidine/diofinder/actions/runs/41481832699
Download `diofinder-sycamore-image-manual-20261102-1628` (about 980 MB, expires Nov 16). The new script runs on your laptop, not the finder, so it doesn't need a new image.

**How to run it** (on your laptop, after `pip install sgp4`):
```
python3 tests/sat_identify.py livecap_XXXX.zip --elements classfd.zip inttles.zip --window 600
```
Or use `--download` to fetch both files from mmccants.org. It also reads CelesTrak downloads, as TLE text or as the newer JSON/CSV/XML element formats.

**What it does:**
- Each trail in the stacked frames is turned into a position for every frame.
- Those positions are compared with every element set you supply, on line, direction and speed, with the timing left free. Out-of-date element sets are fine: the report says how early or late the object ran.
- The best match is then adjusted to fit your observations.

**What you get** (in `<capture>_ident/`):
- **Identification report:** candidates with time offset, error, direction and whether the object was sunlit.
- **Observations in IOD format:** the format SeeSat-L, Find_Orb, sat_id and satfit read. Pass `--station` with your station number; otherwise the default 9999 is a placeholder.
- **The adjusted elements, in every format:**
  - classic TLE;
  - CCSDS OMM as key=value text;
  - CCSDS OMM as XML;
  - OMM as JSON and CSV, the way CelesTrak serves them.

**About the bigger catalog numbers:**
- The classic TLE keeps its 5-character number field. Numbers 100000–339999 are written in the "Alpha-5" form, where a letter replaces the first two digits (A0001 = 100001).
- Numbers above that can't be written as a TLE at all. They go out only in the OMM formats, which allow 9 digits; those are what CelesTrak and Space-Track use for new objects.

**How it was checked:**
- **Synthetic capture:** satellite dashes drawn from NOSS 3-4 (A) in your `classfd` file, made to run 37 s early. The script found it among all 436 element sets and recovered the 37 s. It rejected its sister satellite NOSS 3-4 (C), whose track is 0.9° away. The adjusted elements reproduce the pass to within about 0.01°.
- **Your 2 November capture:** checked against both McCants files, the aircraft trail correctly comes back unidentified. The one nearby candidate was moving the opposite way and was in Earth's shadow.
- **Formats:** every format reads back into SGP4.

**Limits:**
- Only trails seen in 3 or more frames are measured.
- Timing accuracy depends on the finder's clock, so set it from your phone (Utilities → Clock) before capturing.
- A real satellite test needs a capture with sunlit satellites. Dusk, 1–2 hours after sunset, is best; your current capture has only the aircraft.
- Estimating an orbit from a single trail with no catalog match isn't built.

That was the late reminder about image build #194, which I already reported. The build succeeded: https://github.com/mconsidine/diofinder/actions/runs/41481832699

The automated tests passed on GitHub both for #194's code and for the newer satellite-identification script.

---

### **User** — 2026-11-02 19:52:00

So #194 artifact is the latest?  What remains in the queue after that?

---

### **Claude** — 2026-11-02 19:52:17

Yes, #194 is the image to use: https://github.com/mconsidine/diofinder/actions/runs/41481832699. The only commit since is the satellite-identification script, which runs on your laptop, so #194 has everything that runs on the finder.

**Waiting on you or the sky** (most of what blocks a tagged release):
1. **Smooth crosshair A/B in SkySafari.** Toggle "Smooth crosshair (experimental)" on and off, watching slews and the scope at rest. This is the main decision.
2. **Field checks of this release's fixes:**
   - SkySafari actually sets the clock: the log should show `Clock synced … (direct)`, and Utilities → Clock should name SkySafari.
   - The FOV stops recalibrating repeatedly over an evening.
3. **The other pending A/B tests:** bin-at-submit and ROI tracking (Experimental A/B card); IMU Units B/C (keep or drop).
4. **A capture with sunlit satellite trails,** about 1–2 hours after sunset, to validate the identification script on real data.

**Code I can do now without the sky:**
- Size cap for live captures (they're never deleted automatically).
- Pruning that doesn't depend on a test result:
  - the unused old bundle route;
  - the never-implemented IMU "write to chip" setting;
  - `save_failed/solved_frames`;
  - one of the three tuners;
  - a decision on the hidden Polar page;
  - trimming the background-mode menu.

  Some of these need your choice: which tuner goes, what happens to Polar, and which background modes stay.

**After the crosshair verdict:**
- If smoothing wins, remove what it replaces: the old crosshair layers and the legacy IMU fit.
- Apply mid-exposure timing to the IMU fit and solve hint as well.

**Optional / deferred:**
- Satellite tool step 4: a rough orbit estimate for trails with no catalog match.
- "Promote tested image" release mode.
- More mount protocols.

**Releasing:** once the crosshair test is done, or even before since smoothing ships off, the code at #194 can be tagged v0.11.76. Tagging runs a fresh build of the same code rather than reusing #194's file, because "promote tested image" isn't built yet.

Should I start on the live-capture cap and the pruning that needs no decision from you?

---

### **User** — 2026-11-02 19:54:53

Yes, though I want to know what why we decided the "write to chip" setting didnt need to be implemented.  Keep the polar align page hidden; trim the background-mode menu; what are the three tuners?

---

### **Claude** — 2026-11-02 19:54:54

I'll start with the reasoning recorded for the "write to chip" setting.

The live-capture cap is done and tested. Next, the background-mode menu, then the `/debug/collect` fix.

I'm trimming the menu on the Advanced and Background pages. One of my new tests had a bad line; I'll also check whether the other failure predates my change.

All 659 tests pass. Updating the docs, then committing.

The live-capture cap and the menu trim are done and pushed, and all 659 tests pass. Checking the code showed two items from my "safe to drop" list should stay, so I've kept them.

**Why "write to chip" isn't built:** it was postponed on purpose, not judged unnecessary. I shouldn't have listed it for removal. The July IMU design (`docs/decisions/2026-07-21-imu-calibration-from-plate-solves.md`) staged it last:
- The plate-solve IMU correction ("Unit C") first runs in software only. That is never written to the sensor, is fully reversible, and the design says it delivers most of the benefit.
- Writing the correction into the BNO055's own registers comes only after that software correction is proven on sky.
- It also needs the sensor-calibration saving feature ("Unit B") switched on, because it writes through that feature's channel.
- It carries real risk: converting into the chip's raw units, and the chip's own auto-calibration can overwrite the values.

Neither Unit B nor Unit C has been field-tested yet, so the write step was never started. The reserved setting stays.

**Done now:**
- **Live captures are capped.** After each capture, and at start-up, the oldest captures are deleted once all of them together pass 8000 MB. The newest is always kept. The limit is on the Config page ("Live captures kept (MB)"; 0 = keep all), and the Utilities page now explains this.
- **Background-mode menu trimmed** on the Advanced and Background pages to four: block_percentile (the default), row_percentile (fastest), uniform_mean (matches the Legacy preset) and temporal_median. The other four still work if set in the config, still appear in the Background page's preview, and still show in the menu while one is the active mode, so nothing changes behind your back.
- **Polar page:** stays hidden.
- **Kept after all:**
  - `save_solved_frames`: the lens-distortion script `calibrate_lens.py` reads the frames it saves.
  - The old bundle route: it's the documented `curl` command. That command was actually broken (the route only accepted POST, `curl` sends GET), so I fixed it rather than removing the route.

**The three tuners** (each searches detection settings for the best solve rate and speed):
1. **Auto-tune (current sky)**, a card on the Advanced page. It tunes live on the sky the finder is looking at: first exposure and gain, then sigma, kernel and background mode. It can apply the winner.
2. **"Tune from burst"**, also on the Advanced page. It replays a saved Background A/B burst through the finder's solver over a grid of detection settings, so it uses the finder's CPU but doesn't need the sky.
3. **`replay_corpus.py`**, which runs on your laptop. It does the same replay but can also vary the FOV and star limits, and it can push the winner to the finder.

My suggestion: keep 1 and 3 and drop "Tune from burst". It duplicates the laptop script but runs on the Pi; its only advantage is that it needs no laptop. Which would you like to keep?

These three changes run on the finder, so they aren't in image #194; the next image build will include them.

---

### **User** — 2026-11-02 20:02:31

Drop Tune from burst and start the next image build

---

### **Claude** — 2026-11-02 20:03:41

Quick browser check that the Advanced page still loads cleanly and the Experimental A/B toggles initialise.

No page errors, and `/api/tracking` is now polled, so the A/B card's toggles work without saved bursts. Committing and starting the build.

The "Tune from burst" card is removed, and the next image, build #195, has started: https://github.com/mconsidine/diofinder/actions/runs/41515756146

**What changed:**
- **Removed:** the card, its script, its three routes and its background worker.
- **Kept:** auto-tune, which tunes against the live sky, and `tests/replay_corpus.py` on your laptop. The Advanced page still lists the saved burst archives for download.
- **Bug fixed along the way:** the Experimental A/B card's script shared a block with the tune card, behind an early exit when no saved bursts existed. So on a finder without bursts, the ROI-tracking and bin-at-submit toggles never started up. A headless-browser check now shows the A/B card's script running and no page errors.

All 659 tests pass.

**#195 is #194 plus:**
- the live-capture size cap;
- the trimmed background-mode menus;
- the working `curl` bundle command;
- this removal and the A/B-card fix.

It uses the same solver versions (olive-solve v0.1.11, sycamore v0.14.2) and has no release tag. A check is scheduled for about 20:40 UTC, and I'll post the download link then.

---

### **User** — 2026-11-02 20:06:16

Describe what the bin-on-submit and ROI-tracking ideas are all about.

---

### **Claude** — 2026-11-02 20:06:34

Both are speed-ups to the solver that are off by default until they've been tested on a real sky. They attack different costs.

## Bin-at-submit: a cheaper background model

**Background first.** To find faint stars, the finder subtracts the sky's own brightness, the "background". By default it doesn't estimate that from each frame alone. It keeps a stack of the last 8 frames and takes the per-pixel median: stars, satellites and noise flicker, while the sky glow and the sensor's fixed pattern stay put. The median gives a cleaner background than any single frame and also removes hot pixels.

**The cost.** That median is computed on full-resolution frames, 960×760 × 8 frames. Detection then runs on a 2×2-binned frame (each 2×2 block of pixels averaged into one), so the result is shrunk 2×2 afterwards anyway. Each rebuild holds the solver's Python lock for roughly 100–300 ms on the Pi, and all 8 full-size frames sit in memory.

**The idea.** Bin each frame to the detection size when it enters the stack, and take the median of the small frames. That stores about half the data and does about a quarter of the median work, for the same end product.

**Why it isn't automatic.** The two orders aren't mathematically identical: averaging then taking the median is not the same as taking the median then averaging. The background and noise estimate come out slightly different. Offline tests on real sky frames showed the noise within about 1–2% and the same star counts. Before it becomes the default, it should be compared live on a clear night: same field, same stars, same solve rate, with less CPU.

**Risk:** low. It only changes how the background is computed. The worst case is slightly different star detection, and the A/B test checks for exactly that.

## ROI tracking: look only where the stars already are

**The cost it targets.** Every frame, the finder searches the whole image for stars (~6 ms of extraction) and then solves the pattern. When the scope is parked or moving slowly, the stars are almost exactly where they were in the last frame.

**The idea.** After a few consecutive good solves, the solver switches from FULL to TRACKING mode:
- It places small windows, 48×48 px by default, around where each star was last time, and looks for stars only inside them, all in one call to the star detector.
- It then skips the expensive part of solving. Normally the solver hashes star patterns to identify the sky. In tracking mode it just checks the last known attitude: it projects the catalog through it and verifies the stars line up. That takes about 0.01 ms instead of 0.5 ms (measured on a PC).

**Falling back is automatic and conservative.** It returns to a full search when:
- too few stars turn up in the windows;
- the check fails;
- the IMU says the scope is slewing;
- or you switch the feature off.

The fallback is always a full search with a full solve, never another quick check. Everything reported to SkySafari, calibration and alignment is identical either way; only how the stars are found changes.

**Why it isn't automatic.** The danger is a lock that holds onto a wrong attitude, reporting confidently while slightly off. The A/B harness runs the same parked field in both modes, and the key test is whether TRACKING's pointing matches FULL's, not just whether it's faster. It also reports solve rate and timing, then gives a "safe to enable" or "keep off" verdict.

**Payoff, and why it may not matter much:**
- Extraction drops from ~6 ms to ~1.5 ms per frame, and solving becomes nearly free.
- On this finder, frame time is dominated by the exposure itself (0.2–1 s), so faster solving mostly frees CPU and reduces latency; it doesn't raise the solve rate.
- Its biggest gain is when the scope sits still, which is exactly when speed matters least.
- It is also the most complex optional code in the solver.

## How to test both

On a clear night, point at a star field and leave the scope still: Advanced page → **Experimental A/B** card.
- **ROI tracking:** press the A/B run button. It runs a FULL window, then a TRACKING window, and gives a verdict.
- **Bin-at-submit:** turn it on and off with its toggle and compare solve rate and stars. Its cache status reads "bin-at-submit" while it's on.

My suggestion is to test bin-at-submit first, because it saves CPU in every mode with little risk. Then decide whether ROI tracking is worth keeping at all, given its small real-world payoff and its complexity.

Build #195 failed after 6 minutes, although the tests passed on the same commit. Pulling the failing job's log.

Image build #195 failed, but the cause was outside our code. The test suite passed on the same commit.

The build downloads its packages from Debian's snapshot mirror (snapshot.debian.org). That mirror was overloaded ("503 Too Many Requests / No healthy backends"), so the package lists never arrived and the build stopped about 6 minutes in.

I've re-run it: https://github.com/mconsidine/diofinder/actions/runs/41515756146

A check is scheduled for about 01:01 UTC. If the mirror is still failing then, the better move is to try again later rather than re-run straight away.

The re-run of image build #195 succeeded, so the Debian mirror problem was temporary.

Download `diofinder-sycamore-image-manual-20261102-2041` (about 980 MB, available until Nov 16) from https://github.com/mconsidine/diofinder/actions/runs/41515756146

This is now the latest image. It contains everything in #194 plus:
- the live-capture size cap;
- the trimmed background-mode menus;
- the working `curl` command for debug bundles;
- removal of the "Tune from burst" card;
- the fix for the Experimental A/B card's toggles.

The automated tests had already passed on this code.

---

### **User** — 2026-11-03 19:10:09

Does this latest image contain all changes since release of 0.11.75?

---

### **Claude** — 2026-11-03 19:10:30

I'll check this against git rather than from memory: whether the branch contains the v0.11.75 tag, whether anything newer exists, and whether `olive` gained commits the branch lacks.

Yes. Image #195 was built from commit 6494de3, which is still the tip of the branch, and that commit contains all 23 commits made since v0.11.75. I checked against git rather than from memory:

- **v0.11.75 is included:** the branch contains its tag (commit 575e9bd).
- **Nothing is missing from `olive`:** the release branch has no commits that this branch lacks.
- **Nothing newer is left out:** no commits after 6494de3, and no uncommitted changes in my working copy.
- **The finder reports itself as 0.11.76.** The version bump is one of the 23 commits.

The 23 commits in order:

| Area | Commits |
|---|---|
| Mount | settle gate wired; MAX3232 wording |
| Live capture | the button and zip; matched stars; time limit and clock source; size cap |
| Satellite tools (laptop) | overlay; `--stack`; hide unseeable predictions and detect unmatched trails; `sat_identify` (McCants matching, TLE and OMM output) |
| CPU 0 load | hidden tabs pause; IMU 10 Hz (and migration for cards updated over the air); sustained-CPU rule; on-screen-only polling; SkySafari poll-rate hint |
| Bundles and archives | built to a file and downloaded; matched stars in bundles; `curl` works; Background A/B folder capped |
| Clock | Utilities Clock card; daemon sets the clock directly; last-known time restored; only this boot's source trusted |
| Release-candidate fixes | test-mode site never 0,0; FOV recentres on the long-run median; opt-in crosshair smoother |
| Pruning | trimmed background-mode menus; Tune-from-burst card removed (and the A/B card fix) |
| Housekeeping | to-do doc; CI test workflow; version 0.11.76 |

The solver libraries are pinned to olive-solve v0.1.11 and sycamore v0.14.2. The star database isn't pinned: the build takes the newest astro_databases release at the time it runs.

---

### **User** — 2026-11-04 01:52:24

I want to assess how the live capture archive is created.  In particular, I'm wondering if the raw, large frames are available to be saved, ie the one prior to them being downsampled.  I'm assuming that once the user decides to "live capture" a series of frames, they are not concerned about solving so the performance hit is not an issue.  Thoughts?

---

### **Claude** — 2026-11-04 01:52:31

First I'll check how the camera produces frames today and what reaches the live-capture recorder.

No: the larger frame isn't available to save, because it never exists in memory. I checked `diofinder/camera_proc.py` and `diofinder/live_capture.py`:

- **The sensor** reads out in its 2×2-binned full-field mode, 2028×1520 (the default since v0.11.15).
- **The ISP** (the Pi's image processor) is asked for only one output: a 960×760 8-bit image. That's an average of roughly 2×2 sensor pixels, already passed through the finder's tone curve.
- **The raw stream is switched off** (`raw=None`). It was dropped deliberately to save camera memory and allow two buffers.
- **Live capture** saves exactly what the solver sees: the 960×760 frame as a ~200 KB PNG, encoded on a background thread that drops frames rather than slow anything down.

So saving bigger frames means reconfiguring the camera while a capture runs. That's feasible. Two of your assumptions need adjusting first.

**1. Solving still matters during a capture.** The satellite tools depend on each frame's own solve: the matched stars give the exact sky-to-pixel mapping, and the solved pointing groups the frames. Without solving, the overlay and identification tools have nothing to place the trails on. The fix is cheap: give the camera two outputs, a large one for the capture and the usual 960×760 one for the solver. The Pi's image processor supports this, so solving and the per-frame mapping carry on unchanged. Conveniently, 960×760 is the centre 1920×1520 of the sensor scaled by exactly 2. So a 1920×1520 capture frame maps to the solver frame by a simple ×2 and every frame's star fit carries straight over. I'd record the processor's crop metadata to confirm that offset rather than assume it.

**2. The cost isn't the solver; it's writing the data.** Approximate costs per frame, not measured on the Pi yet:

| What's saved | Size | Notes |
|---|---|---|
| Today: 960×760, 8-bit | ~0.2 MB PNG | — |
| 1920×1520, 8-bit (processed) | ~0.9 MB PNG | 4× the pixels; PNG encoding on the Zero's slow cores will likely be the bottleneck, so frames get dropped unless compression is lighter or the frame rate lower |
| 2028×1520 raw Bayer, 12-bit linear | ~4.6 MB packed | Unprocessed sensor data; fast to write uncompressed, but the 4 GB-per-capture limit fills in ~15 min at 1 frame/s, and downloading GBs over the Pi's Wi-Fi takes a long time |
| 4056×3040 full-resolution sensor mode | ~18 MB raw | Not worth it on a Zero 2W: a different sensor mode, a restart, and too much memory |

**What bigger frames would buy:**
- **Finer sampling.** About 25″ per pixel instead of 51″, so trail positions across the track get about twice as precise. This helps the element-set refinement a little and helps separate close trails.
- **Little for identification.** Error along the trail is set by the clock: 0.1 s at 0.5°/s is about 3 arcmin, far more than a pixel. So bigger frames barely improve matching to a catalog.
- **Linear brightness, raw only.** The raw tier is the only one that gives true brightness without the 8-bit tone curve. That's what you'd want for light curves, such as telling a tumbling rocket body from an aircraft strobe by its flash pattern.
- **Faint-trail detection doesn't gain much.** A trail's signal along its length is about the same whether or not it's downsampled. The real limits are the 8-bit tone-mapped output and the exposure.

**My suggestion:** an opt-in "full-resolution" choice next to the Live capture button.
- **Start:** the camera restarts for about 1–2 s with both outputs, the 1920×1520 one for the capture and the 960×760 one for the solver.
- **During:** solving carries on.
- **Stop:** the camera switches back to normal.
- **Raw:** a 12-bit raw option later, only if light curves matter to you.

Before building it, I'd want to measure on your Pi how fast it can encode and write 1920×1520 frames, since that sets the usable frame rate.

Is finer positions or brightness curves the main goal for you? That decides between the 1920×1520 tier and the raw tier.

---

### **User** — 2026-11-04 02:06:49

Here is a recent contribution to the Seesat-L list.  I believe it is IOD format.  Ideally, I'd like to have this kind of a contribution automatically calculated from the live capture archive.  But I don't know what level of accuracy/uncertainty contributors are typically dealing with and if the diofinder is too small/slow to be a useful contributor.  Both position and being able to estimate magnitude, tumbling, etc would be useful.  Thoughts?

---

### **Claude** — 2026-11-04 02:06:52

You mention a SeeSat-L contribution, but no text or file came through with your message. I'll check whether it arrived as an upload.

Your message didn't include the SeeSat-L contribution: no text came through and no new upload arrived. Paste it and I'll check our IOD output against it field by field. Its uncertainty codes also show what accuracy that observer claims. Meanwhile, here's my assessment.

## Accuracy: competitive on position; the clock is the weak link

A finder this size can contribute useful data, mainly because it records precise per-frame times and fits the stars in every frame.

| | Typical SeeSat-L observers (rough ranges, from memory) | diofinder today |
|---|---|---|
| **Position** | Visual with charts: ~0.1–0.5°. Camera/video: ~0.01–0.05° | Star-fit residual measured on sky: 0.64 px (about 0.5′). Across a trail, about 1 px ≈ 0.01–0.02°. That is in the camera-observer range. |
| **Time** | Visual: 0.1–0.5 s. Video with GPS time: a few ms to tens of ms | Frame timing is exact to milliseconds relative to the Pi's clock, so absolute accuracy is the clock's. Network time: ~10 ms. Set from your phone: a fraction of a second. SkySafari: up to ~2 s. |
| **Limiting brightness** | Visual: about magnitude 4–5 for moving targets | Estimated: stars to about magnitude 8–9, satellite trails to about magnitude 6–7 (a moving object spreads its light along the trail). Most low-orbit classified objects are reachable; geostationary objects (magnitude 10+) are not. |

Size and processing speed aren't the problem: the Pi only records, and the analysis runs on your laptop. The limits that matter:
- **Timing.** For low orbits, timing error matters more than position error: 0.1 s at 0.5°/s is 0.05°. To report at the level of camera observers, you'd want network time when available. The serious fix would be a small GPS receiver with a pulse-per-second output wired to a GPIO pin, giving millisecond time in the field. That's a hardware addition, and a design question because the serial pins are taken by the mount link. Also, the camera reads its rows out over tens of milliseconds, so rows near the bottom are exposed slightly later; that needs measuring and correcting.
- **Pointing.** Capture is passive, so you catch what passes through a 13.5° field. That's fine for planned passes and decent for chance catches.
- **Trail measurement.** Each frame's position is the middle of the dash at mid-exposure. That's only valid when the whole dash is inside the frame and evenly lit. Dashes cut off at the frame edge and flares need special handling, and I should add flags for both.

## Brightness and tumbling

Both are feasible, and flash timing may be the finder's strongest contribution.

- **Magnitude.** Every solved frame already records its matched stars with their catalog magnitudes, so each frame can calibrate itself. The satellite's brightness is its total light along the dash compared with those stars. One catch: the saved images are 8-bit and pass through the finder's deliberately steep brightness curve. That curve is known and can be undone, so ±0.3–0.5 mag looks plausible, about what visual observers report. Bright flares will clip at the top of the 8-bit range. The 12-bit raw option from the previous question would fix both issues properly.
- **Tumbling and flash periods.** Position along a dash is time: at 0.5°/s one pixel is about 30 ms. Because exposures nearly abut, a trail is close to a continuous light curve. Flash periods from about 0.2 s to many seconds should be measurable directly, with a repetition-period analysis on the light curve. Rocket-body flash periods are exactly what SeeSat-L observers report. The current "flash ratio" (it flagged your aircraft) is a crude first version of this.

## Suggested next steps

1. **Paste the contribution.** I'll match our IOD lines to it exactly, including the magnitude and flash-period fields, which I won't guess at.
2. **Add to `sat_identify`:**
   - per-trail magnitude with an uncertainty;
   - a light curve per trail (CSV and plot) with the flash period and a steady/flashing/irregular classification;
   - a full-exposure-in-frame flag;
   - the behaviour, magnitude and flash-period IOD fields filled in.

   All of that would produce a ready-to-post report from a live-capture archive.
3. **Validate on a capture with known objects:** the ISS, Starlinks, or a rocket body with a published flash period, before posting anything.
4. **Later, if worthwhile:** 12-bit raw capture for clean photometry, and GPS time for millisecond timing.

Your observations would also need a SeeSat-L station number for your site, so ask the list how they assign them. Accurate latitude, longitude and elevation in the finder's Config are prerequisites.

---

### **User** — 2026-11-04 02:07:49

99999 99 099A   1775 E 20261030232303106 17 25 2326210-104249 37 S
99999 99 099A   1775 E 20261030232504528 17 25 2126120-191906 37 S
99999 99 099A   1775 E 20261030232810398 17 25 1854580-262653 37 S


unk261030 ( chinasat )
1 99999U          26303.97574685 0.00000073  00000-0  50000-4 0    07
2 99999 142.0554  84.0615 0242693 117.2847   3.4921 13.30269374    00

---

### **Claude** — 2026-11-04 02:08:12

This is IOD format, and the sample is very informative. Let me check our own IOD writer against it column by column.

Our writer reproduces all three lines byte for byte. Next I'll measure the arc they covered, since that determines what's needed to derive an element set like theirs.

Yes, it's IOD format, and our writer already produces it. I fed their three positions and times through `sat_identify`'s IOD output and got all three lines back byte for byte. Decoding the first line:

| Field | Value | Meaning |
|---|---|---|
| `99999 99 099A` | catalog number / designator | unknown object (placeholder number) |
| `1775` | station number | their site, registered with the list |
| `E` | station status | conditions excellent (we currently write `G`) |
| `20261030232303106` | time | 2026-10-30 23:23:03.106 UTC |
| `17` | time uncertainty | 1×10⁻¹ s = **0.1 s** |
| `25` | angle format / epoch | RA HHMMmmm, Dec ±DDMMmm, J2000 |
| `2326210-104249` | position | RA 23h 26.210m, Dec −10° 42.49′ |
| `37` | position uncertainty | 3×10⁻¹ ° = **0.3°** |
| `S` | behaviour | steady (no flashing) |

## What this observer did

The three points span **66° of sky over 5 minutes**, with the object moving 0.19–0.25°/s. From those they derived the element set you pasted. It's an unknown object in a retrograde orbit (inclination 142°), nearly circular at 960–1330 km, period 108 minutes. Each point is coarse (0.3°, 0.1 s), but the arc is long, and a long arc is what makes an orbit determinable from scratch.

## How the finder compares

- **Per point, the finder is far better.**
  - Position: about 0.01–0.02° against their 0.3°, so 15–30× finer.
  - Timing: a phone-set clock matches their 0.1 s, and network time does better.
  - At this object's speed, 0.1 s of clock error is about 0.02°, so position and time error would be balanced for us.
- **Per pass, the arc is the problem.** The field is 13.5° (about 17° corner to corner). This object would cross it in roughly 60–80 s, covering at most about 17° of arc against their 66°. We'd get dozens of precise points from a short arc; they had three rough points from a long one. For identifying an object against catalogs, our version is better. For deriving a new orbit from scratch, theirs is better; how much better I'd rather simulate than guess (step 2 below).

**Ways to get a longer arc:**
- **Follow the satellite by hand.** Pan the scope to keep it in view. The finder solves every frame while moving, so the capture becomes a long, densely sampled arc. At ~0.2°/s with 0.2 s exposures, stars smear about 3 px; I expect it still solves, but that needs testing on sky.
- **Combine several passes,** over the same night or several nights. That's normal practice on the list.
- **Hand off to Find_Orb** with our IOD file for the orbit fit.

## Changes needed for automatic, postable reports

1. **Report a few points per pass, not every frame.** They posted 3; we'd pick about 3–5 spread along the in-field arc, preferring frames where the whole dash is inside the frame.
2. **Honest uncertainty codes from the data.** Position from the actual fit (`16`–`26`, i.e. 0.01–0.02°); time from the clock source (network time `16`, phone `17`, SkySafari only `18` or `28`).
3. **Station status as an option** (E/G/F/P) instead of a fixed `G`.
4. **Behaviour from the light curve** (steady, flashing or irregular), plus magnitude and flash period when measured. This observer omitted magnitude, so it's optional.
5. **The unknown-object convention.** They used `99999 99 099A` with the name `unk261030`. I'd copy their convention once someone on the list confirms the standard form.

**Suggested next step:** a simulation using their element set as the truth.
- Generate what the finder would record from a fixed 13.5° field: about 60 s of frames with 0.015° position noise and a 0.1 s clock.
- Do the same for a hand-followed 3–5 minute pass.
- See how well each recovers the inclination, node and period.

That would answer "is the diofinder too small to contribute?" with numbers. My expectation: yes for positions and identification, and for orbit determination only when hand-followed or combined across passes.

Shall I do the reporting changes (1–4) and the simulation?

---

### **User** — 2026-11-04 04:27:52

So your conclusion is that the diofinder is too small to create or update useful TLEs?  Can we also derive estimated magnitudes even with the custom curve and ISP features?

---

### **Claude** — 2026-11-04 04:28:33

Not quite. I said the finder is weak at creating an orbit from scratch out of a single field crossing, not at TLE work in general. Rather than argue it qualitatively, I'll quantify it, using their element set as the truth. First I'll check what the camera's processing does to brightness.

No, that's not the conclusion. The simulation shows the limit is how many times you catch an object, not the finder's size or precision. Methods and caveats are below the summary.

## Results

Their unknown object, simulated over your site. Finder points have 0.015° error; their style has 0.3°. "Error after" is how far off the resulting elements predict the object's position later.

| Observation | Inclination ±1σ | Node ±1σ | Period ±1σ | Error after 1 day | after 3 days |
|---|---|---|---|---|---|
| **Finder, 1 field crossing** (94 frames, 13° arc) | 0.06° | 0.34° | 116 s | ~7,000 km | ~10,000 km |
| **Finder, hand-followed 5 min** (90° arc) | 0.003° | 0.004° | 15 s | ~900 km | ~2,800 km |
| **SeeSat-style 3 points** (0.3°, over 5 min) | 0.75° | 0.93° | ~56 min | undetermined | undetermined |
| **Finder, 2 crossings 1.7 h apart** (fixed field) | 0.001° | 0.013° | <0.1 s | ~4 km | ~5 km |

All six orbital elements are fitted in every row.

What this means:

- **Updating known element sets: yes, from a single pass.** This is what `sat_identify` already does. The old elements supply the orbit's shape and period, and one pass corrects the timing, which is what drifts. It's also most of what classified-satellite observers do day to day.
- **A brand-new TLE from one field crossing: no.** The orbital plane is measured well, better than the 3-point post. The period is not (±2 minutes), so the prediction is useless a day later.
- **The 3-point SeeSat style can't fix an orbit on its own either.** Three points give six numbers for six unknowns, with no redundancy against 0.3° errors. Fitting them depends on assuming a circular orbit; their posted element set is a provisional orbit for others to refine.
- **Two crossings make the finder a genuine new-TLE contributor.** Same object, about one orbit apart (1.7 h here), gives a 4–5 km prediction after three days. That beats both the long hand-followed arc and the 3-point style by a wide margin. In practice: after the first crossing, use the provisional orbit to predict where to point for the next pass.

**Caveats:**
- These are idealised: random measurement noise only.
- Not modelled: clock offset (0.1 s ≈ 0.7 km along track, small), rolling-shutter timing, systematic errors in the star fits, and drag.
- I also ran a variant that forces a circular orbit, as the 3-point style needs. It isn't in the table, because it held the true eccentricity fixed, which made its numbers unrealistically optimistic.
- The script is at /tmp/claude-0/-home-user/04cb6ac2-2a1a-5796-9737-ff7487149953/scratchpad/odsim/sim.py

## Magnitudes despite the tone curve and ISP: yes

Checking the tuning file and the camera setup:
- **Undoable:** the only brightness transform is one fixed, monotonic 51-point tone curve. Contrast enhancement is off, and auto-exposure and white balance are off at runtime, so exposure and gain are fixed and recorded per frame. The curve can be inverted back to linear signal.
- **The method:**
  1. Linearise each frame (invert the curve, subtract the black level).
  2. Calibrate each frame against its own matched stars, which have catalog magnitudes.
  3. Sum the light in a strip along the dash. A trail collects the same total light over the exposure as a star would, so it converts to a magnitude with the same calibration.
  4. Slice the strip along its length to get the light curve for flash periods.
- **Things to correct for:**
  - **Vignetting.** There's no lens-shading stage in this tuning, so the corners are dimmer. That can be fitted from the stars across many frames.
  - **Colour.** Frames are greyscale from the colour sensor, and sunlight on a satellite differs in colour from most stars, so expect a colour term of about 0.1–0.2 mag. The database's magnitude band also needs checking.
  - **Clipping and coarse steps.** Anything reaching 255 gives only a lower limit (bright flares, the ISS). The curve's steps are fine at the faint end and coarse at the bright end.
- **Expected accuracy: about ±0.2–0.3 mag** for steady objects of magnitude 3–6, worse near the limit. That's comparable to or better than visual estimates (±0.3–0.5). Relative brightness within a light curve should be noticeably better than that.

The 12-bit raw capture option from earlier would remove the curve inversion, the coarse bright-end steps and the colour-processing uncertainty. It's an improvement, not a prerequisite.

## What I'd build, in order

1. **Photometry and light curves in `sat_identify`.** Per-trail magnitude with an uncertainty, a light-curve CSV and plot, the flash period, and the steady/flashing/irregular call.
2. **Postable IOD reports.** 3–5 points per pass, uncertainty codes from the actual fit and clock source, a station-status option, and the magnitude and flash fields.
3. **Two-pass orbit fitting.** Combine crossings from one or more captures into a new element set. Plus a "where to point next" prediction from the first crossing.
4. **Validation on a real capture.** Objects with known magnitudes and flash periods (ISS, Starlinks, known tumbling rocket bodies), checked before anything is posted.

Shall I start with 1 and 2?

---

### **User** — 2026-11-04 04:53:30

Yes, start with 1 and 2.  Follow up with 3 as well.  Ensure that anything needed to enable hand-tracking for a longer arc is also in place or at least flagged.  After that I think I should be able to test on a real capture by downloading an archive and running off-device scripts.  Also confirm that we thought the 12-bit raw capture option from earlier was too "expensive" in CPU/storage to make sense.  Also do the reporting changes previously proposed.

---

### **Claude** — 2026-11-04 04:54:50

This is a big job. I'll do it in order:
1. photometry and light curves;
2. the reporting changes;
3. orbit fitting from one or more passes, with "where to point next";
4. hand-tracking support and flags.

First, the exact IOD columns for magnitude and flash period. I won't guess those.

The satobs.org pages are blocked from this session. I'll search for a mirror of the IOD column definitions.

I'll start on the device side: each capture should record the exact tone curve in use, so the off-device photometry can undo it. Then I'll write the photometry module.

Next I'll wire photometry into `sat_identify`. First a look at the trail-timing and stacking code it builds on.

*(The session's context was compacted here; the conversation continued.)*

---

### **Claude** — 2026-11-04 05:00:02

Now the relevant parts of satcheck_overlay: detect_trails, trail_kinematics, stack_group, group_frames, analyse.

Small satcheck_overlay changes first.

The track linker is working on synthetic data: it found 51 of 51 real points with no false tracks. Now I'm putting the magnitude and light-curve code into sat_identify.

Update trail_observations to carry the frame file, and the IOD line to carry magnitude/flash fields.

Stack-trail photometry and the new IOD reporting now pass the end-to-end test. Next is a synthetic hand-tracked capture to test the linker end to end.

Photometry and reporting are done. The linker identifies a synthetic hand-tracked pass across a 10° arc to 0.002°. Now I'm writing the orbit fit and pass predictions.

Orbit fit and prediction are done and tested. Running the full test suite now while I update the docs.

Docs are updated (CLAUDE.md, the to-do doc, and the tests README). I'm waiting for the full test suite before committing.

All three are built (photometry, postable IOD reports, multi-pass orbit fits) and hand-tracking support is in, but everything is tested on synthetic captures only, nothing on real sky yet. It's committed and pushed as `d1c8f29`; the full suite passes (671 passed, 1 skipped).

**1. Magnitudes and light curves.** `sat_identify.py` now reports:
- **Magnitude:** a magnitude ± uncertainty per track, roughly ±0.2–0.3 mag for magnitude 3–6. It calibrates against the stars the solver matched in each frame. Stars under the trail are subtracted, so they don't count toward its brightness.
- **Saturated dashes:** reported as "brighter than", and that magnitude is left out of the IOD lines.
- **Light curve:** a CSV and a plot per track, sampled along each dash (or once per frame for hand-tracked dots).
- **Flash period and behaviour:** a flash period when one is found, and S/F/X (steady / constant flash period / irregular).

On the first synthetic run a slanted dash on the pixel grid produced a fake flash period. I fixed the sampling, and the tool won't claim periods shorter than about 4 px of travel along a dash.

**2. Reporting.**
- **Points per pass:** 4 IOD lines per track by default (`--points-per-pass`), spread over the arc and preferring dashes wholly inside the frame.
- **Position uncertainty:** taken from the actual scatter and the per-frame fit.
- **Time uncertainty:** taken from the capture's clock record: network time 0.05 s, phone 0.2 s, SkySafari 1 s. Otherwise it uses 10 s and warns "do not post". `--time-unc` overrides it.
- **Station:** `--status E/G/F/P` and `--station`.
- **Unknown objects:** written as `99999 99 099A` and named `unkYYMMDD`, as in your sample; `--unknown-desig` changes it.
- **SeeSat-L draft:** `seesat_post.txt` with the station, equipment and clock header, the IOD lines, the name and the refined TLE.

Two things to confirm before posting:
- **IOD columns 67–80** (magnitude, its uncertainty, flash period): I couldn't open the spec, so these follow my reading of it. Please check them against satobs.org/position/IODformat.html, or allow satobs.org in the session's network settings and I'll do it. `--no-iod-mag` leaves them out.
- **The unknown-object convention:** confirm `99999 99 099A` on the list.

**3. Orbit fits and "where to point next"** (new `sat_orbit.py`):
- **One pass:** gives a circular orbit, with the period good to about 4.5% (deliberately included in the uncertainty). The next pass's timing comes out ± several minutes, and later passes are marked "timing UNKNOWN" rather than given a false time.
- **Two passes a few hours apart:** gives a full orbit. In simulation it predicted positions to about 1–2 km three days out, timing ±1 s.
- `predict` lists visible passes with alt/az/RA/Dec pointing tables and the timing uncertainty.

**Hand-tracking a long arc.** A new frame-by-frame tracker (`sat_tracks.py`) finds an object the scope is following (a dot near the centre while the stars move), and arcs that span many fields. On a synthetic 24-frame pass covering about 10° it identified the object to 0.002°. Flags before trying it:
- Leave trail rejection off (`detect_max_axis_ratio` 0, the default).
- Use 0.1–0.3 s exposures. Frames that don't plate-solve are left out of the track.
- Plate solving while you push the scope by hand is untested on sky.
- It takes about 0.4 s per frame on a laptop, so roughly 4 minutes for a 10-minute capture.

**Raw 12-bit frames.** Yes, we decided against them, but correcting what I said earlier: the cost was storage and Wi-Fi transfer, not CPU. A raw frame is about 6–18 MB against about 0.2 MB for a PNG, so several GB per 10-minute capture; writing them is cheap on CPU. They aren't needed for magnitudes, because the camera's tone curve can be undone well enough. If finer photometry is ever wanted, a 2×2-binned 16-bit raw at about 1.5 MB per frame is the middle option.

**Testing on a real capture.** You don't need a new image for your existing archives; they fall back to the shipped tone curve with a note. A new image would make captures record the tone curve themselves. On the laptop: `pip install sgp4`, then
- `python tests/sat_identify.py livecap_….zip --elements classfd.zip inttles.zip --station NNNN`
- `python tests/sat_orbit.py fit livecap_…_ident/observations.json`
- `python tests/sat_orbit.py predict livecap_…_ident/orbit/fit.json --site-from livecap_…_ident/observations.json`

The most useful test captures would be one fixed-field pass, one hand-tracked pass, and a second pass of the same object for the two-pass fit. These details are also in `docs/next-release-todo.md`.

Sources:
- [Positional Observation Reporting Formats (satobs.org)](https://satobs.org/position/posn_formats.html)
- [orbitdeterminator issue #193 (IOD field widths)](https://www.github.com/aerospaceresearch/orbitdeterminator/issues/193)

---

### **User** — 2026-11-04 05:58:30

Positional Observation Reporting Formats
Positional observations of satellites provide the basis to determine their orbits, and to study their optical characteristics. The latter provide the basis for the accurate brightness predictions that we have come to expect from our favourite prediction software or service.
Positional observations are analysed using computer programs, so observers need a systematic and accurate reporting format, ideally one that can be read by both humans and machines.
There are three reporting formats in common use.

IOD Format
Here is an example of an IOD formatted report:

```
23794 96 010A   2701 G 20040506012614270 17 25 1100114-184298 38 I+020 10
90019 03 790B   2701 G 20040506020755480 17 25 0929080-203364 48
90019 03 790B   2701 G 20040506020932610 17 25 1029694-220449 67
90019 03 790B   2701 G 20040506021046340 17 25 1115711-225466 37
90019 03 790B   2701 G 20040506021115210 17 25 1133310-231006 78
23794 96 010A   2701 P 20040506061610940 17 25 1045488+105544 19 I-010 10
23794 96 010A   2701 P 20040506061636730 17 25 1252114+021122 29 I-020 10
23794 96 010A   2701 P 20040506061641360 17 25 1334003-010390 58 I-020 10
23794 96 010A   2701 P 20040506061735610 17 25 1909776-205541 97 I

```

The IOD format is described [here](https://www.satobs.org/position/IODformat.html).

U.K. Format
Here is an example of a U.K. formatted report:

```
0401401267504050320170296  010  12102706  +36412   50 5
0401401267504050320171054  010  12102406  +41279   50 5
0401402267504050320192783  020  12100282  +21570   20 5
0401402267504050320200763  020  12090786  +47320  100 5
9906701267504050320381348  020  12114955  +16154   15 5
9607201267504050320421970  010  12153504  +26400   60 5
9607201267504050320423927  010  12162845  +41547   40 5
9107603267504050320472151  010  12135798  +22002   50 5
9107604267504050320473073  010  12135931  +21233   50 5
9107603267504050320480282  010  12144997  +28369   10 5
9107605267504050320483131  010  12152189  +32549   20 5

```

The U.K. format is described [here](https://www.satobs.org/position/UKformat.html).

R.D.E. Format
Here is an example of an R.D.E. formatted report:

```
2420 0309 0.211 1204
03
0300203 195141.71 005932+313611 4.4 4.4 0 S
8502002 195853.74 010744+311633 5.1 5.1 0 S
04
7704401 015519.00 212317+765245 6.3 7.5 4 R
7704401 015559.35 221342+565110 6.0 7.2 4 R
8700602 020411.32 151747+741235 4.8 4.8 0 S
8205102 021426.21 021503+282843 4.2 4.2 0 S
999

```

The R.D.E. format is described [here](https://www.satobs.org/position/RDEformat.html).

---

### **User** — 2026-11-04 05:58:48

Positional Measurements

Orbital elements of hundreds of secret objects, like the [NOSS satellites](https://www.satobs.org/noss.html), are not available from official sources. An informal network of amateurs specializes in finding and keeping track of such satellites. Regular accurate positional measurements are needed for this kind of detective work.
Positional observers are also the primary public source of brightness data for all kinds of satellites. Prediction services, such as the [Heavens-Above](http://www.heavens-above.com/) web site, make use of the standard magnitude data produced by amateurs to provide accurate magnitude estimates of satellites.
Positional observers make precise measurements of the position and time of a satellite at several points along its track, enabling its orbital elements to be determined or updated. Skilled observers routinely meet or exceed positional accuracy of 0.03 degrees, and timing accuracy of 0.1 second.
The required investment in equipment is modest - a pair of 7 x 50 binoculars, a stopwatch, and an accurate star atlas are sufficient to try your hand at the hobby. Experienced observers graduate to larger binoculars, of at least 80 mm aperture, not only to observe fainter satellites, but to reveal a greater number of closely spaced reference star-pairs, which are key to achieving precise observations, as explained below.
The easiest way to determine the position of a satellite is when it passes between two not too distant stars, say at most half a degree apart. You start the stopwatch at the moment that the satellite intersects the imaginary line joining the two stars; at the same time, you judge the fraction of the distance of the point of intersection from star A to star B. For example, 40 percent of the distance from A to B, down from A toward B. The most accurate measurements are possible when the line connecting the two stars is perpendicular to the path of the satellite.
Skilled observers are able to judge the point of intersection between a pair of stars to an accuracy of about 5 percent of the distance between the stars. For example, using stars spaced 0.5 deg, yields accuracy of about 0.025 deg. Star pairs that close are infrequently seen using 7x50 binoculars, which tends to limit the accuracy you can achieve. Using 80 mm binoculars will reveal many more faint star-pairs, enabling you to more consistently achieve accuracy of 0.03 deg or better.
The accuracy of a position does not only depend on the way it is measured, but on the angular velocity of the satellite. A low orbiting satellite (lower than 500 km) moves across the observer's sky with an angular velocity of 0.5 to 1 degree per second. During a time interval of 0.1 s (our timing precision) the satellite moves between 3 and 6 arc minutes, which will ultimately be our positional accuracy. Naturally, for higher objects the angular velocity will be smaller and the positional accuracy will accordingly be better.
To determine the actual time of your observation, stop the stopwatch at a known reference time, provided by an accurate time signal, such as radio station WWV, and subtract the recorded duration from the reference time. For best accuracy, use a time source that provides audible ticks at each second. You should try to achieve 0.1 seconds precision or better. Always report observations using Universal Time (UT) (also called Greenwich Mean Time - GMT).
To determine the actual position of your observation, look up the reference stars in a star atlas that has sufficient scaling to enable estimating accuracy of 1 or 2 arc minutes. Now you plot the satellite's position in the star atlas. For example, if we call '1' the full distance on the segment joining the two stars, and if you estimated that the satellite passed at '0.4' from one star, you can easily plot the satellite's position in the star atlas (with a ruler). You can then estimate the coordinates of the satellite's position from the plotted position in the star atlas.
Alternatively, you can look up the coordinates of the reference stars in a star catalogue and calculate the satellite's position using spherical trigonometry.
Ted Molczan has written [ObsReduce](https://www.satobs.org/programs/ObsReduce/ObsReduce.html), an MS Windows program that reduces observations of satellites relative to the background stars into their precise coordinates. Observers identify their reference stars in a simulated binocular or telescope field of view, select them using the mouse, enter the observed geometric and positional data, and the program automatically produces a formatted observation report.
Several [reporting formats](https://www.satobs.org/position/posn_formats.html) have been devised to facilitate the exchange of positional observations. Each observation report should include :

* Satellite Identifier(s). There are actually two formats for this. The catalogue-number (e.g. 16619), and the COSPAR international code (e.g. 1986-017A). The COSPAR code, YYYY-NNNP, gives the year (YYYY) of launch, the number of the launch that year (NNN) and the letter (P) indicating the element of that launch (from A onwards), for example the primary payload may be A, a secondary B and the accompanying rocket C. All reporting formats include at least the COSPAR identifier.

* time in UT, up to 0.01 s accuracy.

* position in Right-Ascension and Declination.

* estimated accuracy in time and position.

* the satellite's magnitude (optional).

* the observer's exact location (longitude, latitude and height), accurate to 100 metres or better.

* the observing conditions


* the equipment used

*(sent while Claude was working)*

---

### **User** — 2026-11-04 05:59:11

IOD Observation Format Description
Positional observations of satellites provide the basis to determine their orbits, and to study their optical characteristics. The latter provide the basis for the accurate brightness predictions that we have come to expect from our favourite prediction software or service.
Positional observations are analysed using computer programs, so observers need a systematic and accurate reporting format, ideally one that can be read by both humans and machines. The IOD (Interactive Orbit Determination) format is one of [several](https://www.satobs.org/position/posn_formats.html) that meet this need.
George D. Lewis developed the IOD observation format, and a supporting data-entry program, called ObsEntry, available [here](http://users2.ev1.net/~mmccants/programs/obsentry.zip).
Below is a reproduction of the description of the IOD format, that is included with ObsEntry.

```
		Interactive Orbit Determination (IOD) Version 0

			       George D. Lewis

				10 October 1998

			 Copyright (C) 1998, G. Lewis


		   IOD Observation Format Description

			     Clarified 24 Feb 2002
	  (more realistic examples, cols 32-40, 42-43, 48-54, 56-61)


--------------------------------------------------------------------------------
	10        20        30        40        50        60        70        80
123456789 123456789 123456789 123456789 123456789 123456789 123456789 123456789

					       AAAAAAA+EEEEEE
		       YYYYMMDDHHMMSSsss tt ce RRRRRRR-DDDDDD pp
12345 98 123XYZ 2007 S 19981122112233444 56 14 1122334-112233 46 B+MMm Mm SSSsss

Examples:
12345 98 123A   2007 G 20081122112233444 56 14 1122334+112233 39 S
12345 98 123A   2007 F 2008112211223344  56 25 1122   +1122   28 R+05  1
12345 98 123A   2007 P 200811221122334   27 35 11223  +112    27 S+070 10
12345 98 123LEO 2007 B 20081122112233    18 75 1122334+112222 36 V+110 1
12345 98 123UNK 2007 F 200811221122000   27                      B-005 05
12345 98 123UNK 2007 F 20081122112233444 28                      V+095 05
12345 98 123UNK 2007 F 200811221123400   27                      P-010 05  10000
		2007 O 20081122
		2007 C 200811231130

--------------------------------------------------------------------------------


Cols   1- 15: Object number and international designation.

Cols  17- 20: Four digit station number.

Col       22: Station status code.

	      Sky condition codes:

	      E = excellent: no Moon/clouds, great seeing, minimal air/light pollution
	      G = good     : no Moon/clouds, conditions could be better, but not much
	      F = fair     : young/old Moon, some air/light pollution making fainter stars invisible
	      P = poor     : gibbous Moon, haze, more air/light pollution making more stars invisible
	      B = bad      : bright Moon, air/light pollution, some clouds; difficult
	      T = terrible : bright Moon, air/light pollution, looking through clouds

	      Station status codes:

	      C = clouded out
	      O = sky clear, but observer not available

	      The purpose of the 'C' and 'O' codes is to enable reporting of
	      "station unavailable" status to a network coordinator.

Cols  24- 31: UTC date. Four digit year, two digit month and day of month.

Cols  32- 40: UTC time. Two digit hour and minute. Seconds are reportable to
	      a precision of 0.001 second (same as TLE precision). NON-SIGNIFICANT
	      DIGITS MAY BE ZERO (0), NON-ZERO (1-9) or BLANK. The whole field
	      may be blank if no time reported when using station status codes.
	      Expected accuracy is stated using cols 42-43.

Cols  42- 43: Time uncertainty. Omit if no time reported or station status codes
	      used. Expressed as MX, where M = mantissa, and X = exponent input.
	      Evaluated as M*10E(X-8). Examples:

			     MX = 15: 1*10E(5-8) =   0.001 sec
			     MX = 56: 5*10E(6-8) =   0.05  sec
			     MX = 17: 1*10E(7-8) =   0.1   sec
			     MX = 97: 9*10E(7-8) =   0.9   sec
			     MX = 18: 1*10E(8-8) =   1     sec
			     MX = 28: 2*10E(8-8) =   2     sec
			     MX = 58: 5*10E(8-8) =   5     sec
			     MX = 19: 1*10E(9-8) =  10     sec
			     MX = 29: 2*10E(9-8) =  20     sec
			     MX = 99: 9*10E(9-8) =  90     sec  (largest)

Col       45: Angle format code. BLANK IF NO POSITION IS REPORTED.
	      To allow for user preference, four RA/DEC and three AZ/EL
	      formats are provided. The first six of these are the same
	      formats as used in the OTWG system, with the exception that
	      one less digit of precision is available in both position
	      coordinates. Positional coordinates are reported in columns
	      48-61. The positional uncertainty is reported in columns 63-64.
	      The seven positional formats are:

				     50        60
				    89 123456789 1234

		 Format 1: RA/DEC = HHMMSSs+DDMMSS MX   (MX in seconds of arc)
			2: RA/DEC = HHMMmmm+DDMMmm MX   (MX in minutes of arc)
			3: RA/DEC = HHMMmmm+DDdddd MX   (MX in degrees of arc)
			4: AZ/EL  = DDDMMSS+DDMMSS MX   (MX in seconds of arc)
			5: AZ/EL  = DDDMMmm+DDMMmm MX   (MX in minutes of arc)
			6: AZ/EL  = DDDdddd+DDdddd MX   (MX in degrees of arc)
			7: RA/DEC = HHMMSSs+DDdddd MX   (MX in degrees of arc)


Col       46: Epoch code. BLANK IF AZ/EL.

	      0 or blank = of date
	      1 = 1855
	      2 = 1875
	      3 = 1900
	      4 = 1950
	      5 = 2000
	      6 = 2050

Cols  48- 54: Observed RA or AZ. NON-SIGNIFICANT DIGITS MAY BE ZERO (0),
	      NON-ZERO (1-9) or BLANK. Expected accuracy is stated using
	      cols 63-64.

Col       55: DEC or EL sign

Cols  56- 61: Observed DEC or EL. NON-SIGNIFICANT DIGITS MAY BE ZERO (0),
	      NON-ZERO (1-9), OR BLANK. Expected accuracy is stated using
	      cols 63-64.

Cols  63- 64: Positional uncertainty. Expressed as MX, where M = mantissa,
	      and X = exponent input. Evaluated as M*10E(X-8). Given in seconds,
	      minutes, or degrees of arc according to the format used. Assumed
	      to apply equally to both components.

	      Examples of positional uncertainty ("/" = "or"):

		     MX = 34  3*10E(4-8) =  0.0003 deg/min/sec of arc
		     MX = 56: 5*10E(6-8) =  0.05   deg/min/sec of arc
		     MX = 17: 1*10E(7-8) =  0.1    deg/min/sec of arc
		     MX = 97: 9*10E(7-8) =  0.9    deg/min/sec of arc
		     MX = 18: 1*10E(8-8) =  1      deg/min/sec of arc
		     MX = 28: 2*10E(8-8) =  2      deg/min/sec of arc
		     MX = 58: 5*10E(8-8) =  5      deg/min/sec of arc
		     MX = 19: 1*10E(9-8) = 10      deg/min/sec of arc
		     MX = 29: 2*10E(9-8) = 20      deg/min/sec of arc
		     MX = 99: 9*10E(9-8) = 90      deg/min/sec of arc (largest)


Col       66: Optical behavior code. BLANK IF NO DATA.

	      Optical Tracking Working Group Codes:

	      E = unusually faint because of eclipse exit/entrance
	      F = constant flash period
	      I = irregular
	      R = regular variations
	      S = steady
	      X = irregular flash period

	      Flash Period Codes:

	      B = time zero for averaging several flash cycles
	      H = one flash in a series
	      P = end time for averaging several flash cycles. Time interval
		  from last "B" report divided by flash period reported on
		  this line gives number of flashes that occurred since "B".

	      Miscellaneous Visibility Codes:

	      A = became visible (was invisible); use E for eclipse exit
	      D = object in field of view, but not visible
	      M = brightest
	      N = faintest
	      V = best seen using averted vision


Cols      67: Visual magnitude sign.  BLANK IF NO DATA.

Cols  68- 70: Visual magnitude. Implied decimal point
	      between cols 69 and 70. BLANK IF NO DATA.

Cols  72- 73: Magnitude uncertainty. Implied decimal point
	      between cols 72 and 73. BLANK IF NO DATA.

Cols  75- 80: Flash period in seconds. Implied decimal point
	      between cols 77 and 78. BLANK IF NO DATA.
```

*(sent while Claude was working)*

---

### **User** — 2026-11-04 05:59:46

R.D.E. Observation Format Description
Positional observations of satellites provide the basis to determine their orbits, and to study their optical characteristics. The latter provide the basis for the accurate brightness predictions that we have come to expect from our favourite prediction software or service.
Positional observations are analysed using computer programs, so observers need a systematic and accurate reporting format, ideally one that can be read by both humans and machines. The R.D.E. format is one of [several](https://www.satobs.org/position/posn_formats.html) that meet this need.
This format is named after its main user, Russell D. Eberst. It is an abbreviated and modified form of the [U.K. format](https://www.satobs.org/position/UKformat.html), in which each report consists of at least three lines.


```
For example:

2420 0405 0.211 1204
05
0401203 205744.76 185544+324606 4.0 5.8 3 R

Line 1: contains information common to all of the observations that follow.
        It appears once per report.

Column number:
00000000011111111112
12345678901234567890

Example:
SSSS YYMM T.tSF PPPE
2420 0405 0.211 1204

Cols  01-04: Observing Site Number.


Cols  06-09: UTC Year and Month of Observation, in YYMM format


Cols  11-13: Time Accuracy, in seconds. Format is T.t


Col      14: Time Standard Code.

             Code 1: radio time signal, e.g. MSF. DIZ, WWV
                  2: U.K. Post Office Speaking Clock
                  3: B.B.C. time pips

Col      15: Position Format Code.

             Russell always uses Code 1, with whole arc seconds:

                       Column 1222222222233
                              9012345678901
             Code 1: RA/DEC = HHMMSS+DDMMSS


Cols  17-19: Position Accuracy.

             Russell reports 120 arc seconds, as SSS


Col      20: Epoch of star chart used to determine position

             Code 1: 1855
                  2: 1875
                  3: 1900
                  4: 1950
                  5: 2000
                  6: 2050
                  0: any other date, which should be sent separately

                  Russell always uses code 4.


Line 2: contains the day of the month, in a two digit format, including leading zero,
        It always appears on line 2 of a report, and whenever the day of the month of
        the observations changes within a report.


Line 3 - contains the details specific to each observation.
         There is one such line per observation.

Column number:
0000000001111111111222222222233333333334444444444
1234567890123456789012345678901234567890123456789

Example:
YYLLLPP HHMMSS.ss HHMMSS+DDMMSSbbbbffff S O
0401203 205744.76 185544+324606 4.0 5.8 3 R

Cols  01-07: Object's International Designation.

             Cols 01-02: year of launch
             Cols 03-05: sequential number of the launch in the year.
             Cols 06-07: piece from the launch, e.g. piece A denoted as 01

             If object is unknown, enter 9900000.


Cols  09-17: UTC Time of Observation, in HHMMSS.ss format.


Cols  19-24: Observed Right Ascension or Azimuth, in HHMMSS format.


Cols  25-31: Observed Declination or Elevation, in DDMMSS format.


Cols  32-35: Brightest Visual Magnitude

             This is the brightest stellar magnitude attained by the satellite during
             the period of one minute centred on the time of the observation, It is
             entered as a 3 digit number.

             If magnitude is negative, enter "-" in column 32
             Columns 32-35 state the numerical value, formatted as M.m


Cols  36-39: Faintest Visual Magnitude

             Format is the same as for Brightest Visual Magnitude.

             If the magnitude is constant, repeat the Brightest Visual Magnitude entry


Cols  40-42: Flash Period

             Time in seconds between successive maxima, formatted as SSS.sss

             If value is less than 1, show one leading zero.
             Show only significant trailing zeros.
             Omit decimal point for whole number values.


Col      43: Remarks.

             May occur later than col 43, depending upon length of Flash Period entry.

             Code S: steady magnitude
                  I: irregular brightness variations
                  R: regular brightness variations
                  F: flashing with constant flash period
                  X: flashing with irregular flash period
                  E: unusually faint because of eclipse exit/entrance
```

*(sent while Claude was working)*

---

### **Claude** — 2026-11-04 06:02:16

Behaviour codes, the arcminute fix and RDE output are in. Fixing two comments and running the tests.

I checked the reports against the IOD description you pasted and found one real bug, which is fixed. I also added R.D.E. output. Both are pushed (`e706735`), and the full suite passes (671 passed, 1 skipped).

**The bug:** in the angle format I write (format 2), the position uncertainty in columns 63–64 is in arcminutes. I was writing degrees, so every IOD line overstated our uncertainty 60 times. A typical 0.007° now comes out as `47` (0.4′) instead of `26`. Uncertainty codes are also rounded up now, so they are never understated.

**What the spec confirmed:**
- **Photometry columns:** they were already right. Magnitude goes in 67–70 (`+045` = 4.5), its uncertainty in 72–73, and the flash period in 75–80 (`002250` = 2.25 s).
- **Behaviour codes:** the full set is now used. Periodic light gives F (constant flash period) when it comes in short flashes, or R (regular variations) when smooth. Non-periodic light gives X (irregular flash period) for flashes, or I (irregular) otherwise. I had been using X for everything irregular.
- **Sky codes:** `--status` now also accepts B (bad) and T (terrible).

**What a report should include** (your second page):
- **Conditions and equipment:** the draft post now has a "Conditions:" line, with free text via `--conditions`; `--equipment` overrides the equipment line.
- **Location:** the site is written to about 10 m, inside the 100 m that page asks for.
- **Accuracy:** that page says skilled visual observers reach about 0.03° and 0.1 s. On synthetic data the finder's positions come out at 0.002–0.01°, so on position it matches or beats them. The time error will be whatever the clock gives: about 0.2 s if you set it from the phone, about 1 s from SkySafari.

**R.D.E.:** `observations_rde.txt` now holds the same reported points in Russell Eberst's format:
- a header line, then a day line whenever the day changes;
- brightest and faintest magnitude, flash period (`0` when there is none), and the remark code;
- `9900000` for unknown objects.

The format's own example line comes out exactly. Three things to check before posting R.D.E.:
- **Piece numbering:** I assumed A=01 skipping I and O, as COSPAR letters do.
- **Time standard code:** "1" (radio time signal) is used for network and phone time, since nothing else fits; `--time-standard` changes it.
- **Time accuracy:** the header field has room for at most 9.9 s, so an unset clock (10 s) shows as 9.9. Don't post that case anyway.

Still to confirm on the list: the `99999 99 099A` convention for unknown objects.

---

### **User** — 2026-11-04 11:34:03

what cmd do I now need for an OTA update and what do I need to download to my laptop?

---

### **Claude** — 2026-11-04 11:34:29

**On the finder (OTA):**

```bash
sudo /usr/local/bin/diofinder-update --ref claude/azimuth-on-0.11.74
```

The Update page in the web UI does the same if you give it that branch. A plain `diofinder-update` with no `--ref` would install the latest release tag, which doesn't have any of this work. This also brings the change that makes new captures record the camera's tone curve. Captures you made before updating still work offline, because the scripts fall back to the shipped curve.

**On the laptop:**

1. **The code:** clone or pull the same branch:
   ```bash
   git clone -b claude/azimuth-on-0.11.74 https://github.com/mconsidine/diofinder.git
   # or, in an existing clone:
   git fetch origin && git checkout claude/azimuth-on-0.11.74 && git pull
   ```
   You only need the `tests/` folder (`sat_identify.py`, `sat_tracks.py`, `sat_photometry.py`, `sat_orbit.py`, `satcheck_overlay.py`, `satcheck_archive.py`) and `tuning/imx477_finder.json`. Cloning the whole repo is simplest, and the scripts find the tuning file by their relative path.
2. **Python packages:** `pip install numpy astropy matplotlib pillow sgp4` (scipy isn't needed).
3. **Element files:** `classfd.zip` and `inttles.zip` from https://mmccants.org/tles/. You can reuse the copies you already have, but fresh ones match better. Or pass `--download` and the script fetches them itself.
4. **A capture:** after a session, download the `livecap_<UTC>.zip` from the finder's Utilities → Live captures.

**Running it:** in PyCharm, run from the repo root.

```bash
python tests/sat_identify.py livecap_XXXX.zip --elements classfd.zip inttles.zip --station 9999 --status G
python tests/sat_orbit.py fit livecap_XXXX_ident/observations.json
python tests/sat_orbit.py predict livecap_XXXX_ident/orbit/fit.json --site-from livecap_XXXX_ident/observations.json --hours 24
```

Results land in `livecap_XXXX_ident/`:
- `identification.txt`, the summary to read first;
- `observations_iod.txt` and `observations_rde.txt`;
- `seesat_post.txt`, the draft post;
- `lightcurve_*.png`;
- refined TLE/OMM files for anything identified.

Before a capture you might post, set the finder's clock from your phone (Utilities → Clock). Otherwise the reports carry a 10 s time uncertainty and a do-not-post warning.

---

### **User** — 2026-11-04 12:32:32

How do you "run from the repo root" "in PyCharm"?  And where would those commands go, versus using the IDE editor??

---

### **Claude** — 2026-11-04 12:33:06

You run them in PyCharm's built-in terminal, not the editor. The editor is only for reading and changing code. "Run from the repo root" just means that terminal should be sitting in the `diofinder` folder you cloned, which is where PyCharm's terminal opens by default.

There are two ways to do it.

## Option 1: PyCharm's terminal (simplest)

1. **Open the project.** Use File → Open and pick the cloned `diofinder` folder, the one that contains `tests\` and `tuning\`.
2. **Set the interpreter once.** Go to File → Settings → Project: diofinder → Python Interpreter. Pick or create one (a new virtualenv is fine).
3. **Open the terminal.** Use View → Tool Windows → Terminal, or Alt+F12. A command prompt opens at the bottom, already in the project folder and using that interpreter.
4. **Install the packages.** Type this once in the terminal and press Enter:
   ```
   pip install numpy astropy matplotlib pillow sgp4
   ```
5. **Put your downloads in the project folder.** That's the capture zip, `classfd.zip` and `inttles.zip`. Then type:
   ```
   python tests\sat_identify.py livecap_20261105T020000Z.zip --elements classfd.zip inttles.zip --station 9999
   ```
   Output streams into the terminal. The results folder (`livecap_..._ident`) appears in the project tree on the left, and you can open its text files and PNGs there.

Two Windows notes:
- **One line:** type each command on a single line. The `\` at line ends in my earlier examples is Linux line-continuation and doesn't work in PowerShell.
- **Files elsewhere:** if the files live in another folder, use full paths in quotes, e.g. `"C:\Users\you\Downloads\livecap_….zip"`. The scripts don't care what folder you're in; that only affects how you type file paths.

## Option 2: a Run configuration (click ▶ instead of typing)

1. Go to Run → Edit Configurations → + → Python.
2. Fill in the configuration:
   - **Script path:** `…\diofinder\tests\sat_identify.py`
   - **Parameters:** `livecap_….zip --elements classfd.zip inttles.zip --station 9999`
   - **Working directory:** the `diofinder` folder.
   - **Interpreter:** the one from step 2 above.
3. Save it, then press ▶. Output appears in the Run window at the bottom.

Make one configuration each for `sat_identify`, `sat_orbit fit` and `sat_orbit predict`. For the `sat_orbit` ones, set the script to `tests\sat_orbit.py`, with Parameters starting `fit …` or `predict …`. Then you only edit the parameters for each new capture. This is also handy if you want to use PyCharm's debugger.

I'd start with Option 1 because it's quicker to try. Switch to Option 2 once you're re-running the same steps on each capture.

---

### **User** — 2026-11-04 12:53:24

ok, all set on that.  just to confirm - I need a new livecap_....zip archive generated with the new code, as opposed to the livecap archive I got a few days ago, right?

---

### **Claude** — 2026-11-04 12:53:41

No, the archive from a few days ago will work with the new scripts. Two things in it are older, and neither stops a run:

- **Tone curve:** older archives don't record it, so the photometry assumes the shipped finder curve and prints a note saying so. That's correct as long as the finder was on its default "finder" tuning.
- **Clock record:** if the archive predates clock recording, the reports use a 10 s time uncertainty and the draft post is marked "CLOCK NOT VERIFIED". For testing that doesn't matter. If you know how the clock was set that night, `--time-unc 0.2` (phone) or `--time-unc 1` (SkySafari) overrides it.

The more practical limit is what that capture contains. If it's the 2 Nov one, its only trail was an aircraft, so the run should correctly report it as unidentified. That exercises the pipeline but not identification, orbit fitting or satellite photometry.

So: use the old archive now to check everything runs on your laptop. For a real test, take a new capture after the OTA update. Set the clock from your phone first, then capture during the 1–2 hours after sunset, when many satellites are sunlit.

---

### **User** — 2026-11-04 13:06:34

This is the result.  I thought that in the baked PI image I created that the tone curve was customized at that point, ie it is the non-linear/non-manufacturer's curve.  So it should be linearized, no?

---

### **Claude** — 2026-11-04 13:06:49

Yes, it is linearized. That note doesn't mean the curve was skipped. Your old archive doesn't record which tone curve the camera used, so the script uses the repo's copy of `imx477_finder.json`. That file is the custom curve in your image, the steep non-manufacturer one, and the script inverts it to get back to linear light. "Assuming" only means the curve was taken from the repo rather than read from the archive.

That assumption is safe for your image. `tuning/imx477_finder.json` hasn't changed since v0.11.41 in July, so any image built since then has exactly this curve. It would only be wrong if the finder had been switched to the "scientific" or "standard" profile (Camera page → tuning) when you captured.

Archives made after the OTA update record the curve, and the note goes away.

The result itself didn't come through in your message, only the text. If you paste `identification.txt` or the console output, I'll go through it.

---

### **User** — 2026-11-04 13:09:29

```
(.venv) PS C:\Users\matt\Downloads\diofinder> python tests\sat_identify.py livecap_20261102T020749Z.zip --elements c:\users\matt\Downloads\TLEs\classfd.zip c:\users\matt\Downloads\TLEs\inttles.zip --station 9999                                                                                                               
436 element sets loaded from 2 file(s)
WARNING: clock unknown (capture predates clock recording). Times may be seconds off: do not post these observations unless you know the clock offset (--time-unc).
note: this capture predates tone-curve recording; assuming the shipped finder tuning (imx477_finder.json)
photometry: tone curve assumed finder tuning; 2893 star measurements in 82 frames, rms 0.333 mag, vignetting -0.17 mag at the corners
linking frame-by-frame residuals (hand-tracked or long arcs)...
  residuals: 50/82 frames
  1 linked track(s), 0 new or longer than the stack trails
track S1.1 (stack): 68 frames, 2026-11-02 02:07:49.182 UTC; SatChecker: no match; position sigma 0.030 deg
  magnitude 1.8 +- 0.3 (67 clean frames, frame-to-frame scatter 1.23); light curve along the dashes: behaviour R, flash period 0.69 s, amplitude 5.2 mag
        StarSh                   99159  classfd.zip:classfd.tle  elements +69.2 d old, -276.7 s late, rms 4.019 deg, cross 0.994 deg, rate x0.80, direction 163 deg, IN SHADOW
results in livecap_20261102T020749Z_ident
```

---

### **Claude** — 2026-11-04 13:12:34

That's a sensible result. The only trail in this capture is the aircraft from 2 Nov, and the script correctly found no satellite that matches it. Your run also showed that flashing lights were being mislabelled as smooth variation; that's fixed and pushed.

**What the output says:**

| Output | Meaning |
|---|---|
| 436 element sets, 82 frames, 2893 star measurements | Loading and per-frame star calibration ran over the whole capture. |
| rms 0.333 mag | How well star brightness matches the catalog in this capture: about 0.3 mag scatter, more than I hoped (0.1–0.2). Likely causes are faint stars only a few steps above the black sky in 8-bit, catalog magnitudes not being visual V, and focus. The track's ±0.3 already includes it. Recheck on a clear, well-focused night. |
| 1 linked track, 0 new | The frame-by-frame tracker found the same object as the stack-trail finder, so it wasn't counted twice. |
| magnitude 1.8, frame-to-frame scatter 1.23 | A bright object whose brightness jumps a lot from frame to frame. That fits aircraft lights. |
| flash period 0.69 s, amplitude 5.2 mag | Strobes or a beacon. |
| StarSh … rms 4.0°, direction 163°, IN SHADOW | The closest candidate: 4° off, moving almost the opposite way, and in Earth's shadow. It is correctly not marked MATCH. |

**What your run caught:** the light curve was labelled "R" (smooth regular variation), which is wrong for strobes. The light between flashes is almost zero, and that fooled the flash test, which compared the bright and faint excursions in magnitudes. The test now measures what share of the time the light is high, in linear light. Strobes come out as "F", as would a satellite that vanishes between glints.

**New check note:** any unidentified track that flashes like aircraft lights now gets a "CHECK: may be an AIRCRAFT" note, in both `identification.txt` and `seesat_post.txt`. I kept it as a warning rather than leaving such tracks out, because a tumbling unknown satellite can look the same.

To get the fix on the laptop, run `git pull` in the PyCharm terminal and re-run the same command. With this capture you should then see behaviour "F" and the aircraft note.

---

### **User** — 2026-11-04 13:21:16

ok, so now i need to get the diofinder image updated on the device and get new live capture archives.  I'll do that in the next few days.  In the meantime, what are the other scripts I can run on the livecap archive to get plots etc?

---

### **Claude** — 2026-11-04 13:21:40

Three other scripts work on a livecap archive and produce plots. Run them from the PyCharm terminal in your `diofinder` folder, as before. Each writes into a new folder next to the zip.

**1. `satcheck_overlay.py`** — marks predicted satellites on your frames. It needs internet, because it asks SatChecker (IAU CPS) which public-catalog satellites crossed the field.
```
python tests\satcheck_overlay.py livecap_20261102T020749Z.zip --stack --chart
```
Output goes to `livecap_…_sats\`:
- **`frame_NNNNNN_sats.png`**: each frame that had a satellite in it, with the predicted streak drawn as two rails. The real trail should sit between them. Labels give name, direction and rate.
- **`stack_N_moving.png`**: frames aligned on the stars and stacked so only moving things remain. A satellite shows as a dashed line; unexplained trails are outlined in red, and a flashing one is labelled "aircraft?". `stack_N.png` is the same stack with the stars kept.
- **`chart_N.png`**: a sky chart of the predicted tracks with your frame outline.
- **`report.csv`** and **`trails.csv`**: one row per prediction, and every detected trail with rate, direction and match.
- **`satchecker_responses.json`**: saved answers. Add `--responses livecap_…_sats\satchecker_responses.json --offline` to re-plot without asking again.

Useful extra options:
- `--all-frames` draws every frame, not just those with a satellite.
- `--frames 120-180` limits it to a range.
- `--window 600` stacks 10-minute stretches.
- `--show-unlit` and `--show-stationary` also show satellites in Earth's shadow and geostationary ones.

**2. `satcheck_archive.py`** — no plots, just a text listing of SatChecker hits per frame. It's quick, and handy to see whether anything was predicted at all.
```
python tests\satcheck_archive.py livecap_20261102T020749Z.zip --group 30 --out hits.json
```
`--group 30` makes one query per 30 frames. `--dry-run` only prints the query URLs.

**3. `sat_orbit.py predict`** with an existing element file (`fit.json`, a TLE or OMM file, or a zip of them), not one of your fits. This plans nights for the new captures: it lists when any known object passes over your site, sunlit, in a dark sky, with a pointing table.
```
python tests\sat_orbit.py predict c:\users\matt\Downloads\TLEs\classfd.zip --norad 31701 --lat 43.95 --lon -72.667 --elev 330 --hours 48 --min-elevation 25 --out plan
```
- Longitude is east-positive, so a western longitude is negative.
- Change `--norad` to pick any object in the file. Start times are UTC; add `--start 2026-11-06T00:00:00` to begin at a set time.
- I used the Vermont site from our test data; put in your own.

Also, from the `sat_identify` run you already did, look in `livecap_20261102T020749Z_ident\`:
- `lightcurve_S1.1.png` plots the aircraft's brightness against time. You should see the strobe flashes.
- `observations_rde.txt` and `seesat_post.txt` are worth a look to see the report formats, even though this object isn't one to post.

After `git pull`, a re-run shows the corrected "F" behaviour and the aircraft note.

---

### **User** — 2026-11-04 13:28:50

For  #3, is --norad used to query a specific satellite?  How would I get a list of predicted passes of anything in - in this case - classfd.zip?

---

### **Claude** — 2026-11-04 13:29:54

Now a quick test against the classfd sample and add a unit test.

Yes, `--norad` picks one satellite by its catalog number. Before this change, leaving it off didn't list everything, though: the script quietly predicted only the first object in the file. I've changed it so that leaving off the filter predicts every object in the file. It's pushed; run `git pull` first.

**Every object in classfd.zip:**
```
python tests\sat_orbit.py predict c:\users\matt\Downloads\TLEs\classfd.zip --lat 43.95 --lon -72.667 --elev 330 --hours 48 --min-elevation 25 --out plan
```
You get one line per visible pass, in time order:
```
  highest at (UTC)          object          number  alt  az   RA      Dec    rate    visible (UTC)      elements
  2026-11-06 01:12:40.000   NOSS 3-4 (A)     31701   62  140  123.40 +45.60  0.45/s  01:09:50-01:15:30  +3.2 d
  ...
```
- **"highest at" and "visible"**: the time of peak altitude, and the window when it's above 25°, sunlit and the sky is dark.
- **"rate"**: how fast it crosses the sky, in degrees per second.
- **"elements"**: how old the element set is. The older it is, the more the timing may be off; classified objects manoeuvre. The path across the sky stays close, so be pointed early rather than precisely on time.

`--out plan` writes the same list as `plan\predictions.txt`, `predictions.csv` (for a spreadsheet) and `predictions.json`. Use your own latitude, longitude and elevation; the ones above are from our test data, and longitude is east-positive.

**Narrowing it down:**
- **Specific satellites:** `--norad 31701,31708` (comma list).
- **By name:** `--name NOSS` matches any name containing the text, case-insensitive, e.g. `USA` or `LACROSSE`.
- **Pointing tables:** `--tables` adds one under each pass (UTC, alt/az, RA/Dec every `--step` seconds, default 30).
- **Slow objects:** geostationary and other high objects (under 2 revs/day) are skipped by default, because they'd show as all-night "passes"; `--all-orbits` includes them.
- **Start time:** `--start 2026-11-06T00:00:00` (UTC) begins the window at a set time instead of now.

It takes about 15–20 s for the whole file over 48 hours. You can also run it on `inttles.zip`, or both one after the other.

---

### **User** — 2026-11-04 13:46:50

I want a script that will specifically run against a livecap archive that contains an ISS trail.  The idea is that up-to-date ISS TLEs are always available at https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE  So if the test script assumed that it was ISS in the livecap archive AND assumed an internet connection, then I could do a live cap, download it to the laptop, and run the ISS script against it with the sole goal of testing how well the setup creates a reasonable "current" TLE by comparison to the downloaded one.  Does that make sense as a way of testing this whole setup against a "ground truth"?  Also, is there a way where all scripts that have been created can be changed to accept either 1) a single a tle (zipped or not), 2) a comma-separated listed of tle files (zipped or not), or 3) a folder containing tle files (zipped or not)?  Are there non-TLE formats that should also be accepted?  In the case of overlap or redundancy, the most recent elements are the ones that should be used.

---

### **Claude** — 2026-11-04 13:47:43

ISS against a fresh CelesTrak TLE is a good ground truth for this setup. Building the ISS check script and the shared element-file input now.

Shared element-source handling in sat_identify first.

The ISS check works end to end on synthetic data: it recovers a simulated 0.7 s clock error to within 0.001 s. Updating the docs and running the full suite.

Yes, the ISS against CelesTrak's current TLE is a good ground truth. I've written the script (`tests/iss_check.py`). Every script now also accepts element files in all the forms you listed, with the newest epoch winning. Both are pushed (`e19ceae`); `git pull` on the laptop. The full test suite passes, but everything here is checked on simulated data only so far.

**Why the ISS works as a reference:** its elements are updated several times a day and are good to about a kilometre and under 0.1 s near their epoch. Its orbit is also almost exactly circular, which suits the one-pass circular fit. So one ISS pass tests each stage separately:

| What's compared | What it tests | Healthy result |
|---|---|---|
| Distance across the true track | Plate solves, star matching, trail centroids | About 1 px (0.015°) |
| Distance along the track, in seconds | Sensor timestamps plus your clock | Under 0.2–0.3 s with a phone-set clock |
| Orbit fitted from your pass alone, without the TLE | What one pass can actually determine | Period within a few %, inclination within a fraction of a degree, next-pass timing within minutes |
| Measured magnitude vs a rough prediction | Photometry | ±0.5 mag; often only a bright limit, because the ISS saturates the camera |

Two of these have useful readings:
- **The along-track number is your clock error.** If the positions sit 0.7 s ahead of the TLE, your clock was 0.7 s behind.
- **The orbit comparison is the real test of the "derive a TLE" idea.** It tells you honestly how far one pass gets you.

On a simulated pass with the clock deliberately 0.7 s behind, it reported 0.70 s and near-zero across-track error. Real sky will be worse.

**Running it** (from the PyCharm terminal):
```
python tests\iss_check.py livecap_….zip
```
By default it downloads the ISS TLE from your CelesTrak link, keeping it for 2 hours because CelesTrak asks clients not to re-fetch more often. `--tle iss.tle` uses a saved copy instead. Results go to `livecap_…_iss\`:
- `iss_report.txt`, which has the four sections above with a verdict on each;
- `iss_residuals.png`, the along- and across-track error for each frame;
- `iss_sky.png`, your measured positions against the TLE track;
- all the usual `sat_identify` outputs.

**Tips for the capture:**
- **Clock:** set it from your phone first, unless you're deliberately measuring the clock.
- **Timing of the pass:** pick one in a dark sky with the ISS sunlit and at least 30° up. `sat_orbit.py predict` on the CelesTrak URL lists them.
- **Exposure:** use 0.2–0.3 s. The ISS moves up to about 1°/s, so longer dashes run off the frame quickly.
- **TLE age:** fetch the TLE within a day or two of the pass. A reboost moves the ISS, and the report warns when the element set is more than 3 days from the pass.

**Element-file input (all scripts):** this applies to `sat_identify --elements`, `sat_orbit predict` and `sat_orbit fit --start`, and `iss_check --tle`.
1. **A single file**, zipped or not: plain, `.zip` or `.gz`.
2. **A comma-separated list:** `--elements classfd.zip,inttles.zip,mine.tle`. Space-separated also still works.
3. **A folder:** every element-looking file in it and its subfolders is read (`.tle .txt .3le .2le .csv .xml .json .kvn .omm .zip .gz`).
4. **A URL**, such as your CelesTrak link, which is downloaded and cached.

When the same satellite appears more than once, the most recent epoch is used, as you asked.

**Non-TLE formats:** yes, one matters, and it is now read in all four of its forms. That's the CCSDS OMM (Orbit Mean-elements Message): the same orbit information as a TLE, as XML, KVN (key = value), JSON or CSV. It's what CelesTrak and Space-Track now serve alongside TLEs, and the only form that can hold catalog numbers above 339999. I added KVN reading; the XML, JSON and CSV forms were already read.

The one format worth adding later is a CCSDS OEM ephemeris (precise position-velocity tables). NASA publishes one for the ISS, and it would be an even better ground truth than its TLE. I haven't added it; it's only worth doing if the TLE comparison ever turns out to be the limiting factor.

---

### **User** — 2026-11-04 13:59:23

This is great.  For ISS, does it look first to see if there's a cached version and only go fetch a new one if the cached version is more than 2 hours old or doesnt exist?  Would that be a more convenient approach for just the script that tests against ISS?

---

### **Claude** — 2026-11-04 13:59:30

Yes, that's already how it works. When you run `iss_check.py`, it:

1. looks for a cached copy of the CelesTrak ISS file in `C:\Users\matt\.diofinder_elements\`;
2. uses that copy if it is less than 2 hours old, without going online;
3. otherwise downloads a fresh one and overwrites the cache.

This applies to any URL you give any of the scripts, not just the ISS one, and each URL has its own cached file.

One gap worth closing: if the copy is older than 2 hours and the laptop is offline, the script currently stops with a network error. I'll make it fall back to the older copy, with a warning saying how old it is.

The offline fallback is done and pushed (`6042e7c`); `git pull` to get it. The cache now works like this:

| Situation | What the script does |
|---|---|
| Cached copy under 2 hours old | Uses it, no network |
| Copy older than 2 hours (or none) and online | Downloads a fresh one and replaces the cache |
| Copy older than 2 hours and offline | Uses the old copy with a warning, e.g. "download failed …; using the cached copy from 26.3 h ago" |
| No copy and offline | Stops with a clear message |

The new test covers all four cases.

On whether this suits the ISS script: I'd keep it as it is. The 2-hour rule only stops repeat downloads within one session and keeps CelesTrak happy, so a fresh run after a capture always gets current elements.

One case to know about: when analysing an older capture, the newest TLE may be days after the pass. That's usually fine for the ISS, but a reboost in between would show up as a timing offset. The report shows the element set's age at the pass and warns beyond 3 days. In that case, keep a copy of the TLE from capture night and pass it with `--tle`.

---

### **User** — 2026-11-04 14:03:03

Excellent.  I want to have you 1) save all of our exchanges in this chat from the point when I initiated the satellite idea, with the exception of saving any files created or uploaded, 2) create a conversation artifact that is complete, accurate and suitable to restart or continue the satellite discussion in a new chat, and 3) update the general conversation artifact for this thread with a new, current version saved in its own file, ie don't overwrite older conversation artifacts.

---

### **Claude** — 2026-11-04 14:03:27

I'm checking whether this thread already has a conversation summary saved somewhere, so the new version goes beside it rather than over it.
