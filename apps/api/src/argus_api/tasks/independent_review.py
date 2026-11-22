"""Independent code review gate (docs/specs/settings.spec.md 3.5).

On completion of a task that delivers code, Argus can require a child `code_review`
task whose model is not an author of the parent. The gate is proof-based: the child's
own model_executions must not include an author model (or provider).
"""

from __future__ import annotations

import json
import re
from collections.abc import Set as AbstractSet
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
from argus_core.accounts import LeaseRole
from argus_core.continuation import ArtifactKind
from argus_core.decisions import (
    GATE_DISABLED_GATE,
    NO_REVIEWER_GATE,
    REVIEW_FAILED_GATE,
    REVIEW_FAILED_OPTIONS,
    REVIEW_OPTIONS,
    REVIEWER_FAILED_GATE,
    ROUNDS_EXHAUSTED_GATE,
    Audience,
    Decision,
    DecisionKind,
)
from argus_core.delivery import FailedReviewer
from argus_core.execution_chains import Alternative, Chain, PairedAlternative, Selector
from argus_core.quota import failure_class, sanitize_notice
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
from argus_api.db import Database, Tx, ts
from argus_api.decisions import store as decisions
from argus_api.errors import ConflictError, ValidationFailedError
from argus_api.events import record
from argus_api.tasks import continuation as cont
from argus_api.tasks import deliverable, delivery
from argus_api.tasks import execution_routes as routes

NO_INDEPENDENT_REVIEWER = "no_independent_reviewer"
NO_REVIEW_HEAD = "no_review_head"
REVIEW_FAILED = "review_failed"
# An approved parent that settles on its own once the forge shows the deliverable met.
AWAITING_FORGE = "awaiting_forge"
_GATE_BLOCKS = (NO_INDEPENDENT_REVIEWER, NO_REVIEW_HEAD, REVIEW_FAILED, AWAITING_FORGE)
FORGE_FRESH_S = 120  # how old the forge read may be when the write uses it
PASSED_EVENT = "review.independent_passed"
WAITING_EVENT = "review.independent_awaiting_forge"
_GATE_WHY = {
    NO_REVIEWER_GATE: "no independent reviewer could be selected",
    REVIEWER_FAILED_GATE: "the reviewer failed to deliver a verdict",
    GATE_DISABLED_GATE: "independent review was switched off",
}
CHILD_FAILED_EVENT = "review.independent_child_failed"
RETRY_EVENT = "review.independent_retry"
# Where the global model policy lives; the app sets it at startup (main.py), as the
# child's end can happen in a path that carries no settings (reaper, cancel).
model_policy_path = Path("data/model-policy.json")
CODE_REVIEW_KIND = "code_review"
REVIEW_KEYS = ("verdict", "findings", "head_sha", "fixed_head_sha", "tests")
FIX_PUSHED_EVENT = "review.independent_fix_pushed"
FIX_VERDICT = "fixed"
# Verdicts that spend one review round (`review.independent.max_rounds`).
_ROUND_VERDICTS = ("request_changes", FIX_VERDICT)


def normalise_result(kind: str, result: TaskResult) -> tuple[TaskResult, bool]:
    """Move a code_review's top-level verdict keys into `data` when `data` is absent.

    Only the three review keys move, nothing is invented: a missing one is left for the
    kind's schema to name. Other kinds drop the envelope untouched. Returns whether it
    moved anything."""
    if result.envelope is None:
        return result, False
    if kind != CODE_REVIEW_KIND or result.data is not None:
        return result.model_copy(update={"envelope": None}), False
    extra = sorted(set(result.envelope) - set(REVIEW_KEYS))
    if extra:
        raise ValidationFailedError(
            "result does not match its kind's schema",
            details=[
                f"top level: unexpected key {k}; the review envelope takes only"
                " verdict, findings, head_sha, fixed_head_sha and tests"
                for k in extra
            ],
        )
    return result.model_copy(
        update={"data": dict(result.envelope), "envelope": None}
    ), True


_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def _same_pr(a: str | None, b: str | None) -> bool:
    """True when both name the same PR (case-insensitive URL, or bare number matching
    the URL's trailing PR number). Different PR numbers never match."""
    if not a or not b:
        return False
    left = a.strip().lower().rstrip("/")
    right = b.strip().lower().rstrip("/")
    if left == right:
        return True
    lnum = left.lstrip("#")
    rnum = right.lstrip("#")
    if lnum.isdigit() and "/" in right:
        return right.rsplit("/", 1)[-1] == lnum
    if rnum.isdigit() and "/" in left:
        return left.rsplit("/", 1)[-1] == rnum
    return False


def _same_sha(a: str | None, b: str | None) -> bool:
    """Exact lowercase SHA equality; never approves a different or truncated SHA."""
    if not a or not b:
        return False
    return a.strip().lower() == b.strip().lower()


# Engine name → account provider for harness / executor selection.
_ENGINE_PROVIDER = {
    **HARNESS_PROVIDERS,
    "agent": "cursor",
    "opencode": "openai",
}


def _delivery_refs(
    row: aiosqlite.Row, result: TaskResult
) -> tuple[str | None, str | None]:
    """(pr_url_or_number, head_sha) when the task delivers code, else (None, None).

    Prefers the current completion's `result.data` and current attempt's progress
    over older continuation artifacts, picks the newest PR artifact by `observed_at`,
    and never carries an older PR's `head_sha` onto a replacement PR."""
    have = cont.load(row)
    art_pr: str | None = None
    art_sha: str | None = None
    prs = [a for a in have.artifacts if a.kind is ArtifactKind.PR]
    if prs:
        newest_pr = max(enumerate(prs), key=lambda p: (p[1].observed_at, p[0]))[1]
        art_pr = newest_pr.url or newest_pr.ref
        art_sha = newest_pr.head_sha
    commit_sha: str | None = None
    commits = [a for a in have.artifacts if a.kind is ArtifactKind.COMMIT]
    for a in sorted(commits, key=lambda c: c.observed_at, reverse=True):
        if a.head_sha:
            commit_sha = a.head_sha
            break
        if _SHA.fullmatch(a.ref):
            commit_sha = a.ref
            break

    data = result.data or {}
    data_pr = data["pr"] if isinstance(data.get("pr"), str) else None
    data_sha: str | None = None
    for key in ("head_sha", "sha", "commit"):
        val = data.get(key)
        if isinstance(val, str) and _SHA.fullmatch(val):
            data_sha = val
            break

    prog_pr: str | None = None
    prog_sha: str | None = None
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
                    prog_sha = val
                    break
            if (
                isinstance(pdata.get("url"), str)
                and row["progress_kind"] == "pr_opened"
            ):
                prog_pr = pdata["url"]

    pr_url = data_pr or prog_pr or art_pr
    same_as_art_pr = pr_url is None or art_pr is None or _same_pr(pr_url, art_pr)
    head_sha = (
        data_sha
        or prog_sha
        or (art_sha if same_as_art_pr else None)
        or (commit_sha if same_as_art_pr else None)
    )
    if pr_url or head_sha:
        return pr_url, head_sha.lower() if head_sha else None
    # Fallback: the delivery extractor's PR reference (text/artifacts).
    return None, None


def _forge_head(pr: str | None, forge: deliverable.ForgeRead | None) -> str | None:
    """The forge's head for `pr` when the completion's forge read names that same PR.

    The forge reports the PR URL with its host and owner lowercased, while the worker
    may write them as it spelled them, so the comparison ignores case."""
    known = forge.delivery if forge is not None else None
    if known is None or known.head is None or not known.head.sha:
        return None
    if pr is not None and not _same_pr(pr, known.pr):
        return None
    return known.head.sha.lower()


async def _history_head(conn: aiosqlite.Connection, task_id: str) -> str | None:
    """The newest commit the task itself reported in its milestones (`ci_passed`,
    `branch`, ...), read from its progress history. The last resort before the gate
    blocks: the task's own record, not a guess, and it names a commit it pushed."""
    async with conn.execute(
        "SELECT data FROM events WHERE subject_type = 'task' AND subject_id = ?"
        " AND kind = 'task.progress' ORDER BY id DESC",
        (task_id,),
    ) as cur:
        async for (raw,) in cur:
            data = json.loads(raw) if raw else None
            milestone = data.get("data") if isinstance(data, dict) else None
            if not isinstance(milestone, dict):
                continue
            for key in ("sha", "head_sha"):
                val = milestone.get(key)
                if isinstance(val, str) and _FULL_SHA.fullmatch(val):
                    return val.lower()
    return None


