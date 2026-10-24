"""patch-no-session-ceiling.py against excerpts of NanoClaw's reconcile-session.ts
(pristine 8fbacc91, hooked spans verbatim). The patched suite (host-sweep*.test.ts)
runs in the NanoClaw checkout after install.sh."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "patch-no-session-ceiling.py"

RECONCILE_TS = """ *   If the container IS running:
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
 *        max(60s, current_bash_timeout_ms_if_Bash_running).
 */
import { getContainerStartedAtMs, isContainerRunning, killContainer } from './container-runner.js';
import type { Session } from './types.js';

// Absolute idle ceiling for a running container. If the heartbeat file hasn't
// been touched in this long, the container is either stuck or doing genuinely
// nothing — kill and restart on the next inbound.
export const ABSOLUTE_CEILING_MS = 30 * 60 * 1000;
// Stuck tolerance window applied per 'processing' claim — "did we see any
// signs of life since this message was claimed?"
export const CLAIM_STUCK_MS = 60 * 1000;

export type StuckDecision =
  | { action: 'ok' }
  | { action: 'kill-ceiling'; heartbeatAgeMs: number; ceilingMs: number }
  | { action: 'kill-claim'; messageId: string; claimAgeMs: number; toleranceMs: number };

export function decideStuckAction(args: {
  now: number;
  heartbeatMtimeMs: number; // 0 when heartbeat file absent
  containerStartedAtMs?: number; // fallback when heartbeat file absent
  containerState: ContainerState | null;
  claims: Array<{ messageId: string; statusChanged: string }>;
}): StuckDecision {
  const { now, heartbeatMtimeMs, containerStartedAtMs, containerState, claims } = args;
  const declaredBashMs = bashTimeoutMs(containerState);

  // Ceiling check prefers the heartbeat file's mtime. A freshly-spawned
  // container hasn't had any SDK activity yet.
  const effectiveHeartbeatMs = heartbeatMtimeMs !== 0 ? heartbeatMtimeMs : (containerStartedAtMs ?? 0);
  if (effectiveHeartbeatMs !== 0) {
    const heartbeatAge = now - effectiveHeartbeatMs;
    const ceiling = Math.max(ABSOLUTE_CEILING_MS, declaredBashMs ?? 0);
    if (heartbeatAge > ceiling) {
      return { action: 'kill-ceiling', heartbeatAgeMs: heartbeatAge, ceilingMs: ceiling };
    }
  }

  const tolerance = Math.max(CLAIM_STUCK_MS, declaredBashMs ?? 0);
  return { action: 'ok' };
}

  const decision = decideStuckAction({
    now: Date.now(),
    heartbeatMtimeMs: gatedHeartbeatMs,
    containerStartedAtMs: getContainerStartedAtMs(session.id),
    containerState: outDb.getContainerState(),
    claims: gatedClaims,
  });

  if (decision.action === 'ok') return;

  if (decision.action === 'kill-ceiling') {
    log.warn('Killing container past absolute ceiling', {
      sessionId: session.id,
      heartbeatAgeMs: decision.heartbeatAgeMs,
      ceilingMs: decision.ceilingMs,
    });
    killContainer(session.id, 'absolute-ceiling');
    resetStuckProcessingRows(inDb, outDb, session, 'absolute-ceiling');
    return;
  }

  log.warn('Killing container — message claimed then silent', {});
