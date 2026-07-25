#!/usr/bin/env python3
"""Log the pointing actually reported to SkySafari, over time.

Samples TWO sources in lockstep and writes a CSV plus a summary report:

* ``:GR#`` / ``:GD#`` over the LX200 TCP port — the EXACT values a planetarium
  client receives, including any IMU extrapolation, the poll-snapshot TTL and
  the JNow conversion. This is the ground truth for "what did the reticle do".
* the ``status`` maintenance command — whose ``report_ra_deg``/``report_dec_deg``
  are computed straight from the plate solve (``comms_proc`` precesses
  ``sol["ra_deg"]`` directly and never calls ``_imu_predict``), plus
  ``solved`` / ``stars`` / ``matches`` / ``pointing_age_s``.

Because the maint side is solve-only, the angular difference between the two is
a direct measurement of **how far the IMU is moving the crosshair away from the
last solve** — the question the `imu_predict_hold_solve_s` experiment asks.

The derived metrics answer the two field experiments:

* **stair-stepping** — ``repeat_frac`` (polls returning a byte-identical
  position because no new solve landed) and the median step between distinct
  positions. High repeat fraction + large steps = the crosshair is hopping once
  per solve instead of moving smoothly.
* **oscillation** — ``reversals``: steps that move *against* the overall
  direction of travel during a one-way slew. A steady slew should be monotonic;
  reversals are the v0.11.63 IMU-hunting signature.

Usage (on the device, while slewing)::

    sudo python3 /opt/diofinder/tests/pointing_log.py --seconds 60 \
        --label "ratio0-hold045" --out /var/lib/diofinder/pointing_a.csv

    # Re-analyse a saved run offline (no daemon needed):
    python3 tests/pointing_log.py --analyze pointing_a.csv

Sampling defaults mirror SkySafari (4 Hz LX200 polls). The maint side defaults
to 2 Hz because ``status`` is the heavier call on the Pi's shared CPU 0.
"""
import argparse
import csv
import datetime
import math
import re
import socket
import statistics
import sys
import time

# LX200 quantisation is 1 s of RA (15") and 1" of Dec, so two positions closer
# than this are indistinguishable on the wire. Offsets below it carry no
# information about the IMU.
_LX200_QUANT_DEG = 15.0 / 3600.0
# An LX200-vs-solve offset above this counts as "the IMU is steering".
_IMU_OFFSET_DEG = 0.02
# Ignore reversals smaller than this — they are quantisation, not oscillation.
_REVERSAL_FLOOR_DEG = 0.02

_RA_RE = re.compile(r"(\d+):(\d+):(\d+)")
_DEC_RE = re.compile(r"([+-]?\d+)[*:](\d+):(\d+)")


# ---- pure helpers (unit-tested) ----------------------------------------------

def parse_ra(s):
    """LX200 'HH:MM:SS#' -> degrees, or None if unparseable."""
    if not s:
        return None
    m = _RA_RE.search(s)
    if not m:
        return None
    h, mi, sec = (int(g) for g in m.groups())
    return (h + mi / 60.0 + sec / 3600.0) * 15.0


def parse_dec(s):
    """LX200 '+DD*MM:SS#' -> degrees, or None if unparseable."""
    if not s:
        return None
    m = _DEC_RE.search(s)
    if not m:
        return None
    d_s, mi, sec = m.groups()
    d = int(d_s)
    sign = -1.0 if d_s.strip().startswith("-") else 1.0
    return sign * (abs(d) + int(mi) / 60.0 + int(sec) / 3600.0)


def angsep_deg(ra1, dec1, ra2, dec2):
    """Great-circle separation in degrees (None-safe -> None)."""
    if None in (ra1, dec1, ra2, dec2):
        return None
    r1, d1, r2, d2 = (math.radians(v) for v in (ra1, dec1, ra2, dec2))
    v = (math.sin(d1) * math.sin(d2)
         + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2))
    return math.degrees(math.acos(max(-1.0, min(1.0, v))))


def _tangent_step(ra1, dec1, ra2, dec2):
    """(dRA*cos(dec), dDec) in degrees — a local flat-sky step vector."""
    dra = (ra2 - ra1 + 180.0) % 360.0 - 180.0
    return (dra * math.cos(math.radians((dec1 + dec2) / 2.0)), dec2 - dec1)


