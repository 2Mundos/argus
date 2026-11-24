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
    FINDINGS_REFUTED_GATE,
    GATE_DISABLED_GATE,
    NO_REVIEWER_GATE,
    POLICY_HUMAN_GATE,
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
from argus_core.execution_chains import (
    Alternative,
    Chain,
    ExecutionSelection,
    PairedAlternative,
    Selector,
)
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
from argus_api.tasks import deliverable, delivery, review_policy
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
VERIFICATION_KIND = "review_verification"
CHANGES_EVENT = "review.independent_changes_requested"
RESPONDED_EVENT = "review.findings_responded"
VERIFY_REQUESTED_EVENT = "review.finding_verification_requested"
VERIFIED_EVENT = "review.finding_verified"
VERIFY_SKIPPED_EVENT = "review.finding_verification_skipped"
REFUTED_EVENT = "review.independent_findings_refuted"
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
    review_policy.SKIPPED_EVENT,
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
    earlier approvals. A `review.policy_skipped` of a head counts as its approval."""
    kind, data = await _latest_approval_event(conn, parent_id)
    if kind not in (PASSED_EVENT, review_policy.SKIPPED_EVENT):
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
    if kind not in (PASSED_EVENT, review_policy.SKIPPED_EVENT):
        return False, "independent_review_required"
    approved_pr = data.get("pr") if isinstance(data.get("pr"), str) else None
    approved_sha = (
        data.get("head_sha") if isinstance(data.get("head_sha"), str) else None
    )
    if not approved_pr or not _same_pr(approved_pr, pr):
        return False, "independent_review_pr_mismatch"
    if not _same_sha(approved_sha, head_sha):
        return False, "independent_review_head_mismatch"
    if kind == review_policy.SKIPPED_EVENT:
        return True, None  # the policy waived the reviewer for exactly this head
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
            forge=forge,
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
            return await review_policy.with_files(
                conn,
                checker,
                task,
                deliverable.ForgeRead(await delivery.delivery_of(conn, checker, task)),
            )
        setting = await settings_svc.get_review(
            conn, policy_path or model_policy_path, row["project"]
        )
        if setting.mode == "off":
            return None
        task = (await tasks_svc.get_task(conn, task_id)).model_copy(
            update={"result": result}
        )
        return await review_policy.with_files(
            conn,
            checker,
            task,
            deliverable.ForgeRead(await delivery.delivery_of(conn, checker, task)),
        )


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


FINDING_SHAPE = (
    "Each finding is an object: `id` (short slug, unique in the result: f1, f2, …),"
    " `priority` (P1, P2 or P3), `file` (repository-relative path), optional `lines`"
    " (N or N-M), `title` (one line, up to 120 characters), `description` (up to 2000"
    " characters), `evidence` {`kind`, `detail`} and, only for a finding raised again,"
    " `repeat_of` (`<child id>:<finding id>` of the earlier round's finding). Evidence"
    " `kind` is `test` (a failing test or command and its output), `reproduction`"
    " (steps run and what was seen) or `reading` (code reasoning only); `detail` is up"
    " to 2000 characters. A string finding, or a missing or empty field, is refused."
)
RESPONSE_SHAPE = (
    "Answer every finding by id in `data.review_responses` of your next COMPLETE:"
    ' `[{"id":"f1","status":"fixed"|"disputed","evidence":"..."}]`. `fixed` names the'
    " test or check that shows it; `disputed` explains why the finding is wrong, with a"
    " test, command output or code reference. A finding left unanswered is recorded as"
    " unanswered."
)


def check_findings(kind: str, data: dict[str, Any] | None) -> None:
    """Finding ids are unique in one result (review finding verification spec 3.1)."""
    if kind != CODE_REVIEW_KIND or not data:
        return
    ids = [
        f["id"]
        for f in data.get("findings") or []
        if isinstance(f, dict) and isinstance(f.get("id"), str)
    ]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise ValidationFailedError(
            "result does not match its kind's schema",
            details=[f"findings: id {i} is used more than once" for i in dup],
        )


def take_responses(
    kind: str, result: TaskResult
) -> tuple[TaskResult, list[Any] | None]:
    """Split `data.review_responses` off an implementer's result (spec 3.2): it is not
    part of any kind's result schema. None when the result carries none."""
    if (
        kind in (CODE_REVIEW_KIND, VERIFICATION_KIND)
        or not isinstance(result.data, dict)
        or "review_responses" not in result.data
    ):
        return result, None
    data = dict(result.data)
    raw = data.pop("review_responses")
    return result.model_copy(update={"data": data}), raw if isinstance(
        raw, list
    ) else []


def _result_data(raw: str | None) -> dict[str, Any]:
    try:
        data = (json.loads(raw) if raw else {}).get("data")
    except (json.JSONDecodeError, AttributeError):
        return {}
    return data if isinstance(data, dict) else {}


