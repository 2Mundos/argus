"""Runtimes: *what* runs in the container and how it is wired to Argus (3.5)."""

from argus_core.fleet import Pool

from argus_api.fleet.targets import (
    INSTANCE_LABEL,
    MANAGED_LABEL,
    POOL_LABEL,
    ContainerSpec,
    KubernetesPlacement,
    Mount,
    TargetError,
)


def descendant_label(pool: Pool) -> str | None:
    """The label key under which this runtime's own containers (children of the
    instance's container, which Argus did not start) can be recognised."""
    return "nanoclaw-group-folder" if pool.runtime == "nanoclaw" else None


def agent_name(pool_name: str, instance_id: str) -> str:
    return f"{pool_name}-{instance_id[:8]}"


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
    pool: Pool, instance_id: str, bootstrap_tokens: list[str], argus_url: str
) -> ContainerSpec:
    """Build the container for one instance.

    `plain`: the image reads ARGUS_* and speaks the Argus API itself.
    `nanoclaw`: the image runs a NanoClaw host (with the Argus channel) from a
    prepared checkout on the Docker host, which spawns its own agent containers.
    """
    env = {
        **pool.env,
        "ARGUS_URL": argus_url,
        "ARGUS_BOOTSTRAP_TOKENS": ",".join(bootstrap_tokens),
        "ARGUS_INSTANCE_ID": instance_id,
        "ARGUS_POOL": pool.name,
        "ARGUS_CAPABILITIES": ",".join(pool.capabilities),
    }
    mounts: list[Mount] = []
    user: str | None = None
    group_add: list[str] = []
    if pool.runtime == "nanoclaw":
        nc = pool.nanoclaw
        if nc is None:  # guarded by PoolUpsert; kept for a clear error on bad rows
            raise TargetError(
                "pool uses the nanoclaw runtime but has no nanoclaw options"
            )
        env |= {
            "ARGUS_NANOCLAW_INSTALL_DIR": nc.install_dir,
            "ARGUS_NANOCLAW_INSTANCES_ROOT": nc.instances_root,
            "ARGUS_AGENT_NAME": agent_name(pool.name, instance_id),
            "CONTAINER_IMAGE": nc.agent_image,
            "NANOCLAW_GATEWAY_PROVIDER": "onecli",
            "ONECLI_URL": nc.onecli_url,
            "NO_PROXY": "host.docker.internal,127.0.0.1,localhost",
        }
        if nc.account_providers:
            env["ARGUS_ACCOUNT_PROVIDERS"] = ",".join(nc.account_providers)
        mounts = [
            # Identical host and container paths: NanoClaw resolves the paths it
            # hands to Docker on the daemon's filesystem, not its own.
            Mount(nc.install_dir, nc.install_dir, "ro"),
            Mount(nc.instances_root, nc.instances_root, "rw"),
            Mount(nc.docker_socket, "/var/run/docker.sock", "rw"),
        ]
        user, group_add = nc.user, list(nc.group_add)
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
        mounts=mounts,
        user=user,
        group_add=group_add,
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