def summarize(records, imu_offset_deg=_IMU_OFFSET_DEG,
              reversal_floor_deg=_REVERSAL_FLOOR_DEG):
    """Reduce sampled records to the metrics the two experiments need.

    Pure — no I/O, no daemon. ``records`` is a list of dicts as written to the
    CSV (see ``sample_once``). Returns a dict of metrics; keys are None/0 when
    a metric is not measurable from the data.
    """
    out = {
        "n": len(records), "duration_s": 0.0, "actual_hz": None,
        "n_lx": 0, "repeat_frac": None, "n_distinct": 0,
        "step_median_deg": None, "step_max_deg": None,
        "travel_deg": 0.0, "rate_dps": None,
        "reversals": 0, "reversal_frac": None, "reversal_max_deg": None,
        "solved_frac": None, "stars_median": None, "matches_median": None,
        "dropout_max_s": 0.0,
        "age_median_s": None, "age_p95_s": None, "age_max_s": None,
        "imu_active_frac": None, "imu_offset_median_deg": None,
        "imu_offset_max_deg": None,
    }
    if not records:
        return out

    ts = [r["t"] for r in records]
    out["duration_s"] = round(ts[-1] - ts[0], 2)
    if out["duration_s"] > 0:
        out["actual_hz"] = round((len(records) - 1) / out["duration_s"], 2)

    # ---- LX200 track: stair-stepping + oscillation ----------------------
    pts = [(r["t"], r["lx_ra"], r["lx_dec"]) for r in records
           if r.get("lx_ra") is not None and r.get("lx_dec") is not None]
    out["n_lx"] = len(pts)
    if len(pts) >= 2:
        repeats = 0
        steps = []          # (dt, sep_deg, vec) between CONSECUTIVE samples
        for (t0, ra0, d0), (t1, ra1, d1) in zip(pts, pts[1:]):
            sep = angsep_deg(ra0, d0, ra1, d1)
            if sep is not None and sep < _LX200_QUANT_DEG:
                repeats += 1
            else:
                steps.append((t1 - t0, sep, _tangent_step(ra0, d0, ra1, d1)))
        out["repeat_frac"] = round(repeats / (len(pts) - 1), 3)
        out["n_distinct"] = len(steps)
        if steps:
            seps = [s for _, s, _ in steps]
            out["step_median_deg"] = round(statistics.median(seps), 4)
            out["step_max_deg"] = round(max(seps), 4)
            out["travel_deg"] = round(sum(seps), 3)
            # Rate over the WHOLE window, not per-step: while stair-stepping a
            # step carries a full solve-interval of motion but is separated
            # from its predecessor by only one poll gap, so step/dt
            # overestimates the true slew rate (measured 1.4 vs a true 1.0).
            span = pts[-1][0] - pts[0][0]
            if span > 0:
                out["rate_dps"] = round(sum(seps) / span, 3)

            # Reversals: project each step onto the overall direction of
            # travel. A one-way slew is monotonic; a step with a materially
            # negative projection moved backwards = oscillation.
            gx = sum(v[0] for _, _, v in steps)
            gy = sum(v[1] for _, _, v in steps)
            gnorm = math.hypot(gx, gy)
            if gnorm > 0:
                ux, uy = gx / gnorm, gy / gnorm
                backs = [-(v[0] * ux + v[1] * uy) for _, _, v in steps]
                bad = [b for b in backs if b > reversal_floor_deg]
                out["reversals"] = len(bad)
                out["reversal_frac"] = round(len(bad) / len(steps), 3)
                out["reversal_max_deg"] = round(max(bad), 4) if bad else 0.0

    # ---- solver side ----------------------------------------------------
    solved = [r for r in records if r.get("solved") is not None]
    if solved:
        out["solved_frac"] = round(
            sum(1 for r in solved if r["solved"]) / len(solved), 3)
        st = [r["stars"] for r in solved if r.get("stars") is not None]
        mt = [r["matches"] for r in solved if r.get("matches") is not None]
        if st:
            out["stars_median"] = round(statistics.median(st), 1)
        if mt:
            out["matches_median"] = round(statistics.median(mt), 1)
        # Longest continuous unsolved stretch (the freeze the reticle sees).
        run_start = None
        worst = 0.0
        for r in solved:
            if not r["solved"]:
                run_start = r["t"] if run_start is None else run_start
                worst = max(worst, r["t"] - run_start)
            else:
                run_start = None
        out["dropout_max_s"] = round(worst, 2)

    ages = [r["age_s"] for r in records if r.get("age_s") is not None]
    if ages:
        srt = sorted(ages)
        out["age_median_s"] = round(statistics.median(srt), 2)
        out["age_p95_s"] = round(srt[min(len(srt) - 1,
                                         int(0.95 * len(srt)))], 2)
        out["age_max_s"] = round(srt[-1], 2)

    # ---- IMU involvement: LX200 (may be predicted) vs solve-only --------
    offs = []
    for r in records:
        o = angsep_deg(r.get("lx_ra"), r.get("lx_dec"),
                       r.get("solve_ra"), r.get("solve_dec"))
        if o is not None:
            offs.append(o)
    if offs:
        out["imu_active_frac"] = round(
            sum(1 for o in offs if o > imu_offset_deg) / len(offs), 3)
        out["imu_offset_median_deg"] = round(statistics.median(offs), 4)
        out["imu_offset_max_deg"] = round(max(offs), 4)
    return out


