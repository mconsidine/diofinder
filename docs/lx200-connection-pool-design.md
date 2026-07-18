# LX200 server — fixed worker pool (absorb the SkySafari reconnect storm)

Design note for replacing the thread-per-connection LX200 server with a fixed
worker pool, so a client that opens a new TCP connection per poll costs (almost)
nothing. Implemented in v0.11.58.

## Problem

SkySafari does **not** hold one connection and poll it — with a "readout rate"
of N Hz it opens a **brand-new TCP connection N times per second**, sends its
`:GR`/`:GD` poll, reads the reply, and closes. Confirmed from field bundles:
244 connections in 58 s (~4/s at readout rate 4), all fresh ephemeral ports,
**zero** diofinder-side recv timeouts (the client closes each one cleanly). This
is a SkySafari-side behaviour — reproduced by the user across multiple projects,
not specific to diofinder — and not something a user can switch off (only the
readout rate is exposed, and lowering it trades away crosshair smoothness).

diofinder tolerates it correctly (the shared align target, v0.11.56, is what
makes `:Sr`/`:Sd`/`:CM#` work across the churn; no cap-drops occur). The cost is
CPU, not correctness: the server **spawns and tears down a whole thread per
connection** (v0.11.52), so at readout rate 4 that's ~4 thread create/join +
TCP setup/teardown cycles per second on the **Pi Zero 2W's CPU 0** — which is
shared with the web UI. That incidental load is behind the transient
"camera unavailable" frame misses and crosshair jitter seen in the field.

## Why the current design is thread-per-connection

v0.11.52 moved from a single-connection server to one thread per connection to
fix a **real** failure: a blocking `:CM#` align (holds up to ~13 s waiting for a
solve) or a half-open phone (blocks in `recv` until the 30 s timeout) would
otherwise stall the single accept/serve loop and starve every other client's
`:GR`/`:GD` polls — the poll-timeout → reconnect → broken-pipe storm. That
isolation must be preserved: **a blocking connection must never starve the
polls.** A `BoundedSemaphore(8)` caps concurrency; excess connections are dropped
(they're almost always stale half-open sockets a phone left behind).

## New design — fixed worker pool

Keep the isolation, drop the per-connection thread churn:

- **N long-lived worker threads** created once at server start (`_LX200_POOL_WORKERS = 8`).
- The **accept loop** does nothing but `accept()` → `_lx200_note_connection()`
  (storm telemetry) → `work_q.put_nowait((client, addr))`.
- Each **worker** blocks on `work_q.get()`, serves that connection to completion
  with the *unchanged* per-connection handler (`_serve_lx200_client`: sockopts,
  the `recv`/`#`-split/`_handle_lx200_command`/`sendall` loop, shared
  `_lx200_align_state`), closes it, and loops back for the next.
- The **queue is bounded** (`_LX200_QUEUE_MAX = 16`); on overflow the newcomer is
  closed and dropped — the same shed-load behaviour as the old semaphore cap,
  for the same reason (a runaway/half-open client must not grow the backlog
  without bound).

No thread is created or destroyed per connection. The storm becomes: accept,
enqueue, a worker dequeues and handles it in well under the inter-arrival time,
back to the pool. Thread lifecycle cost → zero.

### Invariant: blocking still can't starve polls

A blocking connection now occupies **one worker** for its duration (a `:CM#`
align ~13 s; a half-open phone up to the 30 s `lx200_client_timeout_s`), exactly
as it occupied one thread before. With 8 workers, up to 8 simultaneously
blocking connections are tolerated before polls queue — the **same threshold**
as the old cap of 8. The dominant blocking sources are one aligning client and a
handful of stale phone sockets, well under 8, so the free workers keep draining
the poll storm. The FIFO queue means an align that lands mid-burst waits behind
the polls (drained in well under a second), then holds its worker; other workers
serve the ongoing polls throughout.

### Sizing

- `_LX200_POOL_WORKERS = 8` — preserves the v0.11.52 concurrency/robustness
  threshold. 8 workers × short-lived poll handling (<100 ms each) ≫ any real
  readout rate, with headroom for a blocking align + stale sockets.
- `_LX200_QUEUE_MAX = 16` — absorbs a burst if every worker is momentarily busy
  without unbounded growth; overflow drops (logged, throttled).

## Behaviour preserved (a checklist for the reviewer)

- Per-connection handling byte-identical (`_serve_lx200_client` body unchanged
  except it no longer touches a semaphore).
- Concurrency cap unchanged in spirit (8 concurrent) and overload still sheds.
- Shared align target, storm telemetry, sockopts, 4 KB no-`#` guard, recv
  timeout, clean close — all unchanged.
- Only the *dispatch* changes: pool + queue instead of spawn-per-connection.

## Alternatives considered

- **Lower the SkySafari readout rate** (user-side): works, but compromises
  crosshair smoothness and asks the user to work around a client bug. Rejected
  as *the* answer; still available as a knob.
- **Single-threaded `selectors`/async server**: would also kill the churn and
  scale further, but it's a full rewrite of the serve loop (every `recv`/`send`
  becomes non-blocking state), and the blocking `:CM#` align exchange
  (`_do_alignment` waits on the solver queues) would need to become async too —
  disproportionate risk for a server that handles a handful of clients. The pool
  keeps the existing, well-understood blocking handler verbatim.
- **Just raise the thread cap**: doesn't help — the cost is thread lifecycle
  churn, not the concurrency ceiling.

## Risk / testing

Pointing-critical path. The change is deliberately minimal and
behaviour-preserving (same handler, pooled dispatch). The socket/threading path
isn't unit-testable in the hardware-free suite (the pre-existing threaded server
wasn't either); validated by byte-compile + review + on-device use. Watch a
field bundle after deploy: the storm WARNING should still fire, but there should
be **no** per-connection thread names churning and fewer/no `camera unavailable`
frame misses under a 4 Hz readout.
