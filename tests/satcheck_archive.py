#!/usr/bin/env python3
"""Look up satellites for a diofinder live-capture archive with SatChecker.

Off-device helper (standard library only). Reads ``livecap_<UTC>.zip``
(written by the Home page "Live capture" button), and for each frame asks the
IAU CPS SatChecker field-of-view endpoint which satellites passed through the
frame during its exposure:

    GET <base>/fov/satellite-passes/?latitude=..&longitude=..&elevation=..
        &start_time_jd=..&duration=<s>&ra=..&dec=..&fov_radius=..
        &group_by=satellite&async=False

Inputs come from the archive: ``session.json`` (observer site, field radius)
and ``frames.jsonl`` (per-frame exposure start JD, exposure length, solved
image-centre RA/Dec, J2000). Docs:
https://satchecker.readthedocs.io/en/latest/fov.html

Usage:
    python3 tests/satcheck_archive.py livecap_20261102T013045Z.zip --dry-run
    python3 tests/satcheck_archive.py livecap_...zip --out passes.json
    python3 tests/satcheck_archive.py livecap_...zip --every 5 --group 5

Options worth knowing:
  --dry-run       print the query URLs only (check the parameters first)
  --group N       one query spanning N consecutive frames (fewer API calls;
                  a satellite is then attributed to the group, not the frame)
  --every N       query every Nth frame (or group) only
  --lat/--lon/--elevation  override the site recorded in the archive
                  (longitude EAST-positive, as diofinder stores it)
  --radius DEG    override the field radius (default: the archive's
                  half-diagonal, which covers the whole rectangular frame)

Frames that did not solve themselves use the last solved pointing recorded
with them (``last_solved``) and are marked as such; frames with no pointing at
all are skipped. Be polite to the public service: --sleep (default 1 s) spaces
the requests.
"""
import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
import zipfile

DEFAULT_BASE = "https://satchecker.cps.iau.org"


def load_archive(path):
    with zipfile.ZipFile(path) as zf:
        session = json.loads(zf.read("session.json")) if "session.json" in zf.namelist() else {}
        frames = []
        if "frames.jsonl" in zf.namelist():
            for line in zf.read("frames.jsonl").decode().splitlines():
                line = line.strip()
                if line:
                    frames.append(json.loads(line))
    return session, frames


def frame_pointing(rec):
    """(ra, dec, source) for a frame, or None."""
    if rec.get("solved") and rec.get("center_ra_deg") is not None:
        return rec["center_ra_deg"], rec["center_dec_deg"], "solved"
    ls = rec.get("last_solved") or {}
    if ls.get("center_ra_deg") is not None:
        return ls["center_ra_deg"], ls["center_dec_deg"], "last_solved"
    return None


def frame_times(rec):
    """(jd_start, duration_s) for a frame, or None."""
    if rec.get("jd_start") is not None and rec.get("exposure_s"):
        return rec["jd_start"], float(rec["exposure_s"])
    if rec.get("jd_saved") is not None:
        # Hand-over time only (no sensor timing): ends after the exposure.
        return rec["jd_saved"], 1.0
    return None


