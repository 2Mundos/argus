import { appendFileSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import {
  COMMAND_GUARD_REASONS,
  type CommandBlockedBody,
  createCommandBlockedRelay,
  readTranscriptLine,
  ruleOfBlockText,
} from "./argus-command-blocked.js";

const task = { id: "t1", attempt: 2 };
const SINCE = Date.parse("2026-11-06T10:00:00Z");
const NOW = "2026-11-06T10:05:00.000Z";
const OLD = "2026-11-05T10:00:00.000Z";

const blockText = (rule: string) =>
  `Blocked by Argus command guard: ${COMMAND_GUARD_REASONS[rule]}. Do not retry or work around this block.`;

const toolUse = (id: string, command: unknown, at: string | null = NOW) => ({
  type: "assistant",
  timestamp: at ?? undefined,
  message: { content: [{ type: "tool_use", id, name: "Bash", input: { command } }] },
});
const toolResult = (
  id: string,
  text: unknown,
  at: string | null = NOW,
  isError: boolean = true,
) => ({
  type: "user",
  timestamp: at ?? undefined,
  message: {
    content: [{ type: "tool_result", tool_use_id: id, is_error: isError, content: text }],
  },
});
const jsonl = (...recs: unknown[]) => recs.map((r) => `${JSON.stringify(r)}\n`).join("");

// The table equals `REASONS` of the hook: integrations/nanoclaw/tests/test_command_guard.py.
describe("rule table", () => {
  it("maps the hook's message back to its rule, and nothing else", () => {
    for (const rule of Object.keys(COMMAND_GUARD_REASONS))
      expect(ruleOfBlockText(blockText(rule))).toBe(rule);
    expect(ruleOfBlockText("Blocked by Argus command guard: made up. Do not retry")).toBeNull();
    expect(ruleOfBlockText(`x ${blockText("rm_root")}`)).toBeNull();
    expect(ruleOfBlockText("")).toBeNull();
  });
});

describe("readTranscriptLine", () => {
  const read = (lines: unknown[]) => {
    const commands = new Map<string, string>();
    return lines
      .map((l) => readTranscriptLine(JSON.stringify(l), commands, task, SINCE))
      .filter(Boolean);
  };

  it("pairs a denied tool_result with its command by tool_use id", () => {
    expect(
      read([toolUse("toolu_1", "rm -rf /"), toolResult("toolu_1", blockText("rm_root"))]),
    ).toEqual([{ attempt: 2, tool_use_id: "toolu_1", rule: "rm_root", command: "rm -rf /" }]);
  });

  it("reads text blocks in the result content", () => {
    const content = [{ type: "text", text: blockText("dd_device") }];
    expect(read([toolUse("a", "dd of=/dev/sda"), toolResult("a", content)])[0]).toMatchObject({
      rule: "dd_device",
    });
  });

  it.each([
    [
      "a result that is not an error",
      [toolUse("a", "x"), toolResult("a", blockText("rm_root"), NOW, false)],
    ],
    ["other error text", [toolUse("a", "x"), toolResult("a", "exit code 1")]],
    ["a result without its tool_use", [toolResult("zzz", blockText("rm_root"))]],
    ["a result before its tool_use", [toolResult("a", blockText("rm_root")), toolUse("a", "x")]],
    [
      "records before the claim",
      [toolUse("a", "x", OLD), toolResult("a", blockText("rm_root"), OLD)],
    ],
    [
      "records without a timestamp",
      [toolUse("a", "x", null), toolResult("a", blockText("rm_root"), null)],
    ],
    ["a non-string command", [toolUse("a", 7), toolResult("a", blockText("rm_root"))]],
    ["an id the API would refuse", [toolUse("a b", "x"), toolResult("a b", blockText("rm_root"))]],
  ])("ignores %s", (_name, lines) => {
    expect(read(lines)).toEqual([]);
  });

  it("ignores torn and unrelated lines", () => {
    const commands = new Map<string, string>();
    for (const line of ["", "{torn", '{"type":"system"}', "[]", '"tool_use"'])
      expect(readTranscriptLine(line, commands, task, SINCE)).toBeNull();
  });
});

describe("relay", () => {
  const dirs: string[] = [];
  afterEach(() => {
    for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
  });

  function setup(post: (b: CommandBlockedBody) => Promise<unknown>) {
    const root = mkdtempSync(path.join(os.tmpdir(), "argus-command-blocked-"));
    dirs.push(root);
    mkdirSync(path.join(root, "-workspace"));
    const file = path.join(root, "-workspace", "5b1c0f3e-aaaa.jsonl");
    writeFileSync(file, "");
    const relay = createCommandBlockedRelay({
      projectsDir: async () => root,
      since: () => SINCE,
      task,
      post,
    });
    return { file, relay };
  }

  const denial = (id: string, rule = "rm_root", command = "rm -rf /") =>
    jsonl(toolUse(id, command), toolResult(id, blockText(rule)));

  it("posts each denial once, across polls and a torn line", async () => {
    const posted: CommandBlockedBody[] = [];
    const { file, relay } = setup(async (b) => void posted.push(b));
    appendFileSync(file, denial("toolu_1"));
    const whole = denial("toolu_2", "device_redirect", "echo x > /dev/sda");
    appendFileSync(file, whole.slice(0, 40)); // the runner is mid-write
    await relay.poll();
    expect(posted.map((b) => b.tool_use_id)).toEqual(["toolu_1"]);
    appendFileSync(file, whole.slice(40));
    await relay.poll();
    await relay.poll();
    expect(posted).toEqual([
      { attempt: 2, tool_use_id: "toolu_1", rule: "rm_root", command: "rm -rf /" },
      {
        attempt: 2,
        tool_use_id: "toolu_2",
        rule: "device_redirect",
        command: "echo x > /dev/sda",
      },
    ]);
  });

  it("does not post a denial of an earlier attempt in a resumed session", async () => {
    const posted: CommandBlockedBody[] = [];
    const { file, relay } = setup(async (b) => void posted.push(b));
    appendFileSync(
      file,
      jsonl(toolUse("old", "rm -rf ~", OLD), toolResult("old", blockText("rm_root"), OLD)),
    );
    await relay.poll();
    expect(posted).toEqual([]);
  });

  it("keeps a denial for the next poll after a retryable failure", async () => {
    const posted: CommandBlockedBody[] = [];
    let fail: unknown = { status: 503 };
    const { file, relay } = setup(async (b) => {
      if (fail) throw fail;
      posted.push(b);
    });
    appendFileSync(file, denial("toolu_1"));
    await relay.poll();
    expect(posted).toEqual([]);
    fail = null;
    await relay.poll();
    expect(posted.map((b) => b.tool_use_id)).toEqual(["toolu_1"]);
  });

  it("drops a denial the server can never accept and goes on", async () => {
    const calls: string[] = [];
    const { file, relay } = setup(async (b) => {
      calls.push(b.tool_use_id);
      if (b.tool_use_id === "bad") throw { status: 422, code: "unknown_rule" };
    });
    appendFileSync(file, denial("bad") + denial("good"));
    await relay.poll();
    await relay.poll();
    expect(calls).toEqual(["bad", "good"]);
    expect(relay.stopped).toBe(false);
  });

  it.each([
    [{ status: 409, code: "stale_attempt" }],
    [{ status: 409, code: "not_assignee" }],
    [{ status: 401 }],
  ])("stops for good when the claim is lost (%j)", async (err) => {
    let calls = 0;
    const { file, relay } = setup(async () => {
      calls++;
      throw err;
    });
    appendFileSync(file, denial("toolu_1"));
    await relay.poll();
    appendFileSync(file, denial("toolu_2"));
    await relay.poll();
    expect(relay.stopped).toBe(true);
    expect(calls).toBe(1);
  });

  it("does nothing without a transcript directory", async () => {
    let calls = 0;
    const relay = createCommandBlockedRelay({
      projectsDir: async () => null,
      since: () => SINCE,
      task,
      post: async () => void calls++,
    });
    await relay.poll();
    expect(calls).toBe(0);
  });
});
