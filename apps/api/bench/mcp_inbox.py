"""Inbox journey benchmark (docs/specs/mcp-operations.spec.md 3.5).

Seeds a fixed synthetic set into a throwaway in-process Argus, then runs the
orchestrator's inbox journey for project `alpha`:

- answer each question addressed to orchestrators;
- accept or reject each review from its report artifact, after checking the
  artifact's size and SHA-256;
- list approvals that wait for a human;
- read back the effects.

It measures what an agent would carry: the number of calls, request and
response bytes, and the cumulative context. Tokens are estimated as
bytes / 4 and labelled as an estimate. Every run must reach the same expected
outcome, or it fails.

`journey_before` is frozen: it uses only the tools that existed before the
spec, plus the REST scripting the agent needed where MCP had no tool. Its
numbers are recorded in `results/before.json`. The tool schemas of that time
are in `results/tools.before.json`. Later journeys are added next to it and
never change it. It runs only on the tree it was recorded on (8c14188): the
tools it calls have changed shape since, so check that commit out to rerun it.

`journey_after` uses the P0 read tools; its numbers are in `results/after.json`.
`journey_answers` adds the batch answer (step 4), in `results/answers.json`.
Each recorded journey stays as it was; a later step adds its own.

    uv run python apps/api/bench/mcp_inbox.py [answers|after|before] [--out FILE]
"""

import argparse
import hashlib
import itertools
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

HERE = Path(__file__).parent
ADMIN_TOKEN = "bench-admin-token-" + "b" * 32
PROJECTS = ("alpha", "beta", "gamma")
TARGET = "alpha"
# Questions, reviews, approvals and filler (ready or succeeded) per project.
LAYOUT = {
    "alpha": (6, 4, 2, 6),
    "beta": (4, 2, 1, 5),
    "gamma": (2, 2, 1, 5),
}
PARAGRAPH = (
    "Context: the service exposes a paginated listing whose ordering must stay"
    " stable across retries; callers depend on it for incremental sync. Keep the"
    " public contract, add tests for the edge cases, and document the behaviour. "
)


# -- metering ------------------------------------------------------------------


@dataclass
class Metrics:
    mcp_calls: int = 0
    rest_calls: int = 0
    request_bytes: int = 0
    response_bytes: int = 0
    tools_list_bytes: int = 0
    by_name: dict[str, dict[str, int]] = field(default_factory=dict)

    def count(self, name: str, response_bytes: int) -> None:
        entry = self.by_name.setdefault(name, {"calls": 0, "response_bytes": 0})
        entry["calls"] += 1
        entry["response_bytes"] += response_bytes

    @property
    def context_bytes(self) -> int:
        """Everything the agent sends and reads, schemas included."""
        return self.request_bytes + self.response_bytes + self.tools_list_bytes


class Agent:
    """An MCP/REST client that meters every byte the agent would see."""

    def __init__(self, client: TestClient, token: str) -> None:
        self.client, self.token = client, token
        self.metrics = Metrics()
        self._ids = itertools.count(1)

    def _rpc(self, method: str, params: dict) -> dict:
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        body["params"] = params
        res = self.client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {self.token}",
            },
            json=body,
        )
        res.raise_for_status()
        return {
            "request": len(json.dumps(body)),
            "body": res.json(),
            "size": len(res.content),
        }

    def list_tools(self) -> list[dict]:
        out = self._rpc("tools/list", {})
        self.metrics.tools_list_bytes += out["size"]
        return out["body"]["result"]["tools"]

    def call(self, name: str, **arguments: Any) -> Any:
        out = self._rpc("tools/call", {"name": name, "arguments": arguments})
        m = self.metrics
        m.mcp_calls += 1
        m.request_bytes += out["request"]
        m.response_bytes += out["size"]
        m.count(name, out["size"])
        result = out["body"]["result"]
        if result["isError"]:
            raise RuntimeError(f"{name}: {result['content'][0]['text']}")
        structured = result.get("structuredContent")
        if structured is not None:
            return structured.get("result", structured)
        return json.loads(result["content"][0]["text"])

    def rest(self, route: str, **ids: str) -> bytes:
        """An ad hoc REST read: what the agent scripts where MCP has no tool."""
        path = route.format(**ids)
        res = self.client.get(
            "/api" + path, headers={"Authorization": f"Bearer {self.token}"}
        )
        res.raise_for_status()
        m = self.metrics
        m.rest_calls += 1
        m.request_bytes += len(path)
        m.response_bytes += len(res.content)
        m.count("REST GET " + route, len(res.content))
        return res.content


