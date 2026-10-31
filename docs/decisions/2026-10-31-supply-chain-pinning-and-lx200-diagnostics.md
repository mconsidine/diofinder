# 2026-10-31 — Supply-chain pinning (v0.11.73) + LX200/Alt-Az diagnostics

Session record. Branch: `claude/supply-chain-pin-0.11.73` (experimental, off
`olive` = v0.11.70). Release built this session: **v0.11.73**.

## Summary

Three threads:
1. Diagnosed a SkySafari `:SC` / "error -13" / 30-second-connect report as a
   SkySafari-side behavior, not a diofinder bug (no code change).
2. Clarified why plate-solve RA/Dec is observer-independent while a *GoTo* to
   RA/Dec — and diofinder's own `:GZ`/`:GA` azimuth — require site + clock.
3. Assessed the build's upstream-version risk and **froze the image build to
   the known-good v0.11.70 versions**, shipped as v0.11.73.

---

## 1. SkySafari `:SC` / error -13 / 30 s connect (diagnosis only)

**Report:** after `:SC`, SkySafari logs a `1` (no error) then a second
"Received ASCII" with error `-13` (a "null character"); separately, connecting
can take up to 30 s though diofinder shows no >5 s setup.

**Findings:**
- diofinder's `:SC` reply (`comms_proc.py`) is spec-correct Meade:
  `1Updating Planetary Data#` + a **blank** `#`-terminated string. That blank
  second string is what SkySafari logs as the `-13`/"null" line — benign, and
  **not** a real NUL byte (diofinder emits pure printable ASCII).
- The 30 s is SkySafari-side: its LX200 **Classic** driver waits for the blank
  trailer with a long timeout because a real Meade mount pauses to compute
  planetary data; SkySafari's own developer attributed the pause to the scope
  in a support thread. diofinder returns the whole reply instantly and its
  `:SC` handler spawns a non-blocking `_sync_clock` thread, so diofinder is not
  the component stalling.

**Action:** none in diofinder. Levers are on the SkySafari side (try a
different Scope Type; confirm instancy with raw `nc diofinder.local 4060`).

## 2. RA/Dec vs Alt/Az (conceptual, no code change)

- **RA/Dec is a label on the celestial sphere** — a plate solve reads it
  directly from the star pattern, independent of observer location and (beyond
  a slow precession term needing only the rough year) time.
- **"Go to RA/Dec" needs where + when**: the target is sky-fixed but a mount's
  axes are ground-fixed, so translating the target into axis angles *now*
  requires site + sidereal time. Even a polar-aligned equatorial mount needs
  time+longitude for the RA/hour-angle axis.
- diofinder only **reports/syncs**, never slews, so that requirement lands on
  the mount — and `:GZ`/`:GA` (added v0.11.72) are the first diofinder outputs
  that depend on site+clock, which is why they are gated on `site_is_set` +
  `clock_is_plausible` and otherwise return the neutral stub.

## 3. Supply-chain pinning → v0.11.73

### Problem

The build tracked "latest" of the forks and the newest Pi OS at build time,
and the OS/apt layer (numpy ABI especially) was entirely unpinned — so a
rebuild could silently differ from a known-good image with no deliberate change.

### What v0.11.70 (the known-good baseline) was built from

v0.11.70 built 2026-10-04 off `olive`. Recovered values (all confirmed against
their sources this session):

| Layer | Pinned value |
|-------|--------------|
| olive-solve (`tetra3` wheel) | v0.1.9 |
| sycamore-extract (`star_detect` wheel) | v0.14.1 |
| astro_databases (DB/names/Messier) | 2026.07.01 |
| Base OS image | raspios_lite_arm64 **2026-09-15** (Trixie) |
| Base image SHA-256 | `cdf4f3bfac35ae947b46e4e767f935453810549779ac3290e05a6754aee627e5` |
| Debian apt snapshot | `20260915T000000Z` (main + security) |

The 2026-09-15 snapshot is what `_latest` resolved to on the 2026-10-04 build
date; the next Pi OS refresh (2026-10-06) landed two days later — the exact
drift this pins against.

### Changes (branch `claude/supply-chain-pin-0.11.73`)

