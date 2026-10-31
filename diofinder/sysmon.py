"""System resource "headroom" monitor (pure parsing + a small stateful sampler).

Measures the three things that actually determine stability on the 512 MB /
memory-bandwidth-bound Pi Zero 2W:

* **Memory** — MemAvailable and, the sharpest "am I RAM-tight" signal, zram
  **swap in use** (on a healthy diofinder this sits at ~0; sustained swap IS
  the alarm, and is exactly what gates the solver catalog memory optimisation).
* **CPU per core** — overall and **CPU 0 specifically**, since CPU 0 is shared
  by comms + webui + the IMU thread + kernel/IRQ, and CPU-0 saturation is what
  produced the "camera unavailable" / SkySafari-jitter class of symptoms.
* **SoC temperature** — an enclosed finder can thermally throttle, which
  degrades everything.

Design notes:
* The pure parse helpers take file *contents* as strings, so they unit-test
  with no /proc present.
* `SysMon` is the only stateful part: it holds the previous /proc/stat snapshot
  (CPU% needs a delta between two reads) and the **high-water-marks** since it
  started — so a transient squeeze during a solve/slew burst is recorded even
  if nobody had the page open at the time.
* Everything is cheap file reads (no `vcgencmd` subprocess — a headroom monitor
  must not itself load CPU 0). Sampled every few seconds by a comms thread.
"""

from __future__ import annotations

import time

_PROC_MEMINFO = "/proc/meminfo"
_PROC_STAT = "/proc/stat"
_PROC_LOADAVG = "/proc/loadavg"
_SYS_THERMAL = "/sys/class/thermal/thermal_zone0/temp"

# Verdict thresholds. Deliberately conservative for a 512 MB board; a finder
# that is comfortable should sit solidly green. Tunable later via config if a
# device's "normal" differs.
DEFAULT_THRESHOLDS = {
    "mem_avail_warn_pct": 20.0,   # < this fraction of RAM available -> tight
    "mem_avail_crit_pct": 10.0,
    "swap_used_warn_mb": 8.0,     # any sustained zram swap is a yellow flag
    "swap_used_crit_mb": 64.0,
    "cpu0_warn_pct": 85.0,        # CPU 0 is the shared/limiting core
    "cpu0_crit_pct": 95.0,
    "temp_warn_c": 70.0,          # Pi Zero 2W soft-throttles ~80-85 C
    "temp_crit_c": 80.0,
}


# ---------------------------------------------------------------------------
# Pure parsers (take file contents as strings)
# ---------------------------------------------------------------------------

def parse_meminfo(text: str) -> dict:
    """/proc/meminfo -> {mem_total_kb, mem_available_kb, swap_total_kb,
    swap_free_kb, mem_available_pct, swap_used_kb, swap_used_mb}."""
    vals = {}
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) != 2:
            continue
        key = parts[0].strip()
        try:
            vals[key] = int(parts[1].strip().split()[0])  # kB
        except (ValueError, IndexError):
            continue
    mt = vals.get("MemTotal", 0)
    ma = vals.get("MemAvailable", 0)
    st = vals.get("SwapTotal", 0)
    sf = vals.get("SwapFree", 0)
    swap_used = max(0, st - sf)
    return {
        "mem_total_kb": mt,
        "mem_available_kb": ma,
        "mem_available_pct": round(100.0 * ma / mt, 1) if mt else 0.0,
        "swap_total_kb": st,
        "swap_free_kb": sf,
        "swap_used_kb": swap_used,
        "swap_used_mb": round(swap_used / 1024.0, 1),
    }


def parse_stat_cpu(text: str) -> dict:
    """/proc/stat -> {cpu_name: (idle_all_jiffies, total_jiffies)} for every
    line beginning with 'cpu' ('cpu' = aggregate, 'cpu0'.. = per core)."""
    out = {}
    for line in text.splitlines():
        if not line.startswith("cpu"):
            continue
        f = line.split()
        name = f[0]
        try:
            nums = [int(x) for x in f[1:]]
        except ValueError:
            continue
        if len(nums) < 5:
            continue
        # fields: user nice system idle iowait irq softirq steal guest guest_nice
        idle_all = nums[3] + nums[4]           # idle + iowait
        total = sum(nums)
        out[name] = (idle_all, total)
    return out


def cpu_pct_from_deltas(prev: dict, cur: dict) -> dict:
    """Per-core busy percentage from two parse_stat_cpu snapshots."""
    out = {}
    for name, (idle1, total1) in prev.items():
        if name not in cur:
            continue
        idle2, total2 = cur[name]
        dtotal = total2 - total1
        didle = idle2 - idle1
        if dtotal <= 0:
            continue
        out[name] = round(100.0 * (dtotal - didle) / dtotal, 1)
    return out


def parse_loadavg(text: str):
    """/proc/loadavg -> (load1, load5, load15) or None on parse failure."""
    try:
        f = text.split()
        return (float(f[0]), float(f[1]), float(f[2]))
    except (ValueError, IndexError):
        return None


def parse_temp_c(text: str):
    """thermal_zone temp (millidegrees C) -> degrees C, or None."""
    try:
        return round(int(text.strip()) / 1000.0, 1)
    except (ValueError, AttributeError):
        return None


