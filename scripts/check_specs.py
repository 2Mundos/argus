#!/usr/bin/env python3
"""Check spec and draft status frontmatter and their indexes (docs/{specs,drafts}/README.md).

Every `docs/specs/*.spec.md` and `docs/drafts/*.draft.md` opens with:

    ---
    status: implemented | partial | draft | deferred | superseded
    created: YYYY-MM-DD
    updated: YYYY-MM-DD
    ---

A `partial` doc has an `Open items` section, a `superseded` one links the spec
that replaces it, and every doc is listed in its directory's index with its status.
`--write-index` regenerates the indexes. Standard library only, like validate_docs.py.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
STATUSES = ("implemented", "partial", "draft", "deferred", "superseded")
FIELDS = ("status", "created", "updated")
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
TITLE = re.compile(r"^#\s+(.+)$", re.M)
OPEN_ITEMS = re.compile(r"^#{1,6}\s+(?:\d+(?:\.\d+)*\.?\s+)?Open items\b", re.M | re.I)
SUPERSEDED_BY = re.compile(r"superseded by\b[^\n]*?\]\(([^)]+\.spec\.md)[^)]*\)", re.I)
ROW = re.compile(r"^\|\s*\[[^\]]*\]\(([^)]+)\)\s*\|\s*`?([a-z]+)`?\s*\|", re.M)
INDEX_HEAD = """# {title}

Every {noun} in this directory with its status, kept in sync by
`scripts/check_specs.py` (CI and pre-push fail otherwise; regenerate with
`python3 scripts/check_specs.py --write-index`). Status meanings:

- `implemented`: main does what it says{built}.
- `partial`: some of it is not built yet; its **Open items** section lists what.
- `draft`: proposed or agreed, not built.
- `deferred`: parked; not planned for now.
- `superseded`: replaced by the spec it links.

| {col} | Status |
| --- | --- |
"""


class Kind:
    def __init__(self, folder: str, pattern: str, noun: str, built: str = "") -> None:
        self.folder, self.pattern, self.noun, self.built = folder, pattern, noun, built


KINDS = (
    Kind("specs", "*.spec.md", "spec"),
    Kind("drafts", "*.draft.md", "draft", ", built without a spec of its own"),
)


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
        errors.append(f"{rel}: partial doc needs an 'Open items' section")
    if status == "superseded":
        m = SUPERSEDED_BY.search(body)
        if not m or not (path.parent / m.group(1).split("#")[0]).is_file():
            errors.append(f"{rel}: superseded doc must link its replacement spec")
    return errors, meta


def render_index(docs: dict[Path, dict[str, str]], kind: Kind) -> str:
    rows = []
    for path, meta in sorted(docs.items()):
        m = TITLE.search(parse(path.read_text())[1])
        name = m.group(1).strip() if m else path.name
        rows.append(f"| [{name}]({path.name}) | `{meta.get('status', '')}` |\n")
    head = INDEX_HEAD.format(
        title=f"{kind.noun.capitalize()}s index",
        noun=kind.noun,
        built=kind.built,
        col=kind.noun.capitalize(),
    )
    return head + "".join(rows)


def check_dir(
    directory: Path, kind: Kind, write: bool, today: dt.date
) -> tuple[list[str], dict[Path, dict[str, str]]]:
    errors: list[str] = []
    docs: dict[Path, dict[str, str]] = {}
    for path in sorted(directory.glob(kind.pattern)):
        errs, meta = check_spec(path, today)
        errors += [f"{kind.folder}/{e}" for e in errs]
        docs[path] = meta
    index = directory / "README.md"
    where = f"{kind.folder}/README.md"
    if write:
        index.write_text(render_index(docs, kind))
    elif not index.is_file():
        errors.append(f"{where}: index missing (run --write-index)")
    else:
        listed = dict(ROW.findall(index.read_text()))
        for path, meta in docs.items():
            if path.name not in listed:
                errors.append(f"{where}: {path.name} not listed in the index")
            elif meta and listed[path.name] != meta.get("status"):
                errors.append(
                    f"{where}: {path.name} listed as '{listed[path.name]}',"
                    f" frontmatter says '{meta.get('status')}'"
                )
        for name in sorted(set(listed) - {p.name for p in docs}):
            errors.append(f"{where}: lists {name}, which does not exist")
    return errors, docs


def main(argv: list[str] | None = None, docs_dir: Path = DOCS) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--write-index", action="store_true", help="regenerate indexes")
    args = ap.parse_args(argv)
    today = dt.date.today()
    errors: list[str] = []
    for kind in KINDS:
        directory = docs_dir / kind.folder
        if not directory.is_dir():
            continue
        errs, docs = check_dir(directory, kind, args.write_index, today)
        errors += errs
        counts = {s: sum(m.get("status") == s for m in docs.values()) for s in STATUSES}
        summary = ", ".join(f"{n} {s}" for s, n in counts.items() if n)
        print(f"check_specs: {kind.folder}: {summary or 'none'}")
    for e in errors:
        print(f"error: {e}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
