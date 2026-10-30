#!/usr/bin/env python3
"""Hook Claude Code's quota block into a NanoClaw agent runner (called by install.sh).

usage: patch-claude-rate-limit.py /path/to/nanoclaw

claude.ts gives a failed turn that hit its quota only Claude Code's one-line notice or a fixed
text as its error (overlays/claude-rate-limit.ts, copied next to it first); poll-loop.ts turns
that error into the structured providerFailure
`{provider:"claude", kind:"rate_limit", resets_at?}` with the generic notice text. A turn
whose assistant message the SDK marked `authentication_failed` (that value only, reset
after every result) errors with the fixed marker instead, which becomes
`{provider:"claude", kind:"auth_expired"}`.
The poll-loop and formatter hooks extend the providerFailure plumbing of NanoClaw's add-codex
skill; a runner without it only gets the claude.ts hook, whose error then reaches the
channel as the notice text. Idempotent: every hook is "ensure this exact text", and an
anchor that is missing or ambiguous stops the install (the upstream shape changed).
Upgrading: a hook also rewrites the text an earlier version of this script wrote, so a
checkout patched before the Codex usage-limit hook (#74) converges instead of stopping.
"""

import sys
from pathlib import Path

RUNNER = Path("container/agent-runner/src")


Hook = tuple[str, str, str] | tuple[str, str, str, tuple[str, ...]]


def edit(root: Path, path: str, hooks: list[Hook]) -> None:
    """hooks: (what, anchor, replacement[, earlier]). Skipped when the replacement is
    present. `earlier` are texts a previous version of this script wrote: each becomes
    the replacement, when the replacement is not there yet or the earlier text wraps it
    (a duplicate left by an upgrade that did not know the earlier text)."""
    p = root / RUNNER / path
    s = p.read_text()
    original = s
    for what, anchor, new, *rest in hooks:
        for old in rest[0] if rest else ():
            if s.count(old) == 1 and (new not in s or new in old):
                s = s.replace(old, new, 1)
        if what == "import" and "from './claude-rate-limit.js'" in s:
            continue
        if new in s:
            continue
        # harness-usage.ts inserts a line between resultAsError and the rate-limit hook.
        if (
            what == "result error"
            and "claudeRateLimitError(m.result, rateLimited)" in s
        ):
            continue
        if s.count(anchor) != 1:
            raise SystemExit(f"{p}: {what}: upstream shape changed")
        s = s.replace(anchor, new, 1)
    if s != original:
        p.write_text(s)
        print(f"patched {p}")


IMPORT = "import { registerProvider } from './provider-registry.js';\n"
LOOP = "      let lastStreamActivityAt = 0;\n      for await (const message of sdkResult) {\n"
ASSISTANT = "        } else if (message.type === 'assistant') {\n"
RESULT = r"""          const resultAsError = hint ? `${candidate}\n${hint}` : '';
          yield {
            type: 'result',
            text: resultAsError ? null : (m.result ?? null),
            isError,
            error: m.errors?.length ? m.errors.join('\n') : resultAsError || undefined,
          };
"""
RESULT_NEW = r"""          const resultAsError = hint ? `${candidate}\n${hint}` : '';
          // Argus: a quota block's error is only its one-line notice or a fixed text (claude-rate-limit.ts).
          const rateLimit = isError ? claudeRateLimitError(m.result, rateLimited) : undefined;
          rateLimited = false;
          const authExpired = isError ? claudeAuthExpiredError(authFailed) : undefined;
          authFailed = false;
          yield {
            type: 'result',
            text: resultAsError || rateLimit || authExpired ? null : (m.result ?? null),
            isError,
            error: authExpired ?? rateLimit ?? (m.errors?.length ? m.errors.join('\n') : resultAsError || undefined),
          };
"""
KIND = """export type ProviderFailureKind = 'auth_expired';

export interface ProviderFailure {
  provider: string;
  kind: ProviderFailureKind;
}"""
# What the hook wrote before the Codex usage-limit fields (#74).
KIND_BEFORE_CODEX = """export type ProviderFailureKind = 'auth_expired' | 'rate_limit';

export interface ProviderFailure {
  provider: string;
  kind: ProviderFailureKind;
  /** ISO time a rate limit resets, when the provider states one. */
  resets_at?: string;
}"""
KIND_NEW = """export type ProviderFailureKind = 'auth_expired' | 'rate_limit';

export interface ProviderFailure {
  provider: string;
  kind: ProviderFailureKind;
  /** ISO time a rate limit resets, when the provider states one. */
  resets_at?: string;
  /** Codex usage limit: the provider's sentence and its reset in its own words. */
  notice?: string;
  reset_text?: string;
}"""
POLL_IMPORT = "import { stripHarnessTagArtifacts } from './harness-tag-strip.js';\n"
CLASSIFY = (
    "function classifyProviderFailure(providerName: string, error: string | undefined):"
    " ProviderFailure | undefined {\n"
)


