"""The text under test, imported from the modules that serve it."""

import asyncio

from argus_api import mcp_server
from argus_core.auth import Scope

from evals.model import ToolSpec


def tools_for(scope: Scope) -> list[ToolSpec]:
    """The tools the MCP server lists to a caller with this scope, as served."""
    listed = asyncio.run(mcp_server.server.list_tools())
    return [
        ToolSpec(t.name, t.description or "", t.input_schema)
        for t in listed
        if scope in mcp_server.audiences[t.name]
    ]


def worker_prompt() -> str:
    return mcp_server.INSTRUCTIONS


def orchestrator_prompt() -> str:
    return f"{mcp_server.INSTRUCTIONS}\n\n{mcp_server.ORCHESTRATOR_SKILL}"
