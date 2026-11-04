"""Guard: every Argus test installed into the NanoClaw checkout is run by CI.

CI's nanoclaw-vitest job runs `integrations/nanoclaw/provision.sh check`, which runs
only the suites named on its `pnpm exec vitest run` line. A suite that install.sh
copies into src/channels or src/gateway-providers but that is not on that line is
silently never run. Rule: every installed src/channels|src/gateway-providers
`*.test.ts` is in the list or in EXEMPT, with a reason.
"""

import re
from pathlib import Path

NANOCLAW = Path(__file__).resolve().parents[1]

_UNTRIAGED = "not in the vitest list at 351f632; untriaged, follow-up"
EXEMPT = {
    "src/channels/argus-local-forward.test.ts": _UNTRIAGED,
    "src/channels/argus-repo-preflight.docker.test.ts": "image-gated, describe.skipIf(!IMAGE)",
    "src/channels/argus-runtime.test.ts": _UNTRIAGED,
    "src/channels/argus-startup-order.test.ts": _UNTRIAGED,
    "src/channels/argus-steer-unknown.e2e.test.ts": "e2e, run by scripts/steer-unknown-e2e.sh",
    "src/channels/argus-task-inputs.e2e.test.ts": "e2e, run by scripts/task-input-e2e.sh",
    "src/channels/argus-usage-relay.test.ts": _UNTRIAGED,
    "src/gateway-providers/argus-claude-stub.test.ts": _UNTRIAGED,
    "src/gateway-providers/argus-codex-stub.test.ts": _UNTRIAGED,
    "src/gateway-providers/argus-custom-bridge.test.ts": _UNTRIAGED,
    "src/gateway-providers/argus-custom-harness.test.ts": _UNTRIAGED,
}

_DIRS = ("src/channels/", "src/gateway-providers/")
_COPY = re.compile(r'^cp\s+"[^"]*"\s+"\$target/(src/[^"]+\.test\.ts)"', re.M)
_VITEST = re.compile(r"^\s*pnpm exec vitest run (.+)$", re.M)


def _in_scope(path: str) -> bool:
    return path.startswith(_DIRS)


def installed_tests(install_sh: str) -> set[str]:
    return {p for p in _COPY.findall(install_sh) if _in_scope(p)}


def listed_tests(provision_sh: str) -> set[str]:
    match = _VITEST.search(provision_sh)
    assert match, "no `pnpm exec vitest run` line in provision.sh"
    return {p for p in match.group(1).split() if p.startswith("src/")}


def problems(install_sh: str, provision_sh: str) -> list[str]:
    installed = installed_tests(install_sh)
    listed = {p for p in listed_tests(provision_sh) if _in_scope(p)}
    out = [
        f"{p}: installed but neither in the vitest list nor in EXEMPT"
        for p in sorted(installed - listed - EXEMPT.keys())
    ]
    out += [
        f"{p}: EXEMPT but no longer installed"
        for p in sorted(EXEMPT.keys() - installed)
    ]
    out += [
        f"{p}: EXEMPT but also in the vitest list"
        for p in sorted(EXEMPT.keys() & listed)
    ]
    # Upstream NanoClaw tests (onecli.test.ts, ...) ship in the checkout, not via install.sh.
    out += [
        f"{p}: in the vitest list but not installed by install.sh"
        for p in sorted(listed - installed)
        if Path(p).name.startswith("argus-")
    ]
    return out


def _real() -> tuple[str, str]:
    return (NANOCLAW / "install.sh").read_text(), (
        NANOCLAW / "provision.sh"
    ).read_text()


def test_every_installed_suite_is_run_or_exempt() -> None:
    assert problems(*_real()) == []


def test_exemptions_have_reasons() -> None:
    assert all(reason.strip() for reason in EXEMPT.values())


def test_guard_fails_when_a_suite_is_dropped_from_the_list(tmp_path: Path) -> None:
    install_sh, provision_sh = _real()
    dropped = "src/channels/argus-repo-preflight.test.ts"
    assert dropped in listed_tests(provision_sh)
    (tmp_path / "provision.sh").write_text(provision_sh.replace(f" {dropped}", "", 1))
    (tmp_path / "install.sh").write_text(install_sh)
    result = problems(
        (tmp_path / "install.sh").read_text(), (tmp_path / "provision.sh").read_text()
    )
    assert result == [
        f"{dropped}: installed but neither in the vitest list nor in EXEMPT"
    ]


def test_guard_fails_on_stale_exemption_and_unknown_listing() -> None:
    install_sh, provision_sh = _real()
    exempt_path = "src/channels/argus-runtime.test.ts"
    stale = problems(
        install_sh.replace(Path(exempt_path).name, "gone.ts"), provision_sh
    )
    assert f"{exempt_path}: EXEMPT but no longer installed" in stale
    both = problems(
        install_sh, provision_sh.replace("vitest run ", f"vitest run {exempt_path} ", 1)
    )
    assert f"{exempt_path}: EXEMPT but also in the vitest list" in both
    ghost = problems(
        install_sh,
        provision_sh.replace(
            "vitest run ", "vitest run src/channels/argus-ghost.test.ts ", 1
        ),
    )
    assert (
        "src/channels/argus-ghost.test.ts: in the vitest list but not installed by install.sh"
        in ghost
    )
