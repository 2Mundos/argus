from pathlib import Path

import pytest
from argus_api.db import AlreadyRunningError, DataDirLock


def test_second_lock_on_same_dir_fails(tmp_path: Path) -> None:
    with DataDirLock(tmp_path), pytest.raises(AlreadyRunningError):
        DataDirLock(tmp_path).acquire()
    # Released on exit: can be taken again.
    with DataDirLock(tmp_path):
        pass
