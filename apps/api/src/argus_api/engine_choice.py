"""Engine account and provider-choice value types (task models spec 3.2).

Kept free of ``argus_api`` imports so ``tasks`` and ``accounts`` can both use
them without an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EngineAccount:
    """An engine account as the pin preference sees it: `status` is `usable`
    (available or past its reset, below `max_leases` or held by the caller),
    `exhausted` (held back, or every slot leased out) or `disabled`."""

    id: str
    provider: str
    status: str


@dataclass(frozen=True)
class EngineChoice:
    """The engine role's effective provider set for one task (task models spec 3.2).
    `providers` None means every engine provider; `account` the one account it may
    use; `reason` (`exhausted`, `disabled`, `offline`) is set when a pin fell back."""

    providers: list[str] | None
    account: str | None = None
    reason: str | None = None

    def fits(self, provider: str, account_id: str) -> bool:
        return (self.providers is None or provider in self.providers) and (
            self.account is None or account_id == self.account
        )
