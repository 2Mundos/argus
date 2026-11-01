"""`argus-replay --fixture <file> [--speed N]`: the dev-only replay worker.

It registers as an agent offering the `replay` capability (the replay pool), claims
the next task that requires it and plays the fixture back through the MCP tools.
Refuses to run unless `ARGUS_DEV_REPLAY=1`.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx2 as httpx

from argus_api.replay import ENV, POOL, enabled, fixture
from argus_api.replay.player import Call, CallError, claim, play


def make_call(client: httpx.AsyncClient, token: str) -> Call:
    """A `Call` that invokes MCP tools over JSON-RPC (`/mcp`, json_response mode)."""
    ids = iter(range(1, 1 << 31))

    async def call(tool: str, **arguments: Any) -> Any:
        res = await client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": next(ids),
                "method": "tools/call",
                "params": {
                    "name": tool,
                    "arguments": {k: v for k, v in arguments.items() if v is not None},
                },
            },
        )
        res.raise_for_status()
        result = res.json()["result"]
        text = result["content"][0]["text"] if result["content"] else ""
        if result["isError"]:
            raise CallError(text.split(": ", 1)[-1] if ": " in text else text)
        if "structuredContent" in result:
            out = result["structuredContent"]
            return out.get("result", out)
        return json.loads(text)

    return call


async def register(client: httpx.AsyncClient, admin_token: str) -> str:
    """An agent token for a new worker in the replay pool."""
    auth = {"Authorization": f"Bearer {admin_token}"}
    boot = await client.post("/api/agents/bootstrap-tokens", json={}, headers=auth)
    boot.raise_for_status()
    reg = await client.post(
        "/api/agents/register",
        json={
            "bootstrap_token": boot.json()["token"],
            "name": f"replay-{os.getpid()}",
            "capabilities": [POOL],
        },
    )
    reg.raise_for_status()
    return reg.json()["token"]


async def run(url: str, admin_token: str, path: Path, speed: float) -> int:
    recorded = fixture.load(path)
    async with httpx.AsyncClient(base_url=url, timeout=60) as client:
        call = make_call(client, await register(client, admin_token))
        print(f"registered in the {POOL} pool; waiting for a task", file=sys.stderr)
        while (task := await claim(call)) is None:
            pass
        print(f"claimed {task['id']} attempt {task['attempt']}", file=sys.stderr)
        result = await play(task, recorded, call, asyncio.sleep, speed=speed)
    print(f"{result.status}: {result.played} steps, {len(result.steers)} steers")
    return 0 if result.status != "cancelled" else 1


def main(argv: list[str] | None = None) -> int:
    if not enabled():
        print(f"argus-replay is a dev tool: set {ENV}=1 to enable it", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(prog="argus-replay", description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--speed", type=float, default=1.0, help="1 = recorded pace")
    parser.add_argument(
        "--url", default=os.environ.get("ARGUS_URL", "http://127.0.0.1:8000")
    )
    args = parser.parse_args(argv)
    admin_token = os.environ.get("ARGUS_ADMIN_TOKEN")
    if not admin_token:
        print("set ARGUS_ADMIN_TOKEN to register the worker", file=sys.stderr)
        return 2
    return asyncio.run(run(args.url, admin_token, args.fixture, args.speed))


if __name__ == "__main__":
    sys.exit(main())
