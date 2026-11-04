"""The loop's one client into the app: REST and MCP over an ASGI transport, with
the agent token (builtin-orchestrator spec 3.3). No network hop, and every call
passes the same authentication, scope and route checks as a remote agent's."""

import itertools
from dataclasses import dataclass
from typing import Any

import httpx2 as httpx

BASE_URL = "http://localhost"  # `localhost` is on the MCP host allowlist
REST_TIMEOUT_S = 60.0
CLAIM_TIMEOUT_S = 45.0  # the claim long-polls up to 30 s


@dataclass(frozen=True)
class Response:
    status: int
    body: Any

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def code(self) -> str | None:
        detail = self.body.get("detail") if isinstance(self.body, dict) else None
        return detail.get("code") if isinstance(detail, dict) else None


class McpError(Exception):
    pass


class ArgusClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http
        self.token: str | None = None
        self._ids = itertools.count(1)

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def rest(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        read_s: float = REST_TIMEOUT_S,
    ) -> Response:
        res = await self.http.request(
            method,
            "/api" + path,
            json=json,
            params=params,
            headers=self._auth(),
            timeout=read_s,
        )
        try:
            body = res.json() if res.content else None
        except ValueError:
            body = res.text
        return Response(res.status_code, body)

    async def mcp(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        res = await self.http.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": next(self._ids),
                "method": method,
                "params": params or {},
            },
            headers={
                "Accept": "application/json, text/event-stream",
                **self._auth(),
                **(headers or {}),
            },
            timeout=REST_TIMEOUT_S,
        )
        if res.status_code != 200:
            raise McpError(f"MCP {method} answered {res.status_code}")
        data = res.json()
        if "error" in data:
            raise McpError(f"MCP {method}: {data['error'].get('message')}")
        return data["result"]
