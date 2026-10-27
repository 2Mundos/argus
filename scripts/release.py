#!/usr/bin/env python3
"""Cut a release: infer the next version, write notes, commit and tag.

See docs/specs/releases.spec.md. Standard library only, like validate_docs.py.
"""

import argparse
import datetime as dt
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRANCH = "main"
REMOTE = "origin"
NOTES_DIR = "docs/releases"
MIGRATIONS = "apps/api/src/argus_api/db/migrations"
SPECS = "docs/specs"
TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
PR_MERGE = re.compile(
    r"^Merge pull request '(?P<title>.+)' \(#(?P<pr>\d+)\) from "
)  # Gitea
GH_MERGE = re.compile(
    r"^Merge pull request #(?P<pr>\d+) from "
)  # GitHub: title is the body
CONVENTIONAL = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]+)\))?(?P<bang>!)?:\s+(?P<desc>.+)$", re.I
)
BREAKING = re.compile(r"^BREAKING[ -]CHANGE:", re.M)
ALIASES = {"feature": "feat", "hotfix": "fix"}
SECTIONS = [("Features", "feat"), ("Fixes", "fix"), ("Maintenance", None)]

Version = tuple[int, int, int]


class ReleaseError(Exception):
    pass


@dataclass(frozen=True)
class Change:
    sha: str
    type: str  # Conventional Commit type, "" when the subject has none
    text: str  # "scope: description", or the subject as written
    pr: int | None
    breaking: bool

    def line(self) -> str:
        pr = f" (#{self.pr})" if self.pr else ""
        return f"- {self.text}{pr} (`{self.sha[:12]}`)"


def parse(sha: str, subject: str, body: str) -> Change | None:
    """One log entry; None for merges that are not PR merges."""
    pr = None
    if m := PR_MERGE.match(subject):
        subject, pr = m["title"], int(m["pr"])
    elif (m := GH_MERGE.match(subject)) and body.strip():
        subject, _, body = body.strip().partition("\n")
        pr = int(m["pr"])
    elif subject.startswith("Merge "):
        return None
    c = CONVENTIONAL.match(subject)
    if c is None:
        return Change(sha, "", subject, pr, bool(BREAKING.search(body)))
    kind = c["type"].lower()
    text = f"{c['scope']}: {c['desc']}" if c["scope"] else c["desc"]
    breaking = bool(c["bang"]) or bool(BREAKING.search(body))
    return Change(sha, ALIASES.get(kind, kind), text, pr, breaking)


def next_version(prev: Version, changes: list[Change], breaking: bool) -> Version:
    major, minor, patch = prev
    if breaking and major > 0:
        return (major + 1, 0, 0)
    if breaking or any(c.type == "feat" for c in changes):
        return (major, minor + 1, 0)
    return (major, minor, patch + 1)


def fmt(v: Version) -> str:
    return "v{}.{}.{}".format(*v)


def render(
    tag: str,
    date: str,
    prev: str | None,
    rng: str,
    changes: list[Change],
    breaking: list[Change],
    migrations: list[str],
    specs: list[str],
) -> str:
    out = [
        f"# {tag}",
        "",
        f"- Date: {date}",
        f"- Previous release: {prev or 'none'}",
        f"- Range: `{rng}`",
        "",
        "## Upgrading",
        "",
    ]
    if prev is None:
        out.append("First release.")
    elif migrations:
        names = ", ".join(f"`{m}`" for m in migrations)
        out += [
            f"Schema migrations: {names}. Argus applies them on start, after which",
            "an older build refuses the database. Back up the database (or confirm",
            "the Litestream replica is current) before upgrading; rolling back means",
            "restoring that backup and running the previous image.",
        ]
    else:
        out.append("No schema migrations: rolling back to the previous image is safe.")
    if specs:
        out += ["", "Specs added or changed:", ""]
        out += [f"- [`{s}`](../specs/{s})" for s in specs]
    if breaking:
        out += ["", "## Breaking changes", ""] + [c.line() for c in breaking]
    for title, kind in SECTIONS:
        known = {k for _, k in SECTIONS if k}
        picked = [
            c
            for c in changes
            if c.type == kind or (kind is None and c.type not in known)
        ]
        if picked:
            out += ["", f"## {title}", ""] + [c.line() for c in picked]
    return "\n".join(out) + "\n"


