import asyncio
import shutil
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
from api_support import FakeClock
from argus_api.db import Database


@pytest.fixture(scope="session", autouse=True)
def _migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Generator[None]:
    """Migrate once per session (per xdist worker) and start every new database from
    a copy: running every migration costs ~0.3 s a test, the copy ~0.03 s. Only a
    database that does not exist yet and uses the shipped migrations is seeded, so
    migration tests and reopened databases see exactly what they did before."""
    template = tmp_path_factory.mktemp("db-template") / "argus.db"

    async def build() -> None:
        await (await Database.open(template, readers=0)).close()

    asyncio.run(build())
    real_open = Database.open.__func__

    async def open_from_template(cls, path: Path, **kw) -> Database:
        if kw.get("migrations") is None and not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(template, path)
        return await real_open(cls, path, **kw)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Database, "open", classmethod(open_from_template))
        yield


@pytest.fixture(autouse=True)
def _disk_guard_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unrelated tests independent of the host's free space.

    The disk guard refuses claims below 2 GiB and is measured from the real
    filesystem at startup; a runner or developer machine short of space must not
    change what any other test observes. Tests that exercise the guard
    (`test_disk_guard.py`) turn it back on explicitly.
    """
    monkeypatch.setenv("ARGUS_DISK_GUARD_ENABLED", "false")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def db(tmp_path: Path, clock: FakeClock) -> AsyncGenerator[Database]:
    database = await Database.open(tmp_path / "argus.db", readers=2, clock=clock)
    yield database
    await database.close()
