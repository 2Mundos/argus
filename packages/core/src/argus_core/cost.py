"""Cost view read model (cost view spec 3.3)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from argus_core.accounts import QuotaStatus

CostWindow = Literal["24h", "7d", "30d"]


class CostFigures(BaseModel):
    """What one group spent. `runs = measured_runs + not_reported_runs`. A token
    field is null when no measured run carried it, never 0 (cost view 3.1)."""

    runs: int
    measured_runs: int
    not_reported_runs: int
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
    by_account: list[AccountCost]
    by_project: list[ProjectCost]
    daily: list[DayCost]
