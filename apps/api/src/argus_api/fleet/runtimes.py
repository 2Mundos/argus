"""Runtimes: *what* runs in the container and how it is wired to Argus (3.5)."""

from argus_core.fleet import Pool

from argus_api.fleet.targets import (
    INSTANCE_LABEL,
    MANAGED_LABEL,
    POOL_LABEL,
    ContainerSpec,
    TargetError,
)


def container_spec(
    pool: Pool, instance_id: str, bootstrap_tokens: list[str], argus_url: str
) -> ContainerSpec:
    if pool.runtime != "plain":
        raise TargetError(f"runtime {pool.runtime!r} is not supported yet")
    # The plain runtime contract: the image reads these and speaks the Argus API.
    env = {
        **pool.env,
        "ARGUS_URL": argus_url,
        "ARGUS_BOOTSTRAP_TOKENS": ",".join(bootstrap_tokens),
        "ARGUS_INSTANCE_ID": instance_id,
        "ARGUS_POOL": pool.name,
        "ARGUS_CAPABILITIES": ",".join(pool.capabilities),
    }
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
    )
