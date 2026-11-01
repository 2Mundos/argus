"""The model boundary. Evals depend on this interface, never on a vendor SDK."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    text: str
    is_error: bool = False


@dataclass(frozen=True)
class Turn:
    """One model reply: free text and the tool calls it asked for."""

    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    cost_usd: float = 0.0


@dataclass(frozen=True)
class UserMessage:
    text: str


Event = UserMessage | Turn | list[ToolResult]


class ModelClient(Protocol):
    """Send the transcript so far; get the model's next turn.

    The client owns pricing: it reports `cost_usd` for each call it makes.
    """

    def complete(
        self, *, system: str, tools: list[ToolSpec], transcript: list[Event]
    ) -> Turn: ...


ClientFactory = Callable[[str], ModelClient]
