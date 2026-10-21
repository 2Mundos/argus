import contextlib
from typing import Any, Literal

from argus_core.accounts import AccountState
from argus_core.criteria_safe import criteria_is_effective
from argus_core.engines import (
    AccountsSummary,
    EngineCatalog,
    LocalAttempt,
    LocalRunRequest,
    LocalRunResult,
    RouteDecision,
    RouteRequest,
)
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from argus_api import events, model_policy
from argus_api.accounts import service as accounts_service
from argus_api.db import utcnow
from argus_api.deps import (
    AgentDep,
    CallerDep,
    DbDep,
    ManagerDep,
    SecretsDep,
    SettingsDep,
)
from argus_api.engines import catalog as catalog_mod
from argus_api.engines import router as routing
from argus_api.engines.local import LocalModel, build_chat_completion_payload
from argus_api.engines.local_readiness import local_identity, validated_from_events
from argus_api.errors import ConflictError, DomainError
from argus_api.secrets import SecretStore, ref_name

router = APIRouter(tags=["engines"])


class LocalModelNotConfiguredError(DomainError):
    status = 409
    code = "local_model_not_configured"


def _local(request: Request) -> LocalModel | None:
    return request.app.state.local_model


async def _account_summaries(
    db: DbDep, secrets: SecretStore
) -> dict[str, AccountsSummary]:
    """Registry counts per provider. `available`/`exhausted`/`disabled` are
    lifecycle (an exhausted account past its reset counts as available, as the
    lease logic treats it); `busy`, `no_credential`, `leasable` and
    `best_priority` mirror what a lease could take now. Read-only: a missing
    secret is counted, never repaired, and no credential is read out."""
    now = utcnow()
    out: dict[str, AccountsSummary] = {}
    async with db.read() as conn:
        listed = await accounts_service.list_accounts(conn, now)
    for a in listed:
        s = out.setdefault(a.provider, AccountsSummary(leasable=0))
        if a.state is AccountState.DISABLED:
            s.disabled += 1
        elif a.state is AccountState.EXHAUSTED and (
            a.exhausted_until is None or a.exhausted_until > now
        ):
            s.exhausted += 1
        else:
            s.available += 1
            name = ref_name(a.secret_ref)
            if name is None or secrets.get(name) is None:
                s.no_credential += 1
            elif a.max_leases is not None and a.active_leases >= a.max_leases:
                s.busy += 1
            else:
                s.leasable = (s.leasable or 0) + 1
                if s.best_priority is None or a.priority > s.best_priority:
                    s.best_priority = a.priority
    return out


async def _catalog(
    request: Request, settings: Any, db: DbDep, secrets: SecretStore
) -> EngineCatalog:
    local = _local(request)
    info = await local.probe() if local else None
    model = local.model if local else None
    identity = None
    event_validated = None
    if local is not None and model is not None and info is not None:
        identity = local_identity(local.url, model, info.n_ctx)
        event_validated = await validated_from_events(db, identity)
    return catalog_mod.build(
        catalog_mod.read_ledger(settings.engines_ledger_path),
        info,
        model,
        local_identity=identity,
        local_validated_event=event_validated,
        accounts=await _account_summaries(db, secrets),
    )


@router.get("/engines")
async def list_engines(
    request: Request,
    settings: SettingsDep,
    db: DbDep,
    secrets: SecretsDep,
    _: CallerDep,
) -> EngineCatalog:
    """Engines, executors and the local model: axes, auth methods and the three
    readiness facts (installed, authenticated, validated). Read-only."""
    return await _catalog(request, settings, db, secrets)


@router.post("/engines/route")
async def route_task(
    body: RouteRequest,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    secrets: SecretsDep,
    caller: ManagerDep,
) -> RouteDecision:
    """Where should this task run? Deterministic; the decision is recorded."""
    decision = routing.route(
        body,
        await _catalog(request, settings, db, secrets),
        max_input_fraction=settings.local_max_input_fraction,
        output_reserve=settings.local_output_reserve,
        chat_template_overhead=settings.local_chat_template_overhead,
        policy=model_policy.effective_policy(settings.model_policy_path),
        executor_chains=settings.difficulty_executors,
    )
    async with db.write() as tx:
        await events.record(
            tx,
            caller.actor,
            "engine",
            decision.target,
            "engine.routed",
            {
                "summary": body.summary[:200],
                "target": decision.target,
                "reason": decision.reason,
                "model": decision.model,
                "estimated_input_tokens": decision.estimated_input_tokens,
                "context_tokens": decision.context_tokens,
                "blocked": decision.blocked,
                "fallback_from": decision.fallback_from,
                "difficulty": decision.difficulty,
                "executor": decision.executor,
                "executor_model": decision.executor_model,
                "executor_reason": decision.executor_reason,
            },
        )
    return decision


