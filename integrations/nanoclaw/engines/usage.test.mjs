import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { parseUsage, runKey, runKeyHash } from "./usage.mjs";

const claudeReal = fs.readFileSync(
  new URL("./fixtures/claude-result.json", import.meta.url),
  "utf8",
);

test("claude: real result sums every model; cache reads and cache writes follow the rule", () => {
  const u = parseUsage("claude", claudeReal);
  assert.deepEqual(u, {
    measurement: "measured",
    // input = fresh + cache creation: (44 + 59005) + (2588 + 0)
    input_tokens: 61637,
    output_tokens: 18073,
    cached_input_tokens: 1156589,
    cost_usd: 0.5325657,
    model: "claude-sonnet-5-5",
    turns: 22,
  });
});

test("claude: without modelUsage the top-level usage is used", () => {
  const u = parseUsage(
    "claude",
    JSON.stringify({
      type: "result",
      total_cost_usd: 0.01,
      num_turns: 2,
      usage: {
        input_tokens: 10,
        output_tokens: 5,
        cache_read_input_tokens: 100,
        cache_creation_input_tokens: 7,
      },
    }),
  );
  assert.deepEqual(
    [u.input_tokens, u.output_tokens, u.cached_input_tokens, u.cost_usd, u.model, u.turns],
    [17, 5, 100, 0.01, null, 2],
  );
});

// codex, agent and opencode fixtures below are SYNTHETIC (documented shapes, no real sample).
test("codex (synthetic): turn.completed usage is summed across turns; cached is not double counted", () => {
  const out = [
    '{"type":"thread.started","thread_id":"x"}',
    '{"type":"turn.completed","usage":{"input_tokens":1000,"cached_input_tokens":800,"output_tokens":50}}',
    "not json at all",
    '{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}',
    '{"type":"turn.completed","usage":{"input_tokens":2000,"cached_input_tokens":1500,"output_tokens":70}}',
  ].join("\n");
  assert.deepEqual(parseUsage("codex", out), {
    measurement: "measured",
    input_tokens: 700, // (1000-800) + (2000-1500)
    output_tokens: 120,
    cached_input_tokens: 2300,
    cost_usd: null,
    model: null,
    turns: 2,
  });
});

test("agent (synthetic): result object usage, cache writes counted as input", () => {
  const out = JSON.stringify({
    type: "result",
    is_error: false,
    result: "ok",
    usage: { inputTokens: 300, outputTokens: 40, cacheReadTokens: 900, cacheWriteTokens: 10 },
  });
  const u = parseUsage("agent", out);
  assert.deepEqual(
    [u.measurement, u.input_tokens, u.output_tokens, u.cached_input_tokens, u.cost_usd],
    ["measured", 310, 40, 900, null],
  );
});

test("opencode (synthetic): step-finish tokens and cost are summed", () => {
  const step = (input, output, read, write, cost) =>
    JSON.stringify({
      type: "step_finish",
      part: {
        type: "step-finish",
        cost,
        tokens: { input, output, reasoning: 0, cache: { read, write } },
      },
    });
  const out = [
    step(100, 10, 50, 5, 0.01),
    '{"type":"text","part":{"text":"x"}}',
    step(200, 20, 60, 0, 0.02),
  ].join("\n");
  const u = parseUsage("opencode", out);
  assert.deepEqual(
    [u.input_tokens, u.output_tokens, u.cached_input_tokens, u.turns],
    [305, 30, 110, 2],
  );
  assert.ok(Math.abs(u.cost_usd - 0.03) < 1e-9);
});

test("absent, partial or garbage usage is not_reported, never zeros", () => {
  for (const [executor, text] of [
    ["claude", ""],
    ["claude", "garbage {{{"],
    ["claude", '{"type":"result","result":"hi"}'],
    ["claude", '{"type":"result","usage":{"input_tokens":5}}'], // output missing: do not invent 0
    ["codex", '{"type":"thread.started"}'],
    ["codex", '{"type":"turn.completed","usage":{"input_tokens":"many"}}'],
    ["agent", '{"type":"result","result":"hi"}'],
    ["opencode", '{"type":"step_finish","part":{"tokens":{}}}'],
    ["unknown", "{}"],
  ]) {
    const u = parseUsage(executor, text);
    assert.equal(u.measurement, "not_reported", `${executor}: ${text}`);
    assert.match(u.reason, /^[a-z_]+$/);
    assert.equal("input_tokens" in u, false);
  }
  assert.equal(parseUsage("claude", undefined).measurement, "not_reported");
  assert.equal(
    parseUsage("claude", '{"usage":{"input_tokens":-1,"output_tokens":2}}').measurement,
    "not_reported",
  );
});

test("run_key is stable, distinguishes runs and stays within the API limit", () => {
  const parts = {
    taskId: "t1",
    agentId: "agent-1",
    engine: "claude",
    executor: "codex",
    startedAt: "2026-10-20T10:00:00.000Z",
  };
  const a = runKey(parts);
  assert.equal(a, runKey({ ...parts }));
  assert.equal(a, "r1:t1:agent-1:claude:codex:2026-10-20T10_00_00.000Z");
  assert.notEqual(a, runKey({ ...parts, startedAt: "2026-10-20T10:00:00.001Z" }));
  assert.notEqual(a, runKey({ ...parts, executor: "claude" }));
  assert.match(runKey({ ...parts, taskId: null }), /^r1:none:/);
  const long = runKey({ ...parts, taskId: "x".repeat(300) });
  assert.match(long, /^r1:[a-f0-9]{64}$/);
  assert.ok(long.length <= 200);
  assert.match(runKeyHash(a), /^[a-f0-9]{16}$/);
});
