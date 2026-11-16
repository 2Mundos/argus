#!/usr/bin/env python3
"""Reject co-author / attribution trailers on new worker commits (worker-lifecycle 3.8).

Shared by the git wrapper, commit-msg hook, command guard, and tests. Does not
rewrite messages or history; it only reports why a commit or push is refused.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import NamedTuple

REAL_GIT_ENV = "ARGUS_GIT_REAL"
DEFAULT_REAL_GIT = "/usr/bin/git"
MODULE_PATH_ENV = "ARGUS_COMMIT_ATTRIBUTION_PY"
DEFAULT_MODULE = "/usr/local/lib/argus/commit_attribution.py"
TEST_HOOKS_ENV = "ARGUS_ATTRIBUTION_TEST_HOOKS"

_PROHIBITED_TRAILER = re.compile(
    r"^(?:"
    r"Co-Authored-By|"
    r"Contributed-By|Helped-by|"
    r"Reported-and-fixed-by|"
    r"Suggested-by|"
    r"Thanks-to|"
    r"Acked-and-tested-by"
    r")\s*:",
    re.MULTILINE | re.IGNORECASE,
)


class Trigger(NamedTuple):
    """One refusal trigger: the family that matched and the offending element."""

    family: str
    element: str


# family id -> reason text. The first six are the families the Bash guard can
# produce; `command_guard.REASONS` and its mirrors carry the same strings (a test
# compares them). The push families only occur in the git wrapper.
FAMILY_REASONS = {
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
    "push_wide": (
        "git push --all, --tags and --mirror cannot be validated safely; push "
        "explicit branch refspecs instead"
    ),
    "push_non_branch_refspec": (
        "tag and non-branch refspecs cannot be validated safely; push explicit "
        "branch refs only (for example `git push origin my-branch`)"
    ),
    "push_follow_tags": "--follow-tags cannot be validated safely",
    "git_dir_without_work_tree": (
        "when using --git-dir you must also pass --work-tree at the repository root"
    ),
    "push_outgoing_trailer": (
        "an outgoing commit contains a co-author attribution trailer; fix the "
        "commit message (for example `git commit --amend`) or reset before pushing"
    ),
}
ELEMENT_MAX = 120


def also_present(triggers: list[Trigger]) -> list[str]:
    """Distinct families of the triggers after the first, other than its own."""
    first = triggers[0].family
    others: list[str] = []
    for t in triggers[1:]:
        if t.family != first and t.family not in others:
            others.append(t.family)
    return others


def refusal(triggers: list[Trigger]) -> str:
    """Refusal text naming the first trigger's family and element, and the other
    families that were also present. The guard builds its own text from the same
    parts (`command_guard.message`)."""
    first = triggers[0]
    text = (
        f"Argus rejects this git command: {FAMILY_REASONS[first.family]} "
        f"({first.family}: {first.element})"
    )
    others = also_present(triggers)
    if others:
        text += f"; also present: {', '.join(others)}"
    return text


def _first(triggers: Iterable[Trigger]) -> str | None:
    for t in triggers:
        return refusal([t])
    return None


_IDENTITY_ENV = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
)
_IDENTITY_ENV_ASSIGNMENT = re.compile(r"(?<![\w])(" + "|".join(_IDENTITY_ENV) + r")=")
_IDENTITY_KEYS = frozenset(
    f"{section}.{field}"
    for section in ("user", "author", "committer")
    for field in ("name", "email")
)
_CONFIG_READ_FLAGS = frozenset(
    {
        "--get",
        "--get-all",
        "--get-regexp",
        "--get-urlmatch",
        "--list",
        "-l",
        "--show-origin",
    }
)


def _test_hooks_allowed() -> bool:
    return os.environ.get(TEST_HOOKS_ENV) == "1"


def real_git() -> str:
    if _test_hooks_allowed():
        return os.environ.get(REAL_GIT_ENV, DEFAULT_REAL_GIT)
    return DEFAULT_REAL_GIT


def module_path() -> str:
    if _test_hooks_allowed():
        return os.environ.get(MODULE_PATH_ENV, DEFAULT_MODULE)
    return DEFAULT_MODULE


def trailer_in_message(message: str) -> Trigger | None:
    """The first prohibited trailer line of `message`, stripped and cut."""
    found = _PROHIBITED_TRAILER.search(message) if message else None
    if not found:
        return None
    line = message[found.start() :].splitlines()[0].strip()[:ELEMENT_MAX]
    return Trigger("commit_coauthor_trailer", line)


def prohibited_trailer_in_message(message: str) -> bool:
    return trailer_in_message(message) is not None


def check_message(message: str) -> str | None:
    found = trailer_in_message(message)
    return refusal([found]) if found else None


def _read_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _git(
    cwd: str,
    *args: str,
    git_dir: str | None = None,
    work_tree: str | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [real_git()]
    if git_dir:
        cmd.extend(["--git-dir", git_dir])
    if work_tree:
        cmd.extend(["--work-tree", work_tree])
    cmd.extend(args)
    run_cwd = work_tree or cwd
    return subprocess.run(  # noqa: S603
        cmd,
        cwd=run_cwd,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def _git_fail(proc: subprocess.CompletedProcess[str], context: str) -> str:
    detail = (proc.stderr or proc.stdout or "unknown error").strip().splitlines()
    line = detail[0][:200] if detail else "unknown error"
    return f"Argus cannot validate this git {context}: {line}"


class GitInvocation(NamedTuple):
    cwd: str
    command: str
    args: list[str]
    git_dir: str | None = None
    work_tree: str | None = None


def _git_context_error(inv: GitInvocation) -> Trigger | None:
    if inv.git_dir and not inv.work_tree:
        return Trigger("git_dir_without_work_tree", "--git-dir")
    return None


def _forbidden_git_config(argv: list[str]) -> Iterator[Trigger]:
    """Per-invocation core.hooksPath overrides (host git may still use them)."""
    args = list(argv)
    if args and args[0] == "git":
        args = args[1:]
    i = 0
    while i < len(args):
        if args[i] in ("commit", "push"):
            break
        a = args[i]
        if a in ("-c", "--config") and i + 1 < len(args):
            cfg = args[i + 1]
            key = cfg.split("=", 1)[0]
            if key == "core.hooksPath":
                yield Trigger("hooks_path_override", key)
            i += 2
            continue
        i += 1


def _is_identity_key(cfg: str) -> bool:
    return cfg.split("=", 1)[0].strip().lower() in _IDENTITY_KEYS


def _config_key(cfg: str) -> str:
    return cfg.split("=", 1)[0].strip()


def _identity_override(argv: list[str]) -> Iterator[Trigger]:
    """Every attempt to override the configured git author/committer identity."""
    for name in _IDENTITY_ENV:
        if name in os.environ:
            yield Trigger("commit_identity_env", name)
            break
    args = list(argv)
    if args and Path(args[0]).name == "git":
        args = args[1:]
    i = 0
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a in ("-c", "--config") and i + 1 < len(args):
            if _is_identity_key(args[i + 1]):
                yield Trigger("commit_identity_config", _config_key(args[i + 1]))
            i += 2
        elif a.startswith("--config-env="):
            if _is_identity_key(a.split("=", 1)[1]):
                yield Trigger("commit_identity_config", _config_key(a.split("=", 1)[1]))
            i += 1
        elif a in ("--config-env", "-C", "--git-dir", "--work-tree", "--namespace"):
            if (
                a == "--config-env"
                and i + 1 < len(args)
                and _is_identity_key(args[i + 1])
            ):
                yield Trigger("commit_identity_config", _config_key(args[i + 1]))
            i += 2
        else:
            i += 1
    if i >= len(args):
        return
    sub, rest = args[i], args[i + 1 :]
    if sub == "commit" and any(
        a == "--author" or a.startswith("--author=") for a in rest
    ):
        yield Trigger("commit_author_flag", "--author")
    is_read = any(a in _CONFIG_READ_FLAGS for a in rest) or rest[:1] in (
        ["get"],
        ["list"],
    )
    if sub == "config" and not is_read:
        for a in rest:
            if a.strip().lower() in _IDENTITY_KEYS:
                yield Trigger("commit_identity_config", a.strip())
                break


def _validate_branch_refspec(
    cwd: str,
    refspec: str,
    *,
    git_dir: str | None = None,
    work_tree: str | None = None,
) -> Trigger | None:
    ctx = {"git_dir": git_dir, "work_tree": work_tree}
    spec = refspec.lstrip("+")
    src, _, dest = spec.partition(":")
    refused = Trigger("push_non_branch_refspec", refspec[:ELEMENT_MAX])
    if src.startswith("refs/tags/") or dest.startswith("refs/tags/"):
        return refused
    if dest.startswith("refs/") and not dest.startswith("refs/heads/"):
        return refused
    sym = _git(cwd, "rev-parse", "--symbolic-full-name", "-q", src, **ctx)
    if sym.returncode != 0 or not sym.stdout.strip().startswith("refs/heads/"):
        return refused
    return None


def _push_destination(
    cwd: str,
    remote: str,
    *,
    git_dir: str | None = None,
    work_tree: str | None = None,
) -> str:
    """Push URL of a configured remote; any other argument is itself the URL."""
    r = _git(
        cwd,
        "remote",
        "get-url",
        "--push",
        remote,
        git_dir=git_dir,
        work_tree=work_tree,
    )
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else remote


def _is_local_destination(destination: str) -> bool:
    """Absolute paths and file:/// URLs only; relative paths and unknown forms are network."""
    return destination.startswith(("/", "file:///"))


