#!/usr/bin/env python3
"""Argus command guard: a Claude Code PreToolUse hook that blocks a short list of
catastrophic shell commands. Accident prevention, not a sandbox
(docs/specs/command-guard.spec.md). The rules in RULES are the single source of
truth; the hook and the tests both read them. Standard library only, so it runs
with the image's python3.

The hook reads the PreToolUse JSON on stdin and, for a blocked Bash command,
prints a deny decision on stdout. Any internal error allows the command.
"""

import importlib.util
import json
import os
import posixpath
import re
import shlex
import sys
from pathlib import Path
from typing import NamedTuple

_ATTRIBUTION = Path(__file__).with_name("commit_attribution.py")
_spec = importlib.util.spec_from_file_location("commit_attribution", _ATTRIBUTION)
_attribution = (
    importlib.util.module_from_spec(_spec) if _spec and _spec.loader else None
)
if _attribution and _spec and _spec.loader:
    sys.modules[_spec.name] = _attribution
    _spec.loader.exec_module(_attribution)
else:
    _attribution = None

DISABLE_ENV = "ARGUS_COMMAND_GUARD"  # "0" or "off" turns the guard off
MAX_DEPTH = 4
SHELLS = {"bash", "sh", "dash", "zsh"}
WRAPPERS = {"sudo", "doas", "env", "command", "nohup", "time", "nice", "exec"}
WRAPPER_ARG_FLAGS = {"-u", "-g", "-C", "-h", "-p", "-n"}  # option that takes a value
DANGEROUS_DIRS = {"/", "~", "$HOME", "${HOME}", "/root", "/home"}
BLOCK_DEVICE = r"/dev/(?:sd|hd|vd|xvd|nvme|mmcblk|loop|md|dm-)"

# id -> reason. The first matching rule wins.
REASONS = {
    "commit_identity_env": (
        "do not set the git author or committer identity through GIT_AUTHOR_* or "
        "GIT_COMMITTER_* variables; worker commits use the configured project identity"
    ),
    "commit_identity_config": (
        "do not override the git author or committer identity with an identity "
        "config key (-c, --config-env or git config); worker commits use the "
        "configured project identity"
    ),
    "commit_author_flag": (
        "do not pass --author to git commit; worker commits use the configured "
        "project identity"
    ),
    "hooks_path_override": (
        "do not override core.hooksPath on the command line; worker commits use "
        "the workspace hook directory"
    ),
    "commit_no_verify": (
        "do not skip the commit-msg hook with --no-verify or -n; the attribution "
        "rules must apply"
    ),
    "commit_coauthor_trailer": (
        "a commit message must not contain a Co-Authored-By or other co-author "
        "attribution trailer (remove the trailer; git author and committer is enough)"
    ),
    "rm_root": "recursive deletion of /, ~, $HOME, /home/* or /root",
    "no_preserve_root": "--no-preserve-root disables the safety check of rm",
    "mkfs": "creating a filesystem destroys the data on the device",
    "dd_device": "writing to a block device overwrites a disk",
    "device_redirect": "redirecting output to a block device overwrites a disk",
    "fork_bomb": "a fork bomb exhausts the machine",
    "kubeconfig_secret": (
        "the task's kubeconfig holds a credential: use it only through "
        "kubectl --kubeconfig, never print, copy or decode it"
    ),
}
_FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:")
_DEVICE_REDIRECT = re.compile(rf">\s*{BLOCK_DEVICE}")
_DD_OF = re.compile(rf"^of={BLOCK_DEVICE}")
_MKFS = re.compile(r"^(?:mkfs(?:\.\w+)?|mke2fs)$")
# The attempt's kubeconfig lives under this directory (integrations spec 3.6).
TASK_INTEGRATIONS = "task-integrations"
KUBECTL_RAW = {"--raw", "--raw=true", "--flatten", "--flatten=true"}


class Block(NamedTuple):
    """A blocked command: the rule id, and for the commit families the offending
    element and the other families that were also present."""

    rule: str
    element: str = ""
    also: tuple[str, ...] = ()


