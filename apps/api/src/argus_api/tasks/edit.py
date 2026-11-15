"""Task edits: dependencies, pins and the edit-history snapshots of attempts.

See docs/specs/task-edit.spec.md.
"""

import hashlib
import json
from datetime import datetime
from typing import Any

import aiosqlite
from argus_core.accounts import ENGINE_PROVIDERS, LOCAL_PROVIDER
from argus_core.difficulty import DifficultySource
from argus_core.task_edit import (
    CLEARABLE,
    TaskAttempt,
    TaskAttemptSummary,
    TaskChanges,
    editable_fields,
)
from argus_core.tasks import (
    MAX_DEPENDENCIES,
    Action,
    ExecutorModel,
    HarnessModel,
    Task,
    TaskCreate,
    TaskState,
    check_harness_providers,
)
from pydantic import ValidationError

from argus_api.db import Tx, ts
from argus_api.errors import ConflictError, NotFoundError, ValidationFailedError
from argus_api.events import record
from argus_api.tasks import cost as task_cost
from argus_api.tasks import deliverable
from argus_api.tasks.cost import BUDGET_EXHAUSTED
from argus_api.tasks.reads import (
    _JSON_FIELDS,
    _PIN_FIELDS,
    _TICK,
    S,
    _row,
    _states,
    _states_of,
    get_task,
)
from argus_api.tasks.transitions import _apply, _dependency_failed_event

_DEPS_EDITABLE = (S.PENDING, S.AWAITING_APPROVAL, S.READY)


async def set_dependencies(
    tx: Tx, task_id: str, depends_on: list[str], expected_version: str, actor: str
) -> Task:
    """Replace a waiting task's dependencies (task dependencies 3.1): an edit of
    `depends_on` alone, which keeps its own code for a task that is not waiting."""
    return await edit_task(
        tx,
        task_id,
        TaskChanges(depends_on=depends_on),
        expected_version,
        actor,
        state_code="task_not_waiting",
    )


