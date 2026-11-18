"""Tools-layer directory permissions and uid-1000 execution (drw-r--r-- regression)."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

ENGINES = Path(__file__).resolve().parents[1] / "engines"
DOCKERFILE = ENGINES / "Dockerfile.tools-layer"
ROOT = Path(__file__).resolve().parents[3]


def _docker_daemon() -> bool:
    exe = shutil.which("docker")
    if not exe:
        return False
    try:
        return (
            subprocess.run([exe, "info"], capture_output=True, timeout=8).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def test_dockerfile_makes_argus_dirs_traversable() -> None:
    text = DOCKERFILE.read_text()
    assert "chmod 755 /etc/claude-code /usr/local/lib/argus" in text
    assert "/usr/local/lib/argus/bin/git" in text
    assert "/usr/local/lib/argus/git-hooks/commit-msg" in text


WORKER_TOOLS = ("rg", "jq", "wget", "patch")
PROBE = ENGINES / "probe-worker-tools.sh"


def test_dockerfile_installs_worker_tools_from_distro_packages() -> None:
    """Source-level: the layer apt-installs the four tools and fails the build without them."""
    text = DOCKERFILE.read_text()
    install = text.split("apt-get install", 1)[1].split("&& rm -rf", 1)[0]
    for pkg in ("ripgrep", "jq", "wget", "patch", "openssh-client", "rsync", "curl"):
        assert pkg in install.split(), pkg
    check = next(ln for ln in text.splitlines() if "for b in ssh" in ln)
    for b in WORKER_TOOLS:
        assert f" {b}" in check
    assert "pip install" not in text.replace("no pip", "")


def test_probe_script_covers_each_tool_offline_as_uid_1000() -> None:
    text = PROBE.read_text()
    assert "--network none" in text and "--user 1000:1000" in text
    for b in WORKER_TOOLS:
        assert b in text
    subprocess.run(["bash", "-n", str(PROBE)], check=True)


def test_image_tools_probe_requires_worker_tools() -> None:
    src = (ROOT / "apps/api/src/argus_api/engines/image_tools.py").read_text()
    for b in WORKER_TOOLS:
        assert f'"{b}"' in src


@pytest.mark.skipif(not _docker_daemon(), reason="docker daemon not available")
def test_built_layer_uid_1000_runs_guard_hook_and_reads_managed_settings() -> None:
    """Build an isolated tools-layer image; only remove the tags this test creates."""
    suffix = uuid.uuid4().hex[:12]
    base_tag = f"argus-wt-attrib-perms-base-{suffix}"
    layer_tag = f"argus-wt-attrib-perms-layer-{suffix}"
    tags = [base_tag, layer_tag]
    try:
        subprocess.run(
            ["docker", "build", "-t", base_tag, "-"],
            input=(
                "FROM node:22-slim\n"
                "RUN apt-get update && apt-get install -y --no-install-recommends git "
                "&& rm -rf /var/lib/apt/lists/*\n"
            ),
            text=True,
            check=True,
            capture_output=True,
        )
        apply = ROOT / "integrations" / "nanoclaw" / "engines" / "apply-tools-layer.sh"
        subprocess.run(
            ["bash", str(apply), base_tag, layer_tag], check=True, capture_output=True
        )
        probe = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--user",
                "1000:1000",
                layer_tag,
                "bash",
                "-ec",
                "set -euo pipefail\n"
                "uid=$(id -u)\n"
                'test "$uid" = 1000\n'
                "test -r /etc/claude-code/managed-settings.json\n"
                "python3 -c \"import json; s=json.load(open('/etc/claude-code/managed-settings.json')); "
                "h=s['hooks']['PreToolUse'][0]['hooks'][0]['command']; "
                "assert h.endswith('command_guard.py')\"\n"
                'echo \'{"tool_name":"Bash","tool_input":{"command":"rm -rf /"}}\' '
                "| python3 /usr/local/lib/argus/command_guard.py | grep -q deny\n"
                "msg=$(mktemp)\n"
                "printf 'bad\\n\\nCo-Authored-By: x\\n' >\"$msg\"\n"
                'if python3 /usr/local/lib/argus/commit_attribution.py validate-msg "$msg"; then exit 1; fi\n'
                "printf 'ok\\n' >\"$msg\"\n"
                'python3 /usr/local/lib/argus/commit_attribution.py validate-msg "$msg"\n'
                "test -x /usr/local/lib/argus/bin/git\n"
                "/usr/local/lib/argus/bin/git --version >/dev/null\n"
                '/usr/local/lib/argus/git-hooks/commit-msg "$msg"\n',
            ],
            capture_output=True,
            text=True,
        )
        assert probe.returncode == 0, probe.stderr or probe.stdout
        tools = subprocess.run(
            ["bash", str(PROBE), layer_tag], capture_output=True, text=True
        )
        assert tools.returncode == 0, tools.stderr or tools.stdout
    finally:
        for tag in tags:
            subprocess.run(["docker", "rmi", tag], capture_output=True)
