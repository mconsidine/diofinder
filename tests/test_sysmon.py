"""Unit tests for the resource-headroom monitor's pure logic (no /proc)."""

from diofinder import sysmon


MEMINFO = """MemTotal:         508000 kB
MemFree:           40000 kB
MemAvailable:     120000 kB
Buffers:            2000 kB
SwapTotal:        254000 kB
SwapFree:         254000 kB
"""

MEMINFO_TIGHT = MEMINFO.replace("MemAvailable:     120000 kB",
                                "MemAvailable:      30000 kB").replace(
                                "SwapFree:         254000 kB",
                                "SwapFree:         154000 kB")  # 100000 kB swap used


def test_parse_meminfo():
    m = sysmon.parse_meminfo(MEMINFO)
    assert m["mem_total_kb"] == 508000
    assert m["mem_available_kb"] == 120000
    assert m["mem_available_pct"] == round(100 * 120000 / 508000, 1)
    assert m["swap_used_kb"] == 0
    assert m["swap_used_mb"] == 0.0


def test_parse_meminfo_swap_used():
    m = sysmon.parse_meminfo(MEMINFO_TIGHT)
    assert m["swap_used_kb"] == 100000
    assert m["swap_used_mb"] == round(100000 / 1024.0, 1)


def test_cpu_pct_from_deltas():
    # cpu line: user nice system idle iowait irq softirq ...
    s0 = "cpu  100 0 50 1000 0 0 0\ncpu0 100 0 50 1000 0 0 0\n"
    # +100 busy (user), +0 idle over the interval -> 100% busy for cpu0
    s1 = "cpu  200 0 50 1000 0 0 0\ncpu0 200 0 50 1000 0 0 0\n"
    prev = sysmon.parse_stat_cpu(s0)
    cur = sysmon.parse_stat_cpu(s1)
    pct = sysmon.cpu_pct_from_deltas(prev, cur)
    assert pct["cpu0"] == 100.0
    # half busy case
    s2 = "cpu0 250 0 50 1050 0 0 0\n"  # +50 busy, +50 idle vs s1 -> 50%
    pct2 = sysmon.cpu_pct_from_deltas(sysmon.parse_stat_cpu(s1), sysmon.parse_stat_cpu(s2))
    assert pct2["cpu0"] == 50.0


def test_parse_proc_statm():
    # fields: size resident shared text lib data dt  (resident = field 2)
    assert sysmon.parse_proc_statm("50000 12000 3000 10 0 8000 0") == round(12000 * sysmon._PAGE_KB, 1)
    assert sysmon.parse_proc_statm("garbage") is None


def test_parse_proc_stat_times():
    # comm with spaces + parens must not break field parsing.
    # Build: pid (comm) state ... utime(f14) stime(f15) ... processor(f39)
    fields3plus = ["R"] + [str(i) for i in range(1, 50)]  # state then f4..f52
    # after[11] = f14 = value "11"; after[12] = f15 = "12"; after[36] = f39 = "36"
    line = "123 (py thread (x)) " + " ".join(fields3plus)
    ticks, last_cpu = sysmon.parse_proc_stat_times(line)
    assert ticks == 11 + 12
    assert last_cpu == 36


def test_parse_temp_and_loadavg():
    assert sysmon.parse_temp_c("48312\n") == 48.3
    assert sysmon.parse_temp_c("garbage") is None
    assert sysmon.parse_loadavg("0.50 0.30 0.10 1/200 1234") == (0.5, 0.3, 0.1)
    assert sysmon.parse_loadavg("") is None


def test_parse_zram_mm_stat():
    # orig compr mem_used_total ... (bytes). Real device reading:
    # 150.2M uncompressed, 37.5M compressed, 40.4M physical.
    text = "157498982 39321600 42362880 435159040 42362880 0 0 0 0"
    z = sysmon.parse_zram_mm_stat(text)
    assert z["zram_orig_mb"] == round(157498982 / 1048576.0, 1)
    assert z["zram_phys_mb"] == round(42362880 / 1048576.0, 1)
    assert z["zram_ratio"] == round(157498982 / 42362880, 1)  # ~3.7x
    assert sysmon.parse_zram_mm_stat("garbage") is None


