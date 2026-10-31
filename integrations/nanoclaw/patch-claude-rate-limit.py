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
skill. The runner at the pinned commit has none, so this script first adds the minimal
plumbing itself (the `providerFailure` content field, `classifyProviderFailure`, and the
failure notice carrying it), so the structured signal travels runner -> channel with or
without add-codex. Idempotent: every hook is "ensure this exact text", and an
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
RATE_FLAG = "      let rateLimited = false; // this turn's SDK error: 'rate_limit'\n"
AUTH_FLAG = (
    "      let authFailed = false; // assistant.error: 'authentication_failed'\n"
)
RATE_MARK = (
    "          if ((message as { error?: string }).error === 'rate_limit')"
    " rateLimited = true;\n"
)
AUTH_MARK = (
    "          if ((message as { error?: string }).error === 'authentication_failed')"
    " authFailed = true;\n"
)
RATE_IMPORT = "import { claudeRateLimitError } from './claude-rate-limit.js';\n"
AUTH_IMPORT = "import { claudeRateLimitError, claudeAuthExpiredError } from './claude-rate-limit.js';\n"
# The result hook before the auth mark, in pieces: harness-usage.ts puts a line inside it.
RATE_RESULT = (
    "          const rateLimit = isError ? claudeRateLimitError(m.result, rateLimited) : undefined;\n"
    "          rateLimited = false;\n"
)
AUTH_RESULT = (
    RATE_RESULT
    + "          const authExpired = isError ? claudeAuthExpiredError(authFailed) : undefined;\n"
    "          authFailed = false;\n"
)
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


# The providerFailure plumbing a runner without NanoClaw's add-codex lacks (the pinned commit).
FAILURE_FIELD = "export const FAILURE_NOTICE_FIELD = 'failureNotice';\n"
FAILURE_FIELD_NEW = (
    FAILURE_FIELD
    + "\n/** Content field carrying the structured provider failure next to a generic notice. */\n"
    + "export const PROVIDER_FAILURE_FIELD = 'providerFailure';\n\n"
    + KIND_NEW
    + "\n"
)
POLL_FIELD_IMPORT = "  FAILURE_NOTICE_FIELD,\n"
POLL_FIELD_IMPORT_NEW = (
    POLL_FIELD_IMPORT + "  PROVIDER_FAILURE_FIELD,\n  type ProviderFailure,\n"
)
DELIVER = "/** Send the dedicated provider error or a generic failure notice. */\n"
DELIVER_NEW = CLASSIFY + "  return undefined;\n}\n\n" + DELIVER
FAILURE_CALL = """            const notice = event.error ?? 'The agent run failed. Check the logs for details.';
            if (sendsFailureNotice(routing)) await deliverErrorResult(routing, notice);
"""
FAILURE_CALL_NEW = """            // A recognised provider failure ships structured, with the generic text only.
            const failure = classifyProviderFailure(providerName, event.error);
            const notice =
              failure || !event.error ? 'The agent run failed. Check the logs for details.' : event.error;
            if (sendsFailureNotice(routing)) await deliverErrorResult(routing, notice, failure);
"""
DELIVER_SIG = "async function deliverErrorResult(routing: RoutingContext, text: string): Promise<void> {\n"
DELIVER_SIG_NEW = (
    "async function deliverErrorResult(\n  routing: RoutingContext,\n  text: string,\n"
    "  failure?: ProviderFailure,\n): Promise<void> {\n"
)
DELIVER_CONTENT = "    content: JSON.stringify({ text: stripHarnessTagArtifacts(text), [FAILURE_NOTICE_FIELD]: true }),\n"
DELIVER_CONTENT_NEW = (
    "    content: JSON.stringify({\n      text: stripHarnessTagArtifacts(text),\n"
    "      [FAILURE_NOTICE_FIELD]: true,\n"
    "      ...(failure ? { [PROVIDER_FAILURE_FIELD]: failure } : {}),\n    }),\n"
)


def add_provider_failure_plumbing(root: Path) -> None:
    """Minimal providerFailure support for a runner that has none; a no-op where it exists."""
    runner = root / RUNNER
    if "PROVIDER_FAILURE_FIELD" not in (runner / "formatter.ts").read_text():
        edit(
            root, "formatter.ts", [("failure field", FAILURE_FIELD, FAILURE_FIELD_NEW)]
        )
    if "function classifyProviderFailure" not in (runner / "poll-loop.ts").read_text():
        edit(
            root,
            "poll-loop.ts",
            [
                ("formatter import", POLL_FIELD_IMPORT, POLL_FIELD_IMPORT_NEW),
                ("classifyProviderFailure stub", DELIVER, DELIVER_NEW),
                ("failure notice", FAILURE_CALL, FAILURE_CALL_NEW),
                ("failure notice signature", DELIVER_SIG, DELIVER_SIG_NEW),
                ("failure notice content", DELIVER_CONTENT, DELIVER_CONTENT_NEW),
            ],
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
                    "      for await", RATE_FLAG + AUTH_FLAG + "      for await"
                ),
                (LOOP.replace("      for await", RATE_FLAG + "      for await"),),
            ),
            (
                "assistant error",
                ASSISTANT,
                ASSISTANT + RATE_MARK + AUTH_MARK,
                (ASSISTANT + RATE_MARK,),
            ),
            ("result error", RESULT, RESULT_NEW),
            # An earlier install wrote the pieces below without the auth half.
            ("auth import", RATE_IMPORT, AUTH_IMPORT),
            ("auth result flag", RATE_RESULT, AUTH_RESULT),
            (
                "auth result text",
                "text: resultAsError || rateLimit ? null",
                "text: resultAsError || rateLimit || authExpired ? null",
            ),
            (
                "auth result error",
                "error: rateLimit ?? (m.errors",
                "error: authExpired ?? rateLimit ?? (m.errors",
            ),
        ],
    )
    add_provider_failure_plumbing(root)
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
