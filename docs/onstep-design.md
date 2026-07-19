# OnStepX sync output — design spec

Status: **SynScan dialect BUILT (v1); OnStepX/LX200 + Alpaca dialects still
design-only.** Optional, off-by-default feature to turn the finder into a
plate-solve alignment source for a GoTo mount.

> **As built — SynScan (SkyWatcher Virtuoso), first dialect.** The pluggable
> `MountLink` design below is realized in **`diofinder/mountlink.py`** with the
> **SynScan/NexStar** dialect (`SynScanLink`) + a pyserial `SerialTransport`
> over the GPIO UART (`/dev/serial0`, GPIO14/15 → **MAX232 → RS-232** to the
> Virtuoso's wired serial port). It follows this spec's shape:
> `sync(ra,dec)` (precise NexStar `s` command, sync-only — never slews),
> `version()`, `get_radec()`, `ping()`; epoch conversion via
> `diofinder.precession` gated by the mount's own epoch. The comms side adds
> `_mount_loop` (auto-push daemon, mirrors `_auto_exposure_loop`), the pure
> `mountlink.should_sync` policy (§3), the `mount_status/sync/test/set` maint
> commands (§6), a Camera-page "Mount sync" card (§7), and `mount_*` config
> keys. The generic prefix is **`mount_*`** (not `onstep_*`) since SynScan
> shipped first and the layer is dialect-agnostic. Unit-tested in
> `tests/test_mountlink.py` (encoding round-trips, sync sequence/reply against
> a fake transport, epoch conversion, the auto-push decision matrix). **When
> the LX200/Alpaca dialects are added, `onstep_*` in the tables below maps to
> the shipped `mount_*` keys.**

---

## 1. Goal & scope

After a confident plate-solve, optionally push the solved position to an
**OnStepX** mount as a **sync** (`:Sr`/`:Sd`/`:CM#`), correcting the mount's
pointing model. The finder becomes a *bridge*: it serves SkySafari over WiFi
**and** drives the mount over serial/TCP at the same time.

```
phone (SkySafari) ──WiFi──▶ finder ──serial/TCP──▶ mount (OnStepX)
                            (comms_proc)
```

**v1 scope**

- **Sync only** (`:CM#`) — corrects the mount's model; **never moves the scope.**
- Serial **and** TCP transport.
- **Manual** (one-shot "sync now") *and* **auto** (gated continuous) push modes.
- Status / test / sync over the maint socket + a web UI card.

**Explicitly out of scope for v1:** GoTo/slew (`:MS#`) — it physically moves the
mount (collision / cable-wrap risk). If ever added, it goes behind a hard
confirmation + limits, as a separate decision.

**Safety stance:** default OFF; opt-in; sync-only cannot move anything; every
push is gated and logged.

---

## 2. Architecture

Serve SkySafari *and* drive the mount concurrently — **not** either/or (the
eFinder `enhanced`-branch reference `onstepx_serial.py` picks one mode via its
AUTO connect; diofinder should run both at once, which its threaded comms
process already makes natural).

- **New module `diofinder/onstep.py`** — a **dialect- and transport-agnostic**
  `MountLink` abstraction (see §11 for why the abstraction matters):
  - transport behind one method `command(cmd, expect_reply, timeout) -> reply`
    (serial via pyserial, or TCP via socket, chosen by config);
  - `sync(ra_deg, dec_deg) -> (ok, detail)`: send `:Sr…#` / `:Sd…#` (verify each
    returns `1`), then `:CM#`, interpret the reply;
  - `version() -> str` (`:GVP#` / `:GVN#`) for the connection test;
  - reconnect-with-backoff on drop; a lock (manual + auto can both call).
  - Pure/testable: formatting and the command *sequence* verified against a fake
    transport — no hardware.
- **New comms thread `_onstep_loop(ctx)`** — a daemon thread alongside
  `_auto_exposure_loop` / `_watchdog_loop`, gated by `onstep_enabled`. Owns the
  connection, runs the push policy, publishes status. It lives in comms because
  comms already owns telescope I/O, the LX200 vocabulary, `latest_solution`, and
  the motion signals.

---

## 3. Push policy — the core decision

A pure, unit-tested function
`_onstep_should_sync(solution, now, last_sync, gates, moving) -> bool`
(mirrors `_auto_exposure_decision`), so the policy is testable without hardware.

**Manual mode (recommended default):** the thread idles; it syncs only when the
`onstep_sync` maint command fires (user pressed "Sync now" and is watching).

**Auto mode (opt-in):** after each solve, push a sync **only if all** gates pass:

| Gate            | Config                          | Why                                                   |
|-----------------|---------------------------------|-------------------------------------------------------|
| Solved & fresh  | `onstep_auto_max_age_s` (3 s)   | never sync a stale / held position                    |
| Confident       | `onstep_auto_min_matches` (8)   | don't sync a weak solve                               |
| Not slewing + settled | `onstep_auto_settle_s` (3 s) | reuse the IMU rate-gate / solution-stability signal; never sync mid-slew |
| Outside deadband | `onstep_auto_deadband_arcmin` (1.0) | don't re-sync the same spot                       |
| Rate-limited    | `onstep_auto_min_interval_s` (15 s) | a mount doesn't need sub-second corrections       |

Each accepted sync records the pushed coords + time + mount reply as the new
"last sync" (used by the deadband + rate-limit gates and the UI).

---

## 4. Epoch handling

**Two epoch facts, both must be pinned:**

1. **Finder solution epoch — determined: J2000 (ICRS).** The solver databases
   are built (in `astro_databases`) from **Gaia DR3 + Hipparcos**, which are in
   the **ICRS** frame (≈ J2000 to ~tens of mas — J2000 for any finder purpose).
   Positions are proper-motion-propagated to epoch 2026.0, but that is the
   *position* epoch, not a rotation of the axes to equinox-of-date, so the frame
   stays J2000. olive-solve returns that frame unmodified, and diofinder applies
   **no precession anywhere** (verified: `_filled_solution` stores
   `float(ra)/float(dec)` raw; a package-wide grep for precess/nutation/j2000/
   jnow finds nothing). So `latest_solution.ra_deg/dec_deg` are **J2000/ICRS**,
   and boresight-corrected (the solver solves at the boresight target pixel).
   *Recommended empirical confirmation:* point at a bright named star and check
   diofinder's RA/Dec against its J2000 vs JNow coords (they differ ~15–20′ in
   2026).
