#!/bin/bash
# Switch the diofinder's Wi-Fi to access-point mode.
#
# Usage:
#   sudo ap.sh                  # use the existing diofinder-ap profile
#   sudo ap.sh SSID PASSWORD    # change SSID/password and activate
#
# Connect from your phone or laptop:
#   1. Look for the SSID printed below in your Wi-Fi list.
#   2. Connect with the password printed below.
#   3. ssh diofinder@1.2.3.4   (or diofinder.local once mDNS resolves)
#
# The AP uses the 1.2.3.0/24 subnet with the Pi at 1.2.3.4 — the SAME address
# Celestron's SkyPortal WiFi module uses — so SkyPortal's "Direct Connect"
# (which dials a hardcoded 1.2.3.4:2000 and ignores discovery) works when a
# phone is joined straight to this AP. See docs/skyportal-aux.md.
#
# Run via sudo. Reports the active SSID and password before and after
# so you know exactly what to look for.

set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "ERROR: must run as root (try: sudo $0 ...)" >&2
  exit 1
fi

PROFILE="diofinder-ap"

# If user provided SSID and password, update the profile first.
if [ $# -ge 1 ]; then
  NEW_SSID="$1"
  if [ $# -ge 2 ]; then
    NEW_PASS="$2"
  else
    echo "ERROR: when specifying SSID, password is also required." >&2
    echo "Usage: sudo ap.sh [SSID PASSWORD]" >&2
    exit 1
  fi
  if [ ${#NEW_PASS} -lt 8 ]; then
    echo "ERROR: WPA2 password must be >= 8 characters." >&2
    exit 1
  fi

  if ! nmcli -t -f NAME con show | grep -qx "$PROFILE"; then
    echo "Creating AP profile $PROFILE"
    nmcli con add \
      type wifi \
      ifname wlan0 \
      con-name "$PROFILE" \
      autoconnect yes \
      ssid "$NEW_SSID" \
      wifi.mode ap \
      wifi.band bg \
      ipv4.method shared \
      ipv4.addresses 1.2.3.4/24 \
      ipv6.method ignore \
      wifi-sec.key-mgmt wpa-psk \
      wifi-sec.psk "$NEW_PASS"
  else
    echo "Updating AP profile $PROFILE: SSID=$NEW_SSID"
    nmcli con modify "$PROFILE" \
      802-11-wireless.ssid "$NEW_SSID" \
      wifi-sec.psk "$NEW_PASS"
  fi
fi

# Verify the profile exists.
if ! nmcli -t -f NAME con show | grep -qx "$PROFILE"; then
  echo "ERROR: $PROFILE profile not found." >&2
  echo "Run 'sudo ap.sh SSID PASSWORD' to create it." >&2
  exit 1
fi

# Migrate devices provisioned before the SkyPortal-subnet change: older
# profiles used 10.42.0.1/24, which SkyPortal "Direct Connect" (hardcoded
# 1.2.3.4:2000) can't reach. Force the Celestron subnet on every run so an
# in-place update fixes it without re-creating the profile.
CUR_ADDR=$(nmcli -t -f ipv4.addresses con show "$PROFILE" 2>/dev/null | cut -d: -f2)
if [ "$CUR_ADDR" != "1.2.3.4/24" ]; then
  echo "Setting AP address to 1.2.3.4/24 (was: ${CUR_ADDR:-unset})"
  nmcli con modify "$PROFILE" \
    ipv4.method shared \
    ipv4.addresses 1.2.3.4/24 \
    2>/dev/null || true
fi

# Take down any active station connection on wlan0.
ACTIVE_WIFI=$(nmcli -t -f NAME,DEVICE con show --active | awk -F: '$2=="wlan0"{print $1}')
if [ -n "$ACTIVE_WIFI" ] && [ "$ACTIVE_WIFI" != "$PROFILE" ]; then
  echo "Deactivating current Wi-Fi connection: $ACTIVE_WIFI"
  nmcli con down "$ACTIVE_WIFI" >/dev/null || true
fi

# Re-enable AP autoconnect and clear any suppression from previous failures
# or from station.sh (which sets autoconnect=no on the AP profile).
nmcli con modify "$PROFILE" \
  connection.autoconnect yes \
  connection.autoconnect-retries 0 \
  2>/dev/null || true

# Bring up the AP.
echo "Activating AP profile: $PROFILE"
nmcli con up "$PROFILE"

# Report what we ended up with.
SSID=$(nmcli -t -s -f 802-11-wireless.ssid con show "$PROFILE" | cut -d: -f2)
PSK=$(nmcli -t -s -f 802-11-wireless-security.psk con show "$PROFILE" | cut -d: -f2)
IP=$(ip -4 addr show wlan0 2>/dev/null | awk '/inet / {print $2; exit}')

cat <<EOF

diofinder Wi-Fi is now in ACCESS POINT mode.

  SSID:     $SSID
  Password: $PSK
  IP:       ${IP:-(none yet)}

Connect a device to that SSID, then:
  ssh diofinder@1.2.3.4
or:
  ssh diofinder@diofinder.local   (if mDNS works on your client)

SkyPortal "Direct Connect" works while joined to this AP (the Pi is at
1.2.3.4, the same address the Celestron WiFi module uses).

To switch to a real Wi-Fi network later:
  sudo /usr/local/bin/station.sh "MyWiFi" "MyPassword"

EOF
