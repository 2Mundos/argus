"""Read-only preview of answering a decision (MCP operations 3.2c).

PreviewAdvisory: it grants nothing and executes nothing. It reads one snapshot, writes no
row or event and makes no network call (not even to the forge). Every rule comes from
the answer path's own functions (`service.check_audience`, `check_answer`,
`same_answer`, `effect_of`, the task state machine), so the two cannot drift.
"""

import re
from datetime import datetime
from typing import Literal

from argus_core.continuation import ArtifactKind, ContinuationArtifact
from argus_core.decisions import (
    Decision,
    DecisionAnswer,
    DecisionKind,
    DecisionState,
)
from argus_core.redact import redact
from argus_core.tasks import Action, InvalidTransitionError, TaskState
from pydantic import BaseModel, Field

from argus_api.auth import Identity
from argus_api.db import Snapshot, ts
from argus_api.decisions import service, store
from argus_api.decisions.service import Effect
from argus_api.errors import DomainError
from argus_api.tasks import continuation as cont
from argus_api.tasks import service as tasks

QUESTION_MAX = 4_000
PROPOSAL_TEXT_MAX = 1_000
MAX_EFFECTS = 20
MAX_CHECKS = 10
MAX_CANCELLED = 20

Provenance = Literal["stored", "derived", "unknown"]

# Hex runs inside a hyphenated id (a UUID) are not SHAs.
_HEX = re.compile(r"(?<![\w-])(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}(?![\w-])")

_TASK_ACTION = {
    Effect.CLARIFY: Action.UNBLOCK,
    Effect.TASK_APPROVE: Action.APPROVE,
    Effect.TASK_CANCEL: Action.CANCEL,
    Effect.REVIEW_ACCEPT: Action.ACCEPT,
    Effect.REVIEW_REJECT: Action.REJECT,
}


class PreviewAdvisory(BaseModel):
    authorizes: Literal[False] = False
    executes: Literal[False] = False


class PreviewSnapshot(BaseModel):
    computed_at: datetime
    decision_version: str  # source timestamp and state; differs once the decision moved


class PreviewDecisionFacts(BaseModel):
    id: str
    kind: DecisionKind
    subject_type: str
    subject_id: str
    state: DecisionState
    audience: str
    options: list[str] | None
    requested_by: str
    decided_by: str | None
    created_at: datetime
    decided_at: datetime | None
    provenance: Provenance = "stored"
    observed_at: datetime


class PreviewQuestionFacts(BaseModel):
    reference: dict[str, str]  # {id, kind}: read the whole item with get_decision
    text: str
    truncated: bool
    full_length: int
    redacted: bool
    provenance: Provenance = "stored"
    observed_at: datetime


class PreviewProposal(BaseModel):
    choice: str | None
    text: str
    request_id: str | None
    valid: bool | Literal["unknown"]
    error: str | None
    offered_option: bool | None  # None: free text, or no choice given
    provenance: Provenance = "derived"


class PreviewAuthorization(BaseModel):
    """The verdict the answer path would give this caller now; not a grant."""

    would_allow: bool
    rule: str
    error: str | None
    provenance: Provenance = "derived"


class PreviewAnswerOutcome(BaseModel):
    result: Literal["would_apply", "replay", "rejected", "unknown"]
    error: dict[str, str | int] | None = None
    provenance: Provenance = "derived"


class PreviewEffect(BaseModel):
    kind: str
    detail: str
    provenance: Provenance


class PreviewTransition(BaseModel):
    decision: dict[str, str | None]
    effects: list[PreviewEffect]
    provenance: Provenance = "derived"


class PreviewTaskFacts(BaseModel):
    id: str
    state: TaskState
    attempt: int
    attempt_id: int | None
    claimed_by: str | None
    claimed_at: str | None
    version: str
    provenance: Provenance = "stored"
    observed_at: datetime


class PreviewPremiseCheck(BaseModel):
    subject: str
    premise: str | None
    recorded: str | None
    observed: str | None
    observed_at: datetime | None
    status: Literal["match", "stale", "unknown"]
    provenance: Provenance


