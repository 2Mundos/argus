"""The engine catalogue: what exists is declared here, what is *ready* is read
from the ledger and the local server, never assumed (engines spec 3)."""

import json
from pathlib import Path

from argus_core.engines import (
    AccountsSummary,
    Axes,
    EngineCatalog,
    EngineEntry,
    EngineRole,
    Fact,
    LocalModelInfo,
    Readiness,
)

from argus_api.engines.local_readiness import merge_local_validated

# (id, label, role, axes, auth methods, notes). Auth methods are the ways the
# vendor's own client supports; none of them is performed or stored by Argus.
_DECLARED: list[tuple[str, str, EngineRole, Axes, list[str], str]] = [
    (
        "codex",
        "Codex (OpenAI)",
        EngineRole.ORCHESTRATOR,
        Axes(
            model_provider="openai",
            model=None,
            harness="nanoclaw provider codex (codex app-server)",
            executor="codex",
            account_provider="openai",
        ),
        ["chatgpt-device-login", "chatgpt-auth-json-import", "openai-api-key"],
        "Main engine: orchestration and review. Its login is an Argus account "
        "(imported or made with the dedicated device login).",
    ),
    (
        "claude",
        "Claude Code (Anthropic)",
        EngineRole.CODE_EXECUTOR,
        Axes(
            model_provider="anthropic",
            model=None,
            harness="nanoclaw provider claude",
            executor="claude",
            account_provider="anthropic",
        ),
        ["argus-account", "claude-login"],
        "Preferred executor for code changes.",
    ),
    (
        "opencode",
        "OpenCode",
        EngineRole.EXECUTOR,
        Axes(
            model_provider="per-config",
            model=None,
            harness="opencode run",
            executor="opencode",
            account_provider=None,
        ),
        ["opencode-auth-login", "user-local-server"],
        "Blocked without its own credentials; never falls back to another "
        "provider. Cloud accounts need explicit authorisation.",
    ),
    (
        "cursor-agent",
        "Cursor agent",
        EngineRole.EXECUTOR,
        Axes(
            model_provider="cursor",
            model=None,
            harness="agent -p",
            executor="agent",
            account_provider="cursor",
        ),
        ["CURSOR_API_KEY", "agent-login"],
        "Blocked without CURSOR_API_KEY or a login made in the container.",
    ),
]

LOCAL_ID = "local"
_FACTS = ("installed", "authenticated", "validated")


def read_ledger(path: Path | None) -> dict:
    """The smoke's facts. A missing or unreadable file means nothing is known."""
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data.get("engines", {}) if isinstance(data, dict) else {}


def _readiness(record: dict) -> Readiness:
    facts: dict[str, Fact] = {}
    for name in _FACTS:
        try:
            facts[name] = Fact.model_validate(record.get(name) or {})
        except ValueError:
            facts[name] = Fact(ok=None, detail="unreadable ledger entry")
    return Readiness(**facts)


def _with_tools(r: Readiness, tools: object) -> Readiness:
    """A binary missing from the image makes `installed` false, named. An absent,
    unreadable or inconclusive record (docker failed) changes nothing: not checked."""
    try:
        fact = Fact.model_validate(tools or {})
    except ValueError:
        return r
    if fact.ok is False:
        return r.model_copy(update={"installed": fact})
    return r


def _with_registry(r: Readiness, summary: AccountsSummary) -> Readiness:
    """`authenticated` from the account registry (codex accounts spec 3). An
    available account makes it true; with none, the ledger's fact stands (a
    native login inside a worker still counts) and an unknown becomes false."""
    held = summary.available + summary.exhausted
    detail = (
        f"{summary.available} available, {summary.exhausted} exhausted, "
        f"{summary.disabled} disabled Argus account(s)"
    )
    if held > 0:
        return r.model_copy(update={"authenticated": Fact(ok=True, detail=detail)})
    if r.authenticated.ok is None:
        return r.model_copy(
            update={
                "authenticated": Fact(ok=False, detail=f"no Argus account; {detail}")
            }
        )
    return r


def build(
    ledger: dict,
    local: LocalModelInfo | None,
    local_model: str | None,
    *,
    local_identity: dict | None = None,
    local_validated_event: Fact | None = None,
    accounts: dict[str, AccountsSummary] | None = None,
) -> EngineCatalog:
    """`accounts` maps an account provider to its registry counts; None means the
    registry was not consulted and the ledger alone decides."""
    entries: list[EngineEntry] = []
    for eid, label, role, axes, auth, notes in _DECLARED:
        rec = ledger.get(eid, {})
        r = _with_tools(_readiness(rec), rec.get("tools"))
        summary: AccountsSummary | None = None
        if accounts is not None and axes.account_provider:
            summary = accounts.get(axes.account_provider, AccountsSummary())
            r = _with_registry(r, summary)
        entries.append(
            EngineEntry(
                id=eid,
                label=label,
                role=role,
                axes=axes,
                auth_methods=auth,
                version=rec.get("version"),
                readiness=r,
                accounts=summary,
                # Reported exhaustion removes the engine; unknown quota does not.
                usable=r.usable and (summary is None or summary.available > 0),
                notes=notes,
            )
        )
    if local is not None:
        rec = ledger.get(LOCAL_ID, {})
        installed = Fact(
            ok=local.reachable and local.model == local_model,
            detail=local.error
            or (
                f"serves {local.model}"
                if local.model == local_model
                else f"serves {local.model}, allow-list wants {local_model}"
            ),
        )
        identity = local_identity or {}
        validated = merge_local_validated(rec, identity, local_validated_event)
        r = Readiness(
            installed=installed,
            authenticated=Fact(
                ok=True, detail="no credential: loopback, Argus-brokered"
            ),
            validated=validated,
        )
        entries.append(
            EngineEntry(
                id=LOCAL_ID,
                label="Local model",
                role=EngineRole.LOCAL,
                axes=Axes(
                    model_provider="local",
                    model=local.model,
                    harness="argus-router (chat completions, delegated)",
                    executor=None,
                    account_provider=None,
                ),
                auth_methods=[],
                readiness=r,
                usable=r.usable,
                notes="Delegated executor for simple tasks, not a Codex backend.",
            )
        )
    return EngineCatalog(engines=entries, local=local)
