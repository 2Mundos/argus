#!/usr/bin/env python3
"""Remove NanoClaw's blanket running-container ceiling (install.sh).

usage: patch-no-session-ceiling.py /path/to/nanoclaw

Upstream's src/reconcile-session.ts kills any running container whose heartbeat
(or spawn time, when it never wrote one) is older than max(30 min, declared Bash
timeout). That ended valid long-running workers. This removes the ceiling and its
exports; the per-claim stale recovery (a processing claim older than
max(60 s, declared Bash timeout) with no heartbeat since) is untouched. No other
global age or heartbeat cap replaces it.

Idempotent: a file without the ceiling is left alone. Fails closed (non-zero exit,
nothing written for that file) when an anchor is missing and the ceiling is still
present. The upstream decideStuckAction tests are replaced with ones asserting the
new behavior.
"""

import sys
from pathlib import Path

root = Path(sys.argv[1])
SRC = root / "src"


def patch(path, steps, done):
    """Apply (what, old, new) steps in order; `done(text)` says it is already patched."""
    p = SRC / path
    if not p.is_file():
        sys.exit(f"{p}: missing; NanoClaw layout changed upstream")
    s = p.read_text()
    if done(s):
        return
    for what, old, new in steps:
        assert s.count(old) == 1, (
            f"{path}: {what}: upstream shape changed ({old[:60]!r}…)"
        )
        s = s.replace(old, new, 1)
    p.write_text(s)
    print(f"patched {p} (no blanket session ceiling)")


# --- reconcile-session.ts ---------------------------------------------------

DOC_OLD = """ *   If the container IS running:
 *     1. Absolute ceiling: heartbeat age > max(30 min, current_bash_timeout)
 *        → kill. Covers the "alive but silent for 30 min" case. Extended
 *        only while Bash is declared as running longer, honouring the
 *        user's own timeout directive. Kill then resets processing rows.
 *        When no heartbeat file exists yet, falls back to the tracked
 *        container spawn time so a container that goes idle without ever
 *        reaching an SDK event —
 *        and so never writes a heartbeat — still ages out instead of
 *        living forever (see decideStuckAction's grace-period comment).
 *
 *     2. Message-scoped stuck: for each 'processing' row, tolerance =
"""
DOC_NEW = """ *   If the container IS running there is no session-age or heartbeat-age
 *   ceiling: a long-lived or idle session is valid and is never killed for
 *   its age alone. Only a stale message claim kills:
 *
 *     Message-scoped stuck: for each 'processing' row, tolerance =
"""

CONST_OLD = """// Absolute idle ceiling for a running container. If the heartbeat file hasn't
// been touched in this long, the container is either stuck or doing genuinely
// nothing — kill and restart on the next inbound.
export const ABSOLUTE_CEILING_MS = 30 * 60 * 1000;
"""

UNION_OLD = (
    "  | { action: 'kill-ceiling'; heartbeatAgeMs: number; ceilingMs: number }\n"
)

ARGS_OLD = "  containerStartedAtMs?: number; // fallback when heartbeat file absent\n"
DESTRUCTURE_OLD = "const { now, heartbeatMtimeMs, containerStartedAtMs, containerState, claims } = args;"
DESTRUCTURE_NEW = "const { now, heartbeatMtimeMs, containerState, claims } = args;"

BLOCK_START = "  // Ceiling check prefers the heartbeat file's mtime."
BLOCK_END = "\n  const tolerance = Math.max(CLAIM_STUCK_MS, declaredBashMs ?? 0);"

IMPORT_OLD = "import { getContainerStartedAtMs, isContainerRunning, killContainer } from './container-runner.js';"
IMPORT_NEW = (
    "import { isContainerRunning, killContainer } from './container-runner.js';"
)

CALL_OLD = "    containerStartedAtMs: getContainerStartedAtMs(session.id),\n"

KILL_OLD = """  if (decision.action === 'kill-ceiling') {
    log.warn('Killing container past absolute ceiling', {
      sessionId: session.id,
      heartbeatAgeMs: decision.heartbeatAgeMs,
      ceilingMs: decision.ceilingMs,
    });
    killContainer(session.id, 'absolute-ceiling');
    resetStuckProcessingRows(inDb, outDb, session, 'absolute-ceiling');
    return;
  }

"""

p = SRC / "reconcile-session.ts"
if not p.is_file():
    sys.exit(f"{p}: missing; NanoClaw layout changed upstream")
text = p.read_text()
if "ABSOLUTE_CEILING_MS" in text or "kill-ceiling" in text:
    for what, old in (
        ("header comment", DOC_OLD),
        ("ceiling constant", CONST_OLD),
        ("kill-ceiling decision", UNION_OLD),
        ("containerStartedAtMs arg", ARGS_OLD),
        ("args destructure", DESTRUCTURE_OLD),
        ("container-runner import", IMPORT_OLD),
        ("containerStartedAtMs caller", CALL_OLD),
        ("kill-ceiling branch", KILL_OLD),
    ):
        assert text.count(old) == 1, (
            f"reconcile-session.ts: {what}: upstream shape changed ({old[:60]!r}…)"
        )
    a, b = text.find(BLOCK_START), text.find(BLOCK_END)
    assert a != -1 and 0 < a < b and text.count(BLOCK_START) == 1, (
        "reconcile-session.ts: ceiling check block: upstream shape changed"
    )
    text = text[:a].rstrip("\n") + "\n" + text[b:]
    text = text.replace(DOC_OLD, DOC_NEW, 1)
    for old in (CONST_OLD, UNION_OLD, ARGS_OLD, CALL_OLD, KILL_OLD):
        text = text.replace(old, "", 1)
    text = text.replace(DESTRUCTURE_OLD, DESTRUCTURE_NEW, 1)
    text = text.replace(IMPORT_OLD, IMPORT_NEW, 1)
    assert (
        "ABSOLUTE_CEILING_MS" not in text
        and "kill-ceiling" not in text
        and "ceilingMs" not in text
    ), "reconcile-session.ts: ceiling remnants after patch"
    p.write_text(text)
    print(f"patched {p} (no blanket session ceiling)")

