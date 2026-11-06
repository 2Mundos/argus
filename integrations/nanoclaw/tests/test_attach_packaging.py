"""`argus-task attach` packaging seam (task-helpers spec 3.10).

The worker half is part of argus-task.mjs (already installed, copied into the tools layer
and the project image). The host half, argus-attach-relay.ts, must be installed into the
NanoClaw checkout, both halves must agree on the bridge file names, and the guidance the
worker is given must name the command and the order (attach before COMPLETE).
"""

import re
from pathlib import Path

NANOCLAW = Path(__file__).resolve().parents[1]
CLI = (NANOCLAW / "argus-task.mjs").read_text()
RELAY = (NANOCLAW / "argus-attach-relay.ts").read_text()


def _consts(source: str) -> dict[str, str]:
    return dict(re.findall(r'const (ATTACH_[A-Z_]+)\s*=\s*"([^"]+)"', source))


def test_relay_is_installed_into_the_checkout() -> None:
    install = (NANOCLAW / "install.sh").read_text()
    assert (
        'cp "$here/argus-attach-relay.ts" "$target/src/channels/argus-attach-relay.ts"'
        in install
    )


def test_cli_and_relay_agree_on_the_bridge_files() -> None:
    cli, relay = _consts(CLI), _consts(RELAY)
    for name in (
        "ATTACH_REQUESTS_FILE",
        "ATTACH_RECEIPTS_FILE",
        "ATTACH_STATUS_FILE",
        "ATTACH_SPOOL_DIR",
        "ATTACH_CAPABILITY",
    ):
        assert cli[name] == relay[name], name
    roles = re.compile(r"ATTACH_ROLES\s*=\s*\[([^\]]+)\]")
    assert roles.search(CLI).group(1) == roles.search(RELAY).group(1)


def test_cli_ships_in_every_image_path() -> None:
    assert (NANOCLAW / "argus-task.mjs").exists()
    layer = (NANOCLAW / "engines/Dockerfile.tools-layer").read_text()
    assert "COPY --chmod=0755 argus-task.mjs /usr/local/bin/argus-task" in layer
    assert (
        'cp "$here/../argus-task.mjs"'
        in (NANOCLAW / "engines/build-project-image.sh").read_text()
    )
    assert (
        'task_src="$here/argus-task.mjs"'
        in (NANOCLAW / "install-gitea-api.sh").read_text()
    )


def test_worker_guidance_names_the_command_and_the_order() -> None:
    source = (NANOCLAW / "argus.ts").read_text()
    start = source.index("export const ATTACH_GUIDANCE")
    block = source[start : source.index("];", start)]
    assert "argus-task attach" in block
    assert "BEFORE" in block and "COMPLETE" in block
    assert "receipt" in block
    assert "send_file" in block
