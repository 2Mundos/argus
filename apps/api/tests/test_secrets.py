import shutil
import subprocess
from pathlib import Path

import pytest
from argus_api.secrets import SecretStoreError, SopsSecretStore, open_store

needs_sops = pytest.mark.skipif(
    not (shutil.which("sops") and shutil.which("age-keygen")),
    reason="sops and age-keygen are required",
)


@pytest.fixture
def age_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    key = tmp_path / "key.txt"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    recipient = next(
        line.split(": ")[1]
        for line in key.read_text().splitlines()
        if line.startswith("# public key")
    )
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(key))
    monkeypatch.setenv("SOPS_AGE_RECIPIENTS", recipient)
    return recipient


@needs_sops
async def test_round_trip_without_plaintext_on_disk(
    tmp_path: Path, age_env: str
) -> None:
    path = tmp_path / "secrets.sops.yaml"
    store = SopsSecretStore(path)
    await store.reload()
    assert store.names() == set()
    await store.put("claude-1", {"api_key": "sk-super-secret"})
    await store.put("codex-1", {"api_key": "sk-other"})
    assert "sk-super-secret" not in path.read_text()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]  # no temp left

    reopened = SopsSecretStore(path)
    await reopened.reload()
    assert reopened.get("claude-1") == {"api_key": "sk-super-secret"}
    await reopened.delete("codex-1")
    await store.reload()
    assert store.names() == {"claude-1"}


@needs_sops
async def test_creating_the_file_needs_recipients(
    tmp_path: Path, age_env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SOPS_AGE_RECIPIENTS")
    store = SopsSecretStore(tmp_path / "secrets.sops.yaml")
    with pytest.raises(SecretStoreError) as err:
        await store.put("x", {"k": "v"})
    assert err.value.code == "secret_store_not_configured"


@needs_sops
async def test_wrong_key_is_reported_not_fatal(
    tmp_path: Path, age_env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secrets.sops.yaml"
    await SopsSecretStore(path).put("x", {"k": "v"})
    other = tmp_path / "other.txt"
    subprocess.run(["age-keygen", "-o", str(other)], check=True, capture_output=True)
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(other))
    store, error = await open_store(path)
    assert error is not None
    assert "decrypt failed" in error
    assert store.names() == set()


async def test_missing_sops_binary_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "secrets.sops.yaml"
    path.write_text("not: really")
    _, error = await open_store(path, sops="/nonexistent/sops")
    assert error is not None
    assert "not found" in error


def test_names_are_validated() -> None:
    import asyncio

    from argus_api.secrets import MemorySecretStore

    with pytest.raises(ValueError):
        asyncio.run(MemorySecretStore().put('x"]', {}))
