#!/bin/bash
# eFinder install script — combo branch.
#
# Installs both the cedar backend (gRPC + tetra3 Python) and the tetra3rs
# backend (Rust wheel), so either can be selected at runtime via the web UI.
#
# Two execution modes, autodetected:
#
#   * "fresh"  : run by a user on a freshly flashed Trixie Lite SD card.
#   * "chroot" : run inside qemu-aarch64 chroot during image build.
#                Source staged at /tmp/efinder-src/. EFINDER_CHROOT=1.
#
# Chroot-mode optional env:
#   EFINDER_CEDAR_DETECT_BIN_LOCAL  path to pre-built cedar-detect-server
#                                   (falls back to vendor/bin/ then release download)
#   EFINDER_TETRA3RS_WHEELS_DIR     dir of pre-built tetra3rs aarch64 wheels
#                                   (falls back to vendor/wheels/ then PyPI)

set -euo pipefail

# --- Config ------------------------------------------------------------------
EFINDER_USER="efinder"
EFINDER_HOME="/home/${EFINDER_USER}"
EFINDER_DIR="/opt/efinder"
REPO_URL="https://github.com/mconsidine/eFinder_cli_new.git"
CEDAR_DETECT_REPO="mconsidine/eFinder_cli_new"
CEDAR_DETECT_BIN="cedar-detect-server"
TARGET_VERSION="${EFINDER_VERSION:-latest}"
IN_CHROOT="${EFINDER_CHROOT:-0}"
LOCAL_CD_BIN="${EFINDER_CEDAR_DETECT_BIN_LOCAL:-}"
LOCAL_WHEELS_DIR="${EFINDER_TETRA3RS_WHEELS_DIR:-}"
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
  [ -f "$SRC_STAGED/proto/cedar_detect.proto" ] \
    || FAIL "vendored proto missing"
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
  protobuf-compiler \
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
else
  if [ ! -d "$EFINDER_DIR/.git" ]; then
    LOG "Cloning eFinder code to $EFINDER_DIR"
    git clone --depth 1 "$REPO_URL" "$EFINDER_DIR"
    chown -R "$EFINDER_USER:$EFINDER_USER" "$EFINDER_DIR"
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

LOG "Installing gRPC / protobuf (cedar backend)"
sudo -u "$EFINDER_USER" "$EFINDER_DIR/venv/bin/pip" install \
  grpcio grpcio-tools protobuf \
  || FAIL "pip install grpcio/grpcio-tools/protobuf failed"

CEDAR_SOLVE_REF="${EFINDER_CEDAR_SOLVE_REF:-v0.6.0}"
LOG "Installing cedar-solve from git@${CEDAR_SOLVE_REF}"
sudo -u "$EFINDER_USER" "$EFINDER_DIR/venv/bin/pip" install \
  --no-deps \
  --no-build-isolation \
  "git+https://github.com/smroid/cedar-solve.git@${CEDAR_SOLVE_REF}#egg=cedar-solve" \
  || FAIL "cedar-solve install failed"

sudo -u "$EFINDER_USER" "$EFINDER_DIR/venv/bin/python" -c "
import sys
mods = ['numpy', 'scipy', 'PIL', 'tetra3']
missing = []
for m in mods:
    try: __import__(m)
    except ImportError as e: missing.append(f'{m}: {e}')
if missing:
    print('Missing deps:', missing, file=sys.stderr); sys.exit(1)
print('cedar-solve runtime deps OK')
" || FAIL "cedar-solve runtime dependency check failed"

# --- Install tetra3rs --------------------------------------------------------
# Priority: vendor/wheels/ in repo > EFINDER_TETRA3RS_WHEELS_DIR env > PyPI.
# In all cases use --find-links + --no-index so pip selects the wheel that
# matches the running Python version (cp311/cp312/cp313) rather than us
# hard-coding a filename.

VENDOR_WHEELS_DIR="$EFINDER_DIR/vendor/wheels"
HAS_VENDOR_WHEEL=$(ls "$VENDOR_WHEELS_DIR/tetra3rs-"*.whl 2>/dev/null | head -1 || true)

if [ -n "$HAS_VENDOR_WHEEL" ]; then
  LOG "Installing tetra3rs from vendored wheels in $VENDOR_WHEELS_DIR"
  sudo -u "$EFINDER_USER" "$EFINDER_DIR/venv/bin/pip" install "gaia-catalog<1.0" \
    || FAIL "gaia-catalog install failed"
  sudo -u "$EFINDER_USER" "$EFINDER_DIR/venv/bin/pip" install \
    --find-links "$VENDOR_WHEELS_DIR" --no-index tetra3rs \
    || FAIL "tetra3rs vendored wheel install failed"