def format_report(summary, label=None):
    """Human-readable interpretation of a summarize() dict."""
    s = summary

    def v(key, unit="", nd=None):
        """Render a metric, or '--' when it wasn't measurable."""
        x = s.get(key)
        if x is None:
            return "--"
        if nd is not None and isinstance(x, float):
            x = round(x, nd)
        return f"{x}{unit}"

    L = []
    L.append("=" * 62)
    L.append(f"pointing_log summary{f'  [{label}]' if label else ''}")
    L.append("=" * 62)
    L.append(f"samples        : {s['n']}  over {s['duration_s']}s "
             f"({s['actual_hz']} Hz)   LX200 replies: {s['n_lx']}")
    L.append("")
    L.append("-- solving --")
    L.append(f"solved         : {v('solved_frac')}   "
             f"stars {v('stars_median')}  matches {v('matches_median')}")
    L.append(f"longest dropout: {v('dropout_max_s', 's')} "
             "(crosshair frozen this long)")
    L.append(f"solution age   : median {v('age_median_s', 's')}  "
             f"p95 {v('age_p95_s', 's')}  max {v('age_max_s', 's')}")
    L.append("")
    L.append("-- reticle motion (what SkySafari saw) --")
    L.append(f"travel         : {v('travel_deg', ' deg')}   "
             f"rate {v('rate_dps', ' deg/s')}")
    L.append(f"repeat_frac    : {v('repeat_frac')}  "
             "(polls returning an IDENTICAL position)")
    L.append(f"step           : median {v('step_median_deg', ' deg')}   "
             f"max {v('step_max_deg', ' deg')}")
    L.append(f"reversals      : {v('reversals')} "
             f"({v('reversal_frac')})   worst {v('reversal_max_deg', ' deg')}")
    L.append("")
    L.append("-- IMU involvement (LX200 report vs solve-only) --")
    L.append(f"imu_active     : {v('imu_active_frac')}  of samples steered "
             "away from the last solve")
    L.append(f"offset         : median {v('imu_offset_median_deg', ' deg')}   "
             f"max {v('imu_offset_max_deg', ' deg')}")
    L.append("")
    L.append("-- reading it --")
    rf, rev = s.get("repeat_frac"), s.get("reversals")
    if rf is not None and rf >= 0.25:
        L.append(f"* STAIR-STEPPING: {rf:.0%} of polls repeated the previous "
                 f"position, then hopped {s['step_median_deg']} deg. The "
                 "report is quantised to the solve cadence.")
    elif rf is not None:
        L.append(f"* Motion is smooth ({rf:.0%} repeats) — the report is "
                 "updating between solves.")
    if rev:
        L.append(f"* OSCILLATION: {rev} step(s) moved AGAINST the overall "
                 f"direction (worst {s['reversal_max_deg']} deg). On a one-way "
                 "slew this is the IMU-hunting signature — raise "
                 "imu_predict_hold_solve_s.")
    else:
        L.append("* No reversals — no oscillation in this run.")
    if s.get("imu_active_frac") == 0.0:
        L.append("* The IMU never moved the crosshair (solves only). If you "
                 "expected interpolation, imu_predict_hold_solve_s is still "
                 "masking it.")
    if s.get("dropout_max_s", 0) >= 1.0:
        L.append(f"* Solves dropped out for up to {s['dropout_max_s']}s — "
                 "detection is failing (check trailing / max_axis_ratio).")
    return "\n".join(L)


# ---- I/O layer ----------------------------------------------------------------

class LX200Client:
    """Minimal LX200 client. ``reconnect=True`` opens a fresh TCP connection
    per query, mimicking SkySafari's connection-per-poll behaviour."""

    def __init__(self, host="127.0.0.1", port=4060, timeout=2.0,
                 reconnect=False):
        self.host, self.port = host, port
        self.timeout, self.reconnect = timeout, reconnect
        self._sock = None

    def _connect(self):
        s = socket.create_connection((self.host, self.port), self.timeout)
        s.settimeout(self.timeout)
        return s

    def query(self, cmd):
        """Send one command, return the '#'-terminated reply (str) or None."""
        try:
            if self._sock is None or self.reconnect:
                self.close()
                self._sock = self._connect()
            self._sock.sendall(cmd.encode("ascii"))
            buf = b""
            while b"#" not in buf and len(buf) < 128:
                chunk = self._sock.recv(64)
                if not chunk:
                    break
                buf += chunk
            if self.reconnect:
                self.close()
            return buf.decode("ascii", "replace") or None
        except OSError:
            self.close()
            return None

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


