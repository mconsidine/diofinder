"""System resource "headroom" monitor (pure parsing + a small stateful sampler).

Measures the three things that actually determine stability on the 512 MB /
memory-bandwidth-bound Pi Zero 2W:

* **Memory** — MemAvailable, plus zram swap judged by its *compressed physical
  footprint* and *sustained* eviction (not the alarming-looking uncompressed
  "swap used", and not a one-shot page-render burst). Sustained swap-out is the
  signal that gates the solver catalog memory optimisation.
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

import os
import time

try:
    _CLK_TCK = os.sysconf("SC_CLK_TCK") or 100
except (ValueError, OSError):
    _CLK_TCK = 100
try:
    _PAGE_KB = (os.sysconf("SC_PAGE_SIZE") or 4096) / 1024.0
except (ValueError, OSError):
    _PAGE_KB = 4.0

_PROC_MEMINFO = "/proc/meminfo"
_PROC_STAT = "/proc/stat"
_PROC_LOADAVG = "/proc/loadavg"
_SYS_THERMAL = "/sys/class/thermal/thermal_zone0/temp"
_SYS_ZRAM_MM = "/sys/block/zram0/mm_stat"

# Start-up window ignored by the verdict/high-water-marks. The star-DB load +
# process spawn + web UI imports + first frames peg CPU0 / squeeze RAM — a
# benign boot transient, not sustained pressure. On a COLD boot (cold SD reads,
# empty zram) that can run past a fixed timer, so warm-up ends only when ALL of:
#   * at least DEFAULT_WARMUP_S has elapsed, and
#   * the caller has reported the system ready (solver publishing + web UI up),
#     plus DEFAULT_SETTLE_S for the kernel to settle (first zram absorption);
# with DEFAULT_MAX_WARMUP_S as a cap so a never-ready system still gets judged.
# A caller that doesn't report readiness (ready=None) gets the plain timer.
DEFAULT_WARMUP_S = 90.0
DEFAULT_SETTLE_S = 30.0
DEFAULT_MAX_WARMUP_S = 300.0

# Verdict thresholds. Deliberately conservative for a 512 MB board; a finder
# that is comfortable should sit solidly green. Tunable later via config if a
# device's "normal" differs.
#
# NOTE on swap (zram-aware). Swap on this box is zram: lz4/zstd-compressed RAM,
# not disk. Two consequences drive how we judge it:
#   1. The "swap used" number from /proc/meminfo is the UNCOMPRESSED size of the
#      pages parked in zram. The real cost is the *compressed* physical RAM the
#      zram device holds (/sys/block/zram0/mm_stat, field 3) — typically 3-4x
#      smaller. We judge pressure on that physical footprint, and report both.
#   2. Pushing a few MB of cold pages to zram on a page render or at solve start
#      is normal and cheap (a little CPU to compress, no disk I/O). A single
#      eviction burst is NOT pressure. Genuine pressure is *sustained* eviction
#      (the footprint growing over several consecutive samples), so we flag on a
#      growth STREAK, never on a one-shot spike, and swap alone only reaches
#      "tight" — the "constrained" escalations are reserved for low available
#      RAM, CPU-0 saturation, and thermal throttling (the states that actually
#      stall the finder).
#
# NOTE on available RAM. MemAvailable counts free memory + droppable cache but
# NOT anonymous pages zram could still compress (~3-4:1), so on this box it
# understates real headroom — and it bottoms out by design at the moment the
# kernel starts reclaiming (e.g. the first zram absorption after a cold boot),
# then recovers. So the WARN rung needs a sustained streak below the line, like
# swap. The CRIT rung stays single-sample: OOM kills are instantaneous, so even
# a brief dip that deep is worth flagging.
DEFAULT_THRESHOLDS = {
    "mem_avail_warn_pct": 15.0,        # below this for a streak -> tight
    "mem_avail_streak_tight": 3,       # consecutive low samples (~15 s) -> tight
    "mem_avail_crit_pct": 8.0,         # below this even once -> constrained
    "swap_growth_warn_mb": 4.0,        # physical zram grew this much in one sample -> count it
    "swap_growth_streak_tight": 3,     # that many consecutive growing samples (~15 s) -> tight
    "swap_phys_backstop_pct": 40.0,    # physical zram footprint this fraction of RAM -> tight
    # CPU 0 is the shared/limiting core. Like RAM and swap, both rungs need a
    # SUSTAINED run of samples: one busy 5 s sample (a debug bundle, a page
    # load) turned the field card "Tight" while nothing was actually starved.
    "cpu0_warn_pct": 90.0,
    "cpu0_streak_tight": 3,            # consecutive samples >= warn (~15 s) -> tight
    "cpu0_crit_pct": 98.0,
    "cpu0_streak_crit": 3,             # consecutive samples >= crit (~15 s) -> constrained
    "temp_warn_c": 70.0,               # Pi Zero 2W soft-throttles ~80-85 C
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


def parse_zram_mm_stat(text: str):
    """/sys/block/zram0/mm_stat -> {zram_orig_mb, zram_compr_mb, zram_phys_mb,
    zram_ratio} or None. Whitespace-separated bytes; field 1 = orig_data_size
    (uncompressed), 2 = compr_data_size, 3 = mem_used_total (physical RAM the
    zram device actually occupies, incl. metadata)."""
    try:
        f = text.split()
        orig = int(f[0]); compr = int(f[1]); used = int(f[2])
    except (ValueError, IndexError, AttributeError):
        return None
    mb = 1024.0 * 1024.0
    return {
        "zram_orig_mb": round(orig / mb, 1),
        "zram_compr_mb": round(compr / mb, 1),
        "zram_phys_mb": round(used / mb, 1),
        "zram_ratio": round(orig / used, 1) if used > 0 else None,
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


def parse_proc_statm(text: str) -> float | None:
    """/proc/<pid>/statm -> resident set size in KB (field 2 = resident pages)."""
    try:
        return round(int(text.split()[1]) * _PAGE_KB, 1)
    except (ValueError, IndexError, AttributeError):
        return None


def parse_proc_stat_times(text: str):
    """/proc/<pid>/stat -> (utime+stime jiffies, last_cpu) or (None, None).

    The comm field (2) may contain spaces/parens, so split after the final ')'
    — everything after is whitespace-delimited starting at field 3 (state)."""
    try:
        after = text[text.rindex(")") + 1:].split()
        # after[0]=state(f3); utime=f14 -> after[11]; stime=f15 -> after[12];
        # processor=f39 -> after[36].
        ticks = int(after[11]) + int(after[12])
        last_cpu = int(after[36]) if len(after) > 36 else None
        return ticks, last_cpu
    except (ValueError, IndexError):
        return None, None


def discover_pid_by_cmdline(marker: str) -> int | None:
    """First PID whose /proc/<pid>/cmdline contains `marker` (NUL-joined args
    rendered with spaces). Used to find the separate web UI service."""
    try:
        pids = [int(d) for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return None
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "ignore")
        except OSError:
            continue
        if marker in cmd:
            return pid
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
    # Memory available (lower is worse). Crit on any single dip (OOM risk is
    # instantaneous); warn only on a sustained streak below the line.
    mem_lo = hw.get("min_mem_available_pct")
    low_streak = hw.get("max_mem_low_streak", 0) or 0
    if mem_lo is not None and mem_lo < t["mem_avail_crit_pct"]:
        bump("constrained"); reasons.append(f"available RAM fell to {mem_lo:.0f}%")
    elif mem_lo is not None and low_streak >= t["mem_avail_streak_tight"]:
        bump("tight")
        reasons.append(f"available RAM stayed below {t['mem_avail_warn_pct']:.0f}% "
                       f"({low_streak} samples running, low {mem_lo:.0f}%)")
    # Swap: judged on SUSTAINED eviction of the compressed zram footprint, not a
    # one-shot burst or uncompressed occupancy (zram is compressed RAM — a lone
    # page-render/solve-start eviction is normal and cheap). Swap alone only
    # reaches "tight"; the genuinely-stalling states escalate below.
    streak = hw.get("max_swap_growth_streak", 0) or 0
    if streak >= t["swap_growth_streak_tight"]:
        sw_grow = hw.get("max_swap_growth_mb", 0.0)
        bump("tight")
        reasons.append(f"leaning on swap ({streak} samples running, peak +{sw_grow:.0f} MB)")
    # Backstop: a large fraction of RAM *physically* held in zram is a concern
    # even without active growth. Uses the compressed footprint, not the
    # uncompressed "swap used".
    mem_total_mb = (snapshot.get("mem_total_kb") or 0) / 1024.0
    sw_phys_hi = hw.get("max_swap_phys_mb", 0.0)
    if mem_total_mb and sw_phys_hi >= mem_total_mb * t["swap_phys_backstop_pct"] / 100.0:
        bump("tight"); reasons.append(f"{sw_phys_hi:.0f} MB of RAM held in zram")
    # CPU 0 (the shared/limiting core): longest busy run post-warm-up. The peak
    # alone is shown on the card but no longer decides the verdict.
    c0_hi = hw.get("max_cpu0_pct") or 0.0
    crit_run = hw.get("max_cpu0_crit_streak", 0) or 0
    warn_run = hw.get("max_cpu0_warn_streak", 0) or 0
    if crit_run >= t["cpu0_streak_crit"]:
        bump("constrained")
        reasons.append(f"CPU 0 at {t['cpu0_crit_pct']:.0f}%+ for {crit_run} samples "
                       f"running (peak {c0_hi:.0f}%)")
    elif warn_run >= t["cpu0_streak_tight"]:
        bump("tight")
        reasons.append(f"CPU 0 at {t['cpu0_warn_pct']:.0f}%+ for {warn_run} samples "
                       f"running (peak {c0_hi:.0f}%)")
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

    def __init__(self, thresholds: dict | None = None, warmup_s: float = DEFAULT_WARMUP_S,
                 settle_s: float = DEFAULT_SETTLE_S,
                 max_warmup_s: float = DEFAULT_MAX_WARMUP_S):
        self.thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        self.warmup_s = float(warmup_s)
        self.settle_s = float(settle_s)
        self.max_warmup_s = float(max_warmup_s)
        self._ready_mono = None         # when the caller first reported ready
        self._prev_cpu = None
        self._prev_swap_phys_mb = None  # physical zram footprint, for growth deltas
        self._swap_growth_streak = 0    # consecutive samples that grew >= warn
        self._mem_low_streak = 0        # consecutive samples below mem warn
        self._cpu0_warn_streak = 0      # consecutive samples with CPU 0 >= warn
        self._cpu0_crit_streak = 0      # consecutive samples with CPU 0 >= crit
        self._prev_proc = {}          # pid -> (ticks, monotonic) for CPU% deltas
        self._last = {}
        self.started_mono = time.monotonic()
        self.started_wall = time.time()
        self.high_water = {
            "min_mem_available_pct": None,
            "max_swap_used_mb": 0.0,     # uncompressed occupancy (info/display only)
            "max_swap_phys_mb": 0.0,     # compressed physical footprint (backstop)
            "max_swap_growth_mb": 0.0,   # largest single-sample growth (info)
            "max_swap_growth_streak": 0, # longest run of consecutive growing samples
            "max_mem_low_streak": 0,     # longest run of samples below mem warn
            "max_cpu_pct": 0.0,
            "max_cpu0_pct": 0.0,
            "max_cpu0_warn_streak": 0,   # longest run of samples with CPU 0 >= warn
            "max_cpu0_crit_streak": 0,   # longest run of samples with CPU 0 >= crit
            "max_temp_c": None,
        }
        # uptime_s (since this monitor started) at which each high-water mark
        # was last set — tells a boot-settle dip from a mid-session squeeze.
        self.high_water_at = {}
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
        hw, at, t = self.high_water, self.high_water_at, self.thresholds
        up = snap.get("uptime_s")

        def hi(key, val):          # raise a max-mark, stamping when it moved
            if val is not None and (hw[key] is None or val > hw[key]):
                hw[key] = val
                at[key] = up

        def lo(key, val):          # lower a min-mark, stamping when it moved
            if val is not None and (hw[key] is None or val < hw[key]):
                hw[key] = val
                at[key] = up

        mp = snap.get("mem_available_pct")
        lo("min_mem_available_pct", mp)
        if mp is not None:
            # Sustained-low streak: the warn rung needs this, a lone dip resets.
            self._mem_low_streak = (self._mem_low_streak + 1
                                    if mp < t["mem_avail_warn_pct"] else 0)
            hi("max_mem_low_streak", self._mem_low_streak)
        hi("max_swap_used_mb", snap.get("swap_used_mb"))
        hi("max_swap_phys_mb", snap.get("swap_phys_mb"))
        g = snap.get("swap_growth_mb")
        if g is not None:
            hi("max_swap_growth_mb", g)
            # A growth STREAK (consecutive samples each adding >= warn MB to the
            # physical footprint) is the sustained-eviction signal; a lone burst
            # resets to 0 on the next quiet sample and never reaches the tight gate.
            self._swap_growth_streak = (self._swap_growth_streak + 1
                                        if g >= t["swap_growth_warn_mb"] else 0)
            hi("max_swap_growth_streak", self._swap_growth_streak)
        cpu = snap.get("cpu_pct") or {}
        hi("max_cpu_pct", cpu.get("cpu"))
        c0 = cpu.get("cpu0")
        hi("max_cpu0_pct", c0)
        if c0 is not None:
            # Sustained-busy streaks: a lone busy sample resets on the next one.
            self._cpu0_warn_streak = (self._cpu0_warn_streak + 1
                                      if c0 >= t["cpu0_warn_pct"] else 0)
            self._cpu0_crit_streak = (self._cpu0_crit_streak + 1
                                      if c0 >= t["cpu0_crit_pct"] else 0)
            hi("max_cpu0_warn_streak", self._cpu0_warn_streak)
            hi("max_cpu0_crit_streak", self._cpu0_crit_streak)
        hi("max_temp_c", snap.get("temp_c"))

    def _warmup_state(self, now_mono: float, ready) -> str | None:
        """None once warm-up is over, else a short reason string. `ready` is the
        caller's "system is up" signal (solver publishing + web UI running);
        None means the caller doesn't report readiness -> plain timer. Latches
        the first ready time so settling is measured from it."""
        if ready and self._ready_mono is None:
            self._ready_mono = now_mono
        up = now_mono - self.started_mono
        if up < self.warmup_s:
            return "starting"
        if ready is None or up >= self.max_warmup_s:
            return None
        if self._ready_mono is None:
            return "waiting for solver / web UI"
        if now_mono - self._ready_mono < self.settle_s:
            return "settling"
        return None

    def _sample_processes(self, pids: dict, now_mono: float) -> list:
        """Per-process RSS / CPU% / core affinity for {label: pid}. Read-only."""
        out = []
        seen = set()
        for label, pid in pids.items():
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                continue
            rss_kb = parse_proc_statm(_read(f"/proc/{pid}/statm") or "")
            rss = round(rss_kb / 1024.0, 1) if rss_kb is not None else None
            ticks, last_cpu = parse_proc_stat_times(_read(f"/proc/{pid}/stat") or "")
            cpu_pct = None
            if ticks is not None:
                prev = self._prev_proc.get(pid)
                if prev is not None:
                    prev_ticks, prev_mono = prev
                    elapsed = now_mono - prev_mono
                    dticks = ticks - prev_ticks
                    if elapsed > 0 and dticks >= 0:
                        cpu_pct = round(100.0 * dticks / _CLK_TCK / elapsed, 1)
                self._prev_proc[pid] = (ticks, now_mono)
                seen.add(pid)
            try:
                aff = sorted(os.sched_getaffinity(pid))
            except OSError:
                aff = None
            out.append({"label": label, "pid": pid, "rss_mb": rss,
                        "cpu_pct": cpu_pct, "affinity": aff, "last_cpu": last_cpu})
        # Forget vanished PIDs so a restarted process doesn't carry stale ticks.
        for dead in [p for p in self._prev_proc if p not in seen]:
            self._prev_proc.pop(dead, None)
        return out

    def sample(self, pids: dict | None = None, ready=None) -> dict:
        """Read /proc + /sys, update CPU deltas and high-water-marks, and
        return the current snapshot (also cached for snapshot()). When `pids`
        ({label: pid}) is given, also attach per-process RSS/CPU/affinity.
        `ready` (bool) ends warm-up only once the system is up + settled; omit
        it for the plain DEFAULT_WARMUP_S timer."""
        now_mono = time.monotonic()
        uptime = round(now_mono - self.started_mono, 1)
        reason = self._warmup_state(now_mono, ready)
        snap = {
            "uptime_s": uptime,
            "warmup_s": self.warmup_s,
            "in_warmup": reason is not None,
            "warmup_reason": reason,
        }
        if pids:
            snap["processes"] = self._sample_processes(pids, now_mono)

        mem = _read(_PROC_MEMINFO)
        if mem:
            snap.update(parse_meminfo(mem))
            # zram's real (compressed) physical footprint, when available. This is
            # the number pressure is judged on; "swap used" from meminfo is the
            # uncompressed size (3-4x larger) and is kept only for display.
            zinfo = parse_zram_mm_stat(_read(_SYS_ZRAM_MM) or "")
            if zinfo:
                snap.update(zinfo)
            phys = snap.get("zram_phys_mb")
            if phys is None:          # no zram stats -> fall back to uncompressed
                phys = snap.get("swap_used_mb")
            snap["swap_phys_mb"] = phys
            # GROWTH of the physical footprint since the previous sample.
            if phys is not None:
                if self._prev_swap_phys_mb is not None:
                    snap["swap_growth_mb"] = round(max(0.0, phys - self._prev_swap_phys_mb), 1)
                self._prev_swap_phys_mb = phys

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
        snap["high_water_at"] = dict(self.high_water_at)
        snap["verdict"] = classify(snap, self.thresholds)
        self._last = snap
        return snap

    def snapshot(self) -> dict:
        """Last computed sample (sampling once if none taken yet)."""
        return self._last or self.sample()
