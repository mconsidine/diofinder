#!/bin/bash
# eFinder install script.
#
# Two execution modes, autodetected:
#
#   * "fresh"  : run by a user on a freshly flashed Trixie Lite SD card.
#                Does the works: apt update, raspi-config, /boot edits,
#                creates user, then reboots.
#
#   * "chroot" : run inside qemu-aarch64 chroot during image build.
#                Skips raspi-config and reboot. Source tree is staged
#                at /tmp/efinder-src/ by build-image.sh. EFINDER_CHROOT=1.
#
# In chroot mode, an optional EFINDER_CEDAR_DETECT_BIN_LOCAL points to
# a pre-built cedar-detect-server binary; if set we skip the release-URL
# download entirely (used during CI where the release isn't populated
# until both jobs finish).
#
# Idempotent: re-running on a working install should be a no-op except
# for re-pulling the cedar-detect binary if a newer release is found.

set -euo pipefail

# --- Config -------------------------------------------------------------------
EFINDER_USER="efinder"
EFINDER_HOME="/home/${EFINDER_USER}"
EFINDER_DIR="/opt/efinder"
REPO_URL="https://github.com/mconsidine/eFinder_cli.git"
CEDAR_DETECT_REPO="mconsidine/eFinder_cli"
CEDAR_DETECT_BIN="cedar-detect-server"
TARGET_VERSION="${EFINDER_VERSION:-latest}"
IN_CHROOT="${EFINDER_CHROOT:-0}"
LOCAL_CD_BIN="${EFINDER_CEDAR_DETECT_BIN_LOCAL:-}"
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
    || FAIL "$SRC_STAGED looks incomplete (missing scripts/install.sh)"
  [ -f "$SRC_STAGED/proto/cedar_detect.proto" ] \
    || FAIL "vendored proto missing at $SRC_STAGED/proto/cedar_detect.proto"
else
  LOG "Running in fresh-install mode version=$TARGET_VERSION"
fi

# --- Create efinder user ------------------------------------------------------

if ! id -u "$EFINDER_USER" >/dev/null 2>&1; then
  LOG "Creating user $EFINDER_USER"
  useradd -m -s /bin/bash "$EFINDER_USER"
  # Password '12345678'. Per project decision: no security risk for this
  # device (always operates on private networks; user is the device owner).
  echo "${EFINDER_USER}:12345678" | chpasswd
  # Groups:
  #   video/gpio/i2c/dialout  hardware access (camera, GPIO, etc.)
  #   sudo                    so ap.sh / station.sh / efinder-update work
  #   netdev                  so nmcli works without sudo for some ops
  usermod -aG video,gpio,i2c,dialout,sudo,netdev "$EFINDER_USER" || true
fi

# --- System packages ----------------------------------------------------------

LOG "Updating apt and installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip python3-dev \
  python3-numpy python3-scipy python3-pil \
  python3-picamera2 python3-libcamera \
  python3-flask \
  build-essential pkg-config \
  curl ca-certificates git \
  protobuf-compiler \
  avahi-daemon \
  openssh-server \
  network-manager

# SSH on by default. Pi OS Lite has had this off-by-default in some
# recent images; ensure it's enabled so the user can ssh in immediately.
systemctl enable ssh.service 2>/dev/null || systemctl enable ssh.socket || true

# --- Application code ---------------------------------------------------------

if [ "$IN_CHROOT" = "1" ]; then
  # Image-build path: copy staged source into /opt/efinder.
  if [ ! -d "$EFINDER_DIR" ]; then
    LOG "Copying staged source $SRC_STAGED -> $EFINDER_DIR"
    mkdir -p "$EFINDER_DIR"
    # Copy everything except the staged binary (handled separately below).
    cp -r "$SRC_STAGED/." "$EFINDER_DIR/"
    rm -f "$EFINDER_DIR/cedar-detect-server.aarch64"
    chown -R "$EFINDER_USER:$EFINDER_USER" "$EFINDER_DIR"
  else
    WARN "$EFINDER_DIR already exists; reusing"
  fi
else
  # Fresh-install path: clone from GitHub.
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

