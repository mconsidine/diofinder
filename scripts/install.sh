#!/bin/bash
# diofinder install script.
#
# Installs the sycamore star_detect wheel and olive-solve's tetra3-py wheel
# (Rust, fully in-process plate solver). No external gRPC server dependency.
#
# Two execution modes, autodetected:
#
#   * "fresh"  : run by a user on a freshly flashed Trixie Lite SD card.
#   * "chroot" : run inside qemu-aarch64 chroot during image build.
#                Source staged at /tmp/diofinder-src/. DIOFINDER_CHROOT=1.
#
# Chroot-mode optional env:
#   DIOFINDER_SOLVER_DB  Path (inside the chroot) to a pre-generated tetra3
#                      .npz database. Staged by build-image.sh from the
#                      output of the 'Generate tetra3 star database' step
#                      in release.yml (runs on x86_64, not QEMU aarch64).
#                      If absent, the image ships without a database and the
#                      user must set solver_db in diofinder.conf before use.

set -euo pipefail

# --- Config ------------------------------------------------------------------
DIOFINDER_USER="diofinder"
DIOFINDER_HOME="/home/${DIOFINDER_USER}"
DIOFINDER_DIR="/opt/diofinder"
# OTA clone URL: overridable via DIOFINDER_REPO_URL (the image build passes the
# real repo from GitHub's context). The default is the canonical repo and only
# applies to a bare `bash install.sh` fresh install.
REPO_URL="${DIOFINDER_REPO_URL:-https://github.com/mconsidine/diofinder.git}"
TARGET_VERSION="${DIOFINDER_VERSION:-latest}"
# Branch/tag the image's /opt/diofinder should track for OTA (empty = skip git
# provisioning of a copied tree). Set by the image build from github.ref_name.
GIT_REF="${DIOFINDER_GIT_REF:-}"
IN_CHROOT="${DIOFINDER_CHROOT:-0}"
SRC_STAGED="/tmp/diofinder-src"

LOG()  { echo "==> $*"; }
WARN() { echo "WARNING: $*" >&2; }
FAIL() { echo "ERROR: $*" >&2; exit 1; }

# --- Preconditions -----------------------------------------------------------

[ "$EUID" -eq 0 ] || FAIL "Run as root (sudo bash $0)"

if [ "$IN_CHROOT" = "1" ]; then
  LOG "Running in chroot mode (image build) version=$TARGET_VERSION"
  [ -d "$SRC_STAGED" ] \
    || FAIL "chroot mode requires source staged at $SRC_STAGED"
  [ -f "$SRC_STAGED/scripts/install.sh" ] \
    || FAIL "$SRC_STAGED looks incomplete"
else
  LOG "Running in fresh-install mode version=$TARGET_VERSION"
fi

# --- Create diofinder user -----------------------------------------------------

if ! id -u "$DIOFINDER_USER" >/dev/null 2>&1; then
  LOG "Creating user $DIOFINDER_USER"
  useradd -m -s /bin/bash "$DIOFINDER_USER"
  echo "${DIOFINDER_USER}:12345678" | chpasswd
  usermod -aG video,gpio,i2c,dialout,sudo,netdev,systemd-journal "$DIOFINDER_USER" || true
fi

# --- Hostname ----------------------------------------------------------------

LOG "Setting hostname to diofinder"
echo "diofinder" > /etc/hostname
if grep -q "^127\.0\.1\.1" /etc/hosts; then
  sed -i $'s/^127\.0\.1\.1.*/127.0.1.1\tdiofinder/' /etc/hosts
else
  printf "127.0.1.1\tdiofinder\n" >> /etc/hosts
fi

# --- WiFi regulatory domain --------------------------------------------------
LOG "Setting WiFi regulatory domain to US"
mkdir -p /etc/default
echo "REGDOMAIN=US" > /etc/default/crda

# --- Disable cloud-init ------------------------------------------------------
LOG "Disabling cloud-init"
mkdir -p /etc/cloud
touch /etc/cloud/cloud-init.disabled

# --- System packages ---------------------------------------------------------

LOG "Updating apt and installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip python3-dev \
  python3-numpy python3-scipy python3-pil \
  python3-picamera2 python3-libcamera \
  rpicam-apps \
  python3-flask \
  build-essential pkg-config \
  curl ca-certificates git wget \
  avahi-daemon \
  openssh-server \
  network-manager \
  initramfs-tools \
  iw \
  wireless-regdb

