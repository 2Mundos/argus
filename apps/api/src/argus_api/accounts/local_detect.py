"""Find a model server already running on this node (builtin-orchestrator 3.11).

Probes `GET /v1/models` on loopback and the Docker host name at the usual ports,
with no credential and no redirects. Stores nothing; runs outside any write.
"""

import asyncio
from pathlib import Path
from typing import Any, Literal

import httpx2 as httpx
from argus_core.accounts import LocalCandidate, LocalDetection, is_model_name

PROBE_TIMEOUT_S = 2.0
HOSTS = ("127.0.0.1", "host.docker.internal")
PORTS: dict[int, Literal["ollama", "lmstudio", "llamacpp"]] = {
    11434: "ollama",
    1234: "lmstudio",
    8080: "llamacpp",  # llama.cpp and Strata
}
LOOPBACK_NOTE = "workers cannot reach this address; only the built-in orchestrator can"
CONTAINER_HINT = (
    "Argus runs in a container. The model server must listen on the Docker bridge"
    " address (for Ollama, `OLLAMA_HOST=<bridge address>:11434`), and the API needs"
    " `host.docker.internal` (`deploy/compose.docker.yaml`)."
)
DOCKERENV = Path("/.dockerenv")


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
    candidates = [
        LocalCandidate(
            base_url=f"http://{host}:{port}",
            server_hint=PORTS[port],
            models=models,
            preferred=host == "host.docker.internal",
            note=LOOPBACK_NOTE if host == "127.0.0.1" else None,
        )
        for (host, port), models in zip(probes, answers, strict=True)
        if models is not None
    ]
    if in_container is None:
        in_container = await asyncio.to_thread(DOCKERENV.exists)
    hint = CONTAINER_HINT if not candidates and in_container else None
    return LocalDetection(candidates=candidates, hint=hint)
