from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from api_support import FakeClock
from argus_api.db import Database


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def db(tmp_path: Path, clock: FakeClock) -> AsyncGenerator[Database]:
    database = await Database.open(tmp_path / "argus.db", readers=2, clock=clock)
    yield database
    await database.close()