# --- zram compressed swap ----------------------------------------------------
LOG "Installing zram-tools (optional)"
if apt-get install -y zram-tools 2>/dev/null; then
  LOG "Configuring zram swap"
  cat > /etc/default/zramswap << 'EOF'
PERCENT=50
ALGO=lz4
EOF
  systemctl enable zramswap.service 2>/dev/null || true
else
  WARN "zram-tools not available (non-fatal)"
fi

systemctl enable ssh.service 2>/dev/null || systemctl enable ssh.socket || true

LOG "Configuring NetworkManager"
mkdir -p /etc/NetworkManager
cat > /etc/NetworkManager/NetworkManager.conf << 'EOF'
[main]
plugins=ifupdown,keyfile

[ifupdown]
managed=true
EOF

# --- Application code --------------------------------------------------------

if [ "$IN_CHROOT" = "1" ]; then
  if [ ! -d "$DIOFINDER_DIR" ]; then
    LOG "Copying staged source $SRC_STAGED -> $DIOFINDER_DIR"
    mkdir -p "$DIOFINDER_DIR"
    cp -r "$SRC_STAGED/." "$DIOFINDER_DIR/"
    chown -R "$DIOFINDER_USER:$DIOFINDER_USER" "$DIOFINDER_DIR"
  else
    WARN "$DIOFINDER_DIR already exists; reusing"
  fi
  # Graft git metadata onto the copied tree so OTA (diofinder-update / webui
  # Update) works on the imaged device. Best-effort: a copy without this still
  # runs, it just can't self-update. Skipped silently if no ref/network.
  if [ -n "$GIT_REF" ] && [ ! -d "$DIOFINDER_DIR/.git" ] && command -v git >/dev/null 2>&1; then
    LOG "Provisioning git clone at $DIOFINDER_DIR (origin=$REPO_URL ref=$GIT_REF)"
    cd "$DIOFINDER_DIR"
    sudo -u "$DIOFINDER_USER" git init -q
    sudo -u "$DIOFINDER_USER" git remote add origin "$REPO_URL"
    sudo -u "$DIOFINDER_USER" git config remote.origin.fetch \
      "+refs/heads/*:refs/remotes/origin/*"
    if sudo -u "$DIOFINDER_USER" git fetch --depth 1 --tags origin "$GIT_REF" 2>/dev/null \
       && sudo -u "$DIOFINDER_USER" git fetch --depth 1 origin 2>/dev/null; then
      # Reset the working tree to the fetched ref (identical to the copy for a
      # CI build) so `git status` is clean and diofinder-update's guard passes.
      if sudo -u "$DIOFINDER_USER" git checkout -f -B "$GIT_REF" "origin/$GIT_REF" 2>/dev/null \
         || sudo -u "$DIOFINDER_USER" git checkout -f "$GIT_REF" 2>/dev/null \
         || sudo -u "$DIOFINDER_USER" git checkout -f FETCH_HEAD 2>/dev/null; then
        # Trust the diofinder-owned repo for root (diofinder-update's guard).
        git config --system --add safe.directory "$DIOFINDER_DIR" 2>/dev/null || true
        LOG "git: $DIOFINDER_DIR tracks $GIT_REF — OTA enabled"
      else
        WARN "git: checkout of $GIT_REF failed; OTA disabled on this image"
        rm -rf "$DIOFINDER_DIR/.git"
      fi
    else
      WARN "git: could not fetch $GIT_REF from $REPO_URL; OTA disabled (offline build?)"
      rm -rf "$DIOFINDER_DIR/.git"
    fi
  fi
else
  if [ ! -d "$DIOFINDER_DIR/.git" ]; then
    LOG "Cloning diofinder code to $DIOFINDER_DIR"
    # --no-single-branch so the clone can later fetch ANY branch for OTA
    # (diofinder-update --ref BRANCH), not just the default.
    git clone --depth 1 --no-single-branch "$REPO_URL" "$DIOFINDER_DIR"
    chown -R "$DIOFINDER_USER:$DIOFINDER_USER" "$DIOFINDER_DIR"
    git config --system --add safe.directory "$DIOFINDER_DIR" 2>/dev/null || true
  fi
  cd "$DIOFINDER_DIR"
  if [ "$TARGET_VERSION" != "latest" ]; then
    LOG "Checking out $TARGET_VERSION"
    sudo -u "$DIOFINDER_USER" git fetch --tags origin
    sudo -u "$DIOFINDER_USER" git checkout --quiet "$TARGET_VERSION"
  fi
