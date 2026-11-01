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
# Wi-Fi: which network wlan0 joins at boot is the user's wifi_boot_network
# setting, applied by scripts/diofinder-wifi-boot (called below). It only
# switches the connection when it must; diofinder-ensure-ap.service is the
# fallback that starts the AP if nothing has connected ~30 s later.

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

# (The camera check runs after the Wi-Fi steps below: it takes ~4 s and
# nothing about Wi-Fi depends on it.)

# --- Avahi --------------------------------------------------------------------
# Only (re-)enable when needed: `systemctl enable` on a unit that also has a
# SysV init script goes through systemd-sysv-install/update-rc.d, measured at
# ~5 s on a Zero 2W, and this runs every boot. Idempotent either way.

if systemctl list-unit-files avahi-daemon.service >/dev/null 2>&1; then
  systemctl is-enabled --quiet avahi-daemon.service 2>/dev/null \
    || systemctl enable avahi-daemon.service 2>/dev/null \
    || WARN "Could not enable avahi-daemon"
  systemctl is-active --quiet avahi-daemon.service 2>/dev/null \
    || systemctl start avahi-daemon.service 2>/dev/null \
    || WARN "Could not start avahi-daemon"
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
    wifi.channel 6 \
    ipv4.method shared \
    ipv4.addresses 10.42.0.1/24 \
    ipv6.method ignore \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$AP_PASS" \
    wifi-sec.proto rsn \
    wifi-sec.pairwise ccmp \
    wifi-sec.group ccmp \
    wifi-sec.pmf 1 \
    || WARN "Could not create AP profile"

  LOG "  SSID=$AP_SSID  password=$AP_PASS  IP=10.42.0.1"
else
  LOG "AP profile already exists — hardening its security (idempotent)"
fi

# Force WPA2-AES (RSN/CCMP) only, no WPA1/TKIP, PMF disabled. NetworkManager's
# default for an unspecified wpa-psk AP is mixed WPA/WPA2 with a TKIP group
# cipher, which modern Windows (11 / recent 10) deprecates and refuses — so the
# AP "works on Android/iPhone but not a Windows laptop". Applied on every boot
# setup so existing profiles are migrated too; pmf 1 = disable (some Windows
# drivers dislike 802.11w on a personal AP). Idempotent.
nmcli con modify diofinder-ap \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.proto rsn \
  wifi-sec.pairwise ccmp \
  wifi-sec.group ccmp \
  wifi-sec.pmf 1 \
  2>/dev/null || WARN "Could not harden AP security (non-fatal)"

# Pin the AP to the 2.4 GHz band and a fixed channel. Without an explicit
# channel, NetworkManager's AP mode can auto-select channel 12/13 — disallowed
# in the US/CA regulatory domain and rejected by many Windows/US clients, so the
# AP appears not to come up at all. Channel 6 is a safe default (1 or 11 also
# work). Applied every boot so existing profiles are migrated too; idempotent.
nmcli con modify diofinder-ap \
  wifi.band bg \
  wifi.channel 6 \
  2>/dev/null || WARN "Could not pin AP band/channel (non-fatal)"

# Boot network: apply the user's wifi_boot_network setting to the profiles
# (autoconnect, priority, unlimited retries) and make sure wlan0 is on the
# right network -- without tearing down a connection that is already right.
# All of that lives in diofinder-wifi-boot (shared with ap.sh/station.sh and
# the Wi-Fi page).
#
# This replaces two things that fought the user's choice on every boot:
#   * forcing the AP's autoconnect back to "yes", which undid joining a
#     network from the Wi-Fi page; and
#   * an "is wlan0 already connected?" check that never worked (in awk,
#     `exit 0` in a rule still runs END, whose `exit 1` wins), so the AP was
#     restarted every boot -- tearing down whatever NM had already brought
#     up ~10 s earlier, and dropping any phone that had started to join.
LOG "Applying the boot network setting"
bash "$(dirname "$(readlink -f "$0")")/diofinder-wifi-boot" boot \
  || WARN "diofinder-wifi-boot failed (diofinder-ensure-ap will still fall back to the AP)"

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

# --- Raspberry Pi OS first-user prompt ----------------------------------------
# userconfig.service waits on the console to create a first user. This image
# creates its own users, so here it only sits blocked forever, which keeps
# systemd's "boot finished" open (systemd-analyze then refuses to report).
# Disable it (takes effect next boot; not stopped mid-run). Idempotent.
if systemctl is-enabled --quiet userconfig.service 2>/dev/null; then
  systemctl disable userconfig.service 2>/dev/null \
    && LOG "Disabled userconfig.service (unused first-user console prompt)" \
    || WARN "Could not disable userconfig.service (non-fatal)"
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