class Repo:
    def __init__(self, root: Path) -> None:
        self.root = root

    def git(self, *args: str, check: bool = True) -> str:
        r = subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True, check=False
        )
        if check and r.returncode:
            raise ReleaseError(f"git {' '.join(args)}: {r.stderr.strip()}")
        # rstrip("\n"), not strip(): str.strip() eats the \x1e/\x1f separators.
        return r.stdout.rstrip("\n") if r.returncode == 0 else ""

    def log(self, rng: str, *opts: str) -> list[tuple[str, str, str]]:
        raw = self.git("log", *opts, "--format=%H%x1f%s%x1f%b%x1e", rng)
        entries = (e.strip("\n").split("\x1f") for e in raw.split("\x1e"))
        return [(e[0], e[1], e[2]) for e in entries if len(e) == 3]

    def added(self, prev: str, path: str, filt: str) -> list[str]:
        names = self.git("diff", "--name-only", f"--diff-filter={filt}", prev, "HEAD")
        return sorted(
            Path(n).name for n in names.splitlines() if n.startswith(path + "/")
        )


def release(a: argparse.Namespace, repo: Repo) -> int:
    branch = repo.git("branch", "--show-current")
    if branch != BRANCH:
        raise ReleaseError(
            f"releases are cut from {BRANCH} (on {branch or 'detached HEAD'})"
        )
    if not a.dry_run and repo.git("status", "--porcelain"):
        raise ReleaseError("worktree has uncommitted changes")
    if not a.no_fetch:
        repo.git("fetch", "--quiet", "--tags", REMOTE, BRANCH)
        if repo.git("rev-parse", "HEAD") != repo.git("rev-parse", f"{REMOTE}/{BRANCH}"):
            msg = f"HEAD differs from {REMOTE}/{BRANCH}; pull or push first"
            if not a.dry_run:
                raise ReleaseError(msg)
            print(f"warning: {msg}", file=sys.stderr)

    tags = repo.git("tag", "--merged", "HEAD", "--sort=-v:refname").splitlines()
    prev = next((t for t in tags if TAG.match(t)), None)
    rng = f"{prev}..HEAD" if prev else "HEAD"
    changes = [
        c for e in repo.log(rng, "--first-parent", "--reverse") if (c := parse(*e))
    ]
    if not changes:
        raise ReleaseError(f"nothing to release in {rng}")
    breaking = [
        c for e in repo.log(rng, "--reverse") if (c := parse(*e)) and c.breaking
    ]

    if a.version:
        if not TAG.match(a.version):
            raise ReleaseError("--version must look like vX.Y.Z")
        tag = a.version
    else:
        m = TAG.match(prev) if prev else None
        base: Version = (int(m[1]), int(m[2]), int(m[3])) if m else (0, 0, 0)
        tag = fmt(next_version(base, changes, bool(breaking)))
    if repo.git("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}", check=False):
        raise ReleaseError(f"tag {tag} already exists")

    migrations = repo.added(prev, MIGRATIONS, "A") if prev else []
    specs = repo.added(prev, SPECS, "AM") if prev else []
    head = repo.git("rev-parse", "--short=12", "HEAD")
    notes = render(
        tag,
        a.date,
        prev,
        f"{prev}..{head}" if prev else head,
        changes,
        breaking,
        migrations,
        specs,
    )
    path = f"{NOTES_DIR}/{a.date}.{tag.replace('.', '-')}.ref.md"
    if a.dry_run:
        print(f"{path}\n\n{notes}", end="")
        return 0

    (repo.root / NOTES_DIR).mkdir(parents=True, exist_ok=True)
    (repo.root / path).write_text(notes)
    repo.git("add", path)
    repo.git("commit", "--quiet", "-m", f"docs(release): {tag}")
    repo.git("tag", "-a", tag, "-m", f"Release {tag}", "-m", f"Notes: {path}")
    print(f"Wrote {path}, committed, tagged {tag}")
    if a.push:
        repo.git("push", "--atomic", REMOTE, BRANCH, tag)
        print(f"Pushed {BRANCH} and {tag}; the release workflow takes it from here")
    else:
        print(f"Push with: git push --atomic {REMOTE} {BRANCH} {tag}")
    return 0


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--version", help="release this vX.Y.Z instead of inferring it")
    p.add_argument("--date", default=dt.date.today().isoformat(), help="YYYY-MM-DD")
    p.add_argument("--dry-run", action="store_true", help="print the notes only")
    p.add_argument("--push", action="store_true", help="push main and the tag")
    p.add_argument("--no-fetch", action="store_true", help="skip the origin check")
    a = p.parse_args(argv)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a.date):
        p.error("--date must be YYYY-MM-DD")
    try:
        return release(a, Repo(root))
    except ReleaseError as e:
        print(f"release: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
