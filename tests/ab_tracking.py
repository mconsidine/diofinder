#!/usr/bin/env python3
"""On-device A/B for ROI tracking mode: measure FULL vs TRACKING on the live sky.

Tracking mode (verify-only solve + ROI detection, olive-solve >= 0.1.6 +
sycamore >= 0.14) is default-OFF pending on-sky validation. This harness runs
that validation *in flight* at the telescope: it drives the live daemon through
a FULL window and a TRACKING window on the same field, then reports the solve
rate, latency, match count, and — the gate that matters — whether TRACKING's
pointing agrees with FULL's.

It changes only the LIVE tracking_enabled flag (never persisted) and restores
it on exit, so it is safe to run against a working finder.

────────────────────────────────────────────────────────────────────────────
RUN IT (at the telescope, scope pointed at stars)
────────────────────────────────────────────────────────────────────────────
  1. Point at a star field and FOCUS until the Home page shows a solve
     (steady RA/Dec, matches > 0). Tracking only helps once you are solving.
  2. Keep the scope STATIONARY for the test — tracking is a steady-state
     optimization and falls back to FULL during a slew, so a moving scope
     makes the comparison meaningless. (No sidereal tracking needed; the
     harness tolerates sidereal drift.)
  3. On the Pi:
         sudo python3 /opt/diofinder/tests/ab_tracking.py --window 30
     or  diofinder-ctl ab-tracking --window 30
  4. Read the verdict. CORRECTNESS must be PASS. If it is and TRACKING is
     faster / higher-rate, enable it persistently:
         diofinder-ctl raw '{"cmd":"solver_params_set","args":{"tracking_enabled":true,"persist":true}}'
     Otherwise leave it off and note the reason in a report.

Exit code 0 = ran and TRACKING is safe to enable; 1 = correctness FAIL or a
precondition wasn't met (not solving, tracking never locked, scope moving).
"""
import argparse
import math
import statistics
import sys
import time


# ── pure stats (unit-tested; no socket) ─────────────────────────────────────

# record = (epoch, tracked_bool, solve_ms, extract_ms, matches, ra, dec)
def _summarize(records):
    """Aggregate a list of solve records into medians + spreads."""
    if not records:
        return {"n": 0}
    solve = [r[2] for r in records]
    extract = [r[3] for r in records]
    matches = [r[4] for r in records]
    ra = [r[5] for r in records]
    dec = [r[6] for r in records]

    def p(xs, q):
        xs = sorted(xs)
        if len(xs) == 1:
            return xs[0]
        i = min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))
        return xs[i]

    # Circular median + spread: a plain median of RA blows up across the
    # 0/360 wrap (median([359.9, 0.1]) = 180, the antipode). Anchor on the
    # first sample, take the median of wrapped residuals, then re-center.
    anchor = ra[0]
    res0 = [((v - anchor + 180.0) % 360.0) - 180.0 for v in ra]
    ra0 = (anchor + statistics.median(res0)) % 360.0
    ra_res = [((v - ra0 + 180.0) % 360.0) - 180.0 for v in ra]
    return {
        "n": len(records),
        "solve_ms_p50": statistics.median(solve),
        "solve_ms_p90": p(solve, 0.9),
        "extract_ms_p50": statistics.median(extract),
        "matches_p50": statistics.median(matches),
        "ra_p50": ra0,
        "dec_p50": statistics.median(dec),
        "ra_spread": (statistics.pstdev(ra_res) if len(ra_res) > 1 else 0.0),
        "dec_spread": (statistics.pstdev(dec) if len(dec) > 1 else 0.0),
    }


def _sky_offset_arcmin(full, track):
    """Great-circle-ish offset between the two median pointings, arcminutes."""
    if not full.get("n") or not track.get("n"):
        return None
    dec = math.radians((full["dec_p50"] + track["dec_p50"]) / 2.0)
    dra = ((track["ra_p50"] - full["ra_p50"] + 180.0) % 360.0 - 180.0) * math.cos(dec)
    ddec = track["dec_p50"] - full["dec_p50"]
    return math.hypot(dra, ddec) * 60.0


