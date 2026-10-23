"""Task continuation contract, end to end through the real services and SQLite:
producer (progress milestone, observation, answer, release) -> persistence -> claim ->
effective contract -> next decision. Nothing here talks to Git, a provider or a browser:
the "live" observations are what a runtime would report, supplied by the test.
See docs/specs/task-continuation.spec.md."""

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from api_support import FakeClock, insert_agent
from argus_api.auth import Identity
from argus_api.db import Database
from argus_api.decisions import service as decisions
from argus_api.errors import ConflictError, LeaseConflictError, ValidationFailedError
from argus_api.main import app
from argus_api.tasks import service as tasks
from argus_core.auth import Scope
from argus_core.continuation import (
    ArtifactState,
    ContinuationArtifact,
    ContinuationPhase,
    ContinuationUpdate,
    ObservationRequest,
    classify_answer,
    split_spec,
)
from argus_core.decisions import Ask, DecisionAnswer
from argus_core.tasks import ClaimedTask, TaskCreate, TaskResult
from argus_core.tasks import TaskState as S
from fastapi.testclient import TestClient
from pydantic import ValidationError

A, B = "agent-a", "agent-b"
ADMIN = Identity("t-admin", Scope.ADMIN, None, "test")
ORCH = Identity("t-orch", Scope.ORCHESTRATOR, "orch", None)
REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "integrations/nanoclaw/fixtures/continuation"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@pytest.fixture(autouse=True)
async def agents(db: Database) -> None:
    await insert_agent(db, A)
    await insert_agent(db, B)


async def new_task(db: Database, **kw) -> str:
    kw.setdefault("title", "task")
    async with db.write() as tx:
        task = await tasks.create_task(
            tx, TaskCreate(project="global", **kw), "admin:test"
        )
    return task.id


async def claim(db: Database, agent: str = A):
    async with db.write() as tx:
        task = await tasks.claim(tx, agent, [])
    assert task is not None
    return task


async def get(db: Database, task_id: str):
    async with db.read() as conn:
        return await tasks.get_task(conn, task_id)


async def milestone(db: Database, clock: FakeClock, task, kind: str, data: dict):
    clock.advance(11)
    async with db.write() as tx:
        return await tasks.report_progress(
            tx,
            task.assignee_agent_id,
            task.id,
            task.attempt,
            kind,
            kind=kind,
            data=data,
        )


async def release(db: Database, task_id: str, reason: str = "recycle"):
    async with db.write() as tx:
        return await tasks.release(tx, task_id, "admin:ops", reason)


async def observe(db: Database, agent: str, task, **body):
    async with db.write() as tx:
        return await tasks.observe(
            tx, agent, task.id, ObservationRequest(attempt=task.attempt, **body)
        )


async def update(db: Database, task_id: str, revision: int, **body):
    async with db.write() as tx:
        return await tasks.update_continuation(
            tx,
            task_id,
            ContinuationUpdate(expected_revision=revision, reason="test", **body),
            "orchestrator:test",
        )


async def ask_and_answer(db: Database, task, question: str, *, options=None, **answer):
    async with db.write() as tx:
        asked = await decisions.ask(
            tx,
            task.assignee_agent_id,
            task.id,
            Ask(attempt=task.attempt, question=question, options=options),
        )
    async with db.write() as tx:
        await decisions.answer(tx, asked.id, DecisionAnswer(**answer), ORCH)
    return asked.id


def claimed(task) -> dict:
    """What POST /tasks/claim returns for this task (the route builds exactly this)."""
    return json.loads(
        ClaimedTask(**task.model_dump(), knowledge_refs=[]).model_dump_json()
    )


def _stable_uuid(match: re.Match[str], ids: dict[str, str]) -> str:
    return ids.setdefault(match.group(0), f"00000000-0000-0000-0000-{len(ids):012d}")


def _stabilize_fixture_ids(value: Any, ids: dict[str, str] | None = None) -> Any:
    """Replace random UUIDs with stable placeholders; preserve structure and list order."""
    if ids is None:
        ids = {}
    if isinstance(value, dict):
        return {k: _stabilize_fixture_ids(v, ids) for k, v in value.items()}
    if isinstance(value, list):
        return [_stabilize_fixture_ids(v, ids) for v in value]
    if isinstance(value, str):
        return re.sub(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            lambda m: _stable_uuid(m, ids),
            value,
        )
    return value


