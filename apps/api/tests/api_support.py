"""Test helpers for the API tests (importable because of pytest `pythonpath`)."""

from datetime import UTC, datetime, timedelta

from argus_api.db import Database, ts
from argus_core.tasks import TaskResult

DONE = TaskResult(summary="done")


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


async def insert_agent(db: Database, agent_id: str, state: str = "active") -> None:
    """Shortcut for tests that need an agent row but not the registration flow."""
    async with db.write() as tx:
        ended = ts(tx.now) if state in ("gone", "lost") else None
        await tx.conn.execute(
            "INSERT INTO agents (id, name, role, capabilities, state,"
            " last_heartbeat_at, created_at, ended_at)"
            " VALUES (?, ?, 'worker', '[]', ?, ?, ?, ?)",
            (agent_id, agent_id, state, ts(tx.now), ts(tx.now), ended),
        )


class FakeTarget:
    """In-memory Target with switches for the failures the reconciler must survive."""

    def __init__(self) -> None:
        from argus_api.fleet.targets import ContainerSpec

        self.containers: dict[str, dict] = {}
        self.specs: dict[str, ContainerSpec] = {}
        self.reachable = True
        self.fail_start = False
        self.ambiguous_start = False  # creates the container, then reports failure
        self.fail_stop = False
        self.started: list[str] = []
        self.stopped: list[str] = []
        # Containers the runtime itself created (not started by Argus):
        # name -> {"label": key, "value": value}
        self.children: dict[str, dict] = {}

    def _check(self) -> None:
        from argus_api.fleet.targets import TargetError

        if not self.reachable:
            raise TargetError("unreachable")

    async def start(self, spec):
        from argus_api.fleet.targets import TargetError

        self._check()
        if spec.name in self.containers:
            return spec.name
        if self.fail_start:
            raise TargetError("image not found")
        self.containers[spec.name] = {
            "instance_id": spec.instance_id,
            "pool": spec.labels["argus.pool"],
            "status": "running",
            "exit_code": None,
        }
        self.specs[spec.name] = spec
        self.started.append(spec.name)
        if self.ambiguous_start:
            raise TargetError("timed out waiting for the daemon")
        return spec.name

    async def stop(self, ref, *, grace):
        from argus_api.fleet.targets import TargetError

        self._check()
        if self.fail_stop:
            raise TargetError("stop failed")
        self.containers.pop(ref, None)
        self.children.pop(ref, None)
        self.stopped.append(ref)

    async def list(self, labels):
        from argus_api.fleet.targets import ProviderInstance

        self._check()
        return [
            ProviderInstance(c["instance_id"], name, c["status"], c["exit_code"])
            for name, c in self.containers.items()
            if c["pool"] == labels["argus.pool"]
        ]

    async def list_by_label(self, key):
        from argus_api.fleet.targets import LabeledContainer

        self._check()
        return [
            LabeledContainer(name, c["value"], True)
            for name, c in self.children.items()
            if c["label"] == key
        ]

    async def logs(self, ref, *, tail):
        return f"logs of {ref}"

    def exit(self, name: str, code: int = 1) -> None:
        self.containers[name] |= {"status": "exited", "exit_code": code}
