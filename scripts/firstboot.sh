#!/bin/bash
# diofinder boot-time setup.  Runs on EVERY boot via diofinder-firstboot.service.
#
# All operations are idempotent — safe to repeat without side effects.
# Removing the one-time 'firstboot.done' guard means:
#   * If the AP NM profile is ever deleted or corrupted, the next boot
#     recreates it automatically.
#   * There is no fragile marker file that can silently prevent recovery.
#
# The last-run timestamp is still written to /var/lib/diofinder/firstboot.done
# for diagnostics (journalctl, support), but it is NOT read as a gate.
#
# Wi-Fi AP activation is NOT done here; diofinder-ensure-ap.service handles
# that after NM has had time to try any station connections first.

set -euo pipefail

LOG()  { echo "[diofinder-setup] $*"; }
WARN() { echo "[diofinder-setup] WARNING: $*" >&2; }

DONE_MARKER=/var/lib/diofinder/firstboot.done

LOG "Running boot-time setup"
mkdir -p /var/lib/diofinder /etc/diofinder

# Self-heal a directory-ownership bug present since v0.11.20: save_keys()
# needs to create a sibling .lock and .tmp file next to diofinder.conf,
# which requires write permission on the DIRECTORY, not just the conf file.
# install.sh historically chowned only the conf file, leaving the directory
# root:root on any device provisioned before this fix -- every settings
# persist (webui Save, :St/:Sg, alignment, calibration) failed with
# PermissionError. Runs every boot (idempotent) so already-deployed devices
# repair themselves after a `diofinder-update` + reboot, no reimage needed.
chown diofinder:diofinder /etc/diofinder 2>/dev/null || true

# --- Hardware sanity check ----------------------------------------------------

MODEL_FILE=/proc/device-tree/model
if [ -r "$MODEL_FILE" ]; then
  MODEL=$(tr -d '\0' < "$MODEL_FILE")
  LOG "Hardware: $MODEL"
  case "$MODEL" in
    *"Zero 2"*) : ;;
    *"Pi 3"*|*"Pi 4"*|*"Pi 5"*)
      WARN "Unsupported Pi model ($MODEL); some pinning assumptions may be wrong"
      ;;
    *)
      WARN "Unknown hardware ($MODEL); proceeding anyway"
      ;;
  esac
fi

# --- Camera detection ---------------------------------------------------------

RPICAM_CMD=""
if command -v rpicam-hello >/dev/null 2>&1; then
  RPICAM_CMD="rpicam-hello"
elif command -v libcamera-hello >/dev/null 2>&1; then
  RPICAM_CMD="libcamera-hello"
fi

if [ -n "$RPICAM_CMD" ]; then
  if $RPICAM_CMD --list-cameras 2>/dev/null | grep -q "Available cameras"; then
    LOG "Camera detected"
  else
    WARN "No camera detected — check CSI ribbon cable"
  fi
fi

# --- Avahi --------------------------------------------------------------------

if systemctl list-unit-files avahi-daemon.service >/dev/null 2>&1; then
  systemctl enable --now avahi-daemon.service 2>/dev/null || \
    WARN "Could not enable avahi-daemon"
fi

# --- WiFi regulatory domain + rfkill -----------------------------------------
# Pi OS Trixie soft-blocks WiFi until a country code is applied.

LOG "Unblocking WiFi radio"
iw reg set US 2>/dev/null || WARN "iw reg set US failed (non-fatal)"
rfkill unblock wifi 2>/dev/null || WARN "rfkill unblock wifi failed (non-fatal)"
nmcli radio wifi on 2>/dev/null || WARN "nmcli radio wifi on failed (non-fatal)"

# CYW43438 on Pi Zero 2W takes 3-8 s to initialise after rfkill unblock.
# Poll until NM reports wlan0 as ready (disconnected or connected) before
# creating or activating the AP profile. Without this, nmcli con up fires
# too early, fails silently, and the AP only appears on the second boot.
for _i in $(seq 1 20); do
  _state=$(nmcli -t -f DEVICE,STATE dev 2>/dev/null | awk -F: '/^wlan0:/{print $2}')
  if [ "$_state" = "disconnected" ] || [ "$_state" = "connected" ]; then
    LOG "wlan0 ready after ${_i}s (state: $_state)"
    break
  fi
  sleep 1
done
unset _i _state

# --- Wi-Fi access point profile -----------------------------------------------
# Create the AP profile if it doesn't exist. If it does, leave it alone
# (the user may have customised the SSID or password via ap.sh).

MAC=$(ip link show wlan0 2>/dev/null | awk '/ether/ {gsub(":",""); print $2; exit}')
if [ -n "${MAC:-}" ]; then
  AP_SSID="diofinder-${MAC: -4}"
else
  AP_SSID="diofinder"
  WARN "Could not read wlan0 MAC; using SSID $AP_SSID"
fi
AP_PASS="12345678"

