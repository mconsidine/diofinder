#!/bin/bash
# eFinder SD card image builder — olive branch.
#
# Strategy:
#   1. Download the official Raspberry Pi OS Trixie Lite image (or use
#      a cached copy in build/output/base.img.xz).
#   2. Grow it by ~2 GB so we have room for our packages.
#   3. Loop-mount, resize the root partition, fsck.
#   4. Bind-mount /dev /proc /sys, copy qemu-aarch64-static into the rootfs.
#   5. Stage our source tree under /tmp/efinder-src in the chroot.
#   6. Run install.sh in chroot mode.
#   7. Unmount, sync, hand off to caller for compression.
#
# Run from the repo root as: sudo bash build/build-image.sh
#
# Environment:
#   EFINDER_VERSION        Tag string for logging (default "main").
#   REPO                   owner/repo (default "mconsidine/eFinder_cli").
#   EFINDER_SOLVER_DB      Path to a pre-generated tetra3 .npz database.
#                          Created by release.yml on the CI runner before
#                          this script is called. Staged into the chroot
#                          so install.sh can copy it to /var/lib/efinder/.
#   EFINDER_BUILD_DRY_RUN  If "1", skip the actual chroot install
#                          (useful for testing the loop-mount/resize
#                          parts on your Dell without spending an
#                          hour on apt-get).

set -euo pipefail

REPO="${REPO:-mconsidine/eFinder_cli}"
EFINDER_VERSION="${EFINDER_VERSION:-main}"
DRY_RUN="${EFINDER_BUILD_DRY_RUN:-0}"

# Pin to "_latest" -- the user has accepted this trade-off (image
# always uses the most recent published Trixie Lite at build time).
BASE_IMAGE_URL="https://downloads.raspberrypi.com/raspios_lite_arm64_latest"

LOG()  { echo "==> $*"; }
WARN() { echo "WARNING: $*" >&2; }
FAIL() { echo "ERROR: $*" >&2; exit 1; }

# --- Preconditions -----------------------------------------------------------

[ "$EUID" -eq 0 ] || FAIL "Run as root (sudo bash $0)"

# Verify we're at the repo root
[ -d efinder ] || FAIL "must run from repo root (didn't find ./efinder/)"
[ -f scripts/install.sh ] || FAIL "missing scripts/install.sh"
[ -d webui ] || FAIL "missing ./webui/"
[ -d systemd ] || FAIL "missing ./systemd/"

# Verify required system tools
for tool in losetup parted e2fsck resize2fs mount umount xz; do
  command -v "$tool" >/dev/null 2>&1 \
    || FAIL "missing required tool: $tool"
done

if [ "$DRY_RUN" != "1" ]; then
  command -v qemu-aarch64-static >/dev/null 2>&1 \
    || FAIL "missing qemu-aarch64-static (apt install qemu-user-static)"
fi

WORK="$(pwd)/build/output"
mkdir -p "$WORK"
cd "$WORK"

# --- 1. Download base image ---------------------------------------------------

if [ ! -f base.img.xz ]; then
  LOG "Downloading base Raspberry Pi OS Lite image ($BASE_IMAGE_URL)"
  curl -fsSL --retry 3 -o base.img.xz "$BASE_IMAGE_URL" \
    || FAIL "Failed to download base image"
fi

if [ ! -f base.img ]; then
  LOG "Decompressing base image"
  xz -d -k base.img.xz
fi

LOG "Copying base.img -> efinder.img"
cp base.img efinder.img

# --- 2. Grow the image --------------------------------------------------------

LOG "Growing image by 2 GB"
truncate -s +2G efinder.img

# --- 3. Loop mount and grow root partition -----------------------------------

LOG "Loop-mounting"
LOOP=$(losetup -fP --show efinder.img)
LOG "Got $LOOP"

cleanup() {
  set +e
  for f in dev proc sys boot/firmware; do
    if mountpoint -q "$WORK/mnt/$f" 2>/dev/null; then
      umount "$WORK/mnt/$f" 2>/dev/null || umount -l "$WORK/mnt/$f" 2>/dev/null
    fi
  done
  if mountpoint -q "$WORK/mnt" 2>/dev/null; then
    umount "$WORK/mnt" 2>/dev/null || umount -l "$WORK/mnt" 2>/dev/null
  fi
  if [ -n "${LOOP:-}" ]; then
    losetup -d "$LOOP" 2>/dev/null || true
  fi
}
trap cleanup EXIT

