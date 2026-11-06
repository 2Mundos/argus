import { readFileSync } from "node:fs";

import {
  claudeAuthExpiredError,
  claudeAuthExpiredFailure,
  claudeRateLimitError,
  claudeRateLimitFailure,
} from "./overlays/claude-rate-limit.js";
import { codexUsageLimitFailure } from "./overlays/codex-usage-limit.js";

const NOW = new Date("2026-10-20T22:06:00Z");

interface ClaudeEvent {
  type?: string;
  subtype?: string;
  is_error?: boolean;
  terminal_reason?: string;
  result?: string;
  error?: string;
}

/** What the channel derives from a Claude Code stream-json transcript, as the poll loop does per turn. */
export function deriveClaude(events: ClaudeEvent[]) {
  const sdkError = (e: string) => events.some((ev) => ev.type === "assistant" && ev.error === e);
  const result = events.findLast((ev) => ev.type === "result");
  const failed = result?.is_error === true;
  const authExpiredError = failed
    ? claudeAuthExpiredError(sdkError("authentication_failed"))
    : undefined;
  const rateLimitError = failed
    ? claudeRateLimitError(result?.result, sdkError("rate_limit"))
    : undefined;
  return {
    result: {
      is_error: result?.is_error,
      subtype: result?.subtype,
      terminal_reason: result?.terminal_reason,
    },
    rateLimitError: rateLimitError ?? null,
    rateLimitFailure: claudeRateLimitFailure(rateLimitError, NOW) ?? null,
    authExpiredError: authExpiredError ?? null,
    authExpiredFailure: claudeAuthExpiredFailure(authExpiredError) ?? null,
  };
}

interface CodexEvent {
  type?: string;
  usage?: unknown;
  error?: { message?: string };
}

/**
 * What the channel derives from a `codex exec --json` stream. Only `turn.failed` carries the
 * final failure; top-level `error` events are retry notices. Fields follow
 * fixtures/codex-0.162.1/PIN.md.
 */
export function deriveCodex(events: CodexEvent[]) {
  const failed = events.findLast((ev) => ev.type === "turn.failed");
  const completed = events.findLast((ev) => ev.type === "turn.completed" && ev.usage != null);
  return {
    turnFailed: failed !== undefined,
    codexUsageLimitFailure: codexUsageLimitFailure(failed?.error?.message) ?? null,
    usage: completed?.usage ?? null,
  };
}

export const DERIVE = { claude: deriveClaude, codex: deriveCodex } as const;

export function readEvents(path: string): unknown[] {
  return readFileSync(path, "utf8")
    .split("\n")
    .filter((l) => l.trim() !== "")
    .map((l) => JSON.parse(l));
}

// `bun transcripts-derive.ts <engine> <events.jsonl>` prints the derived values as JSON;
// scripts/record_transcript.py uses it to draft expected.json.
if (import.meta.main) {
  const [engine, path] = process.argv.slice(2);
  const derive = DERIVE[engine as keyof typeof DERIVE];
  if (!derive || !path) {
    console.error(`usage: transcripts-derive.ts <${Object.keys(DERIVE).join("|")}> <events.jsonl>`);
    process.exit(2);
  }
  console.log(JSON.stringify(derive(readEvents(path) as never), null, 2));
}
