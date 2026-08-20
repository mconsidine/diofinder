# Mount hub — design spec

Status: **Design only.** Supersedes the topology assumptions in
`onstep-design.md` §2 and §11. Optional, off-by-default mode in which the finder
becomes the **sole owner of the mount's command channel** and SkySafari talks to
the mount *through* it.

> **Relationship to `onstep-design.md`.** That document specs the *outbound sync*
> — the `MountLink` abstraction, the push policy, epoch handling, the `mount_*`
> config keys and maint commands. All of it stands and is reused verbatim here.
> What this document changes is the **topology**: `onstep-design.md` §2 assumed
> the finder drives the mount on one path while SkySafari independently connects
> somewhere else ("Serve SkySafari *and* drive the mount concurrently"). That is
> correct in intent but unsafe as drawn, because two LX200 clients on one mount
> share a target register (§2 below). This spec keeps the concurrency and routes
> it through a single owner.
>
> `LX200Link` + `TcpTransport` (the OnStepX dialect work) are a **prerequisite**
> for this document, not an alternative to it. Build them first; they are the
> same code either way.

---

## 1. Goal & scope

Let a phone running SkySafari drive an OnStepX mount **and** see the finder's
plate-solved position, with no possibility of command collision and one network
to join.

```
phone (SkySafari) ──WiFi──▶ finder ──serial──▶ mount (OnStepX)
                            (comms_proc)        MAX232 → RS-232
                   the ONLY client of the mount
```

**In scope**

- The finder as an LX200 **hub**: it answers what it knows, forwards the rest.
- `:GR`/`:GD` answered locally from the plate solve — the whole point of the
  finder.
- Slew, motion, park, tracking and rate commands forwarded to the mount so
  SkySafari keeps full control.
- The finder's own sync (manual or auto) injected into the *same* serialized
  channel, so it cannot race SkySafari.
- A passthrough degraded mode for when solving is down.

**Out of scope for v1**

- Closed-loop GoTo — specced in §8, but a **separate module, separate switch,
  separate decision**. It is the only part of this design that can move the
  mount.
- Alpaca / INDI northbound. If a client wants Alpaca it can drive the finder's
  LX200 port through an existing bridge.

**Safety stance:** hub mode is default OFF. With it off, behaviour is today's
byte-for-byte. With it on and closed-loop GoTo off, the finder still cannot
originate motion — it only relays motion the user asked for.

---

## 2. Why — the collision problem

LX200 syncs and slews are **stateful across commands**: `:Sr` and `:Sd` write a
target, then `:CM#` or `:MS#` acts on whatever is in it. In OnStepX that target
is one **mount-global** register. From `OnStepX/src/telescope/mount/goto/Goto.command.cpp`:

```c
if (command[0] == 'C' && (command[1] == 'S' || command[1] == 'M') && parameter[0] == 0) {
    ...
    e = requestSync(gotoTarget, pps);
```

`gotoTarget` is not per-connection. So with two independent clients:

```
SkySafari: :Sr 05:35:17#  :Sd +22*00:52#            :MS#   ← slews to the FINDER's coords
finder:                              :Sr…#  :Sd…#
```

The failure mode is not a rejected sync — it is **an unexpected slew to the
wrong coordinates**. That is a physical-safety bug.

**This is not a novel observation.** OnStep's own documentation states that the
serial connection allows only one application at a time, and that you should
never connect more than one application directly to the OnStep ASCOM driver —
you should *"always use POTH for multiple connections."* POTH is the classic
ASCOM **hub**: one process owns the mount and serializes everyone else. This
spec is a POTH that also happens to contain a plate solver.

**diofinder has already been bitten by the same shape of bug, one layer up.**
From `comms_proc.py`:

> *"Shared LX200 align target across ALL connection threads (v0.11.56 fix).
> SkySafari sends `:Sr`/`:Sd` then `:CM#`. v0.11.52's threaded server gave each
> connection its OWN `CommsAlignState`, so if the client split those commands
> across connections — or reconnected between them — the `:CM#` landed on a
> connection whose target was never set … the boresight never moved."*