elif [ -n "$LOCAL_WHEELS_DIR" ]; then
  LOG "Installing tetra3rs from local wheels in $LOCAL_WHEELS_DIR"
  sudo -u "$EFINDER_USER" "$EFINDER_DIR/venv/bin/pip" install "gaia-catalog<1.0" \
    || FAIL "gaia-catalog install failed"
  sudo -u "$EFINDER_USER" "$EFINDER_DIR/venv/bin/pip" install \
    --find-links "$LOCAL_WHEELS_DIR" --no-index tetra3rs \
    || FAIL "tetra3rs wheel install failed"
else
  LOG "Installing tetra3rs from PyPI (non-fatal if unavailable)"
  sudo -u "$EFINDER_USER" "$EFINDER_DIR/venv/bin/pip" install tetra3rs \
    || WARN "tetra3rs install failed; tetra backend will be unavailable"
fi

# --- Patch tetra3rs dist-info name mismatch ----------------------------------
# Upstream bug: tetra3rs/__init__.py calls version("tetra3rs") but pip installs
# the dist-info directory as tetra3_python-*.dist-info, causing
# PackageNotFoundError at import time. Patch the __init__.py in-place.
LOG "Checking tetra3rs dist-info name patch"
SITE_PACKAGES=$("$EFINDER_DIR/venv/bin/python" -c \
  "import sysconfig; print(sysconfig.get_paths()['purelib'])" 2>/dev/null || true)
if [ -n "$SITE_PACKAGES" ] && [ -f "$SITE_PACKAGES/tetra3rs/__init__.py" ]; then
  if grep -q 'version("tetra3rs")' "$SITE_PACKAGES/tetra3rs/__init__.py"; then
    LOG "  Patching $SITE_PACKAGES/tetra3rs/__init__.py"
    sed -i 's/version("tetra3rs")/version("tetra3_python")/' \
      "$SITE_PACKAGES/tetra3rs/__init__.py"
  else
    LOG "  tetra3rs dist-info patch not needed (already correct or not found)"
  fi
fi

sudo -u "$EFINDER_USER" "$EFINDER_DIR/venv/bin/python" -c "
try:
    import tetra3rs
    print('tetra3rs importable:', getattr(tetra3rs, '__version__', 'unknown'))
except ImportError as e:
    print('tetra3rs not available:', e, '-- cedar backend will be used')
" || true

# --- Generate tetra3rs binary database ---------------------------------------
# tetra3rs uses its own binary format, incompatible with Python tetra3's .npz.
# generate_from_gaia() uses the bundled gaia-catalog package (no download).
# Baked into the image here so the device is ready on first boot.
mkdir -p /var/lib/efinder
TETRA3RS_DB="/var/lib/efinder/efinder-tetra-database.bin"
if [ ! -f "$TETRA3RS_DB" ]; then
  if "$EFINDER_DIR/venv/bin/python" -c "import tetra3rs" 2>/dev/null; then
    LOG "Generating tetra3rs binary database..."
    if "$EFINDER_DIR/venv/bin/python" -c "
import sys
try:
    import tetra3rs
    db = tetra3rs.SolverDatabase.generate_from_gaia(
        max_fov_deg=14.0,
        star_max_magnitude=8.0,
        patterns_per_lattice_field=50,
        epoch_proper_motion_year=2026,
        verification_stars_per_fov=100,
    )
    db.save_to_file('/var/lib/efinder/efinder-tetra-database.bin')
    print('tetra3rs database: stars=%d patterns=%d' % (db.num_stars, db.num_patterns))
except Exception as e:
    print('ERROR: tetra3rs db generation failed: %s' % e, file=sys.stderr)
    sys.exit(1)
"; then
      chown "${EFINDER_USER}:${EFINDER_USER}" "$TETRA3RS_DB" 2>/dev/null || true
      LOG "tetra3rs database baked into image"
    else
      WARN "tetra3rs database generation failed; tetra backend unavailable"
    fi
  else
    WARN "tetra3rs not importable; skipping database generation"
  fi
fi

# --- Install cedar-detect-server ---------------------------------------------
# Priority: vendored binary in repo > EFINDER_CEDAR_DETECT_BIN_LOCAL env >
#           download from GitHub release.

VENDOR_CD="$EFINDER_DIR/vendor/bin/cedar-detect-server"
if [ -f "$VENDOR_CD" ]; then
  LOG "Using vendored $CEDAR_DETECT_BIN"
  install -m 755 "$VENDOR_CD" /usr/local/bin/${CEDAR_DETECT_BIN}
