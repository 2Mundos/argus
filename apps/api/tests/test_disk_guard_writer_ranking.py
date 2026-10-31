"""Disk guard writer ranking (operations.spec.md 3.5) — fixtures unless noted."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from argus_api.disk_guard_writer_ranking import (
    DirectorySizeSample,
    IoStatSnapshot,
    WriterCandidate,
    build_candidates,
    compute_write_delta,
    parse_io_stat,
    probe_live_cgroup_io,
    rank_writers,
    read_io_stat,
    sample_instance_directory,
    sample_instance_directory_bounded,
)

FIXTURE_IO = """\
259:0 rbytes=100 wbytes=1000 rios=1 wios=10 dbytes=0 dios=0
8:0 rbytes=50 wbytes=500 rios=1 wios=5 dbytes=0 dios=0
"""

FIXTURE_IO_AFTER = """\
259:0 rbytes=200 wbytes=1600 rios=2 wios=16 dbytes=0 dios=0
8:0 rbytes=50 wbytes=700 rios=1 wios=7 dbytes=0 dios=0
"""


def test_parse_io_stat_per_device_wbytes() -> None:
    snap = parse_io_stat(FIXTURE_IO)
    assert snap.readable is True
    assert snap.wbytes_by_device == {"259:0": 1000, "8:0": 500}


def test_parse_io_stat_skips_malformed_and_empty() -> None:
    text = "not a line\n259:0 rbytes=1\n8:0 wbytes=notint\n259:1 wbytes=42\n"
    snap = parse_io_stat(text)
    assert snap.wbytes_by_device == {"259:1": 42}


def test_parse_io_stat_bounded_input() -> None:
    huge = "259:0 wbytes=1\n" * 10_000
    snap = parse_io_stat(huge, max_bytes=32)
    assert snap.readable is True
    assert len(snap.wbytes_by_device) <= 2


def test_read_io_stat_missing_is_unreadable(tmp_path: Path) -> None:
    snap = read_io_stat(tmp_path / "missing")
    assert snap.readable is False
    assert snap.wbytes_by_device == {}


def test_compute_write_delta_sums_devices() -> None:
    before = parse_io_stat(FIXTURE_IO)
    after = parse_io_stat(FIXTURE_IO_AFTER)
    assert compute_write_delta(before, after) == 800  # 600 + 200


def test_compute_write_delta_handles_counter_reset() -> None:
    before = IoStatSnapshot({"259:0": 5000}, readable=True)
    after = IoStatSnapshot({"259:0": 100}, readable=True)
    assert compute_write_delta(before, after) == 100


def test_compute_write_delta_new_device_counts_full_after() -> None:
    before = IoStatSnapshot({"8:0": 10}, readable=True)
    after = IoStatSnapshot({"8:0": 10, "259:0": 50}, readable=True)
    assert compute_write_delta(before, after) == 50


def test_compute_write_delta_unknown_when_unreadable() -> None:
    before = IoStatSnapshot({}, readable=False)
    after = parse_io_stat(FIXTURE_IO)
    assert compute_write_delta(before, after) is None


def test_rank_by_wbytes_delta_with_directory_tiebreak() -> None:
    candidates = (
        WriterCandidate("b", 100, 5000, size_source="measured"),
        WriterCandidate("a", 100, 9000, size_source="measured"),
        WriterCandidate("c", 200, 100, size_source="measured"),
    )
    ranked = rank_writers(candidates)
    assert [c.container_id for c in ranked] == ["c", "a", "b"]
    assert ranked[0].rank_source == "wbytes_delta"
    assert ranked[1].rank_source == "wbytes_delta"


def test_rank_by_size_when_no_io() -> None:
    candidates = (
        WriterCandidate("x", None, 100, size_source="ranked_by_size"),
        WriterCandidate("y", None, 500, size_source="ranked_by_size"),
    )
    ranked = rank_writers(candidates)
    assert [c.container_id for c in ranked] == ["y", "x"]
    assert all(c.rank_source == "ranked_by_size" for c in ranked)


def test_rank_unknown_when_no_signals() -> None:
    ranked = rank_writers((WriterCandidate("z"),))
    assert ranked[0].rank_source == "unknown"
    assert ranked[0].size_source == "unknown"


def test_build_candidates_joins_io_and_directory() -> None:
    ranked = rank_writers(
        build_candidates(
            ("a", "b"),
            io_before={
                "a": parse_io_stat("259:0 wbytes=10\n"),
                "b": parse_io_stat("259:0 wbytes=10\n"),
            },
            io_after={
                "a": parse_io_stat("259:0 wbytes=100\n"),
                "b": parse_io_stat("259:0 wbytes=50\n"),
            },
            directory_bytes={
                "a": DirectorySizeSample(1000, "measured", "measured"),
                "b": DirectorySizeSample(2000, "measured", "measured"),
            },
        )
    )
    assert [c.container_id for c in ranked] == ["a", "b"]


def test_sample_instance_directory_measured(tmp_path: Path) -> None:
    inst = tmp_path / "inst-id"
    inst.mkdir()
    (inst / "f").write_bytes(b"x" * 4096)
    sample = sample_instance_directory(str(tmp_path), "inst-id", os.geteuid())
    assert sample.outcome == "measured"
    assert sample.source == "measured"
    assert sample.bytes is not None and sample.bytes > 0


def test_sample_instance_directory_zero_is_not_unknown(tmp_path: Path) -> None:
    inst = tmp_path / "empty-id"
    inst.mkdir()
    sample = sample_instance_directory(str(tmp_path), "empty-id", os.geteuid())
    assert sample.bytes == 0
    assert sample.source == "measured"


def test_sample_instance_directory_symlink_not_followed(tmp_path: Path) -> None:
    inst = tmp_path / "inst"
    inst.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    big = outside / "big"
    big.write_bytes(b"z" * 8192)
    (inst / "link").symlink_to(big)
    sample = sample_instance_directory(str(tmp_path), "inst", os.geteuid())
    assert sample.bytes == 0


def test_sample_instance_directory_rank_by_size_source(tmp_path: Path) -> None:
    inst = tmp_path / "inst-id"
    inst.mkdir()
    sample = sample_instance_directory(
        str(tmp_path), "inst-id", os.geteuid(), rank_by_size=True
    )
    assert sample.source == "ranked_by_size"
    assert sample.outcome == "measured"


def test_sample_instance_directory_inaccessible(tmp_path: Path) -> None:
    inst = tmp_path / "secret"
    inst.mkdir(mode=0o000)
    try:
        sample = sample_instance_directory(str(tmp_path), "secret", os.geteuid())
    finally:
        inst.chmod(0o700)
    assert sample.bytes is None
    assert sample.source == "unknown"
    assert sample.reason == "permission_denied"


@pytest.mark.asyncio
async def test_bounded_sample_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    inst = tmp_path / "slow"
    inst.mkdir()

    def slow(*_args: object, **_kwargs: object) -> DirectorySizeSample:
        import time

        time.sleep(2)
        return DirectorySizeSample(0, "measured", "measured")

    monkeypatch.setattr(
        "argus_api.disk_guard_writer_ranking.sample_instance_directory",
        slow,
    )
    sample = await sample_instance_directory_bounded(
        str(tmp_path),
        "slow",
        os.geteuid(),
        timeout_s=0.05,
    )
    assert sample.outcome == "timeout"
    assert sample.bytes is None


@pytest.mark.asyncio
async def test_bounded_sample_cancelled() -> None:
    sample = await sample_instance_directory_bounded(
        "/tmp",
        "nope",
        0,
        cancel=lambda: True,
    )
    assert sample.outcome == "cancelled"
    assert sample.bytes is None


@pytest.mark.asyncio
async def test_bounded_sample_propagates_task_cancel(tmp_path: Path) -> None:
    inst = tmp_path / "inst"
    inst.mkdir()

    async def run() -> None:
        task = asyncio.create_task(
            sample_instance_directory_bounded(
                str(tmp_path), "inst", os.geteuid(), timeout_s=30.0
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        await run()


def test_live_cgroup_io_stat_labeled_real_host() -> None:
    """Real-host measurement (skipped when ``io.stat`` is not exposed here)."""
    snap = probe_live_cgroup_io()
    if not snap.readable:
        pytest.skip("no cgroup io.stat on this host")
    assert isinstance(snap.wbytes_by_device, dict)
