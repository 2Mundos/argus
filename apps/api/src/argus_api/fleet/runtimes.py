"""Runtimes: *what* runs in the container and how it is wired to Argus (3.5)."""

import json

from argus_core.fleet import Pool

from argus_api.fleet.targets import (
    HOST_GATEWAY,
    INSTANCE_LABEL,
    MANAGED_LABEL,
    POOL_LABEL,
    RUNTIME_TMPFS,
    SANDBOX_TMPFS,
    ContainerSpec,
    KubernetesPlacement,
    Mount,
    TargetError,
    container_name,
)

DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"
# NanoClaw host may run an inner dockerd (sandbox) or spawn sibling agents; keep
# headroom above the agent-container default of 2048.
NANOCLAW_HOST_PIDS_LIMIT = 4096
# Port the custom-harness bridge listens on inside a NanoClaw host container. The
# Docker target publishes it on the default bridge gateway at a free host port, so
# agent containers (another network) reach it as host.docker.internal:<host port>
# (custom harness spec, "Bridge reachable from agent containers").
CUSTOM_BRIDGE_PORT = 3141


def descendant_label(pool: Pool) -> str | None:
    """The label key under which this runtime's own containers (children of the
    instance's container, which Argus did not start) can be recognised."""
    return "nanoclaw-group-folder" if pool.runtime == "nanoclaw" else None


def agent_name(pool_name: str, instance_id: str, assigned: str | None = None) -> str:
    """Display name for the NanoClaw host / OneCLI agent of this instance.

    New instances get a mythology word in `instances.agent_name` at reserve.
    Rows that predate that column (or plain tests that omit it) keep the legacy
    `<pool>-<id[:8]>` form so OneCLI cleanup still matches them.
    """
    return assigned if assigned else f"{pool_name}-{instance_id[:8]}"


def session_quota_env(pool: Pool) -> dict[str, str]:
    """NanoClaw host env that makes each agent session inherit the pool quota.

    NanoClaw's `composeSessionSpec` (pinned install) maps `CONTAINER_CPU_LIMIT`
    / `CONTAINER_MEMORY_LIMIT` onto `SessionSpec.resources`; the Docker driver
    then passes `--cpus` / `--memory`. Without these, only the host container
    is capped and sibling session containers on the daemon stay unbounded
    (agent-resource-hygiene research). Empty when the pool requests no quota.
    """
    env: dict[str, str] = {}
    if pool.cpus is not None:
        cpus = pool.cpus
        env["CONTAINER_CPU_LIMIT"] = (
            str(int(cpus)) if float(cpus).is_integer() else str(cpus)
        )
    if pool.memory_mb is not None:
        env["CONTAINER_MEMORY_LIMIT"] = f"{pool.memory_mb}m"
    return env


def descendant_owner(pool: Pool, label_value: str) -> str | None:
    """Map a descendant's label value to the owning instance's id prefix (8 chars).

    NanoClaw names its agent group folder `argus-<agent name>` (the init script
    prefixes the agent name), and Argus names the agent `<pool>-<instance8>`.
    """
    prefix = f"argus-{pool.name}-"
    if pool.runtime == "nanoclaw" and label_value.startswith(prefix):
        owner = label_value.removeprefix(prefix)
        return owner if len(owner) == 8 else None
    return None