async def has_code_delivery(
    conn: aiosqlite.Connection,
    row: aiosqlite.Row,
    result: TaskResult,
    forge: deliverable.ForgeRead | None = None,
) -> tuple[str | None, str | None] | None:
    """None when the task delivers no code; else (pr, head_sha).

    Prefers the live head the forge reports for the task's PR (`forge`, read before
    the write) over older stored heads so review rounds always inspect the live head;
    without one the reviewer has no commit to read and the gate blocks (`no_review_head`)."""
    pr, sha = _delivery_refs(row, result)
    if pr is None and sha is None:
        # Try the full delivery extractor (needs a Task-shaped read).
        from argus_api.tasks import service as tasks_svc

        task = await tasks_svc.get_task(conn, row["id"])
        # Attach the just-completed result so extract sees it (row may lag).
        task = task.model_copy(update={"result": result})
        ref = await delivery.reference(conn, task)
        if ref is None:
            return None
        pr = ref.url
    if (live_sha := _forge_head(pr, forge)) is not None:
        sha = live_sha
    if sha is None and pr is not None:
        sha = await _history_head(conn, row["id"])
    return pr, sha.lower() if sha else None


async def settles_alone(conn: aiosqlite.Connection, parent: aiosqlite.Row) -> bool:
    """True when an approved independent review is all the review this parent needs:
    `review=none`, acceptance off, and a deliverable (`pr_green`, `pr_merged`) that the
    forge, not the reviewer, must confirm (settings spec 3.5, deliverable spec 3.5)."""
    from argus_api.settings import service as settings_svc

    return (
        parent["state"] == TaskState.IN_REVIEW
        and Review(parent["review"]) is Review.NONE
        and Deliverable(parent["deliverable"]) in FORGE_DELIVERABLES
        and not await settings_svc.acceptance_enabled(conn, parent["project"])
    )


async def awaits_settlement(conn: aiosqlite.Connection, parent: aiosqlite.Row) -> bool:
    """`settles_alone` and nothing else holds the parent: no open review decision (an
    explicit request) and no active reviewer."""
    return (
        await settles_alone(conn, parent)
        and not await decisions.open_ids(
            conn, "task", parent["id"], (DecisionKind.REVIEW,)
        )
        and not await has_active_reviewer(conn, parent["id"])
    )


def forge_verdict(
    parent: aiosqlite.Row,
    forge: deliverable.ForgeRead | None,
    reviewed_head: str | None,
    now: datetime,
    *,
    reviewed_pr: str | None = None,
) -> tuple[str, str] | None:
    """None when the forge, read for this settlement, shows the deliverable met on
    the exact PR and head the reviewer approved; else (code, why). The code is
    `pr_moved` for a replacement PR, `head_moved` for a head that changed after the
    review, `forge_not_ready` otherwise. Never reads the reviewer's word as proof of
    forge state."""
    try:
        deliverable.check_accept(parent, forge)
    except ConflictError as exc:
        return "forge_not_ready", exc.details[0] if exc.details else exc.message
    seen = forge.delivery if forge is not None else None
    if seen is None or seen.head is None or not seen.head.sha:
        return "forge_not_ready", "the forge reported no head for the pull request"
    if now - seen.checked_at > timedelta(seconds=FORGE_FRESH_S):
        return "forge_not_ready", "the forge read is stale"
    if seen.stale:
        return "forge_not_ready", "the checks do not belong to the pull request head"
    if not reviewed_head:
        return "forge_not_ready", "the reviewed head is unknown"
    if reviewed_pr and not _same_pr(reviewed_pr, seen.pr):
        return "pr_moved", f"the pull request changed to {seen.pr}"
    if not _same_sha(seen.head.sha, reviewed_head):
        return "head_moved", f"the pull request head moved to {seen.head.sha[:12]}"
    return None


_APPROVAL_LIFECYCLE_EVENTS = (
    PASSED_EVENT,
    FIX_PUSHED_EVENT,
    "review.independent_void",
    "review.independent_changes_requested",
    "review.independent_requested",
    "review.independent_waived",
    CHILD_FAILED_EVENT,
)