async def edit_task(
    tx: Tx,
    task_id: str,
    changes: TaskChanges,
    expected_version: str,
    actor: str,
    *,
    state_code: str = "task_not_editable",
) -> Task:
    """Edit a task's definition (task edit spec). The one path for REST, MCP and
    the dashboard: every check runs before the first write, so a refused edit
    leaves the task as it was."""
    if unknown := changes.unknown():
        raise ValidationFailedError(
            "these fields cannot be edited: " + ", ".join(unknown),
            code="immutable_field",
            details=unknown,
        )
    given = changes.given()
    if not given:
        raise ValidationFailedError(
            "nothing to change: name at least one field",
            details=["changes: nothing to change"],
        )
    row = await _row(tx.conn, task_id)
    if row["updated_at"] != expected_version:
        raise ConflictError(
            f"task {task_id} changed since you read it (version"
            f" {row['updated_at']}); read it again and reapply your change",
            code="stale_version",
            details=[row["updated_at"]],
        )
    state = S(row["state"])
    allowed = editable_fields(state, archived=row["archived_at"] is not None)
    if rejected := [f for f in given if f not in allowed]:
        raise ConflictError(
            f"task {task_id} is {state}{' (archived)' if row['archived_at'] else ''};"
            f" {', '.join(rejected)} cannot be edited now",
            code=state_code,
            details=[
                f"state: {state}",
                f"not editable: {', '.join(rejected)}",
                f"editable: {', '.join(allowed) or 'none'}",
            ],
        )
    current = await get_task(tx.conn, task_id)
    changed = await checked_changes(tx.conn, row, current, given)
    if not changed:
        return current  # nothing differs: no write, same version
    deps = changed.get("depends_on")
    dep_states: dict[str, TaskState] = {}
    if deps is not None:
        dep_states = await _check_dependencies(tx.conn, task_id, deps)
    if (pins := changed.get("knowledge")) is not None:
        await _check_new_pins(
            tx.conn, row["project"], [p for p in pins if p not in current.knowledge]
        )

    sets = column_sets(changed)
    if sets:
        assignments = ", ".join(f"{k} = ?" for k in sets)
        await tx.conn.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?",  # noqa: S608 - keys are code-controlled
            (*sets.values(), task_id),
        )
    if pins is not None:
        await tx.conn.execute(
            "DELETE FROM task_knowledge WHERE task_id = ?", (task_id,)
        )
        await tx.conn.executemany(
            "INSERT INTO task_knowledge (task_id, page_id) VALUES (?, ?)",
            [(task_id, p) for p in pins],
        )
    if deps is not None:
        await tx.conn.execute("DELETE FROM task_deps WHERE task_id = ?", (task_id,))
        await tx.conn.executemany(
            "INSERT INTO task_deps (task_id, depends_on_id) VALUES (?, ?)",
            [(task_id, d) for d in deps],
        )
        added = sorted(set(deps) - set(current.depends_on))
        await record(
            tx,
            actor,
            "task",
            task_id,
            "task.dependencies_changed",
            {"added": added, "removed": sorted(set(current.depends_on) - set(deps))},
        )
        for dep in added:
            if dep_states[dep] in (S.FAILED, S.CANCELLED):
                await _dependency_failed_event(tx, task_id, dep, dep_states[dep])
        deps_done = all(s is S.SUCCEEDED for s in dep_states.values())
        if state is S.READY and not deps_done:
            await _apply(tx, row, Action.DEPS_ADDED, actor)
        elif state is S.PENDING and deps_done:
            await _apply(tx, row, Action.DEPS_SATISFIED, actor)
    if (
        "budget_usd" in changed
        and state is S.BLOCKED
        and row["block_reason"] == BUDGET_EXHAUSTED
        and await task_cost.exhausted_reason(tx.conn, await _row(tx.conn, task_id))
        is None
    ):
        # Raising (or clearing) a spent budget is how a manager continues (3.4, 3.6).
        await _apply(tx, row, Action.UNBLOCK, actor, set_={"block_reason": None})
    # A version that did not move would let a stale editor through, so it only grows.
    version = ts(max(tx.now, datetime.fromisoformat(row["updated_at"]) + _TICK))
    await tx.conn.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?", (version, task_id)
    )
    await record(
        tx,
        actor,
        "task",
        task_id,
        "task.edited",
        {
            "from_version": row["updated_at"],
            "to_version": version,
            "changes": {
                f: _diff(f, getattr(current, f), v) for f, v in changed.items()
            },
        },
    )
    return await get_task(tx.conn, task_id)


async def checked_changes(
    conn: aiosqlite.Connection, row: aiosqlite.Row, current: Task, given: dict[str, Any]
) -> dict[str, Any]:
    """The values of `given` that differ from `current`, after
    the creation rules ran on the merged task. Raises before anything is written;
    shared by `edit_task` and a retry's overrides."""
    new = _normalized_edit(row, given)
    if new.get("harness_account") is not None:  # a name is stored as its id
        new["harness_account"] = await _account_id(conn, new["harness_account"])
    changed = {f: v for f, v in new.items() if v != getattr(current, f)}
    if not changed:
        return changed
    if why := check_harness_providers(
        new.get("harness_model", current.harness_model),
        new.get("account_providers", current.account_providers),
    ):
        raise ValidationFailedError(
            f"invalid values: harness_model: {why}",
            details=[f"harness_model: {why}"],
        )
    if changed.keys() & {"deliverable", "required_capabilities"}:
        await deliverable.check_creatable(
            conn,
            row["project"],
            new.get("required_capabilities", current.required_capabilities),
            new.get("deliverable", current.deliverable),
        )
    if changed.keys() & set(_ACCOUNT_PIN_RULES):
        await _resolve_harness_account(
            conn,
            new.get("harness_account", current.harness_account),
            account_providers=new.get("account_providers", current.account_providers),
            harness_model=new.get("harness_model", current.harness_model),
            executor_model=new.get("executor_model", current.executor_model),
        )
    return changed