| Commit | Change |
|--------|--------|
| `d32eb1c` | `config.py` → 0.11.73; base-image dated-URL pin + SHA scaffolding; `release.yml` fork/DB tag **defaults** → v0.1.9 / v0.14.1 / 2026.07.01 (override precedence preserved: input > repo var > default); `esa/tetra3` `TETRA3_REF` hook; `requirements.txt` pins (`smbus2~=0.4.3`, `pyserial==3.5`); `install.sh` installs `requirements.txt` at image build (fixes a fresh-image gap where the IMU's `smbus2` could be missing — previously OTA-only) + writes `/var/lib/diofinder/build-manifest.json` |
| `cf3e932` | **Tier 1** Debian snapshot pin (`snapshot.debian.org`, build-only, live sources restored after install so on-device updates still work; `DEBIAN_SNAPSHOT` var, `off` disables) + **Tier 2** `apt-mark hold` on numpy + the Raspberry Pi camera stack (no RPi snapshot archive exists) |
| `474bc1a` | Enforce the confirmed base-image SHA-256 by default (hard-fail on mismatch; `=skip` downgrades to record-and-warn). **Image was built from here.** |
| `9a83bef` | `docs/supply-chain-pinning.md` "how to intentionally move a pin" runbook (docs-only, not in the image) |

### Split rationale (the load-bearing fact)

apt packages come from two archives: **Debian** (numpy/scipy/pillow/flask) has
`snapshot.debian.org` → pinnable; the **Raspberry Pi archive**
(picamera2/libcamera/rpicam-apps) has **no public snapshot** → frozen only by
the pinned base image + `apt-mark hold`. The highest-risk item, the **numpy
ABI** the fork wheels link against, is a Debian package and is therefore fully
pinned by Tier 1.

### Verification

- Pins confirmed against live servers: base-image URL + SHA (user), both Debian
  snapshot URLs (user), fork/DB tags exist and were what v0.11.70 used.
- `sed` snapshot-rewrite tested against mock Pi OS sources (rewrites Debian,
  leaves `archive.raspberrypi.com` untouched, handles `debian-security` first).
- Build `run 41197510473` = success; every novel gate passed (base-image SHA
  enforcement, Debian snapshot `apt-get update`, chroot wheel import check).
- Release **v0.11.73**, asset `diofinder-sycamore-20261031-v0.11.73.img.xz`
  (~931 MB), asset digest
  `sha256:7dd6d52fc40aa6b67f4cf47bfa4da4ac30f07bd2c7cc2f6873575a3bcc0b0623`.

### Build manifest

Every image now carries `/var/lib/diofinder/build-manifest.json`: base image
URL+SHA, Debian snapshot, both fork wheels + their SHA-256, star-DB SHA, Python
version, and the ABI-critical apt package versions — so residual drift is
visible even where it is not prevented.

## Residual hardening (ranked) and sequencing

Full reference + per-component update recipes: `docs/supply-chain-pinning.md`.

1. **Tier 3 — fork-wheel build-time numpy ABI pin** (sycamore-extract +
   olive-solve CI). The image's numpy is now pinned; a wheel only imports
   cleanly if it was *built* against an ABI-compatible numpy. Deferred to after
   v0.11.73 by design (the shipped wheels are already the proven v0.11.70 set).
2. **Mirror the base image** — Raspberry Pi prunes old dated snapshots, so the
   pinned 2026-09-15 URL will eventually 404 and break rebuilds. Archive the
   `.img.xz` as a release asset and let `BASE_IMAGE_URL` fall back to it.
3. **Hard-verify the fork wheels** — today matched by filename glob and the
   manifest records the SHA but can't check it. Publish a checksums asset per
   fork release and verify in `release.yml`/`install.sh` (mirror the DB's
   `manifest.json` SHA check).
4. RPi-archive `.deb` vendoring (escalation if camera-API drift appears).
5. Pin GitHub Actions to commit SHAs.
6. CI manifest-diff gate (flag unexpected movers build-to-build).
7. Pin pip/setuptools/wheel.
8. Pin `esa/tetra3` (`TETRA3_REF`) if regenerating a custom-FOV DB.

### Important: v0.11.73 cannot be re-cut under the same tag

The `v0.11.73` tag now exists, and tag force-push/delete is blocked by the
GitHub proxy (the same reason v0.11.69 → v0.11.70 happened). Any further
build-affecting change ships as a **new version** (0.11.74+), not a v0.11.73
rebuild. The published v0.11.73 image is a complete, known-good, pinned
artifact; the residual items above improve *future* builds, not this one.

## Status

v0.11.73 is built, published, and flashable. The branch remains **experimental
and unmerged** — rollup into `olive` (optionally folding in the 0.11.71
deadband and 0.11.72 azimuth experiments) is a later, explicit decision.
