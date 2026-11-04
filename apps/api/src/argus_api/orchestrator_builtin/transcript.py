"""The provider-neutral transcript of one turn (builtin-orchestrator spec 3.8).

Each adapter renders it on every request, so a turn can move to another provider
between steps. It lives only for the turn that made it.
"""

from dataclasses import dataclass, field
from typing import Any

ELIDED = "[result elided: {n} bytes; call the tool again if you need it]"


@dataclass
class ToolCall:
    id: str
    name: str
    # Parsed arguments, or None when the model sent a string that is not a JSON
    # object (`raw` keeps it; the call is never sent to Argus).
    arguments: dict[str, Any] | None
    raw: str


@dataclass
class UserText:
    text: str


@dataclass
class Step:
    """One model answer: its text and the tools it called."""

    text: str
    calls: list[ToolCall] = field(default_factory=list)


@dataclass
class Result:
    call_id: str
    name: str
    text: str
    is_error: bool
    step: int  # index of the Step that called it
    elided: bool = False

    def elide(self) -> None:
        if not self.elided:
            self.text = ELIDED.format(n=len(self.text.encode()))
            self.elided = True


Item = UserText | Step | Result


@dataclass
class Transcript:
    items: list[Item] = field(default_factory=list)

    def steps(self) -> int:
        return sum(isinstance(i, Step) for i in self.items)

    def results(self) -> list[Result]:
        return [i for i in self.items if isinstance(i, Result)]

    def elide_oldest(self) -> bool:
        """Elide the oldest result still shown; False when none is left."""
        for item in self.results():
            if not item.elided:
                item.elide()
                return True
        return False

    def elide_earlier_steps(self) -> bool:
        """Elide every result but the latest step's; False when nothing changed."""
        latest = self.steps() - 1
        changed = False
        for item in self.results():
            if item.step < latest and not item.elided:
                item.elide()
                changed = True
        return changed
