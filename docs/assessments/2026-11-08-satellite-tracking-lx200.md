# Satellite tracking: Meade LX200 / Autostar and LX200-protocol mounts — assessment (2026-11-08)

**Status: assessment only, nothing built.** Read
[`2026-11-08-satellite-tracking-common.md`](2026-11-08-satellite-tracking-common.md)
first. This document covers mounts driven with the **Meade LX200 serial
protocol**: Meade LX200 (classic, GPS, ACF), Autostar #497/#494 and Autostar II
controllers, and third-party controllers that speak LX200. OnStepX also speaks
LX200 but has its own document because it adds a great deal.

## 1. Bottom line

- **Feasible.** The command set has everything needed in principle: GoTo in
  alt/az and RA/Dec, move at a chosen rate, custom per-axis rates, stop, and
  position read-back.
- **The weak point is rate resolution.** Where custom rates exist, they are
  given in tenths of a degree per second (`:RADD.D#`). That's coarse for smooth
  satellite tracking: rates get dithered, or replaced by short GoTos.
- **Behaviour varies a lot between firmwares,** so a probe is again the first
  step.

## 2. Relationship to existing code

- **The finder is already an LX200 *server*** (`comms_proc._handle_lx200_command`,
  for SkySafari). Driving an LX200 mount means also being an LX200 *client* on
  the GPIO serial port.
- **Reusable pieces:** the coordinate formatting and parsing (`_format_ra`,
  `_format_dec`, `_format_altitude`, `_format_azimuth`, the sexagesimal
  parsers).
- **Planned but not built:** an `LX200Link` dialect (`docs/onstep-design.md`;
  `mountlink.make_link` lists `lx200` as future work).
- **No conflict:** SkySafari talks to the finder; the finder talks to the
  mount. The finder must not forward SkySafari's commands to the mount while a
  pass is being tracked.

## 3. Commands that matter (Meade documentation; verify per firmware)

| Need | Command | Notes |
|---|---|---|
| GoTo alt/az | `:Sa sDD*MM#`, `:Sz DDD*MM#`, then `:MA#` | Only in LAND/ALTAZ modes. Format is strict: `:Sz` replies `0` on a bad format, but `:Sa` always replies `1`, so a bad altitude fails silently. **`:MA#` without a loaded target can drive the scope into its stops** (reported on Autostar) |
| GoTo RA/Dec | `:Sr`, `:Sd`, then `:MS#` | Works in any mode; the satellite position is converted to RA/Dec of date first |
| Read position | `:GA#`/`:GZ#` (alt/az), `:GR#`/`:GD#` (RA/Dec) | Resolution depends on precision mode (`:U#` toggles high precision) |
| Move at a preset rate | `:RG#`/`:RC#`/`:RM#`/`:RS#` (guide/centre/find/slew) then `:Mn#`/`:Ms#`/`:Me#`/`:Mw#` | Moves along the mount's axes, not RA/Dec |
| Custom per-axis rate | `:RADD.D#` (RA/azimuth), `:REDD.D#` (Dec/elevation), degrees per second | Autostar II / LX200GPS era. **0.1°/s resolution.** No reply |
| Maximum slew rate | `:SwN#` | N = 2–8 °/s in one manual, 2–4 in another: firmware-dependent |
| Stop | `:Q#` (all), `:Qn#` etc. (one direction) | |

## 4. Control options

1. **Rate control with `:RA`/`:RE` + move commands.**
   - Set each axis's rate, start motion in the right direction, and update
     several times a second.
   - With 0.1°/s steps, a satellite at 0.47°/s needs alternating 0.4 and
     0.5°/s. That gives a wobble of a few hundredths of a degree at 5 Hz: fine
     for the finder camera, marginal for a high-power eyepiece.
   - Only on firmware that has `:RA`/`:RE`.
2. **Leapfrog GoTos** (common §3.3). Works on any LX200 GoTo mount, including
   classic LX200s without custom rates. Intermittent view.
3. **The handset's own satellite tracking.** Autostar handsets are reported to
   track satellites from uploaded element sets (loaded with Meade's Autostar
   Update software). If that upload path could be reproduced over serial, the
   handset would do the real-time work and the finder would only plan, upload
   and start. **Unverified:** whether this exists on the target handset,
   whether the upload protocol is documented, and how well it tracks.

**Recommendation:** probe first (§6). Use option 1 where `:RA`/`:RE` exist,
option 2 otherwise. Investigate option 3 only if a specific Autostar handset is
the target.

## 5. Mount specifics that matter

- **Alt-az vs polar.** LX200 forks can run alt-az or on a wedge (polar):
  - alt-az: zenith keyhole;
  - polar: keyhole near the pole; a fork has no meridian flip.
  The planner needs to know which (`:GW#` or the alignment-mode query, where
  supported).
- **Slew speed:** 2–8 °/s maximum depending on model, enough for satellites
  away from the keyhole.
- **High-precision mode** must be on (`:U#`), or position reads are only to the
  arc-minute.
- **Firmware quirks** are many: formats, silent failures (`:Sa`), modes. The
  link must read back after every GoTo, and treat unexpected replies as a stop
  condition.

## 6. Phase 0 probe (LX200)

- Product and firmware: `:GVP#`, `:GVN#`, `:GVD#`.
- Alignment mode: alt-az, polar or land.
- Whether `:RA`/`:RE` are accepted, and what rate actually results (measure
  position change over time).
- `:Sw` range; acceleration.
- `:GA`/`:GZ` read latency and resolution in high-precision mode.
- `:MA#` behaviour with a deliberately safe, loaded target.

## 7. Pointing model (LX200)

- **Alt-az mode:** the levelling sweep (common §5.2, adapted from
  `diofinder/polar.py`) applies unchanged.
- **Polar mode (wedge):** the existing polar routine applies as it is (RA-axis
  sweep vs the pole). With motion available the finder can run the RA sweep
  itself.
- **The mount's own model:** the Autostar alignment can be refreshed with the
  finder's solves via `:CM#` (sync), as planned in `docs/onstep-design.md`.

## 8. Phases (LX200)

| Phase | LX200 work | Size |
|---|---|---|
| 0 | Probe (§6) | Small |
| 1 | Common: device engine + Satellites page | Moderate |
| 2 | `LX200Link` (sync, GoTo, rates, stop, position), simulator, safety | Moderate |
| 3 | Pointing model | Moderate |
| 4 | Tracking loop (rate dithering or leapfrog) | Large |

## 9. Sources

- [LX200-compatible command list](https://soundstepper.sourceforge.net/LX200_Compatible_Commands.html):
  `:RA`/`:RE` in degrees per second, `:Sw`, `:Sa`/`:Sz`/`:MA`.
- [The Autostar/LX200 command set and differences](https://www.astro.louisville.edu/software/xmtel/archive/xmtel-indi-6.0/xmtel-6.0l/support/lx200/CommandSet.html).
- [Weasner: Autostar/LX200 notes](https://www.weasner.com/etx/autostar/as_lx200.html):
  format pitfalls, `:Sa` always replying `1`.
- [Weasner: Autostar commands](https://www.weasner.com/etx/autostar/as_commands.html).
- [Meade Autostar serial protocol (2007)](https://www.weasner.com/etx/autostar/2010/AutostarSerialProtocol2007oct.pdf).
- [Meade LX200 manual (computer control section)](https://www.manualslib.com/manual/295083/Meade-Lx200.html?page=55).
- Existing code: `diofinder/comms_proc.py` (LX200 server, formatting),
  `docs/onstep-design.md` (`LX200Link` plan).