class PreviewPremiseChecks(BaseModel):
    live_forge_checked: Literal[False] = False  # stored observations only
    checks: list[PreviewPremiseCheck]


class DecisionPreview(BaseModel):
    snapshot: PreviewSnapshot
    decision: PreviewDecisionFacts
    question: PreviewQuestionFacts
    proposal: PreviewProposal
    authorization: PreviewAuthorization
    answer_outcome: PreviewAnswerOutcome
    transition: PreviewTransition | Literal["unknown"]
    task: (
        PreviewTaskFacts | Literal["unknown"] | None
    )  # None: the subject is not a task
    premise_checks: PreviewPremiseChecks
    advisory: PreviewAdvisory = Field(default_factory=PreviewAdvisory)


def _clip(text: str, limit: int) -> tuple[str, bool]:
    return (text[:limit], len(text) > limit)


def _error(exc: Exception) -> dict[str, str | int]:
    if isinstance(exc, DomainError):
        return {"status": exc.status, "code": exc.code, "message": exc.message}
    if isinstance(exc, InvalidTransitionError):
        return {"status": 409, "code": "invalid_transition", "message": str(exc)}
    raise exc


async def preview(
    snap: Snapshot,
    decision_id: str,
    caller: Identity,
    *,
    choice: str | None = None,
    text: str = "",
    request_id: str | None = None,
) -> DecisionPreview:
    decision = await store.get(snap.conn, decision_id)
    body = DecisionAnswer(choice=choice, text=text, request_id=request_id)
    source_at = decision.decided_at or decision.created_at
    given = choice is not None or bool(text)

    task_row = None
    task: PreviewTaskFacts | Literal["unknown"] | None = None
    if decision.subject_type == "task":
        task_row, task = await _task_facts(snap, decision)

    auth_error = _auth_error(decision, caller)
    shape_error: DomainError | None = None
    try:
        service.check_answer(decision, body)
    except DomainError as exc:
        shape_error = exc
    effect = service.effect_of(decision, choice)
    if (
        auth_error is None
        and task_row is not None
        and effect in (Effect.REVIEW_ACCEPT, Effect.REVIEW_REJECT)
    ):
        try:
            tasks.check_reviewer(task_row, caller.scope)
        except DomainError as exc:
            auth_error = exc

    transition: PreviewTransition | Literal["unknown"] = "unknown"
    transition_error: Exception | None = None
    if decision.state is DecisionState.OPEN and given and shape_error is None:
        transition, transition_error = await _transition(
            snap, decision, body, effect, task_row
        )

    return DecisionPreview(
        snapshot=PreviewSnapshot(
            computed_at=snap.now,
            decision_version=f"{ts(source_at)}|{decision.state}",
        ),
        decision=PreviewDecisionFacts(
            **decision.model_dump(
                include={
                    "id",
                    "kind",
                    "subject_type",
                    "subject_id",
                    "state",
                    "audience",
                    "options",
                    "requested_by",
                    "decided_by",
                    "created_at",
                    "decided_at",
                }
            ),
            observed_at=source_at,
        ),
        question=_question(decision),
        proposal=PreviewProposal(
            choice=choice,
            text=_clip(redact(text), PROPOSAL_TEXT_MAX)[0],
            request_id=request_id,
            valid="unknown" if not given else shape_error is None,
            error=shape_error.code if shape_error else None,
            offered_option=None
            if decision.options is None or choice is None
            else choice in decision.options,
        ),
        authorization=PreviewAuthorization(
            would_allow=auth_error is None,
            rule=_rule(decision),
            error=auth_error.code if auth_error else None,
        ),
        answer_outcome=_outcome(
            decision, body, caller, auth_error, shape_error, transition_error, given
        ),
        transition=transition,
        task=task,
        premise_checks=_premise(decision, task_row),
    )


