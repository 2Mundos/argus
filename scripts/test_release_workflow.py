"""The release workflow publishes only a complete release (docs/specs/releases.spec.md 3.9).

`validate` reads the parsed workflow, so the negative cases are mutated copies
of it, never files.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = (
    Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yaml"
)
PERMISSIONS = {"contents": "write", "packages": "write"}


def needs(jobs: dict[str, Any], name: str) -> set[str]:
    n = jobs[name].get("needs", [])
    return {n} if isinstance(n, str) else set(n)


def ancestors(jobs: dict[str, Any], name: str) -> set[str]:
    seen: set[str] = set()
    todo = list(needs(jobs, name))
    while todo:
        j = todo.pop()
        if j not in seen:
            seen.add(j)
            todo.extend(needs(jobs, j))
    return seen


def steps_with(job: dict[str, Any], text: str) -> list[str]:
    return [
        s.get("name", "") for s in job.get("steps", []) if text in str(s.get("run", ""))
    ]


def validate(wf: dict[str, Any]) -> list[str]:
    """Why this workflow could publish an incomplete release; empty when it cannot."""
    jobs, errs = wf["jobs"], []
    if wf.get("permissions") != PERMISSIONS:
        errs.append("workflow permissions changed")
    for name, job in jobs.items():
        if "permissions" in job and job["permissions"] != PERMISSIONS:
            errs.append(f"{name}: permissions differ from the workflow's")
    for name in ("publish", "worker-images", "worker-manifests", "finalize"):
        if name not in jobs:
            return [*errs, f"missing job {name}"]
    if "publish" not in needs(jobs, "worker-images"):
        errs.append("worker-images must need publish")
    if not {"publish", "worker-images"} <= needs(jobs, "worker-manifests"):
        errs.append("worker-manifests must need publish and worker-images")
    if "worker-manifests" not in needs(jobs, "finalize"):
        errs.append("finalize must need worker-manifests")
    if not {"publish", "worker-images", "worker-manifests"} <= ancestors(
        jobs, "finalize"
    ):
        errs.append("finalize must follow every build job")
    for name, job in jobs.items():
        if name != "finalize":
            if steps_with(job, "gh release create"):
                errs.append(f"{name} creates the GitHub release")
            if steps_with(job, ":latest"):
                errs.append(f"{name} tags latest")
    if not steps_with(jobs["finalize"], "gh release create"):
        errs.append("finalize does not create the release")
    if not steps_with(jobs["finalize"], "--clobber"):
        errs.append("finalize must upload assets with --clobber")
    if not steps_with(jobs["worker-manifests"], "imagetools inspect"):
        errs.append("worker-manifests does not verify the manifests")
    matrix = jobs["worker-images"]["strategy"]["matrix"]["include"]
    if {m["arch"] for m in matrix} != {"amd64", "arm64"}:
        errs.append("worker-images must build amd64 and arm64")
    return errs


@pytest.fixture
def workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text())


def test_release_workflow_is_ordered_and_least_privilege(
    workflow: dict[str, Any],
) -> None:
    assert validate(workflow) == []


def test_release_workflow_takes_the_tag_and_notes_from_publish(
    workflow: dict[str, Any],
) -> None:
    out = workflow["jobs"]["publish"]["outputs"]
    assert {"tag", "notes", "image", "owner"} <= set(out)


def mutate(wf: dict[str, Any], fn: Any) -> dict[str, Any]:
    wf = copy.deepcopy(wf)
    fn(wf["jobs"])
    return wf


@pytest.mark.parametrize(
    ("fn", "expected"),
    [
        (lambda j: j["worker-images"].pop("needs"), "worker-images must need publish"),
        (
            lambda j: j["worker-manifests"].update(needs=["publish"]),
            "worker-manifests must need publish and worker-images",
        ),
        (
            lambda j: j["finalize"].update(needs=["publish"]),
            "finalize must need worker-manifests",
        ),
        (
            lambda j: j["publish"]["steps"].append({"run": "gh release create $TAG"}),
            "publish creates the GitHub release",
        ),
        (
            lambda j: j["publish"]["steps"].append(
                {"run": 'tags+=(-t "$IMAGE:latest")'}
            ),
            "publish tags latest",
        ),
        (
            lambda j: j["worker-images"]["strategy"]["matrix"].update(
                include=[{"arch": "amd64", "runner": "ubuntu-latest"}]
            ),
            "worker-images must build amd64 and arm64",
        ),
        (
            lambda j: j["finalize"].update(
                permissions={
                    "contents": "write",
                    "packages": "write",
                    "id-token": "write",
                }
            ),
            "finalize: permissions differ from the workflow's",
        ),
    ],
)
def test_validator_rejects_an_incomplete_ordering(
    workflow: dict[str, Any], fn: Any, expected: str
) -> None:
    assert expected in validate(mutate(workflow, fn))


def test_validator_rejects_widened_workflow_permissions(
    workflow: dict[str, Any],
) -> None:
    workflow["permissions"]["id-token"] = "write"
    assert "workflow permissions changed" in validate(workflow)