def _structured(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for f in data.get("findings") or [] if isinstance(f, dict)]


async def _review_children(
    conn: aiosqlite.Connection, parent_id: str
) -> list[aiosqlite.Row]:
    """The parent's `code_review` children, oldest first (round 1)."""
    async with conn.execute(
        "SELECT id, state, result FROM tasks WHERE parent_id = ? AND kind = ?"
        " ORDER BY rowid",
        (parent_id, CODE_REVIEW_KIND),
    ) as cur:
        return list(await cur.fetchall())


async def _event_data(
    conn: aiosqlite.Connection, parent_id: str, *kinds: str
) -> list[tuple[str, dict[str, Any]]]:
    marks = ",".join("?" * len(kinds))
    async with conn.execute(
        "SELECT kind, data FROM events WHERE subject_type = 'task' AND subject_id = ?"  # noqa: S608
        f" AND kind IN ({marks}) ORDER BY id",
        (parent_id, *kinds),
    ) as cur:
        out = []
        for r in await cur.fetchall():
            try:
                data = json.loads(r["data"] or "{}")
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                out.append((r["kind"], data))
        return out


async def responses_by_child(
    conn: aiosqlite.Connection, parent_id: str
) -> dict[str, dict[str, dict[str, Any]]]:
    """child id -> finding id -> `{status, evidence}` the implementer answered."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for _, data in await _event_data(conn, parent_id, RESPONDED_EVENT):
        if isinstance(data.get("child_id"), str) and isinstance(
            data.get("responses"), dict
        ):
            out[data["child_id"]] = data["responses"]
    return out


async def record_responses(
    tx: Tx, row: aiosqlite.Row, raw: list[Any] | None, actor: str
) -> None:
    """Record the implementer's answers to the findings last sent back (spec 3.2).

    One entry per finding: a missing one is `unanswered` and never refuses the
    completion. Nothing is recorded when no round's findings await an answer."""
    pending = None
    for kind, data in await _event_data(
        tx.conn, row["id"], CHANGES_EVENT, RESPONDED_EVENT
    ):
        if kind == CHANGES_EVENT:
            pending = data.get("child_id")
        elif data.get("child_id") == pending:
            pending = None
    if not isinstance(pending, str):
        return
    async with tx.conn.execute(
        "SELECT result FROM tasks WHERE id = ?", (pending,)
    ) as cur:
        child = await cur.fetchone()
    findings = _structured(_result_data(child["result"] if child else None))
    given: dict[str, dict[str, Any]] = {}
    for entry in raw or []:
        if (
            isinstance(entry, dict)
            and entry.get("status") in ("fixed", "disputed")
            and isinstance(entry.get("id"), str)
            and entry["id"] not in given
        ):
            given[entry["id"]] = {
                "status": entry["status"],
                "evidence": str(entry.get("evidence") or "")[:2000],
            }
    responses = {
        f["id"]: given.get(f["id"], {"status": "unanswered", "evidence": ""})
        for f in findings
        if isinstance(f.get("id"), str)
    }
    await record(
        tx,
        actor,
        "task",
        row["id"],
        RESPONDED_EVENT,
        {"child_id": pending, "responses": responses},
    )


def _finding_lines(
    f: dict[str, Any],
    rounds: dict[str, int],
    responses: dict[str, dict[str, dict[str, Any]]],
) -> list[str]:
    where = f"{f.get('file', '?')}" + (f":{f['lines']}" if f.get("lines") else "")
    evidence = f.get("evidence") if isinstance(f.get("evidence"), dict) else {}
    head = f"- [{f['id']}] {f.get('priority', '?')} `{where}`: {f.get('title', '')}"
    lines = [head, f"  {f.get('description', '')}"]
    if evidence:
        lines.append(f"  Evidence ({evidence.get('kind')}): {evidence.get('detail')}")
    earlier = str(f.get("repeat_of") or "")
    if earlier:
        child, _, fid = earlier.partition(":")
        before = responses.get(child, {}).get(fid, {}).get("status", "unanswered")
        n = rounds.get(child)
        lines.append(
            f"  Raised again (round {n if n else '?'}); your earlier response: {before}."
        )
    return lines


