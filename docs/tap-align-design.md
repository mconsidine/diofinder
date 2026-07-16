# Tap-to-align — set the boresight by tapping a star in the live view

Design spec for letting the user **tap a star in the web-UI live view** to set the
boresight, as an alternative to the SkySafari `:CM#` round-trip. Design/analysis
only — **not built**. **Model A** (below) is the intended implementation if this
is picked up; Model B is documented and deliberately deferred.

## 1. Goal & scope

Give the user a direct, on-screen way to calibrate the boresight: tap the star
the telescope's optical axis is on, and that pixel becomes the boresight. In
scope: a webui live-view interaction + the thin backend to apply it. Out of
scope: any change to the solve/pointing math, and (for v1) any change to the
existing `:CM#` align path — this is an *additional* way to set the boresight,
not a replacement for SkySafari sync.

## 2. What an align actually resolves (the key framing)

A `:CM#` align has exactly one job: set the **boresight pixel** — the pixel in
the finder frame that corresponds to the telescope's optical axis. That is the
*only* unknown. After any successful solve diofinder already knows the RA/Dec of
every pixel in the frame (`x_target`/`y_target` projection is the inverse it
already runs), so "where is the scope pointed" is solved; the align only says
*which pixel is center*.

Today that pixel is found **indirectly**: SkySafari names the star → comms
converts JNow→J2000 (`_do_alignment`, v0.11.53) → the solver projects that sky
coord into the frame → the landing pixel becomes the boresight (`x_target`,
`y_target` → `boresight_set`-equivalent persist in `_do_alignment`).

**Tapping supplies that pixel directly.** So it is a natural, first-class way to
do the same calibration.

## 3. The backend is (almost) already there

- **`boresight_set {"y":…, "x":…}`** (comms maint, `_handle_maint_command`)
  already exists: it bounds-checks against the frame, writes `boresight_y`/
  `boresight_x` to `cfg` + `shared_cfg`, and persists via `config.save_keys`.
  This is exactly the "make this pixel the center" operation — **no new solver
  math is required**. The next solve reports that pixel's RA/Dec automatically
  (the solver already solves *at* the boresight target pixel), and SkySafari's
  crosshair follows on its next `:GR/:GD` poll.
- The Config page already exposes `boresight_x`/`boresight_y` as editable
  fields, and `/boresight/center` (route) resets to frame center. There is **no
  `/boresight/set` webui route yet** — a tap feature adds a thin one wrapping the
  existing `boresight_set` maint command.

So the feature is **~80–90% front-end.**

## 4. Model A (recommended) — tap sets the boresight directly

The user centers a star in the eyepiece, recognizes it in the wider finder view,
taps it; the boresight moves to that pixel; SkySafari's crosshair follows. No
`:CM#` needed. Three pieces:

### 4.1 Coordinate mapping (the fiddly part)

A tap on the live-view `<img>` gives coordinates in *screen* space. Three
transforms must be inverted to get a full-frame pixel, in order:

1. **CSS fit-scaling** — the `<img>` is `max-width:100%`, so rendered size ≠
   natural size. Map via `img.naturalWidth / img.clientWidth` (use
   `getBoundingClientRect()` + `offsetX/Y`, accounting for `object-fit`).
2. **`/frame.jpg` downsample** — the served JPEG is half-res by default
   (`ds=2`; `?full=1` is native). Multiply the natural-image pixel by `ds` to
   reach full-frame (960×760) coordinates. The client must know `ds` (send it in
   the image URL it requests, or expose it).
3. **Rotate-180 toggle** — if the display-only `rotate-toggle` is on, the shown
   image is flipped; invert it: `x → W-1-x`, `y → H-1-y`.

All deterministic (~40 lines JS), but it must be exact or the boresight lands
off. Unit-testable as a pure JS function (map screen→frame given
img-rect/natural/ds/rotate) — pin it with a couple of fixtures.

### 4.2 Snap-to-centroid (touch precision)

Stars are 1–2 px; a fingertip is ±15–20 px, so a raw tap sets the boresight
*near* the star, not on it. Snap the tap to the nearest detected centroid.

The solver has the frame's centroids every frame but currently publishes only
the **named** centered star (`star_name`/`desig`/`mag`/`sep`). Add a small feed
of the current frame's detected star **pixel positions** — either:

- **(preferred) server-side snap**: a maint command
  `boresight_set_snap {"y":…, "x":…}` that snaps the requested pixel to the
  nearest solver-known centroid within a tolerance (e.g. ≤ `tracking_window_px`)
  before applying — keeps centroid data solver-side and makes the snap
  authoritative; or
