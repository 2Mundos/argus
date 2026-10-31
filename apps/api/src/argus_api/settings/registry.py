"""The closed set of setting keys (docs/specs/settings.spec.md 3.1)."""

from typing import Any

from argus_core.settings import IndependentReviewSetting, SettingDefinition

# JSON Schema for a model-policy map (engine -> nonempty chain). Used for both the
# full global policy and a partial project override.
_CHAIN = {
    "type": "array",
    "minItems": 1,
    "maxItems": 10,
    "items": {
        "type": "string",
        "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$",
    },
    "uniqueItems": True,
}
_ENGINE_MAP = {
    "type": "object",
    "additionalProperties": False,
    "propertyNames": {"enum": ["codex", "claude", "opencode", "agent"]},
    "patternProperties": {".*": _CHAIN},
}
_MODELS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "harness": _ENGINE_MAP,
        "implementation": _ENGINE_MAP,
        "trivial": _ENGINE_MAP,
        "hard_harness": _ENGINE_MAP,
        "hard_implementation": _ENGINE_MAP,
    },
}

_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["mode", "max_rounds"],
    "properties": {
        "mode": {
            "type": "string",
            "enum": ["off", "different_model", "different_provider"],
        },
        "max_rounds": {"type": "integer", "minimum": 1, "maximum": 20},
    },
}

REVIEW_DEFAULT = IndependentReviewSetting().model_dump()

REGISTRY: dict[str, SettingDefinition] = {
    "models": SettingDefinition(
        key="models",
        schema=_MODELS_SCHEMA,
        default={},  # effective default comes from model-policy.defaults.json
        scopes=["global", "project"],
        description="Ordered model chains per engine for harness and implementation.",
    ),
    "review.independent": SettingDefinition(
        key="review.independent",
        schema=_REVIEW_SCHEMA,
        default=REVIEW_DEFAULT,
        scopes=["global", "project"],
        description="Require review by a different model before a code task succeeds.",
    ),
    "onboarding.dismissed": SettingDefinition(
        key="onboarding.dismissed",
        schema={"type": "boolean"},
        default=False,
        scopes=["global"],
        description="Whether the admin has closed the onboarding wizard.",
    ),
    "routines.enabled": SettingDefinition(
        key="routines.enabled",
        schema={"type": "boolean"},
        default=True,
        scopes=["global"],
        description="Run routines on their schedules; off pauses every routine.",
    ),
    "orchestrator.enabled": SettingDefinition(
        key="orchestrator.enabled",
        schema={"type": "boolean"},
        default=False,
        scopes=["global"],
        description="Host Argus's internal orchestrator (system pool) and chat.",
    ),
    "orchestrator.model": SettingDefinition(
        key="orchestrator.model",
        schema={
            "type": ["string", "null"],
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$",
        },
        default=None,
        scopes=["global"],
        description="Preferred harness engine/model for the internal orchestrator.",
    ),
    "orchestrator.daily_budget_usd": SettingDefinition(
        key="orchestrator.daily_budget_usd",
        schema={"type": "number", "minimum": 0},
        default=5,
        scopes=["global"],
        description="Daily spend cap (USD, UTC day) for the internal orchestrator.",
    ),
    "orchestrator.max_concurrent_turns": SettingDefinition(
        key="orchestrator.max_concurrent_turns",
        schema={"type": "integer", "minimum": 1, "maximum": 4},
        default=2,
        scopes=["global"],
        description="Max conversation turns in flight at once (plus Updates).",
    ),
    "orchestrator.confirm_above_tasks": SettingDefinition(
        key="orchestrator.confirm_above_tasks",
        schema={"type": "integer", "minimum": 0},
        default=3,
        scopes=["global"],
        description="Skill guidance: confirm before creating more than this many tasks.",
    ),
    "orchestrator.confirm_above_usd": SettingDefinition(
        key="orchestrator.confirm_above_usd",
        schema={"type": "number", "minimum": 0},
        default=10,
        scopes=["global"],
        description="Skill guidance: confirm before a plan with budget_usd above this.",
    ),
}


def definition(key: str) -> SettingDefinition | None:
    return REGISTRY.get(key)


def all_definitions() -> list[SettingDefinition]:
    return list(REGISTRY.values())
