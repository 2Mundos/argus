"""Process-level fleet policy that pool upserts cannot override."""

from argus_core.fleet import PoolUpsert, RuntimeKind

from argus_api.config import Settings
from argus_api.errors import ForbiddenError


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