async def _latest_approval_event(
    conn: aiosqlite.Connection, parent_id: str
) -> tuple[str | None, dict[str, Any]]:
    marks = ",".join("?" * len(_APPROVAL_LIFECYCLE_EVENTS))
    async with conn.execute(
        f"SELECT kind, data FROM events WHERE subject_type = 'task'"  # noqa: S608
        f" AND subject_id = ? AND kind IN ({marks}) ORDER BY id DESC LIMIT 1",
        (parent_id, *_APPROVAL_LIFECYCLE_EVENTS),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None, {}
    data = json.loads(row[1]) if row[1] else {}
    return row[0], data if isinstance(data, dict) else {}


async def reviewed_approval_of(
    conn: aiosqlite.Connection, parent_id: str
) -> tuple[str | None, str | None]:
    """(pr, head_sha) of the active independent approval, else (None, None).

    A newer void, change request, retry/request, waiver, or child failure supersedes
    earlier approvals."""
    kind, data = await _latest_approval_event(conn, parent_id)
    if kind != PASSED_EVENT:
        return None, None
    pr = data.get("pr")
    head = data.get("head_sha")
    return (
        pr if isinstance(pr, str) and pr else None,
        head.lower() if isinstance(head, str) and head else None,
    )


async def reviewed_head_of(conn: aiosqlite.Connection, parent_id: str) -> str | None:
    """The head of the newest active approved independent review of the parent."""
    return (await reviewed_approval_of(conn, parent_id))[1]


async def is_waived(conn: aiosqlite.Connection, parent_id: str) -> bool:
    """True when an operator explicitly waived independent review for this task."""
    kind, _ = await _latest_approval_event(conn, parent_id)
    return kind == "review.independent_waived"


async def merge_gate_status(
    conn: aiosqlite.Connection,
    parent: aiosqlite.Row,
    *,
    pr: str | None,
    head_sha: str | None,
    policy_path: Path | None = None,
) -> tuple[bool, str | None]:
    """(allowed, block_reason) for merging `parent`'s PR at `head_sha`.

    Allows merge when `review.independent` is off, the task is a `code_review`, or an
    operator explicitly waived review. Otherwise requires an active independent
    approval matching the exact PR and exact full 40-character head SHA with proof
    that the approving child used no author model/provider."""
    from argus_api.settings import service as settings_svc

    if parent["kind"] == CODE_REVIEW_KIND:
        return True, None
    setting = await settings_svc.get_review(
        conn, policy_path or model_policy_path, parent["project"]
    )
    if setting.mode == "off":
        return True, None
    kind, data = await _latest_approval_event(conn, parent["id"])
    if kind == "review.independent_waived":
        return True, None
    if parent["block_reason"] in (
        NO_INDEPENDENT_REVIEWER,
        REVIEW_FAILED,
        NO_REVIEW_HEAD,
    ):
        return False, str(parent["block_reason"])
    if await has_active_reviewer(conn, parent["id"]):
        return False, "independent_review_pending"
    if not pr or not head_sha or not _FULL_SHA.fullmatch(head_sha.strip().lower()):
        return False, NO_REVIEW_HEAD
    if kind != PASSED_EVENT:
        return False, "independent_review_required"
    approved_pr = data.get("pr") if isinstance(data.get("pr"), str) else None
    approved_sha = (
        data.get("head_sha") if isinstance(data.get("head_sha"), str) else None
    )
    if not approved_pr or not _same_pr(approved_pr, pr):
        return False, "independent_review_pr_mismatch"
    if not _same_sha(approved_sha, head_sha):
        return False, "independent_review_head_mismatch"
    child_id = data.get("child_id") if isinstance(data.get("child_id"), str) else None
    if not child_id:
        return False, "independent_review_same_model"
    author_models, author_providers = await authors(conn, parent["id"])
    if not await child_models_clean(
        conn,
        child_id,
        mode=setting.mode,
        author_models=author_models,
        author_providers=author_providers,
        parent_id=parent["id"],
    ):
        return False, "independent_review_same_model"
    return True, None


async def check_accept_reviewed(
    conn: aiosqlite.Connection,
    parent: aiosqlite.Row,
    forge: deliverable.ForgeRead | None,
    *,
    policy_path: Path | None = None,
) -> None:
    """Refuse accepting a code-delivering task when `review.independent` is on and
    the live PR/head on the forge does not match an independent approval."""
    from argus_api.settings import service as settings_svc

    if parent["kind"] == CODE_REVIEW_KIND:
        return
    setting = await settings_svc.get_review(
        conn, policy_path or model_policy_path, parent["project"]
    )
    if setting.mode == "off" or await is_waived(conn, parent["id"]):
        return
    seen = forge.delivery if forge is not None else None
    if seen is None:
        if Deliverable(parent["deliverable"]) not in FORGE_DELIVERABLES:
            return
        raise ConflictError(
            "independent review cannot be verified without a forge read",
            code="independent_review_required",
        )
    allowed, reason = await merge_gate_status(
        conn,
        parent,
        pr=seen.pr,
        head_sha=seen.head.sha if seen.head else None,
        policy_path=policy_path,
    )
    if not allowed:
        raise ConflictError(
            f"independent review does not cover {seen.pr}"
            f" at {seen.head.sha if seen.head else 'unknown'}: {reason}",
            code=reason or "independent_review_required",
            details=[
                f"pr: {seen.pr}",
                f"head_sha: {seen.head.sha if seen.head else None}",
                f"reason: {reason}",
            ],
        )


async def settle_or_wait(
    tx: Tx,
    parent: aiosqlite.Row,
    forge: deliverable.ForgeRead | None,
    reviewed_head: str | None,
    actor: str,
    *,
    reviewed_pr: str | None = None,
) -> bool:
    """Accept an approved parent the forge shows delivered, else leave it `in_review`
    with the visible `awaiting_forge` block (re-checked by `review_settle`). A moved
    head or replacement PR is reviewed again. True when the parent succeeded."""
    from argus_api.tasks import service as tasks_svc

    verdict = forge_verdict(
        parent, forge, reviewed_head, tx.now, reviewed_pr=reviewed_pr
    )
    if verdict is None:
        await tx.conn.execute(
            "UPDATE tasks SET block_reason = NULL WHERE id = ?", (parent["id"],)
        )
        await tasks_svc._apply(
            tx, parent, Action.ACCEPT, actor, data={"notes": "independent review"}
        )
        return True
    code, why = verdict
    if code in ("head_moved", "pr_moved"):
        seen = forge.delivery if forge is not None else None
        setting, policy = await _load_settings(tx.conn, parent["project"])
        await record(
            tx,
            actor,
            "task",
            parent["id"],
            "review.independent_void",
            {
                "reason": code,
                "reviewed": reviewed_head,
                "reviewed_pr": reviewed_pr,
                "current": why,
                "current_pr": seen.pr if seen else None,
            },
        )
        await tx.conn.execute(
            "UPDATE tasks SET block_reason = NULL WHERE id = ?", (parent["id"],)
        )
        await spawn_reviewer(
            tx,
            await tasks_svc._row(tx.conn, parent["id"]),
            pr=seen.pr if seen else None,
            head_sha=seen.head.sha if seen and seen.head else None,
            setting=setting,
            policy=policy,
            actor=actor,
        )
        return False
    async with tx.conn.execute(
        "SELECT data FROM events WHERE subject_type = 'task' AND subject_id = ?"
        " AND kind = ? ORDER BY id DESC LIMIT 1",
        (parent["id"], WAITING_EVENT),
    ) as cur:
        last = await cur.fetchone()
    seen_before = json.loads(last[0]) if last and last[0] else {}
    if parent["block_reason"] != AWAITING_FORGE or seen_before.get("why") != why:
        await tx.conn.execute(
            "UPDATE tasks SET block_reason = ?, updated_at = ? WHERE id = ?",
            (AWAITING_FORGE, ts(tx.now), parent["id"]),
        )
        await record(
            tx,
            actor,
            "task",
            parent["id"],
            WAITING_EVENT,
            {"why": why, "head_sha": reviewed_head},
        )
    return False


async def completion_forge_read(
    db: Database,
    checker: delivery.PrChecker,
    task_id: str,
    result: TaskResult,
    search: deliverable.PrSearch | None,
    policy_path: Path | None = None,
) -> deliverable.ForgeRead | None:
    """The forge's word on the PR a completion names, read before and outside the
    write (like find_pr). For a parent task whose gate is on, reads the live PR so
    the gate sees the live head SHA rather than an older attempt's stored SHA. For
    an approving `code_review` child, reads the parent's PR when the parent settles
    on that approval alone (`settles_alone`); for a fixing one, always. None otherwise."""
    from argus_api.settings import service as settings_svc
    from argus_api.tasks import service as tasks_svc

    if search is not None and search.match is not None and search.match.head_sha:
        return None
    async with db.read() as conn:
        row = await tasks_svc.task_row(conn, task_id)
        if row["kind"] == CODE_REVIEW_KIND:
            verdict = (result.data or {}).get("verdict")
            if not row["parent_id"] or verdict not in ("approve", FIX_VERDICT):
                return None
            parent = await tasks_svc.task_row(conn, row["parent_id"])
            # A fix is verified against the live PR whatever the parent's mode.
            if verdict == "approve" and not await settles_alone(conn, parent):
                return None
            task = await tasks_svc.get_task(conn, parent["id"])
            return deliverable.ForgeRead(
                await delivery.delivery_of(conn, checker, task)
            )
        setting = await settings_svc.get_review(
            conn, policy_path or model_policy_path, row["project"]
        )
        if setting.mode == "off":
            return None
        task = (await tasks_svc.get_task(conn, task_id)).model_copy(
            update={"result": result}
        )
        return deliverable.ForgeRead(await delivery.delivery_of(conn, checker, task))


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


async def _accounts(conn: aiosqlite.Connection, task_id: str) -> set[str]:
    """Accounts that served the task, across all attempts."""
    async with conn.execute(
        "SELECT DISTINCT account_id FROM model_executions"
        " WHERE task_id = ? AND account_id IS NOT NULL",
        (task_id,),
    ) as cur:
        return {r[0] for r in await cur.fetchall()}


# Account kinds that route to someone else's model: never a vendor themselves.
_ROUTER_PROVIDERS = frozenset({"custom", "local", "cursor", "copilot"})
_MODEL_FAMILY_VENDORS = (
    ("claude", "anthropic"),
    ("gpt-", "openai"),
    ("chatgpt", "openai"),
    ("codex", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("gemini", "google"),
)


def model_vendor(model: str, provider: str | None = None) -> str | None:
    """The vendor of a model: an OpenRouter `vendor/model` prefix, else the native
    family (claude-*, gpt-*, gemini-*), else the account's provider when that is a
    vendor and not a router. None when unknown; never derived from a router."""
    if "/" in model:
        return model.split("/", 1)[0].lower() or None
    lowered = model.lower()
    for prefix, vendor in _MODEL_FAMILY_VENDORS:
        if lowered.startswith(prefix):
            return vendor
    return provider if provider and provider not in _ROUTER_PROVIDERS else None


def author_vendors(models: set[str], providers: set[str]) -> set[str]:
    """Vendors of the models that wrote the task; routers are not vendors."""
    vendors = {v for m in models if (v := model_vendor(m))}
    return vendors | {p for p in providers if p not in _ROUTER_PROVIDERS}


async def _may_author(
    conn: aiosqlite.Connection,
    selector: Selector,
    pin: str | None,
    role: LeaseRole,
    *,
    mode: ReviewMode,
    models: set[str],
    providers: set[str],
    accounts: set[str],
) -> bool:
    """Whether this selector could run an author's model or an author's vendor.

    A reviewer must differ from the author in model and in model vendor, in both
    review modes (settings.spec.md 3.5). The vendor comes from the model, never from
    the router or account kind: a `custom` (OpenRouter) or `local` account is no
    vendor, and an unknown vendor is not excluded on provider alone. An
    account-sourced model is the account's configured one; where that is unknown
    until lease (a local account), the account is a possible author when it served
    the task. An unpinned selector may land on any account of its provider.
    """
    vendors = author_vendors(models, providers)
    if selector.model.source == "explicit":
        return selector.model.id in models or (
            model_vendor(selector.model.id, selector.provider) in vendors
        )
    if selector.provider in vendors:
        return True
    async with conn.execute(
        "SELECT * FROM accounts WHERE provider = ? AND (? IS NULL OR id = ?)",
        (selector.provider, pin, pin),
    ) as cur:
        rows = await cur.fetchall()
    from argus_api.accounts import service as accounts_svc

    for row in rows:
        if not accounts_svc._serves(row, role):
            continue
        settings = accounts_svc._settings(row, role)
        model = settings.model if settings is not None else None
        if model:
            if model in models or model_vendor(model, selector.provider) in vendors:
                return True
        elif row["id"] in accounts:
            return True
    return False


async def _authored(
    conn: aiosqlite.Connection, alternative: Alternative, **authors: Any
) -> bool:
    """Whether the model that actually reviews this alternative may be an author.

    A paired alternative's harness only coordinates; its executor is the reviewer,
    so only the executor is compared. A single alternative's harness is the reviewer.
    """
    harness_pin, executor_pin = routes.pins(alternative)
    if isinstance(alternative, PairedAlternative):
        return await _may_author(
            conn, alternative.executor, executor_pin, "executor", **authors
        )
    return await _may_author(
        conn, alternative.harness, harness_pin, "engine", **authors
    )


async def _selector_usable(
    conn: aiosqlite.Connection,
    now_iso: str,
    selector: Selector,
    pin: str | None,
    role: LeaseRole,
) -> bool:
    """True when at least one account serving `role` for `selector` is usable now."""
    from argus_api.accounts import service as accounts_svc

    async with conn.execute(
        "SELECT * FROM accounts WHERE provider = ? AND (? IS NULL OR id = ?)"
        " AND state IN ('available', 'exhausted')",
        (selector.provider, pin, pin),
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        if row["state"] == "exhausted" and (
            not row["exhausted_until"] or row["exhausted_until"] > now_iso
        ):
            continue
        if accounts_svc._serves(row, role):
            return True
    return False


async def _alternative_usable(
    conn: aiosqlite.Connection, now_iso: str, alternative: Alternative
) -> bool:
    harness_pin, executor_pin = routes.pins(alternative)
    if not await _selector_usable(
        conn, now_iso, alternative.harness, harness_pin, "engine"
    ):
        return False
    return not isinstance(alternative, PairedAlternative) or await _selector_usable(
        conn, now_iso, alternative.executor, executor_pin, "executor"
    )


def _selector_signature(alternative: Alternative) -> str:
    payload: dict[str, Any] = {"harness": alternative.harness.model_dump(mode="json")}
    if isinstance(alternative, PairedAlternative):
        payload["executor"] = alternative.executor.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


async def review_route(
    conn: aiosqlite.Connection,
    now: datetime,
    parent: aiosqlite.Row,
    *,
    mode: ReviewMode,
    models: set[str],
    providers: set[str],
    accounts: set[str],
    exclude_alternatives: AbstractSet[str] = frozenset(),
    exclude_accounts: AbstractSet[str] = frozenset(),
) -> tuple[list[Alternative], routes.ReviewRoute] | None:
    """The reviewer's route, walked from `execution.chains` (settings.spec.md 3.5).

    The walk starts at the parent's difficulty chain (chain order), then continues
    through the other difficulties (sorted names, chain order). Each alternative
    that is not an author model, not excluded, and has a usable account and (when
    workers are registered) a compatible worker joins the route; the first one is
    selected. None when `execution.chains` configures no difficulty (the legacy
    policy-chain picker applies).
    """
    from pydantic import TypeAdapter

    setting = await routes.policy_setting(conn, parent["project"], routes.KEY)
    difficulty = parent["difficulty"]
    configured = setting.value["difficulties"]
    candidates: list[tuple[str, Alternative]] = []
    for d in [difficulty, *sorted(d for d in configured if d != difficulty)]:
        if d in configured:
            candidates.extend(
                (d, alt) for alt in TypeAdapter(Chain).validate_python(configured[d])
            )
    if not candidates:
        return None

    async with conn.execute(
        "SELECT capabilities, runtime_providers FROM agents WHERE state = 'active'"
    ) as cur:
        workers = list(await cur.fetchall())
    now_iso = ts(now)
    skipped = []
    eligible: list[Alternative] = []
    seen_sigs: set[str] = set()
    used_ids: set[str] = set()
    chosen_diff = difficulty
    for cand_diff, alternative in candidates:
        sig = _selector_signature(alternative)
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)
        if alternative.id in exclude_alternatives or (
            exclude_accounts & {p for p in routes.pins(alternative) if p}
        ):
            skipped.append(
                routes.SkippedAlternative(id=alternative.id, reason="failed")
            )
            continue
        if await _authored(
            conn,
            alternative,
            mode=mode,
            models=models,
            providers=providers,
            accounts=accounts,
        ):
            skipped.append(
                routes.SkippedAlternative(
                    id=alternative.id,
                    reason="author",
                    selector="executor"
                    if isinstance(alternative, PairedAlternative)
                    else None,
                )
            )
            continue
        usable = await _alternative_usable(conn, now_iso, alternative)
        worker_ready = (
            any(
                [
                    await routes.alternative_eligible(
                        conn,
                        now,
                        alternative,
                        None,
                        json.loads(w["capabilities"]),
                        json.loads(w["runtime_providers"] or "[]"),
                    )
                    for w in workers
                ]
            )
            if workers
            else True
        )
        if not (usable and worker_ready):
            skipped.append(
                routes.SkippedAlternative(id=alternative.id, reason="unavailable")
            )
            continue
        alt_out = alternative
        if alt_out.id in used_ids:
            alt_out = alt_out.model_copy(update={"id": f"{alt_out.id}-{cand_diff}"})
        used_ids.add(alt_out.id)
        if not eligible:
            chosen_diff = cand_diff
        eligible.append(alt_out)

    return eligible, routes.ReviewRoute(
        parent_id=parent["id"],
        difficulty=chosen_diff,
        selected=eligible[0].id if eligible else "",
        skipped=skipped,
    )


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
    """How many completed `code_review` children ended with request_changes or a fix."""
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
        if data.get("verdict") in _ROUND_VERDICTS:
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


async def _block(
    tx: Tx, parent: aiosqlite.Row, reason: str, actor: str, **data: Any
) -> None:
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
        {"reason": reason, **data},
    )


async def _open_blocked_decision(
    tx: Tx, parent: aiosqlite.Row, reason: str, question: str, actor: str
) -> None:
    """Open at most one actionable `review` decision (retry_reviewer,
    approve_without_review, cancel) when the independent review gate blocks."""
    if parent["state"] != TaskState.IN_REVIEW:
        return
    if await decisions.open_ids(tx.conn, "task", parent["id"], (DecisionKind.REVIEW,)):
        return
    if await has_active_reviewer(tx.conn, parent["id"]):
        return
    failed = [
        {k: a.get(k) for k in ("child_id", "state", "error_class", "alternative")}
        for a in await failed_reviewers(tx.conn, parent["id"])
    ]
    await decisions.open_decision(
        tx,
        subject_type="task",
        subject_id=parent["id"],
        kind=DecisionKind.REVIEW,
        question=question,
        options=list(REVIEW_FAILED_OPTIONS),
        audience=Audience.HUMAN,
        requested_by=actor,
        payload={"gate": REVIEW_FAILED_GATE, "reason": reason, "failed": failed},
    )


async def _no_head(tx: Tx, parent: aiosqlite.Row, pr: str | None, actor: str) -> None:
    """No commit to review: block `no_review_head` and leave exactly one operator
    decision with the review-failed choices (retry, approve without review, cancel)."""
    await _block(tx, parent, NO_REVIEW_HEAD, actor, pr=pr)
    await _open_blocked_decision(
        tx,
        parent,
        NO_REVIEW_HEAD,
        (
            f"Argus cannot name the commit to review for {parent['title']!r}"
            f" (pull request {pr or 'unknown'}: the forge reported no head)."
            " Retry the review, approve without independent review, or cancel?"
        ),
        actor,
    )


async def has_active_reviewer(conn: aiosqlite.Connection, parent_id: str) -> bool:
    async with conn.execute(
        "SELECT 1 FROM tasks WHERE parent_id = ? AND kind = ? AND state NOT IN (?, ?, ?)"
        " LIMIT 1",
        (
            parent_id,
            CODE_REVIEW_KIND,
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        ),
    ) as cur:
        return await cur.fetchone() is not None


async def open_manual_review(
    tx: Tx, parent: aiosqlite.Row, gate: str, actor: str
) -> str | None:
    """Keep the in_review invariant: a parent waits on an open review decision or an
    active reviewer, never neither. Opens the regular review decision, with `gate`
    (`MANUAL_REVIEW_GATES`) in its payload, when the parent has neither; otherwise a
    no-op, so every loss path may call it. Returns the new decision id."""
    if parent["state"] != TaskState.IN_REVIEW:
        return None
    if await decisions.open_ids(tx.conn, "task", parent["id"], (DecisionKind.REVIEW,)):
        return None
    if await has_active_reviewer(tx.conn, parent["id"]):
        return None
    failed = [
        {k: a.get(k) for k in ("child_id", "state", "error_class", "alternative")}
        for a in await failed_reviewers(tx.conn, parent["id"])
    ]
    return await decisions.open_decision(
        tx,
        subject_type="task",
        subject_id=parent["id"],
        kind=DecisionKind.REVIEW,
        question=(
            f"Review the result of {parent['title']!r} (attempt {parent['attempt']})."
            f" The independent review did not complete ({_GATE_WHY[gate]}); review it"
            " manually."
        ),
        options=list(REVIEW_OPTIONS),
        audience=Audience.HUMAN,
        requested_by=actor,
        payload={"gate": gate, "failed": failed},
    )


async def clear_gate_block(tx: Tx, task_id: str) -> None:
    """Drop a gate block_reason once the operator's review decision settled the task."""
    await tx.conn.execute(
        "UPDATE tasks SET block_reason = NULL WHERE id = ? AND block_reason IN (?, ?, ?, ?)",
        (task_id, *_GATE_BLOCKS),
    )


async def _manual_unless_escalating(
    tx: Tx, parent: aiosqlite.Row, none_eligible: str, actor: str
) -> None:
    """A blocked spawn leaves the parent with one actionable review decision
    (retry_reviewer, approve_without_review, cancel), except where the caller
    escalates itself (`none_eligible` is REVIEW_FAILED)."""
    if none_eligible != REVIEW_FAILED:
        await _open_blocked_decision(
            tx,
            parent,
            NO_INDEPENDENT_REVIEWER,
            (
                f"No eligible independent reviewer is available for {parent['title']!r}"
                f" (attempt {parent['attempt']}). Retry the reviewer, approve without"
                " independent review, or cancel?"
            ),
            actor,
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
    exclude_accounts: set[str] | None = None,
    exclude_alternatives: set[str] | None = None,
    none_eligible: str = NO_INDEPENDENT_REVIEWER,
    replace_child_id: str | None = None,
) -> str | None:
    """Create a code_review child, or set block_reason when none is eligible
    (`none_eligible`: the reason when no alternative remains).

    Idempotent for an already-active reviewer targeting the same (pr, head_sha).
    Supersedes and cancels any older active reviewer targeting a stale PR or head.
    Returns the child task id, or None when blocked for want of a reviewer.
    """
    from argus_api.tasks import manage
    from argus_api.tasks import service as tasks_svc

    if head_sha is None:
        # The reviewer's input must name the commit it reviews; without one the
        # parent waits on one operator decision rather than inventing a head.
        await _no_head(tx, parent, pr, actor)
        return None
    sha = head_sha.lower()
    async with tx.conn.execute(
        "SELECT id, input FROM tasks WHERE parent_id = ? AND kind = ?"
        " AND state NOT IN (?, ?, ?) ORDER BY rowid DESC",
        (
            parent["id"],
            CODE_REVIEW_KIND,
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        ),
    ) as cur:
        active_children = [
            r for r in await cur.fetchall() if r["id"] != replace_child_id
        ]
    for active in active_children:
        ainp = json.loads(active["input"] or "{}")
        same_head = _same_sha(ainp.get("head_sha"), sha)
        same_pr = (
            True if (not pr and not ainp.get("pr")) else _same_pr(ainp.get("pr"), pr)
        )
        if same_head and same_pr:
            return str(active["id"])
    author_models, author_providers = await authors(tx.conn, parent["id"])
    fix_models, _ = await fixers(tx.conn, parent["id"])
    author_models |= fix_models
    route = await review_route(
        tx.conn,
        tx.now,
        parent,
        mode=setting.mode,
        models=author_models | (exclude_models or set()),
        providers=author_providers,
        accounts=await _accounts(tx.conn, parent["id"]) | (exclude_accounts or set()),
        exclude_alternatives=exclude_alternatives or set(),
        exclude_accounts=exclude_accounts or set(),
    )
    harness: HarnessModel | None = None
    executor: ExecutorModel | None = None
    if route is not None:
        alternatives, review = route
        if not alternatives:
            # Never fall back to an author: the operator decides from the inbox.
            await _block(
                tx,
                parent,
                none_eligible,
                actor,
                skipped=[s.model_dump(exclude_none=True) for s in review.skipped],
            )
            await _manual_unless_escalating(tx, parent, none_eligible, actor)
            return None
    else:
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
            await _block(tx, parent, none_eligible, actor)
            await _manual_unless_escalating(tx, parent, none_eligible, actor)
            return None
    # Clear a prior gate block if we found a reviewer this time.
    if parent["block_reason"] in _GATE_BLOCKS:
        await tx.conn.execute(
            "UPDATE tasks SET block_reason = NULL WHERE id = ?", (parent["id"],)
        )
    title = f"Independent review of {parent['title']!r}"
    body = TaskCreate(
        title=title[:200],
        spec=(
            f"Review the code delivered by task `{parent['id']}`"
            f" (head `{sha}`).\n\n"
            "Finish with `COMPLETE:` and one JSON object, sent with the send_message"
            ' tool to `to: "argus"`, in exactly this shape:\n'
            '`COMPLETE: {"summary":"<one line>","data":{"verdict":"approve",'
            '"findings":[<strings or objects>],"head_sha":"<the full sha you reviewed>"}}`\n'
            "`verdict` is `approve`, `request_changes` or `fixed`. `data` takes only"
            " verdict, findings, head_sha, fixed_head_sha and tests; do not put them at"
            " the top level.\n\n"
            "If you find actionable problems you can safely fix, fix them on the PR's"
            " own branch: add tests, run the gates, and push one normal new commit"
            " (fast-forward only; never force-push, never rewrite history, never"
            " touch another branch or merge). Then finish with verdict `fixed`,"
            " `head_sha` the head you reviewed, `fixed_head_sha` the full new head,"
            " `findings` what you fixed and `tests` what you ran. You cannot approve"
            " your own fix: Argus schedules a fresh review of the new head with"
            " another model. If you cannot fix safely, return `request_changes` and"
            " change nothing. With no findings, return `approve` and change nothing."
        ),
        kind=CODE_REVIEW_KIND,
        project=parent["project"],
        parent_id=parent["id"],
        input={
            "task_id": parent["id"],
            "pr": pr,
            "head_sha": sha,
            # Scoped write: push to this PR's branch, fast-forward only, no merge.
            "fix": {"pr": pr, "base_head_sha": sha, "force_push": False},
        },
        review=Review.NONE,
        harness_model=harness,
        executor_model=executor,
        # Fixed shape: the reviewer may push a fix to the PR branch and nothing else
        # (no merge grant: the deliverable is a response, `allow_merge` is not set).
        required_capabilities=["repository:write"],
        deliverable=Deliverable.RESPONSE,
    )
    child = await tasks_svc.create_task(tx, body, actor)
    if route is not None:
        await tx.conn.execute(
            "UPDATE tasks SET execution_route = ? WHERE id = ?",
            (routes.explicit_route(alternatives, review), child.id),
        )
        await record(
            tx,
            actor,
            "task",
            parent["id"],
            "review.independent_selected",
            {
                "child_id": child.id,
                "selected": review.selected,
                "fallbacks": [a.id for a in alternatives[1:]],
                "skipped": [s.model_dump(exclude_none=True) for s in review.skipped],
                "chain_source": review.chain_source,
                "difficulty": review.difficulty,
            },
        )
    await record(
        tx,
        actor,
        "task",
        parent["id"],
        "review.independent_requested",
        {
            "child_id": child.id,
            "pr": pr,
            "head_sha": sha,
            "harness_model": harness.model_dump() if harness else None,
            "executor_model": executor.model_dump() if executor else None,
            "mode": setting.mode,
        },
    )
    # Cancel any stale active reviewers for older PRs/heads after the new child exists
    # so `on_review_child_ended` recognizes them as superseded and ignores them.
    for stale in active_children:
        await manage.cancel(tx, stale["id"], actor)
    return child.id


async def on_parent_complete(
    tx: Tx,
    row: aiosqlite.Row,
    result: TaskResult,
    *,
    setting: IndependentReviewSetting,
    policy: model_policy.Policy,
    actor: str,
    forge: deliverable.ForgeRead | None = None,
) -> bool:
    """If the gate applies, move the parent to in_review and spawn a reviewer.

    Returns True when the independent-review path handled completion (caller must
    not run the normal COMPLETE transition). Returns False when the gate is off
    or the task delivers no code (and a skip event may have been recorded).

    A PR that is already merged is reviewed too, at the head that merged: the review
    is never skipped for it, so the parent always gets a reviewer or a decision.
    """
    from argus_api.tasks import service as tasks_svc

    if setting.mode == "off" or row["kind"] == CODE_REVIEW_KIND:
        return False
    refs = await has_code_delivery(tx.conn, row, result, forge)
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
    from argus_api.tasks import merge_resume

    approved_pr, approved_head = await reviewed_approval_of(tx.conn, row["id"])
    if (
        approved_head
        and _same_pr(approved_pr, pr)
        and _same_sha(approved_head, head_sha)
        and await merge_resume.resumed_for_approval(tx.conn, row["id"], pr, head_sha)
    ):
        # The merge attempt of an approved head ended: never review the same head
        # again; the forge's word settles it (pre-merge review spec 3.6).
        await tasks_svc._apply(
            tx,
            row,
            Action.COMPLETE,
            actor,
            set_={"result": result.model_dump_json(exclude_none=True)},
            review=Review.ORCHESTRATOR,
            open_review_decision=False,
        )
        parent = await tasks_svc._row(tx.conn, row["id"])
        if not await settle_or_wait(tx, parent, forge, head_sha, actor, reviewed_pr=pr):
            await merge_resume.open_unmerged_decision(
                tx, await tasks_svc._row(tx.conn, row["id"]), forge, actor
            )
        return True
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


async def fixers(
    conn: aiosqlite.Connection, parent_id: str
) -> tuple[set[str], set[str]]:
    """(models, reviewer child ids) that pushed a fix to the parent's PR branch.

    A reviewer that changed the code is an author of it: its model (and session)
    may never approve, and the next reviewer must run another model."""
    models: set[str] = set()
    children: set[str] = set()
    async with conn.execute(
        "SELECT data FROM events WHERE subject_type = 'task' AND subject_id = ?"
        " AND kind = ?",
        (parent_id, FIX_PUSHED_EVENT),
    ) as cur:
        for (raw,) in await cur.fetchall():
            data = json.loads(raw) if raw else {}
            if isinstance(data.get("child_id"), str):
                children.add(data["child_id"])
            models.update(m for m in data.get("fix_models") or [] if isinstance(m, str))
    return models, children


async def child_models_clean(
    conn: aiosqlite.Connection,
    child_id: str,
    *,
    mode: ReviewMode,
    author_models: set[str],
    author_providers: set[str],
    parent_id: str | None = None,
) -> bool:
    """True when the child's executions prove it used no author model/provider.

    With `parent_id`, the models and sessions of the reviewers that fixed the
    parent's code count as authors too, in every mode."""
    child_models, child_providers = await authors(conn, child_id)
    if parent_id is not None:
        fix_models, fix_children = await fixers(conn, parent_id)
        if child_id in fix_children or not child_models.isdisjoint(fix_models):
            return False
    if not child_models and not child_providers:
        # No execution recorded: cannot prove independence when authors exist.
        return not author_models and not author_providers
    if mode == "different_provider":
        return child_providers.isdisjoint(author_providers)
    return child_models.isdisjoint(author_models)


_CHILD_OUTCOME_EVENTS = (
    PASSED_EVENT,
    FIX_PUSHED_EVENT,
    "review.independent_changes_requested",
    "review.independent_escalated",
    "review.independent_void",
)


async def _child_already_processed(
    conn: aiosqlite.Connection, parent_id: str, child_id: str
) -> bool:
    marks = ",".join("?" * len(_CHILD_OUTCOME_EVENTS))
    async with conn.execute(
        f"SELECT data FROM events WHERE subject_type = 'task'"  # noqa: S608
        f" AND subject_id = ? AND kind IN ({marks})",
        (parent_id, *_CHILD_OUTCOME_EVENTS),
    ) as cur:
        for (raw,) in await cur.fetchall():
            if raw and json.loads(raw).get("child_id") == child_id:
                return True
    return False


async def _fix_refused(
    tx: Tx, parent: aiosqlite.Row, child: aiosqlite.Row, reason: str, actor: str
) -> None:
    """A fix Argus cannot attribute or verify never counts as reviewed: the reviewer's
    verdict is void and a manual review decides (no automatic respawn, no loop)."""
    await record(
        tx,
        actor,
        "task",
        parent["id"],
        "review.independent_void",
        {"reason": reason, "child_id": child["id"]},
    )
    await open_manual_review(tx, parent, REVIEWER_FAILED_GATE, actor)


async def _on_fix_pushed(
    tx: Tx,
    parent: aiosqlite.Row,
    child: aiosqlite.Row,
    child_input: dict[str, Any],
    data: dict[str, Any],
    *,
    forge: deliverable.ForgeRead | None,
    setting: IndependentReviewSetting,
    policy: model_policy.Policy,
    actor: str,
) -> None:
    """The reviewer pushed a correction: record who fixed what, invalidate any older
    approval, and schedule a fresh review of the exact new head by another model.
    The fixer never approves its own change."""
    expected_pr = child_input.get("pr")
    old_head = child_input.get("head_sha")
    new_head = data.get("fixed_head_sha")
    new_head = new_head.strip().lower() if isinstance(new_head, str) else None
    if (
        not new_head
        or not _FULL_SHA.fullmatch(new_head)
        or not old_head
        or not _same_sha(data.get("head_sha"), old_head)
        or _same_sha(new_head, old_head)
    ):
        await _fix_refused(tx, parent, child, "fix_head_invalid", actor)
        return
    seen = forge.delivery if forge is not None else None
    forge_pr = seen.pr if seen is not None else None
    forge_sha = (
        seen.head.sha.lower()
        if seen is not None and seen.head is not None and seen.head.sha
        else None
    )
    if not forge_pr or not forge_sha:
        await _fix_refused(tx, parent, child, "fix_unverified", actor)
        return
    if expected_pr and not _same_pr(expected_pr, forge_pr):
        await _fix_refused(tx, parent, child, "fix_pr_mismatch", actor)
        return
    if forge_sha != new_head:
        await _fix_refused(tx, parent, child, "fix_head_mismatch", actor)
        return
    child_models, child_providers = await authors(tx.conn, child["id"])
    if len(child_models) != 1:
        # Which model wrote the fix must be exact: none or several is ambiguous.
        await _fix_refused(tx, parent, child, "fix_provenance_unclear", actor)
        return
    findings = data.get("findings")
    tests = data.get("tests")
    await record(
        tx,
        actor,
        "task",
        parent["id"],
        FIX_PUSHED_EVENT,
        {
            "child_id": child["id"],
            "pr": forge_pr,
            "old_head_sha": old_head,
            "new_head_sha": new_head,
            "fix_models": sorted(child_models),
            "fix_providers": sorted(child_providers),
            "findings": findings if isinstance(findings, list) else [],
            "tests": tests if isinstance(tests, list) else [],
        },
    )
    rounds = await review_rounds(tx.conn, parent["id"])
    if rounds >= setting.max_rounds:
        await decisions.open_decision(
            tx,
            subject_type="task",
            subject_id=parent["id"],
            kind=DecisionKind.REVIEW,
            question=(
                f"Independent review exhausted {setting.max_rounds} rounds for"
                f" {parent['title']!r}; the last fix ({new_head}) is unreviewed."
                " Accept or reject?"
            ),
            options=list(REVIEW_OPTIONS),
            audience=Audience.HUMAN,
            requested_by=actor,
        )
        await record(
            tx,
            actor,
            "task",
            parent["id"],
            "review.independent_escalated",
            {"child_id": child["id"], "rounds": rounds},
        )
        return
    await spawn_reviewer(
        tx,
        parent,
        pr=forge_pr,
        head_sha=new_head,
        setting=setting,
        policy=policy,
        actor=actor,
        replace_child_id=child["id"],
    )


async def on_review_child_complete(
    tx: Tx,
    child: aiosqlite.Row,
    result: TaskResult,
    *,
    setting: IndependentReviewSetting,
    policy: model_policy.Policy,
    actor: str,
    forge: deliverable.ForgeRead | None = None,
) -> None:
    """Apply the child's verdict to the parent (proof-checked, idempotent per child).
    `forge` is the read of the parent's PR made before the write (`completion_forge_read`)."""
    from argus_api.tasks import service as tasks_svc

    parent_id = child["parent_id"]
    if not parent_id:
        return
    parent = await tasks_svc._row(tx.conn, parent_id)
    if parent["state"] != TaskState.IN_REVIEW:
        return
    # Ignore stale (superseded) children when a newer code_review child exists.
    async with tx.conn.execute(
        "SELECT id FROM tasks WHERE parent_id = ? AND kind = ?"
        " ORDER BY rowid DESC LIMIT 1",
        (parent_id, CODE_REVIEW_KIND),
    ) as cur:
        latest = await cur.fetchone()
    if latest is not None and latest["id"] != child["id"]:
        return
    if await _child_already_processed(tx.conn, parent_id, child["id"]):
        return
    data = result.data or {}
    verdict = data.get("verdict")
    findings = data.get("findings") or []
    head_sha = data.get("head_sha")
    child_input = json.loads(child["input"] or "{}")
    author_models, author_providers = await authors(tx.conn, parent_id)
    if not await child_models_clean(
        tx.conn,
        child["id"],
        mode=setting.mode,
        author_models=author_models,
        author_providers=author_providers,
        parent_id=parent_id,
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
        child_models, _ = await authors(tx.conn, child["id"])
        # The void reviewer's accounts may serve an author model under another
        # name; do not pick them again.
        await spawn_reviewer(
            tx,
            parent,
            pr=child_input.get("pr"),
            head_sha=child_input.get("head_sha"),
            setting=setting,
            policy=policy,
            actor=actor,
            exclude_models=author_models | child_models,
            exclude_accounts=await _accounts(tx.conn, child["id"]),
            replace_child_id=child["id"],
        )
        return

    if verdict == FIX_VERDICT:
        await _on_fix_pushed(
            tx,
            parent,
            child,
            child_input,
            data,
            forge=forge,
            setting=setting,
            policy=policy,
            actor=actor,
        )
        return

    if verdict == "approve":
        # PR identity and head_sha must exactly match both what this reviewer was
        # assigned and what the parent / forge currently delivers.
        parent_result = (
            TaskResult.model_validate_json(parent["result"])
            if parent["result"]
            else TaskResult(summary=".")
        )
        parent_pr, parent_sha = _delivery_refs(parent, parent_result)
        expected_pr = child_input.get("pr")
        expected_sha = child_input.get("head_sha")
        seen = forge.delivery if forge is not None else None
        forge_pr = seen.pr if seen is not None else None
        forge_sha = (
            seen.head.sha.lower()
            if seen is not None and seen.head is not None and seen.head.sha
            else None
        )
        current_pr = forge_pr or parent_pr or expected_pr
        current_sha = (
            forge_sha
            if (forge_sha and (not current_pr or _same_pr(forge_pr, current_pr)))
            else parent_sha
        )
        if expected_pr and parent_pr and not _same_pr(expected_pr, parent_pr):
            await record(
                tx,
                actor,
                "task",
                parent_id,
                "review.independent_void",
                {
                    "reason": "pr_mismatch",
                    "child_id": child["id"],
                    "reviewed_pr": expected_pr,
                    "current_pr": parent_pr,
                },
            )
            await spawn_reviewer(
                tx,
                parent,
                pr=current_pr,
                head_sha=current_sha,
                setting=setting,
                policy=policy,
                actor=actor,
                replace_child_id=child["id"],
            )
            return
        if expected_pr and forge_pr and not _same_pr(expected_pr, forge_pr):
            await record(
                tx,
                actor,
                "task",
                parent_id,
                "review.independent_void",
                {
                    "reason": "pr_mismatch",
                    "child_id": child["id"],
                    "reviewed_pr": expected_pr,
                    "current_pr": forge_pr,
                },
            )
            await spawn_reviewer(
                tx,
                parent,
                pr=forge_pr,
                head_sha=forge_sha,
                setting=setting,
                policy=policy,
                actor=actor,
                replace_child_id=child["id"],
            )
            return
        if expected_sha and (not head_sha or not _same_sha(head_sha, expected_sha)):
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
                    "expected": expected_sha,
                    "current": current_sha or expected_sha,
                },
            )
            await spawn_reviewer(
                tx,
                parent,
                pr=current_pr,
                head_sha=current_sha or expected_sha,
                setting=setting,
                policy=policy,
                actor=actor,
                replace_child_id=child["id"],
            )
            return
        if current_sha and (not head_sha or not _same_sha(head_sha, current_sha)):
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
                pr=current_pr,
                head_sha=current_sha,
                setting=setting,
                policy=policy,
                actor=actor,
                replace_child_id=child["id"],
            )
            return
        if parent["block_reason"] in (NO_INDEPENDENT_REVIEWER, REVIEW_FAILED):
            await tx.conn.execute(
                "UPDATE tasks SET block_reason = NULL WHERE id = ?", (parent_id,)
            )
        review = Review(parent["review"])
        reviewed = (head_sha or expected_sha or "").lower() or None
        reviewed_pr = expected_pr or parent_pr or forge_pr
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
            PASSED_EVENT,
            {
                "child_id": child["id"],
                "verdict": "approve",
                "head_sha": reviewed,
                **({"pr": reviewed_pr} if reviewed_pr else {}),
            },
        )
        # A PR that must still turn green or merge is closed by the forge's word
        # (task deliverable spec 3.5), never by this approval: without a manual review
        # to ask for, the parent settles once the forge read confirms it.
        forge_gated = Deliverable(parent["deliverable"]) in FORGE_DELIVERABLES
        if await settles_alone(tx.conn, parent) and not await decisions.open_ids(
            tx.conn, "task", parent_id, (DecisionKind.REVIEW,)
        ):
            await settle_or_wait(
                tx, parent, forge, reviewed, actor, reviewed_pr=reviewed_pr
            )
        elif review is not Review.NONE or open_items or forge_gated:
            await decisions.open_decision(
                tx,
                subject_type="task",
                subject_id=parent_id,
                kind=DecisionKind.REVIEW,
                question=(
                    f"Review the result of {parent['title']!r}"
                    f" (attempt {parent['attempt']})."
                ),
                options=list(REVIEW_OPTIONS),
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

    if verdict != "request_changes":
        # No usable verdict: a manual review settles the parent.
        await record(
            tx,
            actor,
            "task",
            parent_id,
            "review.independent_void",
            {"reason": "no_verdict", "child_id": child["id"]},
        )
        await open_manual_review(tx, parent, REVIEWER_FAILED_GATE, actor)
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
                options=list(REVIEW_OPTIONS),
                audience=Audience.HUMAN,
                requested_by=actor,
                payload={
                    "gate": ROUNDS_EXHAUSTED_GATE,
                    "child_id": child["id"],
                    "rounds": rounds,
                },
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


async def _gate_events(conn: aiosqlite.Connection, parent_id: str) -> list[Any]:
    async with conn.execute(
        "SELECT kind, data FROM events WHERE subject_type = 'task' AND subject_id = ?"
        " AND kind IN (?, ?) ORDER BY id",
        (parent_id, CHILD_FAILED_EVENT, RETRY_EVENT),
    ) as cur:
        return list(await cur.fetchall())


async def failed_reviewers(
    conn: aiosqlite.Connection, parent_id: str
) -> list[dict[str, Any]]:
    """Data of the `review.independent_child_failed` events since the last operator
    retry, oldest first. The retry starts a new window and its own bound."""
    out: list[dict[str, Any]] = []
    for row in await _gate_events(conn, parent_id):
        if row["kind"] == RETRY_EVENT:
            out = []
        else:
            out.append(json.loads(row["data"]))
    return out


async def failed_reviewer_summaries(
    conn: aiosqlite.Connection, parent_id: str
) -> list[FailedReviewer]:
    return [
        FailedReviewer(
            child_task_id=d["child_id"],
            state=d["state"],
            error_class=d.get("error_class"),
            account=d.get("account"),
            model=d.get("model"),
        )
        for d in await failed_reviewers(conn, parent_id)
    ]


async def _child_facts(
    conn: aiosqlite.Connection, child: aiosqlite.Row
) -> dict[str, Any]:
    """Alternative, account and model the child last ran on, when known."""
    alternative = None
    async with conn.execute(
        "SELECT selection FROM task_execution_routes WHERE task_id = ?", (child["id"],)
    ) as cur:
        picked = await cur.fetchone()
    if picked and picked["selection"]:
        alternative = json.loads(picked["selection"])["alternative"]["id"]
    elif child["execution_route"]:
        alternative = (
            json.loads(child["execution_route"]).get("review", {}).get("selected")
        )
    async with conn.execute(
        "SELECT account_id, provider, observed_model, configured_model"
        " FROM model_executions WHERE task_id = ? ORDER BY recorded_at DESC, id DESC"
        " LIMIT 1",
        (child["id"],),
    ) as cur:
        run = await cur.fetchone()
    return {
        "alternative": alternative or None,
        "account": run["account_id"] if run else None,
        "provider": run["provider"] if run else None,
        "model": (run["observed_model"] or run["configured_model"]) if run else None,
    }


async def _load_settings(
    conn: aiosqlite.Connection, project: str
) -> tuple[IndependentReviewSetting, model_policy.Policy]:
    from argus_api.settings import service as settings_svc

    setting = await settings_svc.get_review(conn, model_policy_path, project)
    policy, _ = await settings_svc.effective_model_policy(
        conn, model_policy_path, project
    )
    return setting, policy


async def _escalate(
    tx: Tx,
    parent: aiosqlite.Row,
    attempts: list[dict[str, Any]],
    actor: str,
    *,
    blocked: bool,
) -> None:
    """Block the parent as `review_failed` and leave exactly one operator decision.
    `blocked` is true when the selector already set the block."""
    failed = [
        {k: v for k, v in a.items() if k in ("child_id", "state", "error_class")}
        | {"alternative": a.get("alternative")}
        for a in attempts
    ]
    if not blocked:
        await _block(tx, parent, REVIEW_FAILED, actor, failed=failed)
    if await decisions.open_ids(tx.conn, "task", parent["id"], (DecisionKind.REVIEW,)):
        return
    await decisions.open_decision(
        tx,
        subject_type="task",
        subject_id=parent["id"],
        kind=DecisionKind.REVIEW,
        question=(
            f"No independent reviewer delivered a verdict for {parent['title']!r}"
            f" ({len(attempts)} reviewer attempt(s) failed). Retry the reviewer,"
            " approve without independent review, or cancel?"
        ),
        options=list(REVIEW_FAILED_OPTIONS),
        audience=Audience.HUMAN,
        requested_by=actor,
        payload={"gate": REVIEW_FAILED_GATE, "failed": failed},
    )


async def on_review_child_ended(
    tx: Tx, child: aiosqlite.Row, state: TaskState, actor: str
) -> None:
    """A `code_review` child reached `failed` or `cancelled` without a verdict.

    Records `review.independent_child_failed` on the parent, then selects again with
    the child's route and account excluded, until `max_reviewer_failures` is spent or
    no alternative remains; then the parent is blocked `review_failed` and an operator
    decides. Idempotent per child; a child that is not the parent's latest is ignored.
    """
    from argus_api.tasks import service as tasks_svc

    parent_id = child["parent_id"]
    if not parent_id or child["kind"] != CODE_REVIEW_KIND:
        return
    parent = await tasks_svc._row(tx.conn, parent_id)
    if parent["state"] != TaskState.IN_REVIEW:
        return
    async with tx.conn.execute(
        "SELECT id FROM tasks WHERE parent_id = ? AND kind = ?"
        " ORDER BY rowid DESC LIMIT 1",
        (parent_id, CODE_REVIEW_KIND),
    ) as cur:
        latest = await cur.fetchone()
    if latest is None or latest["id"] != child["id"]:
        return
    if any(
        json.loads(r["data"]).get("child_id") == child["id"]
        for r in await _gate_events(tx.conn, parent_id)
        if r["kind"] == CHILD_FAILED_EVENT
    ):
        return
    error = sanitize_notice(child["error"]) if child["error"] else None
    attempt = {
        "child_id": child["id"],
        "state": str(state),
        "error_class": failure_class(child["error"], child["block_reason"] or "")
        if child["error"]
        else None,
        "error": error,
    } | await _child_facts(tx.conn, child)
    await record(tx, actor, "task", parent_id, CHILD_FAILED_EVENT, attempt)
    attempts = await failed_reviewers(tx.conn, parent_id)
    setting, policy = await _load_settings(tx.conn, parent["project"])
    if setting.mode == "off":
        await open_manual_review(tx, parent, GATE_DISABLED_GATE, actor)
        return
    if len(attempts) > setting.max_reviewer_failures:
        await _escalate(tx, parent, attempts, actor, blocked=False)
        return
    child_input = json.loads(child["input"] or "{}")
    spawned = await spawn_reviewer(
        tx,
        parent,
        pr=child_input.get("pr"),
        head_sha=child_input.get("head_sha"),
        setting=setting,
        policy=policy,
        actor=actor,
        exclude_models={a["model"] for a in attempts if a.get("model")},
        exclude_accounts={a["account"] for a in attempts if a.get("account")},
        exclude_alternatives={
            a["alternative"] for a in attempts if a.get("alternative")
        },
        none_eligible=REVIEW_FAILED,
    )
    if spawned is None:
        await _escalate(
            tx,
            await tasks_svc._row(tx.conn, parent_id),
            attempts,
            actor,
            blocked=True,
        )


async def retry_reviewer(
    tx: Tx,
    parent_id: str,
    actor: str,
    forge: deliverable.ForgeRead | None = None,
) -> str | None:
    """The operator's `retry_reviewer`: select again with the failures forgotten
    (authors stay excluded). A head the last reviewer never got (or when no prior
    reviewer existed) is taken from the parent's delivery record or the forge read
    made before the write (`forge`). Idempotent when an active reviewer exists.
    Returns the child id; None leaves the parent blocked with a fresh decision."""
    from argus_api.tasks import service as tasks_svc

    parent = await tasks_svc._row(tx.conn, parent_id)
    if parent["state"] != TaskState.IN_REVIEW:
        return None
    async with tx.conn.execute(
        "SELECT id FROM tasks WHERE parent_id = ? AND kind = ?"
        " AND state NOT IN (?, ?, ?) ORDER BY rowid DESC LIMIT 1",
        (
            parent_id,
            CODE_REVIEW_KIND,
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        ),
    ) as cur:
        active = await cur.fetchone()
    if active is not None:
        return str(active["id"])
    async with tx.conn.execute(
        "SELECT input FROM tasks WHERE parent_id = ? AND kind = ?"
        " ORDER BY rowid DESC LIMIT 1",
        (parent_id, CODE_REVIEW_KIND),
    ) as cur:
        last = await cur.fetchone()
    child_input = json.loads(last["input"] or "{}") if last else {}
    pr, head_sha = child_input.get("pr"), child_input.get("head_sha")
    if head_sha is None and forge is not None and forge.delivery is not None:
        pr = forge.delivery.pr
        head_sha = _forge_head(pr, forge)
    if head_sha is None:
        parent_result = (
            TaskResult.model_validate_json(parent["result"])
            if parent["result"]
            else TaskResult(summary=".")
        )
        refs = await has_code_delivery(tx.conn, parent, parent_result, forge)
        if refs is not None:
            pr = pr or refs[0]
            head_sha = refs[1]
    await record(tx, actor, "task", parent_id, RETRY_EVENT, {})
    setting, policy = await _load_settings(tx.conn, parent["project"])
    child_id = await spawn_reviewer(
        tx,
        parent,
        pr=pr,
        head_sha=head_sha,
        setting=setting,
        policy=policy,
        actor=actor,
        none_eligible=REVIEW_FAILED,
    )
    if child_id is None:
        await _escalate(
            tx,
            await tasks_svc._row(tx.conn, parent_id),
            await failed_reviewers(tx.conn, parent_id),
            actor,
            blocked=True,
        )
    return child_id


async def approve_without_review(
    tx: Tx,
    parent_id: str,
    actor: str,
    reviewer: Any,
    *,
    decision_id: str,
    notes: str,
    forge: Any = None,
) -> None:
    """The operator's `approve_without_review`: the parent is accepted with no
    independent verdict, recorded as `review.independent_waived`."""
    from argus_api.tasks import service as tasks_svc

    await record(
        tx,
        actor,
        "task",
        parent_id,
        "review.independent_waived",
        {
            "decision_id": decision_id,
            "notes": notes,
            "failed": await failed_reviewers(tx.conn, parent_id),
        },
    )
    await tasks_svc.review(
        tx,
        parent_id,
        actor,
        reviewer,
        accept=True,
        notes=notes or "approved without independent review",
        forge=forge,
        waive_independent_review=True,
    )
    await clear_gate_block(tx, parent_id)


async def exhausted_child_of(
    conn: aiosqlite.Connection, decision: Decision
) -> str | None:
    """The unresolved review child when `decision` was opened because independent
    review exhausted its rounds, else None. Decisions opened before the payload
    existed are matched to the `review.independent_escalated` event recorded in
    the same transaction."""
    if decision.kind is not DecisionKind.REVIEW or decision.subject_type != "task":
        return None
    payload = decision.payload or {}
    if payload.get("gate") == ROUNDS_EXHAUSTED_GATE:
        child_id = payload.get("child_id")
        return child_id if isinstance(child_id, str) else ""
    if payload:
        return None
    async with conn.execute(
        "SELECT data FROM events WHERE subject_type = 'task' AND subject_id = ?"
        " AND kind = 'review.independent_escalated' AND at = ? ORDER BY id DESC",
        (decision.subject_id, ts(decision.created_at)),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    child_id = json.loads(row["data"]).get("child_id")
    return child_id if isinstance(child_id, str) else ""


async def waive_exhausted(
    tx: Tx,
    parent_id: str,
    actor: str,
    *,
    decision_id: str,
    notes: str,
    child_id: str,
) -> None:
    """`accept_with_deviation` on an exhausted-rounds decision: the independent
    review requirement is waived and the waiver is audited."""
    await record(
        tx,
        actor,
        "task",
        parent_id,
        "review.independent_waived",
        {
            "decision_id": decision_id,
            "notes": notes,
            "child_id": child_id,
            "reason": "rounds_exhausted",
        },
    )


async def on_gate_changed(tx: Tx, actor: str) -> None:
    """`review.independent` changed: a parent still waiting on a reviewer in a project
    where the gate is now off loses that reviewer (cancelled), which opens its manual
    review decision through `on_review_child_ended`."""
    from argus_api.tasks import manage

    async with tx.conn.execute(
        "SELECT c.id AS child_id, p.project AS project FROM tasks c"
        " JOIN tasks p ON p.id = c.parent_id"
        " WHERE c.kind = ? AND p.state = ? AND c.state NOT IN (?, ?, ?)",
        (
            CODE_REVIEW_KIND,
            TaskState.IN_REVIEW,
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        ),
    ) as cur:
        waiting = list(await cur.fetchall())
    for row in waiting:
        setting, _ = await _load_settings(tx.conn, row["project"])
        if setting.mode == "off":
            await manage.cancel(tx, row["child_id"], actor)


async def backfill_manual_reviews(tx: Tx) -> list[str]:
    """Startup repair: every `in_review` task with neither an open review decision nor
    an active reviewer gets the manual review decision (`reviewer_failed` when a
    reviewer child ever existed, else `no_reviewer`) and a `review.decision_backfilled`
    event. State is untouched; a second run finds nothing. Returns the task ids."""
    from argus_api.tasks import service as tasks_svc

    async with tx.conn.execute(
        "SELECT id FROM tasks WHERE state = ? AND coalesce(block_reason, '') != ?"
        " ORDER BY rowid",
        (TaskState.IN_REVIEW, AWAITING_FORGE),
    ) as cur:
        ids = [r["id"] for r in await cur.fetchall()]
    repaired: list[str] = []
    for task_id in ids:
        async with tx.conn.execute(
            "SELECT 1 FROM tasks WHERE parent_id = ? AND kind = ? LIMIT 1",
            (task_id, CODE_REVIEW_KIND),
        ) as cur:
            had_reviewer = await cur.fetchone() is not None
        gate = REVIEWER_FAILED_GATE if had_reviewer else NO_REVIEWER_GATE
        parent = await tasks_svc._row(tx.conn, task_id)
        if decision_id := await open_manual_review(tx, parent, gate, "system"):
            await record(
                tx,
                "system",
                "task",
                task_id,
                "review.decision_backfilled",
                {"decision_id": decision_id, "gate": gate},
            )
            repaired.append(task_id)
    return repaired