2. **Mount epoch** — **OnStepX uses JNow** (confirmed). But OnStepX's coordinate
   epoch is itself configurable on the mount side (JNow default, J2000
   possible), so the finder must **match whatever the mount is set to.**

So the concrete conversion is **J2000 → JNow** (finder J2000 → OnStepX JNow):
apply precession. Because the catalog's position epoch (~2026) already matches
the equinox-of-date target (~2026), a plain mean-place precession is
self-consistent; nutation (~9″) and aberration (~20″) are below finder relevance
and can be skipped. Prefer IAU 1976 precession (ζ, z, θ; sub-arcsecond, ~15
lines, no dependency) over the eFinder first-order Newcomb (~1′). Getting the
epoch wrong is a ~15–20′ error in 2026 — small for a finder, but a real
systematic bias fed into a GoTo model.

> **eFinder bug not to copy:** the `onstepx_serial.py` reference sends raw
> coordinates with **no precession** at all.

> **SkySafari implication:** because diofinder reports J2000 with no precession
> and SkySafari's LX200 interface conventionally expects JNow, there may already
> be a ~15–20′ epoch offset in the SkySafari crosshair today — tolerable on a
> 13.6° finder and largely absorbed by an align, but worth confirming how the
> SkySafari scope/epoch is set. The recommended empirical star check above
> answers this at the same time.

