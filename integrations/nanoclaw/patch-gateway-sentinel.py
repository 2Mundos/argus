#!/usr/bin/env python3
"""Admit the Copilot gateway sentinel through spec admission, and nothing else (install.sh).

usage: patch-gateway-sentinel.py /path/to/nanoclaw

The sentinel (argus-copilot-stub.ts) is a public constant shaped like a fine-grained PAT, so
upstream's looksLikeCredential() refuses it in ContainerSpec.contributedEnv. The exemption is
the exact pair (COPILOT_GITHUB_TOKEN, that constant) in the contributedEnv lane only: any other
key, any other value (including a real github_pat_ token) and the ordinary `env` lane keep the
upstream checks. The constant is read from argus-copilot-stub.ts so there is one source.

Idempotent; refuses loudly when the upstream shape changed.
"""

import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
here = Path(__file__).resolve().parent
stub = (here / "argus-copilot-stub.ts").read_text()
m = re.search(r'COPILOT_SENTINEL_TOKEN =\s*"(github_pat_[A-Za-z0-9_]+)"', stub)
assert m, "argus-copilot-stub.ts: COPILOT_SENTINEL_TOKEN not found"
SENTINEL = m.group(1)

p = root / "src" / "drivers" / "types.ts"
s = p.read_text()
if "isArgusGatewaySentinel" in s:
    sys.exit(0)

OLD = "      if (looksLikeCredential(value)) {\n        throw deniedByPolicy(`credential value in contributed env"
NEW = "      if (!isArgusGatewaySentinel(key, value) && looksLikeCredential(value)) {\n        throw deniedByPolicy(`credential value in contributed env"
assert s.count(OLD) == 1, "types.ts validateSpec contributedEnv check changed upstream"
s = s.replace(OLD, NEW, 1)

ANCHOR = "/**\n * Credential VALUES, recognised by issuer-assigned prefixes."
assert s.count(ANCHOR) == 1, "types.ts looksLikeCredential doc anchor changed upstream"
HELPER = f"""/**
 * Argus: the one non-secret gateway stub that is shaped like a credential. The Copilot
 * executor lease puts this public constant in COPILOT_GITHUB_TOKEN so the pinned CLI
 * accepts the prefix client-side; the vault swaps the real token on the wire. Exempt
 * only as the exact (key, value) pair and only from the contributedEnv value check:
 * a real token, the sentinel under another key, or the `env` lane are still refused.
 */
const ARGUS_GATEWAY_SENTINELS: Readonly<Record<string, string>> = Object.freeze({{
  COPILOT_GITHUB_TOKEN: '{SENTINEL}',
}});

export function isArgusGatewaySentinel(key: string, value: string): boolean {{
  return Object.hasOwn(ARGUS_GATEWAY_SENTINELS, key) && ARGUS_GATEWAY_SENTINELS[key] === value;
}}

"""
s = s.replace(ANCHOR, HELPER + ANCHOR, 1)
p.write_text(s)
print(f"patched {p} (Copilot gateway sentinel admission)")