Two releases to get right, on the finder's own server, where the consequence was
merely a silent no-op. On the mount the consequence is motion. §5 carries the
direct implication for how the hub buffers targets.

### Things that look like fixes but are not

**Separate command channels.** OnStep exposes two or three channels (SERIAL_A =
USB, SERIAL_B = TTL, typically bridged to IP by the WiFi add-on). Putting the
finder on one and SkySafari on another prevents **byte-stream interleaving** but
not **command-sequence interleaving**, because `gotoTarget` is global regardless
of arrival channel. This is precisely why OnStep's guidance is "use a hub"
rather than "use another port."

**ASCOM Alpaca.** Alpaca's `SyncToCoordinates` passes RA and Dec in a single
atomic REST call — no `:Sr`/`:Sd`/`:CM` triple, no shared register, no possible
collision. If SmartWebServer spoke Alpaca natively this whole document would be
unnecessary. **It does not.** SWS exposes a web UI plus the LX200 IP command
channel on 9999; Alpaca for OnStep exists only as a separate third-party
LX200→Alpaca driver running on a PC, which defeats a self-contained finder.
Re-check this if SWS ever gains native Alpaca — it would be a much smaller
design.

**Manual-only sync with direct connections.** Genuinely reduces the risk: if the
finder only touches the mount on a button press, a single human cannot press
"Sync now" and command a GoTo in the same instant. This is a legitimate interim
posture (§11) and is what should ship if `LX200Link` lands before this hub does.
It is *not* a destination, and it is **not safe with `mount_mode: auto`** — an
unattended push into a shared register has no human gate. Do not expose auto
mode for OnStepX until the hub exists.

---

## 3. What SkySafari loses today, and why that forces the issue

The current server answers motion commands without a mount behind them:

```python
if cmd == ":MS":
    return b"0"          # LX200: "0" = slew accepted
if cmd.startswith(":M") or cmd.startswith(":R") or cmd == ":Q":
    return b""
```

`:MS#` claims the slew was accepted and then nothing moves. A user pointing
SkySafari at the finder sees a successful GoTo and a stationary scope — a
confident lie, and the worst available UX failure.

**Fix in both modes.** In hub mode `:MS` is forwarded (§5). In non-hub mode it
must return an honest refusal (`1<string>#`, the below-horizon shape) rather
than `0`, so the client reports failure instead of silently doing nothing.

---

## 4. Architecture

The hub lives entirely in `comms_proc`, which already owns the LX200 vocabulary,
`latest_solution`, the motion signals, and (via `_MountManager`) the mount
connection.

```
_serve_lx200 accept loop  →  bounded queue  →  8 pool workers
                                                    │
                                          _handle_lx200_command
                                                    │
                                   ┌────────────────┴────────────────┐
                              answer locally                    forward
                            (solve, boresight)                     │
                                                          _MountManager._lock
                                                                    │
                                                            one open link
                                                          (LX200Link + transport)
                                                                    │
                                             also used by mount_sync / _mount_loop
```

Two properties fall out of this for free:

1. **Single serialization point.** Every `:Sr`/`:Sd`/`<action>` triple — whether
   it originated from SkySafari or from the finder's own auto-sync — passes
   through `_MountManager._lock` on one connection. Collisions become
   *structurally impossible*, not merely unlikely.
2. **The hot path never touches the mount.** `:GR`/`:GD` are the high-frequency
   traffic (SkySafari polls several times a second and, in many configs, opens a
   **new TCP connection per poll** — see `lx200-connection-pool-design.md`).
   Those are answered from `latest_solution` with no mount round-trip. Forwarded
   commands are user-initiated and rare. The reconnect storm does not churn the
   mount link, because `_MountManager` holds one persistent connection behind
   the lock.

No new threads. No new process. The worker pool and `_MountManager` already
exist and already have the right shapes.

