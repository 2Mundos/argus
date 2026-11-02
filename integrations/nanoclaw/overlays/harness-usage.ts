/**
 * The agent runner's own token usage and live log, handed to the Argus host
 * (account usage spec 3.9, task-live-log spec 3.8).
 *
 * The harness (the engine in the agent container: Claude Agent SDK or Codex app-server) is
 * not an `argus-delegate` run, so nothing else sees what it spent or printed. The providers
 * call the trackers below at each finished turn (usage) and on every SDK message (live log);
 * a delta / batch that is really new is appended to a file in the agent group's directory,
 * which the NanoClaw host reads and posts with its own token. The container never holds an
 * Argus credential.
 *
 * Nothing here estimates: a turn with no figures is recorded as `not_reported` with a
 * reason, a cost the SDK does not give stays null, and every function is best-effort (a
 * measurement or log line must never break a turn).
 *
 * Field rule, as for the delegate: input_tokens = prompt tokens not served from cache
 * (fresh + cache-creation), cached_input_tokens = cache reads, output_tokens as counted.
 */
import fs from "fs";
import path from "path";

export const USAGE_RELAY_FILE = "argus-usage.jsonl";
/** Live log of the harness stream; the channel posts it as `source: "worker"`. */
export const HARNESS_LOG_FILE = "argus-harness-log.jsonl";
const DEFAULT_POLICY_FILE = "/workspace/agent/argus-model-policy.json";
const MAX_FILE_BYTES = 4 * 1024 * 1024;
const LOG_MAX_FILE_BYTES = 8 * 1024 * 1024;
const LOG_LINE_MAX = 2000;
const LOG_ARG_MAX = 120;
const LOG_RESULT_MAX = 200;

export interface Delta {
  input: number;
  output: number;
  cached: number;
  /** The SDK's own estimate for this delta, or null when it cannot be separated or is absent. */
  cost: number | null;
}

export type Observation =
  | { kind: "none" }
  | { kind: "measured"; delta: Delta; model: string | null }
  | { kind: "not_reported"; reason: string; model: string | null };

const NONE: Observation = { kind: "none" };
const isObject = (x: unknown): x is Record<string, unknown> =>
  x !== null && typeof x === "object" && !Array.isArray(x);
const num = (x: unknown): number | null =>
  typeof x === "number" && Number.isFinite(x) && x >= 0 ? x : null;
const part = (x: unknown): string => String(x ?? "none").replace(/[^A-Za-z0-9._-]/g, "_");

interface Counts {
  input: number;
  output: number;
  cached: number;
}
const minus = (a: Counts, b: Counts): Counts => ({
  input: a.input - b.input,
  output: a.output - b.output,
  cached: a.cached - b.cached,
});
const anyNegative = (c: Counts): boolean => c.input < 0 || c.output < 0 || c.cached < 0;
const isZero = (c: Counts): boolean => c.input === 0 && c.output === 0 && c.cached === 0;

// ---------------------------------------------------------------------------------------
// Claude Agent SDK. Documented (agent-sdk cost tracking): in streaming input mode one query()
// carries many turns and each turn emits a result whose
//   usage                      covers that turn only (main agent loop, not subagents),
//   modelUsage, total_cost_usd are the RUNNING TOTAL of the call, plus any spend restored when
//                              the call resumed a session; a /clear starts the totals over.
// So a result is never added up as is. The first result a call emits is measured by its own
// `usage` (whatever was restored before it is not this turn's), later ones by the growth of
// the running total (which also covers subagents), and a total that shrinks is a reset.

function claudeTurn(usage: unknown): Counts | null {
  if (!isObject(usage)) return null;
  const input = num(usage.input_tokens);
  const output = num(usage.output_tokens);
  if (input === null || output === null) return null;
  return {
    input: input + (num(usage.cache_creation_input_tokens) ?? 0),
    output,
    cached: num(usage.cache_read_input_tokens) ?? 0,
  };
}

