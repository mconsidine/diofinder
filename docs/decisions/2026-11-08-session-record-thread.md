# 2026-10-20 → 2026-11-08 — Thread record (current version)

The general record of this chat thread, saved as a new version. Earlier
versions stay as they were:
`docs/decisions/2026-10-31-supply-chain-pinning-and-lx200-diagnostics.md`
(2026-10-31) and `docs/decisions/2026-11-04-session-record-thread.md`
(to 2026-11-04). Sections 1–5 below summarise the 11-04 version; section 6
onward is new.

The satellite work has its own handoff,
`docs/decisions/2026-11-08-satellite-tools-handoff.md` (supersedes the 11-04
one), and exchange logs in `docs/transcripts/`
(`2026-11-02-to-11-04-…` and `2026-11-04-to-11-08-satellite-thread-exchanges.md`).

**Branch:** `claude/azimuth-on-0.11.74` (experimental, unmerged; intended base
of the next primary branch). **Tags this thread:** v0.11.72–v0.11.75. **Latest
image build:** #195 (commit `6494de3`, artifact
`diofinder-sycamore-image-manual-20261102-2041`, expires 2026-11-16).
Everything after `6494de3` reaches a device by OTA:
`sudo /usr/local/bin/diofinder-update --ref claude/azimuth-on-0.11.74`.
Last full test run: 711 passed, 1 skipped.

---

## 1. Alt/Az (2026-10-20)
v0.11.72: altitude/azimuth on LX200 `:GA`/`:GZ` and an `altaz` maint command,
gated on a set site and a plausible clock.

## 2. 2026-10-31: SkySafari `:SC`, supply-chain pinning, v0.11.73/74
`:SC` "error -13" is SkySafari-side. v0.11.73 pinned the image build to
known-good inputs; v0.11.74 consumes hardened fork-published wheels.

## 3. Azimuth branch → v0.11.75 (2026-10-31 → 11-01)
Headroom monitor, performance (one-RPC Manager snapshots and more), boot
network choice, LX200 West-positive longitude, Reboot/Shut down buttons,
SynScan settle gate, "MAX3232, not MAX232".

## 4. v0.11.76 base (2026-11-02)
`docs/next-release-todo.md` worked through (polling, IMU 10 Hz with a conf
migration, debug bundles to file, clock set from the phone, FOV long-run
recentre, opt-in report smoother, menus trimmed, "Tune from burst" removed).
Image builds #192–#195.

## 5. Satellites (2026-11-02 → 11-04)
Live capture on the device; the off-device identification, photometry,
reporting, orbit-fit, prediction and ISS-check toolchain.

---

## 6. 2026-11-04 → 11-08

### 6.1 Satellite tools
- Local-time input, `--out-tz` output; sky-region filter; CCSDS OEM write and
  read; SGP4 sanity checks past a decay and `--max-age-days`; `--norad` with
  Alpha-5 numbers; PreviSat `.gp` files.
- `docs/satellite-user-guide.md`: a user guide limited to satellite work, with
  complete workflows and every script option.

### 6.2 Device: site, defaults, clock
- **Site card** on Utilities (latitude, longitude, elevation; letters
  accepted). Elevation could not be set before: LX200 has no elevation command
  and the Config page is view-only.
- **Shipped-default report:** "absent" keys now say which built-in default
  runs, and are listed only when it differs from the shipped value. Two
  mismatched built-in defaults were fixed (`camera_tuning_file`,
  `star_db_deep`); a test pins built-in = shipped except `conf_version` and
  `fov_calibrated`.
- **Clock:** the phone set now lands within a few hundredths of a second
  (`diofinder-set-time --offset`, one process); the card measures NTP-style
  (± shown). Under network time the card shows the phone's error and refuses
  to set (409). The user's phone runs ~1 s fast; the finder at home is on
  network time.
- SkySafari showing RA 0 / Dec 0 (90° E, altitude 0 at that hour) before the
  first solve is the placeholder, not a regression.

### 6.3 Mount tracking assessment (2026-11-08, nothing built)
The question: paste a TLE or upload a schedule and have the finder drive the
mount through passes. Five documents in `docs/assessments/` (common, SynScan,
LX200, OnStepX, Orion TeleTrack). Headlines: the satellite maths must be
ported to the device without astropy; the finder has no motion today (sync
only, by design); phase 0 is a probe of the real mount; the polar alignment
code (`polar.py`, `polar_run.py`) is reused for the pointing model (levelling
sweep on alt-az, the existing routine on equatorial builds). Summary in the
satellite handoff §5.

### 6.4 Commits (11-04 → 11-08)
`282ba6c`, `bbeccd9`, `31e1b20`, `0f0ddf1`, `2cdea76`, `a1712bc`, `c016901`,
`636bfbf`, `ae9f67c`, `e3e5dc7`, `dc7aa9b`, `f8c5aa5`, plus the commit adding
this record and the new handoff and exchange log.

---

## 7. Open items (next session)

1. User field work for satellites (see the handoff §6).
2. Mount tracking, if pursued: phase 0 probe of the Virtuoso, then phase 1
   (device engine + Satellites page, planning only).
3. On-sky A/Bs still pending: smooth crosshair, bin-at-submit, ROI tracking;
   validate or remove IMU Units B/C.
4. Confirm on SeeSat-L: the unknown-object designator; R.D.E. piece numbering.
5. Decide when to cut v0.11.76 from this branch (build #195 plus the OTA-only
   changes since: tone curve, Site card, clock rework, default alignment) and
   whether to make it the primary branch. A new image build would carry the
   device changes without OTA.
6. Deferred: "promote tested image" release mode; more mount dialects; the
   residual supply-chain items (see the 2026-10-31 record).
