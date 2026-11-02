"""Background A/B archives (/var/lib/diofinder/bg_runs) are capped: loose run
folders go once zipped, and only the newest archives within the size cap stay."""
import pytest

A = pytest.importorskip("webui.app")


def _zip(d, name, size):
    p = d / name
    p.write_bytes(b"x" * size)
    return p


def test_prune_keeps_newest_within_count_and_size(tmp_path):
    for i in range(14):
        _zip(tmp_path, f"bg_ab_20261101_0000{i:02d}.zip", 100)
    run = tmp_path / "20261101_000013"
    (run / "frames").mkdir(parents=True)
    (run / "frames" / "frame_00.png").write_bytes(b"png")
    other = tmp_path / "notes"
    other.mkdir()
    A._bgrun_prune(tmp_path, keep=10, cap_bytes=10_000)
    zips = sorted(p.name for p in tmp_path.glob("bg_ab_*.zip"))
    assert len(zips) == 10 and zips[0] == "bg_ab_20261101_000004.zip"
    assert not run.exists()            # loose frames gone (the zip has them)
    assert other.exists()              # anything else is left alone
    A._bgrun_prune(tmp_path, keep=10, cap_bytes=350)
    assert len(list(tmp_path.glob("bg_ab_*.zip"))) == 3   # size cap: 3 x 100 B


def test_prune_keeps_the_run_in_progress(tmp_path):
    run = tmp_path / "20261101_120000"
    run.mkdir()
    A._bgrun_prune(tmp_path, keep_dir=run)
    assert run.exists()
