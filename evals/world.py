"""Scripted fake world: serves recorded tool responses, never a live Argus."""

import json
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from evals.model import ToolCall, ToolResult, ToolSpec


@dataclass
class Rule:
    """One recorded response. `when` limits it to calls whose args include it;
    `times` limits how often it is served (None: always)."""

    tool: str
    result: Any = None
    error: str | None = None
    when: dict[str, Any] = field(default_factory=dict)
    times: int | None = None


def args_match(want: dict[str, Any], got: dict[str, Any]) -> bool:
    """`want` is a subset of `got`; the value "*" matches any present value."""
    return all(k in got and (v == "*" or got[k] == v) for k, v in want.items())


class World:
    def __init__(self, tools: list[ToolSpec], rules: list[Rule]) -> None:
        self._schemas = {t.name: t.input_schema for t in tools}
        self._rules = rules
        self._left: list[int | None] = [r.times for r in rules]

    @classmethod
    def from_data(cls, tools: list[ToolSpec], data: list[dict[str, Any]]) -> "World":
        return cls(tools, [Rule(**row) for row in data])

    def call(self, call: ToolCall) -> ToolResult:
        schema = self._schemas.get(call.name)
        if schema is None:
            return ToolResult(call.id, f"unknown tool: {call.name}", True)
        try:
            jsonschema.validate(call.args, schema)
        except jsonschema.ValidationError as exc:
            path = ".".join(map(str, exc.absolute_path))
            return ToolResult(
                call.id, f"422 invalid arguments: {path}: {exc.message}", True
            )
        for i, rule in enumerate(self._rules):
            if rule.tool != call.name or not args_match(rule.when, call.args):
                continue
            left = self._left[i]
            if left == 0:
                continue
            if left is not None:
                self._left[i] = left - 1
            if rule.error is not None:
                return ToolResult(call.id, rule.error, True)
            return ToolResult(call.id, json.dumps(rule.result))
        return ToolResult(
            call.id, f"501 not recorded: no response for {call.name}", True
        )
