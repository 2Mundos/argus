"""probe-managed-settings.sh: the image-level check of the Claude managed settings."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ENGINES = Path(__file__).resolve().parents[1] / "engines"
PROBE = ENGINES / "probe-managed-settings.sh"
SHIPPED = json.loads((ENGINES / "claude-managed-settings.json").read_text())
DOCKERFILE = (ENGINES / "Dockerfile.tools-layer").read_text()


def _probe(path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(PROBE), path], capture_output=True, text=True, check=False
    )


def _write(tmp_path: Path, settings: object) -> str:
    p = tmp_path / "managed-settings.json"
    p.write_text(settings if isinstance(settings, str) else json.dumps(settings))
    return str(p)


def _variant(mutate) -> dict:
    settings = json.loads(json.dumps(SHIPPED))
    mutate(settings["hooks"])
    return settings


def test_shipped_settings_pass() -> None:
    assert _probe(str(ENGINES / "claude-managed-settings.json")).returncode == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda h: h.pop("PreToolUse"),
        lambda h: h["PreToolUse"].append(h["PreToolUse"][0]),
        lambda h: h["PreToolUse"][0].update(matcher="Read"),
        lambda h: (h.pop("PostToolUse"), h.pop("UserPromptSubmit")),
    ],
    ids=["no-guard", "duplicate-guard", "other-matcher", "no-repeat-guard"],
)
def test_bad_registration_fails(tmp_path: Path, mutate) -> None:
    assert _probe(_write(tmp_path, _variant(mutate))).returncode != 0


def test_bad_path_or_json_fails(tmp_path: Path) -> None:
    assert _probe(_write(tmp_path, "{")).returncode != 0
    assert _probe(str(tmp_path / "absent.json")).returncode != 0
    assert _probe("managed-settings.json").returncode != 0


def test_dockerfile_sets_env_and_runs_probe() -> None:
    path = "/etc/claude-code/managed-settings.json"
    assert f"ENV ARGUS_CLAUDE_MANAGED_SETTINGS={path}" in DOCKERFILE
    assert 'test "$ARGUS_CLAUDE_MANAGED_SETTINGS" = ' + path in DOCKERFILE
    assert 'probe-managed-settings.sh "$ARGUS_CLAUDE_MANAGED_SETTINGS"' in DOCKERFILE
