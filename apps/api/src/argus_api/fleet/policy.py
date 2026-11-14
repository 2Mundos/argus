"""Process-level fleet policy that pool upserts cannot override."""

from argus_core.fleet import PoolUpsert, RuntimeKind

from argus_api.config import Settings
from argus_api.errors import ForbiddenError, ValidationFailedError

ROUTE_BIND_ENV = "ARGUS_INTEGRATION_ROUTE_BIND"
# The channel's route and MCP broker listeners (integrations 3.6): set by the
# operator, never by a create_worker request.
ROUTE_TOPOLOGY_ENV = (
    ROUTE_BIND_ENV,
    "ARGUS_INTEGRATION_ROUTE_PORT",
    "ARGUS_INTEGRATION_MCP_PORT",
)


def enforce_nanoclaw_image_roles(body: PoolUpsert, settings: Settings) -> None:
    """Refuse a NanoClaw pool whose host/agent images are swapped vs settings.

    `PoolUpsert` already rejects `image == nanoclaw.agent_image`. When the
    process has `ARGUS_NANOCLAW_HOST_IMAGE` / `ARGUS_NANOCLAW_AGENT_IMAGE`, also
    refuse putting the configured agent image in `image` or the configured host
    image in `nanoclaw.agent_image`. Settings only; no network call.
    """
    if body.runtime is not RuntimeKind.NANOCLAW or body.nanoclaw is None:
        return
    host, agent = body.image, body.nanoclaw.agent_image
    configured_host = settings.nanoclaw_host_image
    configured_agent = settings.nanoclaw_agent_image
    if configured_agent and host == configured_agent:
        raise ValidationFailedError(
            f"image ({host!r}) is the configured NanoClaw agent image "
            f"(ARGUS_NANOCLAW_AGENT_IMAGE); set nanoclaw.agent_image for the "
            f"agent image and image for the host",
            code="invalid_pool",
            details=[
                "image: this is the NanoClaw agent image "
                "(ARGUS_NANOCLAW_AGENT_IMAGE), not the host image",
                "nanoclaw.agent_image: put the agent image in this field",
            ],
        )
    if configured_host and agent == configured_host:
        raise ValidationFailedError(
            f"nanoclaw.agent_image ({agent!r}) is the configured NanoClaw host "
            f"image (ARGUS_NANOCLAW_HOST_IMAGE); set image for the host image "
            f"and nanoclaw.agent_image for the agent",
            code="invalid_pool",
            details=[
                "nanoclaw.agent_image: this is the NanoClaw host image "
                "(ARGUS_NANOCLAW_HOST_IMAGE), not the agent image",
                "image: put the host image in this field",
            ],
        )


def enforce_nanoclaw_socket_policy(body: PoolUpsert, settings: Settings) -> None:
    """Refuse a socket-mounted NanoClaw pool unless the operator opted in.

    Templates already gate readiness on `ARGUS_NANOCLAW_ALLOW_DOCKER_SOCKET`;
    this enforces the same process-level policy on every raw
    `PUT`/`PATCH /api/pools/{name}` path (and any other caller that passes
    settings), so an admin cannot bypass the opt-in by editing the pool directly.
    Sandboxed pools (`docker.runtime` set) never mount the socket and are
    allowed without the opt-in; `PoolUpsert` rejects configuring both.
    """
    if body.runtime is not RuntimeKind.NANOCLAW or body.nanoclaw is None:
        return
    if body.docker.runtime is not None:
        return
    if settings.nanoclaw_allow_docker_socket:
        return
    raise ForbiddenError(
        "mounting the Docker socket needs ARGUS_NANOCLAW_ALLOW_DOCKER_SOCKET=true"
        " on the Argus process (root-equivalent on that Docker host);"
        " use docker.runtime for the sandboxed NanoClaw host instead",
        code="docker_socket_not_allowed",
    )


def enforce_route_bind(
    body: PoolUpsert, settings: Settings, previous: str | None = None
) -> None:
    """Refuse `ARGUS_INTEGRATION_ROUTE_BIND` in pool env unless it is the
    operator's `ARGUS_NANOCLAW_INTEGRATION_ROUTE_BIND` (or, on a PATCH, the
    pool's unchanged `previous` value) on a socket-mode NanoClaw pool, where the
    guided factory puts it. Every pool write calls this, so an edit cannot point
    a host's Kubernetes route at another address."""
    value = body.env.get(ROUTE_BIND_ENV)
    if value is None:
        return
    if (
        body.runtime is RuntimeKind.NANOCLAW
        and body.docker.runtime is None
        and value in (settings.nanoclaw_integration_route_bind, previous)
    ):
        return
    raise ValidationFailedError(
        f"{ROUTE_BIND_ENV} in pool env must equal ARGUS_NANOCLAW_INTEGRATION_ROUTE_BIND"
        " on a socket-mode NanoClaw pool; remove it from this pool",
        code="invalid_pool",
    )
