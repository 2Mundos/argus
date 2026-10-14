#!/usr/bin/env python3
"""Validate docs/ naming, spec structure, and index reachability."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
INDEX = DOCS / "README.md"
DATED = re.compile(r"^\d{4}-\d{2}-\d{2}\.[a-z0-9-]+\.(draft|research|ref)\.md$")
SPEC = re.compile(r"^[a-z0-9-]+\.spec\.md$")
SECTIONS = [
    "1. Purpose and Scope",
    "2. Non-Goals",
    "3. Decisions Made",
    "4. Open Questions",
    "5. Constraints",
    "6. Implementation Consequences",
]
LINK = re.compile(r"\]\(([^)]+)\)")


def main() -> int:
    errors: list[str] = []
    linked = {
        (INDEX.parent / m.split("#")[0]).resolve()
        for m in LINK.findall(INDEX.read_text())
    }
    for doc in sorted(DOCS.rglob("*.md")):
        rel = doc.relative_to(ROOT)
        if doc == INDEX:
            continue
        if "templates" not in doc.parts:
            if "specs" in doc.parts:
                if not SPEC.match(doc.name):
                    errors.append(f"{rel}: spec name must match topic.spec.md")
                else:
                    text = doc.read_text()
                    errors += [
                        f"{rel}: missing section '{s}'"
                        for s in SECTIONS
                        if not re.search(rf"^#\s+{re.escape(s)}", text, re.M)
                    ]
            elif not DATED.match(doc.name):
                errors.append(f"{rel}: name must match YYYY-MM-DD.name.type.md")
        if doc.resolve() not in linked:
            errors.append(f"{rel}: not linked from docs/README.md")
    for e in errors:
        print(f"error: {e}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
