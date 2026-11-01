"""Independent code review gate (docs/specs/settings.spec.md 3.5).

On completion of a task that delivers code, Argus can require a child `code_review`
task whose model is not an author of the parent. The gate is proof-based: the child's
own model_executions must not include an author model (or provider).
"""

from __future__ import annotations

import json
import re
from typing import Any

import aiosqlite
from argus_core.continuation import ArtifactKind
from argus_core.decisions import Audience, DecisionKind
from argus_core.settings import IndependentReviewSetting, ReviewMode
from argus_core.tasks import (
    FORGE_DELIVERABLES,
    HARNESS_PROVIDERS,
    Action,
    Deliverable,
    ExecutorModel,
    HarnessModel,
    Review,
    TaskCreate,
    TaskResult,
    TaskState,
)

from argus_api import model_policy
from argus_api.db import Tx, ts
from argus_api.decisions import store as decisions
from argus_api.events import record
from argus_api.tasks import continuation as cont
from argus_api.tasks import delivery

NO_INDEPENDENT_REVIEWER = "no_independent_reviewer"
NO_REVIEW_HEAD = "no_review_head"
CODE_REVIEW_KIND = "code_review"
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_REVIEW_OPTIONS = ("accept", "reject")

# Engine name → account provider for harness / executor selection.
_ENGINE_PROVIDER = {
    **HARNESS_PROVIDERS,
    "agent": "cursor",
    "opencode": "openai",
}


def _delivery_refs(
    row: aiosqlite.Row, result: TaskResult
) -> tuple[str | None, str | None]:
    """(pr_url_or_number, head_sha) when the task delivers code, else (None, None)."""
    have = cont.load(row)
    pr_url: str | None = None
    head_sha: str | None = None
    for a in have.artifacts:
        if a.kind is ArtifactKind.PR:
            pr_url = a.url or a.ref
            head_sha = a.head_sha or head_sha
        elif a.kind is ArtifactKind.COMMIT and a.head_sha:
            head_sha = head_sha or a.head_sha
        elif a.kind is ArtifactKind.COMMIT and _SHA.fullmatch(a.ref):
            head_sha = head_sha or a.ref
    # Result / progress may name a head without a structured artifact yet.
    data = result.data or {}
    for key in ("head_sha", "sha", "commit"):
        val = data.get(key)
        if isinstance(val, str) and _SHA.fullmatch(val):
            head_sha = head_sha or val
    if isinstance(data.get("pr"), str):
        pr_url = pr_url or data["pr"]
    progress = row["progress_data"]
    if progress:
        try:
            pdata = json.loads(progress)
        except (TypeError, json.JSONDecodeError):
            pdata = {}
        if isinstance(pdata, dict):
            for key in ("sha", "head_sha"):
                val = pdata.get(key)
                if isinstance(val, str) and _SHA.fullmatch(val):
                    head_sha = head_sha or val
            if (
                isinstance(pdata.get("url"), str)
                and row["progress_kind"] == "pr_opened"
            ):
                pr_url = pr_url or pdata["url"]
    if pr_url or head_sha:
        return pr_url, head_sha
    # Fallback: the delivery extractor's PR reference (text/artifacts).
    return None, None


async def has_code_delivery(
    conn: aiosqlite.Connection, row: aiosqlite.Row, result: TaskResult
) -> tuple[str | None, str | None] | None:
    """None when the task delivers no code; else (pr, head_sha)."""
    pr, sha = _delivery_refs(row, result)
    if pr or sha:
        return pr, sha
    # Try the full delivery extractor (needs a Task-shaped read).
    from argus_api.tasks import service as tasks_svc

    task = await tasks_svc.get_task(conn, row["id"])
    # Attach the just-completed result so extract sees it (row may lag).
    task = task.model_copy(update={"result": result})
    ref = await delivery.reference(conn, task)
    if ref is None:
        return None
    return ref.url, sha


async def authors(
    conn: aiosqlite.Connection, task_id: str
) -> tuple[set[str], set[str]]:
    """(models, providers) recorded on the task across all attempts.

    Both `observed_model` and `configured_model` count: an unknown observation must
    not make two runs look different (settings.spec.md 3.5).
    """
    models: set[str] = set()
    providers: set[str] = set()
    async with conn.execute(
        "SELECT observed_model, configured_model, provider, engine, executor"
        " FROM model_executions WHERE task_id = ?",
        (task_id,),
    ) as cur:
        for r in await cur.fetchall():
            for col in ("observed_model", "configured_model"):
                if r[col]:
                    models.add(r[col])
            if r["provider"]:
                providers.add(r["provider"])
            else:
                for tool in (r["engine"], r["executor"]):
                    if tool and tool in _ENGINE_PROVIDER:
                        providers.add(_ENGINE_PROVIDER[tool])
    return models, providers


