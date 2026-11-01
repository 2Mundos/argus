#!/usr/bin/env python3
"""Run only the checks the changed files can affect (docs/drafts changed-only-checks).

Compares the working tree (committed and uncommitted changes, plus untracked
files) with the merge base of origin/main. `--plan` prints the steps and exits.
A rerun on the same base and file contents as the last passing run exits early.
When the merge base cannot be found, the full `make check` and `make build` run.
"""

import ast
import hashlib
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
PY_SRC_ROOTS = ("apps/api/src/", "packages/core/src/")
PY_TEST_ROOTS = (
    "apps/api/tests/",
    "scripts/",
)  # on pytest's pythonpath: imported by stem
PY_AREAS = ("apps/api/", "packages/core/")
TEST_DIRS = ("apps/api/tests", "packages/core/tests", "scripts")
MIGRATIONS = re.compile(r"^apps/api/src/argus_api/db/migrations/.+\.sql$")
FULL_CHECK = re.compile(
    r"(^|/)(uv\.lock|pnpm-lock\.yaml|pyproject\.toml|package\.json)$"
)
BIOME_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".css")
TS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
OPENAPI_MARKERS = re.compile(r"APIRouter|include_router|BaseModel")
OPENAPI_FILES = {
    "packages/api-client/openapi.json",
    "packages/api-client/src/schema.d.ts",
    "scripts/export_openapi.py",
}
DASHBOARD_CONFIG = re.compile(
    r"(^|/)(vite\.config\.\w+|tsconfig[\w.]*\.json|index\.html)$"
)
TS_IMPORT = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)["']([^"']+)["']"""
)
WORKSPACE_ALIASES = {"@argus/api-client": "packages/api-client/src/index.ts"}
MIGRATION_PREFLIGHT = (
    "from argus_api.db.migrate import load_migrations;"
    "m = load_migrations();"
    "print(f'Migration preflight OK: {len(m)} migrations, latest={m[-1].version}')"
)


@dataclass(frozen=True)
class Step:
    name: str
    cmd: tuple[str, ...]
    timeout: float  # seconds; the step is killed and reported failed past this
    weight: int = 1  # CPUs of the budget the step occupies


@dataclass
class Result:
    step: Step
    status: str  # "ok", "FAILED" or "TIMEOUT"
    seconds: float
    output: str = field(repr=False, default="")


# ---------------------------------------------------------------- import graph


def py_module_names(path: str) -> list[str]:
    """Dotted names `path` can be imported as (src roots) or its stem (pythonpath dirs)."""
    p = PurePosixPath(path)
    for root in PY_SRC_ROOTS:
        if path.startswith(root):
            parts = list(p.relative_to(root.rstrip("/")).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            return [".".join(parts)]
    if any(path.startswith(r) for r in PY_TEST_ROOTS) and p.parent.as_posix() in {
        r.rstrip("/") for r in PY_TEST_ROOTS
    }:
        return [p.stem]
    return []


def py_imports(path: str, text: str) -> set[str]:
    """Module names a Python file may import (candidates; need not exist)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    own = (py_module_names(path) or [""])[0]
    is_pkg = PurePosixPath(path).name == "__init__.py"
    pkg = own.split(".") if own else []
    if not is_pkg:
        pkg = pkg[:-1]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = (
                    pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                )
                base = ".".join([*base_parts, *([node.module] if node.module else [])])
            else:
                base = node.module or ""
            if base:
                found.add(base)
            found.update(f"{base}.{a.name}" if base else a.name for a in node.names)
    return found


def with_parents(name: str) -> set[str]:
    """Importing a.b.c also runs the __init__ of a and a.b."""
    parts = name.split(".")
    return {".".join(parts[:i]) for i in range(1, len(parts) + 1)}


def ts_candidates(spec: str, importer: str) -> set[str]:
    """Repo paths a TS/JS import specifier may resolve to (candidates; need not exist)."""
    if spec in WORKSPACE_ALIASES:
        return {WORKSPACE_ALIASES[spec]}
    if spec.startswith("@argus/api-client/"):
        return {"packages/api-client/src/" + spec.rsplit("/", 1)[1] + ".d.ts"}
    if not spec.startswith("."):
        return set()
    base = os.path.normpath(os.path.join(os.path.dirname(importer), spec))
    out = {base}
    stem = base.rsplit(".", 1)[0] if base.endswith((".js", ".jsx", ".mjs")) else base
    for ext in (*TS_SUFFIXES, ".d.ts"):
        out.add(stem + ext)
        out.add(f"{base}/index{ext}")
    return out


