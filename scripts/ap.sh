#!/bin/bash
# Switch the eFinder's Wi-Fi to access-point mode.
#
# Usage:
#   sudo ap.sh                  # use the existing efinder-ap profile
#   sudo ap.sh SSID PASSWORD    # change SSID/password and activate
#
# Connect from your phone or laptop:
#   1. Look for the SSID printed below in your Wi-Fi list.
#   2. Connect with the password printed below.
#   3. ssh efinder@10.42.0.1   (or efinder.local once mDNS resolves)
#
# Run via sudo. Reports the active SSID and password before and after
# so you know exactly what to look for.

set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "ERROR: must run as root (try: sudo $0 ...)" >&2
  exit 1
fi

PROFILE="efinder-ap"

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
      ipv4.addresses 10.42.0.1/24 \
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

eFinder Wi-Fi is now in ACCESS POINT mode.

  SSID:     $SSID
  Password: $PSK
  IP:       ${IP:-(none yet)}

Connect a device to that SSID, then:
  ssh efinder@10.42.0.1
or:
  ssh efinder@efinder.local   (if mDNS works on your client)

To switch to a real Wi-Fi network later:
  sudo /usr/local/bin/station.sh "MyWiFi" "MyPassword"

EOF