async def _findings_text(
    conn: aiosqlite.Connection,
    parent_id: str,
    findings: list[Any],
    refuted: list[tuple[dict[str, Any], str]] | None = None,
) -> str:
    """The Review findings section handed to the implementer: ids, responses to earlier
    rounds, repeats marked, and the findings the verifier refuted (spec 3.2, 3.3)."""
    rounds = {
        c["id"]: n for n, c in enumerate(await _review_children(conn, parent_id), 1)
    }
    responses = await responses_by_child(conn, parent_id)
    lines: list[str] = []
    for f in findings:
        if isinstance(f, dict) and isinstance(f.get("id"), str):
            lines += _finding_lines(f, rounds, responses)
        elif isinstance(f, dict):
            lines.append(f"- {json.dumps(f, ensure_ascii=False)}")
        else:
            lines.append(f"- {f}")
    text = "\n".join(lines) if lines else "- (no details)"
    if any(isinstance(f, dict) and "id" in f for f in findings):
        text += f"\n\n{RESPONSE_SHAPE}"
    if refuted:
        text += (
            "\n\nRefuted by the verifier (not required; listed with the refutation):\n"
            + "\n".join(
                f"- [{f['id']}] {f.get('priority', '?')} {f.get('title', '')}: {why}"
                for f, why in refuted
            )
        )
    return text


async def _prior_rounds_text(conn: aiosqlite.Connection, parent_id: str) -> str:
    """What the next reviewer receives: the earlier findings and the responses (spec 3.2)."""
    responses = await responses_by_child(conn, parent_id)
    blocks: list[str] = []
    for n, child in enumerate(await _review_children(conn, parent_id), 1):
        data = _result_data(child["result"])
        if (
            child["state"] != TaskState.SUCCEEDED
            or data.get("verdict") != "request_changes"
        ):
            continue
        lines = []
        for f in _structured(data):
            if not isinstance(f.get("id"), str):
                continue
            got = responses.get(child["id"], {}).get(f["id"])
            answer = (
                f"{got['status']}: {got['evidence']}".rstrip(": ")
                if got
                else "no response yet"
            )
            lines.append(
                f"- `{child['id']}:{f['id']}` {f.get('priority', '?')} {f.get('title', '')}"
                f" (`{f.get('file', '?')}`) — implementer: {answer}"
            )
        if lines:
            blocks.append(f"Round {n}:\n" + "\n".join(lines))
    if not blocks:
        return ""
    return (
        "\n\nEarlier rounds of this review, with the implementer's responses:\n\n"
        + "\n\n".join(blocks)
        + "\n\nDrop a finding the implementer disputed, or raise it again with"
        " `repeat_of` (`<child id>:<finding id>` as listed above) and new evidence."
        " Do not raise a disputed finding again without new evidence."
    )


