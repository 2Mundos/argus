"""What this node can actually run: measured from the targets, never configured
(worker launch 3). Every probe is read-only and bounded in time."""

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite
from argus_core.fleet import (
    Check,
    FleetCapabilities,
    Pool,
    PoolCapability,
    RuntimeKind,
    TargetCapability,
    TargetKind,
)
from argus_core.runtime import RuntimeSpecError, recipe_digest, resolve

from argus_api.fleet import runtimes
from argus_api.fleet.runtimes import session_quota_env
from argus_api.fleet.targets import Target, TargetError, TargetInfo, TargetRegistry
from argus_api.runtime.gate import spec_of
from argus_api.settings import service as settings_service
from argus_api.tasks.delivery import project_repository_url

PROBE_TIMEOUT_S = 5.0
# Where Claude Code reads managed settings on Linux; the tools layer installs them here.
MANAGED_SETTINGS_PATH = "/etc/claude-code/managed-settings.json"
REPOSITORY_WRITE = "repository:write"

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


# -- the environment tasks will run in (worker launch 3) ------------------------------


@dataclass(frozen=True)
class RuntimeBuild:
    base_ref: str
    recipe_digest: str
    state: str
    reason: str | None


@dataclass(frozen=True)
class ProjectRuntime:
    digest: str | None  # None: the declaration no longer resolves
    problems: list[str]
    builds: list[RuntimeBuild]  # newest first


@dataclass(frozen=True)
class Environment:
    """What the database says about the environment a new worker would claim into,
    read before any target call. `ready` maps each project with `ready` tasks to
    their required capability sets; `repositories` and `runtimes` cover only those
    projects."""

    guard_enabled: bool = False
    ready: dict[str, list[frozenset[str]]] = field(default_factory=dict)
    repositories: dict[str, str | None] = field(default_factory=dict)
    runtimes: dict[str, ProjectRuntime] = field(default_factory=dict)


async def load_environment(
    conn: aiosqlite.Connection, *, project_runtime: bool
) -> Environment:
    guard = bool(
        await settings_service.global_value(conn, settings_service.COMMAND_GUARD_KEY)
    )
    if not guard:  # a project may turn it back on for its own claims
        async with conn.execute(
            "SELECT 1 FROM settings WHERE key = ? AND scope != ? AND value = 'true'",
            (settings_service.COMMAND_GUARD_KEY, settings_service.GLOBAL),
        ) as cur:
            guard = await cur.fetchone() is not None
    ready: dict[str, list[frozenset[str]]] = {}
    async with conn.execute(
        "SELECT project, required_capabilities FROM tasks"
        " WHERE state = 'ready' AND project != ?",
        (settings_service.GLOBAL,),
    ) as cur:
        for row in await cur.fetchall():
            needs = frozenset(json.loads(row["required_capabilities"]))
            ready.setdefault(row["project"], []).append(needs)
    repositories = {
        p: await project_repository_url(conn, p)
        for p, needs in ready.items()
        if any(REPOSITORY_WRITE in n for n in needs)
    }
    project_runtimes: dict[str, ProjectRuntime] = {}
    for project in ready if project_runtime else ():
        spec, declared = await spec_of(conn, project)
        if not declared:
            continue
        try:
            digest, problems = recipe_digest(resolve(project, spec)), []
        except RuntimeSpecError as e:
            digest, problems = None, e.problems
        async with conn.execute(
            "SELECT base_ref, recipe_digest, state, reason FROM runtime_builds"
            " WHERE project = ? ORDER BY updated_at DESC",
            (project,),
        ) as cur:
            builds = [RuntimeBuild(*r) for r in await cur.fetchall()]
        project_runtimes[project] = ProjectRuntime(digest, problems, builds)
    return Environment(guard, ready, repositories, project_runtimes)


def registers_command_guard(settings: Any) -> bool:
    """True when managed settings register `command_guard.py` as a PreToolUse Bash
    hook (`registersCommandGuard` in integrations/nanoclaw/argus-guard.ts)."""
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    pre = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
    if not isinstance(pre, list):
        return False
    for entry in pre:
        if not isinstance(entry, dict):
            continue
        matcher, inner = entry.get("matcher"), entry.get("hooks")
        if not isinstance(matcher, str) or "Bash" not in matcher.split("|"):
            continue
        if isinstance(inner, list) and any(
            "command_guard.py" in str(h.get("command") if isinstance(h, dict) else None)
            for h in inner
        ):
            return True
    return False


def _guard_off(pool: Pool) -> bool:
    return pool.env.get("ARGUS_COMMAND_GUARD", "").strip().lower() in ("0", "off")


async def command_guard_check(
    pool: Pool, env: Environment, target: Target
) -> Check | None:
    """Whether the agent image registers the command guard (command guard 3, Missing
    guard), read as the claim does. Omitted when the guard is off."""
    if not env.guard_enabled or _guard_off(pool) or pool.nanoclaw is None:
        return None
    image = pool.nanoclaw.agent_image
    where = f"{MANAGED_SETTINGS_PATH} in image {image}"
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_S):
            text = await target.read_image_file(image, MANAGED_SETTINGS_PATH)
    except (TargetError, TimeoutError, OSError) as exc:
        reason = str(exc) or "timed out"
        return Check(
            name="command_guard", ok=None, detail=f"{where}: could not check: {reason}"
        )
    if text is None:
        return Check(
            name="command_guard", ok=None, detail=f"{where}: not verifiable here"
        )
    try:
        registered = registers_command_guard(json.loads(text))
    except ValueError:  # missing or malformed: the hook is not in effect
        registered = False
    if registered:
        return Check(
            name="command_guard",
            ok=True,
            detail=f"{where} registers command_guard.py as a PreToolUse Bash hook",
        )
    return Check(
        name="command_guard",
        ok=False,
        detail=f"env_guard_missing: {where} does not register command_guard.py"
        " as a PreToolUse Bash hook",
    )