if ! nmcli -t -f NAME con show | grep -qx "diofinder-ap"; then
  LOG "Creating Wi-Fi AP profile: SSID=$AP_SSID"
  nmcli con add \
    type wifi \
    ifname wlan0 \
    con-name diofinder-ap \
    autoconnect yes \
    ssid "$AP_SSID" \
    wifi.mode ap \
    wifi.band bg \
    ipv4.method shared \
    ipv4.addresses 1.2.3.4/24 \
    ipv6.method ignore \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$AP_PASS" \
    || WARN "Could not create AP profile"

  # 1.2.3.4/24 mirrors the Celestron SkyPortal WiFi module so SkyPortal
  # "Direct Connect" (hardcoded 1.2.3.4:2000) works when a phone joins this
  # AP directly. See docs/skyportal-aux.md.
  LOG "  SSID=$AP_SSID  password=$AP_PASS  IP=1.2.3.4"
else
  # Migrate a pre-existing profile off the old 10.42.0.1 subnet, which
  # SkyPortal Direct Connect cannot reach.
  CUR_ADDR=$(nmcli -t -f ipv4.addresses con show diofinder-ap 2>/dev/null | cut -d: -f2)
  if [ "$CUR_ADDR" != "1.2.3.4/24" ]; then
    LOG "Migrating AP address to 1.2.3.4/24 (was: ${CUR_ADDR:-unset})"
    nmcli con modify diofinder-ap \
      ipv4.method shared ipv4.addresses 1.2.3.4/24 \
      2>/dev/null || WARN "Could not migrate AP address (non-fatal)"
  else
    LOG "AP profile already exists — leaving it unchanged"
  fi
fi

# Ensure NM will always retry the AP connection. autoconnect-retries=0
# means retry indefinitely; without this NM stops trying after a few
# failures and will not retry until manually prompted, even across reboots.
nmcli con modify diofinder-ap \
  connection.autoconnect yes \
  connection.autoconnect-retries 0 \
  2>/dev/null || WARN "Could not set AP autoconnect-retries (non-fatal)"

# Explicitly activate the AP now. NM was already running when the profile
# was created so it may have missed the startup autoconnect sweep. Calling
# `nmcli con up` here avoids a 30-90 s delay waiting for NM's retry timer
# or diofinder-ensure-ap's polling loop. This is a no-op if it is already up.
if ! nmcli -t -f NAME,DEVICE con show --active 2>/dev/null \
     | awk -F: '$2=="wlan0"{exit 0} END{exit 1}'; then
  LOG "Activating AP profile on wlan0"
  nmcli -w 30 con up diofinder-ap \
    || WARN "Could not bring up AP immediately (diofinder-ensure-ap will retry)"
else
  LOG "wlan0 already has an active connection — leaving it"
fi

# --- CPU governor ---------------------------------------------------------------
# Pi OS defaults to ondemand; the frequency ramp adds latency jitter to solve
# times. Pin to performance — the Zero 2W draws little extra at idle and the
# finder workload is bursty-periodic anyway. Idempotent; runs every boot.

LOG "Setting CPU governor to performance"
for _gov in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
  [ -w "$_gov" ] && echo performance > "$_gov" 2>/dev/null \
    || WARN "Could not set governor on $_gov (non-fatal)"
done
unset _gov

# --- Filesystem setup ---------------------------------------------------------

mkdir -p /var/lib/diofinder/captures
chown -R diofinder:diofinder /var/lib/diofinder 2>/dev/null || true

# --- I2C clock speed ----------------------------------------------------------
# The BCM2835/BCM2711 I2C master has a hardware bug: it releases SCL before a
# slave finishes clock-stretching, causing bit 7 to be stuck high on some reads.
# The BNO055 IMU is a heavy clock-stretcher. Dropping to 50kHz eliminates the
# need for the sensor to stretch the clock at all.
# config.txt changes take effect on next reboot -- that is fine because this
# runs on every boot and the guard prevents duplicate appends.
CONFIG_TXT=/boot/firmware/config.txt
if [ -f "$CONFIG_TXT" ] && ! grep -qF "i2c_arm_baudrate" "$CONFIG_TXT"; then
  echo "dtparam=i2c_arm_baudrate=50000" >> "$CONFIG_TXT"
  LOG "Set I2C bus to 50kHz to fix BNO055 clock-stretching (reboot required)"
fi

# --- Regenerate tetra3rs database if missing (upgrade/recovery path) ---------
# Normally baked into the image by install.sh. This fallback runs if the file
# was somehow lost (e.g. manual deletion, failed image build).
TETRA3RS_DB=/var/lib/diofinder/diofinder-tetra-database.bin
if [ ! -f "$TETRA3RS_DB" ] && [ -x /opt/diofinder/venv/bin/python ]; then
  if /opt/diofinder/venv/bin/python -c "import tetra3rs" 2>/dev/null; then
    LOG "tetra3rs database missing — regenerating..."
    if /opt/diofinder/venv/bin/python -c "
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
    db.save_to_file('/var/lib/diofinder/diofinder-tetra-database.bin')
    print('tetra3rs database: stars=%d patterns=%d' % (db.num_stars, db.num_patterns))
except Exception as e:
    print('tetra3rs db generation failed: %s' % e, file=sys.stderr)
    sys.exit(1)
"; then
      chown diofinder:diofinder "$TETRA3RS_DB" 2>/dev/null || true
      LOG "tetra3rs database regenerated"
    else
      WARN "tetra3rs database generation failed; tetra backend unavailable"
    fi
  fi
fi

# --- Record last-run time (diagnostic only — NOT read as a gate) --------------

date -u +"%Y-%m-%dT%H:%M:%SZ" > "$DONE_MARKER"
LOG "Boot setup complete"
