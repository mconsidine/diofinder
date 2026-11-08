# Satellite tracking: OnStepX controllers — assessment (2026-11-08)

**Status: assessment only, nothing built.** Read
[`2026-11-08-satellite-tracking-common.md`](2026-11-08-satellite-tracking-common.md)
first. OnStepX is Howard Dutton's open-source stepper/servo telescope controller.
It speaks an extended LX200 set, so most of
[`2026-11-08-satellite-tracking-lx200.md`](2026-11-08-satellite-tracking-lx200.md)
applies. This document covers what is different.

## 1. Bottom line

- **The best-suited of the four mount families**, for three reasons:
  - it's LX200-native (the sync link was already designed around it in
    `docs/onstep-design.md`);
  - stepper mounts can slew fast;
  - **the firmware is open source**, so the hard real-time part could, in
    principle, run in the controller rather than over a serial link.
- **Two routes:**
  - drive OnStepX from the finder with standard commands (like LX200, but
    probably with better rates);
  - or add a feature to OnStepX (a fork or an upstream contribution) that
    follows a list of time-stamped positions sent by the finder.

## 2. What is different from plain LX200

| Topic | OnStepX | Consequence |
|---|---|---|
| Protocol | LX200 superset with extended `:GX`/`:SX` families and alignment commands | `LX200Link` covers the basics; extended commands add status and limits |
| Mount types | German equatorial, fork, alt-az (configured in firmware) | The planner must know which; each has its own keyhole (below) |
| Speed | Stepper-driven, configurable maximum slew, often several °/s | Ample for satellites away from the keyhole (verify on the actual build) |
| Rates | Guide/move rates selectable; custom rates not yet verified | Rate resolution must be measured (§5) |
| Limits | The controller has its own horizon/overhead and meridian limits | A second layer of safety under the finder's own (common §3.6) |
| Transport | Serial, and also a network LX200 port via its web server add-on | Could use Wi-Fi/TCP instead of the GPIO serial link |
| Firmware | Open source | A controller-side ephemeris-following mode is possible (§4) |

## 3. Keyholes by mount type

- **Alt-az:** zenith keyhole, as with the Virtuoso.
- **German equatorial:**
  - a pass that crosses the meridian may need a **meridian flip mid-pass**,
    which can't be done while tracking;
  - the planner must check pier side against the pass and the controller's
    meridian limits, and either split the pass (flip in a gap) or flag it;
  - there's also a keyhole near the celestial pole.
- **Fork (polar):** no flip; keyhole near the pole.

## 4. Control options

1. **Finder-side loop with standard commands.** As in the LX200 document:
   - GoTo ahead of the satellite;
   - then rate commands updated 5–10 Hz from the finder;
   - feedback from position reads.
   Feasibility depends on the rate commands and their resolution (§5).
2. **Leapfrog GoTos** (common §3.3) as a fallback; fast stepper GoTos make this
   better than on slow mounts.
3. **Controller-side ephemeris following (firmware change).**
   - The finder sends a short table of `(time, axis1, axis2)` points (e.g. one
     per second, computed from SGP4 and the pointing model).
   - OnStepX interpolates between them in its own real-time loop.
   - Pros: smoothest, no serial jitter, the controller's own limits stay in
     charge.
   - Cons: it's firmware development in another project. It needs coordination
     with the OnStep community (upstream) or maintaining a fork, and testing on
     the controller hardware.

**Recommendation:** start with option 1 (it shares all its code with the LX200
link). Consider option 3 only if option 1's smoothness falls short of the
accuracy goal (common §3.5) and someone is prepared to work on the firmware.

## 5. Phase 0 probe (OnStepX)

- Firmware version and mount type (extended `:GX` queries).
- Which rate commands exist; achieved rate vs commanded; smallest step.
- Maximum slew and acceleration as configured on this build.
- Configured horizon/overhead/meridian limits; how the controller reports
  hitting one.
- Read latency over serial vs over the network port.

Check against the OnStepX command reference on the OnStep wiki/groups.io (not
reachable from the build sandbox when this was written).

## 6. Pointing model (OnStepX)

- **German equatorial / fork on a wedge:** the existing polar routine
  (`diofinder/polar.py`) is exactly the right tool. With motion available the
  finder can rotate the RA axis itself, so polar alignment becomes automatic. The
  result is shown as "turn the azimuth knob x′, the altitude knob y′", as the
  routine already does.
- **Alt-az build:** the levelling sweep (common §5.2).
- **The controller's own model:** OnStepX has its own multi-star alignment
  model; the finder's solves can feed it via sync/alignment commands. Decide
  whether OnStepX's model or the finder's is authoritative during tracking, to
  avoid applying corrections twice.

## 7. Phases (OnStepX)

| Phase | OnStepX work | Size |
|---|---|---|
| 0 | Probe (§5) | Small |
| 1 | Common: device engine + Satellites page | Moderate |
| 2 | `LX200Link` + OnStepX extensions (status, limits), simulator, safety | Moderate |
| 3 | Pointing model; automatic polar alignment on equatorial builds | Moderate |
| 4 | Tracking loop; meridian-flip planning on German equatorial mounts | Large |
| 5 (optional) | Controller-side ephemeris following (firmware) | Large; external project |

## 8. Sources

- `docs/onstep-design.md` (sync design, `LX200Link`, OnStepX as the v1 target).
- [LX200-compatible command list](https://soundstepper.sourceforge.net/LX200_Compatible_Commands.html)
  (base command family).
- [EQMOD satellite tracking notes](https://eq-mod.sourceforge.net/faqs/sattrack.html)
  (custom-rate tracking approach on another controller family).
- The OnStepX command reference and wiki (to consult during phase 0; not
  fetched for this assessment).
