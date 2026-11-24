"""Path-based review policy (docs/specs/review-policy.spec.md)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from argus_api import main as _main  # noqa: F401 - loads packages in order
from argus_api.errors import ValidationFailedError
from argus_api.settings import service as settings_svc
from argus_api.tasks import independent_review as indep
from argus_api.tasks import review_policy as policy
from argus_api.tasks import review_rounds
from argus_api.tasks import service as tasks
from argus_api.tasks.deliverable import ForgeRead
from argus_api.tasks.delivery import ChangedFiles
from argus_core.settings import IndependentReviewSetting, ReviewPolicySetting
from argus_core.tasks import TaskResult
from test_execution_routes import (
    CODEX_ALT,
    OPUS_ALT,
    parent_events,
    record_execution,
    review_policy,
    reviewed_parent,
)
from test_execution_routes import fleet as _fleet_fixture
from test_pre_merge_independent_review import HEAD_1, HEAD_2, PR_450_URL

fleet = _fleet_fixture

SETTING = IndependentReviewSetting(mode="different_provider")


def _policy(**over: Any) -> ReviewPolicySetting:
    return ReviewPolicySetting.model_validate(
        {
            "mode": "enforce",
            "default": "standard",
            "rules": [
                {"name": "docs", "tier": "skip", "paths": ["docs/**", "*.md"]},
                {"name": "db", "tier": "human", "paths": ["**/migrations/*.sql"]},
                {"name": "src", "tier": "standard", "paths": ["src/**"]},
            ],
            **over,
        }
    )


# -- glob semantics and evaluation ------------------------------------------------


@pytest.mark.parametrize(
    ("glob", "path", "want"),
    [
        ("*.md", "README.md", True),
        ("*.md", "docs/README.md", False),  # `*` stays inside a segment
        ("docs/**", "docs/a/b/c.md", True),
        ("docs/**", "docs", False),
        ("a/**/b.py", "a/b.py", True),  # `**` matches zero segments
        ("a/**/b.py", "a/x/y/b.py", True),
        ("a/**/b.py", "ab.py", False),
        ("**/migrations/*.sql", "migrations/1.sql", True),
        ("**/migrations/*.sql", "x/y/migrations/1.sql", True),
        ("file?.txt", "file1.txt", True),
        ("file?.txt", "file10.txt", False),
        ("file?.txt", "file/.txt", False),
        ("README.md", "readme.md", False),  # case-sensitive
    ],
)
def test_glob_semantics(glob: str, path: str, want: bool) -> None:
    assert policy.matches(glob, path) is want


def test_each_path_takes_the_strictest_matching_tier_and_the_head_the_strictest_path() -> (
    None
):
    both = _policy(
        rules=[
            {"name": "all-docs", "tier": "skip", "paths": ["docs/**"]},
            {"name": "api-docs", "tier": "human", "paths": ["docs/api/**"]},
        ]
    )
    assert policy.evaluate(both, ["docs/a.md"]).tier == "skip"
    one = policy.evaluate(both, ["docs/a.md", "docs/api/x.md"])
    assert one.tier == "human"
    assert {m["rule"] for m in one.matched} == {"all-docs", "api-docs"}


def test_one_migration_makes_a_docs_change_human_and_one_unlisted_path_blocks_skip() -> (
    None
):
    p = _policy()
    assert policy.evaluate(p, ["README.md", "docs/x.md"]).tier == "skip"
    assert policy.evaluate(p, ["docs/x.md", "db/migrations/1.sql"]).tier == "human"
    unlisted = policy.evaluate(p, ["docs/x.md", "tool/run.sh"])
    assert (unlisted.tier, unlisted.unmatched) == ("standard", ["tool/run.sh"])


def test_unknown_and_empty_lists_take_default_and_default_refuses_skip() -> None:
    p = _policy(default="human")
    unknown = policy.evaluate(p, None)
    assert (unknown.tier, unknown.reason) == ("human", policy.FILES_UNKNOWN)
    assert policy.evaluate(p, []).tier == "human"
    with pytest.raises(ValueError, match="default"):
        _policy(default="skip")


def test_unmatched_paths_are_capped() -> None:
    ev = policy.evaluate(_policy(), [f"x/{i}" for i in range(50)])
    assert len(ev.unmatched) == policy.UNMATCHED_CAP


@pytest.mark.parametrize(
    "bad",
    [
        {"mode": "maybe"},
        {"rules": [{"name": "Bad Name", "tier": "skip", "paths": ["a"]}]},
        {"rules": [{"name": "a", "tier": "skip", "paths": []}]},
        {"rules": [{"name": "a", "tier": "skip", "paths": ["/abs"]}]},
        {"rules": [{"name": "a", "tier": "skip", "paths": ["!neg"]}]},
        {"rules": [{"name": "a", "tier": "skip", "paths": ["[ab]"]}]},
        {"rules": [{"name": "a", "tier": "skip", "paths": ["a**b"]}]},
        {
            "rules": [
                {"name": "a", "tier": "skip", "paths": ["a"]},
                {"name": "a", "tier": "human", "paths": ["b"]},
            ]
        },
        {
            "rules": [
                {"name": f"r{i}", "tier": "skip", "paths": ["a"]} for i in range(51)
            ]
        },
    ],
)
async def test_a_malformed_policy_is_refused_naming_the_field(db, bad) -> None:
    async with db.write() as tx:
        with pytest.raises(ValidationFailedError) as err:
            await settings_svc.put_setting(
                tx,
                Path(),
                "global",
                "review.policy",
                {"mode": "enforce", "default": "standard", "rules": [], **bad},
                "a",
            )
    assert any(d.startswith(("mode", "rules")) for d in err.value.details), (
        err.value.details
    )


# -- the gate -------------------------------------------------------------------


async def _setup(db, fleet, mode: str, rules: list[dict] | None = None):
    await review_policy(db, [OPUS_ALT, CODEX_ALT])
    value = _policy(mode=mode).model_dump()
    if rules is not None:
        value["rules"] = rules
    async with db.write() as tx:
        await settings_svc.put_setting(
            tx,
            Path(),
            "global",
            "review.independent",
            {"mode": "different_provider", "max_rounds": 3},
            "admin:test",
        )
        await settings_svc.put_setting(
            tx, Path(), "global", "review.policy", value, "admin:test"
        )
    parent = await reviewed_parent(db, fleet, ("claude-opus-5-5", "anthropic", "cloud"))
    async with db.write() as tx:
        await tx.conn.execute(
            "UPDATE tasks SET result = ? WHERE id = ?",
            (
                TaskResult(
                    summary="pr", data={"pr": PR_450_URL, "head_sha": HEAD_1}
                ).model_dump_json(),
                parent.id,
            ),
        )
    return parent


def _read(head: str, *paths: str | None) -> ForgeRead:
    files = None if paths == (None,) else tuple(p for p in paths if p)
    return ForgeRead(None, ChangedFiles(head, files))


async def _spawn(db, parent_id: str, head: str, forge: ForgeRead | None) -> str | None:
    async with db.write() as tx:
        return await indep.spawn_reviewer(
            tx,
            await tasks.task_row(tx.conn, parent_id),
            pr=PR_450_URL,
            head_sha=head,
            setting=SETTING,
            policy={},
            actor="system",
            forge=forge,
        )


async def _gate(db, parent_id: str, head: str) -> tuple[bool, str | None]:
    async with db.read() as conn:
        return await indep.merge_gate_status(
            conn, await tasks.task_row(conn, parent_id), pr=PR_450_URL, head_sha=head
        )


async def test_enforced_skip_approves_exactly_its_head_and_a_new_head_voids_it(
    db, fleet
) -> None:
    parent = await _setup(db, fleet, "enforce")
    assert (
        await _spawn(db, parent.id, HEAD_1, _read(HEAD_1, "docs/a.md", "x.md")) is None
    )
    [evaluated] = await parent_events(db, parent.id, policy.EVALUATED_EVENT)
    assert (evaluated["tier"], evaluated["mode"], evaluated["files"]) == (
        "skip",
        "enforce",
        2,
    )
    [skipped] = await parent_events(db, parent.id, policy.SKIPPED_EVENT)
    assert skipped == {"pr": PR_450_URL, "head_sha": HEAD_1, "rules": ["docs"]}
    assert await _gate(db, parent.id, HEAD_1) == (True, None)
    assert (await _gate(db, parent.id, HEAD_2))[0] is False
    async with db.read() as conn:
        assert await indep.reviewed_approval_of(conn, parent.id) == (PR_450_URL, HEAD_1)

    async with db.read() as conn:
        [head] = (await review_rounds.review_rounds(conn, parent.id)).policy
    assert (head.tier, head.rules, head.skipped) == ("skip", ["docs"], True)

    # A new head is evaluated again; this one touches a source file: a reviewer runs.
    child = await _spawn(db, parent.id, HEAD_2, _read(HEAD_2, "docs/a.md", "src/a.py"))
    assert child is not None
    assert (await _gate(db, parent.id, HEAD_1))[0] is False
    assert len(await parent_events(db, parent.id, policy.EVALUATED_EVENT)) == 2


@pytest.mark.parametrize(
    "forge", [_read(HEAD_1, None), _read(HEAD_2, "docs/a.md"), None]
)
async def test_unknown_files_never_skip(db, fleet, forge) -> None:
    parent = await _setup(db, fleet, "enforce")
    assert await _spawn(db, parent.id, HEAD_1, forge) is not None
    [evaluated] = await parent_events(db, parent.id, policy.EVALUATED_EVENT)
    assert (evaluated["tier"], evaluated["reason"]) == ("standard", "files_unknown")
    assert await parent_events(db, parent.id, policy.SKIPPED_EVENT) == []


async def test_observe_records_and_changes_nothing(db, fleet) -> None:
    parent = await _setup(db, fleet, "observe")
    child = await _spawn(db, parent.id, HEAD_1, _read(HEAD_1, "docs/a.md"))
    assert child is not None  # reviewed as without the policy
    [evaluated] = await parent_events(db, parent.id, policy.EVALUATED_EVENT)
    assert (evaluated["mode"], evaluated["tier"]) == ("observe", "skip")
    assert await parent_events(db, parent.id, policy.SKIPPED_EVENT) == []
    assert (await _gate(db, parent.id, HEAD_1))[0] is False


async def test_off_records_nothing(db, fleet) -> None:
    parent = await _setup(db, fleet, "off")
    assert await _spawn(db, parent.id, HEAD_1, _read(HEAD_1, "docs/a.md")) is not None
    assert await parent_events(db, parent.id, policy.EVALUATED_EVENT) == []


async def test_human_tier_reviews_then_opens_one_decision_and_never_resumes_the_merge(
    db, fleet
) -> None:
    from argus_api.decisions import store as decisions_store
    from argus_api.tasks import merge_resume

    parent = await _setup(db, fleet, "enforce")
    forge = _read(HEAD_1, "db/migrations/1.sql")
    child = await _spawn(db, parent.id, HEAD_1, forge)
    assert child is not None
    async with db.write() as tx:
        await record_execution(tx, child, 0, "gpt-5.5", "openai", "codex")
        await tx.conn.execute(
            "UPDATE tasks SET state = 'succeeded' WHERE id = ?", (child,)
        )
        await indep.on_review_child_complete(
            tx,
            await tasks.task_row(tx.conn, child),
            TaskResult(
                summary="ok",
                data={"verdict": "approve", "findings": [], "head_sha": HEAD_1},
            ),
            setting=SETTING,
            policy={},
            actor="agent:rev",
            forge=forge,
        )
    async with db.read() as conn:
        ids = await decisions_store.open_ids(conn, "task", parent.id)
        [decision] = [await decisions_store.get(conn, i) for i in ids]
    assert decision.payload == {
        "gate": "policy_human",
        "rules": ["db"],
        "pr": PR_450_URL,
        "head_sha": HEAD_1,
    }
    async with db.write() as tx:
        row = await tasks.task_row(tx.conn, parent.id)
        assert not await merge_resume.resume_merge(
            tx, row, forge, PR_450_URL, HEAD_1, "system"
        )
    assert await parent_events(db, parent.id, merge_resume.RESUMED_EVENT) == []


async def test_the_report_lists_a_skip_head_whose_review_found_a_blocking_problem(
    db, fleet
) -> None:
    parent = await _setup(db, fleet, "observe")
    forge = _read(HEAD_1, "docs/a.md")
    child = await _spawn(db, parent.id, HEAD_1, forge)
    assert child is not None
    verdict = TaskResult(
        summary="no",
        data={
            "verdict": "request_changes",
            "findings": ["P1: broken", "P3: nit"],
            "head_sha": HEAD_1,
        },
    )
    async with db.write() as tx:
        await record_execution(tx, child, 0, "gpt-5.5", "openai", "codex")
        await tx.conn.execute(
            "UPDATE tasks SET state = 'succeeded', result = ? WHERE id = ?",
            (verdict.model_dump_json(), child),
        )
        await indep.on_review_child_complete(
            tx,
            await tasks.task_row(tx.conn, child),
            verdict,
            setting=SETTING,
            policy={},
            actor="agent:rev",
            forge=forge,
        )
        await tx.conn.execute(
            "UPDATE tasks SET state = 'cancelled' WHERE id = ?", (parent.id,)
        )
    async with db.read() as conn:
        report = await policy.policy_report(conn, parent.project, "0", 30)
    skip = report.tiers["skip"]
    assert (skip.heads, skip.tasks, skip.cancelled) == (1, 1, 1)
    assert skip.verdicts["request_changes"] == 1
    assert skip.findings_by_priority == {"P1": 1, "P3": 1}
    assert report.tiers["human"].heads == 0
    [listed] = report.skipped_with_findings
    assert (listed.task_id, listed.pr, listed.head_sha) == (
        parent.id,
        PR_450_URL,
        HEAD_1,
    )
    assert (listed.rules, listed.findings) == (["docs"], 1)
