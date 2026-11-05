#!/usr/bin/env python3
"""Check spec status frontmatter and the specs index (docs/specs/README.md).

Every `docs/specs/*.spec.md` opens with:

    ---
    status: implemented | partial | draft | deferred | superseded
    created: YYYY-MM-DD
    updated: YYYY-MM-DD
    ---

A `partial` spec has an `Open items` section, a `superseded` one links the spec
that replaces it, and every spec is listed in the index with its status.
`--write-index` regenerates the index. Standard library only, like validate_docs.py.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPECS = ROOT / "docs" / "specs"
STATUSES = ("implemented", "partial", "draft", "deferred", "superseded")
FIELDS = ("status", "created", "updated")
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
TITLE = re.compile(r"^#\s+(.+)$", re.M)
OPEN_ITEMS = re.compile(r"^#{1,6}\s+(?:\d+(?:\.\d+)*\.?\s+)?Open items\b", re.M | re.I)
SUPERSEDED_BY = re.compile(r"superseded by\b[^\n]*\]\(([^)]+\.spec\.md)[^)]*\)", re.I)
ROW = re.compile(r"^\|\s*\[[^\]]*\]\(([^)]+)\)\s*\|\s*`?([a-z]+)`?\s*\|", re.M)
INDEX_HEAD = """# Specs index

Every spec in this directory with its status, kept in sync by
`scripts/check_specs.py` (CI and pre-push fail otherwise; regenerate with
`python3 scripts/check_specs.py --write-index`). Status meanings:

- `implemented`: main does what the spec says.
- `partial`: some numbered decisions are not built yet; the spec's **Open items**
  section lists them.
- `draft`: agreed design, not built.
- `deferred`: parked; not planned for now.
- `superseded`: replaced by the spec it links.

| Spec | Status |
| --- | --- |
"""


def parse(text: str) -> tuple[dict[str, str] | None, str]:
    m = FRONTMATTER.match(text)
    if not m:
        return None, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.split("#", 1)[0].strip()
    return meta, text[m.end() :]


def _date(value: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value or "")
    except ValueError:
        return None


def check_spec(path: Path, today: dt.date) -> tuple[list[str], dict[str, str]]:
    rel = path.name
    meta, body = parse(path.read_text())
    if meta is None:
        return [f"{rel}: missing YAML frontmatter (status, created, updated)"], {}
    errors = [f"{rel}: frontmatter missing '{f}'" for f in FIELDS if f not in meta]
    status = meta.get("status", "")
    if "status" in meta and status not in STATUSES:
        errors.append(f"{rel}: status '{status}' not one of {', '.join(STATUSES)}")
    created, updated = _date(meta.get("created")), _date(meta.get("updated"))
    for field, value in (("created", created), ("updated", updated)):
        if field in meta and value is None:
            errors.append(f"{rel}: {field} must be YYYY-MM-DD")
        elif value and value > today:
            errors.append(f"{rel}: {field} {value} is in the future")
    if created and updated and updated < created:
        errors.append(f"{rel}: updated {updated} is before created {created}")
    if status == "partial" and not OPEN_ITEMS.search(body):
        errors.append(f"{rel}: partial spec needs an 'Open items' section")
    if status == "superseded":
        m = SUPERSEDED_BY.search(body)
        if not m or not (path.parent / m.group(1).split("#")[0]).is_file():
            errors.append(f"{rel}: superseded spec must link its replacement")
    return errors, meta


def render_index(specs: dict[Path, dict[str, str]]) -> str:
    rows = []
    for path, meta in sorted(specs.items()):
        m = TITLE.search(parse(path.read_text())[1])
        title = m.group(1).strip() if m else path.name
        rows.append(f"| [{title}]({path.name}) | `{meta.get('status', '')}` |\n")
    return INDEX_HEAD + "".join(rows)


def main(argv: list[str] | None = None, specs_dir: Path = SPECS) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write-index", action="store_true", help="regenerate README.md")
    args = ap.parse_args(argv)
    today = dt.date.today()
    errors: list[str] = []
    specs: dict[Path, dict[str, str]] = {}
    for path in sorted(specs_dir.glob("*.spec.md")):
        errs, meta = check_spec(path, today)
        errors += errs
        specs[path] = meta
    index = specs_dir / "README.md"
    if args.write_index:
        index.write_text(render_index(specs))
    elif not index.is_file():
        errors.append("README.md: specs index missing (run --write-index)")
    else:
        listed = dict(ROW.findall(index.read_text()))
        for path, meta in specs.items():
            if path.name not in listed:
                errors.append(f"README.md: {path.name} not listed in the index")
            elif meta and listed[path.name] != meta.get("status"):
                errors.append(
                    f"README.md: {path.name} listed as '{listed[path.name]}',"
                    f" frontmatter says '{meta.get('status')}'"
                )
        for name in sorted(set(listed) - {p.name for p in specs}):
            errors.append(f"README.md: lists {name}, which does not exist")
    for e in errors:
        print(f"error: {e}")
    counts = {s: sum(m.get("status") == s for m in specs.values()) for s in STATUSES}
    print("check_specs: " + ", ".join(f"{n} {s}" for s, n in counts.items() if n))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