---

## 5. Command routing

`_handle_lx200_command` gains a hub branch. The table is the specification.

| Command | Hub action | Rationale |
|---|---|---|
| `:GR` `:GD` | **Local** — from the plate solve | the reason the finder exists |
| `:Sr` `:Sd` | **Buffer, do not forward yet** | you cannot tell GoTo from sync until the *third* command arrives |
| `:CM` | **Local** — finder boresight align | preserves today's semantics; do not overload one tap with two meanings |
| `:CS` | **Local**, same as `:CM` | currently unhandled and silently returns `#` (§10) |
| `:MS` | **Forward** the buffered `:Sr`/`:Sd`/`:MS` as one locked unit | mount target written atomically by one owner |
| `:Mn` `:Ms` `:Me` `:Mw` | **Forward** verbatim | manual motion must reach the mount |
| `:Q` `:Qn` `:Qs` `:Qe` `:Qw` | **Forward** verbatim | stop must always get through — never queue or drop |
| `:RG` `:RC` `:RM` `:RS` | **Forward** verbatim | slew-rate selection is mount state |
| `:hP` `:hF` park/home, `:Te` `:Td` tracking | **Forward** | mount state, not finder state |
| `:GW` | **Forward** | SkySafari should see the *mount's* alignment status, not the finder's hardcoded `AT2#` |
| `:St` `:Sg` `:SL` `:SG` `:SC` site/time | **Both** — apply locally *and* forward | the finder needs them for polar/altaz; so does the mount |
| `:Gt` `:Gg` `:GS` `:GL` `:GC` `:GG` | **Local** | the finder is authoritative for its own configured site |
| `:GVP` `:GVN` `:GV*` | **Local** (identify as diofinder) | mount firmware surfaced separately via `mount_status` |
| `:GA` `:GZ` alt/az | **Forward** if available, else local | mount knows its own axes |
| **anything else** | **Forward** | see below |

**Flip the unknown-command default.** Today an unrecognised command returns
`b"#"` and is logged once via `_lx200_unhandled_seen`. In hub mode the safe
default inverts: forward it, because the finder is not the endpoint. Keep the
one-shot log — it is exactly the instrument for discovering what real clients
send, and it should keep firing in hub mode so the table above can be refined
from field data rather than guesswork.

### The target buffer must be module-level

`:Sr`, `:Sd` and `:MS` **can arrive on three different TCP connections** when a
client is in reconnect-per-poll mode. This is not hypothetical — it is the
documented cause of the v0.11.56 fix, and it is why `_lx200_align_state` is a
module-level singleton rather than per-connection.

The hub's GoTo target buffer inherits the identical constraint and the identical
solution: **one shared instance, module scope, guarded by the same discipline as
`_align_lock`.** Do not give it per-connection state. This is the single most
likely way to reintroduce a bug this codebase has already paid for twice.

---

## 6. Reply framing — the part that will eat a day

A hub cannot blindly relay bytes. LX200 has three incompatible reply shapes and
picking the wrong one desynchronises the stream for every subsequent command.

| Shape | Commands | Read strategy |
|---|---|---|
| **No reply** | `:Q*` `:M<dir>` `:R<rate>` | write and return immediately; never wait |
| **Single char** | `:Sr` `:Sd` `:St` `:Sg` `:SG` `:SL` `:MS`(success) | read exactly 1 byte |
| **`#`-terminated** | `:GR` `:GD` `:GVP` `:GVN` `:GW` `:CM` `:GA` `:GZ` | `read_until(b"#")` |
| **Mixed** | `:SC` (`1` + two `#`-terminated strings), `:MS`(failure: `1<msg>#`) | per-command special case |

Consequences:

- `SerialTransport.read_reply()` as written always does `read_until(b"#")` and
  raises `MountError` on timeout. It needs a companion `command(cmd, shape)`
  that honours the table, or every no-reply forward stalls the full timeout and
  then reports a spurious error.
