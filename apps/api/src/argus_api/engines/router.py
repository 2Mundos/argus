"""Routing: cheap work to the local model only when everything is provable
(engines spec 3). Pure functions; the same inputs always give the same decision."""

import json

from argus_core.engines import (
    EngineCatalog,
    EngineEntry,
    LocalModelInfo,
    LocalRunRequest,
    RouteDecision,
    RouteRequest,
)

# Worst-case UTF-8 width per Unicode codepoint when only a character count is known.
MAX_UTF8_BYTES_PER_CODEPOINT = 4


def estimate_tokens(chars: int, *, chat_template_overhead: int) -> int:
    """Upper bound when the caller only reports a character count (not a guarantee)."""
    return chars * MAX_UTF8_BYTES_PER_CODEPOINT + chat_template_overhead


def estimate_tokens_from_text(text: str, *, chat_template_overhead: int) -> int:
    """Upper bound from UTF-8 bytes (1 token per byte worst case), plus template."""
    return len(text.encode("utf-8")) + chat_template_overhead


def estimate_local_run_input_tokens(
    req: LocalRunRequest,
    model: str,
    payload: dict,
    *,
    chat_template_overhead: int,
) -> int:
    """Upper bound from the serialized chat completion body actually sent."""
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return len(raw.encode("utf-8")) + chat_template_overhead


def input_fits_context(
    *,
    estimated_input: int,
    max_output_tokens: int,
    n_ctx: int | None,
    max_input_fraction: float,
    output_reserve: int,
) -> tuple[bool, int | None, int | None]:
    """True when input estimate plus output reserve fits the served context."""
    if n_ctx is None:
        return False, None, None
    reserve = max(output_reserve, max_output_tokens)
    budget = int(n_ctx * max_input_fraction)
    return estimated_input + reserve <= budget, budget, reserve


def _lacks_account(entry: EngineEntry | None) -> bool:
    """True when the engine cannot run for want of a usable account: none
    registered, or every one reported exhausted or disabled. Quota that is merely
    unknown never counts."""
    if entry is None:
        return False
    if entry.accounts is not None:
        return entry.accounts.available == 0
    return entry.readiness.authenticated.ok is False


def _why_unusable(entry: EngineEntry | None, name: str) -> str:
    if entry is None:
        return f"{name} is not usable (not in the catalogue)"
    r = entry.readiness
    missing = ", ".join(
        f"{label}: {fact.detail}"
        for label, fact in (
            ("installed", r.installed),
            ("authenticated", r.authenticated),
            ("validated", r.validated),
        )
        if fact.ok is not True
    )
    if not missing and entry.accounts is not None and entry.accounts.available == 0:
        a = entry.accounts
        missing = (
            f"no available account ({a.exhausted} exhausted, {a.disabled} disabled)"
        )
    return f"{name} is not usable ({missing or 'not in the catalogue'})"


def _fallback(
    request: RouteRequest, catalog: EngineCatalog
) -> tuple[str, str | None, str | None]:
    """The engine that runs the task (always Codex first, code or not: Claude Code
    is the preferred *executor* it delegates code to, not the engine); why it is
    blocked, if it is; and the engine it moved from when it falls back. The move
    is explicit and only for lack of a Codex account (codex accounts spec 3):
    every other gap blocks instead."""
    want, other = "codex", "claude"
    by_id = {e.id: e for e in catalog.engines}
    entry, alt = by_id.get(want), by_id.get(other)
    if entry is not None and entry.usable:
        return want, None, None
    reason = _why_unusable(entry, want)
    if _lacks_account(entry) and alt is not None and alt.usable:
        return other, None, f"{want} ({reason.removeprefix(f'{want} is not usable ')})"
    return want, reason, None


def _executor(request: RouteRequest, catalog: EngineCatalog) -> str | None:
    """Preferred code executor when the engine delegates: Claude Code, if usable.
    Advisory: an unusable one never blocks the task, Codex then works directly."""
    if not request.is_code:
        return None
    claude = next((e for e in catalog.engines if e.id == "claude"), None)
    return "claude" if claude is not None and claude.usable else None


def local_rejections(
    request: RouteRequest,
    local: LocalModelInfo | None,
    *,
    max_input_fraction: float,
    output_reserve: int,
    chat_template_overhead: int,
) -> tuple[list[str], int | None]:
    why: list[str] = []
    if local is None:
        return ["no local model is configured"], None
    if not local.reachable or local.error:
        why.append(f"local model unavailable: {local.error}")
    if not request.simple:
        why.append("task is not declared simple")
    if not request.low_risk:
        why.append("task is not declared low risk")
    if not request.criteria_deterministic:
        why.append("no deterministic acceptance criteria")
    if request.multi_file:
        why.append("multi-file change")
    if request.touches_sensitive:
        why.append("touches auth, security or secrets")
    est: int | None = None
    if request.input_chars is None:
        why.append("context size unknown")
    elif local.n_ctx is None:
        why.append("served context size unknown")
    else:
        est = estimate_tokens(
            request.input_chars, chat_template_overhead=chat_template_overhead
        )
        fits, budget, reserve = input_fits_context(
            estimated_input=est,
            max_output_tokens=request.output_tokens,
            n_ctx=local.n_ctx,
            max_input_fraction=max_input_fraction,
            output_reserve=output_reserve,
        )
        if not fits and budget is not None and reserve is not None:
            why.append(
                f"does not fit: {est} input + {reserve} output reserve > "
                f"{budget} ({max_input_fraction:.0%} of {local.n_ctx})"
            )
    return why, est


def route(
    request: RouteRequest,
    catalog: EngineCatalog,
    *,
    max_input_fraction: float = 0.5,
    output_reserve: int = 2048,
    chat_template_overhead: int = 512,
) -> RouteDecision:
    local = catalog.local
    rejections, est = local_rejections(
        request,
        local,
        max_input_fraction=max_input_fraction,
        output_reserve=output_reserve,
        chat_template_overhead=chat_template_overhead,
    )
    n_ctx = local.n_ctx if local else None
    local_entry = next((e for e in catalog.engines if e.id == "local"), None)
    fallback, blocked, moved_from = _fallback(request, catalog)
    executor = _executor(request, catalog)
    if not rejections:
        # Fits and is simple; but a local model that never passed a real run
        # is not trusted with work (installed != validated).
        if local_entry is not None and local_entry.usable:
            return RouteDecision(
                target="local",
                reason="simple, low risk, deterministic criteria, fits the context",
                local_considered=True,
                local_rejections=[],
                estimated_input_tokens=est,
                context_tokens=n_ctx,
                model=local.model if local else None,
                escalate_to=fallback,
                fallback_from=moved_from,
                executor=executor,
            )
        rejections = ["local model is not validated by a real run yet"]
    return RouteDecision(
        target=fallback,
        reason="; ".join(rejections)
        + (f"; fell back from {moved_from}" if moved_from else ""),
        local_considered=local is not None,
        local_rejections=rejections,
        estimated_input_tokens=est,
        context_tokens=n_ctx,
        model=None,
        blocked=blocked,
        fallback_from=moved_from,
        executor=executor,
    )
