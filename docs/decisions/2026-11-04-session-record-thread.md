# 2026-10-20 → 2026-11-04 — Thread record (current version)

The general record of this chat thread, saved as a new version alongside the earlier one: the
record `docs/decisions/2026-10-31-supply-chain-pinning-and-lx200-diagnostics.md`
(written on 2026-10-31, covering that day) stays as it was. This version covers
the whole thread to 2026-11-04. The satellite work has its own handoff
(`docs/decisions/2026-11-04-satellite-tools-handoff.md`) and exchange log
(`docs/transcripts/2026-11-02-to-11-04-satellite-thread-exchanges.md`).

**Branch now:** `claude/azimuth-on-0.11.74` (experimental, unmerged; the
intended base of the next primary branch). Head at this record: `6042e7c`.
**Tags this thread:** v0.11.72 (Alt/Az), v0.11.73, v0.11.74, v0.11.75.
**Latest image build:** #195 (commit `6494de3`, artifact
`diofinder-sycamore-image-manual-20261102-2041`, ~983 MB, expires 2026-11-16).
Commits after `6494de3` are off-device scripts plus one device change
(`live_capture.tone_info`), reachable by OTA:
`sudo /usr/local/bin/diofinder-update --ref claude/azimuth-on-0.11.74`.

---

## 1. Alt/Az and RA/Dec (2026-10-20)

- v0.11.72 (`5a36431`): real altitude/azimuth computed and served on LX200
  `:GA`/`:GZ` plus an `altaz` maint command — the first outputs that need
  site + clock, so they are gated on a set site and a plausible clock.
- Explained: a plate solve's RA/Dec is observer-independent; "go to RA/Dec"
  needs where + when because mount axes are ground-fixed.

## 2. 2026-10-31: SkySafari `:SC`, supply-chain pinning, v0.11.73 / v0.11.74

Details in the 2026-10-31 record. In brief:
- SkySafari's `:SC` "error -13" and 30 s connects are SkySafari-side (the
  blank `#` trailer is spec-correct Meade).
- **v0.11.73**: image build pinned to the known-good v0.11.70 inputs
  (olive-solve v0.1.9, sycamore v0.14.1, astro_databases 2026.07.01, Pi OS
  2026-09-15 with SHA-256 enforced, Debian snapshot 20260915T000000Z, apt
  holds on numpy + camera stack, build manifest in every image).
