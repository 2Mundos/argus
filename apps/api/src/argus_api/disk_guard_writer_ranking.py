"""Bounded writer-ranking measurements for disk guard shed (operations.spec.md 3.5).

Pure parsing, delta accounting, directory-size sampling, and deterministic ranking.
Pause/shed wiring is intentionally absent; callers use these helpers when that slice
lands. No ``docker ps --size``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from argus_api.fleet import workspace_cleanup as wc

log = logging.getLogger(__name__)

MAX_IO_STAT_BYTES = 64 * 1024
MEASUREMENT_TIMEOUT_S = 30.0

RankSource = Literal["wbytes_delta", "ranked_by_size", "unknown"]
SizeSource = Literal["measured", "unknown", "ranked_by_size"]
SampleOutcome = Literal["measured", "unknown", "cancelled", "timeout"]

_DEVICE_RE = re.compile(r"^(\d+:\d+)\s+(.*)$")
_WBYTES_RE = re.compile(r"\bwbytes=(\d+)\b")


@dataclass(frozen=True)
class IoStatSnapshot:
    """Per-device cumulative ``wbytes`` from cgroup v2 ``io.stat``."""

    wbytes_by_device: dict[str, int]
    readable: bool


@dataclass(frozen=True)
class DirectorySizeSample:
    """One bounded workspace directory size sample."""

    bytes: int | None  # ``None`` is unknown; ``0`` is a measured empty tree
    source: SizeSource
    outcome: SampleOutcome
    reason: str | None = None


@dataclass(frozen=True)
class WriterCandidate:
    container_id: str
    write_delta: int | None = None
    directory_bytes: int | None = None
    rank_source: RankSource = "unknown"
    size_source: SizeSource = "unknown"


def parse_io_stat(text: str, *, max_bytes: int = MAX_IO_STAT_BYTES) -> IoStatSnapshot:
    """Parse cgroup v2 ``io.stat`` content with a byte bound on the input."""
    raw = text if len(text) <= max_bytes else text[:max_bytes]
    devices: dict[str, int] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _DEVICE_RE.match(line)
        if m is None:
            continue
        dev, rest = m.group(1), m.group(2)
        wm = _WBYTES_RE.search(rest)
        if wm is None:
            continue
        try:
            devices[dev] = int(wm.group(1))
        except ValueError:
            continue
    return IoStatSnapshot(devices, readable=True)


def read_io_stat(path: Path, *, max_bytes: int = MAX_IO_STAT_BYTES) -> IoStatSnapshot:
    """Read ``io.stat`` from disk; unreadable paths yield ``readable=False``."""
    try:
        with path.open(encoding="utf-8") as f:
            chunk = f.read(max_bytes + 1)
    except OSError:
        return IoStatSnapshot({}, readable=False)
    if len(chunk) > max_bytes:
        chunk = chunk[:max_bytes]
    snap = parse_io_stat(chunk, max_bytes=max_bytes)
    return snap


def compute_write_delta(before: IoStatSnapshot, after: IoStatSnapshot) -> int | None:
    """Sum per-device ``wbytes`` deltas; handle counter resets; ``None`` if unknown."""
    if not before.readable or not after.readable:
        return None
    total = 0
    for dev in set(before.wbytes_by_device) | set(after.wbytes_by_device):
        b = before.wbytes_by_device.get(dev)
        a = after.wbytes_by_device.get(dev)
        if a is None:
            continue
        if b is None:
            total += a
        elif a >= b:
            total += a - b
        else:
            total += a
    return total


def sample_instance_directory(
    instances_root: str,
    instance_id: str,
    uid: int,
    *,
    rank_by_size: bool = False,
) -> DirectorySizeSample:
    """One bounded directory-size sample using the workspace-cleanup walker."""
    try:
        tree = wc.measure(instances_root, instance_id, uid)
    except wc.Unsafe as exc:
        return DirectorySizeSample(
            None,
            "unknown",
            "unknown",
            reason=exc.code,
        )
    except OSError:
        return DirectorySizeSample(None, "unknown", "unknown", reason="io_error")
    source: SizeSource = "ranked_by_size" if rank_by_size else "measured"
    return DirectorySizeSample(tree.bytes, source, "measured")


def _rank_key(candidate: WriterCandidate, *, by_size: bool) -> tuple[object, ...]:
    cid = candidate.container_id
    if by_size:
        size = candidate.directory_bytes
        if size is None:
            return (1, 0, cid)
        return (0, -size, cid)
    delta = candidate.write_delta
    tie = candidate.directory_bytes or 0
    if delta is None:
        return (1, -tie, cid)
    return (0, -delta, -tie, cid)


def rank_writers(
    candidates: tuple[WriterCandidate, ...],
) -> tuple[WriterCandidate, ...]:
    """Deterministic ordering: wbytes delta when any readable, else directory size."""
    if not candidates:
        return ()
    any_delta = any(c.write_delta is not None for c in candidates)
    any_size = any(c.directory_bytes is not None for c in candidates)
    if any_delta:
        mode: RankSource = "wbytes_delta"
        ordered = sorted(candidates, key=lambda c: _rank_key(c, by_size=False))
        return tuple(
            WriterCandidate(
                c.container_id,
                c.write_delta,
                c.directory_bytes,
                mode if c.write_delta is not None else "unknown",
                c.size_source,
            )
            for c in ordered
        )
    if any_size:
        ordered = sorted(candidates, key=lambda c: _rank_key(c, by_size=True))
        return tuple(
            WriterCandidate(
                c.container_id,
                c.write_delta,
                c.directory_bytes,
                "ranked_by_size",
                c.size_source if c.size_source != "unknown" else "ranked_by_size",
            )
            for c in ordered
        )
    return tuple(
        WriterCandidate(c.container_id, None, None, "unknown", "unknown")
        for c in candidates
    )


def build_candidates(
    containers: tuple[str, ...],
    *,
    io_before: dict[str, IoStatSnapshot],
    io_after: dict[str, IoStatSnapshot],
    directory_bytes: dict[str, DirectorySizeSample],
) -> tuple[WriterCandidate, ...]:
    """Join io deltas and optional directory samples into rankable candidates."""
    out: list[WriterCandidate] = []
    for cid in containers:
        before = io_before.get(cid, IoStatSnapshot({}, readable=False))
        after = io_after.get(cid, IoStatSnapshot({}, readable=False))
        delta = compute_write_delta(before, after)
        dir_sample = directory_bytes.get(
            cid,
            DirectorySizeSample(None, "unknown", "unknown"),
        )
        out.append(
            WriterCandidate(
                cid,
                delta,
                dir_sample.bytes,
                "unknown",
                dir_sample.source,
            )
        )
    return tuple(out)


async def sample_instance_directory_bounded(
    instances_root: str,
    instance_id: str,
    uid: int,
    *,
    timeout_s: float = MEASUREMENT_TIMEOUT_S,
    rank_by_size: bool = False,
    cancel: Callable[[], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> DirectorySizeSample:
    """Run ``sample_instance_directory`` off-loop with timeout and cancellation."""
    if cancel is not None and cancel():
        return DirectorySizeSample(None, "unknown", "cancelled", reason="cancelled")
    started = clock()
    try:
        async with asyncio.timeout(timeout_s):
            return await asyncio.to_thread(
                sample_instance_directory,
                instances_root,
                instance_id,
                uid,
                rank_by_size=rank_by_size,
            )
    except TimeoutError:
        return DirectorySizeSample(None, "unknown", "timeout", reason="timeout")
    except asyncio.CancelledError:
        raise
    finally:
        elapsed = clock() - started
        if elapsed > timeout_s:
            log.debug(
                "directory sample for %s exceeded budget (%.2fs)",
                instance_id,
                elapsed,
            )


def sample_io_stat_live(cgroup_io_stat: Path) -> IoStatSnapshot:
    """Real-host read of ``io.stat`` (for optional integration checks only)."""
    return read_io_stat(cgroup_io_stat)


def probe_live_cgroup_io() -> IoStatSnapshot:
    """Real-host probe of this process ``io.stat`` when the kernel exposes it."""
    for path in (Path("/sys/fs/cgroup/self/io.stat"), Path("/sys/fs/cgroup/io.stat")):
        if path.is_file():
            return read_io_stat(path)
    return IoStatSnapshot({}, readable=False)
