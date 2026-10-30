"""What this node can actually run: measured from the targets, never configured
(worker launch 3). Every probe is read-only and bounded in time."""

import asyncio
from pathlib import Path

from argus_core.fleet import (
    Check,
    FleetCapabilities,
    Pool,
    PoolCapability,
    RuntimeKind,
    TargetCapability,
    TargetKind,
)

from argus_api.fleet import runtimes
from argus_api.fleet.runtimes import session_quota_env
from argus_api.fleet.targets import TargetError, TargetInfo, TargetRegistry

PROBE_TIMEOUT_S = 5.0

_Location = tuple[str, str | None, str | None]  # (kind, host/context, namespace)


def _location(pool: Pool) -> _Location:
    return (pool.target.value, *runtimes.target_location(pool))


async def _probe(
    targets: TargetRegistry, loc: _Location
) -> tuple[TargetCapability, TargetInfo | None]:
    kind, host, namespace = loc
    where = host if kind == "docker" else f"{host}/{namespace}"
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_S):
            info = await targets.get(kind, host, namespace).probe()
    except (TargetError, TimeoutError, OSError) as exc:
        reason = str(exc) or "timed out"
        return (
            TargetCapability(
                kind=TargetKind(kind),
                location=where,
                available=False,
                error=reason,
                version=None,
                runtimes=[],
                cpus=None,
                memory_mb=None,
            ),
            None,
        )
    return (
        TargetCapability(
            kind=TargetKind(kind),
            location=where,
            available=True,
            error=None,
            version=info.version,
            runtimes=info.runtimes,
            cpus=info.cpus,
            memory_mb=info.memory_mb,
        ),
        info,
    )


async def _image_check(
    targets: TargetRegistry, loc: _Location, image: str, label: str
) -> Check:
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_S):
            present = await targets.get(*loc).image_present(image)
    except (TargetError, TimeoutError, OSError) as exc:
        return Check(name=label, ok=None, detail=f"could not check: {exc}")
    if present is None:
        return Check(name=label, ok=None, detail=f"{image}: not verifiable here")
    if present:
        return Check(name=label, ok=True, detail=f"{image} is on the target")
    return Check(
        name=label, ok=None, detail=f"{image} is not on the target; pulled on start"
    )


def path_check(
    label: str, path: str, shares_filesystem: bool, *, is_dir: bool
) -> Check:
    """A host path is verified only if the Docker daemon provably uses this
    process's filesystem; otherwise it is unverifiable (ok=None), never assumed."""
    if not shares_filesystem:
        return Check(
            name=label,
            ok=None,
            detail=f"{path}: cannot be verified, the Docker daemon is remote or in a VM",
        )
    p = Path(path)
    ok = p.is_dir() if is_dir else p.exists()
    return Check(
        name=label, ok=ok, detail=f"{path} {'exists' if ok else 'does not exist'}"
    )


def session_quota_check(pool: Pool) -> Check | None:
    """Evidence that a NanoClaw pool with cpus/memory will give sessions a quota.

    Confirmed statically: Argus sets `CONTAINER_*_LIMIT` from the pool request
    (worker-launch 3). Live `--cpus`/`--memory` on a spawned session is the
    session-quota smoke runbook, not this probe. Omitted when the pool is not
    NanoClaw or requests no quota.
    """
    if pool.runtime != RuntimeKind.NANOCLAW:
        return None
    if pool.cpus is None and pool.memory_mb is None:
        return None
    env = session_quota_env(pool)
    if not env:
        return Check(
            name="session_quota",
            ok=False,
            detail="pool requests a quota but session env was not composed",
        )
    parts = [f"{k}={v}" for k, v in sorted(env.items())]
    return Check(
        name="session_quota",
        ok=True,
        detail=(
            "sessions inherit the pool quota via "
            + ", ".join(parts)
            + " (composeSessionSpec.resources)"
        ),
    )


async def judge_pool(
    pool: Pool,
    targets: TargetRegistry,
    public_url_configured: bool,
    probed: tuple[TargetCapability, TargetInfo | None],
) -> PoolCapability:
    cap, info = probed
    loc = _location(pool)
    checks = [
        Check(
            name="public_url",
            ok=public_url_configured,
            detail="ARGUS_PUBLIC_URL is set"
            if public_url_configured
            else "ARGUS_PUBLIC_URL is not set: containers cannot reach Argus",
        ),
        Check(
            name="target",
            ok=cap.available,
            detail=f"{pool.target.value} reachable"
            if cap.available
            else f"{pool.target.value} unreachable: {cap.error}",
        ),
    ]
    free = max(pool.max_replicas - pool.live_instances, 0)
    checks.append(
        Check(
            name="capacity",
            ok=free > 0,
            detail=f"{pool.live_instances} of {pool.max_replicas} slots used",
        )
    )
    if (quota := session_quota_check(pool)) is not None:
        checks.append(quota)
    if info is not None:
        if pool.docker.runtime:
            have = pool.docker.runtime in info.runtimes
            checks.append(
                Check(
                    name="oci_runtime",
                    ok=have,
                    detail=f"{pool.docker.runtime} "
                    + (
                        "is available" if have else f"not offered (has {info.runtimes})"
                    ),
                )
            )
        for name, want, got in (
            ("cpus", pool.cpus, info.cpus),
            ("memory", pool.memory_mb, info.memory_mb),
        ):
            if want is not None and got is not None:
                checks.append(
                    Check(
                        name=name,
                        ok=want <= got,
                        detail=f"requests {want}, node has {got}",
                    )
                )
        checks.append(await _image_check(targets, loc, pool.image, "image"))
        if pool.runtime == RuntimeKind.NANOCLAW and pool.nanoclaw:
            nc = pool.nanoclaw
            fs = info.shares_filesystem
            checks += [
                path_check("nanoclaw_install_dir", nc.install_dir, fs, is_dir=True),
                path_check(
                    "nanoclaw_instances_root", nc.instances_root, fs, is_dir=True
                ),
                await _image_check(
                    targets, loc, nc.agent_image, "nanoclaw_agent_image"
                ),
            ]
            if not pool.docker.runtime:
                checks.append(
                    path_check(
                        "docker_socket",
                        nc.docker_socket or "/var/run/docker.sock",
                        fs,
                        is_dir=False,
                    )
                )
    blockers = [c.detail for c in checks if c.ok is False]
    return PoolCapability(
        name=pool.name,
        target=pool.target,
        runtime=pool.runtime,
        launchable=not blockers,
        free_slots=free,
        checks=checks,
        blockers=blockers,
    )


async def discover(
    pools: list[Pool], targets: TargetRegistry, public_url: str | None
) -> FleetCapabilities:
    locs = list(dict.fromkeys([("docker", None, None), *map(_location, pools)]))
    probed = dict(
        zip(
            locs, await asyncio.gather(*(_probe(targets, k) for k in locs)), strict=True
        )
    )
    judged = await asyncio.gather(
        *(
            judge_pool(p, targets, public_url is not None, probed[_location(p)])
            for p in pools
        )
    )
    return FleetCapabilities(
        public_url_configured=public_url is not None,
        targets=[cap for cap, _ in probed.values()],
        pools=list(judged),
    )


async def discover_pool(
    pool: Pool, targets: TargetRegistry, public_url: str | None
) -> PoolCapability:
    probed = await _probe(targets, _location(pool))
    return await judge_pool(pool, targets, public_url is not None, probed)