- **v0.11.74** (`b607447`): consumes hardened fork-published wheels (verified
  SHA), self-hosted base image; the fork releases are treated as "latest", and
  diofinder keeps only experimental/unmerged branches (user's choice).
- Stale releases/tags were cleaned up by the user.

## 3. Azimuth branch → v0.11.75 (2026-10-31 → 11-01)

- Azimuth overlay merged onto 0.11.74 (`ccea898`) with deterministic
  test-mode Alt/Az; a solving star-field `test.png` ships in images.
- `frames.json` `saved_at` written in UTC.
- olive-solve upstream revisit: solve latency is Amdahl-dominated (detect/solve
  cycles already ~20–30 ms), so the port was parked.
- **Headroom monitor** (Utilities page, `diofinder/sysmon.py`): phase 1 verdicts,
  boot warm-up excluded, swap judged on growth; phase 2 per-process
  RSS/CPU/affinity; zram-aware swap verdict; readiness-gated warm-up and a
  sustained low-RAM rule.
- Performance: Manager snapshots (one RPC instead of N+1), sorting-network
  median in the bg cache, IMU settings TTL, focus poll 500→750 ms, scipy
  dropped from the focus score.
- Networking: user-chosen boot network (AP default), firstboot no longer
  fights it; Wi-Fi rescan fix; boot-time analysis (~60 s to discovery is
  normal).
- LX200 `:Sg`/`:Gg` use West-positive longitude (a US site was ~145° of
  sidereal time off); test epoch accepts `frames.json` timestamps.
- Reboot / Shut down buttons on the Utilities page.
- **v0.11.75** cut from `575e9bd` (2026-11-01).
- SynScan mount-sync How-To produced for the collaborator (a Claude Docs
  document); then the settle gate wired and "MAX3232, not MAX232" fixed
  everywhere (`7aa2f21`).
- SkySafari connect "storms": each poll is a new TCP connection; the readout
  rate is the lever (later surfaced as a hint on the Headroom card).

## 4. v0.11.76 base (2026-11-02): to-do list worked through

`docs/next-release-todo.md` holds the full list with ✅ marks. Done on the
branch: hidden tabs and off-screen cards don't poll; IMU 20 → 10 Hz with a
one-shot OTA conf migration (v2, per-rule versions); debug bundles built to a
file in the background, downloadable by link, `curl`-able (GET), each frame
with its own solve and matched stars; Headroom CPU 0 rule needs sustained
readings; `ap-watchdog.sh` in the OTA refresh list; `bg_runs` capped; CI test
workflow; clock: phone "set time" button, daemon sets the clock directly
(CAP_SYS_TIME), SkySafari threshold 2 s, last-known time restored at start,
only this boot's sync counts; FOV drift recentres on the long-run median;
opt-in IMU report smoother (`imu_report_smoothing`, default off, needs an
on-sky A/B); test mode uses the real site unless test values are set (never
0,0); version 0.11.76 in code; background-mode menus trimmed to four;
"Tune from burst" removed (duplicated `replay_corpus.py`); Polar alignment
stays hidden.
- Resource assessment written into the to-do doc (what costs CPU 0; what awaits
  an A/B before one side is removed: smooth crosshair, bin-at-submit, ROI
  tracking, IMU Units B/C, horizon reject). The Unit C chip-write mode was
  deferred by design (needs Unit C Mode 1 validated and Unit B on).
- Image builds #192–#195 (one transient snapshot.debian.org 503 re-run); #195
  contains every change since v0.11.75 up to `6494de3`.

## 5. Satellites (2026-11-02 → 11-04)

Summarised in the handoff; headline points:
- **Live capture** on the device (frames + sensor UTC + solve + matched
  stars), capped and time-limited, clock source and (now) tone curve recorded.
- Off-device toolchain in `tests/`: SatChecker overlay and stacks;
  identification against McCants/CelesTrak element sets with a free time
  offset and refinement; frame-by-frame tracks for hand-tracked passes;
  photometry (tone-curve inversion, star calibration, light curves, flash
  period); IOD (verified against G. Lewis' spec; position uncertainty in
  arcminutes for angle format 2) and R.D.E. reports and a SeeSat-L draft;
  TLE + OMM outputs; orbit fits (honest one-pass circular, full two-pass);
  pass prediction for one object or a whole file; ISS ground-truth check;
  element sources from files, zips, folders, URLs (cached) and comma lists.
- First real capture (2026-11-02): the trail was an aircraft (correctly
  unmatched); it exposed a flash-classification weakness, now fixed.
- 12-bit raw capture judged unnecessary (cost is storage/Wi-Fi, not CPU).

## 6. Open items (next session)

1. User field work: OTA, phone-set clock, captures (fixed-field satellite,
   ISS pass, hand-tracked pass, two passes of one object); then review
   `identification.txt` / `iss_report.txt`.
2. On-sky A/Bs: smooth crosshair, bin-at-submit, ROI tracking; validate or
   remove IMU Units B/C.
3. Confirm on SeeSat-L: unknown-object designator convention; R.D.E. piece
   numbering.
4. Decide when to cut v0.11.76 from this branch (build #195 + the OTA-only
   tone-curve change) and whether to make it the primary branch.
5. Deferred: "promote tested image" release mode; more mount dialects; OEM
   ephemeris input; Tier 3 fork-wheel numpy ABI pin and the other residual
   supply-chain items (see the 2026-10-31 record).