class Graph:
    """Reverse static import graph over the tracked and untracked files of the repo."""

    def __init__(self, files: dict[str, str]) -> None:
        self.py_importers: dict[str, set[str]] = {}
        self.ts_importers: dict[str, set[str]] = {}
        for path, text in files.items():
            if path.endswith(".py"):
                for name in py_imports(path, text):
                    for dep in with_parents(name):
                        self.py_importers.setdefault(dep, set()).add(path)
            elif path.endswith(TS_SUFFIXES):
                for spec in TS_IMPORT.findall(text):
                    for dep in ts_candidates(spec, path):
                        self.ts_importers.setdefault(dep, set()).add(path)

    def dependents(self, changed: list[str]) -> set[str]:
        """Changed files plus every file that imports one of them, transitively."""
        seen = set(changed)
        todo = list(changed)
        while todo:
            path = todo.pop()
            if path.endswith(".py"):
                keys = py_module_names(path)
                importers = self.py_importers
            else:
                keys, importers = [path], self.ts_importers
            for key in keys:
                for importer in importers.get(key, ()):
                    if importer not in seen:
                        seen.add(importer)
                        todo.append(importer)
        return seen


# ------------------------------------------------------------------------ plan


def is_py_test(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.endswith(".py") and (name.startswith("test_") or name == "conftest.py")


def is_ts_test(path: str) -> bool:
    return bool(re.search(r"\.(test|spec)\.[jt]sx?$", path))


def beside_tests(path: str, all_files: list[str]) -> set[str]:
    """Tests beside a module: test_<stem>.py in its dir or in a sibling tests dir."""
    p = PurePosixPath(path)
    wanted = f"test_{p.stem}.py"
    dirs = {p.parent.as_posix()}
    for area in PY_AREAS:
        if path.startswith(area):
            dirs.add(area + "tests")
    return {
        f
        for f in all_files
        if PurePosixPath(f).name == wanted
        and PurePosixPath(f).parent.as_posix() in dirs
    }


def python_tests(
    changed: list[str], all_files: list[str], graph: Graph
) -> set[str] | None:
    """Existing test files to run for the changed paths, or None for the whole suite."""
    tests: set[str] = set()
    for c in changed:
        if PurePosixPath(c).name == "conftest.py" or "/tests/fixtures/" in c:
            return None  # shared by every test beside it
        if c.endswith(".py"):
            tests |= beside_tests(c, all_files)
    tests |= {
        f
        for f in graph.dependents([c for c in changed if c.endswith(".py")])
        if is_py_test(f) and f.rsplit("/", 1)[0] in TEST_DIRS
    }
    return {t for t in tests if os.path.exists(ROOT / t)}


def plan(
    changed: list[str], all_files: list[str], graph: Graph, cpus: int
) -> list[Step]:
    """Steps for a set of changed paths (deleted ones included)."""
    if not changed:
        return []
    uv = ("uv", "run")
    if any(FULL_CHECK.search(c) for c in changed):
        return [
            Step("make check", ("make", "check"), 3600, cpus),
            Step("make build", ("make", "build"), 900, 2),
        ]
    present = [c for c in changed if os.path.exists(ROOT / c)]
    steps: list[Step] = []
    half = max(1, cpus // 2)

    py = [c for c in present if c.endswith(".py")]
    if any(MIGRATIONS.match(c) for c in changed):
        steps.append(
            Step(
                "pytest (full suite)",
                (*uv, "pytest", "-q", "-n", str(half)),
                3600,
                half,
            )
        )
        steps.append(
            Step("migration preflight", (*uv, "python", "-c", MIGRATION_PREFLIGHT), 120)
        )
    elif any(c.endswith(".py") or "/tests/" in c for c in changed):
        tests = python_tests(changed, all_files, graph)
        if tests is None:
            steps.append(
                Step(
                    "pytest (full suite)",
                    (*uv, "pytest", "-q", "-n", str(half)),
                    3600,
                    half,
                )
            )
        elif tests:
            n = min(half, len(tests))
            cmd = (*uv, "pytest", "-q", "-n", str(n), *sorted(tests))
            steps.append(Step(f"pytest ({len(tests)} files)", cmd, 3600, n))

    if py:
        steps.append(
            Step("ruff check", (*uv, "ruff", "check", "--force-exclude", *py), 120)
        )
        steps.append(
            Step(
                "ruff format",
                (*uv, "ruff", "format", "--check", "--force-exclude", *py),
                120,
            )
        )
    # ty is configured for apps/api/src and packages/core/src and runs whole-project:
    # it needs cross-package types, so per-package scoping is not worth the risk.
    if any(c.endswith(".py") and c.startswith(PY_SRC_ROOTS) for c in changed):
        steps.append(Step("ty", (*uv, "ty", "check"), 600))

    if any(
        c.startswith(PY_SRC_ROOTS) and c.endswith(".py") and _is_api_surface(c)
        for c in changed
    ) or any(c in OPENAPI_FILES for c in changed):
        steps.append(Step("openapi check", ("make", "openapi-check"), 600))

    js = [c for c in present if c.endswith(BIOME_SUFFIXES)]
    if js:
        steps.append(
            Step(
                "biome",
                ("pnpm", "exec", "biome", "check", "--no-errors-on-unmatched", *js),
                120,
            )
        )
    web = [
        c for c in changed if c.startswith(("apps/dashboard/", "packages/api-client/"))
    ]
    if web:
        steps.append(Step("tsc", ("pnpm", "typecheck"), 600))
        tests = dashboard_tests(changed, all_files, graph)
        if tests:
            cmd = ("pnpm", "--filter", "@argus/dashboard", "exec", "vitest", "run")
            args = (
                []
                if tests is True
                else [t.removeprefix("apps/dashboard/") for t in sorted(tests)]
            )
            steps.append(Step("vitest", (*cmd, *args), 900, 2))
        steps.append(Step("dashboard build", ("pnpm", "build"), 900, 2))

    if any(c.startswith("docs/") for c in changed):
        steps.append(Step("validate docs", ("python3", "scripts/validate_docs.py"), 60))
    return steps


def _is_api_surface(path: str) -> bool:
    try:
        return bool(OPENAPI_MARKERS.search((ROOT / path).read_text()))
    except OSError:
        return True  # a deleted route or model module changes the schema


def dashboard_tests(
    changed: list[str], all_files: list[str], graph: Graph
) -> set[str] | bool | None:
    """Vitest files to run; True runs everything."""
    web = [
        c for c in changed if c.startswith(("apps/dashboard/", "packages/api-client/"))
    ]
    if any(DASHBOARD_CONFIG.search(c) for c in web):
        return True
    code = [c for c in web if c.endswith(TS_SUFFIXES) or c.endswith(".d.ts")]
    deps = graph.dependents(code)
    return {
        f
        for f in deps
        if is_ts_test(f)
        and f.startswith("apps/dashboard/")
        and os.path.exists(ROOT / f)
    }


# ------------------------------------------------------------------- git state


def git(*args: str, check: bool = True) -> str:
    done = subprocess.run(
        ("git", *args), cwd=ROOT, capture_output=True, text=True, check=False
    )
    if check and done.returncode:
        raise RuntimeError(done.stderr.strip())
    return done.stdout.strip() if done.returncode == 0 else ""


def merge_base(ref: str = "origin/main") -> str | None:
    """The merge base of `ref` and HEAD, or None (shallow clone, no origin/main)."""
    return git("merge-base", ref, "HEAD", check=False) or None


def changed_files(base: str) -> list[str]:
    tracked = git("diff", "--name-only", "--no-renames", "-z", base).split("\0")
    untracked = git("ls-files", "--others", "--exclude-standard", "-z").split("\0")
    return sorted({p for p in (*tracked, *untracked) if p})


def fingerprint(base: str, changed: list[str]) -> str:
    """Hash of the merge base and the content of every changed file."""
    h = hashlib.sha256(base.encode())
    for path in changed:
        full = ROOT / path
        blob = git("hash-object", "--", path) if full.is_file() else "deleted"
        h.update(f"\0{path}\0{blob}".encode())
    return h.hexdigest()


def read_graph_files() -> dict[str, str]:
    paths = git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split(
        "\0"
    )
    files: dict[str, str] = {}
    for p in filter(None, paths):
        if p.endswith((".py", *TS_SUFFIXES)):
            try:
                files[p] = (ROOT / p).read_text()
            except (OSError, UnicodeDecodeError):
                continue
    return files


# ---------------------------------------------------------------------- runner


def run_step(step: Step) -> Result:
    start = time.monotonic()
    proc = subprocess.Popen(
        step.cmd,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=step.timeout)
        status = "ok" if proc.returncode == 0 else "FAILED"
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)  # the step's own process group only
        out, _ = proc.communicate()
        status = "TIMEOUT"
        out = f"{out or ''}\nkilled after {step.timeout:g}s timeout\n"
    return Result(step, status, time.monotonic() - start, out or "")


def budget_from_env() -> int:
    return max(1, int(os.environ.get("ARGUS_CHECK_JOBS") or os.cpu_count() or 1))


def run_steps(steps: list[Step], budget: int) -> list[Result]:
    """Run steps concurrently while their weights fit in `budget`; print each on completion."""
    pending = sorted(steps, key=lambda s: -s.weight)
    results: list[Result] = []
    used = 0
    cond = threading.Condition()

    def work(step: Step) -> None:
        nonlocal used
        res = run_step(step)
        with cond:
            results.append(res)
            used -= min(step.weight, budget)
            print(
                f"\n=== {step.name}: {res.status} ({res.seconds:.1f}s) ===", flush=True
            )
            print(res.output.rstrip(), flush=True)
            cond.notify_all()

    threads: list[threading.Thread] = []
    with cond:
        while pending:
            idx = next(
                (
                    i
                    for i, s in enumerate(pending)
                    if used == 0 or used + min(s.weight, budget) <= budget
                ),
                None,
            )
            if idx is None:
                cond.wait()
                continue
            step = pending.pop(idx)
            used += min(step.weight, budget)
            t = threading.Thread(target=work, args=(step,))
            t.start()
            threads.append(t)
    for t in threads:
        t.join()
    return results


def passed_file() -> Path:
    return Path(ROOT / git("rev-parse", "--git-path", "argus-checked-changes"))


def main(argv: list[str]) -> int:
    plan_only = "--plan" in argv
    base = merge_base()
    cpus = budget_from_env()
    if base is None:
        print(
            "check-changed: merge base with origin/main not found; running the full check"
        )
        steps = [
            Step("make check", ("make", "check"), 3600, cpus),
            Step("make build", ("make", "build"), 900, 2),
        ]
        key = None
    else:
        changed = changed_files(base)
        if not changed:
            print(f"check-changed: no changes against {base[:12]}; nothing to check")
            return 0
        key = fingerprint(base, changed)
        files = read_graph_files()
        steps = plan(changed, sorted(files), Graph(files), cpus)
        print(f"check-changed: {len(changed)} changed files against {base[:12]}")
        if not steps:
            print("check-changed: no checks apply to these files")
    if plan_only:
        for s in steps:
            print(f"  {s.name}: {' '.join(s.cmd)[:200]}")
        return 0
    record = passed_file()
    if key and record.exists():
        for line in record.read_text().splitlines():
            if line.split("\t")[0] == key:
                print(
                    f"check-changed: these {len(changed)} files already passed: {line.split(chr(9))[1]}"
                )
                return 0
    results = run_steps(steps, cpus)
    print("\ncheck-changed summary:")
    for r in sorted(results, key=lambda r: r.step.name):
        print(f"  {r.status:8} {r.step.name} ({r.seconds:.1f}s)")
    failed = [r for r in results if r.status != "ok"]
    if failed:
        print(
            f"check-changed: FAILED -> {', '.join(r.step.name for r in failed)}",
            file=sys.stderr,
        )
        return 1
    if key:
        with record.open("a") as f:
            summary = ", ".join(s.name for s in steps) or "no checks apply"
            f.write(f"{key}\t{summary}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
