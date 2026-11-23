import fs from "fs";
import os from "os";
import path from "path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { MuseAcpError, MuseProvider } from "./agent-runner/src/providers/muse.js";
import type { AgentQuery, ProviderEvent } from "./agent-runner/src/providers/types.js";

// Stands in for `muse acp`, with the wire shapes recorded from Muse 1.4.25.
const FAKE_ACP = `
const fs = require("fs");
const readline = require("readline");
const scenario = process.env.FAKE_SCENARIO;
const log = (entry) => fs.appendFileSync(process.env.FAKE_LOG, JSON.stringify(entry) + "\\n");
const send = (m) => process.stdout.write(JSON.stringify({ jsonrpc: "2.0", ...m }) + "\\n");
const update = (sessionId, u) => send({ method: "session/update", params: { sessionId, update: u } });
const chunk = (sessionId, text, messageId) =>
  update(sessionId, { sessionUpdate: "agent_message_chunk", messageId, content: { type: "text", text } });
log({ argv: process.argv.slice(2), dataHome: process.env.XDG_DATA_HOME });
let promptId;
readline.createInterface({ input: process.stdin }).on("line", (line) => {
  const m = JSON.parse(line);
  log(m);
  const p = m.params;
  if (m.id === 900) return send({ id: promptId, result: { stopReason: "end_turn" } });
  if (m.method === "initialize") return send({ id: m.id, result: { protocolVersion: 1 } });
  if (m.method === "session/new") return send({ id: m.id, result: { sessionId: "s-new" } });
  if (m.method === "session/load") {
    if (scenario === "stale") {
      return send({ id: m.id, error: { code: -32020, message: "session not found" } });
    }
    chunk(p.sessionId, "replayed reply", "m-old");
    return send({ id: m.id, result: {} });
  }
  if (m.method !== "session/prompt") return;
  promptId = m.id;
  if (scenario === "crash") process.exit(3);
  if (scenario === "refusal") return send({ id: m.id, result: { stopReason: "refusal" } });
  if (scenario === "hang") return chunk(p.sessionId, "working", "m1");
  if (scenario === "permission") {
    return send({
      id: 900,
      method: "session/request_permission",
      params: { options: [{ optionId: "no", kind: "reject_once" }, { optionId: "yes", kind: "allow_once" }] },
    });
  }
  chunk(p.sessionId, "before the tool", "m1");
  update(p.sessionId, { sessionUpdate: "tool_call", toolCallId: "t1" });
  chunk(p.sessionId, "final ", "m2");
  chunk(p.sessionId, p.prompt.at(-1).text, "m2");
  send({ id: m.id, result: { stopReason: "end_turn" } });
});
`;

let dir: string;
let logFile: string;

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), "muse-provider-"));
  logFile = path.join(dir, "log.jsonl");
  fs.writeFileSync(path.join(dir, "fake-acp.cjs"), FAKE_ACP);
});

afterEach(() => fs.rmSync(dir, { recursive: true, force: true }));

function provider(scenario: string, options: Record<string, unknown> = {}): MuseProvider {
  return new MuseProvider(
    {
      model: "muse-spark-1.3",
      env: { FAKE_SCENARIO: scenario, FAKE_LOG: logFile },
      ...options,
    },
    {
      command: process.execPath,
      commandArgs: [path.join(dir, "fake-acp.cjs")],
      agentDir: dir,
      dataDir: path.join(dir, "data"),
    },
  );
}

async function collect(query: AgentQuery): Promise<{ events: ProviderEvent[]; error?: Error }> {
  const events: ProviderEvent[] = [];
  try {
    for await (const event of query.events) events.push(event);
  } catch (error) {
    return { events, error: error as Error };
  }
  return { events };
}

const logged = () =>
  fs
    .readFileSync(logFile, "utf8")
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));
const calls = (method: string) => logged().filter((entry) => entry.method === method);
const results = (events: ProviderEvent[]) =>
  events.flatMap((event) => (event.type === "result" ? [event.text] : []));