def _hw(**kw):
    base = {"min_mem_available_pct": None, "max_swap_used_mb": 0.0,
            "max_swap_phys_mb": 0.0, "max_swap_growth_mb": 0.0,
            "max_swap_growth_streak": 0, "max_mem_low_streak": 0,
            "max_cpu_pct": 0.0, "max_cpu0_pct": 0.0, "max_temp_c": None}
    base.update(kw)
    return base


def test_classify_warming_up():
    v = sysmon.classify({"in_warmup": True, "high_water": _hw(max_cpu0_pct=100.0)})
    assert v["level"] == "warming_up"
    assert v["reasons"] == []


def test_classify_comfortable():
    snap = {"in_warmup": False, "mem_total_kb": 508000,
            "high_water": _hw(min_mem_available_pct=35.0, max_cpu0_pct=40.0,
                              max_temp_c=55.0)}
    v = sysmon.classify(snap)
    assert v["level"] == "comfortable"
    assert v["reasons"] == []


def test_classify_static_swap_occupancy_is_benign():
    # 150 MB uncompressed sitting in zram (the real-world reading), compressing
    # to ~40 MB physical, not growing -> must NOT flag. This is the exact false
    # alarm this recalibration fixes.
    snap = {"in_warmup": False, "mem_total_kb": 508000,
            "high_water": _hw(min_mem_available_pct=20.0, max_swap_used_mb=150.0,
                              max_swap_phys_mb=40.0, max_swap_growth_mb=6.0,
                              max_swap_growth_streak=1, max_cpu0_pct=45.0)}
    assert sysmon.classify(snap)["level"] == "comfortable"


def test_classify_single_burst_is_benign():
    # A lone page-render eviction: one big growth sample, streak 1 -> no flag.
    snap = {"in_warmup": False, "mem_total_kb": 508000,
            "high_water": _hw(min_mem_available_pct=25.0, max_swap_growth_mb=31.0,
                              max_swap_growth_streak=1, max_cpu0_pct=50.0)}
    assert sysmon.classify(snap)["level"] == "comfortable"


def test_classify_tight_on_sustained_swap_streak():
    # Three consecutive growing samples (~15 s of real eviction) -> tight.
    snap = {"in_warmup": False, "mem_total_kb": 508000,
            "high_water": _hw(min_mem_available_pct=25.0, max_swap_growth_mb=6.0,
                              max_swap_growth_streak=3, max_cpu0_pct=50.0)}
    v = sysmon.classify(snap)
    assert v["level"] == "tight"
    assert any("leaning on swap" in r for r in v["reasons"])


def test_classify_swap_backstop_uses_physical():
    # A large PHYSICAL footprint (>40% of RAM) flags even without growth;
    # the uncompressed occupancy alone must not.
    snap = {"in_warmup": False, "mem_total_kb": 508000,
            "high_water": _hw(max_swap_used_mb=400.0, max_swap_phys_mb=220.0)}
    v = sysmon.classify(snap)
    assert v["level"] == "tight"
    assert any("held in zram" in r for r in v["reasons"])


def test_classify_constrained_escalates_to_worst():
    snap = {"in_warmup": False, "mem_total_kb": 508000,
            "high_water": _hw(min_mem_available_pct=25.0, max_swap_growth_mb=6.0,
                              max_swap_growth_streak=3, max_cpu0_pct=99.0,
                              max_temp_c=50.0)}
    v = sysmon.classify(snap)
    assert v["level"] == "constrained"          # CPU0 99% -> constrained, worst wins
    assert any("CPU 0" in r for r in v["reasons"])
    assert any("leaning on swap" in r for r in v["reasons"])  # tight reason still listed


def test_swap_growth_streak_counts_consecutive():
    sm = sysmon.SysMon(warmup_s=0.0)

    def feed(growth):
        sm._bump_marks({"in_warmup": False, "swap_growth_mb": growth,
                        "swap_phys_mb": 40.0, "swap_used_mb": 150.0})

    feed(6.0); feed(6.0)                 # two consecutive growing samples
    assert sm.high_water["max_swap_growth_streak"] == 2
    feed(0.0)                            # quiet sample resets the streak
    assert sm._swap_growth_streak == 0
    feed(6.0)                            # grows again
    assert sm._swap_growth_streak == 1
    assert sm.high_water["max_swap_growth_streak"] == 2   # max retained


