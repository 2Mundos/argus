"""Binaries every worker needs from the agent image (engines spec 3). The probe
runs `command -v` in the real image; smoke.sh records the answer in the ledger
and the catalogue only reads it, so listing engines never starts a container."""

import json
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

REQUIRED = ("ssh", "rsync", "git")
_SCRIPT = f'for b in {" ".join(REQUIRED)}; do command -v "$b" >/dev/null 2>&1 || echo "$b"; done'
_ENGINE_IDS = ("codex", "claude", "opencode", "cursor-agent")

Run = Callable[..., subprocess.CompletedProcess[str]]


def probe(image: str, run: Run = subprocess.run) -> dict:
    """`{"ok", "detail"}`: false names what is missing; null means the docker
    call itself failed, which says nothing about the image."""
    cmd = ["docker", "run", "--rm", "--network", "none", "--entrypoint", "sh"]
    try:
        done = run(
            [*cmd, image, "-lc", _SCRIPT],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": None, "detail": f"docker unavailable: {type(e).__name__}"}
    if done.returncode != 0:
        return {"ok": None, "detail": f"docker run failed (exit {done.returncode})"}
    missing = [b for b in done.stdout.split() if b in REQUIRED]
    if missing:
        return {"ok": False, "detail": f"missing in image: {', '.join(missing)}"}
    return {"ok": True, "detail": f"{', '.join(REQUIRED)} present"}


def record(ledger: Path, image: str, run: Run = subprocess.run) -> dict:
    """Merge the probe into every engine's ledger row as `tools`."""
    fact = {**probe(image, run), "at": datetime.now(UTC).isoformat()}
    try:
        doc = json.loads(ledger.read_text())
    except (OSError, ValueError):
        doc = {}
    engines = doc.setdefault("engines", {})
    for eid in _ENGINE_IDS:
        engines.setdefault(eid, {})["tools"] = fact
    ledger.parent.mkdir(parents=True, exist_ok=True)
    tmp = ledger.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    tmp.replace(ledger)
    return fact


if __name__ == "__main__":
    out = record(Path(sys.argv[2]), sys.argv[1])
    print("tools".ljust(13), f"ok={out['ok']}", "|", out["detail"])