partprobe "$LOOP"
sleep 1

# Verify partitions look right
[ -b "${LOOP}p1" ] || FAIL "${LOOP}p1 not found (expected boot partition)"
[ -b "${LOOP}p2" ] || FAIL "${LOOP}p2 not found (expected root partition)"

LOG "Resizing root partition"
parted -s "$LOOP" resizepart 2 100%
e2fsck -fy "${LOOP}p2"
resize2fs "${LOOP}p2"

# --- 4. Mount and prep chroot ------------------------------------------------

ROOT="$WORK/mnt"
mkdir -p "$ROOT"
mount "${LOOP}p2" "$ROOT"
mount "${LOOP}p1" "$ROOT/boot/firmware"

if [ "$DRY_RUN" = "1" ]; then
  LOG "DRY RUN: skipping chroot install"
  LOG "Image structure:"
  ls "$ROOT"
  LOG "Root partition free space:"
  df -h "$ROOT" | tail -1
  cleanup
  trap - EXIT
  LOG "Dry run complete; image at $WORK/efinder.img"
  exit 0
fi

# --- 4a. Edit boot partition files directly (outside chroot) -----------------

CONFIG_TXT="$ROOT/boot/firmware/config.txt"
CMDLINE_TXT="$ROOT/boot/firmware/cmdline.txt"

LOG "Patching $CONFIG_TXT for USB gadget + camera + I2C"
if [ -f "$CONFIG_TXT" ]; then
  for setting in \
    "camera_auto_detect=1" \
    "enable_uart=1" \
    "dtoverlay=imx477" \
    "dtparam=i2c_arm=on" \
    "dtparam=i2c_arm_baudrate=50000"; do
    if ! grep -qF "$setting" "$CONFIG_TXT"; then
      echo "$setting" >> "$CONFIG_TXT"
      LOG "  Added: $setting"
    else
      LOG "  Already present: $setting"
    fi
  done
  if ! grep -qxF "dtoverlay=dwc2,dr_mode=peripheral" "$CONFIG_TXT"; then
    printf "\n[all]\n# USB serial gadget -- peripheral mode for Pi Zero / Zero 2W\ndtoverlay=dwc2,dr_mode=peripheral\n" >> "$CONFIG_TXT"
    LOG "  Added: dtoverlay=dwc2,dr_mode=peripheral under [all]"
  else
    LOG "  Already present: dtoverlay=dwc2,dr_mode=peripheral"
  fi
else
  WARN "config.txt not found at $CONFIG_TXT; USB gadget mode not configured"
fi

LOG "Patching $CMDLINE_TXT for USB serial console"
if [ -f "$CMDLINE_TXT" ]; then
  if ! grep -q "console=ttyGS0" "$CMDLINE_TXT"; then
    cp "$CMDLINE_TXT" "$CMDLINE_TXT.orig"
    if grep -q "rootwait" "$CMDLINE_TXT"; then
      sed -i 's/rootwait/rootwait console=ttyGS0,115200/' "$CMDLINE_TXT"
    else
      sed -i '1s/^/console=ttyGS0,115200 /' "$CMDLINE_TXT"
    fi
    LOG "  console=ttyGS0,115200 added to cmdline.txt"
    LOG "  cmdline.txt is now: $(cat "$CMDLINE_TXT")"
  else
    LOG "  console=ttyGS0,115200 already present in cmdline.txt"
  fi
else
  WARN "cmdline.txt not found at $CMDLINE_TXT; USB serial console not configured"
fi

# qemu-user-static for cross-arch chroot
cp /usr/bin/qemu-aarch64-static "$ROOT/usr/bin/"

# Bind kernel filesystems
for f in dev proc sys; do
  mount --bind "/$f" "$ROOT/$f"
done

# Replace resolv.conf so apt-get inside chroot has DNS
mv "$ROOT/etc/resolv.conf" "$ROOT/etc/resolv.conf.bak" 2>/dev/null || true
echo "nameserver 1.1.1.1" > "$ROOT/etc/resolv.conf"
echo "nameserver 8.8.8.8" >> "$ROOT/etc/resolv.conf"

