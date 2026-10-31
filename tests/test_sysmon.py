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


def test_classify_comfortable():
    snap = {"mem_available_pct": 23.6, "swap_used_mb": 0.0,
            "cpu_pct": {"cpu": 30.0, "cpu0": 40.0}, "temp_c": 55.0,
            "high_water": {"min_mem_available_pct": 23.6, "max_swap_used_mb": 0.0,
                           "max_cpu0_pct": 40.0, "max_temp_c": 55.0}}
    v = sysmon.classify(snap)
    assert v["level"] == "comfortable"
    assert v["reasons"] == []


def test_classify_tight_on_swap_highwater():
    # instantaneous swap is 0 now, but it peaked -> high-water drives the verdict
    snap = {"mem_available_pct": 25.0, "swap_used_mb": 0.0,
            "cpu_pct": {"cpu0": 50.0}, "temp_c": 50.0,
            "high_water": {"min_mem_available_pct": 25.0, "max_swap_used_mb": 20.0,
                           "max_cpu0_pct": 50.0, "max_temp_c": 50.0}}
    v = sysmon.classify(snap)
    assert v["level"] == "tight"
    assert any("swap" in r for r in v["reasons"])


def test_classify_constrained_escalates_to_worst():
    snap = {"high_water": {"min_mem_available_pct": 25.0, "max_swap_used_mb": 20.0,
                           "max_cpu0_pct": 97.0, "max_temp_c": 50.0}}
    v = sysmon.classify(snap)
    assert v["level"] == "constrained"          # CPU0 97% -> constrained, worst wins
    assert any("CPU 0" in r for r in v["reasons"])
    assert any("swap" in r for r in v["reasons"])  # tight reason still listed
