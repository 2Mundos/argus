"""Opt-in complete execution policies, inheritance and worker protocol.

Static compatibility is not readiness. API admission additionally validates exact
account roles, capacity, credentials and the worker's declared adapter capabilities.
"""

import hashlib
import json
import re
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from argus_core.difficulty import Difficulty

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
# Same opaque model-ID contract as task pins and custom accounts.
ModelIdentifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")]
# A level name; which names an adapter takes is in `_EFFORT`, not in this pattern.
EffortLevel = Annotated[str, Field(pattern=r"^[a-z]{1,16}$")]


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountModel(ClosedModel):
    source: Literal["account"]


class ExplicitModel(ClosedModel):
    source: Literal["explicit"]
    id: ModelIdentifier


class Selector(ClosedModel):
    adapter: Literal["claude", "codex", "agent", "copilot", "opencode", "agy", "dsh"]
    provider: Literal[
        "anthropic", "openai", "local", "custom", "cursor", "copilot", "google"
    ]
    # None = any available account of this provider that can serve the role; admission
    # picks one and records it. A value pins that exact account.
    account_id: Annotated[str, Field(min_length=1, max_length=64)] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    model: Annotated[AccountModel | ExplicitModel, Field(discriminator="source")]
    # Reasoning effort for this role only. None = the runtime's own default; it is
    # left out of dumps so existing policies keep their revisions.
    effort: EffortLevel | None = Field(default=None, exclude_if=lambda v: v is None)


# Closed static target matrix, NOT an advertised list of validated worker features.
# Runtime conformance/readiness and account-role validation remain API concerns.
# It mirrors what the NanoClaw worker can run: harness engines are claude (anthropic,
# local, custom) and codex (openai); executors are the delegate CLIs, one per
# provider (`EXECUTOR_BY_PROVIDER` in integrations/nanoclaw/argus-accounts.ts), plus
# opt-in dsh on the custom/local account contract.
_SELECTOR_SOURCES = {
    ("harness", "claude", "anthropic"): {"explicit"},
    ("harness", "claude", "local"): {"account"},
    ("harness", "claude", "custom"): {"account"},
    ("harness", "codex", "openai"): {"explicit"},
    ("executor", "claude", "anthropic"): {"explicit"},
    ("executor", "codex", "openai"): {"explicit"},
    ("executor", "agent", "cursor"): {"explicit"},
    ("executor", "copilot", "copilot"): {"explicit"},
    ("executor", "agy", "google"): {"explicit"},
    ("executor", "opencode", "local"): {"account"},
    ("executor", "opencode", "custom"): {"account", "explicit"},
    ("executor", "dsh", "local"): {"account"},
    ("executor", "dsh", "custom"): {"account", "explicit"},
}


class EffortModelRule(ClosedModel):
    """Levels a canonical model ID takes, overriding the adapter set."""

    models: list[str] = Field(default_factory=list)
    prefixes: list[str] = Field(default_factory=list)
    efforts: list[str]


class EffortSupport(ClosedModel):
    interface: str
    efforts: list[str]
    model_rules: list[EffortModelRule] = Field(default_factory=list)
    # "supported": the CLI applies any level to a model no rule lists.
    # "unverified": accepted, but whether that model takes the level is unknown.
    unlisted_models: Literal["supported", "unverified"]


