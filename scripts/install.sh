#!/bin/bash
# eFinder install script.
#
# Installs the sycamore star_detect wheel and olive-solve's tetra3-py wheel
# (Rust, fully in-process plate solver). No external gRPC server dependency.
#
# Two execution modes, autodetected:
#
#   * "fresh"  : run by a user on a freshly flashed Trixie Lite SD card.
#   * "chroot" : run inside qemu-aarch64 chroot during image build.
#                Source staged at /tmp/efinder-src/. EFINDER_CHROOT=1.
#
# Chroot-mode optional env:
#   EFINDER_SOLVER_DB  Path (inside the chroot) to a pre-generated tetra3
#                      .npz database. Staged by build-image.sh from the
#                      output of the 'Generate tetra3 star database' step
#                      in release.yml (runs on x86_64, not QEMU aarch64).
#                      If absent, the image ships without a database and the
#                      user must set solver_db in efinder.conf before use.

set -euo pipefail

# --- Config ------------------------------------------------------------------
EFINDER_USER="efinder"
EFINDER_HOME="/home/${EFINDER_USER}"
EFINDER_DIR="/opt/efinder"
# OTA clone URL: overridable via EFINDER_REPO_URL (the image build passes the
# real repo from GitHub's context). The default is the canonical repo and only
# applies to a bare `bash install.sh` fresh install.
REPO_URL="${EFINDER_REPO_URL:-https://github.com/mconsidine/diofinder.git}"
TARGET_VERSION="${EFINDER_VERSION:-latest}"
# Branch/tag the image's /opt/efinder should track for OTA (empty = skip git
# provisioning of a copied tree). Set by the image build from github.ref_name.
GIT_REF="${EFINDER_GIT_REF:-}"
IN_CHROOT="${EFINDER_CHROOT:-0}"
SRC_STAGED="/tmp/efinder-src"

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

# --- Create efinder user -----------------------------------------------------

if ! id -u "$EFINDER_USER" >/dev/null 2>&1; then
  LOG "Creating user $EFINDER_USER"
  useradd -m -s /bin/bash "$EFINDER_USER"
  echo "${EFINDER_USER}:12345678" | chpasswd
  usermod -aG video,gpio,i2c,dialout,sudo,netdev,systemd-journal "$EFINDER_USER" || true
fi

# --- Hostname ----------------------------------------------------------------

LOG "Setting hostname to efinder"
echo "efinder" > /etc/hostname
if grep -q "^127\.0\.1\.1" /etc/hosts; then
  sed -i $'s/^127\.0\.1\.1.*/127.0.1.1\tefinder/' /etc/hosts
else
  printf "127.0.1.1\tefinder\n" >> /etc/hosts
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
  if [ ! -d "$EFINDER_DIR" ]; then
    LOG "Copying staged source $SRC_STAGED -> $EFINDER_DIR"
    mkdir -p "$EFINDER_DIR"
    cp -r "$SRC_STAGED/." "$EFINDER_DIR/"
    chown -R "$EFINDER_USER:$EFINDER_USER" "$EFINDER_DIR"
  else
    WARN "$EFINDER_DIR already exists; reusing"
  fi
  # Graft git metadata onto the copied tree so OTA (efinder-update / webui
  # Update) works on the imaged device. Best-effort: a copy without this still
  # runs, it just can't self-update. Skipped silently if no ref/network.
  if [ -n "$GIT_REF" ] && [ ! -d "$EFINDER_DIR/.git" ] && command -v git >/dev/null 2>&1; then
    LOG "Provisioning git clone at $EFINDER_DIR (origin=$REPO_URL ref=$GIT_REF)"
    cd "$EFINDER_DIR"
    sudo -u "$EFINDER_USER" git init -q
    sudo -u "$EFINDER_USER" git remote add origin "$REPO_URL"
    sudo -u "$EFINDER_USER" git config remote.origin.fetch \
      "+refs/heads/*:refs/remotes/origin/*"
    if sudo -u "$EFINDER_USER" git fetch --depth 1 --tags origin "$GIT_REF" 2>/dev/null \
       && sudo -u "$EFINDER_USER" git fetch --depth 1 origin 2>/dev/null; then
      # Reset the working tree to the fetched ref (identical to the copy for a
      # CI build) so `git status` is clean and efinder-update's guard passes.
      if sudo -u "$EFINDER_USER" git checkout -f -B "$GIT_REF" "origin/$GIT_REF" 2>/dev/null \
         || sudo -u "$EFINDER_USER" git checkout -f "$GIT_REF" 2>/dev/null \
         || sudo -u "$EFINDER_USER" git checkout -f FETCH_HEAD 2>/dev/null; then
        # Trust the efinder-owned repo for root (efinder-update's guard).
        git config --system --add safe.directory "$EFINDER_DIR" 2>/dev/null || true
        LOG "git: $EFINDER_DIR tracks $GIT_REF — OTA enabled"
      else
        WARN "git: checkout of $GIT_REF failed; OTA disabled on this image"
        rm -rf "$EFINDER_DIR/.git"
      fi
    else
      WARN "git: could not fetch $GIT_REF from $REPO_URL; OTA disabled (offline build?)"
      rm -rf "$EFINDER_DIR/.git"
    fi
  fi