function claudeRunning(modelUsage: unknown): { counts: Counts; model: string | null } | null {
  if (!isObject(modelUsage)) return null;
  const sum: Counts = { input: 0, output: 0, cached: 0 };
  let top: string | null = null;
  let topTokens = -1;
  let any = false;
  for (const [name, u] of Object.entries(modelUsage)) {
    if (!isObject(u)) continue;
    const input = num(u.inputTokens);
    const output = num(u.outputTokens);
    if (input === null || output === null) continue;
    const cached = num(u.cacheReadInputTokens) ?? 0;
    sum.input += input + (num(u.cacheCreationInputTokens) ?? 0);
    sum.output += output;
    sum.cached += cached;
    any = true;
    if (input + output + cached > topTokens) {
      topTokens = input + output + cached;
      top = name;
    }
  }
  return any ? { counts: sum, model: top } : null;
}

export function createClaudeUsageTracker() {
  let prev: { counts: Counts; cost: number | null } | null = null;
  const seen = new Set<string>();
  return {
    /** One SDK `result` message. */
    observe(result: unknown): Observation {
      if (!isObject(result)) return NONE;
      const uuid = typeof result.uuid === "string" ? result.uuid : null;
      if (uuid) {
        if (seen.has(uuid)) return NONE; // the same result seen twice is one turn
        seen.add(uuid);
      }
      const turn = claudeTurn(result.usage);
      const running = claudeRunning(result.modelUsage);
      const total = num(result.total_cost_usd);
      const model = running?.model ?? null;
      let counts: Counts | null = null;
      let cost: number | null = null;
      if (running && prev && !anyNegative(minus(running.counts, prev.counts))) {
        counts = minus(running.counts, prev.counts);
        cost =
          total !== null && prev.cost !== null && total >= prev.cost ? total - prev.cost : null;
      } else {
        // First result of the call, or the running total went back (reset, crash): this
        // turn's own figures, and no cost, since the total's baseline is not this turn's.
        counts = turn;
      }
      if (running) prev = { counts: running.counts, cost: total };
      if (!counts) return { kind: "not_reported", reason: "no_usage_in_result", model };
      if (isZero(counts) && (cost === null || cost === 0)) return NONE; // nothing was spent
      return { kind: "measured", delta: { ...counts, cost }, model };
    },
  };
}

// ---------------------------------------------------------------------------------------
// Codex app-server. `thread/tokenUsage/updated` carries `tokenUsage: {total, last}`, each a
// breakdown {inputTokens, cachedInputTokens, outputTokens, ...} (schema from `codex app-server
// generate-ts`, codex-cli 0.163.0). `total` is the thread's running total, so a turn is the
// growth of `total` between the turn boundaries. The first update seen has no earlier total;
// `total - last` is the total before the call it reports. inputTokens includes the cached part.

interface CodexBreakdown {
  input: number;
  cached: number;
  output: number;
}
function codexBreakdown(b: unknown): CodexBreakdown | null {
  if (!isObject(b)) return null;
  const input = num(b.inputTokens);
  const output = num(b.outputTokens);
  if (input === null || output === null) return null;
  return { input, cached: Math.min(num(b.cachedInputTokens) ?? 0, input), output };
}

export function createCodexUsageTracker() {
  let committed: CodexBreakdown | null = null; // the total at the last turn boundary
  let latest: CodexBreakdown | null = null;
  return {
    /** A `thread/tokenUsage/updated` notification's `tokenUsage`. */
    update(tokenUsage: unknown): void {
      if (!isObject(tokenUsage)) return;
      const total = codexBreakdown(tokenUsage.total);
      if (!total) return;
      if (!committed) {
        const last = codexBreakdown(tokenUsage.last);
        committed = last
          ? {
              input: Math.max(total.input - last.input, 0),
              cached: Math.max(total.cached - last.cached, 0),
              output: Math.max(total.output - last.output, 0),
            }
          : null;
        if (!committed) {
          latest = total; // no baseline: the first turn cannot be separated from history
          committed = total;
          return;
        }
      }
      latest = total;
    },
    /** `turn/completed`. */
    complete(model: string | null): Observation {
      const now = latest;
      latest = null;
      if (!now) return { kind: "not_reported", reason: "no_token_usage_update", model };
      const from = committed ?? now;
      committed = now;
      const input = now.input - now.cached - (from.input - from.cached);
      const cached = now.cached - from.cached;
      const output = now.output - from.output;
      if (input < 0 || cached < 0 || output < 0)
        return { kind: "not_reported", reason: "usage_counter_reset", model };
      if (input === 0 && cached === 0 && output === 0) return NONE;
      return { kind: "measured", delta: { input, output, cached, cost: null }, model };
    },
  };
}

