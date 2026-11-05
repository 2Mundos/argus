import { describe, expect, it } from "bun:test";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import {
  claudeAuthExpiredError,
  claudeAuthExpiredFailure,
  claudeRateLimitError,
  claudeRateLimitFailure,
} from "./overlays/claude-rate-limit.js";

const ROOT = join(import.meta.dir, "fixtures", "transcripts");
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
function deriveClaude(events: ClaudeEvent[]) {
  const sdkError = (e: string) => events.some((ev) => ev.type === "assistant" && ev.error === e);
  const result = events.findLast((ev) => ev.type === "result");
  const failed = result?.is_error === true;
  const authExpiredError = failed ? claudeAuthExpiredError(sdkError("authentication_failed")) : undefined;
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

const DERIVE = { claude: deriveClaude } as const;

const cases = readdirSync(ROOT, { withFileTypes: true })
  .filter((d) => d.isDirectory() && d.name in DERIVE)
  .flatMap((engine) =>
    readdirSync(join(ROOT, engine.name), { withFileTypes: true })
      .filter((d) => d.isDirectory())
      .map((c) => [`${engine.name}/${c.name}`, engine.name as keyof typeof DERIVE, c.name] as const),
  );

describe("engine transcripts", () => {
  it("has recordings", () => {
    expect(cases.length).toBeGreaterThan(0);
  });

  it.each(cases)("%s derives what expected.json says", (_label, engine, name) => {
    const dir = join(ROOT, engine, name);
    const events = readFileSync(join(dir, "events.jsonl"), "utf8")
      .split("\n")
      .filter((l) => l.trim() !== "")
      .map((l) => JSON.parse(l));
    const expected = JSON.parse(readFileSync(join(dir, "expected.json"), "utf8"));
    const derived = DERIVE[engine](events) as Record<string, unknown>;
    for (const key of Object.keys(expected)) expect(derived[key]).toEqual(expected[key]);
    expect(Object.keys(derived).sort()).toEqual(Object.keys(expected).sort());
  });
});