describe("Muse ACP provider", () => {
  it("starts a session with context once and returns each turn's last message", async () => {
    const query = provider("ok", {
      effort: "high",
      mcpServers: {
        nanoclaw: { command: "bun", args: ["run", "mcp.ts"], env: { A: "1" } },
        scoped: { command: "tool", cwd: "/plugin" },
        remote: { type: "http", url: "https://mcp.test", headers: { H: "v" } },
      },
    }).query({
      prompt: "first",
      cwd: dir,
      systemContext: { instructions: "runner instructions" },
    });
    query.push("second");
    query.end();

    const { events, error } = await collect(query);

    expect(error).toBeUndefined();
    expect(events[0]).toEqual({ type: "init", continuation: "s-new" });
    expect(results(events)).toEqual(["final first", "final second"]);
    expect(logged()[0]).toMatchObject({
      argv: [
        "acp",
        "--model",
        "muse-spark-1.3",
        "--disable-sandbox",
        "--trust-workspace",
        "--disable-request-user-input",
        "-c",
        'reasoning_effort="high"',
      ],
      dataHome: path.join(dir, "data"),
    });
    expect(calls("session/new")[0].params.mcpServers).toEqual([
      {
        name: "nanoclaw",
        command: "bun",
        args: ["run", "mcp.ts"],
        env: [{ name: "A", value: "1" }],
      },
      {
        name: "scoped",
        command: "/bin/sh",
        args: ["-c", 'cd "$0" && exec "$@"', "/plugin", "tool"],
        env: [],
      },
      {
        type: "http",
        name: "remote",
        url: "https://mcp.test",
        headers: [{ name: "H", value: "v" }],
      },
    ]);
    const [opening, followUp] = calls("session/prompt").map((call) => call.params.prompt);
    expect(opening).toHaveLength(2);
    expect(opening[0].text).toContain("runner instructions");
    expect(opening[0].text).toContain("## Memory");
    expect(opening[1].text).toBe("first");
    expect(followUp).toEqual([{ type: "text", text: "second" }]);
  });

  it("resumes a stored session without replayed output or repeated context", async () => {
    const query = provider("ok").query({ prompt: "next", cwd: dir, continuation: "s-old" });
    query.end();

    const { events } = await collect(query);

    expect(events[0]).toEqual({ type: "init", continuation: "s-old" });
    expect(results(events)).toEqual(["final next"]);
    expect(calls("session/new")).toEqual([]);
    expect(calls("session/prompt")[0].params.prompt).toEqual([{ type: "text", text: "next" }]);
  });

  it("replaces a session the host no longer holds, keeping the message", async () => {
    const query = provider("stale").query({ prompt: "next", cwd: dir, continuation: "s-gone" });
    query.end();

    const { events, error } = await collect(query);

    expect(error).toBeUndefined();
    expect(events[0]).toEqual({ type: "init", continuation: "s-new" });
    expect(results(events)).toEqual(["final next"]);
    expect(calls("session/prompt")[0].params.prompt[0].text).toContain("## Memory");
  });

  it("answers a tool permission request with the allow-once option", async () => {
    const query = provider("permission").query({ prompt: "go", cwd: dir });
    query.end();

    await collect(query);

    expect(logged().find((entry) => entry.id === 900).result).toEqual({
      outcome: { outcome: "selected", optionId: "yes" },
    });
  });

  it.each([
    ["crash", /muse acp exited \(3\)/],
    ["refusal", /Muse stopped the turn: refusal/],
  ])("fails the turn on %s", async (scenario, expected) => {
    const query = provider(scenario).query({ prompt: "go", cwd: dir });
    query.end();

    const { events, error } = await collect(query);

    expect(error?.message).toMatch(expected);
    expect(events.at(-1)).toMatchObject({ type: "error", retryable: false });
    expect(results(events)).toEqual([]);
  });

  it("refuses to run without an explicit model and starts nothing", async () => {
    const query = provider("ok", { model: undefined }).query({ prompt: "go", cwd: dir });

    const { error } = await collect(query);

    expect(error?.message).toBe("muse needs an explicit model");
    expect(fs.existsSync(logFile)).toBe(false);
  });

  it("ends an aborted turn without a result or an error", async () => {
    const query = provider("hang").query({ prompt: "go", cwd: dir });
    const events: ProviderEvent[] = [];

    for await (const event of query.events) {
      events.push(event);
      if (event.type === "activity") query.abort();
    }

    expect(events.map((event) => event.type)).toEqual(["init", "activity"]);
  });

  it("treats only the session-not-found code as an invalid session", () => {
    const muse = provider("ok");
    expect(muse.isSessionInvalid(new MuseAcpError("gone", -32020))).toBe(true);
    expect(muse.isSessionInvalid(new MuseAcpError("other", -32000))).toBe(false);
    expect(muse.isSessionInvalid(new Error("session not found"))).toBe(false);
  });
});