def _verdict(full, track, full_rate, track_rate,
             offset_gate_arcmin=18.0, spread_gate_deg=0.5):
    """Correctness + performance verdict from the two summaries and rates.

    Correctness PASS requires: TRACKING produced solves, its median pointing
    is within ``offset_gate_arcmin`` of FULL's (generous vs sidereal drift over
    the test but far below a wrong-field jump), and its positional spread is
    not pathologically larger than FULL's (a sign of intermittent bad solves).
    """
    reasons = []
    ok = True
    if not track.get("n"):
        return {"correctness_ok": False, "recommend": "keep-off",
                "reasons": ["TRACKING produced no solves in the window"]}

    offset = _sky_offset_arcmin(full, track)
    if offset is None:
        ok = False
        reasons.append("missing FULL or TRACKING samples")
    elif offset > offset_gate_arcmin:
        ok = False
        reasons.append(
            f"TRACKING pointing is {offset:.1f}' from FULL "
            f"(> {offset_gate_arcmin:.0f}' gate) — divergent solutions")
    # Spread blow-up: tracking scattering means intermittent wrong solves.
    if (full.get("n") and track["ra_spread"] > spread_gate_deg
            and track["ra_spread"] > 4.0 * max(full["ra_spread"], 1e-4)):
        ok = False
        reasons.append(
            f"TRACKING RA spread {track['ra_spread']:.3f} deg is >4x FULL's "
            "— unstable pointing")

    speedup_pct = None
    if full.get("solve_ms_p50") and track.get("solve_ms_p50"):
        speedup_pct = 100.0 * (full["solve_ms_p50"] - track["solve_ms_p50"]) \
            / full["solve_ms_p50"]
    rate_ratio = (track_rate / full_rate) if full_rate else None

    faster = (speedup_pct is not None and speedup_pct > 5.0)
    higher_rate = (rate_ratio is not None and rate_ratio > 1.05)
    if ok and (faster or higher_rate):
        recommend = "enable"
        if faster:
            reasons.append(f"solve latency down {speedup_pct:.0f}%")
        if higher_rate:
            reasons.append(f"solve rate up {(rate_ratio - 1) * 100:.0f}%")
    elif ok:
        recommend = "neutral"
        reasons.append("correct but no material speed/rate gain")
    else:
        recommend = "keep-off"

    return {"correctness_ok": ok, "recommend": recommend, "offset_arcmin": offset,
            "speedup_pct": speedup_pct, "rate_ratio": rate_ratio,
            "reasons": reasons}


# ── on-device driver (maint socket) ─────────────────────────────────────────

def run_ab(window_s, settle_s, lock_timeout_s, maint_call=None, progress=None):
    """Drive the FULL vs TRACKING A/B and return a structured result dict.

    Reused by both the CLI (below) and the web-UI worker, so it does NOT
    print — it calls the optional ``progress(str)`` callback and returns a
    dict. It captures the live ``tracking_enabled`` and RESTORES it before
    returning (even on error / precondition abort).

    Returns::

        {"ok": bool, "error": str|None, "aborted": str|None,
         "full": summary, "track": summary, "full_rate", "track_rate",
         "n_fellback": int, "verdict": {...}, "window_s": ...}
    """
    if maint_call is None:
        from diofinder.maint import call as maint_call  # noqa: PLW0642

    def m(cmd, args=None, timeout=10.0):
        r = maint_call(cmd, args, timeout=timeout)
        if not r.ok:
            raise RuntimeError(f"{cmd} failed: {r.error}")
        return r.result

    def emit(msg):
        if progress:
            progress(msg)

    def collect(phase_name):
        mark = m("solve_stats").get("now")
        t_end = time.monotonic() + window_s
        while time.monotonic() < t_end:
            emit(f"{phase_name}: measuring… {max(0, int(t_end - time.monotonic()))}s left")
            time.sleep(0.5)
        res = m("solve_stats", {"after": mark})
        elapsed = max(1e-3, res.get("now") - mark)
        recs = [tuple(r) for r in res.get("records", [])]
        return recs, elapsed

    result = {"ok": False, "error": None, "aborted": None, "window_s": window_s,
              "full": {"n": 0}, "track": {"n": 0}, "full_rate": 0.0,
              "track_rate": 0.0, "n_fellback": 0, "verdict": None}
    orig = bool(m("tracking_status").get("enabled", False))
    try:
        # ── Phase A: FULL ──
        emit("Phase A — FULL (tracking OFF)…")
        m("solver_params_set", {"tracking_enabled": False})
        time.sleep(settle_s)
        full_recs, full_elapsed = collect("FULL")
        if not full_recs:
            result["aborted"] = ("Not solving in FULL mode. Point at a star "
                                 "field, focus until the Home page shows a "
                                 "solve, keep the scope stationary, and retry.")
            return result
        result["full_rate"] = len(full_recs) / full_elapsed
        result["full"] = full = _summarize(full_recs)
        if full["ra_spread"] > 1.0:
            result["aborted"] = (f"FULL RA spread is {full['ra_spread']:.2f} deg "
                                 "— the scope appears to be moving. Keep it "
                                 "stationary and retry.")
            return result

        # ── Phase B: TRACKING ──
        emit("Phase B — TRACKING (tracking ON), waiting for lock-in…")
        m("solver_params_set", {"tracking_enabled": True})
        t_lock = time.monotonic() + lock_timeout_s
        locked = False
        while time.monotonic() < t_lock:
            if m("tracking_status").get("state") == "TRACKING":
                locked = True
                break
            time.sleep(0.5)
        if not locked:
            result["aborted"] = (
                f"Tracking never locked within {lock_timeout_s:.0f}s. Likely "
                "too few recoverable stars — check tracking_min_recover vs the "
                "live star count, or the wheel versions (needs olive-solve "
                ">= 0.1.6, sycamore >= 0.14).")
            return result
        track_recs, track_elapsed = collect("TRACKING")
        result["track_rate"] = len(track_recs) / track_elapsed
        tracked = [r for r in track_recs if r[1]]
        result["n_fellback"] = len(track_recs) - len(tracked)
        result["track"] = track = _summarize(tracked)

        result["verdict"] = _verdict(full, track, result["full_rate"],
                                     result["track_rate"])
        result["ok"] = True
        return result
    except RuntimeError as e:
        result["error"] = str(e)
        return result
    finally:
        # Always restore the live tracking state.
        try:
            maint_call("solver_params_set", {"tracking_enabled": orig})
        except Exception:
            pass
        result["restored_to"] = orig