async def _send_findings(
    tx: Tx,
    parent: aiosqlite.Row,
    child: aiosqlite.Row,
    findings: list[Any],
    rounds: int,
    actor: str,
    *,
    refuted: list[tuple[dict[str, Any], str]] | None = None,
) -> None:
    """Send a request_changes verdict's findings to the implementer (a new attempt)."""
    from argus_api.tasks import service as tasks_svc

    notes = await _findings_text(tx.conn, parent["id"], findings, refuted)
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
        parent["id"],
        CHANGES_EVENT,
        {"child_id": child["id"], "round": rounds},
    )


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
        "SELECT 1 FROM tasks WHERE parent_id = ? AND kind IN (?, ?)"
        " AND state NOT IN (?, ?, ?) LIMIT 1",
        (
            parent_id,
            CODE_REVIEW_KIND,
            VERIFICATION_KIND,
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


_PAIRED_GUIDANCE = (
    "\n\nThis review runs on a delegated executor. You must perform the review"
    " through it with `argus-delegate` and must not review the code yourself: do not"
    " read the diff to form a verdict, and do not decide on your own. Pass the"
    " executor's findings and verdict on in your COMPLETE. A verdict given without a"
    " succeeded delegate execution is void and the review is repeated."
)


def _paired_route(
    route: tuple[list[Alternative], routes.ReviewRoute] | None,
) -> bool:
    return (
        route is not None
        and bool(route[0])
        and isinstance(route[0][0], PairedAlternative)
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
    forge: deliverable.ForgeRead | None = None,
) -> str | None:
    """Create a code_review child, or set block_reason when none is eligible
    (`none_eligible`: the reason when no alternative remains).

    With `review.policy` enforced, a head the policy sorts as `skip` gets no reviewer:
    its `review.policy_skipped` stands as the approval of that head. `forge` carries
    the changed paths read before the write; without them the tier is the default.

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
    tier = await review_policy.decide(
        tx, parent, pr=pr, head=sha, forge=forge, actor=actor
    )
    if tier is not None and tier.skip:
        await _policy_skip(tx, parent, active_children, tier, pr, sha, forge, actor)
        return None
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
            '"findings":[<finding objects>],"head_sha":"<the full sha you reviewed>"}}`\n'
            "`verdict` is `approve`, `request_changes` or `fixed`. `data` takes only"
            " verdict, findings, head_sha, fixed_head_sha and tests; do not put them at"
            " the top level.\n\n"
            "You are a verdict-only reviewer. Do not change the repository or the"
            " pull request: no commit, no push (not to the PR's branch, not to any"
            " other), no `argus-task deliver`, no PR create, update, close or reopen,"
            " no merge. The verdict you return is bound to head `" + sha + "`;"
            " if that head moves while you review, your verdict is void. Report"
            " problems as findings and return `request_changes`; with no findings,"
            " return `approve`. `fixed` is refused.\n\n"
            "Keep the context small: `argus-github-api` prints a compact projection by"
            " default; do not use `--raw`. Read the diff with `pr-diff` or `git diff`,"
            " not by pasting API payloads. Run only the targeted tests that cover the"
            " change, each wrapped in `timeout 300`; never run a whole test suite"
            " (for example all of `argus-delegate`, `make test` or `make check`). If a"
            " command hangs or runs over its timeout, stop it and report it as a"
            " finding or in `tests`, then return your verdict.\n"
            f"\n\n{FINDING_SHAPE} When findings may be checked by a verifier, P1"
            " findings with only `reading` evidence are checked first: run a test or"
            " reproduce the problem whenever you can."
            + await _prior_rounds_text(tx.conn, parent["id"])
            + (_PAIRED_GUIDANCE if _paired_route(route) else "")
        ),
        kind=CODE_REVIEW_KIND,
        project=parent["project"],
        parent_id=parent["id"],
        input={
            "task_id": parent["id"],
            "pr": pr,
            "head_sha": sha,
        },
        review=Review.NONE,
        harness_model=harness,
        executor_model=executor,
        # Verdict-only: no write capability, so no push to the parent's branch and no
        # PR create or close; a reviewer is a response, never a delivery.
        required_capabilities=[],
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


async def _after_approval(
    tx: Tx,
    parent: aiosqlite.Row,
    forge: deliverable.ForgeRead | None,
    reviewed: str | None,
    reviewed_pr: str | None,
    actor: str,
) -> None:
    """What follows an approval of `reviewed` (a reviewer's, or a policy skip): a
    `human` head under enforced policy goes to a person; otherwise the parent settles
    on the forge's word, or the regular review decision opens."""
    from argus_api.tasks import service as tasks_svc

    parent_id = parent["id"]
    if parent["block_reason"] in (NO_INDEPENDENT_REVIEWER, REVIEW_FAILED):
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
    if human := await review_policy.human_gate(tx.conn, parent, reviewed_pr, reviewed):
        # The person merges on the forge and accepts, or rejects; Argus starts no
        # merge continuation for this head (review policy spec 3.4).
        if not await decisions.open_ids(
            tx.conn, "task", parent_id, (DecisionKind.REVIEW,)
        ):
            await decisions.open_decision(
                tx,
                subject_type="task",
                subject_id=parent_id,
                kind=DecisionKind.REVIEW,
                question=(
                    f"The review policy asks a person to decide {parent['title']!r}"
                    f" ({', '.join(human.rules) or 'default'}). Merge it on the forge"
                    " and accept, or reject."
                ),
                options=list(REVIEW_OPTIONS),
                audience=Audience.HUMAN,
                requested_by=actor,
                payload={
                    "gate": POLICY_HUMAN_GATE,
                    "rules": human.rules,
                    "pr": reviewed_pr,
                    "head_sha": reviewed,
                },
            )
        return
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


async def _policy_skip(
    tx: Tx,
    parent: aiosqlite.Row,
    active_children: list[aiosqlite.Row],
    tier: review_policy.Decision,
    pr: str | None,
    sha: str,
    forge: deliverable.ForgeRead | None,
    actor: str,
) -> None:
    """Enforced `skip`: no reviewer for this head. Older active reviewers are
    superseded, and the skip stands as the approval of exactly this (pr, head)."""
    from argus_api.tasks import manage

    for stale in active_children:
        await manage.cancel(tx, stale["id"], actor)
    await record(
        tx,
        actor,
        "task",
        parent["id"],
        review_policy.SKIPPED_EVENT,
        {"pr": pr, "head_sha": sha, "rules": tier.rules},
    )
    from argus_api.tasks import service as tasks_svc

    await _after_approval(
        tx, await tasks_svc._row(tx.conn, parent["id"]), forge, sha, pr, actor
    )


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
    from argus_api.tasks import merge_resume, steward

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
            parent = await tasks_svc._row(tx.conn, row["id"])
            if not await steward.step(tx, parent, forge, pr, head_sha, actor):
                await merge_resume.open_unmerged_decision(tx, parent, forge, actor)
        return True
    # A steward repair that moved the head: note it, then review the new head below.
    await steward.record_pushed(tx, row["id"], pr, head_sha, actor)
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
        forge=forge,
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


async def _paired_alternative(
    conn: aiosqlite.Connection, child: aiosqlite.Row
) -> PairedAlternative | None:
    """The paired alternative the review child ran on, when it ran on one."""
    async with conn.execute(
        "SELECT selection FROM task_execution_routes WHERE task_id = ?", (child["id"],)
    ) as cur:
        picked = await cur.fetchone()
    alternative: Alternative | None = None
    if picked and picked["selection"]:
        alternative = ExecutionSelection.model_validate_json(
            picked["selection"]
        ).alternative
    else:
        async with conn.execute(
            "SELECT execution_route FROM tasks WHERE id = ?", (child["id"],)
        ) as cur:
            row = await cur.fetchone()
        route = (
            json.loads(row["execution_route"]) if row and row["execution_route"] else {}
        )
        from pydantic import TypeAdapter

        selected = route.get("review", {}).get("selected")
        for raw in route.get("alternatives", []):
            if raw.get("id") == selected:
                alternative = TypeAdapter(Alternative).validate_python(raw)
    return alternative if isinstance(alternative, PairedAlternative) else None


async def executor_not_run(conn: aiosqlite.Connection, child: aiosqlite.Row) -> bool:
    """True when a paired reviewer's verdict is void: no succeeded `delegate` execution
    matches the alternative's executor (adapter, provider and, when explicit, model)
    in the child's current attempt; an earlier attempt's delegate run proves nothing.

    The harness of a paired alternative only coordinates; a verdict the harness
    produced itself was not given by the executor that was compared for independence.
    """
    paired = await _paired_alternative(conn, child)
    if paired is None:
        return False
    want = paired.executor
    async with conn.execute(
        "SELECT executor, provider, observed_model, configured_model"
        " FROM model_executions WHERE task_id = ? AND layer = 'delegate'"
        " AND state = 'succeeded' AND attempt = ?",
        (child["id"], child["attempt"]),
    ) as cur:
        rows = await cur.fetchall()
    for r in rows:
        if r["executor"] != want.adapter or r["provider"] != want.provider:
            continue
        if want.model.source == "explicit" and want.model.id not in (
            r["observed_model"],
            r["configured_model"],
        ):
            continue
        return False
    return True


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
    CHANGES_EVENT,
    REFUTED_EVENT,
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
            forge=forge,
            exclude_models=author_models | child_models,
            exclude_accounts=await _accounts(tx.conn, child["id"]),
            replace_child_id=child["id"],
        )
        return

    if await executor_not_run(tx.conn, child):
        # The paired executor never ran: the harness reviewed by itself, so the
        # verdict is void. Select again without this alternative.
        child_facts = await _child_facts(tx.conn, child)
        await record(
            tx,
            actor,
            "task",
            parent_id,
            "review.independent_void",
            {
                "reason": "executor_not_run",
                "child_id": child["id"],
                "alternative": child_facts["alternative"],
            },
        )
        await spawn_reviewer(
            tx,
            parent,
            pr=child_input.get("pr"),
            head_sha=child_input.get("head_sha"),
            setting=setting,
            policy=policy,
            actor=actor,
            forge=forge,
            exclude_alternatives={child_facts["alternative"]}
            if child_facts["alternative"]
            else None,
            exclude_accounts=await _accounts(tx.conn, child["id"]),
            replace_child_id=child["id"],
        )
        return

    if verdict == FIX_VERDICT:
        # Reviewers are verdict-only (spec 3.7): they hold no write grant, so a fix is
        # a write Argus never authorized and the verdict does not count.
        await _fix_refused(tx, parent, child, "fix_not_permitted", actor)
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
                forge=forge,
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
                forge=forge,
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
                forge=forge,
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
                forge=forge,
                replace_child_id=child["id"],
            )
            return
        if parent["block_reason"] in (NO_INDEPENDENT_REVIEWER, REVIEW_FAILED):
            await tx.conn.execute(
                "UPDATE tasks SET block_reason = NULL WHERE id = ?", (parent_id,)
            )
        reviewed = (head_sha or expected_sha or "").lower() or None
        reviewed_pr = expected_pr or parent_pr or forge_pr
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
        await _after_approval(tx, parent, forge, reviewed, reviewed_pr, actor)
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
        listed = findings if isinstance(findings, list) else [findings]
        verifier_id = None
        if setting.verify_findings != "off" and (
            setting.verify_findings == "observe" or rounds < setting.max_rounds
        ):
            verifier_id = await spawn_verifier(
                tx,
                parent,
                child,
                [f for f in listed if isinstance(f, dict)],
                child_input,
                setting=setting,
                policy=policy,
                actor=actor,
            )
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
        if verifier_id is not None and setting.verify_findings == "enforce":
            return  # the verdict waits for the verifier (spec 3.4)
        await _send_findings(tx, parent, child, listed, rounds, actor)
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