def sample_once(lx_query, maint_call, t0, want_maint=True):
    """One (LX200 + optional status) sample -> a record dict."""
    rec = {
        "t": round(time.monotonic() - t0, 3),
        "wall": datetime.datetime.now().isoformat(timespec="milliseconds"),
        "lx_ra": None, "lx_dec": None,
        "solve_ra": None, "solve_dec": None,
        "solved": None, "stars": None, "matches": None,
        "age_s": None, "stale": None, "seq": None,
    }
    rec["lx_ra"] = parse_ra(lx_query(":GR#"))
    rec["lx_dec"] = parse_dec(lx_query(":GD#"))
    if want_maint and maint_call is not None:
        try:
            r = maint_call("status", {}, timeout=5.0)
            if getattr(r, "ok", False):
                res = r.result or {}
                sol = res.get("solution", {}) or {}
                rec["solve_ra"] = sol.get("report_ra_deg")
                rec["solve_dec"] = sol.get("report_dec_deg")
                rec["solved"] = bool(sol.get("solved"))
                rec["stars"] = sol.get("stars")
                rec["matches"] = sol.get("matches")
                rec["seq"] = sol.get("seq")
                rec["age_s"] = res.get("pointing_age_s")
                rec["stale"] = res.get("pointing_stale")
        except Exception:
            pass
    return rec


FIELDS = ["t", "wall", "lx_ra", "lx_dec", "solve_ra", "solve_dec",
          "solved", "stars", "matches", "age_s", "stale", "seq"]


def run_log(seconds, hz=4.0, maint_hz=2.0, lx_query=None, maint_call=None,
            progress=None):
    """Sample for `seconds` and return the record list. Injectable for tests."""
    if maint_call is None:
        from diofinder.maint import call as maint_call  # noqa: PLW0642
    period = 1.0 / max(0.1, hz)
    maint_stride = max(1, int(round(hz / max(0.1, maint_hz))))
    t0 = time.monotonic()
    end = t0 + seconds
    recs = []
    i = 0
    while time.monotonic() < end:
        loop_t = time.monotonic()
        recs.append(sample_once(lx_query, maint_call, t0,
                                want_maint=(i % maint_stride == 0)))
        i += 1
        if progress and i % max(1, int(hz * 5)) == 0:
            progress(f"{int(end - time.monotonic())}s left, {len(recs)} samples")
        slack = period - (time.monotonic() - loop_t)
        if slack > 0:
            time.sleep(slack)
    return recs


def write_csv(path, records):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for r in records:
            w.writerow({k: r.get(k) for k in FIELDS})


def read_csv(path):
    def num(v):
        if v in ("", "None", None):
            return None
        try:
            return float(v)
        except ValueError:
            return {"True": True, "False": False}.get(v, v)
    out = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            rec = {k: num(v) for k, v in row.items()}
            rec["wall"] = row.get("wall")
            for k in ("solved", "stale"):
                if isinstance(rec.get(k), float):
                    rec[k] = bool(rec[k])
            out.append(rec)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=60.0,
                    help="sampling duration (default 60)")
    ap.add_argument("--hz", type=float, default=4.0,
                    help="LX200 poll rate, SkySafari-like (default 4)")
    ap.add_argument("--maint-hz", type=float, default=2.0,
                    help="status sample rate (default 2; heavier call)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4060)
    ap.add_argument("--reconnect", action="store_true",
                    help="new TCP connection per poll (mimics SkySafari)")
    ap.add_argument("--out", help="write samples to this CSV")
    ap.add_argument("--label", help="label for the printed report")
    ap.add_argument("--analyze", metavar="CSV",
                    help="re-analyse a saved CSV and exit (no daemon needed)")
    a = ap.parse_args(argv)

    if a.analyze:
        recs = read_csv(a.analyze)
        print(format_report(summarize(recs), a.label or a.analyze))
        return 0

    lx = LX200Client(a.host, a.port, reconnect=a.reconnect)
    probe = lx.query(":GR#")
    if probe is None:
        print(f"ERROR: no LX200 reply from {a.host}:{a.port} — is the daemon "
              "running?", file=sys.stderr)
        return 2
    print(f"Sampling {a.seconds:.0f}s at {a.hz} Hz "
          f"(status {a.maint_hz} Hz){'  [reconnect]' if a.reconnect else ''}…")
    print("Keep the scope MOVING at a steady rate for the whole window.")
    try:
        recs = run_log(a.seconds, a.hz, a.maint_hz, lx.query,
                       progress=lambda m: print("  " + m))
    finally:
        lx.close()
    if a.out:
        write_csv(a.out, recs)
        print(f"wrote {len(recs)} samples -> {a.out}")
    print(format_report(summarize(recs), a.label))
    return 0


if __name__ == "__main__":
    sys.exit(main())