// ---------------------------------------------------------------------------------------
// The record for the host.

export interface HarnessContext {
  task_id: string;
  attempt: number;
  account_id: string;
}

/** The task and engine account the host pinned for this run (argus-model-policy.json). */
export function readHarnessContext(env: NodeJS.ProcessEnv = process.env): HarnessContext | null {
  try {
    const file = env.ARGUS_MODEL_POLICY_FILE || DEFAULT_POLICY_FILE;
    const ctx = (JSON.parse(fs.readFileSync(file, "utf8")) as { context?: unknown }).context;
    if (!isObject(ctx)) return null;
    const { task_id, attempt, engine_account_id } = ctx;
    if (typeof task_id !== "string" || !task_id) return null;
    if (!Number.isSafeInteger(attempt) || (attempt as number) < 1) return null;
    if (typeof engine_account_id !== "string" || !engine_account_id) return null;
    return { task_id, attempt: attempt as number, account_id: engine_account_id };
  } catch {
    return null;
  }
}

export function usageRelayFile(env: NodeJS.ProcessEnv = process.env): string {
  return (
    env.ARGUS_USAGE_RELAY_FILE ||
    path.join(path.dirname(env.ARGUS_MODEL_POLICY_FILE || DEFAULT_POLICY_FILE), USAGE_RELAY_FILE)
  );
}

/**
 * Append one harness usage record (role `engine`). Returns true when it is on disk. A turn
 * outside an Argus task (no pinned context) is not attributable and writes nothing.
 */
export function recordHarnessUsage(
  engine: string,
  obs: Observation,
  queryId: string,
  seq: number,
  env: NodeJS.ProcessEnv = process.env,
): boolean {
  try {
    if (obs.kind === "none") return false;
    const ctx = readHarnessContext(env);
    if (!ctx) return false;
    const key = ["h1", ctx.task_id, ctx.attempt, engine, queryId, seq].map(part).join(":");
    const measured = obs.kind === "measured";
    const payload = {
      task_id: ctx.task_id,
      attempt: ctx.attempt,
      model: obs.model ? obs.model.slice(0, 100) : null,
      engine,
      role: "engine",
      measurement: measured ? "measured" : "not_reported",
      input_tokens: measured ? Math.round(obs.delta.input) : null,
      output_tokens: measured ? Math.round(obs.delta.output) : null,
      cached_input_tokens: measured ? Math.round(obs.delta.cached) : null,
      cost_usd: measured ? obs.delta.cost : null,
      reason: measured ? null : obs.reason,
      run_key: key.length <= 200 ? key : key.slice(0, 200),
    };
    const file = usageRelayFile(env);
    const line = `${JSON.stringify({
      v: 1,
      kind: "usage",
      source: "harness",
      role: "engine",
      task_id: ctx.task_id,
      attempt: ctx.attempt,
      account_id: ctx.account_id,
      payload,
    })}\n`;
    fs.mkdirSync(path.dirname(file), { recursive: true });
    if (fs.existsSync(file) && fs.statSync(file).size + line.length > MAX_FILE_BYTES) return false;
    fs.appendFileSync(file, line, { mode: 0o660 });
    return true;
  } catch {
    return false;
  }
}

/** A per-query id, so keys of two runner processes (a restart) never collide. */
export const newQueryId = (): string => Math.random().toString(16).slice(2, 10).padEnd(8, "0");