def container_spec(
    pool: Pool,
    instance_id: str,
    bootstrap_tokens: list[str],
    argus_url: str,
    model_policy: str | None = None,
    *,
    worker_name: str | None = None,
) -> ContainerSpec:
    """Build the container for one instance.

    `plain`: the image reads ARGUS_* and speaks the Argus API itself.
    `nanoclaw`: the image runs a NanoClaw host (with the Argus channel) from a
    prepared checkout on the Docker host, which spawns its own agent containers.
    `model_policy` (canonical JSON) becomes its ARGUS_MODEL_POLICY, fixed for the
    life of the container.
    """
    env = {
        **pool.env,
        "ARGUS_URL": argus_url,
        "ARGUS_BOOTSTRAP_TOKENS": ",".join(bootstrap_tokens),
        "ARGUS_INSTANCE_ID": instance_id,
        "ARGUS_POOL": pool.name,
        # The host's persona and MCP wiring depend on it (internal orchestrator 3.1).
        **(
            {"ARGUS_AGENT_ROLE": "orchestrator"}
            if pool.system == "orchestrator"
            else {}
        ),
        "ARGUS_CAPABILITIES": ",".join(pool.capabilities),
    }
    mounts: list[Mount] = []
    user: str | None = None
    group_add: list[str] = []
    tmpfs = dict(RUNTIME_TMPFS)
    pids_limit = 2048
    ports: dict[str, tuple[str, int | None]] = {}
    if pool.runtime == "nanoclaw":
        nc = pool.nanoclaw
        if nc is None:  # guarded by PoolUpsert; kept for a clear error on bad rows
            raise TargetError(
                "pool uses the nanoclaw runtime but has no nanoclaw options"
            )
        env |= {
            "ARGUS_NANOCLAW_INSTALL_DIR": nc.install_dir,
            "ARGUS_NANOCLAW_INSTANCES_ROOT": nc.instances_root,
            "ARGUS_AGENT_NAME": agent_name(pool.name, instance_id, worker_name),
            "CONTAINER_IMAGE": nc.agent_image,
            "NANOCLAW_GATEWAY_PROVIDER": "onecli",
            "ONECLI_URL": nc.onecli_url,
            "ARGUS_REPOSITORIES_JSON": json.dumps(nc.repositories, sort_keys=True),
            "NO_PROXY": "host.docker.internal,127.0.0.1,localhost",
            **session_quota_env(pool),
        }
        if pool.docker.network:
            env["ARGUS_NANOCLAW_NETWORK"] = pool.docker.network
        if model_policy is not None:
            env["ARGUS_MODEL_POLICY"] = model_policy
        workdir = f"{nc.instances_root.rstrip('/')}/{instance_id}"
        sandboxed = pool.docker.runtime is not None
        mounts = [
            # Identical host and container paths: NanoClaw resolves the paths it
            # hands to Docker on the daemon's filesystem, not its own.
            Mount(nc.install_dir, nc.install_dir, "ro"),
            # Only this instance's directory: hosts cannot see each other's state.
            Mount(workdir, workdir, "rw", create_owner=nc.user),
        ]
        pids_limit = NANOCLAW_HOST_PIDS_LIMIT
        if sandboxed:
            # No host socket: the instance runs its own daemon inside the sandbox
            # runtime, so a compromised host cannot reach the real Docker host.
            env["ARGUS_NANOCLAW_INNER_DOCKER"] = "1"
            tmpfs = dict(SANDBOX_TMPFS)
            if nc.agent_image_archive:
                env["ARGUS_NANOCLAW_AGENT_IMAGE_ARCHIVE"] = nc.agent_image_archive
                mounts.append(
                    Mount(nc.agent_image_archive, nc.agent_image_archive, "ro")
                )
        else:
            socket = nc.docker_socket or DEFAULT_DOCKER_SOCKET
            mounts.append(Mount(socket, "/var/run/docker.sock", "rw"))
            user, group_add = nc.user, list(nc.group_add)
            if pool.target == "docker":
                # Sibling agent containers sit on the default bridge, not on this
                # container's network: only a published port reaches the bridge.
                env["ARGUS_CUSTOM_BRIDGE_PORT"] = str(CUSTOM_BRIDGE_PORT)
                env["ARGUS_CUSTOM_BRIDGE_CONTAINER"] = container_name(instance_id)
                ports[f"{CUSTOM_BRIDGE_PORT}/tcp"] = (HOST_GATEWAY, None)
    elif pool.runtime != "plain":
        raise TargetError(f"runtime {pool.runtime!r} is not supported")
    return ContainerSpec(
        instance_id=instance_id,
        image=pool.image,
        env=env,
        labels={
            MANAGED_LABEL: "true",
            POOL_LABEL: pool.name,
            INSTANCE_LABEL: instance_id,
        },
        cpus=pool.cpus,
        memory_mb=pool.memory_mb,
        network=pool.docker.network,
        extra_hosts=pool.docker.extra_hosts,
        ports=ports,
        mounts=mounts,
        user=user,
        group_add=group_add,
        runtime=pool.docker.runtime,
        tmpfs=tmpfs,
        pids_limit=pids_limit,
        allow_mutable_images=pool.allow_mutable_images,
        kubernetes=(
            KubernetesPlacement(
                namespace=pool.kubernetes.namespace,
                service_account=pool.kubernetes.service_account,
                image_pull_secrets=list(pool.kubernetes.image_pull_secrets),
                node_selector=dict(pool.kubernetes.node_selector),
            )
            if pool.target == "kubernetes" and pool.kubernetes
            else None
        ),
    )


def target_location(pool: Pool) -> tuple[str | None, str | None]:
    """(host/context, namespace) that select this pool's target client."""
    if pool.target == "kubernetes" and pool.kubernetes:
        return pool.kubernetes.context, pool.kubernetes.namespace
    return pool.docker.host, None