def _auth_error(decision: Decision, caller: Identity) -> DomainError | None:
    try:
        service.check_audience(decision.audience, caller.scope)
    except DomainError as exc:
        return exc
    return None


def _rule(decision: Decision) -> str:
    who = "an admin" if decision.audience == "human" else "an orchestrator or admin"
    return f"audience {decision.audience}: answered by {who}"


def _question(decision: Decision) -> PreviewQuestionFacts:
    clean = redact(decision.question)
    text, cut = _clip(clean, QUESTION_MAX)
    return PreviewQuestionFacts(
        reference={"id": decision.id, "kind": str(decision.kind)},
        text=text,
        truncated=cut,
        full_length=len(decision.question),
        redacted=clean != decision.question,
        observed_at=decision.created_at,
    )


def _outcome(
    decision: Decision,
    body: DecisionAnswer,
    caller: Identity,
    auth_error: DomainError | None,
    shape_error: DomainError | None,
    transition_error: Exception | None,
    given: bool,
) -> PreviewAnswerOutcome:
    if decision.state is not DecisionState.OPEN:
        if body.request_id is not None and service.same_answer(decision, body, caller):
            return PreviewAnswerOutcome(result="replay")
        return PreviewAnswerOutcome(
            result="rejected",
            error={
                "status": 409,
                "code": "decision_not_open",
                "message": f"decision {decision.id} is {decision.state}",
            },
        )
    if auth_error is not None:
        return PreviewAnswerOutcome(result="rejected", error=_error(auth_error))
    if not given:
        return PreviewAnswerOutcome(result="unknown")
    if shape_error is not None:
        return PreviewAnswerOutcome(result="rejected", error=_error(shape_error))
    if transition_error is not None:
        return PreviewAnswerOutcome(result="rejected", error=_error(transition_error))
    return PreviewAnswerOutcome(result="would_apply")


async def _task_facts(snap: Snapshot, decision: Decision):
    try:
        row = await tasks.task_row(snap.conn, decision.subject_id)
    except DomainError:
        return None, "unknown"
    async with snap.conn.execute(
        "SELECT id, agent_id, claimed_at FROM task_attempts WHERE task_id = ?"
        " ORDER BY id DESC LIMIT 1",
        (row["id"],),
    ) as cur:
        attempt = await cur.fetchone()
    facts = PreviewTaskFacts(
        id=row["id"],
        state=TaskState(row["state"]),
        attempt=row["attempt"],
        attempt_id=attempt["id"] if attempt else None,
        claimed_by=attempt["agent_id"] if attempt else None,
        claimed_at=attempt["claimed_at"] if attempt else None,
        version=row["updated_at"],
        observed_at=datetime.fromisoformat(row["updated_at"]),
    )
    return row, facts


async def _transition(
    snap: Snapshot,
    decision: Decision,
    body: DecisionAnswer,
    effect: Effect,
    task_row,
) -> tuple[PreviewTransition | Literal["unknown"], Exception | None]:
    effects: list[PreviewEffect] = []

    def add(kind: str, detail: str, provenance: Provenance = "derived") -> None:
        effects.append(PreviewEffect(kind=kind, detail=detail, provenance=provenance))

    error: Exception | None = None
    action = _TASK_ACTION.get(effect)
    if action is not None:
        if task_row is None:
            add("unknown", "the task could not be read", "unknown")
        else:
            try:
                row, new = await tasks.planned_state(snap.conn, task_row["id"], action)
            except InvalidTransitionError as exc:
                error = exc
            else:
                old = TaskState(row["state"])
                add("task_state", f"{old} -> {new} ({action})")
                if new is TaskState.READY and old is not new:
                    add("requeue", "the task returns to ready for the next claim")
                if (moot := tasks.moot_decisions(old, new)) is not None:
                    ids = [
                        i
                        for i in await store.open_ids(
                            snap.conn, "task", row["id"], moot[0]
                        )
                        if i != decision.id
                    ]
                    if ids:
                        add(
                            "decisions_cancelled",
                            ", ".join(ids[:MAX_CANCELLED]),
                        )
                if effect is Effect.REVIEW_REJECT:
                    if bump := tasks.review_escalation(row):
                        add("difficulty_escalation", f"{bump['from']} -> {bump['to']}")
                    if body.text:
                        add("review_notes_added", "the notes are appended to the spec")
                if effect is Effect.CLARIFY:
                    add("clarification_added", "the answer is added to the task spec")
                    add(
                        "knowledge_saved",
                        "save_to_knowledge is not previewed",
                        "unknown",
                    )
                if new is TaskState.READY:
                    add(
                        "unknown",
                        "claim and wake effects after ready need a write to know",
                        "unknown",
                    )
    else:
        _other_effects(decision, effect, add)
    if error is not None:
        return "unknown", error
    return (
        PreviewTransition(
            decision={"from": "open", "to": "answered", "choice": body.choice},
            effects=effects[:MAX_EFFECTS],
        ),
        None,
    )


