"""Path-based review policy (docs/specs/review-policy.spec.md).

Sorts each delivered head into a tier (`skip`, `standard`, `human`) from the forge's
changed paths at that exact head. Evaluation is deterministic and in process; the
forge read happens before the write (`with_files`) and is handed in. Nothing here
takes a path from task text, worker progress or the task's change snapshot.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import aiosqlite
from argus_core.settings import TIER_ORDER, ReviewPolicySetting, ReviewTier
from pydantic import BaseModel, Field

from argus_api.db import Tx
from argus_api.events import record
from argus_api.tasks import deliverable, delivery

EVALUATED_EVENT = "review.policy_evaluated"
SKIPPED_EVENT = "review.policy_skipped"
UNMATCHED_CAP = 20
FILES_UNKNOWN = "files_unknown"
DEFAULT_RULE = "default"


@lru_cache(maxsize=512)
def compile_glob(glob: str) -> re.Pattern[str]:
    """`*` within one segment, `**` zero or more whole segments, `?` one character."""
    segments = glob.split("/")
    out: list[str] = []
    for i, seg in enumerate(segments):
        last = i == len(segments) - 1
        if seg == "**":
            out.append(r"[^/]+(?:/[^/]+)*" if last else r"(?:[^/]+/)*")
            continue
        out.append(
            "".join(
                "[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c)
                for c in seg
            )
        )
        if not last:
            out.append("/")
    return re.compile("".join(out))


def matches(glob: str, path: str) -> bool:
    return compile_glob(glob).fullmatch(path) is not None


def _stricter(a: ReviewTier, b: ReviewTier) -> ReviewTier:
    return a if TIER_ORDER.index(a) >= TIER_ORDER.index(b) else b


@dataclass(frozen=True)
class Evaluation:
    tier: ReviewTier
    files: int
    matched: list[dict[str, Any]] = field(default_factory=list)  # {rule, tier, count}
    unmatched: list[str] = field(default_factory=list)
    reason: str | None = None


def rules_of(data: dict[str, Any], default: ReviewTier) -> list[str]:
    """The rules that set a recorded evaluation's tier (`default` when an unlisted
    path or an unknown list did)."""
    tier = data.get("tier")
    names = [m["rule"] for m in data.get("matched") or [] if m["tier"] == tier]
    if (data.get("unmatched") or data.get("reason")) and tier == default:
        names.append(DEFAULT_RULE)
    return names


def evaluate(policy: ReviewPolicySetting, paths: Sequence[str] | None) -> Evaluation:
    """Each path takes the strictest tier of the rules that match it, else `default`;
    the head takes the strictest over its paths. Unknown or empty takes `default`."""
    if not paths:
        return Evaluation(
            tier=policy.default,
            files=0,
            reason=FILES_UNKNOWN if paths is None else None,
        )
    counts: dict[str, int] = {}
    tier: ReviewTier = "skip"
    unmatched: list[str] = []
    for path in paths:
        path_tier: ReviewTier | None = None
        for rule in policy.rules:
            if any(matches(g, path) for g in rule.paths):
                counts[rule.name] = counts.get(rule.name, 0) + 1
                path_tier = (
                    rule.tier if path_tier is None else _stricter(path_tier, rule.tier)
                )
        if path_tier is None:
            path_tier = policy.default
            if len(unmatched) < UNMATCHED_CAP:
                unmatched.append(path)
        tier = _stricter(tier, path_tier)
    by_name = {r.name: r for r in policy.rules}
    matched = [
        {"rule": name, "tier": by_name[name].tier, "count": n}
        for name, n in counts.items()
    ]
    return Evaluation(tier, len(paths), matched, unmatched)


async def with_files(
    conn: aiosqlite.Connection,
    checker: delivery.PrChecker,
    task: Any,
    read: deliverable.ForgeRead | None,
) -> deliverable.ForgeRead | None:
    """`read` plus the PR's changed paths at its head, when the project's policy and
    independent review are on. Called with a read connection, outside any write."""
    from argus_api.settings import service as settings_svc
    from argus_api.tasks import independent_review as indep

    seen = read.delivery if read is not None else None
    if seen is None or seen.head is None or not seen.head.sha:
        return read
    if (await settings_svc.get_review_policy(conn, task.project)).mode == "off":
        return read
    setting = await settings_svc.get_review(conn, indep.model_policy_path, task.project)
    if setting.mode == "off" or task.kind == indep.CODE_REVIEW_KIND:
        return read
    ref = await delivery.reference(conn, task)
    if ref is None:
        return read
    files = await checker.changed_files(conn, ref, seen.head.sha)
    return deliverable.ForgeRead(seen, files)


async def _newest(
    conn: aiosqlite.Connection, task_id: str, kinds: Sequence[str]
) -> list[tuple[str, dict[str, Any]]]:
    marks = ",".join("?" * len(kinds))
    async with conn.execute(
        f"SELECT kind, data FROM events WHERE subject_type = 'task'"  # noqa: S608
        f" AND subject_id = ? AND kind IN ({marks}) ORDER BY id DESC",
        (task_id, *kinds),
    ) as cur:
        rows = await cur.fetchall()
    out = []
    for kind, raw in rows:
        data = json.loads(raw) if raw else {}
        out.append((kind, data if isinstance(data, dict) else {}))
    return out


async def evaluation_of(
    conn: aiosqlite.Connection, task_id: str, pr: str | None, head: str | None
) -> dict[str, Any] | None:
    """The newest recorded `review.policy_evaluated` for exactly (pr, head)."""
    from argus_api.tasks.independent_review import _same_pr, _same_sha

    if not head:
        return None
    for _, data in await _newest(conn, task_id, [EVALUATED_EVENT]):
        if _same_sha(data.get("head_sha"), head) and _same_pr(data.get("pr"), pr):
            return data
    return None


@dataclass(frozen=True)
class Decision:
    mode: str
    tier: ReviewTier
    rules: list[str]

    @property
    def skip(self) -> bool:
        return self.mode == "enforce" and self.tier == "skip"


async def decide(
    tx: Tx,
    parent: aiosqlite.Row,
    *,
    pr: str | None,
    head: str,
    forge: deliverable.ForgeRead | None,
    actor: str,
) -> Decision | None:
    """Evaluate (once per head) and record `review.policy_evaluated`; None when the
    policy is off. A head already evaluated keeps its tier unless that read had no
    file list and this one does."""
    from argus_api.settings import service as settings_svc

    policy = await settings_svc.get_review_policy(tx.conn, parent["project"])
    if policy.mode == "off":
        return None
    prior = await evaluation_of(tx.conn, parent["id"], pr, head)
    files = forge.files if forge is not None else None
    have_files = (
        files is not None
        and files.paths is not None
        and files.head_sha is not None
        and files.head_sha.lower() == head.lower()
    )
    if prior is not None and not (prior.get("reason") == FILES_UNKNOWN and have_files):
        return Decision(policy.mode, prior["tier"], rules_of(prior, policy.default))
    ev = evaluate(policy, files.paths if have_files and files else None)
    event = {
        "mode": policy.mode,
        "tier": ev.tier,
        "pr": pr,
        "head_sha": head,
        "files": ev.files,
        "matched": ev.matched,
        "unmatched": ev.unmatched,
        "reason": ev.reason,
    }
    await record(tx, actor, "task", parent["id"], EVALUATED_EVENT, event)
    return Decision(policy.mode, ev.tier, rules_of(event, policy.default))


async def human_gate(
    conn: aiosqlite.Connection, parent: aiosqlite.Row, pr: str | None, head: str | None
) -> Decision | None:
    """The `human` decision for exactly this approved (pr, head) while enforced."""
    from argus_api.settings import service as settings_svc

    policy = await settings_svc.get_review_policy(conn, parent["project"])
    if policy.mode != "enforce":
        return None
    prior = await evaluation_of(conn, parent["id"], pr, head)
    if prior is None or prior.get("tier") != "human":
        return None
    return Decision(policy.mode, "human", rules_of(prior, policy.default))


class HeadPolicy(BaseModel):
    """What the policy chose for one reviewed head (the task's Reviews section)."""

    pr: str | None
    head_sha: str
    mode: str
    tier: ReviewTier
    rules: list[str]
    files: int
    skipped: bool  # enforced skip: no reviewer ran


async def heads_of(conn: aiosqlite.Connection, task_id: str) -> list[HeadPolicy]:
    """The newest evaluation of each (pr, head) of a task, oldest head first."""
    from argus_api.settings import service as settings_svc
    from argus_api.tasks.independent_review import _same_pr, _same_sha

    async with conn.execute(
        "SELECT project FROM tasks WHERE id = ?", (task_id,)
    ) as cur:
        row = await cur.fetchone()
    events = await _newest(conn, task_id, [EVALUATED_EVENT, SKIPPED_EVENT])
    default = (
        (await settings_svc.get_review_policy(conn, row[0])).default
        if row
        else "standard"
    )
    skips = [d for kind, d in events if kind == SKIPPED_EVENT]
    out: dict[tuple[str | None, str], HeadPolicy] = {}
    for kind, data in reversed(events):  # oldest first, the newest evaluation wins
        if kind != EVALUATED_EVENT or not data.get("head_sha"):
            continue
        head = data["head_sha"]
        out[(data.get("pr"), head.lower())] = HeadPolicy(
            pr=data.get("pr"),
            head_sha=head,
            mode=data.get("mode", "observe"),
            tier=data["tier"],
            rules=rules_of(data, default),
            files=data.get("files", 0),
            skipped=any(
                _same_sha(k.get("head_sha"), head)
                and _same_pr(k.get("pr"), data.get("pr"))
                for k in skips
            ),
        )
    return list(out.values())


# --- review report (spec 3.5) -------------------------------------------------

REPORT_DEFAULT_DAYS = 30
REPORT_MAX_DAYS = 90
_BLOCKING = ("P1", "P2")
_VERDICTS = ("approve", "request_changes", "fixed", "void")


class TierReport(BaseModel):
    heads: int = 0
    tasks: int = 0
    verdicts: dict[str, int] = Field(
        default_factory=lambda: dict.fromkeys(_VERDICTS, 0)
    )
    findings_by_priority: dict[str, int] = Field(default_factory=dict)
    succeeded: int = 0
    rejected: int = 0  # rejected by a person at a review decision
    cancelled: int = 0


class SkipFinding(BaseModel):
    task_id: str
    pr: str | None
    head_sha: str
    rules: list[str]
    findings: int  # P1 and P2 findings the review of that head produced


class PolicyReport(BaseModel):
    days: int
    tiers: dict[str, TierReport]
    skipped_with_findings: list[SkipFinding]


class ReviewReport(BaseModel):
    policy: PolicyReport


async def policy_report(
    conn: aiosqlite.Connection, project: str, since: str, days: int
) -> PolicyReport:
    """What the policy chose in the window and how those heads fared. A head counts
    once, under its newest evaluation; a `request_changes` round followed by an
    approval on the same task counts as `fixed`."""
    from argus_api.tasks import review_rounds

    async with conn.execute(
        "SELECT e.subject_id, e.data, t.state FROM events e"
        " JOIN tasks t ON t.id = e.subject_id"
        " WHERE e.kind = ? AND e.at >= ? AND t.project = ? ORDER BY e.id",
        (EVALUATED_EVENT, since, project),
    ) as cur:
        rows = await cur.fetchall()
    heads: dict[tuple[str, str | None, str], dict[str, Any]] = {}
    states: dict[str, str] = {}
    for task_id, raw, state in rows:
        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict) or not data.get("head_sha"):
            continue
        heads[(task_id, data.get("pr"), data["head_sha"].lower())] = data
        states[task_id] = state
    rejected: set[str] = set()
    if states:
        marks = ",".join("?" * len(states))
        async with conn.execute(
            "SELECT DISTINCT subject_id FROM decisions WHERE subject_type = 'task'"  # noqa: S608
            f" AND kind = 'review' AND answer_choice = 'reject' AND subject_id IN ({marks})",
            list(states),
        ) as cur:
            rejected = {r[0] for r in await cur.fetchall()}
    tiers: dict[str, TierReport] = {t: TierReport() for t in TIER_ORDER}
    seen_tasks: dict[str, set[str]] = {t: set() for t in TIER_ORDER}
    rounds_of = {t: (await review_rounds.review_rounds(conn, t)).rounds for t in states}
    skipped: list[SkipFinding] = []
    for (task_id, pr, head), data in heads.items():
        tier = data.get("tier")
        if tier not in tiers:
            continue
        rep = tiers[tier]
        rep.heads += 1
        if task_id not in seen_tasks[tier]:
            seen_tasks[tier].add(task_id)
            rep.tasks += 1
            rep.succeeded += states[task_id] == "succeeded"
            rep.cancelled += states[task_id] == "cancelled"
            rep.rejected += task_id in rejected
        rounds = rounds_of[task_id]
        blocking = 0
        for n, rnd in enumerate(rounds):
            if (rnd.head_sha or "").lower() != head or rnd.verdict is None:
                continue
            verdict = rnd.verdict
            if verdict == "request_changes" and any(
                later.verdict == "approve" for later in rounds[n + 1 :]
            ):
                verdict = "fixed"
            rep.verdicts[verdict] += 1
            for prio, count in rnd.findings_by_priority.items():
                rep.findings_by_priority[prio] = (
                    rep.findings_by_priority.get(prio, 0) + count
                )
                if prio in _BLOCKING:
                    blocking += count
        if tier == "skip" and blocking:
            skipped.append(
                SkipFinding(
                    task_id=task_id,
                    pr=pr,
                    head_sha=head,
                    rules=[m["rule"] for m in data.get("matched") or []],
                    findings=blocking,
                )
            )
    return PolicyReport(days=days, tiers=tiers, skipped_with_findings=skipped)
