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
RELEASE = re.compile(r"^\d{4}-\d{2}-\d{2}\.v\d+-\d+-\d+\.ref\.md$")
SECTIONS = [
    "1. Purpose and Scope",
    "2. Non-Goals",
    "3. Decisions Made",
    "4. Open Questions",
    "5. Constraints",
    "6. Implementation Consequences",
]
LINK = re.compile(r"\]\(([^)]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.M)
SKIP_URL = re.compile(r"^(?:https?:|mailto:|#)")


def _heading_slugs(text: str) -> set[str]:
    slugs: set[str] = set()
    counts: dict[str, int] = {}
    for m in HEADING.finditer(text):
        base = re.sub(r"[^\w\s-]", "", m.group(1).strip().lower())
        base = re.sub(r"\s+", "-", base).strip("-")
        if not base:
            continue
        n = counts.get(base, 0)
        counts[base] = n + 1
        slugs.add(base if n == 0 else f"{base}-{n}")
    return slugs


def _check_doc_links(path: Path, errors: list[str]) -> None:
    rel_doc = path.relative_to(ROOT)
    for raw in LINK.findall(path.read_text()):
        url = raw.strip()
        if not url or SKIP_URL.match(url):
            continue
        parts = url.split("#", 1)
        target_s, anchor = parts[0], parts[1] if len(parts) > 1 else ""
        if not target_s:
            continue
        resolved = (path.parent / target_s).resolve()
        if not resolved.is_file() and not resolved.is_dir():
            errors.append(f"{rel_doc}: broken link target {url}")
            continue
        if anchor and resolved.suffix.lower() == ".md":
            slugs = _heading_slugs(resolved.read_text())
            if anchor.lower() not in slugs:
                errors.append(f"{rel_doc}: unknown anchor #{anchor} in {url}")


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
            elif "releases" in doc.parts:
                if not RELEASE.match(doc.name):
                    errors.append(f"{rel}: name must match YYYY-MM-DD.vX-Y-Z.ref.md")
                continue  # indexed as a series: README links releases/ once
            elif not DATED.match(doc.name):
                errors.append(f"{rel}: name must match YYYY-MM-DD.name.type.md")
        if doc.resolve() not in linked:
            errors.append(f"{rel}: not linked from docs/README.md")
    releases = DOCS / "releases"
    if any(releases.glob("*.md")) and releases.resolve() not in linked:
        errors.append("docs/releases: not linked from docs/README.md")
    for doc in [INDEX, ROOT / "README.md", ROOT / "AGENTS.md"]:
        if doc.is_file():
            _check_doc_links(doc, errors)
    for doc in sorted(DOCS.rglob("*.md")):
        if doc != INDEX:
            _check_doc_links(doc, errors)
    for e in errors:
        print(f"error: {e}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
