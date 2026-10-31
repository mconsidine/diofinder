# Supply-chain pinning (v0.11.73 / v0.11.74)

This freezes the diofinder image build to exact upstream versions, so a rebuild
is reproducible and nothing advances unless a pin is changed on purpose.

**v0.11.74 (experimental) extends v0.11.73** by closing the remaining gaps —
it is the first build where every tier is pinned:

* **Fork wheels are now reproducible AND integrity-checked.** sycamore-extract
  and olive-solve build with a committed `Cargo.lock` + `--locked` (frozen
  numpy/pyo3/transitive crate versions) and publish a `<wheel>.whl.sha256`;
  `release.yml` **hard-verifies** each downloaded wheel against it. Pins advance
  to the hardened releases: **olive-solve v0.1.11**, **sycamore-extract v0.14.2**
  (each release's tag now equals its wheel version).
* **Base image is self-hosted.** The default URL points at our own mirror of
  the 2026-09-15 snapshot in `astro_databases` (release `base-image-2026-09-15`,
  same SHA-256), so rebuilds survive Raspberry Pi pruning the upstream snapshot.

The sections below describe the v0.11.73 foundation (base-image + Debian
snapshot + apt holds); the deltas above are the v0.11.74 additions. The v0.11.70
values in the first table are the historical baseline, not the current pins.

## What v0.11.70 was built from

v0.11.70 built on **2026-10-04** off the `olive` branch. Before this change the
build pulled "latest" of every fork and the newest Pi OS at build time, so
those values were whatever happened to be current that day:

| Layer | v0.11.70 value | Pin mechanism |
|-------|----------------|---------------|
| olive-solve (`tetra3` wheel) | **v0.1.9** | `release.yml` default (was `latest`) |
| sycamore-extract (`star_detect` wheel) | **v0.14.1** | `release.yml` default (was `latest`) |
| astro_databases (DB, names, Messier) | **2026.07.01** | `release.yml` default (was `latest`) |
| Base OS image | **raspios_lite_arm64 2026-09-15** (Trixie), SHA-256 enforced | `build-image.sh` dated URL (was `_latest`) |

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
* **Base-image integrity** — the SHA-256
  `cdf4f3bfac35ae947b46e4e767f935453810549779ac3290e05a6754aee627e5` (confirmed
  against the downloads server for the 2026-09-15 image) is baked in and
  **enforced by default**: the build hard-fails if the downloaded image doesn't
  match. If you move `BASE_IMAGE_URL`, also set `BASE_IMAGE_SHA256` to the new
  image's hash (they are checked together). Set `BASE_IMAGE_SHA256=skip` to
  downgrade to record-and-warn.
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

1. **Fork-wheel build-time numpy (Tier 3) — DONE in v0.11.74.** Both forks now
   commit `Cargo.lock` and build `--locked`, freezing the `numpy`/`pyo3`/
   transitive crate versions the wheel links against (the Rust `numpy` crate is
   the ABI boundary for a maturin wheel — there is no Python build-time numpy to
   pin). Combined with the image's snapshot-pinned Python numpy, numpy is pinned
   on both sides. Backstop still in place: `install.sh` runs `import star_detect`
   / `import tetra3` in the chroot, so a gross ABI mismatch fails the build.
   *(The "Tier 3 update plan" section below is retained as the procedure for the
   next numpy-major move.)*
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

---

# How to intentionally move a pin

Every pin is a **default in code** plus an **override** (a `workflow_dispatch`
input, a repo variable, or an env var for local builds). You almost never edit
code to move a pin for a one-off build — set the override. Edit the code
default only when you want the new value to become the permanent baseline, then
cut a new diofinder release so the default is recorded in git.

**Golden rule:** change one pin, build, flash, and verify on-device *before*
changing the next. The whole point of this machinery is that a regression has
exactly one suspect. Each recipe below ends with the one check that proves it.

### Update the Pi OS base image

The base image and the Debian snapshot should move **together** (same date) so
the OS and its package set stay consistent.

1. Find the new dated snapshot at
   `https://downloads.raspberrypi.com/raspios_lite_arm64/images/` and its
   `…img.xz.sha256`.
2. One-off build: set repo vars `BASE_IMAGE_URL` (full `.img.xz` URL) and
   `BASE_IMAGE_SHA256` (the hash). Permanent: edit `_DEFAULT_BASE_IMAGE_URL` and
   `_DEFAULT_BASE_IMAGE_SHA256` in `build/build-image.sh`.
3. Also move the Debian snapshot (next section) to the same date.
4. Build. The build **hard-fails at download** if the hash doesn't match — that
   is the integrity check working, not a bug.
5. Verify: solve on-sky, and diff the new device's
   `/var/lib/diofinder/build-manifest.json` against the old one — expect numpy,
   libcamera, picamera2 to be the only intended movers.

### Update the Debian snapshot (and with it, numpy/scipy/pillow/flask)

1. Pick a timestamp `YYYYMMDDTHHMMSSZ` (midnight of the base-image date is fine;
   snapshot.debian.org resolves to the nearest run at or before it).
2. One-off: repo var `DEBIAN_SNAPSHOT`. Permanent: edit the `_DEB_SNAPSHOT`
   default in `scripts/install.sh`.
3. **Check the numpy it pins before building** — this is the ABI the fork
   wheels must tolerate:
   ```
   curl -fsSL "https://snapshot.debian.org/archive/debian/<TS>/dists/trixie/main/binary-arm64/Packages.xz" \
     | xz -d | grep -A1 '^Package: python3-numpy'
   ```
   If the **major** version changes (e.g. 2.x → 3.x), you MUST rebuild the fork
   wheels against it first (see "Update numpy across an ABI boundary").
4. Build, flash. Backstop: the chroot `import star_detect` / `import tetra3`
   check fails the build on a gross ABI mismatch.
5. Verify: `venv/bin/python -c "import numpy; print(numpy.__version__)"` on-device
   matches the snapshot, and the solver runs.
6. Escape hatch: `DEBIAN_SNAPSHOT=off` reverts to live apt if the snapshot host
   is unreachable.

### Update numpy across an ABI boundary (the coupled case — Tier 3)

Only needed when the numpy **major** changes. Within a major (2.4 → 2.7) the ABI
is stable and you can just move the Debian snapshot.

1. Rebuild the fork wheels against the new numpy major: in **sycamore-extract**
   and **olive-solve** CI, pin the build-time numpy (`numpy==<major>.*` /
   `oldest-supported-numpy`), rebuild, and publish new release tags.
2. Move the Debian snapshot to a date carrying that numpy major (above).
3. Bump `SYCAMORE_TAG` / `OLIVE_SOLVE_TAG` to the rebuilt wheels (next section).
4. Build all three changes in ONE image and verify the import check passes and
   it solves. (This is the one case where several pins must move together.)

### Update sycamore-extract or olive-solve (the fork wheels)

1. Confirm the new release has an `aarch64` wheel asset
   (`star_detect-*aarch64*.whl` / `tetra3-*aarch64*.whl`).
2. One-off: `workflow_dispatch` input `sycamore_tag` / `olive_solve_tag`, or repo
   var `SYCAMORE_TAG` / `OLIVE_SOLVE_TAG`. Permanent: edit the `|| 'vX.Y.Z'`
   default in `.github/workflows/release.yml`.
3. Build. Backstop: the chroot import check fails the build if the new wheel is
   numpy-ABI-incompatible with the pinned snapshot.
4. Verify: `diofinder-ctl version` shows the new wheel versions (also in the
   startup log, Home/Update pages, and every debug bundle's
   `effective_params.json`), and it solves on-sky.

### Update the star database

1. New `astro_databases` release with `diofinder_13deg.npz` (+ optional
   `_mag85`, `star_names.csv`, `messier.csv`) and a `manifest.json` carrying
   their SHA-256.
2. One-off: input `db_tag` or repo var `DB_TAG`. Permanent: edit the
   `|| '2026.07.01'` default in `release.yml`.
3. Build — the workflow SHA-256-verifies the DB against the release manifest.
4. **Reset FOV calibration after flashing** (a new DB can shift the FOV band):
   Config page → Reset, or set `fov_calibrated: false` and restart. Verify it
   re-converges and solves.

### Update smbus2 / pyserial

Edit the pins in `requirements.txt`, then confirm on a device:
`venv/bin/python -c "import smbus2, serial; print(smbus2.__version__, serial.VERSION)"`.

### Update esa/tetra3 (custom-FOV DB generation only)

Only matters if you regenerate a DB at a non-default FOV. Set repo var
`TETRA3_REF` (or env `DIOFINDER_TETRA3_REF`) to a commit/tag; empty = HEAD.

### After any permanent default change

Bump `diofinder/config.py` `version`, update this file's tables, commit, and cut
a new diofinder release so the new baseline is what a fresh `workflow_dispatch`
(with no overrides) reproduces.
