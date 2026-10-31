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


def test_parse_temp_and_loadavg():
    assert sysmon.parse_temp_c("48312\n") == 48.3
    assert sysmon.parse_temp_c("garbage") is None
    assert sysmon.parse_loadavg("0.50 0.30 0.10 1/200 1234") == (0.5, 0.3, 0.1)
    assert sysmon.parse_loadavg("") is None


def _hw(**kw):
    base = {"min_mem_available_pct": None, "max_swap_used_mb": 0.0,
            "max_swap_growth_mb": 0.0, "max_cpu_pct": 0.0, "max_cpu0_pct": 0.0,
            "max_temp_c": None}
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
    # 26 MB sitting in zram from boot, not growing, well under the backstop ->
    # must NOT flag (the real-world false alarm this fix addresses).
    snap = {"in_warmup": False, "mem_total_kb": 508000,
            "high_water": _hw(min_mem_available_pct=30.0, max_swap_used_mb=26.0,
                              max_swap_growth_mb=0.0, max_cpu0_pct=50.0)}
    assert sysmon.classify(snap)["level"] == "comfortable"


def test_classify_tight_on_swap_growth():
    snap = {"in_warmup": False, "mem_total_kb": 508000,
            "high_water": _hw(min_mem_available_pct=25.0, max_swap_growth_mb=6.0,
                              max_cpu0_pct=50.0)}
    v = sysmon.classify(snap)
    assert v["level"] == "tight"
    assert any("swapping" in r for r in v["reasons"])


def test_classify_swap_backstop():
    # huge occupancy (>30% of RAM) flags even without growth
    snap = {"in_warmup": False, "mem_total_kb": 508000,
            "high_water": _hw(max_swap_used_mb=200.0)}
    v = sysmon.classify(snap)
    assert v["level"] == "tight"
    assert any("swapped out" in r for r in v["reasons"])


def test_classify_constrained_escalates_to_worst():
    snap = {"in_warmup": False, "mem_total_kb": 508000,
            "high_water": _hw(min_mem_available_pct=25.0, max_swap_growth_mb=6.0,
                              max_cpu0_pct=99.0, max_temp_c=50.0)}
    v = sysmon.classify(snap)
    assert v["level"] == "constrained"          # CPU0 99% -> constrained, worst wins
    assert any("CPU 0" in r for r in v["reasons"])
    assert any("swapping" in r for r in v["reasons"])  # tight reason still listed