def parse_git_argv(argv: list[str], default_cwd: str) -> GitInvocation | None:
    """Parse `git` argv (with or without leading `git`) into cwd and subcommand."""
    args = list(argv)
    if args and args[0] == "git":
        args = args[1:]
    cwd = default_cwd
    git_dir: str | None = None
    work_tree: str | None = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-C", "--work-tree") and i + 1 < len(args):
            cwd = args[i + 1]
            if a == "--work-tree":
                work_tree = args[i + 1]
            i += 2
            continue
        if a.startswith("-C") and len(a) > 2:
            cwd = a[2:]
            i += 1
            continue
        if a == "--git-dir" and i + 1 < len(args):
            git_dir = args[i + 1]
            i += 2
            continue
        if a.startswith("--git-dir="):
            git_dir = a.split("=", 1)[1]
            i += 1
            continue
        if a.startswith("--work-tree="):
            work_tree = a.split("=", 1)[1]
            cwd = work_tree
            i += 1
            continue
        if a.startswith("-") and a not in ("-C", "--work-tree"):
            if a in ("-c", "--config") and i + 1 < len(args):
                i += 2
                continue
            if "=" in a:
                i += 1
                continue
            i += 1
            continue
        if a in ("commit", "push"):
            return GitInvocation(
                cwd=cwd,
                command=a,
                args=args[i + 1 :],
                git_dir=git_dir,
                work_tree=work_tree,
            )
        return None
    return None


