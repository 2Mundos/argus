#!/usr/bin/env python3
"""Send a task run's unwrapped final reply to the send_message nudge, not the wrap-nudge.

usage: patch-task-unwrapped-nudge.py /path/to/nanoclaw

Upstream's result door never delivers content on a task run: only an explicit send_message call
reaches a channel, and a `<message to="...">` block in the final text is deliberately dropped
(dispatchResultText, the `routing.taskRun` branch). An unwrapped final text therefore gets the
chat wrap-nudge, which tells the model to wrap its reply in `<message>` blocks — the very thing
that door refuses. The model wraps, the block is dropped, `hasUnwrapped` is now false so no
further nudge is pushed, and the run ends with the reply only in the log.

Measured on task 34cb5ce5 (Radar, PR #1370): the worker's final reply was a bare
`COMPLETE: {...}`. The session log shows `[scratchpad] COMPLETE: ...` and
`agent output had no <message to="..."> blocks - nothing was sent`; Argus never received the
completion and the task stayed claimed after the worker exited.

This patch routes a task run's unwrapped final text to a nudge that names send_message and quotes
the reply, so the model can resend it through the one door that works. Chat runs are untouched.
Idempotent: every hook is "ensure this exact text"; an anchor that is missing or ambiguous stops
the install (the upstream shape changed).
"""

import sys
from pathlib import Path

RUNNER = Path("container/agent-runner/src")

VERB = "task-unwrapped nudge"

HELPER = """/**
 * Argus: a task run's plain reply text is never delivered (only an explicit send_message call
 * reaches a channel), so pointing the model at `<message>` wrapping sends it into a door that
 * drops the block. Name the tool instead, and quote the reply it should resend.
 */
export function buildTaskUnwrappedNudge(text: string, destinationNames: string): string {
  const whole = text.trim();
  const preview = whole.slice(0, 600);
  const clipped = preview.length < whole.length ? `${preview}…` : preview;
  const to = (destinationNames.split(',')[0] ?? '').trim() || 'argus';
  return (
    '<system>Your final reply was not delivered: in a task run plain reply text only goes to the run log and is never sent anywhere. ' +
    'If it still needs to be sent, call send_message with an explicit to destination and put that text in the message — do not wrap it in <message> blocks, which this door drops. ' +
    `Your destinations: ${escapePromptXml(destinationNames)}.\\n` +
    `<undelivered_message to="${escapePromptXml(to)}">${escapePromptXml(clipped)}</undelivered_message></system>`
  );
}

"""

HELPER_ANCHOR = "export function buildTaskBlockNudge("

NUDGE_ANCHOR = """              sentThisTurn.length > 0
                ? buildInformedWrapNudge(sentThisTurn, undelivered, destinations)
"""

NUDGE_NEW = """              routing.taskRun
                ? buildTaskUnwrappedNudge(undelivered, names)
                : sentThisTurn.length > 0
                ? buildInformedWrapNudge(sentThisTurn, undelivered, destinations)
"""


def edit(root: Path, path: str, hooks: list[tuple[str, str, str]]) -> None:
    """hooks: (what, anchor, replacement). Skipped when the replacement is already present."""
    p = root / RUNNER / path
    s = p.read_text()
    original = s
    for what, anchor, new in hooks:
        if new in s:
            continue
        if s.count(anchor) != 1:
            raise SystemExit(f"{p}: {what}: upstream shape changed")
        s = s.replace(anchor, new, 1)
    if s != original:
        p.write_text(s)
        print(f"patched {p} ({VERB})")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} /path/to/nanoclaw", file=sys.stderr)
        return 2
    edit(
        Path(argv[1]),
        "poll-loop.ts",
        [
            ("helper", HELPER_ANCHOR, HELPER + HELPER_ANCHOR),
            ("nudge", NUDGE_ANCHOR, NUDGE_NEW),
        ],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