# Verified per installed CLI/runtime (docs/specs/execution-chains.spec.md 3.8);
# a selector absent here takes no effort.
_CLAUDE_LEVELS = ["low", "medium", "high", "xhigh", "max"]
_CLAUDE_RULES = [
    EffortModelRule(
        models=[
            "claude-opus-4",
            "claude-opus-4-0",
            "claude-opus-4-1",
            "claude-sonnet-4",
            "claude-sonnet-4-0",
            "claude-sonnet-4-5",
            "claude-haiku-4-5",
        ],
        prefixes=["claude-3-"],
        efforts=[],
    ),
    EffortModelRule(models=["claude-opus-4-5"], efforts=["low", "medium", "high"]),
    EffortModelRule(
        models=["claude-opus-4-6", "claude-sonnet-4-6"],
        efforts=["low", "medium", "high", "max"],
    ),
]
_CODEX_RULES = [
    EffortModelRule(models=["gpt-5.5"], efforts=["low", "medium", "high", "xhigh"]),
    EffortModelRule(
        models=["gpt-6-luna", "gpt-5.6-luna", "codex-auto-review"],
        efforts=["low", "medium", "high", "xhigh", "max"],
    ),
    EffortModelRule(
        models=[
            "gpt-6-astra",
            "gpt-6.1-sol",
            "gpt-6-sol",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-daybreak-blue-latest",
            "gpt-daybreak-red-latest",
        ],
        efforts=["low", "medium", "high", "xhigh", "max", "ultra"],
    ),
]
_EFFORT = {
    ("harness", "claude", "anthropic"): EffortSupport(
        interface="NanoClaw container config effort -> Agent SDK query({effort})",
        efforts=_CLAUDE_LEVELS,
        model_rules=_CLAUDE_RULES,
        unlisted_models="supported",
    ),
    # The agent-runner accepts none..xhigh; no catalogued model takes none/minimal.
    ("harness", "codex", "openai"): EffortSupport(
        interface="NanoClaw container config effort -> Codex model_reasoning_effort",
        efforts=["low", "medium", "high", "xhigh"],
        model_rules=_CODEX_RULES,
        unlisted_models="unverified",
    ),
    ("executor", "claude", "anthropic"): EffortSupport(
        interface="claude --effort <level>",
        efforts=_CLAUDE_LEVELS,
        model_rules=_CLAUDE_RULES,
        unlisted_models="supported",
    ),
    ("executor", "codex", "openai"): EffortSupport(
        interface='codex exec -c model_reasoning_effort="<level>"',
        efforts=["low", "medium", "high", "xhigh", "max", "ultra"],
        model_rules=_CODEX_RULES,
        unlisted_models="unverified",
    ),
    ("executor", "copilot", "copilot"): EffortSupport(
        interface="copilot --reasoning-effort <level>",
        efforts=["none", "minimal", "low", "medium", "high", "xhigh", "max"],
        unlisted_models="unverified",
    ),
    ("executor", "agy", "google"): EffortSupport(
        interface="agy --effort <level>",
        efforts=["low", "medium", "high", "xhigh", "max"],
        model_rules=[
            EffortModelRule(
                models=["gemini-4-neon", "gemini-4-hydrogen"],
                efforts=["low", "medium", "high", "xhigh"],
            ),
            EffortModelRule(
                models=[
                    "gemini-4-carbon",
                    "gemini-4-argon",
                    "gemini-3.8-flash",
                    "gemini-3.7-flash",
                    "gemini-3.6-flash",
                ],
                efforts=["low", "medium", "high"],
            ),
            EffortModelRule(models=["gemini-3.1-pro"], efforts=["low", "high"]),
        ],
        unlisted_models="unverified",
    ),
}


def allowed_efforts(
    selector: Selector, role: Literal["harness", "executor"]
) -> list[str]:
    """Levels this selector's adapter and (explicit) model take; [] = none."""
    support = _EFFORT.get((role, selector.adapter, selector.provider))
    if support is None:
        return []
    if selector.model.source != "explicit":
        return support.efforts
    model = re.sub(r"-\d{8}$", "", selector.model.id.lower())
    for rule in support.model_rules:
        if model in rule.models or model.startswith(tuple(rule.prefixes)):
            return [e for e in support.efforts if e in rule.efforts]
    return support.efforts


def _validate_selector(
    selector: Selector, role: Literal["harness", "executor"]
) -> None:
    allowed = _SELECTOR_SOURCES.get((role, selector.adapter, selector.provider), set())
    if selector.model.source not in allowed:
        raise ValueError(f"unsupported_{role}_selector: adapter/provider/model source")
    if selector.effort is None:
        return
    support = _EFFORT.get((role, selector.adapter, selector.provider))
    if support is None:
        raise ValueError(
            f"{role}_effort_unsupported: {selector.adapter}/{selector.provider}"
        )
    if selector.effort not in support.efforts:
        raise ValueError(
            f"{role}_effort_invalid: {selector.effort} not in {support.efforts}"
        )
    if selector.effort not in allowed_efforts(selector, role):
        raise ValueError(
            f"{role}_effort_unsupported_by_model: {selector.effort} on "
            f"{getattr(selector.model, 'id', 'account model')}"
        )


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
            "dsh",
        }:
            raise ValueError(
                "local_pair_restriction: requires agent, opencode or dsh executor"
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
            "auth_expired",
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
    # Empty = this role/adapter takes no effort; a set value is rejected.
    efforts: list[str] = Field(default_factory=list)
    effort_interface: str | None = None
    effort_model_rules: list[EffortModelRule] = Field(default_factory=list)
    effort_unlisted_models: Literal["supported", "unverified"] | None = None


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
            if role == "executor" and adapter not in {"agent", "opencode", "dsh"}
            else [],
            "same_provider_same_account": role == "executor" and provider != "custom",
            **(
                {
                    "efforts": effort.efforts,
                    "effort_interface": effort.interface,
                    "effort_model_rules": effort.model_rules,
                    "effort_unlisted_models": effort.unlisted_models,
                }
                if (effort := _EFFORT.get((role, adapter, provider)))
                else {}
            ),
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
