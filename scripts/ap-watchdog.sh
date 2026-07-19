#!/bin/bash
# diofinder mid-session Wi-Fi AP-fallback watchdog.
#
# Closes the boot-only gap in diofinder-ensure-ap.service: if the finder is in
# station mode and the link drops in the field (hotspot sleeps, router/mount
# hub powers off, out of range) and does NOT recover within a grace window,
# force the diofinder-ap profile back up so the finder is always reachable at
# its own known SSID + 10.42.0.1. See docs/networking.md (deferred item 2).
#
# Runs continuously as a systemd service (diofinder-ap-watchdog.service, root),
# so it can call ap.sh directly. It never gives up the AP on its own once
# forced — matching the boot fallback's behaviour; the user reconnects a
# station network with station.sh when back in range.
#
# Tunables (env, overridable in the unit):
#   DIOFINDER_APWD_INTERVAL  seconds between checks            (default 10)
#   DIOFINDER_APWD_GRACE     idle seconds before forcing AP   (default 30)

set -uo pipefail

INTERVAL="${DIOFINDER_APWD_INTERVAL:-10}"
GRACE="${DIOFINDER_APWD_GRACE:-30}"
AP_PROFILE="diofinder-ap"
TAG="diofinder-ap-watchdog"
# Command seams (overridable so the loop logic can be exercised with fakes;
# production defaults are the real tools).
NMCLI="${DIOFINDER_APWD_NMCLI:-nmcli}"
AP_CMD="${DIOFINDER_APWD_AP_CMD:-/usr/local/bin/ap.sh}"

# Consecutive idle checks that add up to the grace window (round up, min 1).
need=$(( (GRACE + INTERVAL - 1) / INTERVAL ))
[ "$need" -lt 1 ] && need=1

# Name of the connection active on wlan0 right now (empty = interface idle).
wlan_active() {
  "$NMCLI" -t -f NAME,DEVICE con show --active 2>/dev/null \
    | awk -F: '$2=="wlan0"{print $1}'
}

logger -t "$TAG" "started (interval=${INTERVAL}s grace=${GRACE}s -> ${need} idle checks)"

idle=0
while true; do
  sleep "$INTERVAL"

  active="$(wlan_active)"
  if [ -n "$active" ]; then
    # A connection (station OR the AP) owns wlan0 — nothing to do.
    [ "$idle" -gt 0 ] && logger -t "$TAG" "wlan0 recovered ($active)"
    idle=0
    continue
  fi

  idle=$(( idle + 1 ))
  [ "$idle" -lt "$need" ] && continue

  # wlan0 has been idle for >= the grace window: fall back to the self-AP.
  logger -t "$TAG" "wlan0 idle >=${GRACE}s -- activating $AP_PROFILE"
  if "$AP_CMD" >/dev/null 2>&1; then
    logger -t "$TAG" "AP restored"
  else
    logger -t "$TAG" "WARNING: ap.sh failed; will retry"
  fi
  idle=0   # reset: a successful AP now shows active; a failure re-accumulates
done
