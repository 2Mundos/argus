"""Model observability contracts (docs/specs/model-observability.spec.md).

Two layers are kept apart: the `harness` (the NanoClaw agent runtime) and the
`delegate` (the CLI implementer). Configuration never proves execution: an observed
model exists only with evidence, and a missing one is `null` with `not_reported`.
"""

import re
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator

Layer = Literal["harness", "delegate"]
Executor = Literal["claude", "codex", "opencode", "agent"]
Evidence = Literal[
    "reported_by_runtime", "cli_output", "provider_response", "not_reported"
]
ExecutionState = Literal["selected", "running", "succeeded", "failed", "cancelled"]
SelectionKind = Literal[
    "initial",
    "account_switch",  # another account, same model
    "model_switch",
    "provider_switch",
    "executor_switch",
    "retry_no_fallback",  # tried again, nothing changed
]
ReasonCode = Literal[
    "priority", "availability", "compatibility", "context", "quota", "error", "policy"
]
Outcome = Literal["unknown", "succeeded", "failed", "abandoned"]

MAX_EXPLANATION = 300
MAX_PER_REQUEST = 20
MAX_PER_TASK = 200

_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,99}")
_REVISION = re.compile(r"[0-9a-f]{7,64}")

# What must never reach a stored explanation: credentials in any common shape.
_SECRETS = re.compile(
    r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|\b(?:authorization|api[_-]?key|access[_-]?token|token|secret|password|passwd"
    r"|credential)s?\b\s*[:=]\s*(?:(?:bearer|basic)\s+)?\S+"
    r"|\b(?:sk|pk|rk|ghp|gho|ghs|glpat|xox[abpr])[-_][A-Za-z0-9_-]{10,}"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_.-]+"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|(?<=://)[^/\s@]+@"
    r"|[A-Za-z0-9+/]{32,}={0,2}"
)
_SPACE = re.compile(r"\s+")


def clean_text(text: str | None, limit: int = MAX_EXPLANATION) -> str | None:
    """A short single-line explanation with credential-looking parts replaced and the
    length bounded. Free text is an operational justification, never a prompt."""
    if text is None:
        return None
    text = _SECRETS.sub("[redacted]", _SPACE.sub(" ", text).strip())
    text = "".join(c for c in text if c.isprintable())
    return (text[: limit - 1] + "…") if len(text) > limit else (text or None)


def _ident(value: str | None, pattern: re.Pattern[str], what: str) -> str | None:
    if value is not None and not pattern.fullmatch(value):
        raise ValueError(f"{what} has an invalid format")
    return value


def _model(value: str | None, what: str) -> str | None:
    _ident(value, _MODEL, what)
    if value is not None and _SECRETS.search(value):
        raise ValueError(f"{what} looks like a credential")
    return value


class ExecutionReport(BaseModel):
    """One model run of a layer: a harness session, or one delegation attempt.

    `key` is chosen by the producer (stable across retries of the *report*, unique per
    run). Re-sending it completes the record: values already stored are kept, only
    gaps, `state`, `ended_at`, `error_code` and the observation can be filled.
    """

    key: str
    layer: Layer
    state: ExecutionState
    runtime: str | None = None  # the producer, e.g. nanoclaw or argus-delegate
    engine: str | None = None  # harness only: the engine, e.g. claude or codex
    executor: Executor | None = None  # delegate only: the CLI that ran
    provider: str | None = None  # the account provider, e.g. anthropic
    requested_model: str | None = None  # what was asked for
    configured_model: str | None = None  # what the runtime was configured with
    observed_model: str | None = None  # what ran, only with evidence
    evidence: Evidence = "not_reported"
    policy_revision: str | None = None  # model-selection policy snapshot
    account_id: str | None = None
    delegation_id: str | None = None  # the delegate's run id
    run_key: str | None = Field(
        default=None, max_length=200
    )  # account usage correlation
    resumed_from: str | None = None  # `key` of the execution this one resumes
    error_code: str | None = None
    started_at: AwareDatetime | None = None  # null: the model never started
    ended_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _valid(self) -> "ExecutionReport":
        _ident(self.key, _KEY, "key")
        _ident(self.resumed_from, _KEY, "resumed_from")
        _ident(self.delegation_id, _KEY, "delegation_id")
        _ident(self.account_id, _KEY, "account_id")
        for name in ("runtime", "engine", "provider", "error_code"):
            _ident(getattr(self, name), _IDENT, name)
        _ident(self.policy_revision, _REVISION, "policy_revision")
        for name in ("requested_model", "configured_model", "observed_model"):
            _model(getattr(self, name), name)
        if self.layer == "harness" and self.executor is not None:
            raise ValueError("executor belongs to the delegate layer")
        if self.layer == "delegate" and self.engine is not None:
            raise ValueError("engine belongs to the harness layer")
        if (self.observed_model is None) != (self.evidence == "not_reported"):
            raise ValueError(
                "observed_model needs evidence other than not_reported, and no"
                " observed_model means evidence not_reported"
            )
        if self.resumed_from == self.key:
            raise ValueError("an execution cannot resume itself")
        return self


