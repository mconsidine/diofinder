#!/bin/bash
# eFinder boot-time setup.  Runs on EVERY boot via efinder-firstboot.service.
#
# All operations are idempotent — safe to repeat without side effects.
# Removing the one-time 'firstboot.done' guard means:
#   * If the AP NM profile is ever deleted or corrupted, the next boot
#     recreates it automatically.
#   * There is no fragile marker file that can silently prevent recovery.
#
# The last-run timestamp is still written to /var/lib/efinder/firstboot.done
# for diagnostics (journalctl, support), but it is NOT read as a gate.
#
# Wi-Fi AP activation is NOT done here; efinder-ensure-ap.service handles
# that after NM has had time to try any station connections first.

set -euo pipefail

LOG()  { echo "[efinder-setup] $*"; }
WARN() { echo "[efinder-setup] WARNING: $*" >&2; }

DONE_MARKER=/var/lib/efinder/firstboot.done

LOG "Running boot-time setup"
mkdir -p /var/lib/efinder /etc/efinder

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
sleep 1

# --- Wi-Fi access point profile -----------------------------------------------
# Create the AP profile if it doesn't exist. If it does, leave it alone
# (the user may have customised the SSID or password via ap.sh).

MAC=$(ip link show wlan0 2>/dev/null | awk '/ether/ {gsub(":",""); print $2; exit}')
if [ -n "${MAC:-}" ]; then
  AP_SSID="efinder-${MAC: -4}"
else
  AP_SSID="efinder"
  WARN "Could not read wlan0 MAC; using SSID $AP_SSID"
fi
AP_PASS="12345678"

if ! nmcli -t -f NAME con show | grep -qx "efinder-ap"; then
  LOG "Creating Wi-Fi AP profile: SSID=$AP_SSID"
  nmcli con add \
    type wifi \
    ifname wlan0 \
    con-name efinder-ap \
    autoconnect yes \
    ssid "$AP_SSID" \
    wifi.mode ap \
    wifi.band bg \
    ipv4.method shared \
    ipv4.addresses 10.42.0.1/24 \
    ipv6.method ignore \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$AP_PASS" \
    || WARN "Could not create AP profile"

  LOG "  SSID=$AP_SSID  password=$AP_PASS  IP=10.42.0.1"
else
  LOG "AP profile already exists — leaving it unchanged"
fi

# Ensure NM will always retry the AP connection. autoconnect-retries=0
# means retry indefinitely; without this NM stops trying after a few
# failures and will not retry until manually prompted, even across reboots.
nmcli con modify efinder-ap \
  connection.autoconnect yes \
  connection.autoconnect-retries 0 \
  2>/dev/null || WARN "Could not set AP autoconnect-retries (non-fatal)"

# Explicitly activate the AP now. NM was already running when the profile
# was created so it may have missed the startup autoconnect sweep. Calling
# `nmcli con up` here avoids a 30-90 s delay waiting for NM's retry timer
# or efinder-ensure-ap's polling loop. This is a no-op if it is already up.
if ! nmcli -t -f NAME,DEVICE con show --active 2>/dev/null \
     | awk -F: '$2=="wlan0"{exit 0} END{exit 1}'; then
  LOG "Activating AP profile on wlan0"
  nmcli con up efinder-ap 2>/dev/null \
    || WARN "Could not bring up AP immediately (efinder-ensure-ap will retry)"
else
  LOG "wlan0 already has an active connection — leaving it"
fi

# --- Filesystem setup ---------------------------------------------------------

mkdir -p /var/lib/efinder/captures
chown -R efinder:efinder /var/lib/efinder 2>/dev/null || true

# --- Record last-run time (diagnostic only — NOT read as a gate) --------------

date -u +"%Y-%m-%dT%H:%M:%SZ" > "$DONE_MARKER"
LOG "Boot setup complete"