- `:MS` is genuinely ambiguous on the wire — `0` means accepted, `1`/`2` are
  followed by a `#`-terminated message. Read one byte, then conditionally drain
  to `#`.
- **This table is the reason `:CM` beats `:CS` outbound** (see
  `onstep-design.md` and the Update3 analysis): `:CM` returns `N/A#` on success
  and `E1`–`E9` on failure, `:CS` returns nothing at all. Silent success is
  useless to `_MountManager`'s `syncs_ok`/`syncs_fail`/`last_error` plumbing,
  and a no-reply command in the sync path would need its own framing exception
  for no benefit. **Use `:CM` outbound.**

Build this as a pure table + a `command()` that consults it, unit-tested against
a fake transport. No hardware.

---

## 7. Networking — prefer serial, and the AP problem disappears

The hub topology makes the transport choice consequential in a way it was not
before.

**Serial (recommended).** GPIO UART → MAX232 → the mount's RS-232 port — the
transport `mountlink.py` already implements and `install.sh` already provisions
(`enable_uart=1`, `diofinder` in `dialout`, serial console stripped from
`cmdline.txt`, `serial-getty@*` masked). With it, there is exactly **one
wireless network: the finder's own AP.**

- No station mode.
- No conflict with `diofinder-ap-watchdog.service`, which otherwise forces the
  self-AP back up after `DIOFINDER_APWD_GRACE` (30 s) of an idle station link
  and silently splits the finder off the mount's network mid-session.
- No DHCP address to discover — the finder is at its own AP address.
- The phone joins one network and points SkySafari at one IP. That *is* the
  smooth UX.

**TCP (fallback, WiFi-only mounts).** `mount_transport: tcp`, host/port to the
SmartWebServer's LX200 channel (9999 default). Operationally worse: the finder
must `station.sh`-join the mount's AP, giving up its own, which drags in every
issue in `networking.md` §"Mount is AP-only" plus the watchdog conflict above.
If TCP is used, two things become prerequisites rather than niceties:

1. A **hub-mode gate for the AP watchdog** so it does not tear down a
   deliberate station association.
