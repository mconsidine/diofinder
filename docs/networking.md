# Field networking — topologies, discovery, and the phone-internet problem

How the finder is reached in the field, why "keep the phone's internet **and**
talk to the finder" is constrained, and the deferred UX items that would make it
pleasant. Design/analysis notes — the deferred items are **not built**.

## What exists today (boot behaviour)

NetworkManager profiles + one guard service (`scripts/ap.sh`,
`scripts/station.sh`, `systemd/diofinder-ensure-ap.service`):

- **`diofinder-ap`** — `wifi.mode ap`, `ipv4.method shared` (the Pi runs its own
  DHCP + NAT), **fixed `10.42.0.1/24`**, autoconnect. The shipped default: the
  finder is its own access point at a known SSID + address. This is how a phone
  reaches it now.
- **`diofinder-station`** (`station.sh SSID PASS`) — joins a named network as a
  client, gets a **DHCP** address, and **demotes** `diofinder-ap` to
  autoconnect=no (but keeps the profile).
- **`diofinder-ensure-ap.service`** — at boot, polls `wlan0` for 60 s; if NM
  connected to *any* WiFi it exits, otherwise it **forces the AP up**. So the
  self-AP is the automatic fallback in every condition. **NOTE: boot-only** —
  it does not re-assert the AP if a station link drops mid-session (see the
  deferred watchdog below).
- **`avahi-daemon`** advertises **`diofinder.local`** (mDNS); hostname
  `diofinder`.
- The **USB link is a CDC-ACM serial console** (`ttyGS0`/`ttyACM0`) for bench
  management from a laptop — *not* part of the phone/SkySafari path.

## The governing constraint

A phone/tablet has **one WiFi radio → one network at a time.** So every wireless
peer the phone must reach — the finder, and any WiFi mount — has to be on the
**same** network. One device owns the network; the rest are clients. This single
fact dictates every topology below.

## Keeping the phone's internet *and* reaching the finder

The pain with the finder-as-AP: the phone joins the finder's AP, which has no
upstream, so the phone loses internet. Cross-platform fixes:

- **Reverse the roles — phone is the hotspot, finder is the client.** The phone
  runs Personal Hotspot (keeps its cellular), the finder joins it in **station
  mode**. Both on one subnet; the phone reaches the finder over WiFi and the
  internet over cellular simultaneously. **Works on iOS and Android** (unlike
  Bluetooth PAN, which iOS doesn't support — that's why Bluetooth is a dead end
  for this goal). No diofinder code needed — it's `station.sh` pointed at the
  hotspot SSID, and `ensure-ap` still catches you if the hotspot is down.
  - Catches: no cellular at dark sites (then internet is moot, local link still
    works); iOS hotspot sleeps when idle/locked; the finder's IP is now DHCP
    (iOS hotspots use `172.20.10.0/28`) so you must discover it — `diofinder.local`
    for the web UI, but SkySafari wants a numeric IP.
- **USB tethering** (wired) also works cross-platform and is rock-solid, but
  needs a cable to the phone — a non-starter when the finder is on the end of a
  large OTA, out of reach.

## With a WiFi mount (e.g. an encoder/GoTo mount) in the mix

The finder is an **LX200 server only** — it has no outbound mount connection
(confirmed: no `socket.connect` in `diofinder/`), so it and a mount are parallel
SkySafari endpoints, and SkySafari talks to **one scope at a time**. Topology
depends on the mount's WiFi:

- **Mount is AP-only** (common — Nexus DSC, many WiFi encoder boxes): the mount
  must be the hub. Phone joins the mount's AP; the finder joins it via
  `station.sh MountSSID …`. Clean integration, but the **phone loses internet**
  (the mount has no upstream) — the hotspot trick is off the table because the
  phone is captured by the mount's AP.
- **Mount can be a station**, or you use a **travel router**: the hub has an
  uplink, so phone (or router) + mount + finder all join it and internet is
  retained. **A small battery travel router is the clean multi-device answer** —
  it sidesteps the AP-only-mount problem entirely, and the finder just
  `station.sh`-joins it.

Mid-session caveat sharpens here: if the hub (mount AP or router) drops, after
60 s the finder falls back to its *own* AP — a *different* network from the
phone/mount — so a hub hiccup can split the finder off. Argues for the
mid-session AP-fallback watchdog below.

For actually *driving* a mount from the finder (plate-solve → sync the mount's
model), see `docs/onstep-design.md` — that's a separate outbound feature (v1:
OnStepX/LX200, serial bridge on the OTA).

## Deferred UX improvements (NOT built)

The role-reversal works today with pure configuration; these would make it
pleasant. All webui/systemd-level, no change to the solve/pointing path:

1. **Show the current station IP prominently** in the web UI (and any status
   surface), so the SkySafari numeric-IP step is copy-paste, not a hunt — you
   browse `diofinder.local`, read the IP, paste into SkySafari.
2. **Mid-session AP-fallback watchdog** — bring the self-AP back if the station
   link drops and doesn't recover within N seconds (closes the boot-only gap in
   `diofinder-ensure-ap`). **Highest-value field-robustness item.**
3. **A "join my phone's hotspot" helper** in the WiFi page (enter SSID/password
   once), with the AP-fallback behaviour explained inline so it's obvious the
   self-AP isn't given up.
