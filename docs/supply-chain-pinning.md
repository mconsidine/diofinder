# Supply-chain pinning (v0.11.73)

This release freezes the diofinder image build to the exact upstream versions
that shipped in the known-good **v0.11.70** image, so a rebuild is reproducible
and nothing advances unless a pin is changed on purpose.

## What v0.11.70 was built from

v0.11.70 built on **2026-10-04** off the `olive` branch. Before this change the
build pulled "latest" of every fork and the newest Pi OS at build time, so
those values were whatever happened to be current that day:

| Layer | v0.11.70 value | Pin mechanism |
|-------|----------------|---------------|
| olive-solve (`tetra3` wheel) | **v0.1.9** | `release.yml` default (was `latest`) |
| sycamore-extract (`star_detect` wheel) | **v0.14.1** | `release.yml` default (was `latest`) |
| astro_databases (DB, names, Messier) | **2026.07.01** | `release.yml` default (was `latest`) |
| Base OS image | **raspios_lite_arm64 2026-09-15** (Trixie) | `build-image.sh` dated URL (was `_latest`) |

The 2026-09-15 snapshot is the one `_latest` resolved to on the build date; the
next Pi OS refresh (2026-10-06, Debian 13 + Linux 6.18 update) landed two days
later and would be pulled by any rebuild on `_latest` — the exact drift this
pin prevents.

## How the pins work (override precedence preserved)

Every pin is a **default**, not a lock-out. Precedence is unchanged:

```
dispatch input  >  repo variable  >  pinned default
```

So bumping a fork is still a one-line, deliberate act — set the repo variable
or the `workflow_dispatch` input — but doing nothing keeps the frozen version
instead of silently taking latest.

* **Fork / DB tags** — `OLIVE_SOLVE_TAG`, `SYCAMORE_TAG`, `DB_TAG` repo vars (or
  the dispatch inputs). Unset → the v0.11.70 pins above.
* **Base image** — `BASE_IMAGE_URL` repo var → `DIOFINDER_BASE_IMAGE_URL` in the
  build. Unset → the pinned 2026-09-15 URL in `build-image.sh`.
* **Base-image integrity** — `BASE_IMAGE_SHA256` repo var →
  `DIOFINDER_BASE_IMAGE_SHA256`. **Record-then-enforce**: the downloads server
  publishes no stable hash to bake in, so leave it empty for the first build —
  the log and the build manifest print the measured SHA-256. Capture that value,
  set the repo var, and every later build hard-fails on a mismatch.
* **esa/tetra3** (custom-FOV DB generation only; the default build never imports
  it) — `TETRA3_REF` repo var / `DIOFINDER_TETRA3_REF` env. Empty → HEAD. This
  is the one upstream with no published release to pin to; set a commit/tag only
  when you regenerate a database.
* **smbus2 / pyserial** — pinned in `requirements.txt` (`smbus2~=0.4.3`,
  `pyserial==3.5`) and now installed at **image build** too, not only on OTA
  (closes a fresh-image gap where the IMU's `smbus2` could be missing).

## Build manifest

Every image now carries `/var/lib/diofinder/build-manifest.json` recording the
resolved base-image URL+SHA, both fork wheels + their SHA-256, the star DB SHA,
the Python version, and the versions of the ABI-critical **apt** packages
(numpy, scipy, Pillow, picamera2, libcamera, rpicam-apps, Flask, NetworkManager).

The manifest exists because some layers are still not hard-pinned (see below) —
diffing two images' manifests makes any drift *visible* even where it isn't
*prevented*.

## Still not pinned (known residual risk)

These are deliberately left for a later pass; the manifest records them so a
regression traced to one is obvious:

1. **apt package versions.** `apt-get update && install` on a rolling distro
   serves whatever Debian/RPi publish at build time, even on a pinned base
   image. The sharp edge is **numpy ABI vs the fork wheels** — if Pi OS advances
   numpy across an ABI boundary, a wheel built against the old ABI fails to
   import and the solver restart-loops. Hard pinning needs `apt-mark hold` /
   snapshot.debian.org and matching the wheels' build-time numpy.
2. **`pip install --upgrade pip setuptools wheel`.** Latest build backend each
   run. Low risk, recorded as the Python version in the manifest.
3. **esa/tetra3 HEAD** on the custom-FOV path (see `TETRA3_REF` above).

## To reproduce the v0.11.70 image exactly

Leave all repo variables unset and build this branch — the pinned defaults are
the v0.11.70 values. (apt and pip versions may still differ if the Debian/PyPI
repos have moved; compare the resulting `build-manifest.json` against a
v0.11.70 device to confirm.)