# Disable invoking services from postinst during apt installs
cat > "$ROOT/usr/sbin/policy-rc.d" << 'EOF'
#!/bin/sh
exit 101
EOF
chmod +x "$ROOT/usr/sbin/policy-rc.d"

# --- 5. Stage source tree into chroot ----------------------------------------

LOG "Copying eFinder repo into chroot at /tmp/efinder-src"
mkdir -p "$ROOT/tmp/efinder-src"
SRC_DIR="$(cd "$WORK/../.." && pwd)"

# Required directories
for d in efinder webui systemd scripts etc tests; do
  [ -d "$SRC_DIR/$d" ] || FAIL "missing source dir: $SRC_DIR/$d"
  cp -r "$SRC_DIR/$d" "$ROOT/tmp/efinder-src/"
done

# Required files
for f in requirements.txt; do
  [ -f "$SRC_DIR/$f" ] || FAIL "missing source file: $SRC_DIR/$f"
  cp "$SRC_DIR/$f" "$ROOT/tmp/efinder-src/"
done

# vendor/ contains the pre-built tetra3-py aarch64 wheel committed by the
# 'Vendor Binaries (olive)' workflow. Stage it so install.sh can use it
# without any network fetch.
if [ -d "$SRC_DIR/vendor" ]; then
  LOG "Staging vendor/ (pre-built tetra3-py wheel)"
  cp -r "$SRC_DIR/vendor" "$ROOT/tmp/efinder-src/"
else
  WARN "vendor/ not found; install.sh will fail (run Vendor Binaries workflow first)"
fi

# Optional documentation
for f in README.md TODO.md; do
  [ -f "$SRC_DIR/$f" ] && cp "$SRC_DIR/$f" "$ROOT/tmp/efinder-src/" || true
done

# Stage pre-generated star database (built on x86_64 CI runner before
# this script runs, avoiding hip_main.dat issues inside QEMU aarch64).
CHROOT_DB_PATH=""
if [ -n "${EFINDER_SOLVER_DB:-}" ] && [ -f "${EFINDER_SOLVER_DB}" ]; then
  LOG "Staging pre-generated star database ($(du -sh "${EFINDER_SOLVER_DB}" | cut -f1))"
  mkdir -p "$ROOT/tmp/solver-db"
  cp "${EFINDER_SOLVER_DB}" "$ROOT/tmp/solver-db/default_database.npz"
  CHROOT_DB_PATH="/tmp/solver-db/default_database.npz"
else
  WARN "EFINDER_SOLVER_DB not set or file not found; database will not be baked in"
fi

cat > "$ROOT/tmp/run-install.sh" << EOSH
#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export EFINDER_CHROOT=1
export EFINDER_VERSION="${EFINDER_VERSION}"
${CHROOT_DB_PATH:+export EFINDER_SOLVER_DB="${CHROOT_DB_PATH}"}

cd /tmp/efinder-src
bash scripts/install.sh
EOSH
chmod +x "$ROOT/tmp/run-install.sh"

# --- 6. Run install.sh in chroot ---------------------------------------------

LOG "Running install.sh inside chroot (this is the slow part, ~10-20 min)"
chroot "$ROOT" /tmp/run-install.sh

# --- 7. Cleanup --------------------------------------------------------------

LOG "Cleaning up chroot"
rm -f "$ROOT/usr/sbin/policy-rc.d"
rm -f "$ROOT/etc/resolv.conf"
mv "$ROOT/etc/resolv.conf.bak" "$ROOT/etc/resolv.conf" 2>/dev/null || true
rm -rf "$ROOT/tmp/efinder-src" "$ROOT/tmp/run-install.sh" "$ROOT/tmp/solver-db"
rm -f "$ROOT/usr/bin/qemu-aarch64-static"

# Trim apt caches to reduce final image size
chroot "$ROOT" apt-get clean
rm -rf "$ROOT/var/lib/apt/lists/"*

LOG "Unmounting"
sync
cleanup
trap - EXIT

LOG "Image ready at $WORK/efinder.img"
ls -lh "$WORK/efinder.img"