else
  if [ ! -d "$EFINDER_DIR/.git" ]; then
    LOG "Cloning eFinder code to $EFINDER_DIR"
    # --no-single-branch so the clone can later fetch ANY branch for OTA
    # (efinder-update --ref BRANCH), not just the default.
    git clone --depth 1 --no-single-branch "$REPO_URL" "$EFINDER_DIR"
    chown -R "$EFINDER_USER:$EFINDER_USER" "$EFINDER_DIR"
    git config --system --add safe.directory "$EFINDER_DIR" 2>/dev/null || true
  fi
  cd "$EFINDER_DIR"
  if [ "$TARGET_VERSION" != "latest" ]; then
    LOG "Checking out $TARGET_VERSION"
    sudo -u "$EFINDER_USER" git fetch --tags origin
    sudo -u "$EFINDER_USER" git checkout --quiet "$TARGET_VERSION"
  fi
fi

# --- Python venv -------------------------------------------------------------

if [ ! -d "$EFINDER_DIR/venv" ]; then
  LOG "Creating Python venv (with system site packages for picamera2)"
  sudo -u "$EFINDER_USER" python3 -m venv \
    --system-site-packages "$EFINDER_DIR/venv"
fi

LOG "Installing Python deps"
sudo -u "$EFINDER_USER" "$EFINDER_DIR/venv/bin/pip" install --upgrade pip \
  || FAIL "pip upgrade failed"
sudo -u "$EFINDER_USER" "$EFINDER_DIR/venv/bin/pip" install --upgrade \
  setuptools wheel \
  || FAIL "pip install setuptools wheel failed"

# --- Install olive-solve tetra3-py -------------------------------------------
# Only aarch64 wheels are accepted. The armv6l pre-built wheel shipped in
# olive-solve/tetra3-py/dist/ will NOT work on Pi Zero 2W (aarch64).
# Run 'Vendor Binaries (olive)' to produce and commit the aarch64 wheel.

VENDOR_WHEELS_DIR="$EFINDER_DIR/vendor/wheels"
OLIVE_WHL=$(ls "$VENDOR_WHEELS_DIR"/tetra3-*aarch64*.whl 2>/dev/null | head -1 || true)
if [ -z "$OLIVE_WHL" ]; then
  FAIL "No aarch64 tetra3-py wheel found in $VENDOR_WHEELS_DIR." \
  "Run the 'Vendor Binaries (olive)' workflow first."
fi
LOG "Installing olive-solve: $OLIVE_WHL"
sudo -u "$EFINDER_USER" "$EFINDER_DIR/venv/bin/pip" install "$OLIVE_WHL" \
  || FAIL "olive-solve tetra3-py install failed"

sudo -u "$EFINDER_USER" "$EFINDER_DIR/venv/bin/python" -c "
import tetra3
if not hasattr(tetra3.Tetra3, 'solve_from_image_fast'):
    raise RuntimeError('olive-solve wheel not active (solve_from_image_fast missing)')
print('olive-solve tetra3-py OK')
" || FAIL "olive-solve verification failed"

# --- Install sycamore-extract star_detect (optional) -------------------------
# Install only if a wheel exists in vendor/wheels/. Skip gracefully if absent.

SYCAMORE_WHL=$(ls "$VENDOR_WHEELS_DIR"/star_detect-*aarch64*.whl 2>/dev/null | head -1 || true)
if [ -n "$SYCAMORE_WHL" ]; then
  LOG "Installing sycamore-extract: $SYCAMORE_WHL"
  sudo -u "$EFINDER_USER" "$EFINDER_DIR/venv/bin/pip" install "$SYCAMORE_WHL" \
    || WARN "sycamore-extract install failed (non-fatal; olive backend will be used)"
  sudo -u "$EFINDER_USER" "$EFINDER_DIR/venv/bin/python" -c "
import star_detect
print('sycamore star_detect OK')
" 2>/dev/null && LOG "sycamore star_detect verified" || WARN "sycamore star_detect import check failed (non-fatal)"
else
  LOG "No sycamore-extract wheel in $VENDOR_WHEELS_DIR — skipping (optional)"
fi

# --- Install star database ---------------------------------------------------
# The database is generated on the x86_64 CI runner by release.yml and
# staged into the chroot by build-image.sh as EFINDER_SOLVER_DB.

