"""A fake model client: replays scripted turns, records what it was sent."""

from evals.model import Event, ToolCall, ToolSpec, Turn


class ScriptedClient:
    def __init__(self, turns: list[Turn]) -> None:
        self._turns = iter(turns)
        self.systems: list[str] = []

    def complete(
        self, *, system: str, tools: list[ToolSpec], transcript: list[Event]
    ) -> Turn:
        self.systems.append(system)
        return next(self._turns)


def call(name: str, **args: object) -> ToolCall:
    return ToolCall(id=f"c-{name}", name=name, args=dict(args))
