"""One turn: steps, limits, failover, spend and the reply
(builtin-orchestrator spec 3.7).

A turn posts exactly one reply. Tool calls and their results live only here.
"""

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any

import httpx2 as httpx
from argus_core.accounts import LocalFailure

from argus_api.accounts.local_health import failure_sentence
from argus_api.orchestrator_builtin import prompt, tools
from argus_api.orchestrator_builtin.accounts import Engine, Lease, NoAccount, Sleep
from argus_api.orchestrator_builtin.client import ArgusClient
from argus_api.orchestrator_builtin.models import (
    Endpoint,
    Failure,
    LeaseError,
    Reply,
    ToolSpec,
    adapter_for,
    endpoint,
    send,
)
from argus_api.orchestrator_builtin.transcript import Result, Step, Transcript, UserText

STEP_LIMIT = 40  # model requests that offer tools
DEADLINE_S = 14 * 60  # one minute before the server's TURN_TIMEOUT
HARD_DEADLINE_S = 14 * 60 + 50  # the last request must end by then
RETRY_DELAYS_S = (2.0, 8.0)  # transient errors: three tries in all
BUDGET_SHARE = 0.8  # of the context window, at 4 characters per token
REPLY_TRIES = 3
STEP_LIMIT_TEXT = (
    "Step limit reached. Reply now: what you did, with ids, and what is left."
)
STOPPED_TEXT = "Argus stopped during this turn. Send the message again."
EMPTY_TEXT = "(The model returned no text.)"
NOT_RUN_TEXT = "not run: the turn reached its deadline"