fi

# --- Python venv -------------------------------------------------------------

if [ ! -d "$DIOFINDER_DIR/venv" ]; then
  LOG "Creating Python venv (with system site packages for picamera2)"
  sudo -u "$DIOFINDER_USER" python3 -m venv \
    --system-site-packages "$DIOFINDER_DIR/venv"
fi

LOG "Installing Python deps"
sudo -u "$DIOFINDER_USER" "$DIOFINDER_DIR/venv/bin/pip" install --upgrade pip \
  || FAIL "pip upgrade failed"
sudo -u "$DIOFINDER_USER" "$DIOFINDER_DIR/venv/bin/pip" install --upgrade \
  setuptools wheel \
  || FAIL "pip install setuptools wheel failed"

# --- Install olive-solve tetra3-py -------------------------------------------
# Only aarch64 wheels are accepted (armv6l wheels will NOT work on the
# Pi Zero 2W). vendor/wheels/ is populated at image-build time by
# release.yml (downloaded from the olive-solve GitHub release) or locally
# by build/local/vendor-wheels.sh.

VENDOR_WHEELS_DIR="$DIOFINDER_DIR/vendor/wheels"
OLIVE_WHL=$(ls "$VENDOR_WHEELS_DIR"/tetra3-*aarch64*.whl 2>/dev/null | head -1 || true)
if [ -z "$OLIVE_WHL" ]; then
  FAIL "No aarch64 tetra3-py wheel found in $VENDOR_WHEELS_DIR." \
  "Download one from the mconsidine/olive-solve GitHub release into vendor/wheels/ first."
fi
LOG "Installing olive-solve: $OLIVE_WHL"
sudo -u "$DIOFINDER_USER" "$DIOFINDER_DIR/venv/bin/pip" install "$OLIVE_WHL" \
  || FAIL "olive-solve tetra3-py install failed"

sudo -u "$DIOFINDER_USER" "$DIOFINDER_DIR/venv/bin/python" -c "
import tetra3
if not hasattr(tetra3.Tetra3, 'solve_from_image_fast'):
    raise RuntimeError('olive-solve wheel not active (solve_from_image_fast missing)')
print('olive-solve tetra3-py OK')
" || FAIL "olive-solve verification failed"

# --- Install sycamore-extract star_detect (REQUIRED) -------------------------
# star_detect is a hard runtime requirement: diofinder/bg_cache.py imports it
# at module top and solver_proc refuses to start without it (there is NO
# fallback extractor when the wheel is absent — the tetra3 backend probe only
# covers the reverse direction). A missing wheel must fail the install loudly,
# not produce an image whose solver restart-loops forever.

SYCAMORE_WHL=$(ls "$VENDOR_WHEELS_DIR"/star_detect-*aarch64*.whl 2>/dev/null | head -1 || true)
if [ -z "$SYCAMORE_WHL" ]; then
  echo "FATAL: no sycamore-extract (star_detect) wheel in $VENDOR_WHEELS_DIR — the solver cannot run without it." >&2
  exit 1
fi
LOG "Installing sycamore-extract: $SYCAMORE_WHL"
sudo -u "$DIOFINDER_USER" "$DIOFINDER_DIR/venv/bin/pip" install "$SYCAMORE_WHL"
sudo -u "$DIOFINDER_USER" "$DIOFINDER_DIR/venv/bin/python" -c "
import star_detect
print('sycamore star_detect OK')
" && LOG "sycamore star_detect verified"

# --- Install star database ---------------------------------------------------
# The database is generated on the x86_64 CI runner by release.yml and
# staged into the chroot by build-image.sh as DIOFINDER_SOLVER_DB.

mkdir -p /var/lib/diofinder
chown "${DIOFINDER_USER}:${DIOFINDER_USER}" /var/lib/diofinder 2>/dev/null || true

# Stamp the build version so a freshly burned (never-OTA'd) image reports its
# real tag via `diofinder-ctl version` / the web UI, in the same format
# diofinder-update writes. Prefer the explicit build tag (DIOFINDER_VERSION); fall
# back to `git describe` of the provisioned checkout (a shallow clone may only
# yield a short sha, which is still better than the stale in-code default).
_stamp_ver="${DIOFINDER_VERSION:-}"
case "$_stamp_ver" in
  ""|latest|main)
    _stamp_ver="$(sudo -u "$DIOFINDER_USER" git -C "$DIOFINDER_DIR" describe \
      --tags --always 2>/dev/null || echo unknown)" ;;
