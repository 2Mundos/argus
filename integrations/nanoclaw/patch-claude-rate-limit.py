#!/usr/bin/env python3
"""Hook Claude Code's quota block into a NanoClaw agent runner (called by install.sh).

usage: patch-claude-rate-limit.py /path/to/nanoclaw

claude.ts gives a failed turn that hit its quota only Claude Code's one-line notice or a fixed
text as its error (overlays/claude-rate-limit.ts, copied next to it first); poll-loop.ts turns
that error into the structured providerFailure
`{provider:"claude", kind:"rate_limit", resets_at?}` with the generic notice text. The
poll-loop and formatter hooks extend the providerFailure plumbing of NanoClaw's add-codex
skill; a runner without it only gets the claude.ts hook, whose error then reaches the
channel as the notice text. Idempotent: every hook is "ensure this exact text", and an
anchor that is missing or ambiguous stops the install (the upstream shape changed).
"""

import sys
from pathlib import Path

RUNNER = Path("container/agent-runner/src")


def edit(root: Path, path: str, hooks: list[tuple[str, str, str]]) -> None:
    """hooks: (what, anchor, replacement). Skipped when the replacement is present."""
    p = root / RUNNER / path
    s = p.read_text()
    original = s
    for what, anchor, new in hooks:
        if new in s:
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
          yield {
            type: 'result',
            text: resultAsError || rateLimit ? null : (m.result ?? null),
            isError,
            error: rateLimit ?? (m.errors?.length ? m.errors.join('\n') : resultAsError || undefined),
          };
"""
KIND = """export type ProviderFailureKind = 'auth_expired';

export interface ProviderFailure {
  provider: string;
  kind: ProviderFailureKind;
}"""
KIND_NEW = """export type ProviderFailureKind = 'auth_expired' | 'rate_limit';

export interface ProviderFailure {
  provider: string;
  kind: ProviderFailureKind;
  /** ISO time a rate limit resets, when the provider states one. */
  resets_at?: string;
}"""
POLL_IMPORT = "import { stripHarnessTagArtifacts } from './harness-tag-strip.js';\n"
CLASSIFY = (
    "function classifyProviderFailure(providerName: string, error: string | undefined):"
    " ProviderFailure | undefined {\n"
)


def main(root: Path) -> None:
    edit(
        root,
        "providers/claude.ts",
        [
            (
                "import",
                IMPORT,
                IMPORT
                + "import { claudeRateLimitError } from './claude-rate-limit.js';\n",
            ),
            (
                "turn flag",
                LOOP,
                LOOP.replace(
                    "      for await",
                    "      let rateLimited = false; // this turn's SDK error: 'rate_limit'\n"
                    "      for await",
                ),
            ),
            (
                "assistant error",
                ASSISTANT,
                ASSISTANT
                + "          if ((message as { error?: string }).error === 'rate_limit')"
                " rateLimited = true;\n",
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
    edit(root, "formatter.ts", [("ProviderFailure", KIND, KIND_NEW)])
    edit(
        root,
        "poll-loop.ts",
        [
            (
                "import",
                POLL_IMPORT,
                POLL_IMPORT
                + "import { claudeRateLimitFailure } from './providers/claude-rate-limit.js';\n",
            ),
            (
                "classifyProviderFailure",
                CLASSIFY,
                CLASSIFY
                + "  if (providerName === 'claude') return claudeRateLimitFailure(error);\n",
            ),
        ],
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
