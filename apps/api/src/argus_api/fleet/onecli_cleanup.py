"""Delete the OneCLI agent and secrets of terminated NanoClaw instances
(onecli-cleanup.spec.md). Opt-in, dry-run first; only exact name matches."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx2 as httpx

from argus_api.db import Database
from argus_api.events import record
from argus_api.fleet.runtimes import agent_name

log = logging.getLogger(__name__)

EVENT_NAMES_CAP = 50


@dataclass
class StaleAgent:
    id: str
    name: str
    secret_ids: list[str] = field(default_factory=list)


def plan(
    instances: list[tuple[str, str, str]],
    agents: list[dict[str, Any]],
    secrets: list[dict[str, Any]],
) -> list[StaleAgent]:
    """The OneCLI agents to delete, with their secrets. `instances` is
    `(id, pool, state)`. An agent is stale when its name is that of a terminated
    instance and of no instance in another state (3.2, 3.3)."""
    terminated: set[str] = set()
    other: set[str] = set()
    for instance_id, pool, state in instances:
        name = agent_name(pool, instance_id)
        (terminated if state == "terminated" else other).add(name)
    stale_names = terminated - other
    out: list[StaleAgent] = []
    for agent in agents:
        name, agent_id = agent.get("name"), agent.get("id")
        identifier = agent.get("identifier")
        if name not in stale_names or not isinstance(agent_id, str):
            continue
        stale = StaleAgent(agent_id, name)
        if isinstance(identifier, str) and identifier:
            base = f"argus-{identifier}"
            stale.secret_ids = [
                s["id"]
                for s in secrets
                if isinstance(s.get("id"), str)
                and isinstance(s.get("name"), str)
                and (s["name"] == base or s["name"].startswith(base + "-"))
            ]
        out.append(stale)
    return out


class OneCliCleaner:
    def __init__(
        self,
        db: Database,
        url: str,
        api_key: str | None = None,
        *,
        dry_run: bool = True,
        interval_s: float = 3600.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.db = db
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.dry_run = dry_run
        self.interval_s = interval_s
        self.transport = transport

    async def run_once(self) -> dict[str, Any]:
        """One run: list, plan, delete (unless dry run), record the event."""
        async with (
            self.db.read() as conn,
            conn.execute("SELECT id, pool, state FROM instances") as cur,
        ):
            instances = [(r[0], r[1], r[2]) for r in await cur.fetchall()]
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        async with httpx.AsyncClient(
            base_url=self.url, headers=headers, timeout=30, transport=self.transport
        ) as client:
            agents = await _list(client, "/api/agents")
            secrets = await _list(client, "/api/secrets")
            stale = plan(instances, agents, secrets)
            deleted_agents = deleted_secrets = failed = 0
            if not self.dry_run:
                for agent in stale:
                    ok = True
                    for secret_id in agent.secret_ids:
                        if await _delete(client, f"/api/secrets/{secret_id}"):
                            deleted_secrets += 1
                        else:
                            ok = False
                            failed += 1
                    # Secrets first: the agent goes only once nothing of it is left.
                    if ok and await _delete(client, f"/api/agents/{agent.id}"):
                        deleted_agents += 1
                    elif ok:
                        failed += 1
        data = {
            "dry_run": self.dry_run,
            "agents": len(stale) if self.dry_run else deleted_agents,
            "secrets": (
                sum(len(a.secret_ids) for a in stale)
                if self.dry_run
                else deleted_secrets
            ),
            "failed": failed,
            "names": [a.name for a in stale[:EVENT_NAMES_CAP]],
        }
        async with self.db.write() as tx:
            await record(tx, "system", "system", "onecli", "onecli.cleanup", data)
        # Fixed text only: the counts and names are in the event, not the log.
        log.info("onecli cleanup run recorded (onecli.cleanup event)")
        return data

    async def loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a listing error ends the run, never the loop
                log.warning("onecli cleanup run failed: %s", type(exc).__name__)
            await asyncio.sleep(self.interval_s)


async def _list(client: httpx.AsyncClient, path: str) -> list[dict[str, Any]]:
    res = await client.get(path)
    res.raise_for_status()
    body = res.json()
    if not isinstance(body, list):
        raise ValueError(f"{path} did not return a list")
    return [item for item in body if isinstance(item, dict)]


async def _delete(client: httpx.AsyncClient, path: str) -> bool:
    try:
        res = await client.delete(path)
    except httpx.HTTPError:
        return False
    return res.status_code < 300 or res.status_code == 404
