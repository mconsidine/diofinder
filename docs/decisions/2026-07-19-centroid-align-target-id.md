# Centroid-based align-target identification — considered, rejected

**Date:** 2026-07-19
**Status:** Rejected. The `:CM#` align keeps its current coordinate-projection
behaviour unchanged.
**Repo:** mconsidine/diofinder (`olive`)

## The idea

When SkySafari taps **Align**, it sends the target object's RA/Dec (JNow) then
`:CM#`. Today diofinder plate-solves the frame, projects that RA/Dec through the
solved WCS to a **pixel**, and makes that pixel the boresight — it does *not*
require a star to be detected there.

The proposal: instead of using the raw projection, use the target RA/Dec to
**identify the specific extracted centroid** that is the target, and either snap
the boresight to that centroid or reject the align if no centroid is found —
independent of the target's brightness or where it sits in the field.

## How it would work

After a solve the WCS maps every pixel ↔ RA/Dec, so identifying the target
centroid is pure geometry (hence brightness- and position-independent):

1. **Project → nearest centroid.** The align already computes the projected
   pixel (`x_target`/`y_target`); find the extracted centroid nearest it within
   a tolerance. Small addition — centroids and projection are both already in
   hand.
2. **Use the solver's catalog matches.** olive-solve matches centroids to
   catalog stars during the solve; pick the matched centroid whose catalog
   RA/Dec is closest to the target. Needs `return_matches=True` (currently off
   in the align solve).

## Advantages

- **Sub-pixel accuracy** when the target *is* a detected star: the measured
  centroid is where the photons landed; the WCS projection carries the solve's
  residual (~sub-pixel to ~1 px on this ~50″/px finder). Marginal but real.
- **A validation signal**: "the target RA/Dec has no centroid near it" *can*
  mean the pointing is off or the wrong object was selected.

## Disadvantages — the killer

Making a detected centroid a **requirement** breaks aligning on anything that
isn't a bright, catalogued point source — which is a large, routine fraction of
what a user aligns on:

- **Deep-sky objects** (galaxies, nebulae, clusters): diffuse and/or below the
  detection threshold, and not in a *star* catalog. No centroid exists there.
- **Planets and the Moon**: not in the star database at all; extended/moving.
- **Faint stars** below the extraction threshold: a real target, just not
  detected as a centroid this frame.
- A deliberate "align on this empty patch" is impossible.

So the "validation gate" that looked like the main benefit is actively wrong as
a hard rule — you would hit it the first clear night you centered M31. Even the
*optional* snap has a failure mode: aligning on a DSO with a faint field star
nearby, a loose snap grabs the wrong point; a tight tolerance limits the payoff
to almost nothing.

## Why the current design is correct

The align projects the target RA/Dec to a pixel and does **not** care whether a
star is there *by design*: aligning is "where does this sky coordinate land in
my frame," not "which detected star is this." The plate solve comes from the
*surrounding* stars; the target itself need not be one. That is exactly what
makes DSO / planet / faint-star / Moon alignment work. The one legitimate
hard-reject already exists and is right: if the projection falls **outside the
frame** (`"target outside camera FOV"`), you genuinely aren't pointed near it —
"not in the field," not "not a detected star."

## What (little) survives

At most an **optional, tight-tolerance refinement**, never a requirement:

> If a detected centroid sits within a couple of pixels of the projected target,
> snap the boresight to it; otherwise use the projection as-is.

Given the sub-pixel-only gain on a coarse finder, the added tolerance/ambiguity
handling, and the DSO-near-a-star risk, this was judged **not worth the
complexity**. It is recorded here in case a future need (e.g. a much finer plate
scale, or a photometric-centroiding goal) changes the calculus.

## Decision

Leave the `:CM#` align exactly as-is (coordinate projection to a boresight
pixel; reject only when the target projects outside the frame). Do **not** add a
centroid requirement; do not add the optional snap without a new, concrete
reason.

## Relation to tap-to-align

This is the mirror image of `docs/tap-align-design.md` (there the user supplies
the pixel and the system knows the RA/Dec; here SkySafari supplies the RA/Dec
and the system would find the pixel). If tap-to-align is ever built, note that
its "snap to nearest centroid" step must be **optional** for the same reason
above — a boresight/align target is not always a detected star.