async def _current_target(
    conn: aiosqlite.Connection,
    parent: aiosqlite.Row,
    child_input: dict[str, Any],
    forge: deliverable.ForgeRead | None = None,
) -> tuple[str | None, str | None]:
    """(pr, head_sha) a respawned reviewer targets: the parent's current delivery
    when it names a different PR with a head (a follow-up PR bound after the ended
    reviewer was spawned), else what the ended reviewer was assigned."""
    old_pr, old_sha = child_input.get("pr"), child_input.get("head_sha")
    result = (
        TaskResult.model_validate_json(parent["result"])
        if parent["result"]
        else TaskResult(summary=".")
    )
    refs = await has_code_delivery(conn, parent, result, forge)
    if refs is None:
        return old_pr, old_sha
    pr, sha = refs
    if pr and sha and (not old_pr or not _same_pr(old_pr, pr)):
        return pr, sha
    return old_pr, old_sha


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
    target_pr, target_sha = await _current_target(tx.conn, parent, child_input)
    spawned = await spawn_reviewer(
        tx,
        parent,
        pr=target_pr,
        head_sha=target_sha,
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
    pr, head_sha = await _current_target(tx.conn, parent, child_input)
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
    reason: str = "rounds_exhausted",
) -> None:
    """`accept_with_deviation` on an exhausted-rounds or findings-refuted decision: the
    independent review requirement is waived and the waiver is audited."""
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
            "reason": reason,
        },
    )