def _print_report(res):
    if res.get("aborted"):
        print(f"\nABORT: {res['aborted']}")
        return
    if res.get("error"):
        print(f"\nERROR: {res['error']}")
        return
    full, track = res["full"], res["track"]
    v = res["verdict"]

    def row(label, a, b):
        print(f"  {label:<22} {a:>14}  {b:>14}")
    print(f"\n  FULL: {full.get('n', 0)} solves @ {res['full_rate']:.2f}/s   "
          f"TRACKING: {track.get('n', 0)} solves @ {res['track_rate']:.2f}/s"
          + (f"  ({res['n_fellback']} fell back to FULL)"
             if res.get("n_fellback") else ""))
    print("\n" + "=" * 54)
    print(f"  {'metric':<22} {'FULL':>14}  {'TRACKING':>14}")
    print("  " + "-" * 50)
    row("solves/sec", f"{res['full_rate']:.2f}", f"{res['track_rate']:.2f}")
    row("solve ms (p50)", f"{full.get('solve_ms_p50', 0):.2f}",
        f"{track.get('solve_ms_p50', 0):.2f}")
    row("solve ms (p90)", f"{full.get('solve_ms_p90', 0):.2f}",
        f"{track.get('solve_ms_p90', 0):.2f}")
    row("extract ms (p50)", f"{full.get('extract_ms_p50', 0):.2f}",
        f"{track.get('extract_ms_p50', 0):.2f}")
    row("matches (p50)", f"{full.get('matches_p50', 0):.0f}",
        f"{track.get('matches_p50', 0):.0f}")
    row("RA spread (deg)", f"{full.get('ra_spread', 0):.4f}",
        f"{track.get('ra_spread', 0):.4f}")
    print("=" * 54)
    if v.get("offset_arcmin") is not None:
        print(f"  TRACKING vs FULL pointing offset: {v['offset_arcmin']:.1f} arcmin")
    print(f"\n  CORRECTNESS: {'PASS' if v['correctness_ok'] else 'FAIL'}")
    print(f"  RECOMMEND:   {v['recommend'].upper()}")
    for r in v["reasons"]:
        print(f"    - {r}")
    if v["recommend"] == "enable":
        print("\n  Enable persistently:")
        print("    diofinder-ctl raw '{\"cmd\":\"solver_params_set\","
              "\"args\":{\"tracking_enabled\":true,\"persist\":true}}'")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--window", type=float, default=30.0,
                    help="measurement seconds per phase (default 30)")
    ap.add_argument("--settle", type=float, default=3.0,
                    help="settle seconds after a mode switch (default 3)")
    ap.add_argument("--lock-timeout", type=float, default=15.0,
                    help="max seconds to wait for TRACKING lock-in (default 15)")
    args = ap.parse_args(argv)

    try:
        res = run_ab(args.window, args.settle, args.lock_timeout,
                     progress=lambda s: print(s))
        _print_report(res)
        code = 0 if (res.get("ok") and res["verdict"]["correctness_ok"]) else 1
        if res.get("restored_to") is not None:
            print(f"\nrestored tracking_enabled = {res['restored_to']}")
    except RuntimeError as e:
        print(f"\nERROR talking to the daemon: {e}\n"
              "Is diofinder running? (sudo systemctl status diofinder)")
        code = 1
    except KeyboardInterrupt:
        print("\ninterrupted")
        code = 1
    return code


if __name__ == "__main__":
    sys.exit(main())