# --- re-exports and upstream test doubles -----------------------------------

patch(
    "host-sweep.ts",
    [("re-export", "  ABSOLUTE_CEILING_MS,\n", "")],
    lambda s: "ABSOLUTE_CEILING_MS" not in s,
)
for name in ("host-sweep.queue.test.ts", "host-sweep.feeds.test.ts"):
    patch(
        name,
        [("reconcile-session mock", "  ABSOLUTE_CEILING_MS: 0,\n", "")],
        lambda s: "ABSOLUTE_CEILING_MS" not in s,
    )

# --- decideStuckAction tests ------------------------------------------------

TEST_START = "  it('returns ok when heartbeat is within the absolute ceiling', () => {"
TEST_END = "  it('returns kill-claim when a claim is past 60s and heartbeat has not moved', () => {"

TESTS_NEW = """  it('never kills a quiet session for its age alone', () => {
    // Regression: a blanket 30-minute heartbeat ceiling ended valid long-running workers.
    const longMs = 6 * 60 * 60 * 1000;
    for (const heartbeatMtimeMs of [BASE - longMs, BASE - 31 * 60 * 1000, 0]) {
      expect(decideStuckAction({ now: BASE, heartbeatMtimeMs, containerState: null, claims: [] })).toEqual({
        action: 'ok',
      });
    }
  });

  it('does not kill an idle session whose heartbeat is missing and spawn time is old', () => {
    const res = decideStuckAction({
      now: BASE,
      heartbeatMtimeMs: 0,
      containerStartedAtMs: BASE - 45 * 60 * 1000,
      containerState: null,
      claims: [],
    } as Parameters<typeof decideStuckAction>[0]);
    expect(res).toEqual({ action: 'ok' });
  });

  it('has no ceiling decision or export left', async () => {
    const mod = await import('./reconcile-session.js');
    expect('ABSOLUTE_CEILING_MS' in mod).toBe(false);
  });

  it('kills a stale claim on a session older than 30 minutes', () => {
    const res = decideStuckAction({
      now: BASE,
      heartbeatMtimeMs: BASE - 45 * 60 * 1000,
      containerState: null,
      claims: [claim('msg-1', CLAIM_STUCK_MS + 5_000)],
    });
    expect(res.action).toBe('kill-claim');
  });

  it('kills on claim-stuck when heartbeat is absent AND a claim has aged past tolerance', () => {
    // Hanging fresh container: spawned, picked up a message (claim recorded
    // in processing_ack), but never wrote a heartbeat.
    const res = decideStuckAction({
      now: BASE,
      heartbeatMtimeMs: 0,
      containerState: null,
      claims: [claim('msg-1', CLAIM_STUCK_MS + 5_000)],
    });
    expect(res.action).toBe('kill-claim');
  });

  it('a Bash declaration widens only that claim tolerance, not any session-wide limit', () => {
    const twoHrMs = 2 * 60 * 60 * 1000;
    const bash = {
      currentTool: 'Bash',
      toolDeclaredTimeoutMs: twoHrMs,
      toolStartedAt: parseIsoTimestamp(new Date(BASE - 45 * 60 * 1000).toISOString()),
    };
    const staleHeartbeat = BASE - 3 * 60 * 60 * 1000;
    const within = decideStuckAction({
      now: BASE,
      heartbeatMtimeMs: staleHeartbeat,
      containerState: bash,
      claims: [claim('msg-1', 45 * 60 * 1000)],
    });
    expect(within.action).toBe('ok');
    const beyond = decideStuckAction({
      now: BASE,
      heartbeatMtimeMs: staleHeartbeat,
      containerState: bash,
      claims: [claim('msg-1', twoHrMs + 1_000)],
    });
    expect(beyond.action).toBe('kill-claim');
    if (beyond.action !== 'kill-claim') return;
    expect(beyond.toleranceMs).toBe(twoHrMs);
  });

"""

TEST_P = SRC / "host-sweep.test.ts"
if not TEST_P.is_file():
    sys.exit(f"{TEST_P}: missing; NanoClaw layout changed upstream")
t = TEST_P.read_text()
if "  ABSOLUTE_CEILING_MS,\n" in t:
    a, b = t.find(TEST_START), t.find(TEST_END)
    assert a != -1 and a < b and t.count(TEST_START) == 1 and t.count(TEST_END) == 1, (
        "host-sweep.test.ts: decideStuckAction tests: upstream shape changed"
    )
    for what, old, new in (
        ("ceiling import", "  ABSOLUTE_CEILING_MS,\n", ""),
        (
            "ceiling constants",
            "const JUST_WITHIN_CEILING_MS = ABSOLUTE_CEILING_MS - 1;\nconst JUST_OVER_CEILING_MS = ABSOLUTE_CEILING_MS + 1;\n",
            "",
        ),
    ):
        assert t.count(old) == 1, f"host-sweep.test.ts: {what}: upstream shape changed"
        t = t.replace(old, new, 1)
    a, b = t.find(TEST_START), t.find(TEST_END)
    t = t[:a] + TESTS_NEW + t[b:]
    assert "JUST_OVER_CEILING_MS" not in t and "  ABSOLUTE_CEILING_MS,\n" not in t, (
        "host-sweep.test.ts: ceiling remnants"
    )
    TEST_P.write_text(t)
    print(f"patched {TEST_P} (decideStuckAction: no blanket session ceiling)")
