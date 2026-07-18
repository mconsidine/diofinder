# SkyPortal / Celestron AUX server

SkyPortal (and SkySafari's **Celestron WiFi** scope type) does not speak
LX200 over WiFi — it speaks the **Celestron AUX bus protocol** framed over
TCP. The real SkyPortal WiFi module is a dumb TCP↔serial bridge onto the
mount's internal AUX bus; the app itself joins the bus as device `0x20` and
talks directly to the motor controllers. The app keeps the alignment model
**in the app**: the mount is only ever asked for raw encoder angles, and the
user's in-app star alignment maps encoders → sky.

diofinder therefore serves SkyPortal by pretending to be a NexStar Evolution:
`comms_proc` runs a second TCP server on **port 2000** (alongside LX200 on
4060 — both apps work, interchangeably) that answers AUX frames with the
plate-solved, IMU-smoothed pointing converted to topocentric **alt/az** as
the two "encoder" values. Because the reported angles are *true* alt/az, the
app's alignment converges to essentially a pure rotation and pointing is
accurate across the whole sky. Protocol logic lives in
`diofinder/celestron_aux.py` (pure stdlib, unit-tested in
`tests/test_celestron_aux.py` against a captured real-mount session); the
TCP server, UDP discovery beacon, and pointing callback live in
`comms_proc.py`.

## Using it

1. Set your **site** on the web UI Config page → "Observer location" card
   (paste `lat, lon` from a phone maps app — both Apple and Google Maps copy
   coordinates in that form). The **clock** syncs itself: every web-UI page
   load posts the browser's time to the daemon, which steps its own clock if
   the drift exceeds ~10 s. Alt/az is computed from the solved RA/Dec with
   site + time; the in-app alignment absorbs a constant offset but not a
   wrong rate from a bad site/clock, so both should be roughly right.
2. Put the phone on the same network as the finder (the finder's AP or a
   shared Wi-Fi).
3. SkyPortal → Settings → Telescope → connect. Auto-detect finds the finder
   via the UDP beacon; if it doesn't, disable auto-detect and enter the Pi's
   IP with port 2000.
4. Do the app's star alignment (center a star in the scope, tap Align). The
   crosshair then tracks the plate-solved pointing.

GoTo and manual-slew commands are **acknowledged as no-ops** (slew reports
"done" immediately) — diofinder cannot move the telescope; the user pushes.
The app's crosshair always shows where the scope actually points.

### Getting time and location without SkySafari

The AUX protocol has no channel for the app to hand time or location to the
mount (on the real bus a GPS accessory *serves* those; the Evolution has no
RTC). So a SkyPortal-only setup gets them from the browser instead:

* **Time** — automatic. `base.html` POSTs `Date.now()` to `/api/time_sync`
  on every page load; `comms_proc._sync_clock_epoch` steps the system clock
  past the shared `_CLOCK_DRIFT_MIN_S` (10 s) threshold via the same
  sudo-whitelisted `diofinder-set-time` helper the SkySafari `:SL/:SG/:SC`
  path uses. Stepping the clock shifts the reported alt/az, so re-align
  SkyPortal if it was already connected when a large correction lands.
* **Location** — the Config "Observer location" card. Browser geolocation
  (`navigator.geolocation`) is blocked on plain `http://`, so the card takes
  a pasted `lat, lon` instead (`/location/set` → `location_set` maint
  command → persists both, routes latitude to the solver like
  `polar_set_latitude`). One-time; it rarely changes.

A single SkySafari LX200 connection still sets both automatically, and NTP
covers time whenever the finder has internet in station mode.

## Config keys

| Key | Default | Notes |
|-----|---------|-------|
| `celestron_aux_enabled` | `true` | Master switch for the AUX server. |
| `celestron_aux_port` | `2000` | SkyPortal expects 2000. |
| `celestron_beacon_enabled` | `true` | UDP identity beacon on port 55555, 1 Hz, only while no AUX client is connected (app auto-detect). |
| `celestron_model` | `0x1687` | Model reported to the app (`0x1687` = NexStar Evolution). Dataclass-only key. |

## Wire format

```
0x3b | len | src | dst | cmd | data... | checksum
```

`len` counts src+dst+cmd+data; checksum is the two's complement of the sum
of every byte from `len` through the last data byte. Replies swap src/dst
and repeat the cmd byte. Positions are big-endian **24-bit fractions of a
revolution** (`deg = value / 2^24 × 360`, signed). Device IDs: `0x20` app,
`0x10` AZM motor, `0x11` ALT motor, `0xb5` WiFi, `0xb6` battery, `0xb7`
charge port, `0xbf` lights. Devices we don't emulate (e.g. GPS `0xb0`) get
**no reply** — an absent bus device is silent and the app moves on.

## The handshake (from a captured real session)

Verified byte-for-byte against SkyPortal ↔ NexStar Evolution captures
(jochym/nexstar-evo `analysis/` dumps). On connect the app sends, in order:

| To | Cmd | Meaning | Our reply (data) |
|----|-----|---------|------------------|
| 0x10 | `0xfe` GET_VER | firmware version | `07 0a 10 0d` (as captured) |
| 0x10 | `0x05` GET_MODEL | mount model | `16 87` (Evolution) |
| both | `0x24 00` MOVE_POS rate 0 | stop axes | empty ack |
| both | `0x40` GET_POS_BACKLASH | | `00` |
| both | `0xfc` GET_APPROACH | | `00` |
| 0x10 | `0x21` max slew rate | | `0f 90 11 94` |
| 0x10 | `0x23` max-rate flag | | `01` |
| both | `0x47` probe | (real MC answers with cmd byte `0xf0`!) | `0xf0` + `47`, reproduced verbatim |
| 0xbf | `0x10` lights | | `37` |
| 0xb7 | `0x10` charge mode | | `00` |
| 0xb6 | `0x18` battery cutoff | | `07 d0` |
| both | `0x01` GET_POSITION | begin position polling | live alt/az, 24-bit |
| both | `0x06` SET_POS_GUIDERATE | app-driven tracking | empty ack |
| 0x10 | `0x3a`/`0x38` cordwrap | | empty acks |

Steady state: `0x01` position polls several times a second (drives the
crosshair), `0x06` guiderate updates ~1 Hz (ignored), periodic `0xb6 0x10`
battery polls. GoTo: `0x02` GOTO_FAST → `0x13` SLEW_DONE poll (we answer
`ff` = done) → `0x17` GOTO_SLOW → `0x13`. Any other setter is acknowledged
with an empty-payload echo, exactly like a real motor controller.

## Sources

- jochym/nexstar-evo — protocol notes + the raw `analysis/` captures
- Mraanderson/HBG3, g7ltt/Celestron-GPS-WiFi-BT-Interface — homebrew
  adapters (beacon format, keepalive behavior, emulation precedent)
- Andre Paquette's NexStar AUX command document (paquettefamily.ca/nexstar)