def fixture_payload(task) -> dict[str, Any]:
    """Canonical claim JSON for NanoClaw continuation tests (semantic equality)."""
    got = claimed(task)
    for volatile in (
        "created_at",
        "updated_at",
        "version",
        "lease_expires_at",
        "timeout_at",
    ):
        got[volatile] = "<volatile>" if got.get(volatile) else None
    return _stabilize_fixture_ids(got)


def _write_fixture_file(path: Path, payload: dict[str, Any]) -> None:
    """Write fixtures the way CI lint expects: Biome-formatted JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, dir=path.parent
    ) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    subprocess.run(
        ["pnpm", "exec", "biome", "check", "--write", str(tmp_path)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    tmp_path.replace(path)


def save_fixture(name: str, task) -> None:
    """The TypeScript render tests consume these claims. They are regenerated with
    ARGUS_UPDATE_FIXTURES=1 and otherwise must match semantically (Biome may format
    whitespace), so producer and renderer cannot drift apart unnoticed."""
    payload = fixture_payload(task)
    payload["id"] = name
    path = FIXTURES / f"{name}.json"
    if os.environ.get("ARGUS_UPDATE_FIXTURES") == "1":
        _write_fixture_file(path, payload)
        return
    on_disk = json.loads(path.read_text())
    assert payload == on_disk, (
        f"{path} drifted from the producer (semantic mismatch). "
        "Re-run with ARGUS_UPDATE_FIXTURES=1 if the change is intentional."
    )


def test_continuation_fixture_files_are_biome_canonical() -> None:
    """On-disk fixtures must already match Biome output (no hidden formatting drift)."""
    for path in sorted(FIXTURES.glob("*.json")):
        before = path.read_text()
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, dir=FIXTURES
        ) as tmp:
            tmp.write(before)
            tmp_path = Path(tmp.name)
        subprocess.run(
            ["pnpm", "exec", "biome", "check", "--write", str(tmp_path)],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
        )
        assert tmp_path.read_text() == before, (
            f"{path} is not Biome-canonical; run biome check --write on fixtures"
        )
        tmp_path.unlink()


# -- CA2: a PR and local WIP exist; the next attempt must continue, not replan ----------


async def test_ca2_existing_pr_and_wip_resume_instead_of_a_new_plan(
    db: Database, clock: FakeClock
) -> None:
    task_id = await new_task(
        db,
        title="CA2 corporate actions collection",
        spec="Implement corporate actions collection.",
        acceptance=["collector stores splits", "collector stores dividends"],
    )
    first = await claim(db)
    assert first.continuation.phase is ContinuationPhase.IMPLEMENT
    assert "artifacts_unknown" in first.continuation.gaps  # unknown, not "none exist"
    await milestone(
        db, clock, first, "commit_created", {"branch": "task/ca2", "sha": "561265a"}
    )
    await milestone(
        db,
        clock,
        first,
        "pr_opened",
        {"number": 1104, "url": "https://git.example/org/repo/pulls/1104"},
    )
    await release(db, task_id, "orchestrator recycled the worker")

    second = await claim(db, B)
    c = second.continuation
    assert second.attempt == first.attempt + 1
    assert c.phase is ContinuationPhase.RESUME and c.phase_source == "derived"
    assert {(a.kind.value, a.ref) for a in c.artifacts} == {
        ("branch", "task/ca2"),
        ("commit", "561265a"),
        ("pr", "1104"),
    }
    assert all(a.provenance.value == "worker_reported" for a in c.artifacts)
    assert c.interruption is not None
    assert (c.interruption.reason, c.interruption.attempt) == (
        "released",
        first.attempt,
    )

    # The orchestrator knows the other PR and the unpushed local commit.
    await update(
        db,
        task_id,
        c.revision,
        artifacts=[
            {
                "kind": "pr",
                "ref": "1100",
                "head_sha": "fffdf3e8b3e7739c431ff245ba8865e30067b1cb",
                "state": "open",
                "note": "the PR to continue",
            }
        ],
        wip={"summary": "local commit ab33ee0 not pushed", "ref": "ab33ee0"},
        objective="continue PR 1100, keep the local commit",
    )
    await release(db, task_id)
    third = await claim(db, A)
    c = third.continuation
    assert c.phase is ContinuationPhase.RESUME
    assert {a.ref for a in c.artifacts if a.kind.value == "pr"} == {"1100", "1104"}
    assert c.wip is not None and c.wip.ref == "ab33ee0"
    assert c.wip.provenance.value == "orchestrator"
    assert [a.provenance.value for a in c.artifacts if a.ref == "1100"] == [
        "orchestrator"
    ]
    # Attempt history keeps what each attempt was told.
    async with db.read() as conn:
        history = await tasks.attempt_definitions(conn, task_id)
    told = [h.definition["continuation"]["phase"] for h in history]
    assert told == ["implement", "resume", "resume"]
    save_fixture("ca2-resume", third)


# -- FL4: merged, QA pending, owner already authorised the QA ---------------------------


async def test_fl4_merged_pr_with_pending_qa_does_not_reapprove(
    db: Database, clock: FakeClock
) -> None:
    await new_task(
        db,
        title="FL4",
        spec="Ship FL4.\n\n## Acceptance\nBrowser QA desktop/mobile.",
        acceptance=["browser QA desktop/mobile pt-BR/en-US/es-ES done"],
    )
    t = await claim(db)
    stale = "PR #1099 is open and not merged"
    await ask_and_answer(db, t, "Is PR 1099 merged?", text=f"Not yet: {stale}")
    t = await claim(db)  # a premise that later became false
    await milestone(
        db,
        clock,
        t,
        "pr_opened",
        {"number": 1099, "url": "https://git.example/o/r/pulls/1099"},
    )
    t = await observe(
        db, A, t, artifacts=[{"kind": "pr", "ref": "1099", "state": "merged"}]
    )
    await ask_and_answer(
        db,
        t,
        "May I run browser QA on a disposable stack or synthetic Stage?",
        text="Yes: browser QA desktop/mobile in pt-BR, en-US, es-ES on a disposable stack or synthetic Stage. Not Production.",
    )
    t = await claim(db, A)
    c = t.continuation
    assert (
        c.phase is ContinuationPhase.QA
    )  # merged is not accepted: the checklist is pending
    [merged] = [a for a in c.artifacts if a.ref == "1099"]
    assert merged.state is ArtifactState.MERGED
    assert merged.provenance.value == "runtime_observed"
    assert c.pending and c.checklist[0].status.value == "pending"
    qa = next(d for d in c.decisions if "browser QA" in d.question)
    stale_d = next(d for d in c.decisions if "1099" in d.question)
    assert (
        stale_d.premise_outdated and not qa.premise_outdated
    )  # PR 1099 was seen merged later
    assert qa.status == "effective" and qa.scope == "task"
    # The audit narrative keeps the stale premise; the contract does not rely on it.
    original, history = split_spec(t.spec)
    assert stale in history and stale not in original
    assert "Ship FL4" in original
    # The effective decisions are newest first.
    assert c.decisions[0].decided_at >= c.decisions[-1].decided_at
    # Asking the same authorisation again is refused with the stored answer.
    with pytest.raises(ConflictError) as err:
        async with db.write() as tx:
            await decisions.ask(
                tx,
                A,
                t.id,
                Ask(
                    attempt=t.attempt,
                    question="may I run  browser QA on a disposable stack or synthetic Stage?",
                ),
            )
    assert (
        err.value.code == "already_answered" and "Not Production" in err.value.message
    )
    save_fixture("fl4-merged-qa", t)


# -- Codex: legacy spec-only criteria, short bare answer -------------------------------


async def test_legacy_task_is_an_explicitly_incomplete_contract(
    db: Database, clock: FakeClock
) -> None:
    task_id = await new_task(
        db,
        title="Reimplement #51",
        spec="Reimplementar #51.\n\n## Aceite\n- tudo verde\n\n## Clarifications\n\n- **Asked (attempt 1):** #74 aberto?\n  **Answered by admin:x:** B\n",
        acceptance=[],
    )
    t = await claim(db)
    await ask_and_answer(db, t, "Which option for #74?", text="B")
    t = await claim(db)
    c = t.continuation
    assert c.checklist_state == "unstructured" and c.checklist == []
    assert "acceptance_not_structured" in c.gaps
    [bare] = [d for d in c.decisions if d.question.startswith("Which option")]
    assert bare.status == "unresolved" and not bare.options_known  # "B" grants nothing
    assert any(g.startswith("decision_unresolved") for g in c.gaps)
    # The merged follow-ups are known only after a live look.
    await observe(
        db,
        A,
        t,
        artifacts=[
            {"kind": "pr", "ref": "74", "state": "merged", "head_sha": "abcdef1"}
        ],
    )
    t = await get(db, task_id)
    assert (
        t.continuation.phase is ContinuationPhase.RESUME
    )  # nothing structured to verify yet
    save_fixture("codex-legacy", t)
    # Completing without criteria on continued work is reviewed, never auto-accepted.
    async with db.write() as tx:
        done = await tasks.complete(
            tx, A, task_id, t.attempt, TaskResult(summary="all good")
        )
    assert done.state is S.IN_REVIEW
    # The orchestrator can structure it by assisted reconciliation, audited.
    async with db.read() as conn:
        rev = (await tasks.continuation_of(conn, task_id)).revision
    after = await update(
        db, task_id, rev, checklist=[{"text": "review findings of #74 followups fixed"}]
    )
    assert after.continuation.checklist_state == "structured"
    assert after.continuation.checklist[0].source == "orchestrator"
    save_fixture("codex-reconciled", await get(db, task_id))


# -- acceptance is evidence, never silence ----------------------------------------------


async def test_missing_acceptance_is_not_success(db: Database) -> None:
    task_id = await new_task(db, acceptance=["unit tests pass", "browser QA passes"])
    t = await claim(db)
    ids = [i.id for i in t.continuation.checklist]
    async with db.write() as tx:  # done without proof is not done
        out = await tasks.complete(
            tx,
            A,
            task_id,
            t.attempt,
            TaskResult(
                summary="x",
                checklist=[
                    {"id": ids[0], "status": "done", "evidence": "pytest: 12 passed"},
                    {"id": ids[1], "status": "done"},
                ],
            ),
        )
    assert out.state is S.IN_REVIEW
    by = {i.id: i for i in out.continuation.checklist}
    assert (
        by[ids[0]].status.value == "done"
        and by[ids[0]].provenance.value == "worker_reported"
    )
    assert by[ids[1]].status.value == "verification_missing"
    # Rejecting returns it to ready with the evidence already recorded.
    async with db.write() as tx:
        from argus_api.tasks.service import review

        await review(
            tx, task_id, "admin:x", Scope.ADMIN, accept=False, notes="needs QA"
        )
    t2 = await claim(db, B)
    assert [i.status.value for i in t2.continuation.checklist] == [
        "done",
        "verification_missing",
    ]
    assert t2.continuation.pending == [ids[1]]
    async with db.write() as tx:
        ok = await tasks.complete(
            tx,
            B,
            task_id,
            t2.attempt,
            TaskResult(
                summary="x",
                checklist=[
                    {"id": ids[1], "status": "done", "evidence": "qa receipt #4"}
                ],
            ),
        )
    assert ok.state is S.SUCCEEDED
    # A report cannot add or invent criteria.
    t3 = await new_task(db, acceptance=["only"])
    c3 = await claim(db, A)
    async with db.write() as tx:
        res = await tasks.complete(
            tx,
            A,
            t3,
            c3.attempt,
            TaskResult(
                summary="x",
                checklist=[
                    {"id": "ac-forged", "status": "done", "evidence": "trust me"}
                ],
            ),
        )
    assert res.state is S.IN_REVIEW


# -- environments: typed, and assistance is not a decision ------------------------------


async def test_missing_qa_resource_asks_for_concrete_assistance(db: Database) -> None:
    task_id = await new_task(db, acceptance=["integration tests on Postgres"])
    t = await claim(db)
    t = await observe(
        db,
        A,
        t,
        environment=[
            {
                "resource": "postgres",
                "installed": "yes",
                "credential_usable": "no",
                "reachable": "unknown",
                "permitted": "yes",
                "validated": "no",
            }
        ],
    )
    [env] = t.continuation.environment
    assert not env.ready and env.missing == [
        "credential_usable",
        "reachable",
        "validated",
    ]
    assert "environment_not_ready:postgres" in t.continuation.gaps
    async with db.write() as tx:
        asked = await decisions.ask(
            tx,
            A,
            task_id,
            Ask(
                attempt=t.attempt,
                question="postgres credential unusable; need a disposable instance",
                audience="human",  # ignored: assistance goes to the orchestrator
                category="assistance",
                resource="postgres",
            ),
        )
    assert asked.audience.value == "orchestrator"
    assert asked.payload == {"category": "assistance", "resource": "postgres"}
    async with db.write() as tx:
        await decisions.answer(
            tx, asked.id, DecisionAnswer(text="use pg://disposable-1"), ORCH
        )
    t = await claim(db, B)
    [d] = t.continuation.decisions
    assert d.category == "assistance" and d.status == "effective"
    # An assistance answer does not make the asked question a settled goal decision.
    async with db.write() as tx:
        again = await decisions.ask(
            tx,
            B,
            task_id,
            Ask(
                attempt=t.attempt,
                question="postgres credential unusable; need a disposable instance",
            ),
        )
    assert again.state.value == "open"
    # Nothing is "ready" until a real run validates it.
    t = await get(db, task_id)
    assert "environment_not_ready:postgres" in t.continuation.gaps


# -- decisions: newest wins, nothing is deleted -----------------------------------------


async def test_latest_decision_supersedes_without_erasing_history(db: Database) -> None:
    task_id = await new_task(db, spec="Do X.")
    t = await claim(db)
    first = await ask_and_answer(db, t, "Scope?", text="Only the API, no UI")
    t = await claim(db)
    with pytest.raises(ConflictError) as err:  # settled: the worker may not reopen it
        await ask_and_answer(db, t, "Scope?", text="API and UI")
    assert err.value.code == "already_answered"
    # Changing a decision is the orchestrator's act: retire it, then the question is open again.
    t = await update(db, task_id, t.continuation.revision, supersede_decisions=[first])
    assert [d.status for d in t.continuation.decisions] == ["superseded"]
    await ask_and_answer(db, t, "Scope?", text="API and UI, final")
    t = await claim(db)
    live = [d for d in t.continuation.decisions if d.status == "effective"]
    assert [d.answer for d in live] == ["API and UI, final"]
    assert [d.status for d in t.continuation.decisions].count("superseded") == 1
    assert "Only the API, no UI" in t.spec and "API and UI, final" in t.spec  # audit
    with pytest.raises(ValidationFailedError):
        await update(db, task_id, t.continuation.revision, supersede_decisions=["nope"])


def test_bare_answers_authorise_nothing() -> None:
    assert classify_answer("B", "", None) == "unresolved"
    assert classify_answer(None, "b", None) == "unresolved"
    assert classify_answer("B", "", ["A", "B"]) == "effective"
    assert classify_answer("C", "", ["A", "B"]) == "unresolved"
    assert (
        classify_answer(None, "Yes, only on the disposable stack", None) == "effective"
    )


# -- concurrency, staleness, repeat claim ------------------------------------------------


async def test_concurrent_writers_and_stale_attempts_do_not_corrupt_context(
    db: Database, clock: FakeClock
) -> None:
    task_id = await new_task(db, acceptance=["a"])
    first = await claim(db, A)
    rev = first.continuation.revision
    await update(db, task_id, rev, objective="one")
    with pytest.raises(ConflictError) as err:  # a second writer on the same read
        await update(db, task_id, rev, objective="two")
    assert err.value.code == "stale_continuation"
    assert (await get(db, task_id)).continuation.objective == "one"

    await observe(
        db, A, first, artifacts=[{"kind": "branch", "ref": "b1", "state": "open"}]
    )
    rev_after = (await get(db, task_id)).continuation.revision
    await observe(
        db, A, first, artifacts=[{"kind": "branch", "ref": "b1", "state": "open"}]
    )
    again = await get(db, task_id)
    assert (
        again.continuation.revision == rev_after
    )  # same observation, same instant: no write
    # observations carry their own time, so a repeat only refreshes it: never duplicates.
    assert [a.ref for a in again.continuation.artifacts] == ["b1"]

    await release(db, task_id)
    second = await claim(db, B)
    assert second.continuation.objective == "one"  # repeat claim loses nothing
    assert [a.ref for a in second.continuation.artifacts] == ["b1"]
    with pytest.raises(LeaseConflictError):  # the old holder is fenced out
        await observe(db, A, first, artifacts=[{"kind": "branch", "ref": "b2"}])
    with pytest.raises(LeaseConflictError):  # and so is a non-holder at the new attempt
        await observe(db, A, second, artifacts=[{"kind": "branch", "ref": "b2"}])
    await observe(db, B, second, artifacts=[{"kind": "branch", "ref": "b3"}])
    assert len((await get(db, task_id)).continuation.artifacts) == 2


async def test_interruptions_are_remembered_for_the_next_attempt(db: Database) -> None:
    task_id = await new_task(db, max_attempts=3)
    t = await claim(db)
    async with db.write() as tx:
        await tasks.fail(
            tx, A, task_id, t.attempt, "gate failed: lint (token=abcd1234efgh5678)"
        )
    t = await claim(db, B)
    i = t.continuation.interruption
    assert i is not None and i.reason == "worker_fail"
    assert "abcd1234efgh5678" not in (i.detail or "")  # secrets never persist


async def test_contract_inputs_are_closed_and_redacted(db: Database) -> None:
    task_id = await new_task(db)
    t = await claim(db)
    with pytest.raises(
        ValidationError
    ):  # provenance and time are Argus's, not the caller's
        ObservationRequest(
            attempt=t.attempt,
            artifacts=[{"kind": "pr", "ref": "1", "provenance": "orchestrator"}],
        )
    with pytest.raises(ValidationError):
        ContinuationUpdate(expected_revision=0, reason="x", phase="ship_it")
    await observe(
        db,
        A,
        t,
        wip={"summary": "kept; api_key=sk-abcdefghijklmnopqrstuv", "ref": "ab33ee0"},
    )
    wip = (await get(db, task_id)).continuation.wip
    assert wip is not None and "sk-abcdefghijklmnop" not in wip.summary


def test_older_observation_never_overwrites_a_newer_one() -> None:
    from datetime import UTC, datetime

    from argus_api.tasks.continuation import upsert_artifact

    def art(state: str, minute: int) -> ContinuationArtifact:
        return ContinuationArtifact(
            kind="pr",
            ref="9",
            state=state,
            provenance="runtime_observed",
            observed_at=datetime(2026, 1, 1, 0, minute, tzinfo=UTC),
        )

    merged = upsert_artifact([], art("merged", 5))
    kept = upsert_artifact(merged, art("open", 1))
    assert kept[0].state is ArtifactState.MERGED


async def test_head_moved_or_unmerged_becomes_bounded_reconciliation(
    db: Database,
) -> None:
    task_id = await new_task(db, acceptance=["a"])
    t = await claim(db)
    await observe(
        db,
        A,
        t,
        artifacts=[
            {"kind": "pr", "ref": "7", "state": "merged", "head_sha": "aaaaaaa"}
        ],
    )
    t = await observe(
        db,
        A,
        t,
        artifacts=[{"kind": "pr", "ref": "7", "state": "open", "head_sha": "bbbbbbb"}],
    )
    [r] = t.continuation.reconciliation
    assert r.subject == "pr 7" and "merged" in r.recorded and "open" in r.observed
    assert t.continuation.phase is ContinuationPhase.RECONCILE
    # Settling it is an explicit, audited orchestrator act.
    t = await update(db, task_id, t.continuation.revision, clear_reconciliation=True)
    assert t.continuation.phase is not ContinuationPhase.RECONCILE


async def test_a_task_that_predates_the_contract_reads_as_incomplete(
    db: Database,
) -> None:
    task_id = await new_task(db, spec="old")
    async with db.write() as tx:  # a row from before the column existed
        await tx.conn.execute(
            "UPDATE tasks SET continuation = NULL WHERE id = ?", (task_id,)
        )
    t = await get(db, task_id)
    c = t.continuation
    assert c is not None and c.phase is ContinuationPhase.IMPLEMENT and c.revision == 0
    assert "acceptance_not_structured" in c.gaps and "artifacts_unknown" in c.gaps
    assert t.state is S.READY  # reading it marks nothing done


# -- the same journey over HTTP: auth, isolation, the claim a worker really receives ------

ADMIN_TOKEN = "test-admin-token-" + "c" * 32


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}) as c:
        yield c


def register(client: TestClient, role: str = "worker") -> dict[str, str]:
    boot = client.post("/api/agents/bootstrap-tokens", json={"role": role}).json()
    res = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    return {"Authorization": f"Bearer {res.json()['token']}"}


def test_the_contract_over_http_is_scoped_fenced_and_survives_a_release(
    client: TestClient,
) -> None:
    w1, w2, orch = register(client), register(client), register(client, "orchestrator")
    created = client.post(
        "/api/tasks",
        json={
            "project": "global",
            "title": "CA2",
            "acceptance": ["collector stores splits"],
        },
    ).json()
    tid = created["id"]
    first = client.post("/api/tasks/claim", json={}, headers=w1).json()
    assert first["continuation"]["phase"] == "implement"
    base = f"/api/tasks/{tid}/continuation"

    # Only the holder reads it among workers; only managers write it.
    assert client.get(base, headers=w2).status_code == 403
    assert client.get(base, headers=w1).status_code == 200
    patch = {
        "expected_revision": 0,
        "reason": "known PR",
        "artifacts": [{"kind": "pr", "ref": "1100", "state": "open"}],
    }
    assert client.patch(base, json=patch, headers=w1).status_code == 403
    # Provenance and time are not the caller's to set.
    forged = {
        **patch,
        "artifacts": [{"kind": "pr", "ref": "1", "provenance": "orchestrator"}],
    }
    assert client.patch(base, json=forged, headers=orch).status_code == 422

    # A worker that does not hold the attempt cannot report observations.
    obs = {
        "attempt": first["attempt"],
        "artifacts": [{"kind": "pr", "ref": "1099", "state": "merged"}],
    }
    other = client.post(f"/api/tasks/{tid}/observations", json=obs, headers=w2)
    assert other.status_code in (403, 409)
    stale = client.post(
        f"/api/tasks/{tid}/observations", json={**obs, "attempt": 99}, headers=w1
    )
    assert (stale.status_code, stale.json()["detail"]["code"]) == (409, "stale_attempt")
    ok = client.post(f"/api/tasks/{tid}/observations", json=obs, headers=w1)
    assert ok.status_code == 200 and ok.json()["continuation"]["phase"] == "qa"

    done = client.patch(
        base,
        json={**patch, "expected_revision": ok.json()["continuation"]["revision"]},
        headers=orch,
    )
    assert done.status_code == 200
    again = client.patch(
        base,
        json={**patch, "expected_revision": ok.json()["continuation"]["revision"]},
        headers=orch,
    )
    assert (again.status_code, again.json()["detail"]["code"]) == (
        409,
        "stale_continuation",
    )

    # An assistance ask goes to the orchestrator audience, whatever the worker sends.
    asked = client.post(
        f"/api/tasks/{tid}/ask",
        json={
            "attempt": first["attempt"],
            "question": "need a browser",
            "audience": "human",
            "category": "assistance",
            "resource": "browser",
        },
        headers=w1,
    )
    assert asked.status_code == 201 and asked.json()["audience"] == "orchestrator"
    answered = client.post(
        f"/api/decisions/{asked.json()['id']}/answer",
        json={"text": "use image qa-browser:1"},
        headers=orch,
    )
    assert answered.status_code == 200

    # The next claim receives the full contract on the wire.
    second = client.post("/api/tasks/claim", json={}, headers=w2).json()
    c = second["continuation"]
    assert second["attempt"] == first["attempt"] + 1
    assert c["phase"] == "qa" and c["pending"]
    assert {a["ref"] for a in c["artifacts"]} == {"1099", "1100"}
    assert c["decisions"][0]["category"] == "assistance"
    assert c["interruption"]["reason"] == "asked"
    assert client.get(base, headers=w2).json()["revision"] == c["revision"]
