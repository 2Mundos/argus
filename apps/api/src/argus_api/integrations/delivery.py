"""Task opt-in and worker delivery of Kubernetes and MCP integrations
(docs/specs/integrations.spec.md 3.1, 3.6).

A task names integration ids of its own project; the worker that holds the
task's current attempt fetches each credential through the one secret-bearing
route, which re-checks everything inside a write transaction (so a rotation or
revocation either lands before it or after it, never half way) and records a
secret-free audit event. Nothing here is an MCP tool or an orchestrator surface.
"""

import json
from datetime import datetime, timedelta
from typing import Literal

import aiosqlite
from argus_core.accounts import GITHUB_HOST
from argus_core.tasks import Task, TaskState
from pydantic import BaseModel, Field

from argus_api.db import Snapshot, Tx, ts
from argus_api.errors import ConflictError, ForbiddenError
from argus_api.events import record
from argus_api.integrations import selection
from argus_api.integrations.service import (
    IntegrationChangedError,
    IntegrationRevokedError,
    ToolPolicy,
    project_forge,
    tool_policy,
)
from argus_api.secrets import SecretStore, SecretStoreError, ref_name
from argus_api.tasks import service as tasks

# Providers whose harness Argus can confine to the attempt's kubeconfig:
# anthropic, local and custom run the same Claude harness session, and the
# Codex harness reads the installed file the same CLI-level way (spec 3.6
# engine matrix). MCP servers reach Claude sessions and the Codex harness; for
# Codex the channel's broker refuses a legacy HTTP+SSE server before any grant,
# since Codex's config has only Streamable HTTP entries. A delegated executor is
# confined only for Claude.
KUBERNETES_ENGINES = frozenset({"anthropic", "local", "custom", "openai"})
MCP_ENGINES = frozenset({"anthropic", "local", "custom", "openai"})
EXECUTOR_PROVIDERS = frozenset({"anthropic"})
REPOSITORY_WRITE = "repository:write"

Readiness = Literal[
    "ready",
    "revoked",
    "expired",
    "tools_unknown",
    "unvalidated",
    "secret_missing",
    "store_unavailable",
]


class IntegrationExpiredError(ConflictError):
    code = "integration_expired"


class IntegrationUnvalidatedError(ConflictError):
    code = "integration_unvalidated"


class EngineUnsupportedError(ConflictError):
    code = "integration_engine_unsupported"


class IntegrationToolsUnknownError(ConflictError):
    code = "integration_tools_unknown"


class ProjectRepositoryMissingError(ConflictError):
    code = "project_repository_missing"


class ForgeMissingError(ConflictError):
    code = "forge_missing"


class ForgeReadOnlyError(ConflictError):
    code = "forge_read_only"


class DeliveryRequest(BaseModel):
    attempt: int
    # The version the worker expects (from the status read); a mismatch is 409
    # `integration_changed`, never an older token.
    secret_version: int | None = None


class IntegrationState(BaseModel):
    """Non-secret: what the worker needs to decide to (re)install or remove."""

    integration_id: str
    kind: Literal["kubernetes", "mcp"]
    name: str
    namespace: str | None  # Kubernetes only
    tool_policy: ToolPolicy | None  # MCP only
    secret_version: int
    # A config edit raises it (3.4): reinstall, as for a new secret_version.
    config_version: int
    expires_at: datetime | None
    readiness: Readiness


class TaskIntegrations(BaseModel):
    """The body of `PUT /api/tasks/{id}/integrations` (admin only)."""

    expected_version: str = Field(min_length=1)  # the task's `version`
    integrations: list[str] = Field(max_length=selection.MAX_PER_TASK)


class KubernetesCredential(BaseModel):
    """With McpCredential, the only models that carry a token. Returned to the
    assigned worker's channel by the delivery route and nowhere else; never logged
    or recorded."""

    kind: Literal["kubernetes"] = "kubernetes"
    integration_id: str
    name: str
    server: str
    namespace: str
    ca_data: str | None
    access: Literal["read_only", "read_write"]
    token: str
    secret_version: int
    config_version: int
    expires_at: datetime | None