async def _skip_verification(
    tx: Tx,
    parent_id: str,
    review_id: str,
    verifier_id: str | None,
    reason: str,
    actor: str,
) -> None:
    await record(
        tx,
        actor,
        "task",
        parent_id,
        VERIFY_SKIPPED_EVENT,
        {"review_child_id": review_id, "verifier_id": verifier_id, "reason": reason},
    )


async def spawn_verifier(
    tx: Tx,
    parent: aiosqlite.Row,
    review: aiosqlite.Row,
    findings: list[dict[str, Any]],
    review_input: dict[str, Any],
    *,
    setting: IndependentReviewSetting,
    policy: model_policy.Policy,
    actor: str,
) -> str | None:
    """Create the read-only `review_verification` child of a request_changes verdict
    (review finding verification spec 3.4); None, with a skipped event, when no
    verifier is eligible. Its model is neither an author's nor the reviewer's."""
    from argus_api.tasks import service as tasks_svc

    head_sha = review_input.get("head_sha")
    if not findings or not head_sha:
        await _skip_verification(
            tx, parent["id"], review["id"], None, "no_findings", actor
        )
        return None
    author_models, author_providers = await authors(tx.conn, parent["id"])
    fix_models, _ = await fixers(tx.conn, parent["id"])
    reviewer_models, _ = await authors(tx.conn, review["id"])
    excluded = author_models | fix_models | reviewer_models
    route = await review_route(
        tx.conn,
        tx.now,
        parent,
        mode=setting.mode,
        models=excluded,
        providers=author_providers,
        accounts=await _accounts(tx.conn, parent["id"]),
    )
    harness: HarnessModel | None = None
    executor: ExecutorModel | None = None
    if route is not None:
        alternatives, _ = route
        if not alternatives:
            await _skip_verification(
                tx, parent["id"], review["id"], None, "no_eligible_verifier", actor
            )
            return None
    else:
        harness, executor = pick_reviewer(
            policy,
            mode=setting.mode,
            author_models=excluded,
            author_providers=author_providers,
            usable=await usable_providers(tx.conn, ts(tx.now)),
        )
        if harness is None and executor is None:
            await _skip_verification(
                tx, parent["id"], review["id"], None, "no_eligible_verifier", actor
            )
            return None
    responses = await responses_by_child(tx.conn, parent["id"])
    disputes = []
    for f in findings:
        child_id, _, fid = str(f.get("repeat_of") or "").partition(":")
        earlier = responses.get(child_id, {}).get(fid)
        if earlier and earlier["status"] == "disputed":
            disputes.append(
                f"- `{f.get('id')}` repeats `{child_id}:{fid}`, which the implementer"
                f" disputed: {earlier['evidence']}"
            )
    # P1 findings with only `reading` evidence are checked first (spec 3.1).
    ordered = sorted(
        findings,
        key=lambda f: (
            not (
                f.get("priority") == "P1"
                and (f.get("evidence") or {}).get("kind") == "reading"
            )
        ),
    )
    body = TaskCreate(
        title=f"Verification of review findings of {parent['title']!r}"[:200],
        spec=(
            f"Verify the findings of independent review `{review['id']}` of task"
            f" `{parent['id']}` (head `{head_sha}`). You are read-only: change,"
            " commit and push nothing. Try to refute each finding: run its evidence,"
            " read the code, and say what you ran or read and what you saw. Check"
            " the findings in the order given.\n\nFinish with `COMPLETE:` and one JSON"
            ' object, sent with the send_message tool to `to: "argus"`:\n'
            '`COMPLETE: {"summary":"<one line>","data":{"verifications":'
            '[{"id":"f1","status":"confirmed","evidence":"<what you ran or read>"}]}}`\n'
            "`status` is `confirmed` (you could not refute it), `refuted` (it is"
            " wrong; the evidence shows why) or `unclear` (you could not decide)."
            " Return one entry per finding id.\n\nFindings:\n"
            + json.dumps(ordered, ensure_ascii=False, indent=1)
            + ("\n\nDisputes:\n" + "\n".join(disputes) if disputes else "")
            + (_PAIRED_GUIDANCE if _paired_route(route) else "")
        ),
        kind=VERIFICATION_KIND,
        project=parent["project"],
        parent_id=parent["id"],
        input={
            "review_child_id": review["id"],
            "pr": review_input.get("pr"),
            "head_sha": head_sha,
            "findings": ordered,
        },
        review=Review.NONE,
        harness_model=harness,
        executor_model=executor,
        required_capabilities=[],  # read-only: never repository:write
        deliverable=Deliverable.RESPONSE,
    )
    child = await tasks_svc.create_task(tx, body, actor)
    if route is not None:
        alternatives, review_route_ = route
        await tx.conn.execute(
            "UPDATE tasks SET execution_route = ? WHERE id = ?",
            (routes.explicit_route(alternatives, review_route_), child.id),
        )
    await record(
        tx,
        actor,
        "task",
        parent["id"],
        VERIFY_REQUESTED_EVENT,
        {
            "review_child_id": review["id"],
            "verifier_id": child.id,
            "mode": setting.verify_findings,
        },
    )
    return child.id


