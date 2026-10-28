#!/usr/bin/env python3
"""Publish when NanoClaw's startup adoption has finished (install.sh).

usage: patch-startup-adoption-gate.py /path/to/nanoclaw

index.ts starts the channel adapters (the Argus claim loop) before it calls
adoptRunningSessions(), which stops every listed session that has no database row. A
repository preflight session has none, so one started during that window would be
stopped as an orphan. This wraps adoptRunningSessions() and exports `startupAdoptionDone`,
a promise that resolves only after adoption succeeds and rejects when it throws, so the
Argus channel claims nothing and a preflight creates nothing before it settles. The upstream
function is renamed; its fail-soft `listSessions` catch is changed to rethrow so listing
errors reject the gate instead of opening it.

Idempotent; fails closed (non-zero) when the anchor is missing and the gate is absent.
"""

import re
import sys
from pathlib import Path

p = Path(sys.argv[1]) / "src" / "container-runner.ts"
if not p.is_file():
    sys.exit(f"{p}: missing; NanoClaw layout changed upstream")
s = p.read_text()

GATE_DECL = """let resolveStartupAdoption!: () => void;
let rejectStartupAdoption!: (reason?: unknown) => void;
/** Argus: resolves after successful startup adoption, rejects when it throws (patch-startup-adoption-gate.py). */
export const startupAdoptionDone: Promise<void> = new Promise<void>((resolve, reject) => {
  resolveStartupAdoption = resolve;
  rejectStartupAdoption = reject;
});

"""

ADOPT_WRAPPER = """export async function adoptRunningSessions(): Promise<{ adopted: number; stopped: number }> {
  try {
    const result = await adoptRunningSessionsOnce();
    resolveStartupAdoption();
    return result;
  } catch (error) {
    rejectStartupAdoption(error);
    throw error;
  }
}

"""

ADOPT_ONCE_HEAD = (
    "async function adoptRunningSessionsOnce():"
    " Promise<{ adopted: number; stopped: number }> {\n"
)

LIST_FAIL_SOFT = """  } catch (err) {
    log.warn('Failed to list existing sessions for adoption', { err });
    return { adopted: 0, stopped: 0 };
  }
"""

LIST_FAIL_CLOSED = """  } catch (err) {
    log.warn('Failed to list existing sessions for adoption', { err });
    throw err;
  }
"""

ADOPT_BAD_FINALLY = """export async function adoptRunningSessions(): Promise<{ adopted: number; stopped: number }> {
  try {
    return await adoptRunningSessionsOnce();
  } finally {
    resolveStartupAdoption();
  }
}
"""

GATE_DECL_RESOLVE_ONLY = """let resolveStartupAdoption!: () => void;
/** Argus: settles once startup adoption has returned or thrown (patch-startup-adoption-gate.py). */
export const startupAdoptionDone: Promise<void> = new Promise<void>((resolve) => {
  resolveStartupAdoption = resolve;
});

"""

GATE_DECL_RESOLVE_ONLY_ALT = """let resolveStartupAdoption!: () => void;
export const startupAdoptionDone: Promise<void> = new Promise<void>((resolve) => {
  resolveStartupAdoption = resolve;
});

"""


def adoption_listing_fail_closed(s: str) -> bool:
    if "Failed to list existing sessions for adoption" not in s:
        return True
    if LIST_FAIL_SOFT in s:
        return False
    return LIST_FAIL_CLOSED in s


def ensure_adoption_listing_fail_closed(s: str) -> str:
    if LIST_FAIL_CLOSED in s:
        return s
    if LIST_FAIL_SOFT in s:
        return s.replace(LIST_FAIL_SOFT, LIST_FAIL_CLOSED, 1)
    if "Failed to list existing sessions for adoption" in s:
        sys.exit(
            "container-runner.ts: adoptRunningSessions listSessions catch: upstream shape changed"
        )
    return s


def canonical_gate(s: str) -> bool:
    return (
        "let rejectStartupAdoption!: (reason?: unknown) => void;" in s
        and "export const startupAdoptionDone: Promise<void> = new Promise<void>((resolve, reject) => {"
        in s
        and s.count(ADOPT_ONCE_HEAD) == 1
        and ADOPT_WRAPPER in s
        and "finally {\n    resolveStartupAdoption();" not in s
        and "rejectStartupAdoption(error)" in s
        and adoption_listing_fail_closed(s)
    )


def repair_duplicate_once_header(s: str) -> str:
    dup = ADOPT_ONCE_HEAD + "\n" + ADOPT_ONCE_HEAD
    while dup in s:
        s = s.replace(dup, ADOPT_ONCE_HEAD, 1)
    once_pat = re.compile(
        r"(async function adoptRunningSessionsOnce\(\):"
        r" Promise<\{ adopted: number; stopped: number \}> \{\n)"
        r"(?:\s*\n)+"
        r"\1"
    )
    while once_pat.search(s):
        s = once_pat.sub(r"\1", s, count=1)
    return s


def upgrade_gate_declaration(s: str) -> str:
    for old in (GATE_DECL_RESOLVE_ONLY, GATE_DECL_RESOLVE_ONLY_ALT):
        if old in s:
            return s.replace(old, GATE_DECL, 1)
    if (
        "export const startupAdoptionDone" in s
        and "let rejectStartupAdoption!" not in s
        and "new Promise<void>((resolve) => {" in s
    ):
        s = s.replace(
            "export const startupAdoptionDone: Promise<void> = new Promise<void>((resolve) => {\n"
            "  resolveStartupAdoption = resolve;\n"
            "});",
            "export const startupAdoptionDone: Promise<void> = new Promise<void>((resolve, reject) => {\n"
            "  resolveStartupAdoption = resolve;\n"
            "  rejectStartupAdoption = reject;\n"
            "});",
            1,
        )
        s = s.replace(
            "let resolveStartupAdoption!: () => void;\n",
            "let resolveStartupAdoption!: () => void;\n"
            "let rejectStartupAdoption!: (reason?: unknown) => void;\n",
            1,
        )
    return s


if canonical_gate(s):
    sys.exit(0)

changed = False

if "startupAdoptionDone" in s:
    s = repair_duplicate_once_header(s)
    if ADOPT_BAD_FINALLY in s:
        s = s.replace(ADOPT_BAD_FINALLY, ADOPT_WRAPPER, 1)
        changed = True
    upgraded = upgrade_gate_declaration(s)
    if upgraded != s:
        s = upgraded
        changed = True
    s = repair_duplicate_once_header(s)
    listed = ensure_adoption_listing_fail_closed(s)
    if listed != s:
        s = listed
        changed = True
    if canonical_gate(s):
        if changed:
            p.write_text(s)
            print(f"upgraded {p} (startupAdoptionDone rejects on failure)")
        sys.exit(0)
    sys.exit("container-runner.ts: startupAdoptionDone: installed shape unrecognized")

OLD = "export async function adoptRunningSessions(): Promise<{ adopted: number; stopped: number }> {\n"
if s.count(OLD) != 1:
    sys.exit("container-runner.ts: adoptRunningSessions: upstream shape changed")
s = s.replace(OLD, GATE_DECL + ADOPT_WRAPPER + ADOPT_ONCE_HEAD, 1)
s = ensure_adoption_listing_fail_closed(s)
if not canonical_gate(s):
    sys.exit(
        "container-runner.ts: startupAdoptionDone: patch did not produce canonical gate"
    )
p.write_text(s)
print(f"patched {p} (startupAdoptionDone gate)")