class McpCredential(BaseModel):
    """For the channel's MCP broker only, which keeps the token in memory and
    hands the session a grant URL instead (3.6, MCP broker)."""

    kind: Literal["mcp"] = "mcp"
    integration_id: str
    name: str
    url: str
    # The allowlist (`allowlist`), or null with `all`; `unknown` is never delivered.
    tools: list[str] | None
    tool_policy: Literal["allowlist", "all"]
    access: Literal["read_only", "read_write"]
    token: str
    secret_version: int
    config_version: int
    expires_at: datetime | None


class ForgeCredential(BaseModel):
    """The project's repository credential for one attempt (forge integrations
    spec 3.4). Only the channel's OneCLI step sees it; never the session."""

    kind: Literal["forge"] = "forge"
    integration_id: str
    name: str
    provider: Literal["gitea", "github"]
    host: str
    username: str | None
    access: Literal["read_only", "read_write"]
    token: str
    secret_version: int
    config_version: int
    expires_at: datetime | None


# A claimed attempt keeps the selection it started with; a finished task has none.
_SELECTABLE = frozenset(
    {
        TaskState.AWAITING_APPROVAL,
        TaskState.PENDING,
        TaskState.READY,
        TaskState.BLOCKED,
    }
)


async def set_selection(
    tx: Tx, task_id: str, body: TaskIntegrations, actor: str
) -> Task:
    """Replace a task's opt-in (spec 3.6). Not while an attempt runs, not on an
    archived or finished task; a stale `expected_version` is 409 `stale_version`."""
    task = await tasks.get_task(tx.conn, task_id)
    if task.version != body.expected_version:
        raise ConflictError(
            f"task {task_id} changed since you read it; read it again",
            code="stale_version",
        )
    if task.state not in _SELECTABLE or task.archived_at is not None:
        raise ConflictError(
            f"task {task_id} is {task.state}; its integrations cannot change now",
            code="task_not_editable",
        )
    before = task.integrations
    after = await selection.select(tx, task_id, task.project, body.integrations)
    if after != before:
        await tx.conn.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?",
            (ts(max(tx.now, task.updated_at + _TICK)), task_id),
        )
        await record(
            tx,
            actor,
            "task",
            task_id,
            "task.integrations_changed",
            {
                "added": sorted(set(after) - set(before)),
                "removed": sorted(set(before) - set(after)),
            },
        )
    return await tasks.get_task(tx.conn, task_id)


_TICK = timedelta(microseconds=1)


def _readiness(
    row: aiosqlite.Row,
    now: str,
    store: SecretStore,
    store_available: bool,
    *,
    exact_version: bool,
) -> Readiness:
    if row["state"] != "active" or row["secret_ref"] is None:
        return "revoked"
    if row["expires_at"] is not None and row["expires_at"] <= now:
        return "expired"
    if _policy(row) == "unknown":
        return "tools_unknown"
    # A forge record is delivered unchecked: every attempt's repository preflight
    # proves access, and a migrated record must not block work (forge spec 3.4).
    if row["validation"] != "ok" and not (
        row["kind"] == "forge" and row["validation"] == "unchecked"
    ):
        return "unvalidated"
    if not store_available:
        return "store_unavailable"
    ref = ref_name(row["secret_ref"])
    if ref is None:
        return "secret_missing"
    try:
        stored = store.get(ref)
    except SecretStoreError:
        return "store_unavailable"
    if not stored or not isinstance(stored.get("token"), str):
        return "secret_missing"
    # Rotate writes the store inside its transaction, so a status snapshot may see the
    # new token beside the old row; only delivery, which serializes with rotate, decides.
    if exact_version and stored.get("version") not in (None, row["secret_version"]):
        return "store_unavailable"  # the store is not at the row's version
    return "ready"


def _policy(row: aiosqlite.Row) -> ToolPolicy | None:
    return tool_policy(row["kind"], row["access"], json.loads(row["config"]))


async def _held(
    tx: Tx | Snapshot, task_id: str, attempt: int, agent_id: str
) -> aiosqlite.Row:
    """The task row, if `agent_id` holds `attempt` now (worker lifecycle 3.3)."""
    return await tasks.check_lease(tx, task_id, attempt, agent_id)


