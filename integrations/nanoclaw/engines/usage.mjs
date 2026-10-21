// Token and cost accounting for one delegated CLI run (docs/specs/account-usage.spec.md 3.8).
//
// parseUsage(executor, stdoutText) reads only what the CLI printed and never estimates:
// no recognizable usage means { measurement: "not_reported" }, never zeros.
//
// Field rule (one rule for every executor, so accounts compare):
//   input_tokens        = prompt tokens that were NOT served from cache
//                         = fresh input + cache-creation (cache-write) tokens
//   cached_input_tokens = cache-READ tokens
//   output_tokens       = completion tokens (reasoning tokens as the CLI counts them)
// Multi-turn runs are summed. cost_usd is the CLI's own figure, or null.
//
// Provenance of the parsers:
//   claude   - verified against a real `claude -p --output-format json` result
//              (fixtures/claude-result.json, trimmed).
//   codex, agent, opencode - SYNTHETIC / UNVERIFIED: written from the documented event
//              shapes, with no real sample yet. Their fixtures in usage.test.mjs are
//              hand-made. Treat a wrong guess as "not_reported", never as a number.
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

const MAX_RUN_KEY = 200;

/** Where usage records wait for the NanoClaw host, which holds the Argus token (spec 3.8). */
export const USAGE_RELAY_FILE = "argus-usage.jsonl";
export const USAGE_RELAY_MAX_BYTES = 4 * 1024 * 1024; // per file; further records are dropped

/** A finite, non-negative number or null. */
const num = (x) => (typeof x === "number" && Number.isFinite(x) && x >= 0 ? x : null);
const isObject = (x) => x !== null && typeof x === "object" && !Array.isArray(x);
const first = (o, ...names) => {
  for (const n of names) {
    const v = num(o?.[n]);
    if (v !== null) return v;
  }
  return null;
};
const notReported = (reason) => ({ measurement: "not_reported", reason });

/** Every JSON object in the text: the whole text, else one per line. */
function jsonObjects(text) {
  const trimmed = text.trim();
  if (!trimmed) return [];
  try {
    const whole = JSON.parse(trimmed);
    if (isObject(whole)) return [whole];
    if (Array.isArray(whole)) return whole.filter(isObject);
  } catch {
    /* JSONL or garbage */
  }
  const out = [];
  for (const line of trimmed.split("\n")) {
    const l = line.trim();
    if (!l.startsWith("{")) continue;
    try {
      const o = JSON.parse(l);
      if (isObject(o)) out.push(o);
    } catch {
      /* skip a broken line */
    }
  }
  return out;
}

/** Add per-turn figures; a figure is null until some turn reported it. */
function sumTokens(parts) {
  const total = { input: 0, output: 0, cached: 0, cost: null, turns: 0 };
  for (const p of parts) {
    total.input += p.input;
    total.output += p.output;
    total.cached += p.cached;
    if (p.cost !== null && p.cost !== undefined) total.cost = (total.cost ?? 0) + p.cost;
    total.turns += p.turns ?? 1;
  }
  return total;
}

function measured(sum, model, turns = sum.turns) {
  return {
    measurement: "measured",
    input_tokens: Math.round(sum.input),
    output_tokens: Math.round(sum.output),
    cached_input_tokens: Math.round(sum.cached),
    cost_usd: sum.cost,
    model: typeof model === "string" && model ? model.slice(0, 100) : null,
    turns,
  };
}

/** claude: one result object; modelUsage (per model, includes helper models) matches total_cost_usd. */
function parseClaude(objects) {
  const result = [...objects].reverse().find((o) => o.type === "result" || "usage" in o);
  if (!result) return notReported("no_result_object");
  const cost = num(result.total_cost_usd);
  const perModel = isObject(result.modelUsage) ? Object.entries(result.modelUsage) : [];
  const parts = [];
  for (const [name, u] of perModel) {
    const input = first(u, "inputTokens");
    const output = first(u, "outputTokens");
    if (input === null || output === null) continue;
    parts.push({
      name,
      input: input + (first(u, "cacheCreationInputTokens") ?? 0),
      output,
      cached: first(u, "cacheReadInputTokens") ?? 0,
      cost: null,
    });
  }
  if (parts.length) {
    const top = parts.reduce((a, b) =>
      b.input + b.output + b.cached > a.input + a.output + a.cached ? b : a,
    );
    const sum = sumTokens(parts);
    sum.cost = cost;
    return measured(sum, top.name, num(result.num_turns) ?? 1);
  }
  const u = result.usage;
  const input = first(u, "input_tokens");
  const output = first(u, "output_tokens");
  if (input === null || output === null) return notReported("no_usage_in_result");
  const sum = sumTokens([
    {
      input: input + (first(u, "cache_creation_input_tokens") ?? 0),
      output,
      cached: first(u, "cache_read_input_tokens") ?? 0,
      cost,
    },
  ]);
  return measured(sum, null, num(result.num_turns) ?? 1);
}