def column_sets(changed: dict[str, Any]) -> dict[str, Any]:
    """The `tasks` columns that store the scalar fields of `changed`."""
    sets: dict[str, Any] = {
        f: json.dumps(v) if f in _JSON_FIELDS else v
        for f, v in changed.items()
        if f not in ("knowledge", "depends_on", "account_providers", *_PIN_FIELDS)
    }
    if "difficulty" in sets:
        sets["difficulty_source"] = DifficultySource.DECLARED
    if "account_providers" in changed:
        sets["account_providers"] = _providers_json(changed["account_providers"])
    for f in _PIN_FIELDS:
        if f in changed:
            sets[f] = _pin_json(changed[f])
    return sets


def _normalized_edit(row: aiosqlite.Row, given: dict[str, Any]) -> dict[str, Any]:
    """The values an edit would store, checked with the creation rules: a clear
    becomes the empty value, lists are de-duplicated as at creation."""
    errors: list[str] = []
    values: dict[str, Any] = {}
    for field, value in given.items():
        if value is None:
            if field in (
                "account_providers",
                "harness_account",
                "budget_usd",
                *_PIN_FIELDS,
            ):
                values[field] = None
            elif field not in CLEARABLE:
                errors.append(f"{field}: cannot be cleared")
                continue
            else:
                values[field] = "" if field == "spec" else []
        else:
            values[field] = value
    if "required_capabilities" in values:
        values["required_capabilities"] = sorted(set(values["required_capabilities"]))
    if "knowledge" in values:
        values["knowledge"] = list(dict.fromkeys(values["knowledge"]))
    if "depends_on" in values:
        values["depends_on"] = sorted(set(values["depends_on"]))
    if "account_providers" in values:
        # an empty list clears to "unrestricted", like the other list fields
        values["account_providers"] = (
            sorted(set(values["account_providers"]))
            if values["account_providers"]
            else None
        )
    try:
        TaskCreate.model_validate(
            {"project": row["project"], "kind": row["kind"], "title": row["title"]}
            | values
        )
    except ValidationError as exc:
        errors += [
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
            for e in exc.errors(include_input=False, include_url=False)
            if e["loc"] and e["loc"][0] in values
        ]
    if errors:
        raise ValidationFailedError(
            "invalid values: " + "; ".join(errors), details=errors
        )
    return values


def _providers_json(providers: list[str] | None) -> str | None:
    return None if providers is None else json.dumps(providers)


# The fields a harness account pin is checked against (task models spec 3.4).
_ACCOUNT_PIN_RULES = (
    "harness_account",
    "account_providers",
    "harness_model",
    "executor_model",
)


async def _account_id(conn: aiosqlite.Connection, ref: str) -> str:
    """The id of the account `ref` names (its id or its name); 422 when none does."""
    return (await _named_account(conn, ref))["id"]