async def _engine_supported(tx: Tx, agent_id: str, kind: str) -> None:
    """The worker's harness and any delegated executor must run a session Argus
    can confine to the attempt's credential: a Kubernetes kubeconfig is
    CLI-level and reads the same on every harness, while MCP servers need a
    Claude or Codex harness session; a delegated executor is confined only for Claude."""
    async with tx.conn.execute(
        "SELECT l.role, a.provider FROM account_leases l"
        " JOIN accounts a ON a.id = l.account_id"
        " WHERE l.agent_id = ? AND l.released_at IS NULL"
        " AND l.role IN ('engine', 'executor')",
        (agent_id,),
    ) as cur:
        leases = {r["role"]: r["provider"] for r in await cur.fetchall()}
    engine = leases.get("engine")
    allowed = KUBERNETES_ENGINES if kind == "kubernetes" else MCP_ENGINES
    if engine not in allowed:
        want = (
            "a Kubernetes-capable harness engine (anthropic, local, custom or openai)"
            if kind == "kubernetes"
            else "a Claude or Codex harness engine (anthropic, local, custom or openai)"
        )
        raise EngineUnsupportedError(
            f"Task integrations need {want}; this worker runs"
            f" {engine or 'no leased engine'}"
        )
    if (executor := leases.get("executor")) not in (None, *EXECUTOR_PROVIDERS):
        raise EngineUnsupportedError(
            f"Task integrations need a Claude executor; this worker delegates"
            f" to {executor}"
        )


async def _selected_rows(
    tx: Tx | Snapshot, task: aiosqlite.Row, integration_id: str | None = None
) -> list[aiosqlite.Row]:
    async with tx.conn.execute(
        "SELECT i.* FROM task_integrations s JOIN integrations i"
        " ON i.id = s.integration_id"
        " WHERE s.task_id = ? AND i.project = ?"
        " AND (? IS NULL OR i.id = ?) ORDER BY i.name",
        (task["id"], task["project"], integration_id, integration_id),
    ) as cur:
        return list(await cur.fetchall())


async def status(
    tx: Tx | Snapshot,
    task_id: str,
    attempt: int,
    agent_id: str,
    store: SecretStore,
    *,
    store_available: bool,
) -> list[IntegrationState]:
    """The task's selected integrations and whether each is deliverable now. The
    channel reads it before the session starts and on every heartbeat; anything
    not `ready` means remove the installed file."""
    task = await _held(tx, task_id, attempt, agent_id)
    now = ts(tx.now)
    return [
        IntegrationState(
            integration_id=r["id"],
            kind=r["kind"],
            name=r["name"],
            namespace=json.loads(r["config"]).get("namespace"),
            tool_policy=_policy(r),
            secret_version=r["secret_version"],
            config_version=r["config_version"],
            expires_at=r["expires_at"],
            readiness=_readiness(r, now, store, store_available, exact_version=False),
        )
        for r in await _selected_rows(tx, task)
    ]


async def deliver(
    tx: Tx,
    task_id: str,
    integration_id: str,
    body: DeliveryRequest,
    agent_id: str,
    actor: str,
    store: SecretStore,
    *,
    store_available: bool,
) -> KubernetesCredential | McpCredential:
    """The credential for the attempt `agent_id` holds, or a refusal with a code
    the channel turns into an `env_integration_*` block. Runs inside the write
    so it serializes with rotate and revoke."""
    task = await _held(tx, task_id, body.attempt, agent_id)
    rows = await _selected_rows(tx, task, integration_id)
    if not rows:
        raise ForbiddenError(
            f"task {task_id} has not selected integration {integration_id}",
            code="integration_not_selected",
        )
    row = rows[0]
    await _engine_supported(tx, agent_id, row["kind"])
    name = row["name"]
    _refuse(
        row, _readiness(row, ts(tx.now), store, store_available, exact_version=True)
    )
    if body.secret_version not in (None, row["secret_version"]):
        raise IntegrationChangedError(
            f"integration {name} is at version {row['secret_version']}; read the"
            " status again"
        )
    ref = ref_name(row["secret_ref"])
    stored = store.get(ref) if ref else None
    assert stored is not None  # _readiness checked it, in this same transaction
    config = json.loads(row["config"])
    await record(
        tx,
        actor,
        "task",
        task_id,
        "task.integration_delivered",
        {
            "attempt": body.attempt,
            "integration_id": row["id"],
            "name": name,
            "secret_version": row["secret_version"],
            "config_version": row["config_version"],
        },
    )
    if row["kind"] == "mcp":
        policy = _policy(row)
        assert policy in ("allowlist", "all")  # `unknown` was refused above
        return McpCredential(
            integration_id=row["id"],
            name=name,
            url=config["url"],
            tools=config.get("tools"),
            tool_policy=policy,
            access=row["access"],
            token=stored["token"],
            secret_version=row["secret_version"],
            config_version=row["config_version"],
            expires_at=row["expires_at"],
        )
    return KubernetesCredential(
        integration_id=row["id"],
        name=name,
        server=config["server"],
        namespace=config["namespace"],
        ca_data=config.get("ca_data"),
        access=row["access"],
        token=stored["token"],
        secret_version=row["secret_version"],
        config_version=row["config_version"],
        expires_at=row["expires_at"],
    )


