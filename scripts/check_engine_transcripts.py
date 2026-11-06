#!/usr/bin/env python3
"""Fail an engine version bump that does not re-record its transcripts.

Rule (integrations/nanoclaw/fixtures/transcripts/README.md): a change to an
engine's pinned version re-records at least one `success-*` and one `failure-*`
case under `transcripts/<engine>/` in the same change. Pins are read from the
files that hold them, at the merge base with origin/main and in the working
tree (staged, unstaged and untracked files count). Standard library only.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRANSCRIPTS = "integrations/nanoclaw/fixtures/transcripts"
CASE_TYPES = ("success", "failure")
ENGINE_DIR = "integrations/nanoclaw/engines"

# engine -> (file holding the pin, regex whose group 1 is the version)
PINS: dict[str, tuple[str, str]] = {
    "claude": (
        "apps/api/src/argus_api/accounts/clients.py",
        r"claude-code-releases/(\d[^/\s\"']*)/",
    ),
    "codex": (f"{ENGINE_DIR}/install-engines.sh", r'^CODEX_VERSION="([^"]+)"'),
    "copilot": (
        f"{ENGINE_DIR}/install-copilot-cli.sh",
        r'^VERSION="\$\{COPILOT_CLI_VERSION:-([^}]+)\}"',
    ),
    "cursor": (
        f"{ENGINE_DIR}/install-cursor-agent.sh",
        r'^VERSION="\$\{CURSOR_AGENT_VERSION:-([^}]+)\}"',
    ),
}


class CheckError(Exception):
    pass


def git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ("git", *args), cwd=root, capture_output=True, text=True, check=False
    )
    if done.returncode:
        raise CheckError(done.stderr.strip() or f"git {' '.join(args)} failed")
    return done.stdout


def parse_pin(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, re.M)
    return match.group(1) if match else None


def changed_paths(root: Path, base: str) -> tuple[list[str], str]:
    try:
        merge_base = git(root, "merge-base", base, "HEAD").strip()
    except CheckError as exc:
        raise CheckError(f"cannot resolve base {base!r}: {exc}") from exc
    tracked = git(
        root, "diff", "--name-only", "--no-renames", "--diff-filter=d", merge_base
    ).splitlines()
    new = git(root, "ls-files", "--others", "--exclude-standard").splitlines()
    return [*tracked, *new], merge_base


def pin_at(root: Path, ref: str, path: str, pattern: str) -> str | None:
    try:
        text = git(root, "show", f"{ref}:{path}")
    except CheckError:
        return None
    return parse_pin(text, pattern)


def check(root: Path, base: str) -> list[str]:
    changed, merge_base = changed_paths(root, base)
    errors: list[str] = []
    for engine, (path, pattern) in PINS.items():
        try:
            text = (root / path).read_text()
        except OSError as exc:
            errors.append(
                f"engine transcripts: {engine} pin file {path} unreadable: {exc}"
            )
            continue
        new = parse_pin(text, pattern)
        if new is None:
            errors.append(
                f"engine transcripts: {engine} pin not found in {path} "
                f"(pattern {pattern})"
            )
            continue
        old = pin_at(root, merge_base, path, pattern)
        if old == new:
            continue
        prefix = f"{TRANSCRIPTS}/{engine}/"
        errors.extend(
            f"engine transcripts: {engine} pin {old or '(none)'} -> {new} "
            f"but no {kind} case re-recorded under {prefix} (expected {kind}-*)"
            for kind in CASE_TYPES
            if not any(
                p.startswith(f"{prefix}{kind}-") and p.count("/") > prefix.count("/")
                for p in changed
            )
        )
    return errors


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/main", help="ref to diff against")
    args = parser.parse_args(argv)
    try:
        errors = check(root, args.base)
    except CheckError as exc:
        print(f"engine transcripts: {exc}")
        return 1
    for line in errors:
        print(line)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