"""

SWEEP_TS = "export {\n  ABSOLUTE_CEILING_MS,\n  CLAIM_STUCK_MS,\n} from './reconcile-session.js';\n"
MOCK_TS = "vi.mock('./reconcile-session.js', () => ({\n  ABSOLUTE_CEILING_MS: 0,\n  CLAIM_STUCK_MS: 0,\n}));\n"
SWEEP_TEST_TS = """import {
  ABSOLUTE_CEILING_MS,
  CLAIM_STUCK_MS,
} from './host-sweep.js';
const JUST_WITHIN_CEILING_MS = ABSOLUTE_CEILING_MS - 1;
const JUST_OVER_CEILING_MS = ABSOLUTE_CEILING_MS + 1;
describe('decideStuckAction', () => {
  it('returns ok when heartbeat is within the absolute ceiling', () => {
    expect(JUST_WITHIN_CEILING_MS).toBe(1);
  });

  it('returns kill-claim when a claim is past 60s and heartbeat has not moved', () => {
    expect(1).toBe(1);
  });
});
"""

FILES = {
    "reconcile-session.ts": RECONCILE_TS,
    "host-sweep.ts": SWEEP_TS,
    "host-sweep.queue.test.ts": MOCK_TS,
    "host-sweep.feeds.test.ts": MOCK_TS,
    "host-sweep.test.ts": SWEEP_TEST_TS,
}


def checkout(root: Path, overrides: dict[str, str] | None = None) -> Path:
    src = root / "src"
    src.mkdir(parents=True)
    for name, text in (FILES | (overrides or {})).items():
        (src / name).write_text(text)
    return src


def patch(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_removes_the_ceiling_once_and_keeps_claim_recovery(tmp_path: Path) -> None:
    src = checkout(tmp_path)
    first = patch(tmp_path)
    assert first.returncode == 0, first.stderr
    assert first.stdout.count("patched") == 5
    for name in FILES:
        text = (src / name).read_text()
        assert "kill-ceiling" not in text
        assert "  ABSOLUTE_CEILING_MS" not in text
        assert "const ABSOLUTE_CEILING_MS" not in text
    rec = (src / "reconcile-session.ts").read_text()
    assert "containerStartedAtMs" not in rec
    assert "getContainerStartedAtMs" not in rec
    assert "export const CLAIM_STUCK_MS = 60 * 1000;" in rec
    assert "const tolerance = Math.max(CLAIM_STUCK_MS, declaredBashMs ?? 0);" in rec
    assert "kill-claim" in rec
    assert "Killing container — message claimed then silent" in rec
    tests = (src / "host-sweep.test.ts").read_text()
    assert "never kills a quiet session for its age alone" in tests
    assert "a Bash declaration widens only that claim tolerance" in tests
    assert "kills a stale claim on a session older than 30 minutes" in tests
    assert "returns kill-claim when a claim is past 60s" in tests

    before = {p: p.read_text() for p in src.iterdir()}
    again = patch(tmp_path)
    assert (again.returncode, again.stdout) == (0, "")
    assert {p: p.read_text() for p in src.iterdir()} == before


@pytest.mark.parametrize(
    ("name", "old"),
    [
        ("reconcile-session.ts", "export const ABSOLUTE_CEILING_MS = 30 * 60 * 1000;"),
        ("reconcile-session.ts", "  if (decision.action === 'kill-ceiling') {"),
        (
            "reconcile-session.ts",
            "  // Ceiling check prefers the heartbeat file's mtime.",
        ),
        ("host-sweep.ts", "  ABSOLUTE_CEILING_MS,\n"),
        ("host-sweep.test.ts", "const JUST_OVER_CEILING_MS = ABSOLUTE_CEILING_MS + 1;"),
    ],
)
def test_upstream_drift_fails_closed(tmp_path: Path, name: str, old: str) -> None:
    # Keep the constant's name so the file still looks unpatched.
    new = (
        "  ABSOLUTE_CEILING_MS as CEILING,\n"
        if name == "host-sweep.ts"
        else "// drifted"
    )
    drifted = FILES[name].replace(old, new, 1)
    assert drifted != FILES[name]
    src = checkout(tmp_path, {name: drifted})
    result = patch(tmp_path)
    assert result.returncode != 0
    assert "AssertionError" in result.stderr
    assert (src / name).read_text() == drifted


def test_missing_source_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    result = patch(tmp_path)
    assert result.returncode != 0
    assert "layout changed upstream" in result.stderr
