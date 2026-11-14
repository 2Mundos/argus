"""NanoClaw redirect allowlist tests (node:test, local harness spec)."""

import os
import subprocess
from pathlib import Path

NANOCLAW = Path(__file__).resolve().parents[1]


def _loopback_env() -> dict[str, str]:
    """Keep loopback out of any proxy the worker environment injects."""
    env = dict(os.environ)
    for name in ("NO_PROXY", "no_proxy"):
        entries = [e for e in env.get(name, "").split(",") if e]
        env[name] = ",".join([*entries, "127.0.0.1", "localhost"])
    return env


def test_anthropic_redirect_allowlist() -> None:
    subprocess.run(
        ["node", "--test", str(NANOCLAW / "engines/anthropic-redirect.test.mjs")],
        cwd=NANOCLAW / "engines",
        check=True,
        env=_loopback_env(),
        timeout=60,
    )


def test_anthropic_openai_bridge() -> None:
    subprocess.run(
        ["node", "--test", str(NANOCLAW / "engines/anthropic-openai-bridge.test.mjs")],
        cwd=NANOCLAW / "engines",
        check=True,
        env=_loopback_env(),
        timeout=60,
    )