async def usable_providers(conn: aiosqlite.Connection, now_iso: str) -> set[str]:
    """Account providers that can serve a lease right now."""
    async with conn.execute(
        "SELECT provider, state, exhausted_until FROM accounts"
        " WHERE state IN ('available', 'exhausted')"
    ) as cur:
        rows = await cur.fetchall()
    out: set[str] = set()
    for r in rows:
        if r["state"] == "available" or (
            r["exhausted_until"] and r["exhausted_until"] <= now_iso
        ):
            out.add(r["provider"])
    return out


def pick_reviewer(
    policy: model_policy.Policy,
    *,
    mode: ReviewMode,
    author_models: set[str],
    author_providers: set[str],
    usable: set[str],
    exclude_models: set[str] | None = None,
) -> tuple[HarnessModel | None, ExecutorModel | None]:
    """First non-author model in the effective chains with a usable account.

    Prefers a harness pin when the engine is claude/codex; also returns an executor
    pin for the same (or agent) engine so the child is fully pinned.
    """
    excluded = set(author_models) | (exclude_models or set())
    harness_pin: HarnessModel | None = None
    executor_pin: ExecutorModel | None = None
    for scope in ("harness", "implementation", "hard_harness", "hard_implementation"):
        for engine, chain in (policy.get(scope) or {}).items():
            provider = _ENGINE_PROVIDER.get(engine)
            if provider is None or provider not in usable:
                continue
            if mode == "different_provider" and provider in author_providers:
                continue
            for model in chain:
                if mode == "different_model" and model in excluded:
                    continue
                if engine in ("claude", "codex") and harness_pin is None:
                    harness_pin = HarnessModel(engine=engine, model=model)  # type: ignore[arg-type]
                if engine in ("claude", "codex", "agent") and executor_pin is None:
                    executor_pin = ExecutorModel(executor=engine, model=model)  # type: ignore[arg-type]
                if harness_pin is not None or executor_pin is not None:
                    # Prefer both pins when the engine can also execute.
                    if (
                        harness_pin is not None
                        and executor_pin is None
                        and engine in ("claude", "codex", "agent")
                    ):
                        executor_pin = ExecutorModel(
                            executor=engine,
                            model=model,  # type: ignore[arg-type]
                        )
                    return harness_pin, executor_pin
    return harness_pin, executor_pin


async def review_rounds(conn: aiosqlite.Connection, parent_id: str) -> int:
    """How many completed `code_review` children ended with request_changes."""
    async with conn.execute(
        "SELECT result FROM tasks WHERE parent_id = ? AND kind = ? AND state = ?",
        (parent_id, CODE_REVIEW_KIND, TaskState.SUCCEEDED),
    ) as cur:
        rows = await cur.fetchall()
    n = 0
    for (raw,) in rows:
        if not raw:
            continue
        try:
            data = json.loads(raw).get("data") or {}
        except json.JSONDecodeError:
            continue
        if data.get("verdict") == "request_changes":
            n += 1
    return n


def _findings_text(findings: list[Any]) -> str:
    lines = []
    for f in findings:
        if isinstance(f, str):
            lines.append(f"- {f}")
        elif isinstance(f, dict):
            lines.append(f"- {json.dumps(f, ensure_ascii=False)}")
        else:
            lines.append(f"- {f}")
    return "\n".join(lines) if lines else "- (no details)"


async def _block(tx: Tx, parent: aiosqlite.Row, reason: str, actor: str) -> None:
    """Leave the parent in_review with a visible block_reason (no silent skip)."""
    await tx.conn.execute(
        "UPDATE tasks SET block_reason = ?, updated_at = ? WHERE id = ?",
        (reason, ts(tx.now), parent["id"]),
    )
    await record(
        tx,
        actor,
        "task",
        parent["id"],
        "review.independent_blocked",
        {"reason": reason},
    )