mkdir -p /var/lib/efinder
chown "${EFINDER_USER}:${EFINDER_USER}" /var/lib/efinder 2>/dev/null || true
SOLVER_DB="/var/lib/efinder/default_database.npz"

if [ ! -f "$SOLVER_DB" ]; then
  if [ -n "${EFINDER_SOLVER_DB:-}" ] && [ -f "${EFINDER_SOLVER_DB}" ]; then
    LOG "Installing pre-generated star database ($(du -sh "${EFINDER_SOLVER_DB}" | cut -f1))"
    cp "${EFINDER_SOLVER_DB}" "$SOLVER_DB"
    chown "${EFINDER_USER}:${EFINDER_USER}" "$SOLVER_DB"
    LOG "Star database installed at $SOLVER_DB"
  else
    WARN "No star database provided; set solver_db in /etc/efinder/efinder.conf before first use"
  fi
fi

# --- Download solver test images (fresh install only) ------------------------
# In chroot/image-build mode these are downloaded by build-image.sh after
# the chroot exits, so they are already present in the image.

if [ "$IN_CHROOT" != "1" ]; then
  LOG "Downloading solver test images..."
  mkdir -p "$EFINDER_DIR/test-images"
  OLIVE_RAW="https://raw.githubusercontent.com/mconsidine/olive-solve/main/tetra3/tests/fixtures/sample_images"
  for img in orion_belt.jpg orion2.jpg pleiades.jpg orion_trees.jpg crappy.jpg; do
    wget -q "${OLIVE_RAW}/${img}" \
         -O "$EFINDER_DIR/test-images/${img}" \
      && LOG "  ${img}" \
      || WARN "  Could not download: ${img} (non-fatal)"
  done
  chown -R "$EFINDER_USER:$EFINDER_USER" "$EFINDER_DIR/test-images" || true
fi

# --- systemd units -----------------------------------------------------------

LOG "Installing systemd units"
install -m 644 "$EFINDER_DIR/systemd/efinder.service"             /etc/systemd/system/
install -m 644 "$EFINDER_DIR/systemd/efinder-firstboot.service"   /etc/systemd/system/
install -m 644 "$EFINDER_DIR/systemd/efinder-webui.service"       /etc/systemd/system/
install -m 644 "$EFINDER_DIR/systemd/efinder-usb-gadget.service"  /etc/systemd/system/
install -m 644 "$EFINDER_DIR/systemd/efinder-ensure-ap.service"   /etc/systemd/system/

install -m 440 "$EFINDER_DIR/etc/sudoers.d/efinder-update" /etc/sudoers.d/efinder-update
install -m 440 "$EFINDER_DIR/etc/sudoers.d/efinder-clock"  /etc/sudoers.d/efinder-clock
install -m 440 "$EFINDER_DIR/etc/sudoers.d/efinder-wifi"   /etc/sudoers.d/efinder-wifi

install -m 755 "$EFINDER_DIR/scripts/efinder-update"          /usr/local/bin/
install -m 755 "$EFINDER_DIR/scripts/efinder-ctl"             /usr/local/bin/
install -m 755 "$EFINDER_DIR/scripts/ap.sh"                   /usr/local/bin/ap.sh
install -m 755 "$EFINDER_DIR/scripts/station.sh"              /usr/local/bin/station.sh
install -m 755 "$EFINDER_DIR/scripts/efinder-gadget-connect"  /usr/local/bin/efinder-gadget-connect
install -m 755 "$EFINDER_DIR/scripts/efinder-set-time"        /usr/local/bin/efinder-set-time
chmod 755 "$EFINDER_DIR/scripts/firstboot.sh"

chown -R "$EFINDER_USER:$EFINDER_USER" /var/lib/efinder

if [ ! -f /etc/efinder/efinder.conf ]; then
  mkdir -p /etc/efinder
  install -m 644 -o "$EFINDER_USER" -g "$EFINDER_USER" \
    "$EFINDER_DIR/etc/efinder.conf.default" /etc/efinder/efinder.conf
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

LOG "Writing /etc/modules-load.d/efinder-gadget.conf"
mkdir -p /etc/modules-load.d
cat > /etc/modules-load.d/efinder-gadget.conf << 'EOF'
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
    [ -f "$CMDLINE_TXT.efinder-orig" ] \
      || cp "$CMDLINE_TXT" "$CMDLINE_TXT.efinder-orig"
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
systemctl enable efinder.service \
                 efinder-firstboot.service efinder-webui.service \
                 efinder-usb-gadget.service efinder-ensure-ap.service

if [ "$IN_CHROOT" != "1" ]; then
  LOG "Starting services"
  systemctl start efinder.service efinder-webui.service || true
fi

# --- Reboot ------------------------------------------------------------------

if [ "$IN_CHROOT" != "1" ]; then
  LOG "Install complete. Rebooting in 5s..."
  sleep 5
  reboot
else
  LOG "Install complete (chroot mode)"
fi
