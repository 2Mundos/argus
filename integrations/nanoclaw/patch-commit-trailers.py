#!/usr/bin/env python3
"""Forbid Co-Authored-By trailers in the harness's system-level prompt (install.sh).

usage: patch-commit-trailers.py /path/to/nanoclaw

Appends one Markdown section to NanoClaw's core canon container/CLAUDE.md. That
file is the shared source composeGroupProjectDoc() emits as the runtime contract
(CLAUDE.md for the claude provider, AGENTS.md for codex). Idempotent: the marker
comment makes a re-run a byte-identical no-op. Fails closed (non-zero exit,
nothing written) when the file is missing or does not look like the upstream
canon — an installer must never silently skip this.
"""

import sys
from pathlib import Path

MARKER = "<!-- argus: no attribution trailers in commit messages -->"
HEADING = "## Commit attribution"
CANON_OPENING = "You are a NanoClaw agent."

SECTION = f"""
{HEADING}

{MARKER}

A commit message you create must not contain a `Co-Authored-By:` trailer, or
any other co-author or attribution trailer. Do not add one yourself, and do
not ask a delegated executor to add one. This covers new commits only: never
rewrite history or strip trailers from existing commits, and keep the
project's configured git author and committer unchanged.
"""


def main(root: Path) -> None:
    p = root / "container" / "CLAUDE.md"
    if not p.is_file():
        sys.exit(f"{p}: missing; NanoClaw layout changed upstream")
    text = p.read_text()
    if not text.startswith(CANON_OPENING):
        sys.exit(f"{p}: does not open as NanoClaw core canon ({CANON_OPENING!r})")
    if MARKER in text:
        return
    # Heading without the Argus marker is not our section — fail closed.
    if f"\n{HEADING}\n" in text or text.startswith(f"{HEADING}\n"):
        sys.exit(
            f"{p}: {HEADING!r} present without Argus marker; upstream shape changed"
        )
    # Preserve a single trailing newline before the section (canon already has one).
    patched = text.rstrip("\n") + "\n" + SECTION
    if not patched.endswith("\n"):
        patched += "\n"
    p.write_text(patched)
    print(f"patched {p}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: patch-commit-trailers.py /path/to/nanoclaw")
    main(Path(sys.argv[1]))
