#!/usr/bin/env python3
"""Test hygiene checks (docs/drafts/2026-10-31.test-hygiene-lint.draft.md).

Report-only by default; `--strict` exits non-zero on any unannotated hit.
Suppress one hit with a comment on the line above:
`# check-tests: allow <rule> — <reason>` (no reason, no suppression).
"""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("apps/api", "packages/core", "scripts")
SKIP_PARTS = {".venv", "node_modules", "__pycache__", "dist", "build"}

SLEEP_CALLS = {"time.sleep", "asyncio.sleep", "sleep"}
CLOCK_CALLS = {
    "time.time",
    "time.monotonic",
    "time.perf_counter",
    "datetime.now",
    "datetime.utcnow",
    "datetime.datetime.now",
    "datetime.datetime.utcnow",
}
TX_CLOCK_CALLS = {
    "time.time",
    "datetime.now",
    "datetime.utcnow",
    "datetime.datetime.now",
    "datetime.datetime.utcnow",
}
TIMEOUT_RETRY_MARKS = {"timeout", "flaky", "rerun", "reruns", "retry"}
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
REASON_MARKS = {"skip", "skipif", "xfail"}

ALLOW = re.compile(r"#\s*check-tests:\s*allow\s+([\w-]+)\s*(?:—|--|-|:)\s*(\S.*)$")
SUPPRESSIONS = (
    re.compile(r"noqa(?::\s*[A-Za-z]+\d+(?:\s*,\s*[A-Za-z]+\d+)*)?"),
    re.compile(r"type:\s*ignore(?:\[[^\]]*\])?"),
    re.compile(r"biome-ignore(?:\s+[\w/]+)*"),
)


class Hit(NamedTuple):
    path: str
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.message}"


def dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def call_name(node: ast.AST) -> str:
    return dotted(node.func) if isinstance(node, ast.Call) else ""


def is_nonzero_sleep(node: ast.AST) -> bool:
    if call_name(node) not in SLEEP_CALLS:
        return False
    args = node.args
    return not (
        len(args) == 1 and isinstance(args[0], ast.Constant) and args[0].value == 0
    )


def walk_scope(nodes: list[ast.stmt]):
    """Walk statements without descending into nested function definitions."""
    stack = list(nodes)
    while stack:
        node = stack.pop()
        yield node
        stack.extend(
            c for c in ast.iter_child_nodes(node) if not isinstance(c, FUNCTIONS)
        )


def check_sleeps(tree: ast.Module) -> list[tuple[int, str, str]]:
    hits = []
    for node in ast.walk(tree):
        if is_nonzero_sleep(node):
            hits.append((node.lineno, "sleep", "wall-time sleep; await an event"))
        if isinstance(node, (ast.While, ast.For, ast.AsyncFor)):
            body = list(walk_scope(node.body))
            if any(is_nonzero_sleep(n) for n in body) and (
                isinstance(node, ast.While)
                or any(isinstance(n, (ast.Break, ast.Return, ast.If)) for n in body)
            ):
                hits.append((node.lineno, "poll-loop", "loop polls and sleeps"))
    return hits


def check_elapsed(tree: ast.Module) -> list[tuple[int, str, str]]:
    hits = []
    scopes = [tree, *(n for n in ast.walk(tree) if isinstance(n, FUNCTIONS))]
    for scope in scopes:
        nodes = list(walk_scope(scope.body))
        stamps = {
            t.id
            for n in nodes
            if isinstance(n, ast.Assign) and call_name(n.value) in CLOCK_CALLS
            for t in n.targets
            if isinstance(t, ast.Name)
        }

        def is_clock(e: ast.AST, stamps: set[str] = stamps) -> bool:
            return call_name(e) in CLOCK_CALLS or (
                isinstance(e, ast.Name) and e.id in stamps
            )

        hits += [
            (n.lineno, "elapsed-time", "elapsed-time delta; assert on events")
            for n in nodes
            if isinstance(n, ast.BinOp)
            and isinstance(n.op, ast.Sub)
            and (is_clock(n.left) or is_clock(n.right))
        ]
    return hits


