import fs from "fs";
import os from "os";
import path from "path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  AGY_SENTINEL_API_KEY,
  AgyAcpError,
  AgyProvider,
  stageAgyAuth,
} from "./agent-runner/src/providers/agy.js";
import type { AgentQuery, ProviderEvent } from "./agent-runner/src/providers/types.js";

// Stands in for `agy acp`, with the wire shapes recorded from agy 1.5.12.
const FAKE_ACP = `
const fs = require("fs");
const path = require("path");
const readline = require("readline");
const scenario = process.env.FAKE_SCENARIO;
const log = (entry) => fs.appendFileSync(process.env.FAKE_LOG, JSON.stringify(entry) + "\\n");
const send = (m) => process.stdout.write(JSON.stringify({ jsonrpc: "2.0", ...m }) + "\\n");
const update = (sessionId, u) => send({ method: "session/update", params: { sessionId, update: u } });
const chunk = (sessionId, text, messageId) =>
  update(sessionId, { sessionUpdate: "agent_message_chunk", messageId, content: { type: "text", text } });
const geminiDirIdx = process.argv.indexOf("--gemini-dir");
const geminiDir = geminiDirIdx >= 0 ? process.argv[geminiDirIdx + 1] : null;
const cliDir = geminiDir ? path.join(geminiDir, "antigravity-cli") : null;
log({
  argv: process.argv.slice(2),
  home: process.env.HOME,
  noBrowser: process.env.NO_BROWSER,
  hasGeminiKey: Boolean(process.env.GEMINI_API_KEY),
  hasOauthStubEnv: Boolean(process.env.ARGUS_AGY_OAUTH_STUB),
  settings: cliDir && fs.existsSync(path.join(cliDir, "settings.json"))
    ? JSON.parse(fs.readFileSync(path.join(cliDir, "settings.json"), "utf8"))
    : null,
  hasStagedToken: Boolean(cliDir && fs.existsSync(path.join(cliDir, "antigravity-oauth-token"))),
});
let promptId;
readline.createInterface({ input: process.stdin }).on("line", (line) => {
  const m = JSON.parse(line);
  log(m);
  const p = m.params;
  if (m.id === 900) return send({ id: promptId, result: { stopReason: "end_turn" } });
  if (m.method === "initialize") return send({ id: m.id, result: { protocolVersion: 1 } });
  if (m.method === "session/new") return send({ id: m.id, result: { sessionId: "agy-new" } });
  if (m.method === "session/load") {
    if (scenario === "stale") {
      return send({ id: m.id, error: { code: -32002, message: "Session not found" } });
    }
    chunk(p.sessionId, "replayed reply", "m-old");
    return send({ id: m.id, result: {} });
  }
  if (m.method === "session/set_config_option") {
    if (scenario === "bad_effort" && p.configId === "thought_level") {
      return send({ id: m.id, error: { code: -32602, message: "Thinking level unsupported" } });
    }
    return send({ id: m.id, result: { configOptions: [] } });
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
      params: {
        options: [
          { optionId: "no", kind: "reject_once" },
          { optionId: "yes", kind: "allow_once" },
        ],
      },
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
let dataDir: string;
let stubFile: string;

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), "agy-provider-"));
  logFile = path.join(dir, "log.jsonl");
  dataDir = path.join(dir, "agy-data");
  stubFile = path.join(dir, "missing-oauth-stub");
  fs.writeFileSync(path.join(dir, "fake-acp.cjs"), FAKE_ACP);
});

afterEach(() => fs.rmSync(dir, { recursive: true, force: true }));

function provider(scenario: string, options: Record<string, unknown> = {}): AgyProvider {
  const env = {
    FAKE_SCENARIO: scenario,
    FAKE_LOG: logFile,
    GEMINI_API_KEY: AGY_SENTINEL_API_KEY,
    ...(options.env as Record<string, string | undefined> | undefined),
  };
  return new AgyProvider(
    {
      model: "gemini-4-neon",
      ...options,
      env,
    },
    {
      command: process.execPath,
      commandArgs: [path.join(dir, "fake-acp.cjs")],
      agentDir: dir,
      dataDir,
      oauthStubPath: stubFile,
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

describe("Agy ACP provider", () => {
  it("stages API key mode, configures model and effort, and returns each turn's last message", async () => {
    const query = provider("ok", {
      effort: "xhigh",
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
    expect(events[0]).toEqual({ type: "init", continuation: "agy-new" });
    expect(results(events)).toEqual(["final first", "final second"]);

    const startup = logged()[0];
    expect(startup.argv).toEqual([
      "acp",
      "--dangerously-skip-permissions",
      "--disable-telemetry",
      "--gemini-dir",
      dataDir,
    ]);
    expect(startup.home).toBe(process.env.HOME);
    expect(startup.noBrowser).toBe("1");
    expect(startup.hasGeminiKey).toBe(true);
    expect(startup.settings).toEqual({ modelProvider: "gemini" });
    expect(startup.hasStagedToken).toBe(false);

    const created = calls("session/new")[0];
    const appended = created.params._meta?.antigravity?.systemPrompt?.append;
    expect(appended).toContain("runner instructions");
    expect(appended).toContain("## Memory");
    expect(created.params.mcpServers).toEqual([
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

    expect(calls("session/set_config_option").map((c) => c.params)).toEqual([
      { sessionId: "agy-new", configId: "model", value: "gemini-4-neon" },
      { sessionId: "agy-new", configId: "thought_level", value: "xhigh" },
    ]);
  });

  it("stages OAuth mode, scrubs GEMINI_API_KEY, and clears stale modelProvider", async () => {
    // Seed a prior API-key settings.json to verify switching to OAuth clears modelProvider.
    const cliDir = path.join(dataDir, "antigravity-cli");
    fs.mkdirSync(cliDir, { recursive: true });
    fs.writeFileSync(
      path.join(cliDir, "settings.json"),
      JSON.stringify({ modelProvider: "gemini", customFlag: true }),
    );
    const oauthStub = path.join(dir, "oauth-stub.json");
    fs.writeFileSync(oauthStub, JSON.stringify({ token: { access_token: "ya29.SENTINEL" } }));

    const query = provider("ok", {
      env: { GEMINI_API_KEY: undefined, ARGUS_AGY_OAUTH_STUB: oauthStub },
    }).query({ prompt: "hello", cwd: dir });
    query.end();

    const { events, error } = await collect(query);
    expect(error).toBeUndefined();
    expect(results(events)).toEqual(["final hello"]);

    const startup = logged()[0];
    expect(startup.hasGeminiKey).toBe(false);
    expect(startup.hasOauthStubEnv).toBe(false);
    expect(startup.hasStagedToken).toBe(true);
    expect(startup.settings).toEqual({ customFlag: true });
  });

  it("fails closed when neither gateway sentinel nor OAuth stub is present", async () => {
    expect(() =>
      stageAgyAuth({ GEMINI_API_KEY: "AIzaSyREAL_AMBIENT_KEY" }, dataDir, stubFile),
    ).toThrow(/no usable google account/);
    const query = provider("ok", { env: { GEMINI_API_KEY: undefined } }).query({
      prompt: "hello",
      cwd: dir,
    });
    query.end();
    const { events, error } = await collect(query);
    expect(error).toBeInstanceOf(AgyAcpError);
    expect(error?.message).toMatch(/no usable google account/);
    expect(events.at(-1)).toMatchObject({ type: "error", retryable: false });
    expect(fs.existsSync(logFile)).toBe(false);
  });

  it("resumes a stored session without replaying old output, and falls back on -32002", async () => {
    const resumed = provider("ok").query({ prompt: "next", cwd: dir, continuation: "agy-old" });
    resumed.end();
    const r1 = await collect(resumed);
    expect(r1.error).toBeUndefined();
    expect(r1.events[0]).toEqual({ type: "init", continuation: "agy-old" });
    expect(results(r1.events)).toEqual(["final next"]);
    expect(calls("session/load")).toHaveLength(1);
    expect(calls("session/new")).toHaveLength(0);

    fs.rmSync(logFile, { force: true });
    const stale = provider("stale").query({ prompt: "fresh", cwd: dir, continuation: "agy-gone" });
    stale.end();
    const r2 = await collect(stale);
    expect(r2.error).toBeUndefined();
    expect(r2.events[0]).toEqual({ type: "init", continuation: "agy-new" });
    expect(results(r2.events)).toEqual(["final fresh"]);
    expect(calls("session/load")).toHaveLength(1);
    expect(calls("session/new")).toHaveLength(1);
  });

  it("allows tool permission requests, requires explicit model, and surfaces errors", async () => {
    const perm = provider("permission").query({ prompt: "run", cwd: dir });
    perm.end();
    expect((await collect(perm)).error).toBeUndefined();

    const noModel = new AgyProvider(
      { env: { GEMINI_API_KEY: AGY_SENTINEL_API_KEY } },
      {
        command: process.execPath,
        commandArgs: [],
        agentDir: dir,
        dataDir,
        oauthStubPath: stubFile,
      },
    ).query({ prompt: "hi", cwd: dir });
    noModel.end();
    expect((await collect(noModel)).error?.message).toMatch(/explicit model/);

    const badEffort = provider("bad_effort", { effort: "max" }).query({ prompt: "hi", cwd: dir });
    badEffort.end();
    expect((await collect(badEffort)).error?.message).toMatch(/Thinking level unsupported/);

    const refusal = provider("refusal").query({ prompt: "hi", cwd: dir });
    refusal.end();
    expect((await collect(refusal)).error?.message).toMatch(/refusal/);

    const crash = provider("crash").query({ prompt: "hi", cwd: dir });
    crash.end();
    expect((await collect(crash)).error?.message).toMatch(/exited \(3\)/);
  });

  it("ends an aborted turn without a result or an error", async () => {
    const query = provider("hang").query({ prompt: "wait", cwd: dir });
    const seen: ProviderEvent[] = [];
    for await (const event of query.events) {
      seen.push(event);
      if (event.type === "activity") query.abort();
    }
    expect(seen.map((event) => event.type)).toEqual(["init", "activity"]);
  });
});
