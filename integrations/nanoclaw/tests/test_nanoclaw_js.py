"""NanoClaw redirect allowlist tests (node:test, local harness spec)."""

import subprocess
from pathlib import Path

NANOCLAW = Path(__file__).resolve().parents[1]


def test_anthropic_redirect_allowlist() -> None:
    subprocess.run(
        ["node", "--test", str(NANOCLAW / "engines/anthropic-redirect.test.mjs")],
        cwd=NANOCLAW / "engines",
        check=True,
        timeout=60,
    )