def runtime_check(project: str, rt: ProjectRuntime, base: str) -> Check:
    """The project's runtime image for this pool's agent image, from `runtime_builds`
    (project runtime 3): the claim gate's view, before any claim."""
    name = f"project_runtime:{project}"
    if rt.digest is None:
        return Check(
            name=name,
            ok=False,
            detail=f"runtime_failed: {project}: the runtime declaration does not"
            f" resolve: {'; '.join(rt.problems)}",
        )
    builds = [b for b in rt.builds if b.base_ref == base]
    current = [b for b in builds if b.recipe_digest == rt.digest]
    if not current:
        why = (
            "is not current (the recipe changed since the last build)"
            if builds
            else "is not built yet"
        )
        return Check(
            name=name,
            ok=None,
            detail=f"{project}: runtime image on {base} {why}; the first claim prepares it",
        )
    build = current[0]
    if build.state == "ready":
        return Check(
            name=name, ok=True, detail=f"{project}: runtime image ready on {base}"
        )
    if build.state == "failed":
        return Check(
            name=name,
            ok=False,
            detail=f"runtime_failed: {project}: "
            + (build.reason or "project runtime preparation failed"),
        )
    return Check(
        name=name,
        ok=False,
        detail=f"runtime_preparing: {project}: runtime image is {build.state}",
    )


def repository_check(project: str, pool: Pool, env: Environment) -> Check:
    """Whether a claim of the project's write tasks would name a repository: its
    record, the pools' agreed allowlist, else this pool's own map."""
    allow = pool.nanoclaw.repositories if pool.nanoclaw else {}
    url = env.repositories.get(project) or allow.get(project)
    name = f"project_repository:{project}"
    if url:
        return Check(name=name, ok=True, detail=f"{project}: repository {url}")
    return Check(
        name=name,
        ok=False,
        detail=f"env_repository_unconfigured: no repository is configured for project"
        f" {project} (set the project's repository or the pool's repositories)",
    )


async def environment_checks(
    pool: Pool, env: Environment, target: Target
) -> tuple[list[Check], list[Check]]:
    """(checks, those that block a start) for the environment a worker of `pool`
    would claim into. Only NanoClaw worker pools are judged. The command guard is
    pool-wide, so its false blocks. A project check blocks only when every project
    with work this pool could claim is blocked: one broken project never stops a
    pool that can still serve the others."""
    if pool.runtime != RuntimeKind.NANOCLAW or pool.nanoclaw is None or pool.system:
        return [], []
    checks: list[Check] = []
    if (guard := await command_guard_check(pool, env, target)) is not None:
        checks.append(guard)
    offered = set(pool.capabilities)
    broken: dict[str, list[Check]] = {}
    for project, needs in sorted(env.ready.items()):
        claimable = [n for n in needs if n <= offered]
        if not claimable:
            continue
        own: list[Check] = []
        if (rt := env.runtimes.get(project)) is not None:
            own.append(runtime_check(project, rt, pool.nanoclaw.agent_image))
        if any(REPOSITORY_WRITE in n for n in claimable):
            own.append(repository_check(project, pool, env))
        checks += own
        broken[project] = [c for c in own if c.ok is False]
    blocking = [c for c in checks if c.ok is False and ":" not in c.name]
    if broken and all(broken.values()):
        blocking += [c for cs in broken.values() for c in cs]
    return checks, blocking


async def judge_pool(
    pool: Pool,
    targets: TargetRegistry,
    public_url_configured: bool,
    probed: tuple[TargetCapability, TargetInfo | None],
    env: Environment | None = None,
) -> PoolCapability:
    cap, info = probed
    loc = _location(pool)
    blocking: list[Check] = []
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
        if env is not None:
            found, blocking = await environment_checks(pool, env, targets.get(*loc))
            checks += found
    env_names = {c.name for c in checks if ":" in c.name}
    blockers = [
        c.detail
        for c in checks
        if c.ok is False and (c.name not in env_names or c in blocking)
    ]
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
    pools: list[Pool],
    targets: TargetRegistry,
    public_url: str | None,
    *,
    launcher: bool = False,
    env: Environment | None = None,
) -> FleetCapabilities:
    locs = list(dict.fromkeys([("docker", None, None), *map(_location, pools)]))
    probed = dict(
        zip(
            locs, await asyncio.gather(*(_probe(targets, k) for k in locs)), strict=True
        )
    )
    judged = await asyncio.gather(
        *(
            judge_pool(p, targets, public_url is not None, probed[_location(p)], env)
            for p in pools
        )
    )
    return FleetCapabilities(
        public_url_configured=public_url is not None,
        launcher=launcher,
        targets=[cap for cap, _ in probed.values()],
        pools=list(judged),
    )


async def discover_pool(
    pool: Pool,
    targets: TargetRegistry,
    public_url: str | None,
    env: Environment | None = None,
) -> PoolCapability:
    probed = await _probe(targets, _location(pool))
    return await judge_pool(pool, targets, public_url is not None, probed, env)