> **STATUS UPDATE (v0.11.53, shipped 2026-07-15):** the SkySafari epoch offset
> above was **confirmed real** (SkySafari was set to "Use Current Epoch" = JNow)
> and **fixed**. `diofinder/precession.py` (IAU 1976, `math`-only — exactly the
> ζ/z/θ recommendation here) now converts at the comms boundary:
> `_report_radec` does J2000→JNow outbound (`:GR/:GD` + status), `_do_alignment`
> does JNow→J2000 for the `:CM#` target, gated by `shared_cfg["report_epoch"]`
> (`jnow` default, `j2000` kill switch). **The OnStep sync should reuse this
> machinery verbatim** — `_report_radec` for the outbound sync coordinate and
> `precession.jnow_to_j2000` for any inbound mount target — rather than
> reimplement the conversion. The `report_epoch` config key already exists; the
> mount-epoch setup field below is a *separate* per-mount value (the mount could
> be J2000 while SkySafari is JNow), so keep them distinct.

### Should the epoch be a web UI toggle? — **Yes, as a setup field.**

- OnStepX defaults to JNow but can be configured either way, and LX200 has **no
  reliable "what epoch are you?" query**, so auto-detection isn't dependable.
- Therefore the *mount* epoch must be a user-settable value. Expose it in the
  Mount card as a small **JNow / J2000 dropdown, default JNow**, grouped with the
  other connection setup fields (transport / port).
- It is a **setup-time field set once per mount**, not a nightly tuning knob —
  but there's no harm making it live-mutable via `shared_cfg` (it only changes
  the conversion). Persist it like the rest.
- The *finder's own* solution epoch is an internal fact fixed by the solver, not
  a user setting — it is not exposed.

Config key: `onstep_epoch` (`jnow` default / `j2000`).

### Cost, implementation language, and whether it's even needed

**Overhead: negligible.** The conversion is a handful of trig ops (a 3×3
precession rotation) run **once per sync** — at most every ~15 s in auto mode,
or per button-press in manual mode. It is **not** a per-frame path:
microseconds, ~100,000× cheaper than the solve it follows and run far less
often. It never touches the solve/detect hot loop.

**Language: Python.** Because it is once-per-sync (not per pixel/frame), it lives
in `comms_proc` (Python) beside the sync logic — pure `math`, no FFI, no astropy
(too heavy for the Pi Zero and overkill), trivially unit-testable against known
precession values. Rust is for the per-frame pixel/solve hot paths; a
once-per-sync coordinate rotation is the opposite case. **No Rust.**

**Does it need doing, or does boresight calibration compensate? It must be done —
boresight does NOT compensate.** Boresight (`:CM#` align) is a **constant** pixel
offset; precession is a **position-dependent** frame rotation (the J2000↔JNow
shift varies with RA and Dec across the sky). A boresight/align absorbs the
*local* epoch offset at one point, but cannot track it as it changes across the
sky. For the finder's own job (put the target near the centre of a 13.6° FOV) a
local align makes the few-arcminute residual tolerable — which is why the finder
works today despite reporting J2000. But feeding a **GoTo mount**, an
uncorrected epoch injects a ~15–20′ systematic error that carries into every
subsequent slew. So the sync must send coordinates in the mount's epoch; since
the cost is microseconds, there is no reason not to.

---

## 5. Config keys (`config.py` + conf)

| Key                                    | Default        | Live? | Notes                                   |
|----------------------------------------|----------------|-------|-----------------------------------------|
| `onstep_enabled`                       | `false`        | yes   | master switch                           |
| `onstep_transport`                     | `serial`       | no    | `serial` / `tcp` (restart — opens a device) |
| `onstep_serial_port`                   | `/dev/ttyAMA0` | no    | or `/dev/ttyUSB*`; auto-detect if empty |
| `onstep_serial_baud`                   | `9600`         | no    |                                         |
| `onstep_tcp_host` / `onstep_tcp_port`  | — / `9999`     | no    | OnStepX SmartWebServer                  |
| `onstep_mode`                          | `manual`       | yes   | `manual` / `auto`                       |
| `onstep_epoch`                         | `jnow`         | yes   | `jnow` / `j2000` — mount's epoch (§4)   |
| `onstep_auto_*` (the 5 gates)          | see §3         | yes   | tunable without restart                 |

Transport / port are restart-level (opening a device); toggles and gates are
live via `shared_cfg`.

---

## 6. Maintenance commands (`comms_proc._handle_maint_command`)

- **`onstep_status`** → `{enabled, transport, connected, mount_version, mode,
  last_sync:{ra,dec,utc,result}, syncs_ok, syncs_fail, last_error}`.
