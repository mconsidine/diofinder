#!/bin/bash
# Switch the eFinder's Wi-Fi from access-point mode to station (client) mode.
#
# Usage:
#   sudo station.sh                     # scan for networks and choose interactively
#   sudo station.sh "SSID" "Password"  # non-interactive (scripted)
#
# After this script:
#   * The Pi joins the named Wi-Fi network as a client.
#   * The 'efinder-ap' profile is deactivated (but kept; you can switch
#     back with sudo /usr/local/bin/ap.sh).
#   * The Pi gets an IP from your router's DHCP. mDNS still advertises
#     efinder.local, so 'ssh efinder@efinder.local' should work after
#     ~10 seconds.
#
# Tip: run this over the USB tether (10.55.0.1) -- the USB connection
# stays up while Wi-Fi switches. Once Wi-Fi connects, you can use
# either link to ssh in.

set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "ERROR: must run as root (try: sudo $0 ...)" >&2
  exit 1
fi

# ---- Interactive SSID discovery when no arguments are supplied ---------------
if [ $# -eq 0 ]; then
  echo "Scanning for Wi-Fi networks..."
  # Make sure the radio is on so nmcli can scan.
  nmcli radio wifi on 2>/dev/null || true
  sleep 2

  # Collect unique SSIDs (non-empty, exclude our own AP).
  mapfile -t SSIDS < <(
    nmcli -t -f SSID,SIGNAL dev wifi list 2>/dev/null \
      | grep -v '^efinder-' \
      | grep -v '^:' \
      | grep -v '^$' \
      | sort -t: -k2 -rn \
      | awk -F: '!seen[$1]++ && $1!=""  {print $1}' \
  )

  if [ ${#SSIDS[@]} -eq 0 ]; then
    echo "No networks found. Make sure Wi-Fi is unblocked and try again." >&2
    exit 1
  fi

  echo ""
  echo "Available networks:"
  for i in "${!SSIDS[@]}"; do
    printf "  %2d) %s\n" $((i+1)) "${SSIDS[$i]}"
  done
  echo ""
  read -rp "Select network [1-${#SSIDS[@]}]: " SEL
  if ! [[ "$SEL" =~ ^[0-9]+$ ]] || [ "$SEL" -lt 1 ] || [ "$SEL" -gt ${#SSIDS[@]} ]; then
    echo "Invalid selection." >&2
    exit 1
  fi
  NEW_SSID="${SSIDS[$((SEL-1))]}"

  read -rsp "Password for '$NEW_SSID' (Enter for open network): " NEW_PASS
  echo ""

elif [ $# -eq 2 ]; then
  NEW_SSID="$1"
  NEW_PASS="$2"
else
  cat <<EOF >&2
Usage:
  sudo station.sh                     # scan and choose interactively
  sudo station.sh "SSID" "Password"  # non-interactive

To switch back to AP mode:
  sudo /usr/local/bin/ap.sh
EOF
  exit 1
fi
PROFILE="efinder-station"

# WPA2 requires 8+ chars; empty is allowed (open network).
if [ -n "$NEW_PASS" ] && [ ${#NEW_PASS} -lt 8 ]; then
  echo "ERROR: WPA2 password must be 8+ characters (or empty for open network)." >&2
  exit 1
fi

# Take down the AP if it's active. Allow failure -- it might already be down.
ACTIVE_WIFI=$(nmcli -t -f NAME,DEVICE con show --active | awk -F: '$2=="wlan0"{print $1}')
if [ -n "$ACTIVE_WIFI" ]; then
  echo "Deactivating $ACTIVE_WIFI"
  nmcli con down "$ACTIVE_WIFI" >/dev/null 2>&1 || true
fi

# Create or update the station connection.
if ! nmcli -t -f NAME con show | grep -qx "$PROFILE"; then
  echo "Creating station profile: SSID=$NEW_SSID"
  if [ -n "$NEW_PASS" ]; then
    nmcli con add \
      type wifi \
      ifname wlan0 \
      con-name "$PROFILE" \
      autoconnect yes \
      ssid "$NEW_SSID" \
      wifi-sec.key-mgmt wpa-psk \
      wifi-sec.psk "$NEW_PASS"
  else
    nmcli con add \
      type wifi \
      ifname wlan0 \
      con-name "$PROFILE" \
      autoconnect yes \
      ssid "$NEW_SSID"
  fi
else
  echo "Updating station profile: SSID=$NEW_SSID"
  nmcli con modify "$PROFILE" \
    802-11-wireless.ssid "$NEW_SSID" \
    autoconnect yes
  if [ -n "$NEW_PASS" ]; then
    nmcli con modify "$PROFILE" \
      wifi-sec.key-mgmt wpa-psk \
      wifi-sec.psk "$NEW_PASS"
  else
    nmcli con modify "$PROFILE" \
      wifi-sec.key-mgmt "" \
      wifi-sec.psk "" 2>/dev/null || true
  fi
fi

# We want station to be the autoconnecting profile from now on. Demote AP
# to manual-only so it doesn't auto-grab the radio at next boot.
if nmcli -t -f NAME con show | grep -qx "efinder-ap"; then
  nmcli con modify "efinder-ap" autoconnect no
fi

# Activate.
echo "Connecting to '$NEW_SSID'..."
if ! nmcli con up "$PROFILE"; then
  echo "" >&2
  echo "ERROR: Could not connect to '$NEW_SSID'." >&2
  echo "Possible reasons:" >&2
  echo "  - Wrong password" >&2
  echo "  - Wrong SSID (case matters)" >&2
  echo "  - Network out of range" >&2
  echo "  - Network requires more than WPA2-PSK auth" >&2
  echo "" >&2
  echo "Returning to AP mode so you can re-tether and try again..." >&2
  nmcli con modify "efinder-ap" autoconnect yes 2>/dev/null || true
  nmcli con up efinder-ap >/dev/null 2>&1 || true
  exit 1
fi

# Wait briefly for an IP to land. NM activate doesn't wait for DHCP.
echo "Waiting for IP..."
for i in $(seq 1 20); do
  IP=$(ip -4 addr show wlan0 2>/dev/null | awk '/inet / {print $2; exit}')
  if [ -n "$IP" ]; then break; fi
  sleep 0.5
done

cat <<EOF

eFinder Wi-Fi is now in STATION mode.

  Network: $NEW_SSID
  IP:      ${IP:-(none yet -- check 'ip -4 addr show wlan0')}

Other devices on the same network can reach the eFinder at:
  ssh efinder@efinder.local
or directly at the IP above.

The USB tether (10.55.0.1) is still active in parallel; you can keep
using it or unplug now.

To return to AP mode later:
  sudo /usr/local/bin/ap.sh

EOF