esac
echo "$_stamp_ver $(date -u +%Y-%m-%dT%H:%M:%SZ)" > /var/lib/diofinder/version
chown "${DIOFINDER_USER}:${DIOFINDER_USER}" /var/lib/diofinder/version 2>/dev/null || true
LOG "Stamped image version: $_stamp_ver"

SOLVER_DB="/var/lib/diofinder/default_database.npz"

if [ ! -f "$SOLVER_DB" ]; then
  if [ -n "${DIOFINDER_SOLVER_DB:-}" ] && [ -f "${DIOFINDER_SOLVER_DB}" ]; then
    LOG "Installing pre-generated star database ($(du -sh "${DIOFINDER_SOLVER_DB}" | cut -f1))"
    cp "${DIOFINDER_SOLVER_DB}" "$SOLVER_DB"
    chown "${DIOFINDER_USER}:${DIOFINDER_USER}" "$SOLVER_DB"
    LOG "Star database installed at $SOLVER_DB"
  else
    WARN "No star database provided; set solver_db in /etc/diofinder/diofinder.conf before first use"
  fi
fi

# --- Install optional deep (mag 8.5) star database ---------------------------
# Used by the "bad" seeing preset (star_db_deep in diofinder.conf points here).
# Staged into the chroot by build-image.sh as DIOFINDER_SOLVER_DB_DEEP. Optional;
# if absent, selecting Bad falls back to the standard DB at runtime.

SOLVER_DB_DEEP="/var/lib/diofinder/diofinder_13deg_mag85.npz"

if [ ! -f "$SOLVER_DB_DEEP" ]; then
  if [ -n "${DIOFINDER_SOLVER_DB_DEEP:-}" ] && [ -f "${DIOFINDER_SOLVER_DB_DEEP}" ]; then
    LOG "Installing deep star database ($(du -sh "${DIOFINDER_SOLVER_DB_DEEP}" | cut -f1))"
    cp "${DIOFINDER_SOLVER_DB_DEEP}" "$SOLVER_DB_DEEP"
    chown "${DIOFINDER_USER}:${DIOFINDER_USER}" "$SOLVER_DB_DEEP"
    LOG "Deep star database installed at $SOLVER_DB_DEEP"
  else
    LOG "No deep star database provided; 'bad' seeing preset will use the standard DB"
  fi
fi

# --- Install star-names catalog ----------------------------------------------
# Downloaded from the astro_databases release by release.yml and staged into
# the chroot by build-image.sh as DIOFINDER_STAR_NAMES. Optional; a missing
# catalog just disables the brightest-star label on the Camera page.

STAR_NAMES="/var/lib/diofinder/star_names.csv"
if [ -n "${DIOFINDER_STAR_NAMES:-}" ] && [ -f "${DIOFINDER_STAR_NAMES}" ]; then
  LOG "Installing star-names catalog ($(du -sh "${DIOFINDER_STAR_NAMES}" | cut -f1))"
  cp "${DIOFINDER_STAR_NAMES}" "$STAR_NAMES"
  chown "${DIOFINDER_USER}:${DIOFINDER_USER}" "$STAR_NAMES"
fi

# --- Download solver test images (fresh install only) ------------------------
# In chroot/image-build mode these are downloaded by build-image.sh after
# the chroot exits, so they are already present in the image.

if [ "$IN_CHROOT" != "1" ]; then
  LOG "Downloading solver test images..."
  mkdir -p "$DIOFINDER_DIR/test-images"
  OLIVE_RAW="https://raw.githubusercontent.com/mconsidine/olive-solve/main/tetra3/tests/fixtures/sample_images"
  for img in orion_belt.jpg orion2.jpg pleiades.jpg orion_trees.jpg crappy.jpg; do
    wget -q "${OLIVE_RAW}/${img}" \
         -O "$DIOFINDER_DIR/test-images/${img}" \
      && LOG "  ${img}" \
      || WARN "  Could not download: ${img} (non-fatal)"
  done
  chown -R "$DIOFINDER_USER:$DIOFINDER_USER" "$DIOFINDER_DIR/test-images" || true
fi

# --- systemd units -----------------------------------------------------------