- **`onstep_sync`** → one-shot manual push of the current solved position;
  returns result or a clear error (`no fresh solution`, `not connected`, or the
  mount's reply).
- **`onstep_set`** → set/persist the live-tunable keys (enabled, mode, epoch,
  gates); `config.save_keys` + `shared_cfg`.
- **`onstep_test`** → open the link and query version; returns the mount's
  response or the wiring error (for setup).

---

## 7. Web UI

A **"Mount (OnStepX)"** card (expert Camera/Advanced page or a new Mount section):

- Enable toggle; transport + port/baud (or host:port); **epoch dropdown (§4)**;
  mode; the auto gates.
- **"Test connection"** → shows mount version or the error.
- **"Sync now"** → one-shot manual sync, shows the result.
- Live status line (connected, last sync coords/time/result, counters) via an
  `/api/onstep` poller.
- Routes `/onstep/{set,sync,test}` + `/api/onstep`, each wrapping a maint command
  through `_safe_call`.

Keep the OnStep sync **separate from SkySafari's `:CM#`** (which syncs the finder
*boresight*) — don't overload one tap to mean two things.

---

## 8. Safety

- Default OFF; sync-only cannot move the mount.
- Gates prevent syncing mid-slew or on weak/stale solves; rate-limited.
- Manual mode = user in control and watching (the recommended default).
- Every sync logs the command sent and the mount's reply.
- GoTo is out of scope for v1.

---

## 9. Failure handling

- Not connected / wrong port / permission (the `diofinder` user needs device
  access, e.g. `dialout`) → clear status + error; auto mode idles.
- Mount rejects a sync → capture and surface the reply.
- Link drops mid-session → reconnect with backoff; never take down comms.
- No / stale solution → manual sync returns "no fresh solution."

---

## 10. Testing (no hardware)

- Formatting (RA/Dec ↔ LX200) and the sync **sequence** against a fake transport
  (asserts `:Sr…#` → `:Sd…#` → `:CM#`, and reply interpretation).
- `_onstep_should_sync` decision matrix (not-solved / stale / low-matches /
  slewing / deadband / rate-limited / all-clear).
- Epoch conversion against known precession values.

---

## 11. Talking to other mounts (SkyWatcher, and the general strategy)

The whole reason `MountLink` is an abstraction: OnStepX is v1 because it is
**LX200-native**, so it reuses the exact command vocabulary the finder already
implements (`:Sr`/`:Sd`/`:CM#`). Other mounts are *not* free, and how expensive
they are depends entirely on their protocol.

### SkyWatcher — viable, but a separate adapter, not a toggle

SkyWatcher/SynScan mounts (EQ6-R, AZ-EQ6, HEQ5, AZ-GTi, …) do **not** speak
LX200. They use the **SynScan protocol** — a different ASCII command set — over
either the hand-controller serial link or the SynScan WiFi adapter's UDP
(port 11880). So the transport abstraction helps, but the **command layer is
entirely different**; you cannot just point the LX200 `sync()` at it. Three
possible paths, in order of attractiveness:

1. **ASCOM Alpaca (recommended investigation).** Alpaca is a standardized HTTP
   REST API (`PUT /api/v1/telescope/0/synctocoordinates`) with a queryable
   `EquatorialSystem` (so epoch is discoverable, not guessed). If the specific
   SkyWatcher setup exposes Alpaca (newer SynScan app / firmware do), an
   `AlpacaLink` implementing the same `sync()` interface would work — **and the
   same adapter then drives *any* Alpaca mount**, not just SkyWatcher. This is
   the best "talk to everything else" strategy: one HTTP client, many mounts,
   standardized sync + epoch. Verify Alpaca support on the target hardware first.
2. **Native SynScan protocol** (serial HC, or UDP 11880 to the SynScan WiFi
   adapter). Direct and dependency-free, but proprietary: a dedicated
   `SynScanLink` speaking SynScan's command set and sync semantics, with its own
   epoch handling. Moderate effort, hardware-specific to validate.
