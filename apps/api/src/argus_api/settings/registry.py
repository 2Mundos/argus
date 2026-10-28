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
}


def definition(key: str) -> SettingDefinition | None:
    return REGISTRY.get(key)


def all_definitions() -> list[SettingDefinition]:
    return list(REGISTRY.values())
