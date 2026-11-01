#!/usr/bin/env python3
"""Argus command guard: a Claude Code PreToolUse hook that blocks a short list of
catastrophic shell commands. Accident prevention, not a sandbox
(docs/specs/command-guard.spec.md). The rules in RULES are the single source of
truth; the hook and the tests both read them. Standard library only, so it runs
with the image's python3.

The hook reads the PreToolUse JSON on stdin and, for a blocked Bash command,
prints a deny decision on stdout. Any internal error allows the command.
"""

import json
import os
import posixpath
import re
import shlex
import sys

DISABLE_ENV = "ARGUS_COMMAND_GUARD"  # "0" or "off" turns the guard off
MAX_DEPTH = 4
SHELLS = {"bash", "sh", "dash", "zsh"}
WRAPPERS = {"sudo", "doas", "env", "command", "nohup", "time", "nice", "exec"}
WRAPPER_ARG_FLAGS = {"-u", "-g", "-C", "-h", "-p", "-n"}  # option that takes a value
DANGEROUS_DIRS = {"/", "~", "$HOME", "${HOME}", "/root", "/home"}
BLOCK_DEVICE = r"/dev/(?:sd|hd|vd|xvd|nvme|mmcblk|loop|md|dm-)"

# id -> reason. The first matching rule wins.
REASONS = {
    "rm_root": "recursive deletion of /, ~, $HOME, /home/* or /root",
    "no_preserve_root": "--no-preserve-root disables the safety check of rm",
    "mkfs": "creating a filesystem destroys the data on the device",
    "dd_device": "writing to a block device overwrites a disk",
    "device_redirect": "redirecting output to a block device overwrites a disk",
    "fork_bomb": "a fork bomb exhausts the machine",
}
_FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:")
_DEVICE_REDIRECT = re.compile(rf">\s*{BLOCK_DEVICE}")
_DD_OF = re.compile(rf"^of={BLOCK_DEVICE}")
_MKFS = re.compile(r"^(?:mkfs(?:\.\w+)?|mke2fs)$")


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


def _check_segment(tokens: list[str], depth: int) -> str | None:
    tokens = _unwrap(tokens)
    if not tokens:
        return None
    name, args = posixpath.basename(tokens[0]), tokens[1:]
    if name == "eval":
        return _check(" ".join(args), depth + 1)
    if name in SHELLS:
        for i, arg in enumerate(args[:-1]):
            if re.fullmatch(r"-[a-z]*c[a-z]*", arg):
                return _check(args[i + 1], depth + 1)
        return None
    if name == "rm":
        if "--no-preserve-root" in args:
            return "no_preserve_root"
        recursive = any(
            a in ("--recursive",)
            or (re.fullmatch(r"-[A-Za-z]+", a) and set(a) & {"r", "R"})
            for a in args
        )
        targets = [a for a in args if not a.startswith("-")]
        if recursive and any(_dangerous_target(t) for t in targets):
            return "rm_root"
    elif _MKFS.match(name):
        return "mkfs"
    elif name == "dd" and any(_DD_OF.match(a) for a in args):
        return "dd_device"
    return None


def _check(command: str, depth: int = 0) -> str | None:
    if depth > MAX_DEPTH:
        return None
    if _FORK_BOMB.search(command):
        return "fork_bomb"
    if _DEVICE_REDIRECT.search(command):
        return "device_redirect"
    for tokens in _segments(command):
        rule = _check_segment(tokens, depth)
        if rule:
            return rule
    return None


def check(command: str) -> str | None:
    """Return the id of the rule that blocks `command`, or None to allow it.

    A parse error allows the command (fail open).
    """
    try:
        return _check(command)
    except Exception:
        return None


def message(rule: str) -> str:
    return (
        f"Blocked by Argus command guard: {REASONS[rule]}. "
        "Do not retry or work around this block."
    )


def main() -> int:
    try:
        if os.environ.get(DISABLE_ENV, "").lower() in ("0", "off"):
            return 0
        event = json.load(sys.stdin)
        if event.get("tool_name") != "Bash":
            return 0
        rule = check(str(event["tool_input"]["command"]))
        if rule:
            decision = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": message(rule),
                }
            }
            sys.stdout.write(json.dumps(decision))
    except Exception:  # noqa: S110 - fail open on any guard error
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