class Alternative(BaseModel):
    """An option that was available when a choice was made (identifiers only)."""

    model: str | None = None
    provider: str | None = None
    executor: Executor | None = None
    reason_code: ReasonCode | None = None  # why it was not chosen

    @model_validator(mode="after")
    def _valid(self) -> "Alternative":
        _model(self.model, "model")
        _ident(self.provider, _IDENT, "provider")
        return self


class SelectionReport(BaseModel):
    """One routing decision: the initial selection, or a switch, with its coded
    operational reason (never model reasoning)."""

    key: str
    layer: Layer
    kind: SelectionKind
    reason_code: ReasonCode
    explanation: str | None = None
    from_model: str | None = None
    to_model: str | None = None
    from_provider: str | None = None
    to_provider: str | None = None
    from_executor: Executor | None = None
    to_executor: Executor | None = None
    from_account_id: str | None = None
    to_account_id: str | None = None
    alternatives: list[Alternative] = Field(default_factory=list, max_length=10)
    execution_key: str | None = None  # the execution this selection led to
    policy_revision: str | None = None
    outcome: Outcome = "unknown"
    occurred_at: AwareDatetime | None = None  # default: when Argus received it

    @model_validator(mode="after")
    def _valid(self) -> "SelectionReport":
        _ident(self.key, _KEY, "key")
        _ident(self.execution_key, _KEY, "execution_key")
        _ident(self.policy_revision, _REVISION, "policy_revision")
        for name in ("from_provider", "to_provider"):
            _ident(getattr(self, name), _IDENT, name)
        for name in ("from_account_id", "to_account_id"):
            _ident(getattr(self, name), _KEY, name)
        for name in ("from_model", "to_model"):
            _model(getattr(self, name), name)
        self.explanation = clean_text(self.explanation)
        if self.layer == "harness" and (self.from_executor or self.to_executor):
            raise ValueError("executors belong to the delegate layer")
        if self.kind == "initial" and any(
            getattr(self, f"from_{p}")
            for p in ("model", "provider", "executor", "account_id")
        ):
            raise ValueError("an initial selection has no from_* values")
        # A kind states what changed; it must never contradict what was reported.
        for kind, part in (
            ("account_switch", "account_id"),
            ("model_switch", "model"),
            ("provider_switch", "provider"),
            ("executor_switch", "executor"),
        ):
            a, b = getattr(self, f"from_{part}"), getattr(self, f"to_{part}")
            if self.kind == kind and (a is None or b is None or a == b):
                raise ValueError(
                    f"{kind} needs from_{part} and to_{part}, and they must differ"
                )
        if self.kind in ("account_switch", "retry_no_fallback"):
            for part in ("model", "provider", "executor"):
                a, b = getattr(self, f"from_{part}"), getattr(self, f"to_{part}")
                if a is not None and b is not None and a != b:
                    raise ValueError(f"{self.kind} cannot change the {part}")
        return self


class ModelTraceReport(BaseModel):
    """The ingestion payload: executions and selections of one attempt."""

    attempt: int = Field(ge=1)
    executions: list[ExecutionReport] = Field(
        default_factory=list, max_length=MAX_PER_REQUEST
    )
    selections: list[SelectionReport] = Field(
        default_factory=list, max_length=MAX_PER_REQUEST
    )

    @model_validator(mode="after")
    def _not_empty(self) -> "ModelTraceReport":
        if not (self.executions or self.selections):
            raise ValueError("report at least one execution or selection")
        return self


class UsageRef(BaseModel):
    """The account usage row of the same run (null fields: not reported)."""

    measurement: str
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    cost_usd: float | None


class Execution(ExecutionReport):
    id: str
    task_id: str
    attempt: int
    agent_id: str | None
    account_name: str | None = None
    recorded_at: datetime
    updated_at: datetime
    usage: UsageRef | None = None


class Selection(SelectionReport):
    id: str
    task_id: str
    attempt: int
    agent_id: str | None  # reporter; Argus-recorded switches use an `argus:` key
    from_account_name: str | None = None
    to_account_name: str | None = None
    at: datetime
    recorded_at: datetime


class LayerModels(BaseModel):
    """The latest execution of a layer. Fields stay separate: asked for, configured
    and observed are different facts."""

    execution_id: str
    state: ExecutionState
    executor: Executor | None
    engine: str | None
    provider: str | None
    requested_model: str | None
    configured_model: str | None
    observed_model: str | None
    evidence: Evidence
    executions: int  # every run recorded for the layer, across attempts


class ModelsSummary(BaseModel):
    """Compact card view. A layer is null when nothing was reported for it."""

    harness: LayerModels | None = None
    delegate: LayerModels | None = None
    executions: int = 0
    switches: int = 0  # selections other than `initial`
    used: list["UsedModel"] = Field(default_factory=list, max_length=8)


class UsedModel(BaseModel):
    """Distinct model identity for compact task cards."""

    layer: Layer
    tool: str | None = None
    model: str | None = None
    observed: bool = False


class ModelTrace(BaseModel):
    """Full detail: every execution and the routing timeline, oldest first."""

    task_id: str
    summary: ModelsSummary
    executions: list[Execution]
    timeline: list[Selection]
