"""Tool discovery, the offered set, calls and result caps
(builtin-orchestrator spec 3.6).

Hiding a tool is not access control: the REST route behind every tool still
decides, and a refusal comes back as a tool error.
"""

import json
from typing import Any

from argus_core.conversations import CHAT_TURN_HEADER

from argus_api.orchestrator_builtin.client import ArgusClient
from argus_api.orchestrator_builtin.models import ToolSpec
from argus_api.orchestrator_builtin.transcript import ToolCall

# Tools that act on the caller's own claim or identity, and tools the internal
# orchestrator is refused. A test fails when a tool listed to the orchestrator
# scope is in neither this set nor offered, so every new tool gets classified.
HIDDEN = frozenset(
    {
        "claim_task",
        "heartbeat",
        "leave",
        "lease_account",
        "report_account_exhausted",
        "complete_task",
        "fail_task",
        "report_progress",
        "append_task_log",
        "report_task_models",
        "upload_text_artifact",
        "report_observations",
        "ask_question",
        "launch_worker",
        "scale_pool",
        "create_worker",
        "drain_agent",
    }
)
# Offered when the model's context window is known and small (3.6).
CORE = (
    "get_inbox",
    "list_decisions",
    "get_decision",
    "preview_decision",
    "answer_decision",
    "list_tasks",
    "get_tasks",
    "get_task",
    "get_task_log",
    "create_task",
    "edit_task",
    "search_knowledge",
    "get_knowledge_page",
    "get_settings",
    "list_engines",
    "get_fleet_capabilities",
)
CORE_PROFILE_BELOW = 65_536  # tokens of context window
RESULT_CAP = 16 * 1024  # bytes per tool result
SKILL_URI = "skill://argus-orchestrator/SKILL.md"
_ERROR_PREFIX = "Error executing tool "


async def discover(argus: ArgusClient) -> list[ToolSpec]:
    """`initialize` and `tools/list`: already filtered to the caller's scope."""
    await argus.mcp(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "argus-builtin-orchestrator", "version": "1"},
        },
    )
    listed = (await argus.mcp("tools/list"))["tools"]
    return [
        ToolSpec(
            t["name"],
            t.get("description") or "",
            t.get("inputSchema") or {"type": "object", "properties": {}},
        )
        for t in listed
        if t["name"] not in HIDDEN
    ]


async def read_skill(argus: ArgusClient) -> str:
    result = await argus.mcp("resources/read", {"uri": SKILL_URI})
    return "\n".join(c.get("text") or "" for c in result["contents"])


def offered(tools: list[ToolSpec], context_tokens: int | None) -> list[ToolSpec]:
    if context_tokens is not None and context_tokens < CORE_PROFILE_BELOW:
        return [t for t in tools if t.name in CORE]
    return tools


def cap(text: str) -> str:
    """At most `RESULT_CAP` bytes, cut at a code point boundary."""
    raw = text.encode()
    if len(raw) <= RESULT_CAP:
        return text
    shown = raw[:RESULT_CAP].decode(errors="ignore")
    return f"{shown}\n[cut: {len(shown.encode())} of {len(raw)} bytes]"


def _text(result: dict[str, Any], name: str) -> str:
    if not result.get("isError") and result.get("structuredContent") is not None:
        return json.dumps(
            result["structuredContent"], separators=(",", ":"), ensure_ascii=False
        )
    text = "\n".join(
        c.get("text") or ""
        for c in result.get("content") or []
        if c.get("type") == "text"
    )
    prefix = f"{_ERROR_PREFIX}{name}: "
    return text.removeprefix(prefix)


async def call(
    argus: ArgusClient, call: ToolCall, offered_names: set[str], turn_id: str
) -> tuple[str, bool]:
    """(result text, is_error) for one call, sent with the turn header."""
    if call.arguments is None:
        return "arguments are not valid JSON", True
    if call.name not in offered_names:
        return f"unknown tool: {call.name}", True
    try:
        result = await argus.mcp(
            "tools/call",
            {"name": call.name, "arguments": call.arguments},
            headers={CHAT_TURN_HEADER: turn_id},
        )
    except Exception as exc:  # the MCP transport itself failed
        return f"the tool call failed ({type(exc).__name__})", True
    return cap(_text(result, call.name)), bool(result.get("isError"))