LOG "Installing systemd units"
install -m 644 "$DIOFINDER_DIR/systemd/diofinder.service"             /etc/systemd/system/
install -m 644 "$DIOFINDER_DIR/systemd/diofinder-firstboot.service"   /etc/systemd/system/
install -m 644 "$DIOFINDER_DIR/systemd/diofinder-webui.service"       /etc/systemd/system/
install -m 644 "$DIOFINDER_DIR/systemd/diofinder-usb-gadget.service"  /etc/systemd/system/
install -m 644 "$DIOFINDER_DIR/systemd/diofinder-ensure-ap.service"   /etc/systemd/system/

install -m 440 "$DIOFINDER_DIR/etc/sudoers.d/diofinder-update" /etc/sudoers.d/diofinder-update
install -m 440 "$DIOFINDER_DIR/etc/sudoers.d/diofinder-clock"  /etc/sudoers.d/diofinder-clock
install -m 440 "$DIOFINDER_DIR/etc/sudoers.d/diofinder-wifi"   /etc/sudoers.d/diofinder-wifi
install -m 440 "$DIOFINDER_DIR/etc/sudoers.d/diofinder-factory-reset" /etc/sudoers.d/diofinder-factory-reset

# Auto-activate the venv for interactive login (ssh) shells.
install -m 644 "$DIOFINDER_DIR/etc/profile.d/diofinder-venv.sh" /etc/profile.d/diofinder-venv.sh

install -m 755 "$DIOFINDER_DIR/scripts/diofinder-update"          /usr/local/bin/
install -m 755 "$DIOFINDER_DIR/scripts/diofinder-factory-reset"   /usr/local/bin/
install -m 755 "$DIOFINDER_DIR/scripts/diofinder-db-update"       /usr/local/bin/
install -m 755 "$DIOFINDER_DIR/scripts/diofinder-ctl"             /usr/local/bin/
install -m 755 "$DIOFINDER_DIR/scripts/diofinder-bg-setup"        /usr/local/bin/diofinder-bg-setup
install -m 755 "$DIOFINDER_DIR/scripts/diofinder-bg-test"         /usr/local/bin/diofinder-bg-test
install -m 755 "$DIOFINDER_DIR/scripts/ap.sh"                   /usr/local/bin/ap.sh
install -m 755 "$DIOFINDER_DIR/scripts/station.sh"              /usr/local/bin/station.sh
install -m 755 "$DIOFINDER_DIR/scripts/diofinder-gadget-connect"  /usr/local/bin/diofinder-gadget-connect
install -m 755 "$DIOFINDER_DIR/scripts/diofinder-set-time"        /usr/local/bin/diofinder-set-time
chmod 755 "$DIOFINDER_DIR/scripts/firstboot.sh"

# --- libcamera tuning (finder-optimised IMX477) ------------------------------
# DPC off (it deletes 1-2 px faint stars) + asinh companding gamma so faint
# stars survive the 12->8-bit reduction; black_level kept at the sensor pedestal.
# Copied into the vc4 tuning dir so camera_tuning_file can point at it. NR /
# sharpen / AWB are already disabled at runtime by camera_proc's controls, so
# this tuning only changes the two stages the tuning alone governs (DPC, gamma).
# Non-fatal if the libcamera path is absent (e.g. a non-Pi build host).
LIBCAMERA_VC4=/usr/share/libcamera/ipa/rpi/vc4
if [ -d "$LIBCAMERA_VC4" ]; then
  install -m 644 "$DIOFINDER_DIR/tuning/imx477_finder.json" "$LIBCAMERA_VC4/imx477_finder.json"
  LOG "Installed finder libcamera tuning to $LIBCAMERA_VC4/imx477_finder.json"
else
  WARN "libcamera vc4 tuning dir not found ($LIBCAMERA_VC4); skipping finder tuning install"
fi

chown -R "$DIOFINDER_USER:$DIOFINDER_USER" /var/lib/diofinder

mkdir -p /etc/diofinder
# The directory itself, not just diofinder.conf, must be owned by
# $DIOFINDER_USER: save_keys() (diofinder/config.py) creates a sibling
# .lock file and a .tmp file for its atomic replace, both of which need
# directory-level write permission, not just file-level. Installing the
# conf file with -o/-g alone left the directory root:root 755, so every
# settings-persist call (any webui Save button, :St/:Sg, alignment, auto
# calibration) failed with PermissionError on a freshly imaged or
# reprovisioned device. Unconditional (not just on first install) so an
# upgrade of an already-broken device is also repaired.
chown "$DIOFINDER_USER:$DIOFINDER_USER" /etc/diofinder

