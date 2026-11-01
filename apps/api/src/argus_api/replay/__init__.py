"""Dev-only replay worker (docs/drafts/2026-10-31.replay-engine.draft.md).

Plays a recorded attempt back through the MCP tools a worker uses, without a model.
Nothing here is imported by the API: the command is enabled by `ARGUS_DEV_REPLAY=1`.
"""

import os

ENV = "ARGUS_DEV_REPLAY"
POOL = "replay"  # the capability that names the replay pool


def enabled() -> bool:
    return os.environ.get(ENV) == "1"
