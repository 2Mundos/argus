"""Command guard rules and hook (docs/specs/command-guard.spec.md).

Every rule in `REASONS` has a paired block/allow table; the hook is exercised
through its real stdin/stdout contract.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
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
    "kubeconfig_secret": {
        "block": [
            "cat /workspace/agent/task-integrations/T/1-u/kubeconfig",
            "base64 < /workspace/agent/task-integrations/T/1-u/kubeconfig",
            "cp /workspace/agent/task-integrations/T/1-u/kubeconfig ./kc",
            "grep token /workspace/agent/task-integrations/*/*/kubeconfig",
            "python3 -c 'print(open(\"/workspace/agent/task-integrations/T/1-u/kubeconfig\").read())'",
            "kubectl config view --raw",
            "kubectl --kubeconfig /workspace/agent/task-integrations/T/1-u/kubeconfig config view --flatten",
            "bash -c 'kubectl config view --raw=true'",
        ],
        "allow": [
            "kubectl --kubeconfig /workspace/agent/task-integrations/T/1-u/kubeconfig get pods",
            "KUBECONFIG=/workspace/agent/task-integrations/T/1-u/kubeconfig kubectl get pods -n team-a",
            "export KUBECONFIG=/workspace/agent/task-integrations/T/1-u/kubeconfig",
            "kubectl config view",
            "cat ./kubeconfig.example",
        ],
    },
    "commit_attribution": {
        "block": [
            'git commit -m "x\n\nCo-Authored-By: bot@example.com"',
            "git commit --message='Co-authored-by: a'",
            "git commit --no-verify -m ok",
        ],
        "allow": [
            "git commit -m 'fix'",
            "git status",
            'echo "Co-Authored-By: not a git commit"',
        ],
    },
}


def test_every_rule_has_both_cases():
    assert set(CASES) == set(guard.REASONS)
    for rule in guard.REASONS:
        assert any(guard.check(c) == rule for c in CASES[rule]["block"]), rule
        assert any(guard.check(c) is None for c in CASES[rule]["allow"]), rule


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
    assert run_hook(bash("rm -rf /"), ARGUS_COMMAND_GUARD="0").stdout == ""


@pytest.mark.parametrize("env", [{}, {"ARGUS_COMMAND_GUARD": "on"}])
def test_hook_blocks_when_env_unset_or_on(env):
    assert '"deny"' in run_hook(bash("rm -rf /"), **env).stdout


def test_managed_settings_register_the_hook_on_bash():
    settings = json.loads((ENGINES / "claude-managed-settings.json").read_text())
    (entry,) = settings["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Bash"
    assert entry["hooks"][0]["command"].endswith(
        "/usr/local/lib/argus/command_guard.py"
    )


def test_the_api_reason_table_equals_the_hook_table() -> None:
    from argus_core.events import COMMAND_GUARD_REASONS

    assert COMMAND_GUARD_REASONS == guard.REASONS


def test_the_channel_rule_table_equals_the_hook_table():
    """`COMMAND_GUARD_REASONS` in argus-command-blocked.ts is what maps a block message
    back to its rule; it must not drift from the hook."""
    source = (ENGINES.parent / "argus-command-blocked.ts").read_text()
    body = source.split("COMMAND_GUARD_REASONS", 1)[1].split("};", 1)[0]
    table = {
        key: json.loads(text)
        for key, text in re.findall(r'(\w+):\s*("(?:[^"\\]|\\.)*")', body)
    }
    assert table == guard.REASONS