# --- Available-RAM: warn needs a sustained streak, crit stays single-sample ---

def test_classify_single_ram_dip_is_benign():
    # The real cold-boot reading: one dip to 14% (just under the 15% warn line)
    # at the kernel's first zram absorption, then recovery -> must NOT flag.
    snap = {"in_warmup": False, "mem_total_kb": 425000,
            "high_water": _hw(min_mem_available_pct=14.0, max_mem_low_streak=1,
                              max_cpu0_pct=40.0)}
    assert sysmon.classify(snap)["level"] == "comfortable"


def test_classify_tight_on_sustained_low_ram():
    snap = {"in_warmup": False, "mem_total_kb": 425000,
            "high_water": _hw(min_mem_available_pct=12.0, max_mem_low_streak=3)}
    v = sysmon.classify(snap)
    assert v["level"] == "tight"
    assert any("stayed below 15%" in r for r in v["reasons"])


def test_classify_ram_crit_is_single_sample():
    # OOM risk is instantaneous: even a lone dip below 8% is constrained.
    snap = {"in_warmup": False, "mem_total_kb": 425000,
            "high_water": _hw(min_mem_available_pct=6.0, max_mem_low_streak=1)}
    v = sysmon.classify(snap)
    assert v["level"] == "constrained"
    assert any("fell to 6%" in r for r in v["reasons"])


def test_mem_low_streak_counts_and_resets():
    sm = sysmon.SysMon(warmup_s=0.0)

    def feed(pct):
        sm._bump_marks({"in_warmup": False, "mem_available_pct": pct})

    feed(14.0); feed(13.0)
    assert sm._mem_low_streak == 2
    feed(30.0)                                   # recovery resets
    assert sm._mem_low_streak == 0
    feed(14.0)
    assert sm.high_water["max_mem_low_streak"] == 2


# --- Readiness-gated warm-up ---

def test_warmup_waits_for_ready_then_settles():
    sm = sysmon.SysMon(warmup_s=90.0, settle_s=30.0, max_warmup_s=300.0)
    sm.started_mono = 0.0
    assert sm._warmup_state(50.0, False) == "starting"          # inside the floor
    assert sm._warmup_state(100.0, False) == "waiting for solver / web UI"
    assert sm._warmup_state(110.0, True) == "settling"          # ready latched at 110
    assert sm._warmup_state(139.0, True) == "settling"
    assert sm._warmup_state(141.0, True) is None                # 110 + 30 passed
    # Latched: a later not-ready report does not re-open warm-up.
    assert sm._warmup_state(150.0, False) is None


def test_warmup_ready_before_floor_still_waits_for_floor():
    sm = sysmon.SysMon(warmup_s=90.0, settle_s=30.0, max_warmup_s=300.0)
    sm.started_mono = 0.0
    assert sm._warmup_state(20.0, True) == "starting"           # ready early
    assert sm._warmup_state(91.0, True) is None                 # 20 + 30 < 91


def test_warmup_cap_when_never_ready():
    sm = sysmon.SysMon(warmup_s=90.0, settle_s=30.0, max_warmup_s=300.0)
    sm.started_mono = 0.0
    assert sm._warmup_state(299.0, False) == "waiting for solver / web UI"
    assert sm._warmup_state(301.0, False) is None                # cap: judge anyway


def test_warmup_plain_timer_without_ready_signal():
    sm = sysmon.SysMon(warmup_s=90.0)
    sm.started_mono = 0.0
    assert sm._warmup_state(89.0, None) == "starting"
    assert sm._warmup_state(91.0, None) is None


# --- When each worst reading happened ---

def test_high_water_at_records_uptime_of_each_mark():
    sm = sysmon.SysMon(warmup_s=0.0)
    sm._bump_marks({"in_warmup": False, "uptime_s": 112.0,
                    "mem_available_pct": 14.0, "cpu_pct": {"cpu0": 40.0}})
    sm._bump_marks({"in_warmup": False, "uptime_s": 300.0,
                    "mem_available_pct": 30.0, "cpu_pct": {"cpu0": 55.0}})
    assert sm.high_water_at["min_mem_available_pct"] == 112.0   # min didn't move
    assert sm.high_water_at["max_cpu0_pct"] == 300.0            # max moved later
