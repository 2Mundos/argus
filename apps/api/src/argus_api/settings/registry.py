"""The closed set of setting keys (docs/specs/settings.spec.md 3.1)."""

from typing import Any

from argus_core.difficulty import Difficulty
from argus_core.settings import IndependentReviewSetting, SettingDefinition
from argus_core.tasks import Deliverable

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

_DIFFICULTY_EXECUTORS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "propertyNames": {"enum": ["trivial", "simple", "standard", "hard"]},
    "patternProperties": {
        ".*": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "string",
                "enum": ["claude", "codex", "opencode", "agent", "none"],
            },
            "uniqueItems": True,
        }
    },
}

_GIT_IDENTITY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "email"],
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 200},
        "email": {
            "type": "string",
            "maxLength": 320,
            "pattern": r"^[^\s@]+@[^\s@]+$",
        },
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
    "difficulty.executors": SettingDefinition(
        key="difficulty.executors",
        schema=_DIFFICULTY_EXECUTORS_SCHEMA,
        default={},
        scopes=["global", "project"],
        description=(
            "Ordered executor CLIs per task difficulty; empty or [none] means the "
            "harness does the work with no delegated executor."
        ),
    ),
    "git.identity": SettingDefinition(
        key="git.identity",
        schema=_GIT_IDENTITY_SCHEMA,
        default={},
        scopes=["global", "project"],
        description=(
            "Git author and committer {name, email} for worker commits; a project "
            "value overrides the global one. Unset means workers cannot commit."
        ),
    ),
    "review.independent": SettingDefinition(
        key="review.independent",
        schema=_REVIEW_SCHEMA,
        default=REVIEW_DEFAULT,
        scopes=["global", "project"],
        description="Require review by a different model before a code task succeeds.",
    ),
    "task_default_budget_usd": SettingDefinition(
        key="task_default_budget_usd",
        schema={"type": ["number", "null"], "exclusiveMinimum": 0, "maximum": 10000},
        default=None,
        scopes=["global", "project"],
        description=(
            "Budget (USD, estimated) for a new task that names none; a project value"
            " overrides the global one. Unset: no budget."
        ),
    ),
    # Task defaults (task defaults spec 3.1): fill what a new task leaves out.
    "task_default_deliverable": SettingDefinition(
        key="task_default_deliverable",
        schema={"enum": [d.value for d in Deliverable]},
        default=Deliverable.RESPONSE.value,
        scopes=["global", "project"],
        description=(
            "Deliverable for a new task that names none. Code deliverables also need"
            " repository:write in the capabilities and a project repository."
        ),
    ),
    "task_default_priority": SettingDefinition(
        key="task_default_priority",
        schema={"type": "integer", "minimum": -1_000_000, "maximum": 1_000_000},
        default=0,
        scopes=["global", "project"],
        description="Priority for a new task that names none; higher is claimed first.",
    ),
    "task_default_difficulty": SettingDefinition(
        key="task_default_difficulty",
        schema={"enum": [*(d.value for d in Difficulty), None]},
        default=None,
        scopes=["global", "project"],
        description=(
            "Difficulty for a new task that names none. Null: inferred from the"
            " title and spec."
        ),
    ),
    "task_default_required_capabilities": SettingDefinition(
        key="task_default_required_capabilities",
        schema={
            "type": "array",
            "maxItems": 50,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
        },
        default=[],
        scopes=["global", "project"],
        description=(
            "Capabilities a new task requires when it names none; [] requires none."
        ),
    ),
    "task_default_max_attempts": SettingDefinition(
        key="task_default_max_attempts",
        schema={"type": "integer", "minimum": 1, "maximum": 20},
        default=3,
        scopes=["global", "project"],
        description="Attempts a new task gets when it names no limit.",
    ),
    "task_default_timeout_s": SettingDefinition(
        key="task_default_timeout_s",
        schema={"type": ["integer", "null"], "minimum": 60, "maximum": 7 * 24 * 3600},
        default=None,
        scopes=["global", "project"],
        description=(
            "Per-attempt timeout (seconds) for a new task that names none. Null: the"
            " kind's, then the server's."
        ),
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
    "command_guard.enabled": SettingDefinition(
        key="command_guard.enabled",
        schema={"type": "boolean"},
        default=True,
        scopes=["global", "project"],
        description=(
            "Claude Code PreToolUse command guard is on by default; off disables "
            "it for new attempts."
        ),
    ),
    "delivery.auto_merge": SettingDefinition(
        key="delivery.auto_merge",
        schema={"type": "boolean"},
        default=False,
        scopes=["global", "project"],
        description=(
            "Workers merge their own pull request for pr_merged tasks, and those "
            "tasks succeed only once the PR is merged."
        ),
    ),
    "review.forge_agentic": SettingDefinition(
        key="review.forge_agentic",
        schema={"type": "boolean"},
        default=False,
        scopes=["global", "project"],
        description=(
            "The project's forge posts an agentic review on pull requests, and a "
            "worker may not call a PR ready or merge it without one for the exact "
            "head. Off: a missing review is not a blocker; findings and changes "
            "requested that exist still are."
        ),
    ),
    "review.acceptance": SettingDefinition(
        key="review.acceptance",
        schema={"type": "boolean"},
        default=True,
        scopes=["global", "project"],
        description=(
            "Completed tasks wait in review for acceptance; off lets them succeed "
            "directly (a green or merged PR deliverable still needs the forge)."
        ),
    ),
    "orchestrator.enabled": SettingDefinition(
        key="orchestrator.enabled",
        schema={"type": "boolean"},
        default=False,
        scopes=["global"],
        description="Host Argus's internal orchestrator (system pool) and chat.",
    ),
    "orchestrator.runtime": SettingDefinition(
        key="orchestrator.runtime",
        schema={"type": "string", "enum": ["auto", "builtin", "nanoclaw"]},
        default="auto",
        scopes=["global"],
        description=(
            "Where the internal orchestrator runs: builtin (inside Argus, on an"
            " API-key, custom or local account), nanoclaw (a container), or auto."
            " Applied when it is enabled."
        ),
    ),
    "orchestrator.model": SettingDefinition(
        key="orchestrator.model",
        schema={
            "type": ["string", "null"],
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$",
        },
        default=None,
        scopes=["global"],
        description=(
            "Preferred provider and model for the internal orchestrator:"
            " <engine>/<model> (claude, codex), or local or custom with an optional"
            " /<model>."
        ),
    ),
    "orchestrator.daily_budget_usd": SettingDefinition(
        key="orchestrator.daily_budget_usd",
        schema={"type": "number", "minimum": 0},
        default=5,
        scopes=["global"],
        description="Daily spend cap (USD, UTC day) for the internal orchestrator.",
    ),
    "inbox.stuck_after_s": SettingDefinition(
        key="inbox.stuck_after_s",
        schema={"type": "integer", "minimum": 60},
        default=21600,
        scopes=["global"],
        description="Seconds a task may keep the same block reason or progress before the inbox flags it stuck.",
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
