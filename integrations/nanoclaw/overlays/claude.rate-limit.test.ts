// Installed next to claude.ts by integrations/nanoclaw/install.sh: the real Claude
// provider and poll loop, with the SDK replaced by a scripted message stream.
import { afterEach, beforeEach, expect, it, mock, setSystemTime } from "bun:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { getUndeliveredMessages } from "../db/messages-out.js";
import { closeSessionDb, getInboundDb, initTestSessionDb } from "../mailbox/sqlite/connection.js";
import { processQuery } from "../poll-loop.js";

const sdkMessages: unknown[] = [];
mock.module("@anthropic-ai/claude-agent-sdk", () => ({
  query: () =>
    (async function* () {
      for (const message of sdkMessages) yield message;
    })(),
}));

await import("./index.js");
await import("../provider-contracts/index.js");
const { createProvider } = await import("./factory.js");
const { claudeRuntimeContract } = await import("../provider-contracts/claude.js");
const { MEMORY_SESSION_HOOK } = await import("../memory/session-hook.js");
const { CLAUDE_AUTH_FAILED } = await import("./claude-rate-limit.js");
// The poll-loop hook needs the runner's providerFailure plumbing (NanoClaw add-codex);
// without it only the claude.ts hook is installed (patch-claude-rate-limit.py).
const hasProviderFailure = "PROVIDER_FAILURE_FIELD" in (await import("../formatter.js"));

const GENERIC = "The agent run failed. Check the logs for details.";
const LINE = "You've hit your session limit · resets 2am (UTC)";
let tmp: string;
let previousHome: string | undefined;

beforeEach(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "claude-rate-limit-"));
  previousHome = process.env.HOME;
  process.env.HOME = tmp;
  sdkMessages.length = 0;
  setSystemTime(new Date("2026-10-20T22:06:00Z"));
  initTestSessionDb();
  getInboundDb()
    .prepare(
      `INSERT INTO destinations (name, display_name, type, channel_type, platform_id, agent_group_id)
       VALUES ('main', 'main', 'channel', 'discord', 'chan-1', NULL)`,
    )
    .run();
});

afterEach(() => {
  setSystemTime();
  closeSessionDb();
  if (previousHome === undefined) delete process.env.HOME;
  else process.env.HOME = previousHome;
  fs.rmSync(tmp, { recursive: true, force: true });
});

/** What the channel receives for one scripted Claude turn. */
async function delivered(): Promise<Array<Record<string, unknown>>> {
  const provider = createProvider("claude");
  provider.registerMemorySessionHook(MEMORY_SESSION_HOOK);
  await processQuery(
    provider.query({ prompt: "work", cwd: tmp }),
    { platformId: "chan-1", channelType: "discord", threadId: null, inReplyTo: "m1" },
    ["m1"],
    "claude",
    undefined,
    "work",
    undefined,
    claudeRuntimeContract.textDelivery === "mid-turn-complete",
  );
  return getUndeliveredMessages().map((row) => JSON.parse(row.content));
}

/** The provider's result events for one scripted turn (the claude.ts hook alone). */
async function results(): Promise<Array<Record<string, unknown>>> {
  const provider = createProvider("claude");
  provider.registerMemorySessionHook(MEMORY_SESSION_HOOK);
  const query = provider.query({ prompt: "work", cwd: tmp });
  const out: Array<Record<string, unknown>> = [];
  for await (const event of query.events) {
    if (event.type === "result") out.push(event as unknown as Record<string, unknown>);
  }
  return out;
}

/** A failed turn whose assistant message carries the SDK's `error` (or none). */
function erroredTurn(error: string | undefined, text: string): void {
  sdkMessages.push(
    { type: "system", subtype: "init", session_id: "s1" },
    {
      type: "assistant",
      ...(error ? { error } : {}),
      message: { model: "<synthetic>", content: [{ type: "text", text }] },
    },
    { type: "result", subtype: "success", is_error: true, result: text, errors: [] },
  );
}

/** The SDK's shape for a quota-blocked turn: a synthetic assistant message, then an error result. */
function blockedTurn(text: string, sdkRateLimited = true): void {
  sdkMessages.push(
    { type: "system", subtype: "init", session_id: "s1" },
    {
      type: "assistant",
      ...(sdkRateLimited ? { error: "rate_limit" } : {}),
      message: { model: "<synthetic>", content: [{ type: "text", text }] },
    },
    { type: "result", subtype: "success", is_error: true, result: text, errors: [] },
  );
}