# --- Python venv --------------------------------------------------------------

if [ ! -d "$EFINDER_DIR/venv" ]; then
  LOG "Creating Python venv (with system site packages for picamera2)"
  sudo -u "$EFINDER_USER" python3 -m venv \
    --system-site-packages "$EFINDER_DIR/venv"
fi

LOG "Installing Python deps"
sudo -u "$EFINDER_USER" "$EFINDER_DIR/venv/bin/pip" install --upgrade pip \
  || FAIL "pip install --upgrade pip failed"

# Python 3.13's venv doesn't include setuptools/wheel by default
# (distutils removal aftermath). --no-build-isolation needs them
# present in the venv so PEP 517 build backends can find them.
sudo -u "$EFINDER_USER" "$EFINDER_DIR/venv/bin/pip" install --upgrade \
  setuptools wheel \
  || FAIL "pip install setuptools wheel failed"

# --- Step 1: install non-cedar-solve Python deps via pip wheels.
# These resolve to aarch64 wheels on PyPI and don't need anything special.
# (Listed explicitly here rather than via requirements.txt so this script
# is self-contained for image-build context.)
LOG "Installing wheel-based Python deps"
sudo -u "$EFINDER_USER" "$EFINDER_DIR/venv/bin/pip" install \
  grpcio grpcio-tools protobuf \
  || FAIL "pip install of grpcio/grpcio-tools/protobuf failed"

# --- Step 2: install cedar-solve from git with --no-deps
# Cedar-solve's pyproject.toml pins numpy<2, Pillow<9, scipy<2 (defensive
# caps from June 2024 when 0.5.1 was tagged). Trixie ships numpy 2.x
# and newer Pillow/scipy. The pins aren't required; cedar-solve works
# fine with current versions. We bypass the resolver:
#
#   --no-deps             skip the version-conflict check entirely
#   --no-build-isolation  build using the apt-provided numpy from
#                         --system-site-packages, rather than pulling
#                         a fresh numpy<2 source tarball
#
# numpy/scipy/Pillow come from apt (python3-numpy, python3-scipy,
# python3-pil) installed earlier.
CEDAR_SOLVE_REF="${EFINDER_CEDAR_SOLVE_REF:-v0.6.0}"
LOG "Installing cedar-solve from git@${CEDAR_SOLVE_REF} (no-deps, no-build-isolation)"
sudo -u "$EFINDER_USER" "$EFINDER_DIR/venv/bin/pip" install \
  --no-deps \
  --no-build-isolation \
  "git+https://github.com/smroid/cedar-solve.git@${CEDAR_SOLVE_REF}#egg=cedar-solve" \
  || FAIL "pip install of cedar-solve failed"

# Sanity check: tetra3 module must be importable, and runtime deps must
# be present (we get them from apt, but verify in case of skew).
sudo -u "$EFINDER_USER" "$EFINDER_DIR/venv/bin/python" -c "
import sys
mods_required = ['numpy', 'scipy', 'PIL', 'tetra3']
missing = []
for m in mods_required:
    try:
        __import__(m)
    except ImportError as e:
        missing.append(f'{m}: {e}')
if missing:
    print('Missing runtime deps:', missing, file=sys.stderr)
    sys.exit(1)
print('All cedar-solve runtime deps importable')
" || FAIL "cedar-solve runtime dependency check failed"

# --- Install cedar-detect-server -----------------------------------

if [ -n "$LOCAL_CD_BIN" ]; then
  # Image build with locally-staged binary -- no network fetch.
  [ -f "$LOCAL_CD_BIN" ] || FAIL "EFINDER_CEDAR_DETECT_BIN_LOCAL=$LOCAL_CD_BIN not found"
  LOG "Installing locally-staged $CEDAR_DETECT_BIN"
  install -m 755 "$LOCAL_CD_BIN" /usr/local/bin/${CEDAR_DETECT_BIN}