def classify(snapshot: dict, thresholds: dict | None = None) -> dict:
    """Pure verdict from a snapshot's instantaneous + high-water values.

    Returns {"level": "comfortable"|"tight"|"constrained", "reasons": [...]}.
    Uses the WORST of instantaneous and high-water-mark per axis, so a verdict
    reflects whether the device has *ever* been squeezed since monitoring
    started, not just this instant."""
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    reasons = []
    level = "comfortable"

    def bump(new):
        nonlocal level
        order = {"comfortable": 0, "tight": 1, "constrained": 2}
        if order[new] > order[level]:
            level = new

    hw = snapshot.get("high_water", {})
    # Memory available (lower is worse): use the min seen.
    mem_lo = hw.get("min_mem_available_pct", snapshot.get("mem_available_pct"))
    if mem_lo is not None:
        if mem_lo < t["mem_avail_crit_pct"]:
            bump("constrained"); reasons.append(f"available RAM fell to {mem_lo:.0f}%")
        elif mem_lo < t["mem_avail_warn_pct"]:
            bump("tight"); reasons.append(f"available RAM fell to {mem_lo:.0f}%")
    # Swap used (higher is worse): use the max seen.
    sw_hi = hw.get("max_swap_used_mb", snapshot.get("swap_used_mb", 0.0))
    if sw_hi is not None:
        if sw_hi >= t["swap_used_crit_mb"]:
            bump("constrained"); reasons.append(f"swap reached {sw_hi:.0f} MB")
        elif sw_hi >= t["swap_used_warn_mb"]:
            bump("tight"); reasons.append(f"swap reached {sw_hi:.0f} MB")
    # CPU 0 (the shared/limiting core): use the max seen.
    c0_hi = hw.get("max_cpu0_pct", (snapshot.get("cpu_pct") or {}).get("cpu0"))
    if c0_hi is not None:
        if c0_hi >= t["cpu0_crit_pct"]:
            bump("constrained"); reasons.append(f"CPU 0 peaked at {c0_hi:.0f}%")
        elif c0_hi >= t["cpu0_warn_pct"]:
            bump("tight"); reasons.append(f"CPU 0 peaked at {c0_hi:.0f}%")
    # Temperature: use the max seen.
    tmp_hi = hw.get("max_temp_c", snapshot.get("temp_c"))
    if tmp_hi is not None:
        if tmp_hi >= t["temp_crit_c"]:
            bump("constrained"); reasons.append(f"temperature reached {tmp_hi:.0f} C")
        elif tmp_hi >= t["temp_warn_c"]:
            bump("tight"); reasons.append(f"temperature reached {tmp_hi:.0f} C")

    return {"level": level, "reasons": reasons}


# ---------------------------------------------------------------------------
# Stateful sampler
# ---------------------------------------------------------------------------

def _read(path: str):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


class SysMon:
    """Periodically-sampled resource monitor with high-water-marks."""

    def __init__(self, thresholds: dict | None = None):
        self.thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        self._prev_cpu = None
        self._last = {}
        self.started_mono = time.monotonic()
        self.started_wall = time.time()
        self.high_water = {
            "min_mem_available_pct": None,
            "max_swap_used_mb": 0.0,
            "max_cpu_pct": 0.0,
            "max_cpu0_pct": 0.0,
            "max_temp_c": None,
        }
        # Seed the CPU baseline so the first sample() after an interval yields a
        # real delta.
        stat = _read(_PROC_STAT)
        if stat:
            self._prev_cpu = parse_stat_cpu(stat)

    def _bump_marks(self, snap: dict):
        hw = self.high_water
        mp = snap.get("mem_available_pct")
        if mp is not None:
            hw["min_mem_available_pct"] = (mp if hw["min_mem_available_pct"] is None
                                           else min(hw["min_mem_available_pct"], mp))
        hw["max_swap_used_mb"] = max(hw["max_swap_used_mb"], snap.get("swap_used_mb", 0.0))
        cpu = snap.get("cpu_pct") or {}
        if "cpu" in cpu:
            hw["max_cpu_pct"] = max(hw["max_cpu_pct"], cpu["cpu"])
        if "cpu0" in cpu:
            hw["max_cpu0_pct"] = max(hw["max_cpu0_pct"], cpu["cpu0"])
        tc = snap.get("temp_c")
        if tc is not None:
            hw["max_temp_c"] = tc if hw["max_temp_c"] is None else max(hw["max_temp_c"], tc)

    def sample(self) -> dict:
        """Read /proc + /sys, update CPU deltas and high-water-marks, and
        return the current snapshot (also cached for snapshot())."""
        snap = {"uptime_s": round(time.monotonic() - self.started_mono, 1)}

        mem = _read(_PROC_MEMINFO)
        if mem:
            snap.update(parse_meminfo(mem))

        stat = _read(_PROC_STAT)
        if stat:
            cur = parse_stat_cpu(stat)
            if self._prev_cpu:
                snap["cpu_pct"] = cpu_pct_from_deltas(self._prev_cpu, cur)
            self._prev_cpu = cur

        la = parse_loadavg(_read(_PROC_LOADAVG) or "")
        if la:
            snap["load"] = list(la)

        tc = parse_temp_c(_read(_SYS_THERMAL) or "")
        if tc is not None:
            snap["temp_c"] = tc

        self._bump_marks(snap)
        snap["high_water"] = dict(self.high_water)
        snap["verdict"] = classify(snap, self.thresholds)
        self._last = snap
        return snap

    def snapshot(self) -> dict:
        """Last computed sample (sampling once if none taken yet)."""
        return self._last or self.sample()
