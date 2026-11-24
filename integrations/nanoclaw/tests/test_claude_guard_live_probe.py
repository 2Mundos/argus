"""claude-guard-live-probe.sh: the parts that run without Docker or credentials.

The live turn needs Claude Code in an image (run by the orchestrator after merge). Here the
probe runs with stub `node` and `claude` on PATH, so the scripted marker commands and the
argv it hands the CLI are checked for real; the verdict program is taken out of the probe and
run on fake transcripts (docs/specs/command-guard.spec.md section 6).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROBE = Path(__file__).resolve().parents[1] / "scripts" / "claude-guard-live-probe.sh"
REASON = "--no-preserve-root disables the safety check of rm. Do not retry or work around this block."
BLOCK = f"Blocked by Argus command guard: {REASON}"
WRAPPED = f"<tool_use_error>PreToolUse:Bash hook error: {BLOCK}</tool_use_error>"
VERIFIER = re.search(r"<<'PY'\n(.*?)\nPY\n", PROBE.read_text(), re.S).group(1)

# Stands in for fake-anthropic.mjs: runs the two scripted commands in the marker
# directory (the probe creates it before starting the API), records each command's exit
# status and the marker it writes, then prints a port for the probe.
NODE_STUB = """#!/usr/bin/env python3
import json, os, shlex, subprocess
env = dict(os.environ, PATH=os.environ["FAKE_RM_DIR"] + os.pathsep + os.environ["PATH"])
results = []
for cmd in json.loads(os.environ["FAKE_ANTHROPIC_BASH"]):
    exit_code = subprocess.run(["sh", "-c", cmd], env=env).returncode
    marker = shlex.split(cmd)[-1]
    content = open(marker).read() if os.path.exists(marker) else None
    results.append({"exit": exit_code, "marker": marker, "content": content})
with open(os.environ["FAKE_RESULT"], "w") as f:
    json.dump(results, f)
print(4567)
"""

# Stands in for `claude`: records its argv, one argument per line, and creates the debug file.
# The probe runs it under `env -i`, so the record path is written into the script (@ARGV@).
CLAUDE_STUB = """#!/bin/sh
prev=""
for a in "$@"; do
  if [ "$prev" = --debug-file ]; then : >"$a"; fi
  prev="$a"
done
printf '%s\\n' "$@" >"@ARGV@"
exit 0
"""

# Stands in for `rm`: prints its arguments, so the denied command's output is recorded.
RM_STUB = """#!/bin/sh
printf '%s\\n' "$@"
"""


def _executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def test_scripted_marker_paths_with_spaces_and_glob_characters_work(
    tmp_path: Path,
) -> None:
    base = tmp_path / "w d [1]*"  # the probe's work dir lands here via TMPDIR
    base.mkdir()
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    rm_dir = tmp_path / "fake-rm"
    rm_dir.mkdir()
    _executable(stubs / "node", NODE_STUB)
    argv_file = tmp_path / "argv.txt"
    _executable(stubs / "claude", CLAUDE_STUB.replace("@ARGV@", str(argv_file)))
    _executable(rm_dir / "rm", RM_STUB)
    result_file = tmp_path / "result.json"
    env = dict(
        os.environ,
        TMPDIR=str(base),
        PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}",
        FAKE_RM_DIR=str(rm_dir),
        FAKE_RESULT=str(result_file),
    )

    subprocess.run(
        ["sh", str(PROBE)], env=env, capture_output=True, text=True, timeout=120
    )

    benign, denied = json.loads(result_file.read_text())
    assert benign["exit"] == 0 and denied["exit"] == 0
    assert benign["marker"].startswith(str(base))
    assert benign["content"] == "argus-guard-benign\n"
    assert denied["marker"].startswith(str(base))
    assert denied["content"] == "--no-preserve-root\n--version\n"
    # The CLI's argv reaches it intact too: no word split or glob on the path.
    argv = argv_file.read_text().splitlines()
    assert argv[argv.index("--debug-file") + 1].startswith(str(base))


def _verify(
    tmp_path: Path, transcript: list[dict], code: int = 0
) -> subprocess.CompletedProcess[str]:
    work = Path(tempfile.mkdtemp(dir=tmp_path))
    (work / "ws").mkdir()
    (work / "out.jsonl").write_text("".join(json.dumps(e) + "\n" for e in transcript))
    (work / "debug.log").write_text("Watching for changes in setting files /etc/x\n")
    (work / "ws" / "benign.marker").write_text("argus-guard-benign\n")
    verifier = tmp_path / "verify.py"
    verifier.write_text(VERIFIER)
    return subprocess.run(
        [sys.executable, "-I", str(verifier), str(work), str(code)],
        capture_output=True,
        text=True,
        check=False,
    )


def _transcript(denial_content: object, is_error: bool = True) -> list[dict]:
    deny = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": BLOCK,
        }
    }
    hook = {"type": "system", "hook_name": "PreToolUse:Bash"}
    return [
        {"type": "system", "subtype": "init", "claude_code_version": "2.1.296"},
        {**hook, "subtype": "hook_started", "hook_event": "PreToolUse"},
        {**hook, "subtype": "hook_response", "exit_code": 0, "output": "{}"},
        {**hook, "subtype": "hook_started", "hook_event": "PreToolUse"},
        {
            **hook,
            "subtype": "hook_response",
            "exit_code": 2,
            "output": json.dumps(deny),
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "is_error": False,
                        "content": "",
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_2",
                        "is_error": is_error,
                        "content": denial_content,
                    }
                ]
            },
        },
    ]


def test_verdict_passes_the_hook_error_wrapper(tmp_path: Path) -> None:
    for content in (WRAPPED, [{"type": "text", "text": WRAPPED}]):
        out = _verify(tmp_path, _transcript(content))
        assert out.returncode == 0, out.stdout
        assert "PASS is_error_tool_result" in out.stdout
        assert "FAIL" not in out.stdout


def test_verdict_rejects_a_response_without_the_wrapper(tmp_path: Path) -> None:
    unrelated = [
        BLOCK,  # the guard's words, bare
        f"<tool_use_error>error: {BLOCK}</tool_use_error>",  # another wrapper
        f"Explanation: {WRAPPED}",  # the wrapper is not the whole text
    ]
    for content in unrelated:
        out = _verify(tmp_path, _transcript(content))
        assert out.returncode == 1, content
        assert "FAIL is_error_tool_result" in out.stdout, content


def test_verdict_rejects_a_wrapped_result_that_is_not_an_error(tmp_path: Path) -> None:
    out = _verify(tmp_path, _transcript(WRAPPED, is_error=False))
    assert out.returncode == 1
    assert "FAIL is_error_tool_result" in out.stdout