def _commit_messages_from_argv(argv: list[str]) -> list[str]:
    if not argv or argv[0] != "commit":
        return []
    messages: list[str] = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in ("-m", "--message"):
            if i + 1 < len(argv):
                messages.append(argv[i + 1])
            i += 2
            continue
        if arg.startswith("-m") and len(arg) > 2:
            messages.append(arg[2:])
            i += 1
            continue
        if arg in ("-F", "--file"):
            if i + 1 < len(argv):
                messages.append(_read_file(argv[i + 1]))
            i += 2
            continue
        if arg.startswith("--message="):
            messages.append(arg.split("=", 1)[1])
            i += 1
            continue
        if arg.startswith("--file="):
            messages.append(_read_file(arg.split("=", 1)[1]))
            i += 1
            continue
        i += 1
    return messages


def _commit_argv_triggers(argv: list[str]) -> Iterator[Trigger]:
    """No-verify first, then one trailer per message (a file read may raise)."""
    for flag in ("--no-verify", "-n"):
        if flag in argv:
            yield Trigger("commit_no_verify", flag)
            break
    for msg in _commit_messages_from_argv(argv):
        found = trailer_in_message(msg)
        if found:
            yield found


def check_commit_argv(argv: list[str]) -> str | None:
    return _first(_commit_argv_triggers(argv))


