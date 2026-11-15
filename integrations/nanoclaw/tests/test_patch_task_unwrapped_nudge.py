"""patch-task-unwrapped-nudge.py against an excerpt of NanoClaw's agent runner.

The worker's reply text on a task run is dropped by the result door, so an unwrapped final reply
must be turned into a send_message instruction instead of the chat wrap-nudge. The fixture below
carries the two upstream spans the patch hooks, copied verbatim from the pinned runner.
"""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "patch-task-unwrapped-nudge.py"

POLL_LOOP_TS = """          if (willRetryWrapping) {
            unwrappedNudged = true;
            // Everything that went out this turn: tool sends, door sends and
            // reactions alike, so a reaction-only turn can also answer done.
            const sentThisTurn = chatRowsInWindow(turnStartSeq);
            const destinations = getAllDestinations();
            const names = destinations.map((d) => d.name).join(', ');
            // A tool send can't be told apart from an "on it" ack, so after
            // one the model sees what went out and decides; never a silent drop.
            pushRetry(
              sentThisTurn.length > 0
                ? buildInformedWrapNudge(sentThisTurn, undelivered, destinations)
                : `<system>Your response was not delivered — it was not wrapped in <message to="name">...</message> blocks. ` +
                    `All output must be wrapped.</system>`,
              { after: turnStartSeq, upto: maxOutboundSeq() },
            );
          }
          if (willRetryTaskBlocks) {
            taskBlockNudged = true;
          }
}

export function buildTaskBlockNudge(taskBlocks: TaskMessageBlock[], destinationNames: string): string {
  return '<system>not delivered</system>';
}

function escapePromptXml(value: string): string {
  return value;
}
"""


def runner(root: Path, poll_loop: str = POLL_LOOP_TS) -> Path:
    src = root / "container" / "agent-runner" / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "poll-loop.ts").write_text(poll_loop)
    return src


def patch(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_routes_a_task_run_to_the_send_message_nudge(tmp_path: Path) -> None:
    src = runner(tmp_path)
    done = patch(tmp_path)
    assert done.returncode == 0, done.stderr
    s = (src / "poll-loop.ts").read_text()

    # The task run branches before the chat nudge decision.
    assert (
        "routing.taskRun\n                ? buildTaskUnwrappedNudge(undelivered, names)"
        in s
    )
    # The chat nudge survives untouched for chat runs.
    assert "buildInformedWrapNudge(sentThisTurn, undelivered, destinations)" in s
    # The helper exists once and names the tool, not the block wrapping.
    assert s.count("export function buildTaskUnwrappedNudge(") == 1
    assert "call send_message with an explicit to destination" in s
    assert "do not wrap it in <message> blocks, which this door drops" in s
    # The failed path is not involved.
    assert s.count("if (willRetryWrapping) {") == 1


def test_is_idempotent(tmp_path: Path) -> None:
    src = runner(tmp_path)
    assert patch(tmp_path).returncode == 0
    once = (src / "poll-loop.ts").read_text()
    again = patch(tmp_path)
    assert again.returncode == 0, again.stderr
    assert (src / "poll-loop.ts").read_text() == once
    assert again.stdout == ""


def test_stops_when_the_anchor_moved(tmp_path: Path) -> None:
    # A second copy of the helper anchor makes the target ambiguous.
    doubled = POLL_LOOP_TS.replace(
        "export function buildTaskBlockNudge(",
        "export function buildTaskBlockNudge(\nexport function buildTaskBlockNudge(",
    )
    src = runner(tmp_path, doubled)
    before = (src / "poll-loop.ts").read_text()
    stopped = patch(tmp_path)
    assert stopped.returncode != 0
    assert "upstream shape changed" in stopped.stderr
    assert (src / "poll-loop.ts").read_text() == before


def test_usage_error_without_a_target(tmp_path: Path) -> None:
    missing = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode == 2
    assert "usage:" in missing.stderr