- **client-side snap**: publish the frame's centroid list (new maint command /
  `latest_solution` field) and let the JS pick nearest. Lighter on the solver
  but adds an IPC payload and a second round-trip.

Preferred: server-side snap (one call, no extra payload, exact).

### 4.3 Webui route + UX

- New `POST /boresight/set` wrapping `boresight_set` (or `boresight_set_snap`),
  mirroring `/boresight/center`. Form-submit or AJAX.
- A **"Tap to set center"** mode toggle on the Home/Advanced live view: when
  armed, a tap shows a confirm ("Set boresight here?" with the snapped pixel and,
  nice-to-have, the star name), then POSTs. Disarm after one set.
- Reuse the reticle: after setting, the boresight reticle jumps to the new pixel
  on the next `/frame.jpg` (the boresight is cached comms-side, `_bs_cache`, TTL
  5 s — the set invalidates naturally within a poll).

## 5. Model A+ — named tap targets (nice-to-have)

The richer version: after a solve, overlay the **bright named stars** as tap
targets, so the experience is literally "tap Vega." The solver already projects
a sky coord to a pixel (the `target_sky_coord` → `x_target`/`y_target` path); the
same projection over the `star_names.csv` catalog entries in-frame yields
label positions. Publish `{name, x, y, mag}` for in-frame catalog stars (new
maint command or a solve field), and the webui draws tappable labels. This leans
entirely on the star-naming catalog already shipped and makes the tap
unambiguous. Additive on top of §4; can follow later.

## 6. Model B (deferred) — tap feeds the `:CM#` align

Alternative: SkySafari names the object and hits Align; the tap provides the
pixel *instead of* the auto-projection. Flow: a "pending tap" that the incoming
`:CM#` consumes in `_do_alignment` instead of projecting the target.

**Deferred, and probably not worth building:**

- It adds align-path wiring (pending-tap state, correlation with the `:CM#`
  arrival, a timeout) to the very path v0.11.54 just made careful (`_align_promote`).
- **No accuracy gain**: the solve's auto-projection of a SkySafari-named star is
  *sub-pixel* — strictly better than a fingertip. The tap's only value is
  choosing/confirming *which* star, which Model A already gives standalone.

Revisit only if a concrete workflow needs the tap and the SkySafari identity
bound in one gesture.

## 7. The semantic caveat (applies to all models)

A tap tells diofinder "the optical axis = this pixel." That is only *true* if the
tapped star is actually centered in the eyepiece — the tap can't verify it. So
tap-to-align **trusts the user to tap the eyepiece-centered star**. The existing
`:CM#` flow carries the identical trust (you must physically center the named
star), so this is not worse — but the UX copy should say "tap the star you have
centered in the eyepiece," not just "tap a star." If the user taps an arbitrary
bright star, they are aligning *SkySafari-to-the-finder-view*, not to the
eyepiece — a legitimate but different operation worth not conflating.

## 8. Config keys / maint / webui touched

| Layer | Change |
|---|---|
| comms maint | reuse `boresight_set`; optional new `boresight_set_snap` (snap to nearest centroid) |
| solver | optional: publish in-frame centroid pixels (snap) and/or named-star pixel labels (Model A+) |
| webui route | new `POST /boresight/set` (thin wrapper, like `/boresight/center`) |
| webui template | live-view "tap to set center" mode + confirm; screen→frame mapper JS (unit-tested) |
| config | none new (boresight_x/y already exist) |

## 9. Effort

- **Model A MVP** (raw tap → `boresight_set`, no snap): **small** — a webui route
  + the coordinate mapper + a mode toggle. A few hours; webui-only; low risk.
- **Model A with snap**: **small-to-moderate** — add the server-side snap maint
  command + a centroid feed. Realistically a focused day.
- **Model A+ named targets**: **moderate** — the catalog→pixel projection feed +
  label rendering.
- **Model B**: **moderate** and deferred (see §6).

## 10. Open questions

1. Snap tolerance and behavior when no centroid is within it (fall back to the
   raw tap, or refuse?).
2. Should the tap require a *current successful solve* (so the frame astrometry
   is valid) before arming? Almost certainly yes — gate the mode on
   `solution.solved` and a fresh `pointing_age_s`.
3. Interaction with the rotate-180 and `sub=1` (flat-field) view states — map
   from whatever is currently displayed.
4. Persist vs live-only: `boresight_set` already persists via `save_keys`; keep
   that (an align should survive a restart), matching `:CM#`.
