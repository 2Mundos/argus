"""Command guard rules and hook (docs/specs/command-guard.spec.md).

Every rule in `REASONS` has a paired block/allow table; the hook is exercised
through its real stdin/stdout contract.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ENGINES = Path(__file__).resolve().parents[1] / "engines"
GUARD = ENGINES / "command_guard.py"
spec = importlib.util.spec_from_file_location("command_guard", GUARD)
assert spec and spec.loader
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

CASES = {
    "rm_root": {
        "block": [
            "rm -rf /",
            "rm -rf /*",
            "rm -rf ~",
            "rm -rf ~/",
            'rm -rf "$HOME"',
            "rm -fr ${HOME}/",
            "rm -rf /home/*",
            "rm -R /home/alice",
            "rm --recursive --force /root",
            "sudo rm -rf /",
            "sudo -u root rm -rf ~",
            "bash -c 'rm -rf /'",
            'sh -lc "echo hi; rm -rf $HOME"',
            "cd /tmp && rm -rf /",
            "make clean || rm -rf ~",
            "echo ok\nrm -rf /",
            "FOO=1 /bin/rm -rf /",
        ],
        "allow": [
            "rm -rf ./build",
            "rm -rf /tmp/x",
            "rm -rf ~/work/project/node_modules",
            "rm -rf /home/alice/project",
            "rm -f /",  # not recursive
            "rm -rf build/ dist/",
            "echo rm -rf /",
        ],
    },
    "no_preserve_root": {
        "block": [
            "rm -rf --no-preserve-root /tmp/x",
            "sudo rm --no-preserve-root -r a",
        ],
        "allow": ["rm -rf /tmp/x"],
    },
    "mkfs": {
        "block": [
            "mkfs.ext4 /dev/sda1",
            "sudo mkfs -t ext4 /dev/sdb",
            "mke2fs /dev/sdc",
        ],
        "allow": ["echo mkfs", "ls mkfs.d"],
    },
    "dd_device": {
        "block": ["dd if=/dev/zero of=/dev/sda bs=1M", "sudo dd if=x of=/dev/nvme0n1"],
        "allow": [
            "dd if=/dev/zero of=./disk.img bs=1M",
            "dd if=/dev/sda of=./backup.img",
        ],
    },
    "device_redirect": {
        "block": ["cat x > /dev/sda", "echo hi >/dev/nvme0n1p1"],
        "allow": ["echo hi > /dev/null", "cat x > ./sda"],
    },
    "fork_bomb": {
        "block": [":(){ :|:& };:", "bash -c ':(){ :|: & }; :'"],
        "allow": ["echo done; ls"],
    },
}


def test_every_rule_has_both_cases():
    assert set(CASES) == set(guard.REASONS)
    assert all(c["block"] and c["allow"] for c in CASES.values())


@pytest.mark.parametrize(
    ("rule", "command"),
    [(r, c) for r, t in CASES.items() for c in t["block"]],
)
def test_blocked(rule, command):
    assert guard.check(command) == rule


@pytest.mark.parametrize("command", [c for t in CASES.values() for c in t["allow"]])
def test_allowed(command):
    assert guard.check(command) is None


def test_unparseable_command_fails_open():
    assert guard.check("echo 'unterminated") is None


def run_hook(payload: object, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(GUARD)],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ["PATH"], **env},
    )


def bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def test_hook_denies_with_the_block_message():
    done = run_hook(bash("rm -rf ~"))
    assert done.returncode == 0
    out = json.loads(done.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == (
        "Blocked by Argus command guard: " + guard.REASONS["rm_root"] + ". "
        "Do not retry or work around this block."
    )


@pytest.mark.parametrize(
    "payload",
    [
        bash("rm -rf ./build"),
        {"tool_name": "Read", "tool_input": {"file_path": "/"}},
        "not json",
        {"tool_name": "Bash"},
    ],
)
def test_hook_allows_and_fails_open(payload):
    done = run_hook(payload)
    assert (done.returncode, done.stdout) == (0, "")


def test_hook_can_be_disabled_by_env():
    assert run_hook(bash("rm -rf /"), ARGUS_COMMAND_GUARD="off").stdout == ""


def test_managed_settings_register_the_hook_on_bash():
    settings = json.loads((ENGINES / "claude-managed-settings.json").read_text())
    (entry,) = settings["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Bash"
    assert entry["hooks"][0]["command"].endswith(
        "/usr/local/lib/argus/command_guard.py"
    )
