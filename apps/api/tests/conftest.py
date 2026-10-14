from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from argus_api.db import Database


@pytest.fixture
async def db(tmp_path: Path) -> AsyncGenerator[Database]:
    database = await Database.open(tmp_path / "argus.db", readers=2)
    yield database
    await database.close()
