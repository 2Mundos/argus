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
attribution = sys.modules["commit_attribution"]

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
    "commit_identity_env": {
        "block": [
            "GIT_AUTHOR_NAME=x git commit -m ok",
            "export GIT_COMMITTER_EMAIL=a@b.c",
            "env GIT_AUTHOR_EMAIL=a@b.c git commit -m ok",
        ],
        "allow": ['git commit -m "GIT_AUTHOR_NAME=x"', "echo GIT_AUTHOR_NAME"],
    },
    "commit_identity_config": {
        "block": [
            "git -c user.email=x@y.z commit -m ok",
            "git -c Committer.Name=x status",
            "git --config-env=author.email=VAR commit -m ok",
            "git config user.name X",
        ],
        "allow": ["git config --get user.name", "git -c color.ui=never status"],
    },
    "commit_author_flag": {
        "block": [
            "git commit --author='A <a@b.c>' -m ok",
            "git commit --author Bot -m ok",
        ],
        "allow": ["git log --author=alice", "git commit -m 'by author'"],
    },
    "hooks_path_override": {
        "block": [
            "git -c core.hooksPath=/dev/null commit -m ok",
            "git -c core.hooksPath=/x -c core.editor=true commit -m ok",
        ],
        "allow": ["git -c core.editor=true commit -m ok", "git status"],
    },
    "commit_no_verify": {
        "block": ["git commit --no-verify -m ok", "git commit -n -m ok"],
        "allow": ["git log --grep commit --no-verify", "git push -n origin x"],
    },
    "commit_coauthor_trailer": {
        "block": [
            'git commit -m "x\n\nCo-Authored-By: bot@example.com"',
            "git commit --message='Co-authored-by: a'",
            "git commit -m ok -m 'Helped-by: someone'",
        ],
        "allow": [
            "git add a.txt b/c.txt",
            'git commit -m "fix: one line"',
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


# One distinct message per family: it names the family reason and the element.
FAMILY_BLOCKS = {
    "commit_identity_env": ("GIT_AUTHOR_NAME=x git commit -m ok", "GIT_AUTHOR_NAME"),
    "commit_identity_config": ("git -c user.email=x@y.z commit -m ok", "user.email"),
    "commit_author_flag": ("git commit --author=Bot -m ok", "--author"),
    "hooks_path_override": (
        "git -c core.hooksPath=/x commit -m ok",
        "core.hooksPath",
    ),
    "commit_no_verify": ("git commit -n -m ok", "-n"),
    "commit_coauthor_trailer": (
        'git commit -m "x\n\nCo-Authored-By: Bot <b@x.y>  "',
        "Co-Authored-By: Bot <b@x.y>",
    ),
}


def test_every_commit_family_is_covered():
    commit_families = {r for r in guard.REASONS if r.startswith(("commit_", "hooks_"))}
    assert set(FAMILY_BLOCKS) == commit_families
    assert "commit_attribution" not in guard.REASONS


@pytest.mark.parametrize(("rule", "case"), FAMILY_BLOCKS.items())
def test_a_refusal_names_the_family_and_the_element(rule, case):
    command, element = case
    block = guard.inspect(command)
    assert block == (rule, element, ())
    expected = (
        f"Blocked by Argus command guard: {guard.REASONS[rule]} ({element}). "
        "Do not retry or work around this block."
    )
    assert guard.message(*block) == expected
    reason = json.loads(run_hook(bash(command)).stdout)["hookSpecificOutput"][
        "permissionDecisionReason"
    ]
    assert reason == expected
    bash_text = attribution.check_bash_command(command)
    assert bash_text
    assert guard.REASONS[rule] in bash_text
    assert f"({rule}: {element})" in bash_text


def test_the_family_messages_are_distinct():
    messages = {guard.message(rule, el) for rule, (_, el) in FAMILY_BLOCKS.items()}
    assert len(messages) == len(FAMILY_BLOCKS)
    assert len(set(guard.REASONS.values())) == len(guard.REASONS)


def test_a_long_trailer_element_is_cut_to_120_characters():
    block = guard.inspect(f'git commit -m "Co-Authored-By: {"x" * 300}"')
    assert block and block.rule == "commit_coauthor_trailer"
    assert block.element == ("Co-Authored-By: " + "x" * 300)[:120]


def test_two_triggers_report_the_first_and_list_the_other():
    # The identity check over git argvs runs before the forbidden-config check over
    # commit segments, so --author is first and core.hooksPath is also present.
    command = "git -c core.hooksPath=/x commit --author=Bot -m ok"
    block = guard.inspect(command)
    assert block == ("commit_author_flag", "--author", ("hooks_path_override",))
    expected = (
        f"Blocked by Argus command guard: {guard.REASONS['commit_author_flag']} "
        "(--author); also present: hooks_path_override. "
        "Do not retry or work around this block."
    )
    assert guard.message(*block) == expected
    out = run_hook(bash(command)).stdout
    assert json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] == expected


def test_trigger_order_is_env_then_identity_then_config_then_commit_argv():
    command = (
        "GIT_AUTHOR_NAME=x git -c core.hooksPath=/x commit --no-verify "
        "-m 'Co-Authored-By: a'"
    )
    assert guard.inspect(command) == (
        "commit_identity_env",
        "GIT_AUTHOR_NAME",
        ("hooks_path_override", "commit_no_verify", "commit_coauthor_trailer"),
    )
    config_first = "git -c core.hooksPath=/x commit --no-verify -m 'Co-Authored-By: a'"
    assert guard.inspect(config_first) == (
        "hooks_path_override",
        "core.hooksPath",
        ("commit_no_verify", "commit_coauthor_trailer"),
    )
    assert guard.inspect("git commit -n -m 'Co-Authored-By: a'") == (
        "commit_no_verify",
        "-n",
        ("commit_coauthor_trailer",),
    )


def test_the_same_family_twice_is_not_listed_as_also_present():
    block = guard.inspect("git -c user.name=a -c user.email=b commit -m ok")
    assert block == ("commit_identity_config", "user.name", ())


def test_an_unreadable_message_file_keeps_the_triggers_found_so_far():
    assert guard.check("git commit -F /nonexistent/msg") is None
    assert guard.inspect("git commit -n -F /nonexistent/msg") == (
        "commit_no_verify",
        "-n",
        (),
    )


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


def test_the_family_reasons_equal_the_attribution_module_reasons():
    for rule, reason in guard.REASONS.items():
        if rule in attribution.FAMILY_REASONS:
            assert attribution.FAMILY_REASONS[rule] == reason, rule


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