def check_marks(tree: ast.Module) -> list[tuple[int, str, str]]:
    hits = []
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        name = dotted(node)
        if not name.startswith("pytest.mark."):
            continue
        mark = name.removeprefix("pytest.mark.")
        if mark in TIMEOUT_RETRY_MARKS:
            hits.append((node.lineno, "timeout-retry", f"{name}; fix the test"))
        elif mark in REASON_MARKS:
            call = next(
                (
                    c
                    for c in ast.walk(tree)
                    if isinstance(c, ast.Call) and c.func is node
                ),
                None,
            )
            if id(node) not in called or not any(
                k.arg == "reason" for k in call.keywords
            ):
                hits.append((node.lineno, "skip-reason", f"{name} needs reason="))
    return hits


def explained(comment: str, directive: re.Match[str]) -> bool:
    rest = comment[: directive.start()] + comment[directive.end() :]
    return bool(re.search(r"\w", rest))


def check_suppressions(comments: dict[int, str]) -> list[tuple[int, str, str]]:
    hits = []
    for line, comment in comments.items():
        body = comment.lstrip("#").strip()
        for pattern in SUPPRESSIONS:
            m = pattern.search(body)
            if m and not explained(body, m):
                hits.append((line, "suppress-reason", f"`{m.group(0)}` needs a reason"))
    return hits


def check_tx_clock(tree: ast.Module) -> list[tuple[int, str, str]]:
    hits = []
    for fn in ast.walk(tree):
        if not isinstance(fn, FUNCTIONS):
            continue
        args = fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs
        if not any(
            a.annotation and dotted(a.annotation).split(".")[-1] == "Tx" for a in args
        ):
            continue
        hits += [
            (n.lineno, "tx-clock", f"{call_name(n)}() in a Tx function; use tx.now")
            for n in ast.walk(fn)
            if call_name(n) in TX_CLOCK_CALLS
        ]
    return hits


def comments_of(source: str) -> dict[int, str]:
    out = {}
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            out[tok.start[0]] = tok.string
    return out


def check_source(source: str, kind: str, path: str = "<source>") -> list[Hit]:
    """Check one module. `kind` is "test" or "app". Returns unsuppressed hits."""
    tree = ast.parse(source)
    comments = comments_of(source)
    if kind == "app":
        found = check_tx_clock(tree)
    else:
        found = [
            *check_sleeps(tree),
            *check_elapsed(tree),
            *check_marks(tree),
            *check_suppressions(comments),
        ]
    hits = []
    for line, rule, message in sorted(set(found)):
        allow = ALLOW.match(comments.get(line - 1, ""))
        if allow and allow.group(1) == rule:
            continue
        hits.append(Hit(path, line, rule, message))
    return hits


def kind_of(path: Path) -> str | None:
    parts = path.parts
    if "argus_api" in parts and "tests" not in parts:
        return "app"
    if "tests" in parts or path.name.startswith("test_") or path.name == "conftest.py":
        return "test"
    return None


def discover() -> list[Path]:
    return sorted(
        p
        for d in SCAN_DIRS
        for p in (ROOT / d).rglob("*.py")
        if not SKIP_PARTS & set(p.parts)
    )


def main(argv: list[str]) -> int:
    strict = "--strict" in argv
    paths = [Path(a) for a in argv if a != "--strict"] or [
        p.relative_to(ROOT) for p in discover()
    ]
    hits: list[Hit] = []
    for path in paths:
        kind = kind_of(ROOT / path)
        if kind:
            hits += check_source((ROOT / path).read_text(), kind, str(path))
    for hit in hits:
        print(hit)
    print(f"check_tests: {len(hits)} hit(s)" + ("" if strict else " (report-only)"))
    return 1 if strict and hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