async def spawn_reviewer(
    tx: Tx,
    parent: aiosqlite.Row,
    *,
    pr: str | None,
    head_sha: str | None,
    setting: IndependentReviewSetting,
    policy: model_policy.Policy,
    actor: str,
    exclude_models: set[str] | None = None,
) -> str | None:
    """Create a code_review child, or set block_reason when none is eligible.

    Returns the child task id, or None when blocked for want of a reviewer.
    """
    from argus_api.tasks import service as tasks_svc

    if head_sha is None:
        # The reviewer's input must name the commit it reviews; without one the
        # gate blocks visibly rather than inventing a head.
        await _block(tx, parent, NO_REVIEW_HEAD, actor)
        return None
    author_models, author_providers = await authors(tx.conn, parent["id"])
    usable = await usable_providers(tx.conn, ts(tx.now))
    harness, executor = pick_reviewer(
        policy,
        mode=setting.mode,
        author_models=author_models,
        author_providers=author_providers,
        usable=usable,
        exclude_models=exclude_models,
    )
    if harness is None and executor is None:
        await _block(tx, parent, NO_INDEPENDENT_REVIEWER, actor)
        return None
    # Clear a prior gate block if we found a reviewer this time.
    if parent["block_reason"] in (NO_INDEPENDENT_REVIEWER, NO_REVIEW_HEAD):
        await tx.conn.execute(
            "UPDATE tasks SET block_reason = NULL WHERE id = ?", (parent["id"],)
        )
    title = f"Independent review of {parent['title']!r}"
    sha = head_sha
    body = TaskCreate(
        title=title[:200],
        spec=(
            f"Review the code delivered by task `{parent['id']}`"
            f" (head `{sha}`).\n\n"
            "Return a structured result with `verdict` (`approve` or"
            " `request_changes`), `findings`, and the `head_sha` you reviewed."
            " Do not modify the repository."
        ),
        kind=CODE_REVIEW_KIND,
        project=parent["project"],
        parent_id=parent["id"],
        input={"task_id": parent["id"], "pr": pr, "head_sha": sha},
        review=Review.NONE,
        harness_model=harness,
        executor_model=executor,
        required_capabilities=[],
    )
    child = await tasks_svc.create_task(tx, body, actor)
    await record(
        tx,
        actor,
        "task",
        parent["id"],
        "review.independent_requested",
        {
            "child_id": child.id,
            "harness_model": harness.model_dump() if harness else None,
            "executor_model": executor.model_dump() if executor else None,
            "mode": setting.mode,
        },
    )
    return child.id


async def on_parent_complete(
    tx: Tx,
    row: aiosqlite.Row,
    result: TaskResult,
    *,
    setting: IndependentReviewSetting,
    policy: model_policy.Policy,
    actor: str,
) -> bool:
    """If the gate applies, move the parent to in_review and spawn a reviewer.

    Returns True when the independent-review path handled completion (caller must
    not run the normal COMPLETE transition). Returns False when the gate is off
    or the task delivers no code (and a skip event may have been recorded).
    """
    from argus_api.tasks import service as tasks_svc

    if setting.mode == "off" or row["kind"] == CODE_REVIEW_KIND:
        return False
    refs = await has_code_delivery(tx.conn, row, result)
    if refs is None:
        await record(
            tx,
            actor,
            "task",
            row["id"],
            "review.independent_skipped",
            {"reason": "no_delivery"},
        )
        return False
    pr, head_sha = refs
    # Persist the result, then force in_review without opening a human decision yet.
    await tasks_svc._apply(
        tx,
        row,
        Action.COMPLETE,
        actor,
        set_={"result": result.model_dump_json(exclude_none=True)},
        review=Review.ORCHESTRATOR,  # force in_review even when review=none
        open_review_decision=False,
    )
    # Re-read after transition.
    parent = await tasks_svc._row(tx.conn, row["id"])
    await spawn_reviewer(
        tx,
        parent,
        pr=pr,
        head_sha=head_sha,
        setting=setting,
        policy=policy,
        actor=actor,
    )
    return True


async def child_models_clean(
    conn: aiosqlite.Connection,
    child_id: str,
    *,
    mode: ReviewMode,
    author_models: set[str],
    author_providers: set[str],
) -> bool:
    """True when the child's executions prove it used no author model/provider."""
    child_models, child_providers = await authors(conn, child_id)
    if not child_models and not child_providers:
        # No execution recorded: cannot prove independence when authors exist.
        return not author_models and not author_providers
    if mode == "different_provider":
        return child_providers.isdisjoint(author_providers)
    return child_models.isdisjoint(author_models)