class TurnEnded(Exception):
    """The turn stops here; `text` is its reply."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


@dataclass
class TurnDeps:
    argus: ArgusClient
    models: httpx.AsyncClient
    engine: Engine
    tools: list[ToolSpec]
    skill: str
    sleep: Sleep
    log: Callable[[str], None]
    clock: Callable[[], float] = time.monotonic


@dataclass
class _State:
    """What one turn keeps besides its transcript."""

    made: list[tuple[str, bool]] = field(default_factory=list)  # (tool, ok)
    tokens_in: int = 0
    tokens_out: int = 0
    switched_to: str | None = None
    secrets: set[str] = field(default_factory=set)  # of every lease it used

    def summary(self) -> str:
        return ", ".join(f"`{n}` ({'ok' if ok else 'error'})" for n, ok in self.made)


async def run(deps: TurnDeps, claim: dict[str, Any]) -> None:
    conv, message = claim["conversation"], claim["message"]
    turn_id = message["turn_id"]
    deps.log(f"turn {turn_id} started in {conv['kind']}")
    state = _State()
    transcript = Transcript()
    try:
        text = await _steps(deps, conv, message, transcript, state, deps.clock())
    except TurnEnded as end:
        text = end.text
    except asyncio.CancelledError:
        await asyncio.shield(post(deps, conv["id"], turn_id, STOPPED_TEXT, None))
        raise
    if state.switched_to:
        text = (
            f"Switched to account `{state.switched_to}` after a provider limit.\n{text}"
        )
    await _reply(deps, conv, turn_id, text, state.secrets)
    deps.log(
        f"turn {turn_id} finished ({transcript.steps()} steps,"
        f" {state.tokens_in}/{state.tokens_out} tokens)"
    )


async def _steps(
    deps: TurnDeps,
    conv: dict[str, Any],
    message: dict[str, Any],
    transcript: Transcript,
    state: _State,
    started: float,
) -> str:
    res = await deps.argus.rest("GET", f"/agents/me/chat/{conv['id']}/context")
    context = res.body if res.ok else {"messages": [], "tasks": []}
    history = prompt.history(context["messages"], message, conv)
    tool_steps = 0
    while True:
        elapsed = deps.clock() - started
        final = tool_steps >= STEP_LIMIT or elapsed >= DEADLINE_S
        if final:
            transcript.items.append(UserText(STEP_LIMIT_TEXT))
        try:
            async with asyncio.timeout(max(HARD_DEADLINE_S - elapsed, 1.0)):
                reply, lease = await _model_step(
                    deps,
                    conv,
                    context["tasks"],
                    history,
                    transcript,
                    final,
                    state,
                    message["turn_id"],
                )
        except TimeoutError:
            raise TurnEnded(_out_of_time(transcript, state)) from None
        await _report_usage(deps, conv, message["turn_id"], reply, lease, state)
        if final or not reply.calls:
            return reply.text
        tool_steps += 1
        step = transcript.steps()
        transcript.items.append(Step(reply.text, reply.calls))
        deps.log(f"step {step + 1}: {', '.join(c.name for c in reply.calls)}")
        names = {t.name for t in _offered(deps, lease)}
        for call in reply.calls:
            elapsed = deps.clock() - started
            if elapsed >= DEADLINE_S:
                # Every call needs a result; the next request is the final one.
                text, is_error = NOT_RUN_TEXT, True
            else:
                try:
                    # A tool never runs past the server's turn expiry, and the
                    # reply keeps time to be posted.
                    async with asyncio.timeout(HARD_DEADLINE_S - elapsed):
                        text, is_error = await tools.call(
                            deps.argus, call, names, message["turn_id"]
                        )
                except TimeoutError:
                    state.made.append((call.name, False))
                    raise TurnEnded(_out_of_time(transcript, state)) from None
                state.made.append((call.name, not is_error))
            transcript.items.append(Result(call.id, call.name, text, is_error, step))


def _out_of_time(transcript: Transcript, state: _State) -> str:
    return f"This turn ran out of time after {transcript.steps()} steps." + (
        f" Tools called: {state.summary()}." if state.made else ""
    )


def _offered(deps: TurnDeps, lease: Lease) -> list[ToolSpec]:
    return tools.offered(deps.tools, lease.context_tokens)


async def _model_step(
    deps: TurnDeps,
    conv: dict[str, Any],
    tasks: list[dict[str, Any]],
    history: list[tuple[str, str]],
    transcript: Transcript,
    final: bool,
    state: _State,
    turn_id: str,
) -> tuple[Reply, Lease]:
    """One step, sent again on failover or a transient error; never twice once
    it was answered."""
    transient = 0
    shortened = False
    while True:
        lease = deps.engine.lease
        if lease is None and not await deps.engine.acquire():
            raise TurnEnded(
                "No model account is available: add an Anthropic or OpenAI API key,"
                " a custom account or a local model."
            )
        lease = deps.engine.lease
        assert lease is not None
        if lease.secret:
            state.secrets.add(lease.secret)
        if lease.model is None:
            raise TurnEnded(
                f"No model is configured for `{lease.provider}`: set"
                " `orchestrator.model` or the model policy."
            )
        try:
            target = endpoint(lease.raw)
        except LeaseError as exc:
            raise TurnEnded(
                f"The model account `{lease.name}` cannot be used: {exc}."
            ) from None
        offered = _offered(deps, lease)
        system = prompt.system_prompt(
            deps.skill,
            conv,
            tasks,
            step_limit=STEP_LIMIT,
            core_profile=len(offered) < len(deps.tools),
        )
        adapter = adapter_for(lease.raw)

        render = partial(
            adapter.body,
            lease.model,
            system,
            history,
            transcript,
            offered,
            use_tools=not final,
        )
        body = render()
        budget = lease.window * BUDGET_SHARE * 4
        while len(json.dumps(body)) > budget and transcript.elide_oldest():
            body = render()
        outcome = await send(deps.models, target, adapter, body)
        if isinstance(outcome, Reply):
            return outcome, lease
        where = _Where(lease, target, turn_id)
        deps.log(f"error: {outcome.kind} {outcome.label} {where.log}")
        if outcome.kind == "quota" and lease.provider == "local":
            # A local server's 429 is load, not a quota: local accounts are never
            # exhausted, so failover would take the same account straight back.
            outcome = replace(outcome, kind="transient")
        await _handle(deps, lease, outcome, state, where)
        if outcome.kind == "transient":
            transient += 1
            if transient > len(RETRY_DELAYS_S):
                raise TurnEnded(
                    f"The model provider did not answer (`{outcome.label}`)."
                    f"{_sentence(lease, target, outcome)} {where.reply}"
                    " Send the message again."
                )
            await deps.sleep(RETRY_DELAYS_S[transient - 1])
        elif outcome.kind == "too_long":
            if shortened or not transcript.elide_earlier_steps():
                raise TurnEnded(_rejected(lease, outcome, where))
            shortened = True


@dataclass(frozen=True)
class _Where:
    """Which account, endpoint, runtime and turn a model error came from: names
    and an origin only, never a key, header or body (builtin-orchestrator 3.4)."""

    lease: Lease
    target: Endpoint
    turn_id: str

    @property
    def reply(self) -> str:
        return (
            f"Account `{self.lease.name}`, endpoint `{self.target.origin}`,"
            f" runtime builtin, turn `{self.turn_id}`."
        )

    @property
    def log(self) -> str:
        return (
            f"account={self.lease.name} endpoint={self.target.origin}"
            f" runtime=builtin turn={self.turn_id}"
        )


# Failure classes with a sentence of their own (a provider status or error token
# speaks for itself).
_TRANSPORT: dict[str, LocalFailure] = {
    f: f
    for f in (
        "dns_failed",
        "connection_refused",
        "timeout",
        "tls_failed",
        "connect_failed",
        "redirect_refused",
    )
}


def _sentence(lease: Lease, target: Endpoint, failure: Failure) -> str:
    """The transport failure in words, with what to change on a local account."""
    known = _TRANSPORT.get(failure.error_type or "")
    if known is None:
        return ""
    return " " + failure_sentence(known, target.url, local=lease.provider == "local")


async def _handle(
    deps: TurnDeps, lease: Lease, failure: Failure, state: _State, where: _Where
) -> None:
    """Quota moves to the next account; auth, request, unreachable and invalid
    responses end the turn."""
    if failure.kind == "quota":
        moved = await deps.engine.failover(lease, failure)
        if isinstance(moved, NoAccount):
            when = (
                f" The earliest reset is {moved.earliest_reset.isoformat()}."
                if moved.earliest_reset
                else ""
            )
            raise TurnEnded(
                "The model provider's limit was reached and no other account can"
                f" take over.{when} Send the message again then."
            )
        if moved.name != lease.name:
            state.switched_to = moved.name
    elif failure.kind == "auth":
        await deps.engine.auth_rejected(lease)
        raise TurnEnded(
            f"The model account `{lease.name}` was rejected. Sign it in again or"
            " replace its key."
        )
    elif failure.kind == "request":
        raise TurnEnded(_rejected(lease, failure, where))
    elif failure.kind == "unreachable":
        label = failure.error_type or failure.label
        raise TurnEnded(
            f"The model provider could not be reached (`{label}`)."
            f"{_sentence(lease, where.target, failure)} {where.reply}"
        )
    elif failure.kind == "invalid":
        raise TurnEnded(
            "The model provider answered with a response Argus cannot read"
            f" (`invalid_response`). {where.reply}"
        )


def _rejected(lease: Lease, failure: Failure, where: _Where) -> str:
    text = f"The model rejected the request (`{failure.label}`)."
    if lease.provider in ("custom", "local"):
        text += " Check that the model supports tool calls."
    return f"{text} {where.reply}"


async def _report_usage(
    deps: TurnDeps,
    conv: dict[str, Any],
    turn_id: str,
    reply: Reply,
    lease: Lease,
    state: _State,
) -> None:
    """Per-step usage (3.9); the cap stops the turn with no further model call."""
    usage = reply.usage
    if usage is not None:
        state.tokens_in += (usage.input_tokens or 0) + (usage.cached_input_tokens or 0)
        state.tokens_out += usage.output_tokens or 0
    res = await deps.argus.rest(
        "POST",
        f"/agents/me/chat/{conv['id']}/usage",
        json={
            "turn_id": turn_id,
            "usage": {
                "input_tokens": usage.input_tokens if usage else None,
                "output_tokens": usage.output_tokens if usage else None,
                "cached_input_tokens": usage.cached_input_tokens if usage else None,
                "model": lease.model,
            },
        },
    )
    if res.ok and res.body.get("cap_reached"):
        made = f" It stopped after {len(state.made)} tool calls"
        raise TurnEnded(
            "Daily budget reached during this turn."
            + (f"{made}: {state.summary()}." if state.made else f"{made}.")
        )


async def _reply(
    deps: TurnDeps,
    conv: dict[str, Any],
    turn_id: str,
    text: str,
    secrets: set[str],
) -> None:
    # Redacted whole first: a key in the title line must not reach the title.
    for secret in secrets:
        text = text.replace(secret, "[redacted]")
    title = None
    if conv["kind"] == "chat" and conv.get("title_source") == "orchestrator":
        text, title = prompt.split_title(text)
    text = text.strip() or EMPTY_TEXT
    await post(deps, conv["id"], turn_id, text, title)


async def post(
    deps: TurnDeps, conv_id: str, turn_id: str, text: str, title: str | None
) -> None:
    body = {"body": text[:65536], "turn_id": turn_id, "title": title, "usage": None}
    for _ in range(REPLY_TRIES):
        try:
            res = await deps.argus.rest("POST", f"/agents/me/chat/{conv_id}", json=body)
        except httpx.HTTPError:
            continue
        if res.ok or 400 <= res.status < 500:
            return
    # Left to the server's stale-turn expiry.
