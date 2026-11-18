#!/usr/bin/env python3
"""Reject stray or misplaced files in a change (docs/specs/path-gate.spec.md).

Judges only the paths ADDED or RENAMED-TO between the merge base with the base ref
(default origin/main) and HEAD, never files that already exist; `--all` judges every
file at HEAD. The rules are data in scripts/path_policy.json. One line per violation:
`path: rule-id: message`; `--json` prints a list of {path, rule, action}. Exit 1 on
any violation. Standard library only.
"""

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
POLICY = ROOT / "scripts" / "path_policy.json"


def git(root: Path, *args: str, stdin: bytes | None = None) -> str:
    done = subprocess.run(
        ("git", "-C", str(root), *args), input=stdin, capture_output=True, check=False
    )
    if done.returncode:
        raise RuntimeError(done.stderr.decode(errors="replace").strip())
    return done.stdout.decode(errors="replace")


def added_paths(root: Path, base: str) -> list[str]:
    """Paths added, or renamed to, between `base` and HEAD (a rename counts at its new path)."""
    out = git(
        root, "diff", "--name-status", "-z", "-M", "--diff-filter=AR", base, "HEAD"
    ).split("\0")
    paths: list[str] = []
    i = 0
    while i < len(out) and out[i]:
        status = out[i]
        step = 3 if status.startswith("R") else 2
        paths.append(out[i + step - 1])
        i += step
    return paths


def changed_paths(root: Path, base: str) -> set[str]:
    return {
        p for p in git(root, "diff", "--name-only", "-z", base, "HEAD").split("\0") if p
    }


def file_sizes(root: Path) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for rec in git(root, "ls-tree", "-r", "-l", "-z", "HEAD").split("\0"):
        meta, _, path = rec.partition("\t")
        if path and meta.split()[-1].isdigit():
            sizes[path] = int(meta.split()[-1])
    return sizes


def binary_paths(root: Path, base: str) -> set[str]:
    """Paths git treats as binary in the diff from `base` to HEAD."""
    out = git(root, "diff", "--numstat", "-z", "--no-renames", base, "HEAD").split("\0")
    return {rec.split("\t", 2)[2] for rec in out if rec.startswith("-\t-\t")}


def longest_prefix(table: dict[str, list[str]], path: str) -> str | None:
    hits = [p for p in table if path.startswith(p)]
    return max(hits, key=len) if hits else None


def allowed_type(name: str, allowed: list[str]) -> bool:
    return any(name.endswith(a) if a.startswith(".") else name == a for a in allowed)


def check(
    policy: dict, paths: list[str], sizes: dict[str, int], binaries: set[str]
) -> list[tuple[str, str, str]]:
    """(path, rule id, action) per violation."""
    found: list[tuple[str, str, str]] = []

    def add(path: str, rule: str, key: str, **ctx: object) -> None:
        found.append((path, rule, policy[key].format(path=path, **ctx)))

    for path in sorted(paths):
        name = PurePosixPath(path).name
        if "/" not in path and path not in policy["root_files"]:
            add(path, "root-file", "root_message")
        denied = any(fnmatch.fnmatchcase(name, g) for g in policy["deny_patterns"])
        denied = denied or any(f"/{d}" in f"/{path}" for d in policy["deny_dirs"])
        if denied and path not in policy["deny_exceptions"]:
            add(path, "deny-pattern", "deny_message")
        if "/" in path:
            prefix = longest_prefix(policy["extensions"], path)
            if prefix is None:
                add(path, "unknown-dir", "unknown_dir_message")
            elif not allowed_type(name, policy["extensions"][prefix]):
                allowed = " ".join(policy["extensions"][prefix])
                add(
                    path,
                    "extension",
                    "extension_message",
                    where=prefix,
                    allowed=allowed,
                )
        found.extend(
            (path, "name-convention", rule["message"].format(path=path))
            for rule in policy["names"]
            if path.startswith(rule["prefix"])
            and not re.fullmatch(rule["pattern"], path[len(rule["prefix"]) :])
        )
        size = sizes.get(path, 0)
        if size > policy["max_bytes"] and path not in policy["large_files"]:
            add(path, "size", "size_message", size=size, limit=policy["max_bytes"])
        if path in binaries and not path.startswith(tuple(policy["binary_dirs"])):
            add(
                path,
                "binary",
                "binary_message",
                allowed=" ".join(policy["binary_dirs"]),
            )
    return found


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true", help="judge every file at HEAD")
    ap.add_argument(
        "--base", default="origin/main", help="ref to take the merge base with"
    )
    ap.add_argument("--json", action="store_true", help="print violations as JSON")
    ap.add_argument("--root", type=Path, default=ROOT, help="repository to check")
    ap.add_argument("--policy", type=Path, default=POLICY)
    args = ap.parse_args(argv)
    root: Path = args.root
    policy = json.loads(args.policy.read_text())
    policy_rel = "scripts/path_policy.json"

    try:
        sizes = file_sizes(root)
        if args.all:
            base = git(root, "hash-object", "-t", "tree", "--stdin", stdin=b"").strip()
            paths = list(sizes)
            changed: set[str] = set()
        else:
            base = git(root, "merge-base", args.base, "HEAD").strip()
            paths = added_paths(root, base)
            changed = changed_paths(root, base)
        violations = check(policy, paths, sizes, binary_paths(root, base))
    except RuntimeError as e:
        print(
            f"check_paths: {e}; fetch the base (git fetch origin main) and retry",
            file=sys.stderr,
        )
        return 2

    if args.json:
        print(
            json.dumps([{"path": p, "rule": r, "action": a} for p, r, a in violations])
        )
    else:
        for p, r, a in violations:
            print(f"{p}: {r}: {a}")
        if policy_rel in changed:
            print(f"{policy_rel}: policy changed: reviewers, check the allowlist edits")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