CLAUDE_HOOK = "  if (providerName === 'claude') return claudeRateLimitFailure(error);\n"
CLAUDE_CLASSIFY_BLOCK = (
    "  if (providerName === 'claude') {\n"
    "    const auth = claudeAuthExpiredFailure(error);\n"
    "    if (auth) return auth;\n"
    "    return claudeRateLimitFailure(error);\n"
    "  }\n"
)
CODEX_USAGE_BLOCK = (
    "  if (providerName === 'codex') {\n"
    "    const limit = codexUsageLimitFailure(error);\n"
    "    if (limit) return limit;\n"
    "  }\n"
)
CLAUDE_IMPORT = "import { claudeRateLimitFailure, claudeAuthExpiredFailure } from './providers/claude-rate-limit.js';\n"
CODEX_IMPORT = (
    "import { codexUsageLimitFailure } from './providers/codex-usage-limit.js';\n"
)
POLL_IMPORTS = POLL_IMPORT + CLAUDE_IMPORT + CODEX_IMPORT


def main(root: Path) -> None:
    edit(
        root,
        "providers/claude.ts",
        [
            (
                "import",
                IMPORT,
                IMPORT
                + "import { claudeRateLimitError, claudeAuthExpiredError } from './claude-rate-limit.js';\n",
                (
                    IMPORT
                    + "import { claudeRateLimitError } from './claude-rate-limit.js';\n",
                ),
            ),
            (
                "turn flag",
                LOOP,
                LOOP.replace(
                    "      for await",
                    "      let rateLimited = false; // this turn's SDK error: 'rate_limit'\n"
                    "      let authFailed = false; // assistant.error: 'authentication_failed'\n"
                    "      for await",
                ),
            ),
            (
                "assistant error",
                ASSISTANT,
                ASSISTANT
                + "          if ((message as { error?: string }).error === 'rate_limit')"
                " rateLimited = true;\n"
                + "          if ((message as { error?: string }).error === 'authentication_failed')"
                " authFailed = true;\n",
            ),
            ("result error", RESULT, RESULT_NEW),
        ],
    )
    formatter = (root / RUNNER / "formatter.ts").read_text()
    if "PROVIDER_FAILURE_FIELD" not in formatter:
        print(
            "warning: the agent runner has no providerFailure field (NanoClaw add-codex);"
            " a Claude quota block reaches Argus as notice text only",
            file=sys.stderr,
        )
        return
    edit(
        root,
        "formatter.ts",
        [("ProviderFailure", KIND, KIND_NEW, (KIND_BEFORE_CODEX,))],
    )
    edit(
        root,
        "poll-loop.ts",
        [
            (
                "import",
                POLL_IMPORT,
                POLL_IMPORTS,
                # The duplicate a #74-era upgrade left (it lacked these earlier texts); before #74.
                (POLL_IMPORTS + CLAUDE_IMPORT, POLL_IMPORT + CLAUDE_IMPORT),
            ),
            (
                "classifyProviderFailure",
                CLASSIFY,
                CLASSIFY + CLAUDE_CLASSIFY_BLOCK,
                (CLASSIFY + CLAUDE_HOOK,),
            ),
            (
                "codex usage limit",
                CLAUDE_CLASSIFY_BLOCK,
                CLAUDE_CLASSIFY_BLOCK + CODEX_USAGE_BLOCK,
                (CLAUDE_HOOK + CODEX_USAGE_BLOCK,),
            ),
        ],
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