async def _held_for(
    conn: aiosqlite.Connection, parent: aiosqlite.Row, review_id: str, verifier_id: str
) -> bool:
    """True while an `enforce` verdict still waits for this verifier."""
    if parent["state"] != TaskState.IN_REVIEW:
        return False
    mode = None
    for _, data in await _event_data(conn, parent["id"], VERIFY_REQUESTED_EVENT):
        if data.get("verifier_id") == verifier_id:
            mode = data.get("mode")
    if mode != "enforce":
        return False
    async with conn.execute(
        "SELECT id FROM tasks WHERE parent_id = ? AND kind = ? ORDER BY rowid DESC LIMIT 1",
        (parent["id"], CODE_REVIEW_KIND),
    ) as cur:
        latest = await cur.fetchone()
    return (
        latest is not None
        and latest["id"] == review_id
        and not await _child_already_processed(conn, parent["id"], review_id)
    )


async def _verifier_clean(
    conn: aiosqlite.Connection,
    parent: aiosqlite.Row,
    verifier: aiosqlite.Row,
    review_id: str,
) -> bool:
    """True when the verifier's runs prove it used neither an author's nor the
    reviewer's model."""
    models, _ = await authors(conn, verifier["id"])
    author_models, _ = await authors(conn, parent["id"])
    fix_models, _ = await fixers(conn, parent["id"])
    reviewer_models, _ = await authors(conn, review_id)
    return models.isdisjoint(author_models | fix_models | reviewer_models) and (
        bool(models) or not author_models
    )


async def _apply_verdict(
    tx: Tx,
    parent: aiosqlite.Row,
    review: aiosqlite.Row,
    findings: list[dict[str, Any]],
    actor: str,
    *,
    refuted: dict[str, str] | None = None,
    verifier_id: str | None = None,
) -> None:
    """Resolve a held `enforce` verdict: confirmed and unclear findings go to the
    implementer; when every finding is refuted a person decides (spec 3.4)."""
    rounds = await review_rounds(tx.conn, parent["id"])
    refuted = refuted or {}
    send = [f for f in findings if f.get("id") not in refuted]
    if send:
        await _send_findings(
            tx,
            parent,
            review,
            send,
            rounds,
            actor,
            refuted=[(f, refuted[f["id"]]) for f in findings if f.get("id") in refuted],
        )
        return
    both = "\n".join(
        f"- [{f.get('id')}] {f.get('priority', '?')} {f.get('title', '')}\n"
        f"  Reviewer ({(f.get('evidence') or {}).get('kind')}):"
        f" {(f.get('evidence') or {}).get('detail')}\n"
        f"  Verifier (refuted): {refuted[f['id']]}"
        for f in findings
    )
    await decisions.open_decision(
        tx,
        subject_type="task",
        subject_id=parent["id"],
        kind=DecisionKind.REVIEW,
        question=(
            f"The verifier refuted every finding of the independent review of"
            f" {parent['title']!r}. Reject sends the findings to the implementer"
            " anyway; accept_with_deviation waives the independent review; accept"
            f" needs an approval at this head.\n\n{both}"
        ),
        options=list(REVIEW_OPTIONS),
        audience=Audience.HUMAN,
        requested_by=actor,
        payload={
            "gate": FINDINGS_REFUTED_GATE,
            "review_child_id": review["id"],
            "verifier_id": verifier_id,
        },
    )
    await record(
        tx,
        actor,
        "task",
        parent["id"],
        REFUTED_EVENT,
        {"child_id": review["id"], "verifier_id": verifier_id},
    )


