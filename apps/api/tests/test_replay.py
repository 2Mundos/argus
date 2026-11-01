"""The dev-only replay worker against the real API (no Docker, no model)."""

import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from argus_api.db import Database
from argus_api.main import app
from argus_api.replay import ENV, POOL, cli, fixture
from argus_api.replay.player import NotReplayTask, claim, play
from fastapi.testclient import TestClient

ADMIN_TOKEN = "test-admin-token-" + "r" * 32
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
SAMPLE = Path(__file__).parent / "fixtures" / "replay" / "sample-attempt.json"


class SkewedClock:
    """Real time plus an offset the replay's `sleep` advances, so the API's rate
    limits pass without waiting."""

    def __init__(self) -> None:
        self.offset = timedelta()

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.offset

    async def sleep(self, seconds: float) -> None:
        self.offset += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> SkewedClock:
    clock = SkewedClock()
    real_open = Database.open.__func__

    async def open_with_clock(cls, path: Path, **kw: Any) -> Database:
        return await real_open(cls, path, **{**kw, "clock": clock})

    monkeypatch.setattr(Database, "open", classmethod(open_with_clock))
    return clock


@pytest.fixture
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: SkewedClock
) -> Generator[TestClient]:
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    with TestClient(app) as c:
        yield c


def new_task(client: TestClient, *capabilities: str) -> str:
    res = client.post(
        "/api/tasks",
        json={
            "project": "global",
            "title": "replayed",
            "required_capabilities": list(capabilities),
        },
        headers=ADMIN,
    )
    res.raise_for_status()
    return res.json()["id"]


def replay(
    client: TestClient,
    clock: SkewedClock,
    recorded: dict,
    *,
    hook: Any = None,
    speed: float = 1000.0,
):
    """Register, claim and play `recorded` on the app's event loop. `hook(tool, args)`
    runs before each call, to act as an operator mid-playback."""

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app), base_url="http://127.0.0.1"
        ) as http:
            inner = cli.make_call(http, await cli.register(http, ADMIN_TOKEN))

            async def call(tool: str, **arguments: Any) -> Any:
                if hook is not None:
                    await hook(http, tool, arguments)
                return await inner(tool, **arguments)

            task = await claim(call, wait_s=1)
            assert task is not None
            return task, await play(
                task, recorded, call, clock.sleep, speed=speed, heartbeat_s=0
            )

    return client.portal.call(run)


def test_replay_plays_the_fixture_into_task_state(
    client: TestClient, clock: SkewedClock
) -> None:
    task_id = new_task(client, POOL)
    recorded = fixture.load(SAMPLE)

    _, result = replay(client, clock, recorded)

    assert (result.status, result.task_id) == ("complete", task_id)
    done = client.get(f"/api/tasks/{task_id}", headers=ADMIN).json()
    # The fixture holds no checklist and the commit milestone makes the attempt a
    # delivery, so Argus sends the result to review rather than accepting it.
    assert done["state"] == "in_review"
    assert done["result"]["summary"] == recorded["final"]["result"]["summary"]
    assert done["progress_kind"] == "commit_created"
    assert done["progress_data"]["branch"] == "feat/sample"
    lines = client.get(f"/api/tasks/{task_id}/log", headers=ADMIN).json()["lines"]
    assert [(ln["source"], ln["line"]) for ln in lines] == [
        (s["source"], s["line"]) for s in recorded["steps"] if s["type"] == "log"
    ]
    models = client.get(f"/api/tasks/{task_id}/models", headers=ADMIN).json()
    assert json.dumps(models).count("harness-1") >= 1


def test_a_steer_during_playback_is_delivered_and_logged(
    client: TestClient, clock: SkewedClock
) -> None:
    task_id = new_task(client, POOL)
    sent = False

    async def hook(http: httpx.AsyncClient, tool: str, args: dict) -> None:
        nonlocal sent
        if tool == "report_progress" and not sent:
            sent = True
            res = await http.post(
                f"/api/tasks/{task_id}/steers",
                json={"message": "use tabs"},
                headers=ADMIN,
            )
            assert res.status_code == 201, res.text

    _, result = replay(client, clock, fixture.load(SAMPLE), hook=hook)

    assert (result.status, result.steers) == ("complete", ["use tabs"])
    lines = client.get(f"/api/tasks/{task_id}/log", headers=ADMIN).json()["lines"]
    assert "[replay] steer: use tabs" in [ln["line"] for ln in lines]
    [steer] = client.get(f"/api/tasks/{task_id}/steers", headers=ADMIN).json()["items"]
    assert steer["state"] == "delivered"


def test_a_cancel_during_playback_stops_it(
    client: TestClient, clock: SkewedClock
) -> None:
    task_id = new_task(client, POOL)
    recorded = fixture.load(SAMPLE)

    async def hook(http: httpx.AsyncClient, tool: str, args: dict) -> None:
        if tool == "report_progress" and args.get("kind") == "commit_created":
            res = await http.post(f"/api/tasks/{task_id}/cancel", headers=ADMIN)
            assert res.status_code == 200, res.text

    _, result = replay(client, clock, recorded, hook=hook)

    assert result.status == "cancelled"
    task = client.get(f"/api/tasks/{task_id}", headers=ADMIN).json()
    assert task["state"] == "cancelled"
    assert task["result"] is None
    lines = client.get(f"/api/tasks/{task_id}/log", headers=ADMIN).json()["lines"]
    assert "writing the summary" not in [ln["line"] for ln in lines]


def test_replay_refuses_a_task_outside_the_pool(
    client: TestClient, clock: SkewedClock
) -> None:
    new_task(client)  # untagged tasks match any worker

    with pytest.raises(NotReplayTask):
        replay(client, clock, fixture.load(SAMPLE))


def test_disabled_without_the_env_var(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)

    def no_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the disabled command must not register or connect")

    monkeypatch.setattr(cli, "run", no_network)
    assert cli.main(["--fixture", str(SAMPLE)]) == 2
    assert ENV in capsys.readouterr().err


def test_nothing_registers_the_pool_outside_the_command(
    client: TestClient,
) -> None:
    agents = client.get("/api/agents", headers=ADMIN).json()
    assert agents == []
    assert POOL not in [
        p["name"] for p in client.get("/api/pools", headers=ADMIN).json()
    ]
    assert "replay" not in app.state.__dict__ and not any(
        "replay" in getattr(r, "path", "") for r in app.routes
    )


def test_enabled_runs_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV, "1")
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", ADMIN_TOKEN)
    seen: list[tuple[str, float]] = []

    async def fake_run(url: str, token: str, path: Path, speed: float) -> int:
        seen.append((url, speed))
        return 0

    monkeypatch.setattr(cli, "run", fake_run)
    assert cli.main(["--fixture", str(SAMPLE), "--speed", "4"]) == 0
    assert seen == [("http://127.0.0.1:8000", 4.0)]


def test_load_rejects_unordered_steps(tmp_path: Path) -> None:
    bad = fixture.load(SAMPLE)
    bad["steps"][1]["t"] = 99
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="ordered"):
        fixture.load(path)
