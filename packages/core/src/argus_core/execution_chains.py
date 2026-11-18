"""Opt-in complete execution policies, inheritance and worker protocol.

Static compatibility is not readiness. API admission additionally validates exact
account roles, capacity, credentials and the worker's declared adapter capabilities.
"""

import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from argus_core.difficulty import Difficulty

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
# Same opaque model-ID contract as task pins and custom accounts.
ModelIdentifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")]


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountModel(ClosedModel):
    source: Literal["account"]


class ExplicitModel(ClosedModel):
    source: Literal["explicit"]
    id: ModelIdentifier


class Selector(ClosedModel):
    adapter: Literal["claude", "codex", "agent", "copilot", "opencode"]
    provider: Literal["anthropic", "openai", "local", "custom", "cursor", "copilot"]
    # None = any available account of this provider that can serve the role; admission
    # picks one and records it. A value pins that exact account.
    account_id: Annotated[str, Field(min_length=1, max_length=64)] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    model: Annotated[AccountModel | ExplicitModel, Field(discriminator="source")]


# Closed static target matrix, NOT an advertised list of validated worker features.
# Runtime conformance/readiness and account-role validation remain API concerns.
# It mirrors what the NanoClaw worker can run: harness engines are claude (anthropic,
# local, custom) and codex (openai); executors are the delegate CLIs, one per
# provider (`EXECUTOR_BY_PROVIDER` in integrations/nanoclaw/argus-accounts.ts).
_SELECTOR_SOURCES = {
    ("harness", "claude", "anthropic"): {"explicit"},
    ("harness", "claude", "local"): {"account"},
    ("harness", "claude", "custom"): {"account"},
    ("harness", "codex", "openai"): {"explicit"},
    ("executor", "claude", "anthropic"): {"explicit"},
    ("executor", "codex", "openai"): {"explicit"},
    ("executor", "agent", "cursor"): {"explicit"},
    ("executor", "copilot", "copilot"): {"explicit"},
    ("executor", "opencode", "local"): {"account"},
    ("executor", "opencode", "custom"): {"account", "explicit"},
}


def _validate_selector(
    selector: Selector, role: Literal["harness", "executor"]
) -> None:
    allowed = _SELECTOR_SOURCES.get((role, selector.adapter, selector.provider), set())
    if selector.model.source not in allowed:
        raise ValueError(f"unsupported_{role}_selector: adapter/provider/model source")


class SingleAlternative(ClosedModel):
    id: Identifier
    mode: Literal["single"]
    runtime: Literal["nanoclaw"]
    harness: Selector

    @model_validator(mode="after")
    def supported(self) -> Self:
        _validate_selector(self.harness, "harness")
        return self


class PairedAlternative(ClosedModel):
    id: Identifier
    mode: Literal["paired"]
    runtime: Literal["nanoclaw"]
    harness: Selector
    executor: Selector

    @model_validator(mode="after")
    def supported(self) -> Self:
        _validate_selector(self.harness, "harness")
        _validate_selector(self.executor, "executor")
        # Preserve the existing local task-pin matrix until adapter conformance
        # proves another pair. Custom bridges have separate environment handling.
        if self.harness.provider == "local" and self.executor.adapter not in {
            "agent",
            "opencode",
        }:
            raise ValueError(
                "local_pair_restriction: requires agent or opencode executor"
            )
        # Two pins that differ can never be satisfied; an unpinned side adopts the
        # other's account at selection time (one native identity per provider).
        if (
            self.harness.provider == self.executor.provider
            and self.harness.provider != "custom"
            and self.harness.account_id is not None
            and self.executor.account_id is not None
            and self.harness.account_id != self.executor.account_id
        ):
            raise ValueError(
                "one_gateway_account_per_provider: select the same native account"
            )
        return self


Alternative = Annotated[
    SingleAlternative | PairedAlternative, Field(discriminator="mode")
]
Chain = Annotated[list[Alternative], Field(min_length=1, max_length=16)]


class ExecutionChains(ClosedModel):
    version: Literal[1] = 1
    difficulties: dict[Difficulty, Chain] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        for difficulty, alternatives in self.difficulties.items():
            if len({a.id for a in alternatives}) != len(alternatives):
                raise ValueError(f"difficulties/{difficulty}: duplicate_alternative_id")
        return self