// ---------------------------------------------------------------------------------------
// Live log for the host (task-live-log spec 3.8).

export function harnessLogFile(env: NodeJS.ProcessEnv = process.env): string {
  return (
    env.ARGUS_HARNESS_LOG_FILE ||
    path.join(path.dirname(env.ARGUS_MODEL_POLICY_FILE || DEFAULT_POLICY_FILE), HARNESS_LOG_FILE)
  );
}

const cut = (s: string, n: number): string => (s.length > n ? `${s.slice(0, n - 1)}…` : s);
const oneLine = (s: unknown): string => String(s ?? "").replace(/\s+/g, " ").trim();

function toolArgs(input: unknown): string {
  if (!isObject(input)) return "";
  for (const k of ["command", "file_path", "path", "pattern", "url", "query", "description"]) {
    const v = input[k];
    if (typeof v === "string" && v) return cut(oneLine(v), LOG_ARG_MAX);
  }
  try {
    const json = JSON.stringify(input);
    return json === "{}" ? "" : cut(json, LOG_ARG_MAX);
  } catch {
    return "";
  }
}

function resultText(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content.map((c) => (isObject(c) && typeof c.text === "string" ? c.text : "")).join(" ");
}

/**
 * Short lines for one Claude Agent SDK message (same shape as stream-json). Unknown or
 * empty messages give []. Thinking and the echoed user prompt are never shown.
 */
export function harnessLogLines(ev: unknown): string[] {
  if (!isObject(ev)) return [];
  const message = isObject(ev.message) ? ev.message : null;
  const blocks = Array.isArray(message?.content) ? message.content : [];
  const lines: string[] = [];
  switch (ev.type) {
    case "system":
      if (ev.subtype === "init")
        lines.push(
          `claude started${typeof ev.model === "string" ? ` (${cut(ev.model, 80)})` : ""}`,
        );
      break;
    case "assistant":
      for (const b of blocks) {
        if (!isObject(b)) continue;
        if (b.type === "text" && typeof b.text === "string") {
          for (const l of b.text.replace(/\r\n/g, "\n").split("\n"))
            if (l.trim()) lines.push(cut(l, LOG_LINE_MAX));
        } else if (b.type === "tool_use") {
          const args = toolArgs(b.input);
          lines.push(`▸ ${String(b.name ?? "tool").slice(0, 60)}${args ? ` ${args}` : ""}`);
        }
      }
      break;
    case "user":
      for (const b of blocks) {
        if (!isObject(b) || b.type !== "tool_result") continue;
        const text = cut(oneLine(resultText(b.content)), LOG_RESULT_MAX);
        if (b.is_error) lines.push(`  ✗ error: ${text}`);
        else if (text) lines.push(`  ↳ ${text}`);
      }
      break;
    case "result":
      lines.push(
        ev.is_error
          ? `claude finished with an error: ${cut(oneLine(ev.result ?? ev.subtype ?? ""), LOG_RESULT_MAX)}`
          : "claude finished",
      );
      break;
    default:
  }
  return lines;
}

/**
 * Append readable lines for one harness SDK message. Returns true when anything was
 * written. Outside a pinned task context, or for a message with nothing to show, writes
 * nothing. Best effort: never throws into the runner.
 */
export function recordHarnessLog(
  ev: unknown,
  env: NodeJS.ProcessEnv = process.env,
): boolean {
  try {
    const ctx = readHarnessContext(env);
    if (!ctx) return false;
    const lines = harnessLogLines(ev);
    if (!lines.length) return false;
    const file = harnessLogFile(env);
    const rec = `${JSON.stringify({
      v: 1,
      task_id: ctx.task_id,
      attempt: ctx.attempt,
      at: new Date().toISOString(),
      lines,
    })}\n`;
    fs.mkdirSync(path.dirname(file), { recursive: true });
    if (fs.existsSync(file) && fs.statSync(file).size + rec.length > LOG_MAX_FILE_BYTES)
      return false;
    fs.appendFileSync(file, rec, { mode: 0o660 });
    return true;
  } catch {
    return false;
  }
}