3. **INDI / ASCOM host bridge** (`indi_eqmod`, EQMOD/GS Server). The finder
   talks to a PC running the bridge. Heavy, needs a host on the network, and
   defeats the "finder pushes directly to the mount on the OTA" simplicity.
   Not recommended.

### SkyWatcher/SynScan "Virtuoso" over GPIO serial — feasible, second dialect

A wired GPIO link (§13) to a SkyWatcher **Virtuoso** (Freedom-Find alt-az) is
feasible, with two caveats that make it more work than OnStepX:

- **Protocol: SynScan/AUX, not LX200.** The Virtuoso's motor controller speaks
  the SkyWatcher **AUX/SynScan** command set (the `:`-prefixed motor protocol
  INDI's `skywatcherAPIMount` uses, and/or the NexStar-derived SynScan HC ASCII
  set) at **9600 baud TTL**. So it needs the **`SynScanLink` dialect** (path 2
  above), not the near-free LX200 `sync()`. Its sync is an axis-position/offset
  set (INDI implements it), so an aim-point correction *is* expressible — but the
  semantics differ from `:CM#` and need care.
- **Electrical:** confirm the mount's serial-port level (TTL vs the HC's RS-232)
  and pinout before wiring — likely TTL → a divider or MAX3232 (see §13).
- **Great fit for Freedom-Find.** Because the Virtuoso tracks hand-pushes via its
  encoders, diofinder plate-solving + a serial **sync** gives it an accurate
  absolute reference: push roughly to target, diofinder solves and corrects the
  mount's model, and its GoTo/tracking is then dead-on **without a manual star
  alignment** — arguably the most compelling mount pairing here (auto-align the
  Virtuoso from the finder).
- **GTi caveat.** The **Virtuoso GTi** is Wi-Fi-primary (SynScan app → UDP
  11880). If it exposes a wired serial port, GPIO still works and avoids Wi-Fi
  contention; if not, you're back to the Wi-Fi/Alpaca path (path 1) with its
  topology trade-offs. Confirm the exact model's ports.

Net: a Virtuoso over GPIO is **feasible as a second `MountLink` dialect**
(`SynScanLink`) sharing the GPIO transport — it vindicates the pluggable-dialect
design. More effort than OnStepX (new command set), identical transport story.

### General strategy

- Keep `MountLink` **dialect-pluggable**: `sync(ra_deg, dec_deg) -> (ok, detail)`
  is the stable interface; `LX200Link` (OnStepX/Meade/Celestron NexStar-ish),
  `AlpacaLink` (broadest reach), and `SynScanLink` (native SkyWatcher) are
  interchangeable implementations selected by a `mount_protocol` config.
- Ship **OnStepX / LX200 first** (near-free), then evaluate **Alpaca** as the
  single highest-leverage addition (covers SkyWatcher *and* a wide field of
  other mounts through one standardized client), and treat native SynScan as a
  fallback only if the target hardware can't do Alpaca.

---

## 12. Prerequisites / open questions

1. **Epoch + boresight of `latest_solution` — RESOLVED (§4) and BUILT (v0.11.53).**
   Determined J2000/ICRS, boresight-corrected (diofinder applies no precession;
   the frame is the Gaia/Hipparcos catalog frame). The J2000⇄JNow **comms
   boundary** and the pure IAU-1976 `diofinder/precession.py` helper shipped in
   v0.11.53 (so `:GR/:GD` and the web UI already report JNow, and `:CM#` align
   converts back) — **the OnStep sync reuses that same helper**; it does not
   re-implement precession. Mount side is JNow.
2. **Serial vs TCP as the shipped default.** Serial is topology-independent
   (finder ↔ OnStepX colocated on the OTA, no shared network) — recommended
   primary, over the **GPIO UART** (§13). TCP is there for WiFi OnStepX but
   reintroduces the network-topology dependency discussed for the phone/mount/
   finder case.
3. **UART transport — RESOLVED (§13): use the GPIO UART, not USB-serial.** The
   earlier "prefer USB-serial" guess was wrong for this device: the single OTG
   port is committed to the USB-gadget PuTTY console, and the login console is
   on `ttyGS0` (USB), *not* `serial0` — so with `enable_uart=1` (already set)
   the GPIO UART is free for the mount and the tether is untouched. Bluetooth is
   rejected (shared 2.4 GHz radio contends with the SkySafari Wi-Fi). See §13.