def _other_effects(decision: Decision, effect: Effect, add) -> None:
    payload = decision.payload or {}
    match effect:
        case Effect.KNOWLEDGE_ACCEPT | Effect.KNOWLEDGE_REJECT:
            verb = "accepted" if effect is Effect.KNOWLEDGE_ACCEPT else "rejected"
            add("knowledge_proposal", f"proposal {decision.subject_id} is {verb}")
        case Effect.POOL_SCALE_APPROVE | Effect.POOL_SCALE_REJECT:
            if effect is Effect.POOL_SCALE_APPROVE:
                add(
                    "pool_scale",
                    f"pool {decision.subject_id} scales to"
                    f" {payload.get('desired_replicas', 'unknown')} replicas",
                )
                add("unknown", "the reconciler's later actions need a write", "unknown")
            else:
                add("pool_scale", f"pool {decision.subject_id} scale is declined")
        case Effect.ROUTINE_REENABLE:
            add("routine_reenabled", f"routine {decision.subject_id} is re-enabled")
        case Effect.ROUTINE_STAY_OFF:
            add("routine_unchanged", f"routine {decision.subject_id} stays disabled")


# A PR URL, an owner/repo#N reference, or a bare PR N / #N mention.
_PR_MENTION = re.compile(
    r"https?://[^\s/]+/([\w.-]+)/([\w.-]+)/pulls?/(\d+)"
    r"|\b([\w.-]+)/([\w.-]+)#(\d+)"
    r"|(?:\bPR\b|\bpull request\b|\bpull/|#)\s*#?(\d+)",
    re.IGNORECASE,
)

PrId = tuple[str, tuple[str, str] | None]  # (number, (owner, repo) or unknown)


def _mention(m: re.Match[str]) -> PrId:
    if m.group(3):
        return m.group(3), (m.group(1).lower(), m.group(2).lower())
    if m.group(6):
        return m.group(6), (m.group(4).lower(), m.group(5).lower())
    return m.group(7), None


def _pr_id(ref: str, url: str | None) -> PrId | None:
    """A stored PR's number and repository; the repository comes from the ref or URL."""
    found = [_mention(m) for t in (ref, url or "") for m in _PR_MENTION.finditer(t)]
    with_repo = [f for f in found if f[1] is not None]
    if bare := re.fullmatch(r"#?(\d+)", ref.strip()):
        number = bare.group(1)
        return number, next((r for n, r in with_repo if n == number), None)
    return (with_repo or found or [None])[0]


def _same_head(a: str, b: str) -> bool:
    return a.startswith(b) or b.startswith(a)


def _premise_pairs(
    question: str, payload_sha: str | None
) -> list[tuple[str, PrId | None]]:
    """(sha, PR) pairs; the PR is the nearest preceding mention, else the only
    mention, else None (association unknown)."""
    mentions = [(m.start(), _mention(m)) for m in _PR_MENTION.finditer(question)]
    only = mentions[0][1] if len({p for _, p in mentions}) == 1 else None
    out: dict[str, PrId | None] = {}
    if payload_sha:
        out[payload_sha] = only
    else:
        for m in _HEX.finditer(question):
            before = [p for pos, p in mentions if pos < m.start()]
            out.setdefault(m.group(0), before[-1] if before else only)
            if len(out) >= 5:
                break
    return list(out.items())