if [ ! -f /etc/diofinder/diofinder.conf ]; then
  install -m 644 -o "$DIOFINDER_USER" -g "$DIOFINDER_USER" \
    "$DIOFINDER_DIR/etc/diofinder.conf.default" /etc/diofinder/diofinder.conf
fi

# --- Boot config / kernel options --------------------------------------------

CONFIG_TXT=/boot/firmware/config.txt
CMDLINE_TXT=/boot/firmware/cmdline.txt

if [ -f "$CONFIG_TXT" ]; then
  if ! grep -q "^camera_auto_detect=1" "$CONFIG_TXT"; then
    LOG "Enabling camera_auto_detect in $CONFIG_TXT"
    echo "camera_auto_detect=1" >> "$CONFIG_TXT"
  fi
  if ! grep -q "^enable_uart=1" "$CONFIG_TXT"; then
    echo "enable_uart=1" >> "$CONFIG_TXT"
  fi
  if ! grep -qxF "dtoverlay=imx477" "$CONFIG_TXT"; then
    echo "dtoverlay=imx477" >> "$CONFIG_TXT"
  fi
  if ! grep -qxF "dtparam=i2c_arm=on" "$CONFIG_TXT"; then
    echo "dtparam=i2c_arm=on" >> "$CONFIG_TXT"
    LOG "Enabled I2C in $CONFIG_TXT"
  fi
  if ! grep -qF "i2c_arm_baudrate" "$CONFIG_TXT"; then
    echo "dtparam=i2c_arm_baudrate=50000" >> "$CONFIG_TXT"
    LOG "Set I2C to 50kHz (BNO055 clock-stretching fix)"
  fi
  if ! grep -qxF "dtoverlay=dwc2,dr_mode=peripheral" "$CONFIG_TXT"; then
    printf "\n[all]\n# USB serial gadget\ndtoverlay=dwc2,dr_mode=peripheral\n" \
      >> "$CONFIG_TXT"
  fi
fi

LOG "Writing /etc/modules-load.d/diofinder-gadget.conf"
mkdir -p /etc/modules-load.d
cat > /etc/modules-load.d/diofinder-gadget.conf << 'EOF'
dwc2
libcomposite
u_serial
usb_f_acm
EOF

LOG "Enabling i2c-dev module for /dev/i2c-* device nodes"
grep -qxF i2c-dev /etc/modules 2>/dev/null || echo i2c-dev >> /etc/modules

LOG "Adding dwc2 module to initramfs"
mkdir -p /etc/initramfs-tools
grep -qxF dwc2 /etc/initramfs-tools/modules 2>/dev/null \
  || echo dwc2 >> /etc/initramfs-tools/modules
update-initramfs -u -k all \
  || WARN "update-initramfs failed; USB gadget may be delayed on first boot"

if [ -f "$CMDLINE_TXT" ]; then
  if ! grep -q "console=ttyGS0" "$CMDLINE_TXT"; then
    LOG "Adding console=ttyGS0,115200 to $CMDLINE_TXT"
    [ -f "$CMDLINE_TXT.diofinder-orig" ] \
      || cp "$CMDLINE_TXT" "$CMDLINE_TXT.diofinder-orig"
    if grep -q "rootwait" "$CMDLINE_TXT"; then
      sed -i 's/rootwait/rootwait console=ttyGS0,115200/' "$CMDLINE_TXT"
    else
      sed -i '1s|^|console=ttyGS0,115200 |' "$CMDLINE_TXT"
    fi
  fi
fi

LOG "Enabling USB serial console (serial-getty@ttyGS0)"
systemctl enable serial-getty@ttyGS0.service 2>/dev/null || \
  WARN "Could not enable serial-getty@ttyGS0"

# --- Enable services ---------------------------------------------------------

LOG "Enabling services"
systemctl daemon-reload
systemctl enable diofinder.service \
                 diofinder-firstboot.service diofinder-webui.service \
                 diofinder-usb-gadget.service diofinder-ensure-ap.service

if [ "$IN_CHROOT" != "1" ]; then
  LOG "Starting services"
  systemctl start diofinder.service diofinder-webui.service || true
fi

# --- Reboot ------------------------------------------------------------------

if [ "$IN_CHROOT" != "1" ]; then
  LOG "Install complete. Rebooting in 5s..."
  sleep 5
  reboot
else
  LOG "Install complete (chroot mode)"
fi