/** codex exec --json: `turn.completed` events carry per-turn usage; input_tokens includes the cached part. */
function parseCodex(objects) {
  const parts = [];
  for (const o of objects) {
    if (o.type !== "turn.completed" || !isObject(o.usage)) continue;
    const input = first(o.usage, "input_tokens");
    const output = first(o.usage, "output_tokens");
    if (input === null || output === null) continue;
    const cached = Math.min(first(o.usage, "cached_input_tokens") ?? 0, input);
    parts.push({ input: input - cached, output, cached, cost: null });
  }
  return parts.length ? measured(sumTokens(parts), null) : notReported("no_turn_completed_usage");
}

/** cursor agent: the final result object's usage, camelCase (snake_case tolerated). */
function parseAgent(objects) {
  const result = [...objects].reverse().find((o) => isObject(o.usage));
  if (!result) return notReported("no_usage_in_result");
  const u = result.usage;
  const input = first(u, "inputTokens", "input_tokens");
  const output = first(u, "outputTokens", "output_tokens");
  if (input === null || output === null) return notReported("no_usage_in_result");
  const sum = sumTokens([
    {
      input: input + (first(u, "cacheWriteTokens", "cache_write_tokens") ?? 0),
      output,
      cached: first(u, "cacheReadTokens", "cache_read_tokens") ?? 0,
      cost: null,
    },
  ]);
  return measured(sum, typeof result.model === "string" ? result.model : null, 1);
}

/** opencode run --format json: each `step_finish` carries tokens {input, output, reasoning, cache:{read, write}}. */
function parseOpencode(objects) {
  const parts = [];
  for (const o of objects) {
    if (o.type !== "step_finish" && o.type !== "step-finish" && o.part?.type !== "step-finish")
      continue;
    const body = isObject(o.part) ? o.part : o;
    const t = body.tokens;
    const input = first(t, "input");
    const output = first(t, "output");
    if (input === null || output === null) continue;
    parts.push({
      input: input + (first(t?.cache, "write") ?? 0),
      output,
      cached: first(t?.cache, "read") ?? 0,
      cost: num(body.cost),
    });
  }
  return parts.length ? measured(sumTokens(parts), null) : notReported("no_step_finish_tokens");
}

const PARSERS = {
  claude: parseClaude,
  codex: parseCodex,
  agent: parseAgent,
  opencode: parseOpencode,
};

/** Never throws; anything unrecognizable is `not_reported` with a reason code. */
export function parseUsage(executor, stdoutText) {
  const parser = PARSERS[executor];
  if (!parser) return notReported("unknown_executor");
  try {
    const objects = jsonObjects(String(stdoutText ?? ""));
    if (!objects.length) return notReported("no_json_output");
    return parser(objects);
  } catch {
    return notReported("unparseable_output");
  }
}

const part = (x) => String(x ?? "none").replace(/[^A-Za-z0-9._-]/g, "_");

/**
 * Stable idempotency key for one run: the same inputs give the same key, so a retried
 * report is deduplicated by the API (`run_key`). Built from identifiers only.
 */
export function runKey({ taskId, agentId, engine, executor, startedAt }) {
  const key = ["r1", taskId, agentId, engine, executor, startedAt].map(part).join(":");
  return key.length <= MAX_RUN_KEY ? key : `r1:${createHash("sha256").update(key).digest("hex")}`;
}

/** Short filename-safe digest of a run key (spool file name). */
export const runKeyHash = (key) => createHash("sha256").update(key).digest("hex").slice(0, 16);

/**
 * Append one usage record for the host to post: identifiers and figures only, never a
 * credential. `record` is `{source, role, task_id, attempt, account_id, payload}`. Returns
 * true when the record is on disk; never throws (a measurement must not break a delivery).
 */
export function appendUsageRecord(file, record) {
  try {
    const line = `${JSON.stringify({ v: 1, kind: "usage", ...record })}\n`;
    fs.mkdirSync(path.dirname(file), { recursive: true });
    if (fs.existsSync(file) && fs.statSync(file).size + line.length > USAGE_RELAY_MAX_BYTES)
      return false;
    fs.appendFileSync(file, line, { mode: 0o660 });
    return true;
  } catch {
    return false;
  }
}
