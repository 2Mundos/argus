"""OpenAI-compatible Chat Completions rendering and parsing: OpenAI, custom and
local accounts (builtin-orchestrator 3.4, 3.9)."""

import json
from typing import Any

from argus_api.orchestrator_builtin.models import (
    MAX_OUTPUT_TOKENS,
    Reply,
    ToolSpec,
    Usage,
    parse_arguments,
)
from argus_api.orchestrator_builtin.transcript import (
    Result,
    Step,
    ToolCall,
    Transcript,
    UserText,
)


class OpenAIAdapter:
    def __init__(
        self, *, token_field: str, provider_block: dict[str, Any] | None = None
    ) -> None:
        self.token_field = token_field
        self.provider_block = provider_block

    def body(
        self,
        model: str,
        system: str,
        history: list[tuple[str, str]],
        transcript: Transcript,
        tools: list[ToolSpec],
        *,
        use_tools: bool,
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        messages += [{"role": role, "content": text} for role, text in history]
        for item in transcript.items:
            if isinstance(item, UserText):
                if messages[-1]["role"] == "user":
                    messages[-1]["content"] += f"\n\n{item.text}"
                else:
                    messages.append({"role": "user", "content": item.text})
            elif isinstance(item, Step):
                msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": item.text or None,
                }
                if item.calls:
                    msg["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            **(
                                {"extra_content": call.extra_content}
                                if call.extra_content
                                else {}
                            ),
                            "function": {
                                "name": call.name,
                                "arguments": call.raw
                                or json.dumps(call.arguments or {}),
                            },
                        }
                        for call in item.calls
                    ]
                messages.append(msg)
            elif isinstance(item, Result):
                text = f"ERROR {item.text}" if item.is_error else item.text
                messages.append(
                    {"role": "tool", "tool_call_id": item.call_id, "content": text}
                )
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            self.token_field: MAX_OUTPUT_TOKENS,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]
            body["tool_choice"] = "auto" if use_tools else "none"
        if self.provider_block:
            body["provider"] = self.provider_block
        return body

    def parse(self, data: dict[str, Any]) -> Reply:
        if not isinstance(data, dict):
            raise ValueError("invalid OpenAI-compatible response")
        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError("invalid OpenAI-compatible choices")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ValueError("invalid OpenAI-compatible choice")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ValueError("invalid OpenAI-compatible message")
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None and finish_reason not in (
            "stop",
            "length",
            "tool_calls",
            "content_filter",
        ):
            raise ValueError("invalid OpenAI-compatible finish reason")
        if "tool_calls" in message and not isinstance(message["tool_calls"], list):
            raise ValueError("invalid OpenAI-compatible tool calls")
        raw_calls = message.get("tool_calls", [])
        if finish_reason == "tool_calls" and not raw_calls:
            raise ValueError("missing OpenAI-compatible tool call")
        if raw_calls and finish_reason != "tool_calls":
            raise ValueError("truncated OpenAI-compatible tool call")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError("invalid OpenAI-compatible message content")
        calls = []
        for raw in raw_calls:
            if not isinstance(raw, dict):
                raise ValueError("invalid OpenAI-compatible tool call")
            call_id = raw.get("id")
            if not isinstance(call_id, str) or not call_id.strip():
                raise ValueError("invalid OpenAI-compatible tool call id")
            if raw.get("type") != "function":
                raise ValueError("invalid OpenAI-compatible tool call type")
            fn = raw.get("function")
            if not isinstance(fn, dict):
                raise ValueError("invalid OpenAI-compatible function")
            name = fn.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("invalid OpenAI-compatible function name")
            raw_arguments = fn.get("arguments")
            if isinstance(raw_arguments, str):
                text = raw_arguments
                if not text.strip():
                    raise ValueError("invalid OpenAI-compatible function arguments")
            elif isinstance(raw_arguments, dict):
                try:
                    text = json.dumps(raw_arguments)
                except (TypeError, ValueError, RecursionError) as exc:
                    raise ValueError(
                        "invalid OpenAI-compatible function arguments"
                    ) from exc
            else:
                raise ValueError("invalid OpenAI-compatible function arguments")
            arguments = parse_arguments(text)
            if arguments is None:
                raise ValueError("invalid OpenAI-compatible function arguments")
            extra = raw.get("extra_content")
            calls.append(
                ToolCall(
                    id=call_id,
                    name=name,
                    arguments=arguments,
                    raw=text,
                    extra_content=extra if isinstance(extra, dict) else None,
                )
            )
        return Reply(
            content if isinstance(content, str) else "",
            calls,
            _usage(data.get("usage")),
        )


def _usage(raw: Any) -> Usage | None:
    """`prompt_tokens` counts cached input too: cached is taken out of it."""
    if not isinstance(raw, dict):
        return None
    prompt = raw.get("prompt_tokens")
    out = raw.get("completion_tokens")
    details = raw.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    cached = cached if isinstance(cached, int) else None
    if not isinstance(prompt, int):
        prompt = None
    uncached = None if prompt is None else prompt - (cached or 0)
    return Usage(uncached, out if isinstance(out, int) else None, cached)
