"""The closed milestone kind set is enforced in four places; they must agree
(docs/specs/progress-milestones.spec.md 3.1)."""

from __future__ import annotations

import re
from pathlib import Path

from argus_core.tasks import ProgressKind

NANOCLAW = Path(__file__).resolve().parents[1]


def _list(path: Path, opener: str) -> set[str]:
    body = path.read_text().split(opener, 1)[1].split("]", 1)[0]
    return set(re.findall(r'"([a-z_]+)"', body))


def test_four_places_agree():
    core = {k.value for k in ProgressKind}
    delegate = _list(NANOCLAW / "engines" / "milestones.mjs", "export const KINDS = [")
    relay = _list(NANOCLAW / "argus-milestone-relay.ts", "const KINDS = [")
    # The emitter never writes the host's own `waiting`; the host relay never accepts the
    # worker's own words (`waiting`, `waiting_input`).
    assert delegate == core - {"waiting"}
    assert relay == delegate - {"waiting_input"}
    assert "looping" in core and "looping" in relay