2. **Prominent station-IP display** in the web UI (`networking.md` open action
   #1) so the user can find the finder on the mount's DHCP.

`TcpTransport` also needs an explicit stale-byte drain before each command;
unlike pyserial there is no `reset_input_buffer()`, so a leftover `E9#` from a
previous exchange will be read as this one's result.

---

## 8. Closed-loop GoTo — the payoff, and the one dangerous part

Once the finder sits in the command path:

1. SkySafari requests target **T**.
2. Finder forwards the GoTo; mount slews.
3. Finder waits for settle — reuse `_imu_is_moving`, the dedicated mount-side
   IMU rate state that already exists precisely so mount logic cannot perturb
   the LX200 pointing path.
4. Finder plate-solves → true position **P**.
5. Finder syncs the mount to **P** (`:Sr`/`:Sd`/`:CM#`).
6. Finder re-issues the GoTo to **T**.
7. Repeat until `|P − T| < mount_goto_tolerance_arcmin` or
   `mount_goto_max_iters` is reached.

This converges to the *solver's* accuracy regardless of the mount's pointing
model — arcminutes on a mount that otherwise could not find anything. It is the
highest-value feature available in this system and it is only possible with the
finder in the command path.

**Safety — this breaks an existing invariant, deliberately and visibly.**
`mountlink.py`'s module docstring currently promises:

> *"Sync only — there is no slew/GoTo command in the module; nothing here can
> move the mount."*

That property is load-bearing and greppable. **Keep it.** Closed-loop GoTo goes
in a *separate* module (`diofinder/mount_goto.py`) layered above `mountlink`, so
the sync layer's guarantee stays literally true. Rules for that module:

- Default OFF (`mount_goto_closed_loop: false`), own UI toggle, own status.
- It may only ever re-issue a GoTo to a target **the user just requested via
  `:MS#`**. It must never originate a slew, never slew to a solver-derived
  position, and never retry a target the client did not send.
- Hard iteration cap and a wall-clock budget; on exhaustion, stop and report —
  never loop.
- Abort immediately on any `:Q*` from the client, on solver failure, and on a
  motion signal the finder did not initiate.
- Every iteration logged with T, P, residual, and iteration number.

---

## 9. Config keys

Extends the `mount_*` namespace from `onstep-design.md` §5.

| Key | Default | Live | Notes |
|---|---|---|---|
| `mount_protocol` | `none` | **yes** | `none` / `onstepx` / `synscan`. Promoted to live — see below |
| `mount_transport` | `serial` | **yes** | `serial` / `tcp` |
| `mount_tcp_host` | `""` | **yes** | SmartWebServer address |
| `mount_tcp_port` | `9999` | **yes** | SWS LX200 command channel |
| `mount_hub_enabled` | `false` | yes | master switch for hub mode |
| `mount_hub_forward_unknown` | `true` | yes | §5 unknown-command default |
| `mount_goto_closed_loop` | `false` | yes | §8; independent of `mount_hub_enabled` being on |
| `mount_goto_tolerance_arcmin` | `2.0` | yes | convergence target |
| `mount_goto_max_iters` | `3` | yes | hard cap |
| `mount_goto_settle_s` | `3.0` | yes | post-slew settle before solving |

**`mount_protocol` and the transport keys must become live.** They are currently
restart-only, and `mount_set` explicitly rejects them:

```python
# Transport keys (protocol/port/baud) are restart-level and set in
# the conf, so they are NOT accepted here.
```

A dropdown that needs a restart is not a dropdown. `_MountManager._ensure`
already tears down and rebuilds whenever its key changes, so the work is: widen
that key from `(proto, port, baud)` to include transport/host/tcp_port, and add
these keys to the `mount_set` allowlist and to `_mount_params`.

**Fold `None` into the protocol selector.** `mount_protocol: none` duplicates
`mount_enabled`. Make the dropdown *be* the enable control and derive
`mount_enabled` from it — one control instead of two that can contradict each
other.

---

## 10. Server-side gaps to close while in the file

- **`:CS` is unhandled.** It falls through to the unknown branch and returns
  `b"#"`, so a client that syncs with `:CS#` silently fails to align — with no
  log beyond the one-shot "unhandled command (first seen)" line that was written
  to catch exactly this. Handle it identically to `:CM`.
- **`:MS` lies** (§3). Fix in non-hub mode too.
- **`:GW` is hardcoded `AT2#`.** Fine standalone; wrong in hub mode.

---

## 11. Relationship to the interim posture

If `LX200Link` ships before this hub does, the shippable interim is: direct
connections, **manual sync only**, auto mode not exposed for OnStepX. That is
human-serialized and adequate for a user who presses a button. It is documented
here so the constraint travels with the code:

> **Do not enable `mount_mode: auto` against an OnStepX mount that another
> client is also connected to.** Unattended pushes into a shared target register
> have no human gate. This restriction lifts when hub mode ships.

None of the interim work is wasted — `LX200Link`, `TcpTransport`, the reply-shape
table and the live protocol keys are all prerequisites of the hub.

---

## 12. Failure handling & degraded mode

- **Solver down / no fresh solve.** The hub must keep forwarding. `:GR`/`:GD`
  fall back to the mount's own position rather than a stale held solve, so
  SkySafari sees *something honest*. Surface which source is answering
  (§13) — this is the same provenance problem the `PositionSource` idea from the
  olive-solve review addresses, and the two should share one enum if that lands.
- **Mount link down.** Forwarded commands return an LX200-shaped failure, not a
  hang. `:GR`/`:GD` continue from the solve. The finder remains useful as a
  plate solver with a dead mount link.
- **Finder down.** This is the real cost of the hub: it becomes a single point of
  failure for mount control. Mitigations: the existing solver watchdog +
  systemd restart; a documented fallback (repoint SkySafari at the mount
  directly); and keeping hub mode opt-in so a user who does not want that
  coupling does not get it.
- **Client disconnect mid-sequence.** A buffered `:Sr`/`:Sd` with no following
  action must expire (time-bounded) rather than persist into someone else's
  `:MS`.

---

## 13. Maint commands & web UI

Extend rather than duplicate. `mount_status` gains:

```
hub_enabled, hub_active, commands_forwarded, commands_local,
last_forward: {cmd, reply, t}, goto_closed_loop: {enabled, last_run:
{target, iterations, residual_arcmin, result}}, radec_source
```

Web UI: the existing Camera-page mount card grows a protocol dropdown
(None / OnStepX / SynScan), transport selector with conditional host/port
fields, a hub-mode toggle, and a closed-loop-GoTo toggle gated behind hub mode
with an explicit "this moves your mount" warning inline (per `CLAUDE.md`, safety
warnings stay inline — not behind a `tip()` tooltip). The card heading currently
hardcodes `Mount sync (SynScan)` and the Config-page description of
`mount_protocol` says "synscan (SkyWatcher)" — both become dynamic.

---

## 14. Testing (no hardware)

- **Routing table**: every row in §5 asserted against a fake mount transport —
  local commands never touch it, forwarded commands arrive verbatim and in order.
- **Reply framing**: each shape in §6, including `:MS` accepted vs refused and
  `:SC`'s double-terminated reply. A no-reply command must not stall.
- **Split-connection target buffer**: `:Sr` on connection A, `:Sd` on B, `:MS`
  on C → one correct forwarded triple. This is the v0.11.56 regression, ported
  to the hub.
- **Interleave**: a client GoTo and a finder auto-sync issued concurrently →
  assert two complete, non-interleaved triples on the wire.
- **Closed-loop GoTo**: convergence, iteration cap, abort on `:Q`, abort on
  solve failure, and the invariant that no slew is ever issued to a target the
  client did not send.
- **Degraded mode**: solver down → forwarding continues, `radec_source` reports
  the mount.

---

## 15. Open questions

1. **Does SmartWebServer accept multiple simultaneous IP clients on 9999, and if
   so does it interleave them onto one serial channel?** Affects how loudly to
   warn users who keep a direct SkySafari→mount connection alongside the finder.
   Not blocking — the hub is correct either way — but it determines whether the
   interim posture (§11) is "risky" or "actively broken."
2. **Which position does `mount_sync` push?** `_solved_j2000(sol, ...)` must
   yield the *boresight-corrected telescope* pointing, not the raw camera
   centre, or the mount gets synced to wherever the camera happens to look.
   Confirm before enabling any auto path. (Epoch is already right: `mount_epoch`
   is the mount's own, distinct from `report_epoch`.)
3. **`alignActive()` hijack.** In OnStepX, if the mount is mid-way through its
   own star-alignment procedure, `:CM`/`:CS` add an **align star** instead of
   performing a sync — the same branch, different arm. An auto-push during the
   user's alignment run would silently corrupt it. Gate on `:GW#` alignment
   status before auto-syncing, or document loudly.
4. **Does SkySafari tolerate a `:GW` that changes** (finder `AT2#` → mount's
   real status) mid-session? Probably, but worth one field check.
5. **Baud.** `mount_serial_baud` defaults to 9600. With forwarded traffic added
   to sync traffic, confirm headroom or raise it — OnStepX supports faster.

---

## 16. Build order

1. `LX200Link` + `TcpTransport` + live protocol/transport keys — shared with the
   interim posture, needed regardless. Ship manual-only (§11).
2. Reply-shape table + `command(cmd, shape)` on both transports (§6).
3. Hub routing (§5) with the module-level target buffer, behind
   `mount_hub_enabled`, default off.
4. Server-side gaps (§10) — small, independent, do them any time.
5. Closed-loop GoTo (§8) as `mount_goto.py`, default off, after 3 is stable
   on-sky.
