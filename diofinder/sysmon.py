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

# How long after start to ignore for the verdict/high-water-marks. The star-DB
# load + process spawn + first solve all land in the first few seconds and peg
# CPU0 / squeeze RAM — a benign boot transient, not sustained pressure.
DEFAULT_WARMUP_S = 90.0

# Verdict thresholds. Deliberately conservative for a 512 MB board; a finder
# that is comfortable should sit solidly green. Tunable later via config if a
# device's "normal" differs.
#
# NOTE on swap: we flag on *activity* (zram GROWING between samples), not on
# occupancy. Pages offloaded once during boot and never reclaimed sit in zram
# indefinitely with no cost — that is not pressure. A large absolute occupancy
# is kept only as a backstop (a big fraction of RAM swapped out IS a concern).
DEFAULT_THRESHOLDS = {
    "mem_avail_warn_pct": 15.0,      # < this fraction of RAM available -> tight
    "mem_avail_crit_pct": 8.0,
    "swap_growth_warn_mb": 4.0,      # zram grew this much in one sample -> swapping
    "swap_growth_crit_mb": 16.0,
    "swap_used_backstop_pct": 30.0,  # this fraction of RAM swapped out -> tight
    "cpu0_warn_pct": 90.0,           # CPU 0 is the shared/limiting core
    "cpu0_crit_pct": 98.0,
    "temp_warn_c": 70.0,             # Pi Zero 2W soft-throttles ~80-85 C
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
    """Pure verdict from a snapshot's high-water values (post-warm-up).

    Returns {"level": "warming_up"|"comfortable"|"tight"|"constrained",
    "reasons": [...]}. The high-water-marks exclude the startup window (so a
    boot spike doesn't drive the verdict), and swap is judged on *growth*
    (actively swapping), not static occupancy (benign zram left over from boot).
    While still warming up, returns "warming_up" with no reasons."""
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    if snapshot.get("in_warmup"):
        return {"level": "warming_up", "reasons": []}
    reasons = []
    level = "comfortable"

    def bump(new):
        nonlocal level
        order = {"comfortable": 0, "tight": 1, "constrained": 2}
        if order[new] > order[level]:
            level = new

    hw = snapshot.get("high_water", {})
    # Memory available (lower is worse): min seen post-warm-up.
    mem_lo = hw.get("min_mem_available_pct")
    if mem_lo is not None:
        if mem_lo < t["mem_avail_crit_pct"]:
            bump("constrained"); reasons.append(f"available RAM fell to {mem_lo:.0f}%")
        elif mem_lo < t["mem_avail_warn_pct"]:
            bump("tight"); reasons.append(f"available RAM fell to {mem_lo:.0f}%")
    # Swap ACTIVITY (growth per sample), not occupancy — the real pressure sign.
    sw_grow = hw.get("max_swap_growth_mb", 0.0)
    if sw_grow is not None:
        if sw_grow >= t["swap_growth_crit_mb"]:
            bump("constrained"); reasons.append(f"actively swapping (+{sw_grow:.0f} MB/sample)")
        elif sw_grow >= t["swap_growth_warn_mb"]:
            bump("tight"); reasons.append(f"swapping (+{sw_grow:.0f} MB/sample)")
    # Backstop: a large fraction of RAM swapped out is a concern even if idle.
    mem_total_mb = (snapshot.get("mem_total_kb") or 0) / 1024.0
    sw_hi = hw.get("max_swap_used_mb", 0.0)
    if mem_total_mb and sw_hi >= mem_total_mb * t["swap_used_backstop_pct"] / 100.0:
        bump("tight"); reasons.append(f"{sw_hi:.0f} MB swapped out")
    # CPU 0 (the shared/limiting core): max seen post-warm-up.
    c0_hi = hw.get("max_cpu0_pct")
    if c0_hi is not None:
        if c0_hi >= t["cpu0_crit_pct"]:
            bump("constrained"); reasons.append(f"CPU 0 peaked at {c0_hi:.0f}%")
        elif c0_hi >= t["cpu0_warn_pct"]:
            bump("tight"); reasons.append(f"CPU 0 peaked at {c0_hi:.0f}%")
    # Temperature: max seen post-warm-up.
    tmp_hi = hw.get("max_temp_c")
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

    def __init__(self, thresholds: dict | None = None, warmup_s: float = DEFAULT_WARMUP_S):
        self.thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        self.warmup_s = float(warmup_s)
        self._prev_cpu = None
        self._prev_swap_mb = None
        self._last = {}
        self.started_mono = time.monotonic()
        self.started_wall = time.time()
        self.high_water = {
            "min_mem_available_pct": None,
            "max_swap_used_mb": 0.0,     # occupancy (info + backstop)
            "max_swap_growth_mb": 0.0,   # growth per sample (the pressure signal)
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
        # Ignore the startup window: the DB load + process spawn + first solve
        # spike CPU0 / RAM at boot and must not drive the verdict.
        if snap.get("in_warmup"):
            return
        hw = self.high_water
        mp = snap.get("mem_available_pct")
        if mp is not None:
            hw["min_mem_available_pct"] = (mp if hw["min_mem_available_pct"] is None
                                           else min(hw["min_mem_available_pct"], mp))
        hw["max_swap_used_mb"] = max(hw["max_swap_used_mb"], snap.get("swap_used_mb", 0.0))
        g = snap.get("swap_growth_mb")
        if g is not None:
            hw["max_swap_growth_mb"] = max(hw["max_swap_growth_mb"], g)
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
        uptime = round(time.monotonic() - self.started_mono, 1)
        snap = {
            "uptime_s": uptime,
            "warmup_s": self.warmup_s,
            "in_warmup": uptime < self.warmup_s,
        }

        mem = _read(_PROC_MEMINFO)
        if mem:
            snap.update(parse_meminfo(mem))
            # Swap GROWTH since the previous sample — the real pressure signal
            # (static zram occupancy left over from boot is not).
            cur_swap = snap.get("swap_used_mb")
            if cur_swap is not None:
                if self._prev_swap_mb is not None:
                    snap["swap_growth_mb"] = round(max(0.0, cur_swap - self._prev_swap_mb), 1)
                self._prev_swap_mb = cur_swap

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