def _refuse(row: aiosqlite.Row, readiness: Readiness) -> None:
    """Raise the delivery error of a readiness other than `ready`."""
    name = row["name"]
    if readiness == "revoked":
        raise IntegrationRevokedError(f"integration {name} is revoked")
    if readiness == "expired":
        raise IntegrationExpiredError(
            f"integration {name} expired at {row['expires_at']}; rotate it"
        )
    if readiness == "tools_unknown":
        raise IntegrationToolsUnknownError(
            f"integration {name} is read-only without a tool allowlist; an admin"
            " must add one before workers can use it"
        )
    if readiness == "unvalidated":
        raise IntegrationUnvalidatedError(
            f"integration {name} is {row['validation']}; validate it before use"
        )
    if readiness == "secret_missing":
        raise SecretStoreError(
            f"integration {name} has no stored credential; rotate it",
            code="integration_secret_missing",
        )
    if readiness == "store_unavailable":
        raise SecretStoreError("integration credential store unavailable")


async def repository_credential(
    tx: Tx,
    task_id: str,
    body: DeliveryRequest,
    agent_id: str,
    actor: str,
    store: SecretStore,
    *,
    store_available: bool,
) -> ForgeCredential:
    """The project's forge credential for the attempt `agent_id` holds (forge
    integrations spec 3.4): no selection, the project's repository is the grant.
    Inside the write, so it serializes with rotate and revoke."""
    await _held(tx, task_id, body.attempt, agent_id)
    task = await tasks.get_task(tx.conn, task_id)
    repository = await project_forge(tx.conn, task.project)
    if repository is None:
        raise ProjectRepositoryMissingError(f"project {task.project} has no repository")
    host = repository[0]
    async with tx.conn.execute(
        "SELECT * FROM integrations WHERE project = ? AND kind = 'forge'"
        " AND state = 'active'",
        (task.project,),
    ) as cur:
        row = await cur.fetchone()
    config = json.loads(row["config"]) if row else {}
    if row is None or config.get("host") != host:
        raise ForgeMissingError(
            f"no forge integration for {host} in project {task.project}; an admin"
            " adds one in Setup → Integrations"
        )
    if REPOSITORY_WRITE in task.required_capabilities and row["access"] != "read_write":
        raise ForgeReadOnlyError(
            f"forge integration {row['name']} is read-only and this task writes to"
            " the repository"
        )
    _refuse(
        row, _readiness(row, ts(tx.now), store, store_available, exact_version=True)
    )
    if body.secret_version not in (None, row["secret_version"]):
        raise IntegrationChangedError(
            f"integration {row['name']} is at version {row['secret_version']}"
        )
    ref = ref_name(row["secret_ref"])
    stored = store.get(ref) if ref else None
    assert stored is not None  # _readiness checked it, in this same transaction
    await record(
        tx,
        actor,
        "task",
        task_id,
        "task.integration_delivered",
        {
            "attempt": body.attempt,
            "integration_id": row["id"],
            "name": row["name"],
            "secret_version": row["secret_version"],
            "config_version": row["config_version"],
        },
    )
    return ForgeCredential(
        integration_id=row["id"],
        name=row["name"],
        provider="github" if host == GITHUB_HOST else "gitea",
        host=host,
        username=config.get("username"),
        access=row["access"],
        token=stored["token"],
        secret_version=row["secret_version"],
        config_version=row["config_version"],
        expires_at=row["expires_at"],
    )