def _bash_segments(command: str) -> list[list[str]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    segments: list[list[str]] = [[]]
    for token in lexer:
        if token and set(token) <= set(lexer.punctuation_chars):
            segments.append([])
        else:
            segments[-1].append(token)
    return [seg for seg in segments if seg]


def _bash_git_argvs(command: str) -> list[list[str]]:
    out: list[list[str]] = []
    for seg in _bash_segments(command):
        for idx, tok in enumerate(seg):
            if Path(tok).name == "git":
                out.append(seg[idx:])
                break
    return out


def _bash_git_commit_segments(command: str) -> list[list[str]]:
    out: list[list[str]] = []
    for seg in _bash_segments(command):
        while seg and re.match(r"^\w+=", seg[0]):
            seg = seg[1:]
        for idx, tok in enumerate(seg):
            if Path(tok).name == "git":
                inv = parse_git_argv(seg[idx:], ".")
                if inv and inv.command == "commit":
                    out.append(seg[idx:])
                break
        else:
            if seg and seg[0] == "commit":
                inv = parse_git_argv(["git", *seg], ".")
                if inv and inv.command == "commit":
                    out.append(["git", *seg])
    return out


def _is_message_flag(token: str) -> bool:
    return token in ("-m", "--message", "-F", "--file") or token.startswith(
        ("--message=", "--file=")
    )


def _is_short_flag_with_message(token: str) -> bool:
    return (
        token.startswith("-")
        and not token.startswith("--")
        and ("m" in token or "F" in token)
    )


def _identity_assignments(command: str) -> Iterator[Trigger]:
    for seg in _bash_segments(command):
        for idx, token in enumerate(seg):
            found = _IDENTITY_ENV_ASSIGNMENT.match(token)
            if not found:
                continue
            if idx > 0 and (
                _is_message_flag(seg[idx - 1])
                or _is_short_flag_with_message(seg[idx - 1])
            ):
                continue
            yield Trigger("commit_identity_env", found.group(1))
        if (
            seg[0] in ("export", "env")
            and len(seg) > 1
            and _IDENTITY_ENV_ASSIGNMENT.match(seg[1])
        ):
            yield Trigger("commit_identity_env", seg[1].split("=", 1)[0])


def bash_triggers(command: str) -> list[Trigger]:
    """Every trigger in `command`, in decision order: identity env assignment,
    identity overrides over git argvs, then per commit segment the forbidden git
    config and the commit argv (no-verify, trailers). The first one decides.
    An exception while scanning past the env assignments keeps what was found."""
    found = list(_identity_assignments(command))
    try:
        for argv in _bash_git_argvs(command):
            found.extend(_identity_override(argv))
        for seg in _bash_git_commit_segments(command):
            argv = seg if seg and seg[0] == "git" else ["git", *seg]
            found.extend(_forbidden_git_config(argv))
            inv = parse_git_argv(seg, ".")
            if not inv or inv.command != "commit":
                continue
            found.extend(_commit_argv_triggers(["commit", *inv.args]))
    except Exception:  # noqa: S110 - keep the triggers collected so far
        pass
    return found


def check_bash_command(command: str) -> str | None:
    found = bash_triggers(command)
    return refusal(found) if found else None


def _parse_push(
    cwd: str,
    remote_default: str,
    push_args: list[str],
    *,
    git_dir: str | None = None,
    work_tree: str | None = None,
) -> tuple[str, list[str], str | None]:
    remote = remote_default
    refspecs: list[str] = []
    positional: list[str] = []
    i = 0
    while i < len(push_args):
        a = push_args[i]
        if a in ("--all", "--tags", "--mirror"):
            return remote, [a], None
        if a == "--follow-tags":
            return remote, ["--follow-tags"], None
        if a.startswith("-"):
            if a in ("--repo", "-r") and i + 1 < len(push_args):
                remote = push_args[i + 1]
                i += 2
                continue
            i += 1
            continue
        positional.append(a)
        i += 1
    if len(positional) >= 2:
        remote, refspecs = positional[0], positional[1:]
    elif len(positional) == 1:
        remote = positional[0]
    if not refspecs:
        head = _git(
            cwd,
            "symbolic-ref",
            "-q",
            "HEAD",
            git_dir=git_dir,
            work_tree=work_tree,
        )
        if head.returncode != 0:
            return remote, [], _git_fail(head, "symbolic-ref")
        ref = head.stdout.strip()
        if ref.startswith("refs/heads/"):
            refspecs = [ref.removeprefix("refs/heads/")]
        else:
            refspecs = ["HEAD"]
    return remote, refspecs, None


def _resolve_local_sha(
    cwd: str,
    refspec: str,
    *,
    git_dir: str | None = None,
    work_tree: str | None = None,
) -> tuple[str | None, str | None]:
    spec = refspec.lstrip("+").split(":", 1)[0]
    ctx = {"git_dir": git_dir, "work_tree": work_tree}
    if spec == "HEAD":
        r = _git(cwd, "rev-parse", "HEAD", **ctx)
    elif spec.startswith("refs/"):
        r = _git(cwd, "rev-parse", spec, **ctx)
    else:
        r = _git(cwd, "rev-parse", spec, **ctx)
    if r.returncode != 0:
        return None, _git_fail(r, "rev-parse")
    return r.stdout.strip(), None


def _remote_branch_tip(
    cwd: str,
    remote: str,
    branch: str,
    *,
    git_dir: str | None = None,
    work_tree: str | None = None,
) -> tuple[str | None, str | None]:
    ctx = {"git_dir": git_dir, "work_tree": work_tree}
    r = _git(cwd, "rev-parse", "-q", f"{remote}/{branch}", **ctx)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip(), None
    probe = _git(cwd, "ls-remote", "--heads", remote, f"refs/heads/{branch}", **ctx)
    if probe.returncode != 0:
        return None, _git_fail(probe, "ls-remote")
    lines = [ln for ln in probe.stdout.splitlines() if ln.strip()]
    if not lines:
        return None, None
    return lines[0].split()[0], None


def _remote_default_tip(
    cwd: str,
    remote: str,
    *,
    git_dir: str | None = None,
    work_tree: str | None = None,
) -> tuple[str | None, str | None]:
    ctx = {"git_dir": git_dir, "work_tree": work_tree}
    for branch in ("main", "master"):
        probe = _git(cwd, "ls-remote", "--heads", remote, f"refs/heads/{branch}", **ctx)
        if probe.returncode != 0:
            return None, _git_fail(probe, "ls-remote")
        if probe.stdout.strip():
            return probe.stdout.split()[0], None
    sym = _git(cwd, "ls-remote", "--symref", remote, "HEAD", **ctx)
    if sym.returncode != 0:
        return None, _git_fail(sym, "ls-remote")
    for line in sym.stdout.splitlines():
        parts = line.split()
        if parts and re.fullmatch(r"[0-9a-f]{40}", parts[0]):
            return parts[0], None
    return None, None


def _merge_base(
    cwd: str,
    a: str,
    b: str,
    *,
    git_dir: str | None = None,
    work_tree: str | None = None,
) -> tuple[str | None, str | None]:
    r = _git(
        cwd,
        "merge-base",
        a,
        b,
        git_dir=git_dir,
        work_tree=work_tree,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None, _git_fail(r, "merge-base")
    return r.stdout.strip(), None


def outgoing_shas_for_refspec(
    cwd: str,
    remote: str,
    refspec: str,
    *,
    git_dir: str | None = None,
    work_tree: str | None = None,
) -> tuple[list[str], str | None]:
    ctx = {"git_dir": git_dir, "work_tree": work_tree}
    local_sha, err = _resolve_local_sha(cwd, refspec, **ctx)
    if err:
        return [], err
    if not local_sha:
        return [], "Argus cannot validate this push: could not resolve the local ref"
    dest = refspec.lstrip("+").split(":", 1)[1] if ":" in refspec.lstrip("+") else None
    if dest and dest.startswith("refs/heads/"):
        branch = dest.removeprefix("refs/heads/")
    elif dest:
        branch = dest
    else:
        head = _git(cwd, "symbolic-ref", "-q", "HEAD", **ctx)
        if head.returncode != 0:
            return [], _git_fail(head, "symbolic-ref")
        branch = head.stdout.strip().removeprefix("refs/heads/")

    remote_tip, err = _remote_branch_tip(cwd, remote, branch, **ctx)
    if err:
        return [], err

    if remote_tip:
        base = remote_tip
    else:
        default_tip, err = _remote_default_tip(cwd, remote, **ctx)
        if err:
            return [], err
        if default_tip:
            mb, err = _merge_base(cwd, default_tip, local_sha, **ctx)
            if err:
                return [], err
            base = mb
        else:
            # First push of this branch to an empty remote: only local branch commits apply.
            rev = _git(cwd, "rev-list", "--reverse", local_sha, **ctx)
            if rev.returncode != 0:
                return [], _git_fail(rev, "rev-list")
            return [s for s in rev.stdout.split() if s], None

    rev = _git(cwd, "rev-list", "--reverse", f"{base}..{local_sha}", **ctx)
    if rev.returncode != 0:
        return [], _git_fail(rev, "rev-list")
    return [s for s in rev.stdout.split() if s], None


def check_push(cwd: str, argv: list[str]) -> str | None:
    cfg_err = _first(
        _forbidden_git_config(argv if argv and argv[0] == "git" else ["git", *argv])
    )
    if cfg_err:
        return cfg_err
    inv = parse_git_argv(argv if argv and argv[0] == "git" else ["git", *argv], cwd)
    if not inv or inv.command != "push":
        return None
    ctx_err = _git_context_error(inv)
    if ctx_err:
        return refusal([ctx_err])
    remote, refspecs, err = _parse_push(
        inv.cwd,
        "origin",
        inv.args,
        git_dir=inv.git_dir,
        work_tree=inv.work_tree,
    )
    if err:
        return err if err.startswith("Argus") else f"Argus rejects this push: {err}"
    if any(r in ("--all", "--tags", "--mirror", "--follow-tags") for r in refspecs):
        if refspecs == ["--follow-tags"]:
            return refusal([Trigger("push_follow_tags", "--follow-tags")])
        return refusal([Trigger("push_wide", refspecs[0])])
    local = _is_local_destination(
        _push_destination(inv.cwd, remote, git_dir=inv.git_dir, work_tree=inv.work_tree)
    )
    scanned: list[str] = []
    for spec in refspecs:
        branch_err = _validate_branch_refspec(
            inv.cwd,
            spec,
            git_dir=inv.git_dir,
            work_tree=inv.work_tree,
        )
        if branch_err and not local:
            return refusal([branch_err])
        if not branch_err:
            scanned.append(spec)
    seen: set[str] = set()
    for spec in scanned:
        shas, err = outgoing_shas_for_refspec(
            inv.cwd,
            remote,
            spec,
            git_dir=inv.git_dir,
            work_tree=inv.work_tree,
        )
        if err:
            return err if err.startswith("Argus") else f"Argus rejects this push: {err}"
        for sha in shas:
            if sha in seen:
                continue
            seen.add(sha)
            log = _git(
                inv.cwd,
                "log",
                "-1",
                "--format=%B",
                sha,
                git_dir=inv.git_dir,
                work_tree=inv.work_tree,
            )
            if log.returncode != 0:
                return _git_fail(log, "log")
            trailer = trailer_in_message(log.stdout)
            if trailer:
                return refusal([Trigger("push_outgoing_trailer", trailer.element)])
    return None


def gate_git(default_cwd: str, argv: list[str]) -> str | None:
    ident_err = _first(_identity_override(argv))
    if ident_err:
        return ident_err
    inv = parse_git_argv(argv, default_cwd)
    if not inv:
        return None
    cfg_err = _first(_forbidden_git_config(argv))
    if cfg_err:
        return cfg_err
    ctx_err = _git_context_error(inv)
    if ctx_err:
        return refusal([ctx_err])
    if inv.command == "commit":
        return check_commit_argv(["commit", *inv.args])
    if inv.command == "push":
        return check_push(inv.cwd, ["git", "push", *inv.args])
    return None


def validate_commit_msg_file(path: str) -> int:
    err = check_message(_read_file(path))
    if err:
        sys.stderr.write(f"error: {err}\n")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        return 2
    cmd = args[0]
    if cmd == "validate-msg":
        return validate_commit_msg_file(args[1])
    if cmd == "check-commit":
        err = check_commit_argv(["commit", *args[1:]])
        if err:
            sys.stderr.write(f"error: {err}\n")
            return 1
        return 0
    if cmd == "check-push":
        if len(args) < 2:
            return 2
        cwd = args[1]
        err = check_push(cwd, args[2:])
        if err:
            sys.stderr.write(f"error: {err}\n")
            return 1
        return 0
    if cmd == "gate":
        if len(args) < 2:
            return 2
        cwd = args[1]
        err = gate_git(cwd, args[2:])
        if err:
            sys.stderr.write(f"error: {err}\n")
            return 1
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