it("a session-limit result ships the generic text plus providerFailure claude/rate_limit", async () => {
  blockedTurn(LINE);
  const sent = await delivered();
  expect(sent).toEqual([
    {
      text: GENERIC,
      failureNotice: true,
      providerFailure: {
        provider: "claude",
        kind: "rate_limit",
        resets_at: "2026-10-21T02:00:00.000Z",
      },
    },
  ]);
});

it("the exact line counts even without the SDK's rate_limit mark", async () => {
  blockedTurn(LINE, false);
  expect((await delivered())[0]?.providerFailure).toEqual({
    provider: "claude",
    kind: "rate_limit",
    resets_at: "2026-10-21T02:00:00.000Z",
  });
});

it("an SDK rate_limit with other text reports no reset and leaks none of the text", async () => {
  blockedTurn('API Error: 429 {"error":"tenant private-customer"}');
  const sent = await delivered();
  expect(sent).toEqual([
    {
      text: GENERIC,
      failureNotice: true,
      providerFailure: { provider: "claude", kind: "rate_limit" },
    },
  ]);
  expect(JSON.stringify(sent)).not.toContain("private-customer");
});

it.each([`I think ${LINE}`, `${LINE}\nand more`])(
  "model text about limits stays a plain failure (%p)",
  async (text) => {
    blockedTurn(text, false);
    const sent = await delivered();
    expect(sent).toEqual([{ text: GENERIC, failureNotice: true }]);
  },
);

it("a successful turn that mentions the line is not a failure", async () => {
  sdkMessages.push({
    type: "result",
    subtype: "success",
    is_error: false,
    result: `<message to="main">${LINE}</message>`,
  });
  const sent = await delivered();
  expect(sent.some((m) => m.failureNotice)).toBe(false);
});

const AUTH_TEXT = 'Failed to authenticate. API Error: 401 {"error":"tenant private-customer"}';
// Only the SDK's structured mark counts, never words that resemble it.
const NOT_AUTH: Array<[string, string | undefined, string]> = [
  ["billing_error", "billing_error", "Credit balance is too low"],
  ["rate_limit", "rate_limit", "API Error: 429"],
  ["server_error", "server_error", "API Error: 500"],
  ["unknown", "unknown", "Your organization has been disabled"],
  ["no mark, auth words", undefined, AUTH_TEXT],
  ["no mark, the marker text", undefined, CLAUDE_AUTH_FAILED],
  ["no mark, quoted notice", undefined, `"error": "${CLAUDE_AUTH_FAILED}" (account on hold)`],
];

it("an authentication_failed turn errors with the fixed marker only", async () => {
  erroredTurn(CLAUDE_AUTH_FAILED, AUTH_TEXT);
  const out = await results();
  expect(out).toEqual([{ type: "result", text: null, isError: true, error: CLAUDE_AUTH_FAILED }]);
  expect(JSON.stringify(out)).not.toContain("private-customer");
});

it.each(NOT_AUTH)("%s is not an auth failure", async (_name, error, text) => {
  erroredTurn(error, text);
  const [result] = await results();
  expect(result?.error).not.toBe(CLAUDE_AUTH_FAILED);
});

it("the mark does not outlive its turn", async () => {
  erroredTurn(CLAUDE_AUTH_FAILED, AUTH_TEXT);
  sdkMessages.push(
    { type: "assistant", message: { model: "m", content: [{ type: "text", text: "x" }] } },
    { type: "result", subtype: "success", is_error: true, result: "boom", errors: ["boom"] },
  );
  const out = await results();
  expect(out.map((r) => r.error)).toEqual([CLAUDE_AUTH_FAILED, "boom"]);
});

it.skipIf(!hasProviderFailure)(
  "an authentication_failed turn ships the generic text plus providerFailure claude/auth_expired",
  async () => {
    erroredTurn(CLAUDE_AUTH_FAILED, AUTH_TEXT);
    const sent = await delivered();
    expect(sent).toEqual([
      {
        text: GENERIC,
        failureNotice: true,
        providerFailure: { provider: "claude", kind: "auth_expired" },
      },
    ]);
    expect(JSON.stringify(sent)).not.toContain("private-customer");
  },
);

it.skipIf(!hasProviderFailure).each(NOT_AUTH)(
  "%s ships no auth_expired providerFailure",
  async (_name, error, text) => {
    erroredTurn(error, text);
    const [sent] = await delivered();
    expect((sent?.providerFailure as { kind?: string } | undefined)?.kind).not.toBe("auth_expired");
  },
);