def build_queries(session, frames, group=1, every=1, site=None, radius=None):
    obs = dict(session.get("observer") or {})
    if site:
        obs.update({k: v for k, v in site.items() if v is not None})
    cam = session.get("camera") or {}
    rad = radius or cam.get("fov_radius_deg")
    if rad is None:
        raise SystemExit("no field radius in the archive; pass --radius")
    if obs.get("latitude_deg") is None or obs.get("longitude_deg") is None:
        raise SystemExit("no observer site in the archive; pass --lat/--lon")
    out = []
    groups = [frames[i:i + group] for i in range(0, len(frames), group)]
    for gi, g in enumerate(groups):
        if gi % every:
            continue
        pts = [p for p in (frame_pointing(r) for r in g) if p]
        tms = [t for t in (frame_times(r) for r in g) if t]
        if not pts or not tms:
            continue
        ra, dec, src = pts[len(pts) // 2]          # middle frame's pointing
        jd0 = min(t[0] for t in tms)
        jd1 = max(t[0] + t[1] / 86400.0 for t in tms)
        params = {
            "latitude": obs["latitude_deg"],
            "longitude": obs["longitude_deg"],
            "elevation": obs.get("elevation_m") or 0.0,
            "start_time_jd": f"{jd0:.8f}",
            "duration": round((jd1 - jd0) * 86400.0, 3),
            "ra": round(ra, 5),
            "dec": round(dec, 5),
            "fov_radius": round(rad, 3),
            "group_by": "satellite",
            "async": "False",
        }
        out.append({
            "frames": [r.get("file") for r in g],
            "pointing_source": src,
            "time_source": g[0].get("time_source"),
            "params": params,
        })
    return out


def _get_json(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "diofinder-satcheck/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def query(base, params, timeout=60, poll_s=5.0, max_wait_s=600.0):
    """GET /fov/satellite-passes/. With async=False the result comes back
    directly; if the service answers with a task_id instead (async=True, or
    a request it chose to queue), poll /fov/task-status/<id> until done."""
    url = base.rstrip("/") + "/fov/satellite-passes/?" + urllib.parse.urlencode(params)
    resp = _get_json(url, timeout)
    if isinstance(resp, dict) and resp.get("task_id") and "data" not in resp:
        status_url = base.rstrip("/") + f"/fov/task-status/{resp['task_id']}"
        t_end = time.monotonic() + max_wait_s
        while True:
            resp = _get_json(status_url, timeout)
            if str(resp.get("status", "")).upper() not in ("PENDING", "STARTED",
                                                           "PROGRESS", "RETRY"):
                break
            if time.monotonic() > t_end:
                raise TimeoutError(f"SatChecker task {status_url} still pending")
            time.sleep(poll_s)
    return url, resp


def parse_satellites(resp):
    """{name: [(jd, ra_deg, dec_deg), ...] sorted by time} from a SatChecker
    FOV response grouped by satellite: resp["data"]["satellites"][name]
    ["positions"] = [{"ra", "dec", "julian_date", ...}, ...]."""
    out = {}
    data = resp.get("data") if isinstance(resp, dict) else None
    sats = (data.get("satellites") if isinstance(data, dict) else None) or {}
    if isinstance(sats, dict):
        items = sats.items()
    else:                                  # tolerate a list form
        items = ((s.get("name") or s.get("satellite_name") or str(i), s)
                 for i, s in enumerate(sats))
    for name, sd in items:
        pts = []
        for p in (sd or {}).get("positions") or []:
            try:
                pts.append((float(p["julian_date"]), float(p["ra"]), float(p["dec"])))
            except (KeyError, TypeError, ValueError):
                continue
        if pts:
            out[str(name)] = sorted(pts)
    return out


def summarize(resp):
    """Satellite names in a SatChecker response (the full JSON is always
    saved with --out)."""
    sats = parse_satellites(resp)
    if sats:
        return list(sats)
    names = []

    def walk(o):
        if isinstance(o, dict):
            n = o.get("name") or o.get("satellite_name")
            if isinstance(n, str):
                nid = o.get("norad_id") or o.get("satellite_id")
                names.append(f"{n} ({nid})" if nid else n)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(resp)
    seen, uniq = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("archive")
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--group", type=int, default=1)
    ap.add_argument("--every", type=int, default=1)
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float, help="EAST-positive degrees")
    ap.add_argument("--elevation", type=float)
    ap.add_argument("--radius", type=float)
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--out", help="write every query + full response here (JSON)")
    a = ap.parse_args(argv)

    session, frames = load_archive(a.archive)
    if session.get("clock_plausible") is False:
        print("WARNING: the finder's clock looked wrong when this was recorded; "
              "times (and so the satellites found) may be wrong.", file=sys.stderr)
    site = {"latitude_deg": a.lat, "longitude_deg": a.lon, "elevation_m": a.elevation}
    qs = build_queries(session, frames, group=max(1, a.group),
                       every=max(1, a.every), site=site, radius=a.radius)
    print(f"{len(frames)} frames -> {len(qs)} queries")
    results = []
    for i, q in enumerate(qs):
        label = q["frames"][0] + (f"..{q['frames'][-1]}" if len(q["frames"]) > 1 else "")
        if a.dry_run:
            print(label, a.base_url.rstrip("/") + "/fov/satellite-passes/?" +
                  urllib.parse.urlencode(q["params"]))
            continue
        try:
            url, resp = query(a.base_url, q["params"])
            sats = summarize(resp)
            print(f"{label}: {', '.join(sats) if sats else 'none'}"
                  + ("  [pointing from last solve]" if q["pointing_source"] != "solved" else ""))
            results.append(dict(q, url=url, response=resp, satellites=sats))
        except Exception as e:
            print(f"{label}: query failed: {e}", file=sys.stderr)
            results.append(dict(q, error=str(e)))
        if i + 1 < len(qs):
            time.sleep(a.sleep)
    if a.out and not a.dry_run:
        with open(a.out, "w") as f:
            json.dump({"archive": a.archive, "session": session,
                       "results": results}, f, indent=2)
        print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