class EffectiveChain(ClosedModel):
    """Configuration facts only; no availability, selected account or lease claim."""

    source: Literal["global", "project", "legacy"]
    revision: str
    alternatives: list[Alternative] | None


def resolve_chains(
    global_policy: ExecutionChains | None = None,
    project_policy: ExecutionChains | None = None,
) -> dict[Difficulty, EffectiveChain]:
    """Replace whole per-level lists; absent lists inherit, absent both stay legacy.

    Revisions cover only that difficulty's effective value and source. Unrelated
    level edits and account health must not restart an active route traversal.
    Return deep copies so editing a preview cannot mutate a stored policy object.
    """
    global_policy = global_policy or ExecutionChains()
    project_policy = project_policy or ExecutionChains()
    result: dict[Difficulty, EffectiveChain] = {}
    for difficulty in Difficulty:
        if difficulty in project_policy.difficulties:
            source = "project"
            alternatives = project_policy.difficulties[difficulty]
        elif difficulty in global_policy.difficulties:
            source = "global"
            alternatives = global_policy.difficulties[difficulty]
        else:
            source = "legacy"
            alternatives = None
        payload = {
            "version": 1,
            "difficulty": difficulty,
            "source": source,
            "alternatives": (
                [a.model_dump(mode="json") for a in alternatives]
                if alternatives is not None
                else None
            ),
        }
        revision = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        result[difficulty] = EffectiveChain(
            source=source, revision=revision, alternatives=alternatives
        ).model_copy(deep=True)
    return result


class ResolvedSelector(ClosedModel):
    selector: Selector
    account_id: str
    model: str
    lease_id: str


class ExecutionSelection(ClosedModel):
    version: Literal[1] = 1
    # "review": an independent reviewer's route, chosen from `review.chains`.
    source: Literal["global", "project", "review"]
    revision: str
    difficulty: Difficulty
    alternative: Alternative
    index: int
    generation: int
    harness: ResolvedSelector
    candidates: list[Alternative] = Field(default_factory=list)
    executor: ResolvedSelector | None = Field(
        default=None, exclude_if=lambda v: v is None
    )


class ExecutionReport(ClosedModel):
    attempt: int = Field(ge=1)
    generation: int = Field(ge=1)
    action: Literal["applied", "advance"]
    stopped: bool = False
    failed_role: Literal["harness", "executor"] = "harness"
    # An advance attests the previous runtime stopped. Free text never selects a route.
    reason: (
        Literal[
            "quota",
            "provider_unavailable",
            "auth_missing",
            "not_installed",
            "model_not_served",
            "provider_protocol",
            "provider_transport",
        ]
        | None
    ) = None


class ExecutionCapability(ClosedModel):
    id: str
    runtime: Literal["nanoclaw"] = "nanoclaw"
    role: Literal["harness", "executor"]
    adapter: str
    provider: str
    model_sources: list[str]
    modes: list[Literal["single", "paired"]]
    excluded_harness_providers: list[str] = Field(default_factory=list)
    same_provider_same_account: bool = False


EXECUTION_CAPABILITIES = [
    ExecutionCapability.model_validate(
        {
            "id": f"nanoclaw:{role}:{adapter}:{provider}",
            "role": role,
            "adapter": adapter,
            "provider": provider,
            "model_sources": sorted(sources),
            "modes": ["single", "paired"] if role == "harness" else ["paired"],
            "excluded_harness_providers": ["local"]
            if role == "executor" and adapter not in {"agent", "opencode"}
            else [],
            "same_provider_same_account": role == "executor" and provider != "custom",
        }
    )
    for (role, adapter, provider), sources in _SELECTOR_SOURCES.items()
]


def required_capabilities(alternative: Alternative) -> set[str]:
    selectors = [("harness", alternative.harness)]
    if isinstance(alternative, PairedAlternative):
        selectors.append(("executor", alternative.executor))
    return {"execution-chains-v1"} | {
        f"{alternative.runtime}:{role}:{selector.adapter}:{selector.provider}"
        for role, selector in selectors
    }
