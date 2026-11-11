"""Cost view read model (cost view spec 3.3)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from argus_core.accounts import QuotaStatus, QuotaWindow

CostWindow = Literal["24h", "7d", "30d"]


class CostFigures(BaseModel):
    """What one group spent. `runs = measured_runs + not_reported_runs`. A token
    field is null when no measured run carried it, never 0 (cost view 3.1).
    Attempt coverage counts distinct (task, attempt) pairs among the group's rows
    (account usage 3.10); rows with no attempt count only in `runs`."""

    runs: int
    measured_runs: int
    not_reported_runs: int
    attempts: int = 0
    measured_attempts: int = 0
    not_reported_attempts: int = 0
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    estimated_cost_usd: float | None  # tokens x price table; null: nothing priced
    reported_cost_usd: float | None  # what the engine reported, never mixed in
    unpriced_runs: int  # measured runs with tokens but no price for the model


class AccountCost(CostFigures):
    account_id: str
    name: str
    provider: str
    quota: list[QuotaWindow]  # provider snapshot, as on GET /api/accounts
    quota_status: QuotaStatus


class ProjectCost(CostFigures):
    project: str | None  # null: runs without a task or project


class DayCost(CostFigures):
    start: datetime  # local midnight in the requested timezone


class PricingInfo(BaseModel):
    currency: str
    as_of: str
    version: str
    source: str
    estimate: Literal[True] = True
    unpriced_models: list[str]


class CostReport(BaseModel):
    window: CostWindow
    start: datetime
    end: datetime
    tz: str
    pricing: PricingInfo
    totals: CostFigures
    unattributed: CostFigures  # attempt placeholders with no account (3.10)
    by_account: list[AccountCost]
    by_project: list[ProjectCost]
    # Chat turns for the internal orchestrator (internal-orchestrator 3.7); not in totals.
    orchestrator: CostFigures
    daily: list[DayCost]


# Task cost spec 3.2: `partial_*` when the task has unpriced or unreported runs, so
# the true figure may be higher; `exhausted` is judged on the known figure alone.
BudgetState = Literal[
    "none", "ok", "warning", "exhausted", "partial_ok", "partial_warning"
]


class ProvisionalCost(BaseModel):
    """Running executor spend not yet replaced by its run's final row (task cost
    spec 3.8): a known lower bound. In flight while its attempt is the claimed one;
    interrupted when the run ended with no measured final row."""

    runs: int
    in_flight_runs: int
    interrupted_runs: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    estimated_cost_usd: float | None  # null: no run's model is priced
    unpriced_runs: int


class AttemptCost(CostFigures):
    attempt: int
    # Distinct models of the attempt's rows, provisional ones included; "(no model)"
    # if unnamed.
    models: list[str]
    provisional: ProvisionalCost | None = None


class TaskCost(BaseModel):
    """What one task spent, in total and per attempt (task cost spec 3.2). Rows
    with no attempt count only in `totals`. `partial` also covers interrupted
    provisional runs."""

    task_id: str
    pricing: PricingInfo
    totals: CostFigures  # final usage rows only
    provisional: ProvisionalCost | None = None
    # The budget's figure: the final estimate plus the provisional one (3.8).
    known_cost_usd: float | None = None
    partial: bool
    by_attempt: list[AttemptCost]
    budget_usd: float | None
    budget_state: BudgetState