async def _named_account(conn: aiosqlite.Connection, ref: str) -> aiosqlite.Row:
    """The account `ref` names (its id first, else its name); 422 when none does."""
    async with conn.execute(
        "SELECT id, name, provider FROM accounts WHERE id = ? OR name = ?"
        " ORDER BY id = ? DESC LIMIT 1",
        (ref, ref, ref),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        why = f"no account {ref!r}"
        raise ValidationFailedError(
            f"invalid values: harness_account: {why}",
            details=[f"harness_account: {why}"],
        )
    return row


async def _resolve_harness_account(
    conn: aiosqlite.Connection,
    ref: str | None,
    *,
    account_providers: list[str] | None,
    harness_model: HarnessModel | None,
    executor_model: ExecutorModel | None,
) -> str | None:
    """The id of the account a harness account pin names, checked against the
    task's other fields (task models spec 3.4); None when the task is not pinned.
    The account must be an engine account its `account_providers` and harness pin
    allow; a local (Strata) harness orchestrates and may pin Cursor (`agent`) or
    OpenCode (`opencode`) as executor (local harness / task models specs)."""
    if ref is None:
        return None
    acc = await _named_account(conn, ref)
    account_id, name, provider = acc["id"], acc["name"], acc["provider"]
    why = None
    if provider not in ENGINE_PROVIDERS:
        why = f"account {name} ({provider}) cannot run a harness"
    elif account_providers is not None and provider not in account_providers:
        why = f"account {name} is {provider}, excluded by account_providers"
    elif harness_model is not None and harness_model.provider != provider:
        why = (
            f"account {name} is {provider}; harness_model engine"
            f" {harness_model.engine} needs {harness_model.provider}"
        )
    elif (
        provider == LOCAL_PROVIDER
        and executor_model is not None
        and executor_model.executor not in ("agent", "opencode")
    ):
        why = (
            f"account {name} is the local harness, which delegates to the Cursor"
            f" agent or OpenCode; executor_model {executor_model.executor} cannot run"
        )
    if why:
        raise ValidationFailedError(
            f"invalid values: harness_account: {why}",
            details=[f"harness_account: {why}"],
        )
    return account_id


def _pin_json(pin: HarnessModel | ExecutorModel | None) -> str | None:
    return None if pin is None else pin.model_dump_json()


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _diff(field: str, old: Any, new: Any) -> dict[str, Any]:
    """What the audit keeps of one change. Spec and acceptance text never enter the
    event log (it is streamed and shown): only sizes and digests do."""
    if field in _PIN_FIELDS:
        return {
            "from": old and old.model_dump(),
            "to": new and new.model_dump(),
        }
    if field in (
        "title",
        "priority",
        "difficulty",
        "deliverable",
        "account_providers",
        "harness_account",
        "budget_usd",
    ):
        return {"from": old, "to": new}
    if field in ("required_capabilities", "knowledge", "depends_on"):
        return {
            "added": sorted(set(new) - set(old)),
            "removed": sorted(set(old) - set(new)),
        }
    texts = (old, new) if field == "spec" else (json.dumps(old), json.dumps(new))
    sizes = (len(old), len(new))
    return {
        "from_size": sizes[0],
        "to_size": sizes[1],
        "from_sha256": _digest(texts[0]),
        "to_sha256": _digest(texts[1]),
    }


async def _check_dependencies(
    conn: aiosqlite.Connection, task_id: str, deps: list[str]
) -> dict[str, TaskState]:
    """The rules for a task's dependencies (task dependencies 3.1); returns the
    state of each. Raises before anything is written."""
    if len(deps) > MAX_DEPENDENCIES:
        raise ValidationFailedError(f"at most {MAX_DEPENDENCIES} dependencies")
    if task_id in deps:
        raise ValidationFailedError(
            "a task cannot depend on itself",
            code="dependency_cycle",
            details=[task_id, task_id],
        )
    dep_states = await _states(conn, deps)
    if missing := sorted(set(deps) - dep_states.keys()):
        raise NotFoundError(
            f"dependency not found: {', '.join(missing)}",
            details=[f"depends_on: {m}" for m in missing],
        )
    if cycle := await _path_back(conn, deps, task_id):
        raise ValidationFailedError(
            "these dependencies would close a cycle: " + " -> ".join(cycle),
            code="dependency_cycle",
            details=cycle,
        )
    return dep_states


async def _check_new_pins(
    conn: aiosqlite.Connection, project: str, added: list[str]
) -> None:
    """Pages an edit adds must exist and belong to the task's project or `global`."""
    if not added:
        return
    await _check_pages_exist(conn, added)
    marks = ",".join("?" * len(added))
    async with conn.execute(
        f"SELECT id FROM knowledge_pages WHERE id IN ({marks})"  # noqa: S608
        " AND project NOT IN (?, 'global')",
        [*added, project],
    ) as cur:
        foreign = sorted(r[0] for r in await cur.fetchall())
    if foreign:
        raise ValidationFailedError(
            f"pages of another project cannot be pinned: {', '.join(foreign)}",
            details=[f"knowledge: {p}: not in {project} or global" for p in foreign],
        )


async def _snapshot_attempt(tx: Tx, task: Task, agent_id: str) -> None:
    """Keep the definition this attempt was handed (task edit 3.5)."""
    definition = {
        f: getattr(task, f)
        for f in (
            "kind",
            "project",
            "title",
            "spec",
            "acceptance",
            "priority",
            "required_capabilities",
            "knowledge",
            "depends_on",
            "difficulty",
            "difficulty_source",
            "deliverable",
            "harness_account",
        )
    }
    for f in _PIN_FIELDS:
        pin = getattr(task, f)
        definition[f] = pin and pin.model_dump()
    # What the attempt was told about continuing (history is never rewritten).
    definition["continuation"] = (
        task.continuation.model_dump(mode="json") if task.continuation else None
    )
    await tx.conn.execute(
        "INSERT INTO task_attempts (task_id, attempt, agent_id, claimed_at, definition)"
        " VALUES (?, ?, ?, ?, ?)",
        (task.id, task.attempt, agent_id, ts(tx.now), json.dumps(definition)),
    )


async def attempt_definitions(
    conn: aiosqlite.Connection, task_id: str
) -> list[TaskAttempt]:
    """The definition of every attempt, oldest first."""
    await _row(conn, task_id)
    async with conn.execute(
        "SELECT * FROM task_attempts WHERE task_id = ? ORDER BY id", (task_id,)
    ) as cur:
        return [
            TaskAttempt(
                attempt=r["attempt"],
                agent_id=r["agent_id"],
                claimed_at=r["claimed_at"],
                definition=json.loads(r["definition"]),
                outcome=r["outcome"],
                outcome_reason=r["outcome_reason"],
                steer_unknown=r["steer_unknown"],
            )
            for r in await cur.fetchall()
        ]


async def attempt_summaries(
    conn: aiosqlite.Connection, task_id: str
) -> list[TaskAttemptSummary]:
    """What the attempt strip needs, oldest first, without the stored definitions."""
    await _row(conn, task_id)
    async with conn.execute(
        "SELECT attempt, steer_unknown FROM task_attempts"
        " WHERE task_id = ? ORDER BY id",
        (task_id,),
    ) as cur:
        return [
            TaskAttemptSummary(attempt=r["attempt"], steer_unknown=r["steer_unknown"])
            for r in await cur.fetchall()
        ]


async def _path_back(
    conn: aiosqlite.Connection, starts: list[str], target: str
) -> list[str] | None:
    """A dependency path from one of `starts` back to `target`, if any, as
    [target, start, ..., target]: adding target -> start would close it."""
    parent: dict[str, str | None] = dict.fromkeys(starts)
    frontier = list(starts)
    while frontier:
        marks = ",".join("?" * len(frontier))
        sql = f"SELECT task_id, depends_on_id FROM task_deps WHERE task_id IN ({marks})"  # noqa: S608
        async with conn.execute(sql, frontier) as cur:
            edges = await cur.fetchall()
        frontier = []
        for node, dep in edges:
            if dep in parent:
                continue
            parent[dep] = node
            if dep == target:
                path, at = [], dep
                while at is not None:
                    path.append(at)
                    at = parent[at]
                return [target, *reversed(path)]
            frontier.append(dep)
    return None


async def _check_pages_exist(conn: aiosqlite.Connection, pages: list[str]) -> None:
    found = set((await _states_of(conn, "knowledge_pages", pages)).keys())
    if missing := sorted(set(pages) - found):
        raise NotFoundError(
            f"knowledge page not found: {', '.join(missing)}",
            details=[f"knowledge: {m}" for m in missing],
        )
