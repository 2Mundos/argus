"""Find a model server already running on this node (builtin-orchestrator 3.11).

Probes `GET /v1/models` on loopback and the Docker host name at the usual ports,
with no credential and no redirects. Stores nothing; runs outside any write. Each
candidate names the worker endpoint it would store and the endpoint the API
process would use for it (local harness spec, "One endpoint per runtime").
"""

import asyncio
from pathlib import Path
from typing import Any, Literal

import httpx2 as httpx
from argus_core.accounts import (
    DOCKER_HOST,
    LocalCandidate,
    LocalDetection,
    is_model_name,
    local_api_endpoint,
)

PROBE_TIMEOUT_S = 2.0
HOSTS = ("127.0.0.1", DOCKER_HOST)
PORTS: dict[int, Literal["ollama", "lmstudio", "llamacpp"]] = {
    11434: "ollama",
    1234: "lmstudio",
    8080: "llamacpp",  # llama.cpp and Strata
}
LOOPBACK_NOTE = "workers cannot reach this address; only the built-in orchestrator can"
FORWARDER_NOTE = (
    "workers reach it through the Docker bridge forwarder; the Argus API uses 127.0.0.1"
)
CONTAINER_HINT = (
    "Argus runs in a container. The model server must listen on the Docker bridge"
    " address (for Ollama, `OLLAMA_HOST=<bridge address>:11434`), and the API needs"
    " `host.docker.internal` (`deploy/compose.docker.yaml`)."
)
DOCKERENV = Path("/.dockerenv")


def _candidate(
    base_url: str,
    port: int,
    models: list[str],
    *,
    container: bool,
    note: str | None,
) -> LocalCandidate:
    api, _ = local_api_endpoint({"base_url": base_url}, in_container=container)
    return LocalCandidate(
        base_url=base_url,
        api_endpoint=api,
        server_hint=PORTS[port],
        models=models,
        preferred=base_url.startswith(f"http://{DOCKER_HOST}:"),
        note=note,
    )


async def _models(client: httpx.AsyncClient, base_url: str) -> list[str] | None:
    try:
        res = await client.get(f"{base_url}/v1/models")
    except (httpx.HTTPError, OSError):
        return None
    if res.status_code != 200:
        return None
    try:
        body: Any = res.json()
    except ValueError:
        return None
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return None
    return [
        item["id"]
        for item in data
        if isinstance(item, dict) and is_model_name(item.get("id"))
    ]


async def detect(
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    in_container: bool | None = None,
) -> LocalDetection:
    probes = [(host, port) for port in PORTS for host in HOSTS]
    async with httpx.AsyncClient(
        transport=transport, timeout=PROBE_TIMEOUT_S, follow_redirects=False
    ) as client:
        answers = await asyncio.gather(
            *(_models(client, f"http://{host}:{port}") for host, port in probes)
        )
    if in_container is None:
        in_container = await asyncio.to_thread(DOCKERENV.exists)
    found = {
        (host, port): models
        for (host, port), models in zip(probes, answers, strict=True)
        if models is not None
    }
    candidates: dict[str, LocalCandidate] = {}
    for (host, port), models in found.items():
        url = f"http://{host}:{port}"
        note = LOOPBACK_NOTE if host == "127.0.0.1" else None
        candidates[url] = _candidate(
            url, port, models, container=in_container, note=note
        )
    if not in_container:
        # A host API cannot resolve host.docker.internal; workers can, and the
        # bridge forwarder takes them to the same port on loopback.
        for (host, port), models in found.items():
            url = f"http://{DOCKER_HOST}:{port}"
            if host == "127.0.0.1" and url not in candidates:
                candidates[url] = _candidate(
                    url, port, models, container=False, note=FORWARDER_NOTE
                )
    ordered = sorted(candidates.values(), key=lambda c: (not c.preferred, c.base_url))
    hint = CONTAINER_HINT if not ordered and in_container else None
    return LocalDetection(candidates=ordered, hint=hint)