async def _review_row(
    conn: aiosqlite.Connection, review_id: Any
) -> aiosqlite.Row | None:
    if not isinstance(review_id, str):
        return None
    async with conn.execute(
        "SELECT * FROM tasks WHERE id = ? AND kind = ?", (review_id, CODE_REVIEW_KIND)
    ) as cur:
        return await cur.fetchone()


async def on_verification_complete(
    tx: Tx, child: aiosqlite.Row, result: TaskResult, actor: str
) -> None:
    """The verifier answered: record each verdict on a finding (`observe` and `enforce`);
    in `enforce` also apply the held verdict. An unusable answer leaves the original
    verdict as it was and records why (spec 3.4)."""
    from argus_api.tasks import service as tasks_svc

    parent_id = child["parent_id"]
    inp = json.loads(child["input"] or "{}")
    review = await _review_row(tx.conn, inp.get("review_child_id"))
    if not parent_id or review is None:
        return
    parent = await tasks_svc._row(tx.conn, parent_id)
    held = await _held_for(tx.conn, parent, review["id"], child["id"])
    findings = [f for f in inp.get("findings") or [] if isinstance(f, dict)]
    by_id = {f.get("id"): f for f in findings}
    answers: dict[str, dict[str, Any]] = {}
    for a in (result.data or {}).get("verifications") or []:
        if isinstance(a, dict) and a.get("id") in by_id and a["id"] not in answers:
            answers[a["id"]] = a
    reason = None
    if not answers:
        reason = "no_answer"
    elif not await _verifier_clean(tx.conn, parent, child, review["id"]):
        reason = "same_model"
    elif await executor_not_run(tx.conn, child):
        reason = "executor_not_run"
    if reason:
        await _skip_verification(
            tx, parent_id, review["id"], child["id"], reason, actor
        )
        if held:
            await _apply_verdict(
                tx, parent, review, _structured(_result_data(review["result"])), actor
            )
        return
    for f in findings:
        if f.get("id") in answers:
            await record(
                tx,
                actor,
                "task",
                parent_id,
                VERIFIED_EVENT,
                {
                    "review_child_id": review["id"],
                    "verifier_id": child["id"],
                    "id": f["id"],
                    "priority": f.get("priority"),
                    "status": answers[f["id"]]["status"],
                },
            )
    if held:
        await _apply_verdict(
            tx,
            parent,
            review,
            findings,
            actor,
            refuted={
                i: str(a["evidence"])
                for i, a in answers.items()
                if a["status"] == "refuted"
            },
            verifier_id=child["id"],
        )


async def on_verification_ended(tx: Tx, child: aiosqlite.Row, actor: str) -> None:
    """The verifier failed or was cancelled without an answer: the original verdict
    applies unchanged and the skip is recorded (spec 3.4)."""
    from argus_api.tasks import service as tasks_svc

    parent_id = child["parent_id"]
    inp = json.loads(child["input"] or "{}")
    review = await _review_row(tx.conn, inp.get("review_child_id"))
    if not parent_id or review is None:
        return
    parent = await tasks_svc._row(tx.conn, parent_id)
    held = await _held_for(tx.conn, parent, review["id"], child["id"])
    await _skip_verification(
        tx, parent_id, review["id"], child["id"], "verifier_failed", actor
    )
    if held:
        await _apply_verdict(
            tx, parent, review, _structured(_result_data(review["result"])), actor
        )


async def release_refuted(
    tx: Tx, parent_id: str, review_child_id: str, actor: str
) -> None:
    """`reject` on a findings_refuted decision: the findings go to the implementer
    anyway (spec 3.4)."""
    from argus_api.tasks import service as tasks_svc

    parent = await tasks_svc._row(tx.conn, parent_id)
    review = await _review_row(tx.conn, review_child_id)
    if review is None or parent["state"] != TaskState.IN_REVIEW:
        return
    await _send_findings(
        tx,
        parent,
        review,
        _structured(_result_data(review["result"])),
        await review_rounds(tx.conn, parent_id),
        actor,
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