# -- the synthetic set -----------------------------------------------------------


@dataclass
class Expected:
    """The outcome every journey must reach."""

    states: dict[str, str]  # task id -> state after the journey
    for_human: list[str]  # alpha tasks awaiting a human's approval


def register(client: TestClient, role: str) -> str:
    boot = client.post(
        "/api/agents/bootstrap-tokens",
        json={"role": role},
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    ).json()
    res = client.post("/api/agents/register", json={"bootstrap_token": boot["token"]})
    return res.json()["token"]


def seed(client: TestClient) -> Expected:
    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    def api(method: str, path: str, body: Any = None, token: str | None = None) -> Any:
        headers = {"Authorization": f"Bearer {token}"} if token else admin
        res = client.request(method, "/api" + path, json=body, headers=headers)
        res.raise_for_status()
        return res.json() if res.content else None

    worker = register(client, "worker")
    for slug in PROJECTS:
        api("PUT", f"/projects/{slug}", {"name": slug.title()})

    n = itertools.count(1)
    expected = Expected(states={}, for_human=[])

    def create(project: str, **extra: Any) -> dict:
        i = next(n)
        return api(
            "POST",
            "/tasks",
            {
                "title": f"{project}: change {i:02d} to the listing endpoint",
                "project": project,
                "spec": PARAGRAPH * 6,
                "acceptance": ["tests cover empty and last pages", "docs updated"],
                "input": {"repo": f"{project}/service", "ticket": f"OPS-{100 + i}"},
                **extra,
            },
        )

    def claim(task: dict) -> int:
        claimed = api("POST", "/tasks/claim", {"wait_s": 0}, worker)
        assert claimed["id"] == task["id"], "seed claims tasks in creation order"
        return claimed["attempt"]

    for project, (questions, reviews, approvals, filler) in LAYOUT.items():
        for q in range(questions):
            task = create(project)
            attempt = claim(task)
            prefer = ("keep", "change")[q % 2]
            api(
                "POST",
                f"/tasks/{task['id']}/ask",
                {
                    "attempt": attempt,
                    "question": f"{PARAGRAPH}Should the cursor format {prefer}?"
                    f" The owner noted earlier: prefer {prefer}.",
                    "options": ["keep", "change"],
                    "audience": "orchestrator",
                },
                worker,
            )
            if project == TARGET:
                expected.states[task["id"]] = "ready"
        for r in range(reviews):
            task = create(project, review="orchestrator")
            attempt = claim(task)
            verdict = ("pass", "fail")[r % 2]
            report = f"# QA report\n\n{PARAGRAPH * 4}\n\nverdict: {verdict}\n"
            uploaded = client.post(
                f"/api/tasks/{task['id']}/artifacts",
                params={"attempt": attempt, "name": "report.md"},
                content=report.encode(),
                headers={
                    "Authorization": f"Bearer {worker}",
                    "Content-Type": "text/markdown",
                },
            ).json()
            api(
                "POST",
                f"/tasks/{task['id']}/complete",
                {
                    "attempt": attempt,
                    "result": {
                        "summary": PARAGRAPH * 2,
                        "data": {"files_changed": 7, "tests_added": 12},
                        "artifacts": [uploaded["id"]],
                    },
                },
                worker,
            )
            if project == TARGET:
                expected.states[task["id"]] = (
                    "succeeded" if verdict == "pass" else "ready"
                )
        for _ in range(approvals):
            task = create(project, requires_approval=True)
            if project == TARGET:
                expected.for_human.append(task["id"])
        for _ in range(filler - filler // 2):
            task = create(project)
            attempt = claim(task)
            api(
                "POST",
                f"/tasks/{task['id']}/complete",
                {"attempt": attempt, "result": {"summary": PARAGRAPH}},
                worker,
            )
    for project, (*_, filler) in LAYOUT.items():  # ready: created after every claim
        for _ in range(filler // 2):
            create(project)

    for name in ("builders", "reviewers"):
        api(
            "PUT",
            f"/pools/{name}",
            {"image": "example/worker:1", "desired_replicas": 0, "max_replicas": 1},
        )
    orchestrator = register(client, "orchestrator")
    for name in ("builders", "reviewers"):
        api("POST", f"/pools/{name}/scale", {"desired_replicas": 4}, orchestrator)
    expected.for_human.sort()
    return expected


# -- journeys ------------------------------------------------------------------


def pick_option(question: str, options: list[str]) -> str:
    """The rule the agent applies: the owner's stated preference."""
    return next(o for o in options if f"prefer {o}." in question)


def verdict_of(report: bytes) -> str:
    return "accept" if b"verdict: pass" in report else "reject"


def journey_before(agent: Agent) -> dict[str, Any]:
    """Frozen: the pre-spec tool set (29 tools, as of 57efba3) plus REST scripts."""
    agent.list_tools()
    tasks: list[dict] = []
    for state in ("blocked", "in_review", "awaiting_approval"):
        tasks += agent.call("list_tasks", state=state, limit=500)
    mine = {t["id"]: t for t in tasks if t["project"] == TARGET}
    decisions = {d["subject_id"]: d for d in agent.call("list_decisions")}

    touched: list[str] = []
    for_human: list[str] = []
    for task_id, task in mine.items():
        decision = decisions.get(task_id)
        if decision is None:
            continue
        if decision["audience"] == "human":
            for_human.append(task_id)
        elif decision["kind"] == "question":
            choice = pick_option(decision["question"], decision["options"])
            agent.call("answer_decision", decision_id=decision["id"], choice=choice)
            touched.append(task_id)
        elif decision["kind"] == "review":
            artifacts = json.loads(agent.rest("/tasks/{id}/artifacts", id=task_id))
            ids = set(task["result"]["artifacts"])
            report = next(a for a in artifacts if a["id"] in ids)
            content = agent.rest("/artifacts/{id}/content", id=report["id"])
            assert len(content) == report["size"]
            assert hashlib.sha256(content).hexdigest() == report["sha256"]
            agent.call(
                "answer_decision",
                decision_id=decision["id"],
                choice=verdict_of(content),
                text="checked report.md (size and sha256 match)",
            )
            touched.append(task_id)
    states = {t: agent.call("get_task", task_id=t)["state"] for t in touched}
    return {"states": states, "for_human": sorted(for_human)}


def journey_after(agent: Agent) -> dict[str, Any]:
    """The P0 read tools (MCP operations step 3): the inbox, decision details in
    one page, artifact checks on the server, and a batch read-back."""
    agent.list_tools()
    inbox = agent.call("get_inbox", project=TARGET, limit=100)
    assert not inbox["has_more"]
    rows = [r for r in inbox["items"] if r["decision_id"] is not None]
    questions = agent.call(
        "list_decisions", project=TARGET, kinds=["question"], view="detail", limit=100
    )
    assert not questions["has_more"]
    asked = {d["id"]: d for d in questions["items"]}

    touched: list[str] = []
    for_human: list[str] = []
    for row in rows:
        task_id = row["subject_id"]
        if row["audience"] == "human":
            for_human.append(task_id)
        elif row["decision_kind"] == "question":
            decision = asked[row["decision_id"]]
            choice = pick_option(decision["question"], decision["options"])
            agent.call("answer_decision", decision_id=decision["id"], choice=choice)
            touched.append(task_id)
        elif row["decision_kind"] == "review":
            listed = agent.call("list_artifacts", task_id=task_id)
            report = next(a for a in listed if a["in_result"])
            assert agent.call("verify_artifact", artifact_id=report["id"])["ok"]
            read = agent.call("read_artifact", artifact_id=report["id"])
            assert read["next_offset"] is None
            agent.call(
                "answer_decision",
                decision_id=row["decision_id"],
                choice=verdict_of(read["text"].encode()),
                text="checked report.md (verified on the server)",
            )
            touched.append(task_id)
    back = agent.call("get_tasks", ids=touched)
    assert not back["missing"]
    states = {t["id"]: t["state"] for t in back["items"]}
    return {"states": states, "for_human": sorted(for_human)}


def journey_answers(agent: Agent) -> dict[str, Any]:
    """Step 4: the same reads as `after`, then every answer in one
    answer_decisions call, each with a request id so a retry is safe."""
    agent.list_tools()
    inbox = agent.call("get_inbox", project=TARGET, limit=100)
    assert not inbox["has_more"]
    questions = agent.call(
        "list_decisions", project=TARGET, kinds=["question"], view="detail", limit=100
    )
    assert not questions["has_more"]
    asked = {d["id"]: d for d in questions["items"]}

    answers: list[dict[str, Any]] = []
    touched: list[str] = []
    for_human: list[str] = []
    for row in inbox["items"]:
        task_id, decision_id = row["subject_id"], row["decision_id"]
        if decision_id is None:
            continue
        if row["audience"] == "human":
            for_human.append(task_id)
            continue
        if row["decision_kind"] == "question":
            decision = asked[decision_id]
            answer = {"choice": pick_option(decision["question"], decision["options"])}
        elif row["decision_kind"] == "review":
            listed = agent.call("list_artifacts", task_id=task_id)
            report = next(a for a in listed if a["in_result"])
            assert agent.call("verify_artifact", artifact_id=report["id"])["ok"]
            read = agent.call("read_artifact", artifact_id=report["id"])
            assert read["next_offset"] is None
            answer = {
                "choice": verdict_of(read["text"].encode()),
                "text": "checked report.md (verified on the server)",
            }
        else:
            continue
        answers.append(
            {"decision_id": decision_id, "request_id": decision_id, **answer}
        )
        touched.append(task_id)
    results = agent.call("answer_decisions", answers=answers)["results"]
    assert all(r["ok"] for r in results), results
    back = agent.call("get_tasks", ids=touched)
    assert not back["missing"]
    states = {t["id"]: t["state"] for t in back["items"]}
    return {"states": states, "for_human": sorted(for_human)}


JOURNEYS: dict[str, Callable[[Agent], dict[str, Any]]] = {
    "before": journey_before,
    "after": journey_after,
    "answers": journey_answers,
}


# -- runner --------------------------------------------------------------------


def tools_list(client: TestClient, token: str) -> dict[str, int]:
    """What `tools/list` costs a client of this scope: tool count and bytes."""
    probe = Agent(client, token)
    tools = probe.list_tools()
    return {"tools": len(tools), "bytes": probe.metrics.tools_list_bytes}


def run(name: str, schemas: Path | None = None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as data_dir:
        os.environ["ARGUS_DATA_DIR"] = data_dir
        os.environ["ARGUS_ADMIN_TOKEN"] = ADMIN_TOKEN
        from argus_api.main import app

        with TestClient(app) as client:
            expected = seed(client)
            agent = Agent(client, register(client, "orchestrator"))
            started = time.perf_counter()
            outcome = JOURNEYS[name](agent)
            wall_s = time.perf_counter() - started
            tokens = {
                "worker": register(client, "worker"),
                "orchestrator": agent.token,
                "admin": ADMIN_TOKEN,
            }
            listing = {scope: tools_list(client, t) for scope, t in tokens.items()}
            if schemas is not None:  # the full tool set, as an orchestrator sees it
                tools = Agent(client, agent.token).list_tools()
                schemas.write_text(json.dumps(tools, indent=2) + "\n")
    if outcome != asdict(expected):
        raise SystemExit(f"journey {name!r} reached a different outcome:\n{outcome}")
    m = agent.metrics
    return {
        "journey": name,
        "outcome_ok": True,
        "tasks_resolved": len(outcome["states"]),
        "for_human": len(outcome["for_human"]),
        "mcp_calls": m.mcp_calls,
        "rest_calls": m.rest_calls,
        "by_name": dict(sorted(m.by_name.items())),
        "tools_list_bytes": m.tools_list_bytes,
        "tools_list_by_scope": listing,
        "request_bytes": m.request_bytes,
        "response_bytes": m.response_bytes,
        "context_bytes": m.context_bytes,
        "context_tokens_estimate": m.context_bytes // 4,
        "wall_s": round(wall_s, 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("journey", nargs="?", default="answers", choices=JOURNEYS)
    parser.add_argument("--out", type=Path, help="write the result as JSON here")
    parser.add_argument("--schemas", type=Path, help="write tools/list here")
    args = parser.parse_args()
    result = run(args.journey, args.schemas)
    text = json.dumps(result, indent=2) + "\n"
    if args.out:
        args.out.write_text(text)
    sys.stdout.write(text)


if __name__ == "__main__":
    main()