def _premise(decision: Decision, task_row) -> PreviewPremiseChecks:
    if task_row is None:
        return PreviewPremiseChecks(
            checks=[
                PreviewPremiseCheck(
                    subject="premise",
                    premise=None,
                    recorded=None,
                    observed=None,
                    observed_at=None,
                    status="unknown",
                    provenance="unknown",
                )
            ]
        )
    payload_sha = (decision.payload or {}).get("head_sha")
    pairs = _premise_pairs(
        decision.question, payload_sha if isinstance(payload_sha, str) else None
    )
    contract = cont.load(task_row)
    stored = [
        (a, a.head_sha)
        for a in contract.artifacts
        if a.kind in (ArtifactKind.PR, ArtifactKind.COMMIT) and a.head_sha
    ]
    checks: list[PreviewPremiseCheck] = []

    def unknown(subject: str, sha: str | None) -> None:
        checks.append(
            PreviewPremiseCheck(
                subject=subject,
                premise=sha,
                recorded=None,
                observed=None,
                observed_at=None,
                status="unknown",
                provenance="unknown",
            )
        )

    def check(a: ContinuationArtifact, head: str, sha: str) -> None:
        status: Literal["match", "stale", "unknown"] = "match"
        if not _same_head(head, sha):
            # An observation older than the question cannot prove it stale: the
            # head may have moved to the premise after it was recorded.
            status = "stale" if a.observed_at >= decision.created_at else "unknown"
        checks.append(
            PreviewPremiseCheck(
                subject=redact(f"{a.kind.value} {a.ref}")[:300],
                premise=sha,
                recorded=head,
                observed=None,
                observed_at=a.observed_at,
                status=status,
                provenance="stored",
            )
        )

    if not pairs or not stored:
        unknown(
            "decision names no head"
            if not pairs
            else "decision has no stored observation",
            pairs[0][0] if pairs else None,
        )
        pairs = []
    for sha, pr in pairs:
        if pr is not None:
            # A named PR is compared with its own stored head only; another
            # artifact at the same SHA never stands in for it.
            named = _named(stored, pr)
            if len({repo for _, _, repo in named}) == 1:
                for a, head, _ in named:
                    check(a, head, sha)
            elif named:
                unknown("PR number names several repositories", sha)
            else:
                unknown("premise cannot be tied to a stored PR or commit", sha)
            continue
        same = [(a, head) for a, head in stored if _same_head(head, sha)]
        # No PR named: only a match on one commit is asserted, never staleness.
        if same and all(_same_head(h, k) for _, h in same for _, k in same):
            for a, head in same:
                check(a, head, sha)
        else:
            unknown("premise cannot be tied to a stored PR or commit", sha)
    checks.extend(
        PreviewPremiseCheck(
            subject=redact(r.subject),
            premise=None,
            recorded=redact(r.recorded),
            observed=redact(r.observed),
            observed_at=r.at,
            status="stale",
            provenance="stored",
        )
        for r in contract.reconciliation
    )
    return PreviewPremiseChecks(checks=checks[:MAX_CHECKS])


def _named(
    stored: list[tuple[ContinuationArtifact, str]], pr: PrId
) -> list[tuple[ContinuationArtifact, str, tuple[str, str] | None]]:
    """Stored PRs with the named number; a named repository narrows them to that
    repository, or to PRs whose repository is not recorded."""
    number, repo = pr
    found = [
        (a, head, pid[1])
        for a, head in stored
        if a.kind is ArtifactKind.PR
        and (pid := _pr_id(a.ref, a.url)) is not None
        and pid[0] == number
    ]
    if repo is None:
        return found
    return [f for f in found if f[2] == repo] or [f for f in found if f[2] is None]