elif [ -n "$LOCAL_CD_BIN" ]; then
  [ -f "$LOCAL_CD_BIN" ] || FAIL "EFINDER_CEDAR_DETECT_BIN_LOCAL=$LOCAL_CD_BIN not found"
  LOG "Installing locally-staged $CEDAR_DETECT_BIN"
  install -m 755 "$LOCAL_CD_BIN" /usr/local/bin/${CEDAR_DETECT_BIN}
else
  LOG "Fetching $CEDAR_DETECT_BIN binary from GitHub release"
  if [ "$TARGET_VERSION" = "latest" ]; then
    URL=$(curl -fsSL \
      "https://api.github.com/repos/${CEDAR_DETECT_REPO}/releases/latest" \
          | grep "browser_download_url" \
          | grep "${CEDAR_DETECT_BIN}" \
          | head -n1 \
          | cut -d'"' -f4 || true)
    [ -n "$URL" ] || FAIL "Could not resolve latest $CEDAR_DETECT_BIN URL"
  else
    URL="https://github.com/${CEDAR_DETECT_REPO}/releases/download/${TARGET_VERSION}/${CEDAR_DETECT_BIN}"
  fi
  LOG "Downloading from $URL"
  TMP=$(mktemp); trap 'rm -f "$TMP"' EXIT
  curl -fsSL --retry 3 "$URL" -o "$TMP" \
    || FAIL "Failed to download $CEDAR_DETECT_BIN"
  install -m 755 "$TMP" /usr/local/bin/${CEDAR_DETECT_BIN}
  trap - EXIT
fi

# --- Generate Python protobuf stubs ------------------------------------------

LOG "Generating Python gRPC stubs from vendored cedar_detect.proto"
[ -f "$EFINDER_DIR/proto/cedar_detect.proto" ] \
  || FAIL "missing $EFINDER_DIR/proto/cedar_detect.proto"
sudo -u "$EFINDER_USER" "$EFINDER_DIR/venv/bin/python" \
  -m grpc_tools.protoc \
  -I "$EFINDER_DIR/proto" \
  --python_out="$EFINDER_DIR/proto" \
  --grpc_python_out="$EFINDER_DIR/proto" \
  "$EFINDER_DIR/proto/cedar_detect.proto" \
  || FAIL "protoc compile failed"
[ -f "$EFINDER_DIR/proto/cedar_detect_pb2.py" ] \
  || FAIL "cedar_detect_pb2.py not produced"
[ -f "$EFINDER_DIR/proto/cedar_detect_pb2_grpc.py" ] \
  || FAIL "cedar_detect_pb2_grpc.py not produced"

# --- Download solver test images (fresh install only) ------------------------
# In chroot/image-build mode these are downloaded by build-image.sh after
# the chroot exits, so they are already present in the image.

if [ "$IN_CHROOT" != "1" ]; then
  LOG "Downloading solver test images..."
  mkdir -p "$EFINDER_DIR/tests/test-images"
  OLIVE_RAW="https://raw.githubusercontent.com/mconsidine/olive-solve/main/tetra3/tests/fixtures/sample_images"
  for img in orion_belt.jpg orion2.jpg pleiades.jpg orion_trees.jpg crappy.jpg; do
    wget -q "${OLIVE_RAW}/${img}" \
         -O "$EFINDER_DIR/tests/test-images/${img}" \
      && LOG "  ${img}" \
      || WARN "  Could not download: ${img} (non-fatal)"
  done
  chown -R "$EFINDER_USER:$EFINDER_USER" "$EFINDER_DIR/tests/test-images" || true
fi

# --- systemd units -----------------------------------------------------------

LOG "Installing systemd units"
install -m 644 "$EFINDER_DIR/systemd/cedar-detect.service"        /etc/systemd/system/
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

mkdir -p /etc/efinder /var/lib/efinder
chown -R "$EFINDER_USER:$EFINDER_USER" /var/lib/efinder

if [ ! -f /etc/efinder/efinder.conf ]; then
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
systemctl enable cedar-detect.service efinder.service \
                 efinder-firstboot.service efinder-webui.service \
                 efinder-usb-gadget.service efinder-ensure-ap.service

if [ "$IN_CHROOT" != "1" ]; then
  LOG "Starting services"
  systemctl start cedar-detect.service efinder.service \
                  efinder-webui.service || true
fi

# --- Reboot ------------------------------------------------------------------

if [ "$IN_CHROOT" != "1" ]; then
  LOG "Install complete. Rebooting in 5s..."
  sleep 5
  reboot
else
  LOG "Install complete (chroot mode)"
fi
