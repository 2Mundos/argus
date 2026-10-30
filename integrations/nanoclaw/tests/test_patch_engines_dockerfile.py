"""patch-engines-dockerfile.py against real NanoClaw agent Dockerfiles: upstream's
pristine one (NanoClaw 8fbacc91) and the states earlier installs left on a worker host,
including the ones whose install steps outran their COPY line ("cannot stat
/tmp/argus-engines/milestones.mjs" at build time)."""

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "engines/patch-engines-dockerfile.py"
FIXTURES = ROOT / "fixtures/engines-dockerfile"
STAGE = "/tmp/argus-engines/"
REQUIRED = {
    "/usr/local/bin/argus-delegate",
    "/usr/local/bin/argus-gitea-api",
    "/usr/local/bin/argus-github-api",
    "/tmp/argus-engines/account-settings.mjs",
    "/tmp/argus-engines/anthropic-redirect.mjs",
    "/tmp/argus-engines/milestones.mjs",
    "/tmp/argus-engines/livelog.mjs",
    "/tmp/argus-engines/changes.mjs",
    "/tmp/argus-engines/usage.mjs",
}
# Earlier layouts: model-policy on its own COPY line, modules added one release at a time.
UPGRADES = [
    "oct20-copy-without-model-policy",
    "oct21-milestones-not-copied",
    "oct21-usage-not-copied",
    "oct21-account-settings-not-copied",
    "oct27-no-github-helper",
    "oct29-changes-not-copied",
]


def patch(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def argus_block(s: str) -> tuple[list[str], str]:
    """The Argus COPY line's sources and its RUN step (up to the cleanup)."""
    (copy,) = re.findall(
        r"^COPY (install-cursor-agent\.sh(?: install-copilot-cli\.sh)? argus-delegate\.mjs.*) "
        + re.escape(STAGE)
        + "$",
        s,
        re.M,
    )
    start = s.index(copy)
    run = s[start : s.index(" && rm -rf /tmp/argus-engines", start)]
    return shlex.split(copy), run


def staged_reads(run: str) -> set[str]:
    return set(re.findall(re.escape(STAGE) + r"([\w.-]+)", run))


def copied(tmp_path: Path, name: str) -> Path:
    p = tmp_path / "Dockerfile"
    p.write_text((FIXTURES / f"{name}.Dockerfile").read_text())
    return p


def assert_buildable(s: str) -> None:
    sources, run = argus_block(s)
    missing = staged_reads(run) - set(sources)
    assert not missing, f"installed but not copied: {sorted(missing)}"
    assert len(sources) == len(set(sources))
    for needed in REQUIRED - {"/usr/local/bin/argus-gitea-api"}:
        assert needed in run, needed
    # Installed once: in the Argus step or by the older standalone Gitea section.
    assert s.count("/usr/local/bin/argus-gitea-api") == 1
    assert s.count("npm install --prefix /usr/local/lib/argus-gitea") == 1
    assert re.search(
        r"^COPY model-policy\.mjs model-policy\.defaults\.json /usr/local/bin/$",
        s,
        re.M,
    )
    assert s.count("Argus specialist CLIs") == 1


def test_pristine_gets_the_whole_block_once(tmp_path: Path) -> None:
    p = copied(tmp_path, "pristine")
    assert "argus-engines" not in p.read_text()
    first = patch(p)
    assert first.returncode == 0, first.stderr
    assert_buildable(p.read_text())
    # What a fresh install produces (2026-10-21 worker host, plus the GitHub helper).
    assert p.read_text() == (FIXTURES / "patched.Dockerfile").read_text()
    again = patch(p)
    assert (again.returncode, again.stdout) == (0, "")


def test_already_patched_is_left_alone(tmp_path: Path) -> None:
    p = copied(tmp_path, "patched")
    before = p.read_text()
    result = patch(p)
    assert (result.returncode, result.stdout) == (0, "")
    assert p.read_text() == before


@pytest.mark.parametrize("name", UPGRADES)
def test_earlier_layouts_upgrade_to_a_buildable_block(
    tmp_path: Path, name: str
) -> None:
    p = copied(tmp_path, name)
    before = p.read_text()
    first = patch(p)
    assert first.returncode == 0, first.stderr
    after = p.read_text()
    assert_buildable(after)
    # Only the Argus COPY line and RUN step change; NanoClaw's own lines stay.
    old_lines = {line for line in before.splitlines() if STAGE not in line}
    assert old_lines <= set(after.splitlines())
    again = patch(p)
    assert (again.returncode, again.stdout) == (0, "")
    assert p.read_text() == after


# The first is a layout change, the last two only lack a module nothing reads yet.
@pytest.mark.parametrize("name", UPGRADES[1:-2])
def test_fixtures_reproduce_the_build_failure(name: str) -> None:
    """The broken states really are broken: a step reads a file the COPY never staged."""
    sources, run = argus_block((FIXTURES / f"{name}.Dockerfile").read_text())
    assert staged_reads(run) - set(sources)


def test_an_unknown_shape_stops_the_install(tmp_path: Path) -> None:
    p = tmp_path / "Dockerfile"
    p.write_text("FROM node:22-slim\nRUN true\n")
    result = patch(p)
    assert result.returncode != 0
    assert "Dockerfile shape changed" in result.stderr
    two = (FIXTURES / "patched.Dockerfile").read_text()
    sources, _ = argus_block(two)
    line = f"COPY {' '.join(sources)} {STAGE}"
    p.write_text(two + "\n" + line + "\n")
    result = patch(p)
    assert result.returncode != 0
    assert "2 Argus COPY lines" in result.stderr


def test_install_engines_uses_the_patcher() -> None:
    install = (ROOT / "engines/install-engines.sh").read_text()
    assert (
        'python3 "$here/patch-engines-dockerfile.py" "$target/container/Dockerfile"'
        in install
    )
    # Every file the block may COPY is one install-engines.sh ships next to the Dockerfile.
    shipped = re.search(r'^cp ("\$here/[^\n]*) "\$target/container/"$', install, re.M)
    assert shipped
    names = {Path(w.strip('"')).name for w in shipped[1].split()}
    # Staged from ../ by install-gitea-api.sh.
    names |= {"gitea-api.mjs", "github-api.mjs"}
    sources, _ = argus_block((FIXTURES / "patched.Dockerfile").read_text())
    assert set(sources) <= names
