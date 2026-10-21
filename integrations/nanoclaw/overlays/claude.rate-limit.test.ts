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