async def on_review_child_complete(
    tx: Tx,
    child: aiosqlite.Row,
    result: TaskResult,
    *,
    setting: IndependentReviewSetting,
    policy: model_policy.Policy,
    actor: str,
) -> None:
    """Apply the child's verdict to the parent (proof-checked)."""
    from argus_api.tasks import service as tasks_svc

    parent_id = child["parent_id"]
    if not parent_id:
        return
    parent = await tasks_svc._row(tx.conn, parent_id)
    if parent["state"] != TaskState.IN_REVIEW:
        return
    data = result.data or {}
    verdict = data.get("verdict")
    findings = data.get("findings") or []
    head_sha = data.get("head_sha")
    author_models, author_providers = await authors(tx.conn, parent_id)
    if not await child_models_clean(
        tx.conn,
        child["id"],
        mode=setting.mode,
        author_models=author_models,
        author_providers=author_providers,
    ):
        await record(
            tx,
            actor,
            "task",
            parent_id,
            "review.independent_void",
            {"reason": "same_model", "child_id": child["id"]},
        )
        # Prefer a different model next time.
        child_input = json.loads(child["input"] or "{}")
        child_models, _ = await authors(tx.conn, child["id"])
        await spawn_reviewer(
            tx,
            parent,
            pr=child_input.get("pr"),
            head_sha=child_input.get("head_sha"),
            setting=setting,
            policy=policy,
            actor=actor,
            exclude_models=author_models | child_models,
        )
        return

    if verdict == "approve":
        # head_sha must match what the parent currently delivers, when known.
        parent_result = (
            TaskResult.model_validate_json(parent["result"])
            if parent["result"]
            else TaskResult(summary=".")
        )
        parent_refs = _delivery_refs(parent, parent_result)
        current_sha = parent_refs[1]
        if current_sha and head_sha and current_sha != head_sha:
            # Stale review of an older head: spawn again.
            await record(
                tx,
                actor,
                "task",
                parent_id,
                "review.independent_void",
                {
                    "reason": "head_mismatch",
                    "child_id": child["id"],
                    "reviewed": head_sha,
                    "current": current_sha,
                },
            )
            await spawn_reviewer(
                tx,
                parent,
                pr=parent_refs[0],
                head_sha=current_sha,
                setting=setting,
                policy=policy,
                actor=actor,
            )
            return
        if parent["block_reason"] == NO_INDEPENDENT_REVIEWER:
            await tx.conn.execute(
                "UPDATE tasks SET block_reason = NULL WHERE id = ?", (parent_id,)
            )
        review = Review(parent["review"])
        have = cont.load(parent)
        open_items = (
            [i.id for i in have.checklist if i.status.value != "done"]
            if have.checklist
            else []
        )
        await record(
            tx,
            actor,
            "task",
            parent_id,
            "review.independent_passed",
            {"child_id": child["id"], "verdict": "approve"},
        )
        # A PR that must still turn green or merge is closed by a decision, which
        # reads the forge (task deliverable spec 3.5), never by this approval.
        forge_gated = Deliverable(parent["deliverable"]) in FORGE_DELIVERABLES
        if review is not Review.NONE or open_items or forge_gated:
            await decisions.open_decision(
                tx,
                subject_type="task",
                subject_id=parent_id,
                kind=DecisionKind.REVIEW,
                question=(
                    f"Review the result of {parent['title']!r}"
                    f" (attempt {parent['attempt']})."
                ),
                options=list(_REVIEW_OPTIONS),
                audience=Audience.HUMAN
                if review is Review.HUMAN
                else Audience.ORCHESTRATOR,
                requested_by=actor,
            )
        else:
            await tasks_svc._apply(
                tx, parent, Action.ACCEPT, actor, data={"notes": "independent review"}
            )
        return

    if verdict == "request_changes":
        # Count prior request_changes rounds plus this one.
        rounds = await review_rounds(tx.conn, parent_id)
        if rounds >= setting.max_rounds:
            await decisions.open_decision(
                tx,
                subject_type="task",
                subject_id=parent_id,
                kind=DecisionKind.REVIEW,
                question=(
                    f"Independent review exhausted {setting.max_rounds} rounds for"
                    f" {parent['title']!r}. Accept or reject?"
                ),
                options=list(_REVIEW_OPTIONS),
                audience=Audience.HUMAN,
                requested_by=actor,
            )
            await record(
                tx,
                actor,
                "task",
                parent_id,
                "review.independent_escalated",
                {"child_id": child["id"], "rounds": rounds},
            )
            return
        notes = _findings_text(findings if isinstance(findings, list) else [findings])
        spec = parent["spec"] + (
            f"\n\n## Review findings (attempt {parent['attempt']})\n\n{notes}"
        )
        await tasks_svc._apply(
            tx,
            parent,
            Action.REJECT,
            actor,
            set_={"spec": spec, "block_reason": None},
            data={"notes": notes, "independent_review": True},
            open_review_decision=False,
        )
        await record(
            tx,
            actor,
            "task",
            parent_id,
            "review.independent_changes_requested",
            {"child_id": child["id"], "round": rounds},
        )
        return