class EngineSelected(BaseModel):
    engine: Literal["codex", "claude"]
    provider: str = Field(min_length=1, max_length=50)
    account_id: str = Field(min_length=1, max_length=64)
    reason: Literal["lease", "exhausted"] = "lease"
    # The claim the switch serves: marks that attempt's failover `applied`.
    task_id: str | None = None
    attempt: int | None = Field(default=None, ge=1)


@router.post("/engines/selected", status_code=204)
async def engine_selected(body: EngineSelected, db: DbDep, agent_id: AgentDep) -> None:
    """A worker's channel switched its harness to follow a leased account
    (codex accounts spec 3). Recorded for the audit trail; credentials never ride here."""
    async with db.write() as tx:
        await events.record(
            tx,
            f"agent:{agent_id}",
            "engine",
            body.engine,
            "engine.selected",
            body.model_dump(exclude={"task_id", "attempt"}),
        )
        if body.task_id is not None and body.attempt is not None:
            # A stale claim must not lose the audit event above.
            with contextlib.suppress(ConflictError):
                await accounts_service.switch_applied(
                    tx, agent_id, body.task_id, body.attempt, body.account_id
                )


@router.post("/engines/local/run")
async def run_local(
    body: LocalRunRequest,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    secrets: SecretsDep,
    caller: CallerDep,
) -> LocalRunResult:
    """One bounded completion on the local model, checked against `criteria`.
    No tools act, so retrying or escalating duplicates no effect. An answer that
    fails the criteria returns `verdict: escalate`; Argus does not start the
    escalated work, the caller does."""
    local = _local(request)
    if local is None:
        raise LocalModelNotConfiguredError(
            "no local model: set ARGUS_LOCAL_MODEL_URL and ARGUS_LOCAL_MODEL"
        )
    info = await local.probe()
    escalate_to = routing.escalation_target(
        await _catalog(request, settings, db, secrets)
    )
    payload = build_chat_completion_payload(body, local.model)
    est = routing.estimate_local_run_input_tokens(
        body,
        local.model,
        payload,
        chat_template_overhead=settings.local_chat_template_overhead,
    )
    fits, budget, reserve = routing.input_fits_context(
        estimated_input=est,
        max_output_tokens=body.max_output_tokens,
        n_ctx=info.n_ctx,
        max_input_fraction=settings.local_max_input_fraction,
        output_reserve=settings.local_output_reserve,
    )
    if info.n_ctx is None:
        result = LocalRunResult(
            verdict="escalate",
            model=local.model,
            attempts=[
                LocalAttempt(
                    text=None,
                    finish_reason=None,
                    prompt_tokens=None,
                    completion_tokens=None,
                    latency_ms=0,
                    failures=["served context size unknown"],
                )
            ],
            text=None,
            escalate_to=escalate_to,
        )
    elif not fits:
        result = LocalRunResult(
            verdict="escalate",
            model=local.model,
            attempts=[
                LocalAttempt(
                    text=None,
                    finish_reason=None,
                    prompt_tokens=None,
                    completion_tokens=None,
                    latency_ms=0,
                    failures=[
                        f"does not fit: {est} input + {reserve} output reserve > "
                        f"{budget} ({settings.local_max_input_fraction:.0%} of {info.n_ctx})"
                    ],
                )
            ],
            text=None,
            escalate_to=escalate_to,
        )
    else:
        result = await local.run(body, escalate_to=escalate_to)
    last = result.attempts[-1] if result.attempts else None
    vid = local_identity(local.url, local.model, info.n_ctx)
    async with db.write() as tx:
        await events.record(
            tx,
            caller.actor,
            "engine",
            "local",
            "engine.local_run",
            {
                "verdict": result.verdict,
                "model": result.model,
                "attempts": len(result.attempts),
                "prompt_tokens": last.prompt_tokens if last else None,
                "completion_tokens": last.completion_tokens if last else None,
                "latency_ms": sum(a.latency_ms for a in result.attempts),
                "failures": last.failures if last else [],
                "cost": None,
                "validation_identity": vid,
                "criteria_effective": criteria_is_effective(body.criteria),
            },
        )
    return result
