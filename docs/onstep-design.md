# OnStepX sync output — design spec

Status: **design only, not implemented.** Optional, off-by-default feature to
turn the finder into a plate-solve alignment source for a GoTo mount.

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

1. **Finder solution epoch** — is `latest_solution.ra_deg/dec_deg` J2000 or
   JNow, and is it boresight-corrected? (It should be boresight-corrected — the
   scope's actual pointing.) **Prerequisite to verify in code** before wiring
   the conversion.
2. **Mount epoch** — **OnStepX uses JNow** (confirmed). But OnStepX's coordinate
   epoch is itself configurable on the mount side (JNow default, J2000
   possible), so the finder must **match whatever the mount is set to.**

The conversion is `(finder epoch) -> (mount epoch)`. If the finder solves in
J2000 and the mount wants JNow, apply precession first (first-order Newcomb, as
in the eFinder `Coordinates.precess`, is ~1′ accurate — fine for a finder; use a
proper routine if we want better). Getting it wrong is a ~15–20′ error in 2026 —
small but real for a GoTo sync.

> **Note:** the eFinder `onstepx_serial.py` reference sends raw coordinates with
> **no precession** — a latent correctness bug we must not copy.

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

1. **Epoch + boresight of `latest_solution`** — confirm whether
   `ra_deg/dec_deg` are J2000 or JNow and that they're boresight-corrected.
   Decides the §4 conversion. (Mount side is confirmed JNow.)
2. **Serial vs TCP as the shipped default.** Serial is topology-independent
   (finder ↔ OnStepX colocated on the OTA, no shared network) — recommended
   primary. TCP is there for WiFi OnStepX but reintroduces the network-topology
   dependency discussed for the phone/mount/finder case.
3. **UART on the Pi Zero 2W** (`/dev/ttyAMA0` is entangled with Bluetooth /
   console) — likely prefer a **USB-serial** link to OnStepX; document the
   config step.
4. **Alpaca reach** — confirm whether the SkyWatcher (and other) target mounts
   expose ASCOM Alpaca, which would make §11 path 1 the general answer.