4. **Alpaca reach** — confirm whether the SkyWatcher (and other) target mounts
   expose ASCOM Alpaca, which would make §11 path 1 the general answer.
5. **Push-to-Dob: which feature? (§13)** — "deal with a push-to Dob" is three
   different things (replace / feed / read encoders); #1 needs no mount serial
   at all. Resolve before speccing.

---

## 13. Transport on the Pi Zero 2W — use the GPIO UART

Resolves §12's UART question with the device's **actual** serial layout
(verified in `scripts/install.sh` / `build/build-image.sh`).

**The finding.** diofinder's backup access (PuTTY) is a **USB CDC-ACM gadget**
on the single OTG port: install.sh sets `console=ttyGS0,115200` +
`serial-getty@ttyGS0`, and `diofinder-gadget-connect` builds the ACM device via
configfs on the dwc2 peripheral port. The login console therefore lives on the
**USB port**. Separately, install.sh already sets **`enable_uart=1`**, and the
console is *not* on `serial0` — so **GPIO14 (TXD) / GPIO15 (RXD) are free** and a
mount can own `/dev/serial0` without touching the tether.

**Consequences:**

- **GPIO UART is the recommended mount transport.** A dedicated wired link (zero
  radio contention) that leaves the USB port for the PuTTY tether. Near
  drop-in: `enable_uart=1` is already set; just ensure no
  `serial-getty@serial0`/`@ttyS0` is enabled and the `MountLink` serial
  transport opens `/dev/serial0`.
- **USB-serial to the mount is NOT viable** without giving up the tether: the Pi
  Zero has one OTG data port, committed to the gadget (`dr_mode=peripheral`); a
  USB-serial *host* link would need that port in host mode → conflict. (Reverses
  the earlier "prefer USB-serial" note.)
- **Bluetooth is inferior.** Wi-Fi + BT share the one 2.4 GHz radio on the Zero
  2W combo chip, so an active BT mount link contends with the SkySafari Wi-Fi
  stream for airtime — the opposite of what we want after taming the
  connection storm. Skip unless a wire is truly impossible.

**Electrical.** GPIO is **3.3 V TTL**. Most OnStep boards are 3.3 V TTL → direct
3-wire hookup (TX↔RX crossed, GND). A 5 V-TTL mount needs a divider on the
mount-TX→Pi-RX line; true RS-232 (±12 V) needs a **MAX3232**. With Bluetooth left
on, GPIO14/15 carry the **mini-UART** (`/dev/serial0`; PL011 stays on BT), stable
at 9600–19200 because `enable_uart=1` pins the core clock. `dtoverlay=disable-bt`
would move the PL011 there but is unnecessary for a slow mount link.

**Wiring.** 3 wires (TX, RX, GND) from the finder's GPIO header down the OTA to
the mount — a physical run worth a small connector.

### The push-to-Dob fork (resolve before speccing)

"Deal with a push-to Dob" hides **three different features** — decide which
before any protocol work, because they need different (or no) serial:

1. **Finder *replaces* the encoders** — diofinder *is* the digital setting
   circles. **No mount serial at all**: it already reports position to
   SkySafari/SkyPortal over Wi-Fi. Simplest, and arguably what a plate-solving
   finder is *for*. (Most likely the real goal for a manual Dob.)
2. **Finder *feeds* an existing DSC / hand controller** by emulating an encoder
   protocol (Nexus / "basic encoder" / BBox) *out* the GPIO UART — a different
   output dialect, same transport. Worth it only to keep an existing DSC display
   driven by diofinder's (better) position.
3. **Finder *reads* the Dob's encoders** *in* over the UART — possible, but the
   plate solve is already more accurate; the only value is dead-reckoning
   between solves (the IMU already does that) or when it can't solve. Marginal.

Most likely the real pairing is **#1 for a manual Dob** and **the outbound
`sync()` for a GoTo mount** (OnStepX, or a Virtuoso via `SynScanLink`, §11) —
both served by the GPIO UART transport + the `MountLink` dialect layer.