def _segments(command: str) -> list[list[str]]:
    lexer = shlex.shlex(
        command.replace("\n", " ; "), posix=True, punctuation_chars=True
    )
    lexer.whitespace_split = True
    segments: list[list[str]] = [[]]
    for token in lexer:
        if token and set(token) <= set(lexer.punctuation_chars):
            segments.append([])
        else:
            segments[-1].append(token)
    return [s for s in segments if s]


def _unwrap(tokens: list[str]) -> list[str]:
    """Drop sudo/env/VAR=x style prefixes and their options."""
    while tokens:
        head = posixpath.basename(tokens[0])
        if head not in WRAPPERS and not re.match(r"^\w+=", tokens[0]):
            break
        tokens = tokens[1:]
        while tokens and (tokens[0].startswith("-") or re.match(r"^\w+=", tokens[0])):
            flag = tokens[0]
            tokens = tokens[1:]
            if flag in WRAPPER_ARG_FLAGS and tokens:
                tokens = tokens[1:]
    return tokens


def _dangerous_target(target: str) -> bool:
    if target.endswith("/*"):
        target = target[:-2] or "/"
    elif target.endswith("/"):
        target = target.rstrip("/") or "/"
    return target in DANGEROUS_DIRS or re.fullmatch(r"/home/[^/]+", target) is not None


def _check_segment(tokens: list[str], depth: int) -> Block | None:
    tokens = _unwrap(tokens)
    if not tokens:
        return None
    name, args = posixpath.basename(tokens[0]), tokens[1:]
    if name == "kubectl":
        view = any(args[i : i + 2] == ["config", "view"] for i in range(len(args)))
        if view and KUBECTL_RAW & set(args):
            return Block("kubeconfig_secret")
        return None
    if name != "export" and any(TASK_INTEGRATIONS in t for t in tokens):
        return Block("kubeconfig_secret")
    if name == "eval":
        return _check(" ".join(args), depth + 1)
    if name in SHELLS:
        for i, arg in enumerate(args[:-1]):
            if re.fullmatch(r"-[a-z]*c[a-z]*", arg):
                return _check(args[i + 1], depth + 1)
        return None
    if name == "rm":
        if "--no-preserve-root" in args:
            return Block("no_preserve_root")
        recursive = any(
            a in ("--recursive",)
            or (re.fullmatch(r"-[A-Za-z]+", a) and set(a) & {"r", "R"})
            for a in args
        )
        targets = [a for a in args if not a.startswith("-")]
        if recursive and any(_dangerous_target(t) for t in targets):
            return Block("rm_root")
    elif _MKFS.match(name):
        return Block("mkfs")
    elif name == "dd" and any(_DD_OF.match(a) for a in args):
        return Block("dd_device")
    return None


def _check(command: str, depth: int = 0) -> Block | None:
    if depth > MAX_DEPTH:
        return None
    triggers = _attribution.bash_triggers(command) if _attribution else []
    if triggers:
        first = triggers[0]
        return Block(
            first.family, first.element, tuple(_attribution.also_present(triggers))
        )
    if _FORK_BOMB.search(command):
        return Block("fork_bomb")
    if _DEVICE_REDIRECT.search(command):
        return Block("device_redirect")
    for tokens in _segments(command):
        rule = _check_segment(tokens, depth)
        if rule:
            return rule
    return None


def inspect(command: str) -> Block | None:
    """Return the block for `command`, or None to allow it.

    A parse error allows the command (fail open).
    """
    try:
        return _check(command)
    except Exception:
        return None


def check(command: str) -> str | None:
    """Return the id of the rule that blocks `command`, or None to allow it."""
    block = inspect(command)
    return block.rule if block else None


def message(rule: str, element: str = "", also: tuple[str, ...] = ()) -> str:
    reason = REASONS[rule]
    if element:
        reason += f" ({element})"
    if also:
        reason += f"; also present: {', '.join(also)}"
    return (
        f"Blocked by Argus command guard: {reason}. "
        "Do not retry or work around this block."
    )


def main() -> int:
    try:
        if os.environ.get(DISABLE_ENV, "").lower() in ("0", "off"):
            return 0
        event = json.load(sys.stdin)
        if event.get("tool_name") != "Bash":
            return 0
        block = inspect(str(event["tool_input"]["command"]))
        if block:
            decision = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": message(*block),
                }
            }
            sys.stdout.write(json.dumps(decision))
    except Exception:  # noqa: S110 - fail open on any guard error
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
