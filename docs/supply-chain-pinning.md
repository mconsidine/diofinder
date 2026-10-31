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
* **apt packages (Tier 1 + Tier 2)** — split by archive:
  * **Debian archive** (numpy, scipy, pillow, flask, build-essential, deps):
    `install.sh` repoints the Debian deb822/list sources at
    `snapshot.debian.org` for the duration of the build, pinned to
    `DEBIAN_SNAPSHOT` (default `20260915T000000Z`, matching the base image),
    then **restores the live sources** so the shipped device still updates
    normally. This freezes the **numpy ABI** the fork wheels link against —
    the highest-value apt pin. Set the `DEBIAN_SNAPSHOT` repo var to move it,
    or `off` to fall back to live apt if the snapshot host is unreachable.
  * **Raspberry Pi archive** (picamera2, libcamera, rpicam-apps): has **no
    public snapshot service**, so these are frozen by the pinned base image and
    `apt-mark hold` — the build holds numpy + the camera stack so an on-device
    `apt upgrade` can't bump them out from under the code. Reversible with
    `sudo apt-mark unhold <pkg>`.

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

1. **Fork-wheel build-time numpy (Tier 3).** The Debian snapshot now freezes the
   *image's* numpy, but a wheel only imports cleanly if it was **built against
   an ABI-compatible numpy** in its own repo's CI (sycamore-extract,
   olive-solve). Pinning one side without the other is the remaining ABI
   exposure. Backstop: `install.sh` runs `import star_detect` / `import tetra3`
   in the chroot after installing the wheels, so a gross ABI mismatch **fails
   the build** rather than restart-looping in the field. See the Tier 3 update
   plan below.
2. **Raspberry Pi archive has no snapshot.** picamera2/libcamera/rpicam-apps are
   frozen only by the pinned base image + `apt-mark hold` — robust against
   on-device upgrades, but a *rebuild* still pulls them from the live RPi
   archive. Full reproducibility there needs vendored `.deb`s (download the
   known-good versions the manifest records, install from a local cache) — a
   later escalation, only if camera-API drift actually appears.
3. **`pip install --upgrade pip setuptools wheel`.** Latest build backend each
   run. Low risk, recorded as the Python version in the manifest.
4. **esa/tetra3 HEAD** on the custom-FOV path (see `TETRA3_REF` above).

## Tier 3 update plan — fork-wheel numpy (do AFTER 0.11.73)

Order: **0.11.73 first, Tier 3 after.** 0.11.73 pins to the *existing*
v0.1.9 / v0.14.1 wheels, which already shipped in the known-good v0.11.70 image
and are therefore already ABI-compatible with the ~2026-09-15 Trixie numpy the
snapshot now pins. So 0.11.73 is internally consistent and safe to build now;
Tier 3 only affects the **next** wheel you build. Doing it first would mean
cutting new wheels and re-pinning 0.11.73 to them — more churn, no safety gain.

Steps, in the fork repos (not diofinder):

1. **Record the baseline.** From a v0.11.70/0.11.73 device, read
   `/var/lib/diofinder/build-manifest.json` → `apt.python3-numpy` (the numpy
   major the working wheels tolerate). Confirm it's numpy 2.x (Trixie) so the
   target is unambiguous.
2. **Pin build-time numpy in sycamore-extract CI.** In its wheel-build job, add
   `oldest-supported-numpy` or an explicit `numpy==<major>.*` build requirement
   so the PyO3/`numpy`-crate ABI targets that major. Rebuild and bump its tag.
3. **Pin build-time numpy in olive-solve CI** the same way; bump its tag.
4. **Adopt the new wheels deliberately** in diofinder by bumping
   `OLIVE_SOLVE_TAG` / `SYCAMORE_TAG` (the pinned defaults in `release.yml`) to
   the rebuilt tags — a normal, reviewed pin move, in its own diofinder release.
5. **Keep the build-time import check** as the permanent backstop; optionally
   add an explicit `numpy.__version__` assertion to the manifest-time check so a
   major mismatch is named loudly.

Net: numpy is then pinned on *both* sides (image via snapshot, wheels via their
CI), which closes the ABI loop. Until then, the import-check backstop + the
`apt-mark hold` on numpy are what keep the shipped image safe.

## To reproduce the v0.11.70 image exactly

Leave all repo variables unset and build this branch — the pinned defaults are
the v0.11.70 values. (apt and pip versions may still differ if the Debian/PyPI
repos have moved; compare the resulting `build-manifest.json` against a
v0.11.70 device to confirm.)