else
  LOG "Fetching $CEDAR_DETECT_BIN binary"
  if [ "$TARGET_VERSION" = "latest" ]; then
    URL=$(curl -fsSL "https://api.github.com/repos/${CEDAR_DETECT_REPO}/releases/latest" \
          | grep "browser_download_url" \
          | grep "${CEDAR_DETECT_BIN}" \
          | head -n1 \
          | cut -d'"' -f4 || true)
    [ -n "$URL" ] || FAIL "Could not resolve latest $CEDAR_DETECT_BIN URL from $CEDAR_DETECT_REPO"
  else
    URL="https://github.com/${CEDAR_DETECT_REPO}/releases/download/${TARGET_VERSION}/${CEDAR_DETECT_BIN}"
  fi
  LOG "Downloading from $URL"
  TMP=$(mktemp); trap 'rm -f "$TMP"' EXIT
  curl -fsSL --retry 3 "$URL" -o "$TMP" \
    || FAIL "Failed to download $CEDAR_DETECT_BIN from $URL"
  install -m 755 "$TMP" /usr/local/bin/${CEDAR_DETECT_BIN}
  trap - EXIT
fi

# --- Generate Python protobuf stubs ------------------------------------------

LOG "Generating Python gRPC stubs from vendored cedar_detect.proto"
[ -f "$EFINDER_DIR/proto/cedar_detect.proto" ] \
  || FAIL "missing $EFINDER_DIR/proto/cedar_detect.proto"
# grpcio + grpcio-tools were installed by requirements.txt above
sudo -u "$EFINDER_USER" "$EFINDER_DIR/venv/bin/python" \
  -m grpc_tools.protoc \
  -I "$EFINDER_DIR/proto" \
  --python_out="$EFINDER_DIR/proto" \
  --grpc_python_out="$EFINDER_DIR/proto" \
  "$EFINDER_DIR/proto/cedar_detect.proto" \
  || FAIL "protoc compile failed"

# Verify the stubs were actually produced -- protoc sometimes silently
# does nothing if invoked wrong.
[ -f "$EFINDER_DIR/proto/cedar_detect_pb2.py" ] \
  || FAIL "cedar_detect_pb2.py not produced; check protoc invocation"
[ -f "$EFINDER_DIR/proto/cedar_detect_pb2_grpc.py" ] \
  || FAIL "cedar_detect_pb2_grpc.py not produced"

# --- systemd units ------------------------------------------------------------

LOG "Installing systemd units"
install -m 644 "$EFINDER_DIR/systemd/cedar-detect.service"      /etc/systemd/system/
install -m 644 "$EFINDER_DIR/systemd/efinder.service"           /etc/systemd/system/
install -m 644 "$EFINDER_DIR/systemd/efinder-firstboot.service" /etc/systemd/system/
install -m 644 "$EFINDER_DIR/systemd/efinder-webui.service"     /etc/systemd/system/

# Sudoers rule scoped to just efinder-update (used by the web UI)
install -m 440 "$EFINDER_DIR/etc/sudoers.d/efinder-update" /etc/sudoers.d/efinder-update

install -m 755 "$EFINDER_DIR/scripts/efinder-update" /usr/local/bin/
install -m 755 "$EFINDER_DIR/scripts/efinder-ctl"    /usr/local/bin/
install -m 755 "$EFINDER_DIR/scripts/ap.sh"          /usr/local/bin/ap.sh
install -m 755 "$EFINDER_DIR/scripts/station.sh"     /usr/local/bin/station.sh
# firstboot.sh runs in place from /opt/efinder/scripts/ per the systemd
# unit (no copy needed); just ensure it's executable.
chmod 755 "$EFINDER_DIR/scripts/firstboot.sh"

mkdir -p /etc/efinder /var/lib/efinder
chown -R "$EFINDER_USER:$EFINDER_USER" /var/lib/efinder

if [ ! -f /etc/efinder/efinder.conf ]; then
  install -m 644 -o "$EFINDER_USER" -g "$EFINDER_USER" \
    "$EFINDER_DIR/etc/efinder.conf.default" /etc/efinder/efinder.conf
fi

# --- Web UI -----------------------------------------------------------------
# The Flask app at /opt/efinder/webui is started by efinder-webui.service
# and reads its templates/static assets in place. Nothing more to do.

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
  # USB serial gadget mode -- the Pi appears as /dev/ttyACM0 (Linux/Mac)
  # or a COM port (Windows) when plugged into the host via the Pi's USB
  # data port (the middle micro-USB on the Zero 2W, NOT the leftmost PWR
  # port). A getty on ttyGS0 (enabled below) provides a login prompt.
  # Use: screen /dev/ttyACM0 115200
  # Whole-line match (-x) is intentional: the base Pi OS Trixie image
  # ships 'dtoverlay=dwc2,dr_mode=host' in [cm5] for CM5 host mode.
  # A substring or prefix match would falsely skip adding the [all]
  # peripheral-mode entry that the Pi Zero / Zero 2W actually needs.
  # dr_mode=peripheral is explicit and required. Without it, the dwc2
  # driver on newer kernels defaults to OTG mode and waits to detect
  # host vs device rather than committing to peripheral (gadget) mode.
  if ! grep -qxF "dtoverlay=dwc2,dr_mode=peripheral" "$CONFIG_TXT"; then
    LOG "Enabling USB gadget mode (dwc2,dr_mode=peripheral) in $CONFIG_TXT"
    printf "\n[all]\n# USB serial gadget -- peripheral mode for Pi Zero / Zero 2W\ndtoverlay=dwc2,dr_mode=peripheral\n" >> "$CONFIG_TXT"
  fi
fi

# Belt-and-suspenders: also write a modules-load.d file. The cmdline.txt
# modules-load= parameter is processed by systemd-modules-load, but a
# dedicated conf file is more reliable across Pi OS variants.
LOG "Writing /etc/modules-load.d/efinder-gadget.conf"
mkdir -p /etc/modules-load.d
cat > /etc/modules-load.d/efinder-gadget.conf << 'EOF'
# eFinder USB serial gadget
# dwc2: DWC2 USB OTG controller driver
# g_cdc_acm: USB CDC ACM serial gadget (/dev/ttyACM0 on the host PC)
dwc2
g_cdc_acm
EOF

# cmdline.txt is one long single line; we splice into it rather than
# append. The kernel parses each space-separated token as a parameter.
# console=ttyGS0,115200 routes kernel boot messages to the USB serial
# port so the full boot sequence is visible in screen.
if [ -f "$CMDLINE_TXT" ]; then
  if ! grep -q "modules-load=.*\bdwc2\b" "$CMDLINE_TXT"; then
    LOG "Adding dwc2,g_cdc_acm modules-load to $CMDLINE_TXT"
    [ -f "$CMDLINE_TXT.efinder-orig" ] || cp "$CMDLINE_TXT" "$CMDLINE_TXT.efinder-orig"
    if grep -q "rootwait" "$CMDLINE_TXT"; then
      sed -i 's/rootwait/rootwait modules-load=dwc2,g_cdc_acm console=ttyGS0,115200/' "$CMDLINE_TXT"
    else
      sed -i '1s|^|modules-load=dwc2,g_cdc_acm console=ttyGS0,115200 |' "$CMDLINE_TXT"
    fi
  fi
fi

# --- USB serial console (g_cdc_acm gadget) -----------------------------------
# g_cdc_acm creates /dev/ttyGS0 on the Pi. Enable a getty on it so the
# user gets a login prompt when they connect with screen /dev/ttyACM0 115200.

LOG "Enabling USB serial console (serial-getty@ttyGS0)"
systemctl enable serial-getty@ttyGS0.service 2>/dev/null || \
  WARN "Could not enable serial-getty@ttyGS0"

# --- Enable services ---------------------------------------------------------

LOG "Enabling services"
systemctl daemon-reload
systemctl enable cedar-detect.service efinder.service \
                 efinder-firstboot.service efinder-webui.service

if [ "$IN_CHROOT" != "1" ]; then
  LOG "Starting services"
  systemctl start cedar-detect.service efinder.service \
                  efinder-webui.service || true
fi

# --- Reboot if running on real hardware --------------------------------------

if [ "$IN_CHROOT" != "1" ]; then
  LOG "Install complete. Rebooting in 5s..."
  sleep 5
  reboot
else
  LOG "Install complete (chroot mode; image will boot on first power-up)"
fi
