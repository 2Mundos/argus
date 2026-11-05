// Live task log for a delegated run (docs/specs/task-live-log.spec.md).
//
// The executor's stdout becomes short readable lines, batched every few seconds and
// appended as JSON lines to a relay file in the agent group's directory. The NanoClaw
// host (argus.ts) tails that file and posts it to Argus with the agent token; this
// module has no token, no network, and never throws into the delegate run.

import fs from "node:fs";
import path from "node:path";
import { StringDecoder } from "node:string_decoder";

export const RELAY_FILE = "argus-task-log.jsonl";
export const RELAY_MAX_BYTES = 8 * 1024 * 1024; // per run; the relay stops, the run goes on
const LINE_MAX = 300;
const BATCH_MAX_LINES = 200;
const BUFFER_MAX_LINES = 2000;
const ARG_MAX = 120;
const RESULT_MAX = 200;

const MASK = "[redacted]";
// Linear in the input (no ReDoS): a rule starts only where its run starts (a lookbehind,
// or a literal keyword), so no two starts rescan the same run.
const PATTERNS = [
  /-----BEGIN [A-Z ]{0,32}PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]{0,32}PRIVATE KEY-----|$)/g,
  /\b(?:authorization|proxy-authorization)\s*[:=]\s*\S+(?:[ \t]+\S+)?/gi,
  /\bbearer\s+[A-Za-z0-9._~+/=-]{8,}/gi,
  /(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]*/g,
  /\bsk-[A-Za-z0-9_-]{16,}/g,
  /\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}/g,
  /\bgithub_pat_[A-Za-z0-9_]{20,}/g,
  /\bglpat-[A-Za-z0-9_-]{16,}/g,
  /\bxox[abprs]-[A-Za-z0-9-]{10,}/g,
  /\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/g,
  /\bAIza[A-Za-z0-9_-]{30,}/g,
  /(\b[a-z][a-z0-9+.-]{0,16}:\/\/[^\s/:@]+:)[^\s/@]+(?=@)/gi,
  // NAME=value, name: value, "name": "value" for secret-looking names (any prefix)
  /((?:token|secret|password|passwd|pwd|api[_-]?key|access[_-]?key|private[_-]?key|credentials?)["']?\s*[:=]\s*["']?)[^\s"',;]+/gi,
  /(?<![\w-])(\w*--(?=[\w-]*?(?:token|secret|password|api-key))[\w-]*[ =])\S+/gi,
];

/** Values of secret-looking variables (`*KEY*`, `*TOKEN*`, `*SECRET*`, `*PASSWORD*`), to mask exactly. */
export function envSecrets(env = process.env) {
  return Object.entries(env)
    .filter(([k, v]) => /KEY|TOKEN|SECRET|PASSWORD/i.test(k) && typeof v === "string")
    .map(([, v]) => v.trim())
    .filter((v) => v.length >= 8);
}

/** Best-effort masking of obvious secrets; the server redacts again. `secrets` are exact values. */
export function redactText(text, secrets = []) {
  let out = String(text);
  for (const s of secrets) if (s && s.length >= 6) out = out.split(s).join(MASK);
  for (const re of PATTERNS)
    out = out.replace(re, (...m) => (typeof m[1] === "string" ? `${m[1]}${MASK}` : MASK));
  return out;
}

const shown = (s) => s.split(MASK).join("").length;

/** `row` without its bare CRs when that leaves less unmasked (a CR split a secret mid-token). */
function joinSplitSecret(row, secrets) {
  if (!row.includes("\r")) return row;
  const joined = row.replaceAll("\r", "");
  const split = redactText(row, secrets)
    .split("\r")
    .reduce((n, p) => n + shown(p), 0);
  return shown(redactText(joined, secrets)) < split ? joined : row;
}

/**
 * The lines of `text` (a bare CR ends one too), redacted before the split as well, so
 * no CR or newline splits a secret; a row whose CR splits one mid-token stays whole.
 */
export function textLines(text, secrets = []) {
  const rows = String(text)
    .replace(/\r\n/g, "\n")
    .split("\n")
    .map((r) => joinSplitSecret(r, secrets));
  return redactText(rows.join("\n"), secrets).split(/[\r\n]/);
}

const cut = (s, n) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);
const oneLine = (s) => String(s).replace(/\s+/g, " ").trim();
const isObject = (x) => x !== null && typeof x === "object" && !Array.isArray(x);

/** The most telling argument of a tool call, short. */
function toolArgs(input) {
  if (!isObject(input)) return "";
  for (const k of ["command", "file_path", "path", "pattern", "url", "query", "description"])
    if (typeof input[k] === "string" && input[k]) return cut(oneLine(input[k]), ARG_MAX);
  let json = "";
  try {
    json = JSON.stringify(input);
  } catch {
    /* unprintable */
  }
  return json === "{}" ? "" : cut(json, ARG_MAX);
}

const resultText = (content) =>
  typeof content === "string"
    ? content
    : Array.isArray(content)
      ? content.map((c) => (typeof c?.text === "string" ? c.text : "")).join(" ")
      : "";

/**
 * Readable lines for one Claude `--output-format stream-json` event. Unknown or
 * malformed events give no lines. Text keeps its line breaks (one output line each),
 * redacted with `secrets` before it is split.
 */
export function claudeEventLines(ev, secrets = []) {
  if (!isObject(ev)) return [];
  const blocks = Array.isArray(ev.message?.content) ? ev.message.content : [];
  const lines = [];
  switch (ev.type) {
    case "system":
      if (ev.subtype === "init")
        lines.push(
          `claude started${typeof ev.model === "string" ? ` (${cut(ev.model, 80)})` : ""}`,
        );
      break;
    case "assistant":
      for (const b of blocks) {
        if (b?.type === "text" && typeof b.text === "string") {
          for (const l of textLines(b.text, secrets)) if (l.trim()) lines.push(l);
        } else if (b?.type === "tool_use") {
          const args = toolArgs(b.input);
          lines.push(`▸ ${String(b.name ?? "tool").slice(0, 60)}${args ? ` ${args}` : ""}`);
        }
      }
      break;
    case "user":
      for (const b of blocks) {
        if (b?.type !== "tool_result") continue;
        const text = cut(oneLine(resultText(b.content)), RESULT_MAX);
        if (b.is_error) lines.push(`  ✗ error: ${text}`);
        else if (text) lines.push(`  ↳ ${text}`);
      }
      break;
    case "result":
      lines.push(
        ev.is_error
          ? `claude finished with an error: ${cut(oneLine(ev.result ?? ev.subtype ?? ""), RESULT_MAX)}`
          : "claude finished",
      );
      break;
    default:
  }
  return lines;
}

/** `readToolCall` -> `read`; anything else keeps its key. */
const cursorToolName = (key) => key.replace(/ToolCall$/, "") || "tool";

const CURSOR_FAILURE_KEYS = ["error", "failure", "rejected", "rejectedError", "timeout"];

/**
 * Readable lines for one Cursor `agent --output-format stream-json` event. Only the
 * assistant's own text, tool names with their most telling argument, failed tool
 * results, and the final status are shown: never the echoed prompt (`user`), the
 * model's thinking, or tool output. `state` carries the lines already shown in the
 * current text segment (a partial delta stream is followed by the same text whole) and
 * the unfinished tail of a delta stream; create it with `{ seen: new Set(), partial: "" }`.
 */
export function agentEventLines(ev, secrets = [], state = { seen: new Set(), partial: "" }) {
  if (!isObject(ev)) return [];
  const lines = [];
  const text = (raw) => {
    for (const l of textLines(raw, secrets)) {
      if (!l.trim() || state.seen.has(l)) continue;
      state.seen.add(l);
      lines.push(l);
    }
  };
  const flushPartial = () => {
    if (state.partial) text(state.partial);
    state.partial = "";
  };
  switch (ev.type) {
    case "system":
      if (ev.subtype === "init")
        lines.push(
          `agent started${typeof ev.model === "string" ? ` (${cut(oneLine(ev.model), 80)})` : ""}`,
        );
      else if (ev.subtype === "background_shell_timeout")
        lines.push(
          `agent aborted ${Number(ev.aborted_count) || 0} background shell(s) after ${Number(ev.timeout_ms) / 1000 || 0}s`,
        );
      break;
    case "assistant": {
      const blocks = Array.isArray(ev.message?.content) ? ev.message.content : [];
      const body = blocks
        .map((b) => (b?.type === "text" && typeof b.text === "string" ? b.text : ""))
        .join("");
      if (!body) break;
      if (typeof ev.timestamp_ms === "number" && ev.model_call_id === undefined) {
        // A delta: show only the lines it completes.
        state.partial += body;
        const cutAt = state.partial.lastIndexOf("\n");
        if (cutAt >= 0) {
          text(state.partial.slice(0, cutAt));
          state.partial = state.partial.slice(cutAt + 1);
        }
      } else {
        state.partial = "";
        text(body);
      }
      break;
    }
    case "tool_call": {
      const key = isObject(ev.tool_call) ? Object.keys(ev.tool_call)[0] : undefined;
      if (!key) break;
      const call = ev.tool_call[key];
      if (ev.subtype === "started") {
        flushPartial();
        state.seen.clear();
        const args = toolArgs(call?.args);
        lines.push(`▸ ${cut(cursorToolName(key), 60)}${args ? ` ${args}` : ""}`);
      } else if (ev.subtype === "completed" && isObject(call?.result)) {
        const bad = CURSOR_FAILURE_KEYS.find((k) => call.result[k] !== undefined);
        if (bad) {
          const detail = call.result[bad];
          const msg =
            typeof detail === "string" ? detail : (detail?.error ?? detail?.message ?? bad);
          lines.push(`  ✗ ${cut(cursorToolName(key), 60)}: ${cut(oneLine(msg), RESULT_MAX)}`);
        }
      }
      break;
    }
    case "retry":
    case "connection":
      if (typeof ev.subtype === "string")
        lines.push(`agent ${ev.type}: ${cut(oneLine(ev.subtype), 60)}`);
      break;
    case "result":
      flushPartial();
      lines.push(
        ev.is_error
          ? `agent finished with an error: ${cut(oneLine(ev.result ?? ev.subtype ?? ""), RESULT_MAX)}`
          : "agent finished",
      );
      break;
    default:
  }
  return lines;
}

/** `codex exec --json` item statuses that mean the item did not succeed. */
const CODEX_BAD_STATUS = new Set(["failed", "declined"]);

/** Connection-retry notices the CLI emits as `error` events and `error` items; the final failure is `turn.failed`. */
const CODEX_RETRY_NOTICE = /^(Reconnecting\.\.\.|Falling back)/;

/**
 * Readable lines for one `codex exec --json` event (`thread.started`, `turn.*`,
 * `item.started|completed`, `error`). Shown: the agent's messages, `▸ tool arg` for a
 * command, patch, MCP call or web search, a bounded result or error summary, and the
 * final status. A retry notice (`Reconnecting... N/5`, `Falling back ...`) reads
 * `codex error (retrying): ...`, not as a failure. Never shown: `reasoning`, the prompt, `todo_list`, or unknown events.
 * `state.started` holds the ids of items whose `▸` line was already printed.
 */
export function codexEventLines(ev, secrets = [], state = { started: new Set() }) {
  if (!isObject(ev)) return [];
  const lines = [];
  const msg = (e) => cut(oneLine(isObject(e) ? (e.message ?? "") : (e ?? "")), RESULT_MAX);
  const errorLine = (message, prefix) =>
    CODEX_RETRY_NOTICE.test(oneLine(message ?? ""))
      ? `codex error (retrying): ${msg(message)}`
      : `${prefix}: ${msg(message)}`;
  const call = (item, label) => {
    const id = typeof item.id === "string" ? item.id : null;
    if (id && state.started.has(id)) return;
    if (id && state.started.size < 5000) state.started.add(id);
    lines.push(`▸ ${label}`);
  };
  const outcome = (item, detail) => {
    if (CODEX_BAD_STATUS.has(item.status)) lines.push(`  ✗ error: ${detail}`);
    else if (detail) lines.push(`  ↳ ${detail}`);
  };
  switch (ev.type) {
    case "thread.started":
      lines.push("codex started");
      break;
    case "item.started":
    case "item.completed": {
      const item = ev.item;
      if (!isObject(item)) break;
      const done = ev.type === "item.completed";
      switch (item.type) {
        case "agent_message":
          if (done && typeof item.text === "string")
            for (const l of textLines(item.text, secrets)) if (l.trim()) lines.push(l);
          break;
        case "command_execution": {
          if (typeof item.command !== "string") break;
          call(item, `shell ${cut(oneLine(item.command), ARG_MAX)}`);
          if (!done) break;
          const code = Number.isInteger(item.exit_code) ? item.exit_code : null;
          const out = cut(oneLine(item.aggregated_output ?? ""), RESULT_MAX);
          if (CODEX_BAD_STATUS.has(item.status) || (code !== null && code !== 0))
            lines.push(`  ✗ error: ${code !== null ? `exit ${code} ` : ""}${out}`.trimEnd());
          else if (out) lines.push(`  ↳ ${out}`);
          break;
        }
        case "file_change": {
          const paths = (Array.isArray(item.changes) ? item.changes : [])
            .map((c) => (isObject(c) && typeof c.path === "string" ? c.path : ""))
            .filter(Boolean);
          call(item, `patch ${cut(oneLine(paths.join(", ")), ARG_MAX)}`.trimEnd());
          if (done && CODEX_BAD_STATUS.has(item.status)) lines.push(`  ✗ error: ${item.status}`);
          break;
        }
        case "mcp_tool_call": {
          if (typeof item.tool !== "string") break;
          const name = `${typeof item.server === "string" && item.server ? `${item.server}/` : ""}${item.tool}`;
          const args = toolArgs(item.arguments);
          call(item, `${cut(name, 60)}${args ? ` ${args}` : ""}`);
          if (done && (item.error || CODEX_BAD_STATUS.has(item.status)))
            lines.push(`  ✗ error: ${msg(item.error) || String(item.status)}`);
          break;
        }
        case "web_search":
          if (typeof item.query === "string")
            call(item, `web_search ${cut(oneLine(item.query), ARG_MAX)}`);
          break;
        case "error":
          if (done) lines.push(errorLine(item.message, "  ✗ error"));
          break;
        default:
      }
      break;
    }
    case "error":
      lines.push(errorLine(ev.message, "codex error"));
      break;
    case "turn.failed":
      lines.push(`codex finished with an error: ${msg(ev.error)}`);
      break;
    case "turn.completed":
      lines.push("codex finished");
      break;
    default:
  }
  return lines;
}

/** Feed it stdout chunks; calls `onLines` with the readable lines of each complete event. */
export function createStreamParser({ executor, onLines, secrets = [] }) {
  let rest = "";
  const agentState = { seen: new Set(), partial: "" };
  const codexState = { started: new Set() };
  const handle = (raw) => {
    const line = raw.trim();
    if (!line) return;
    let lines = [];
    if (executor === "claude") {
      try {
        lines = claudeEventLines(JSON.parse(line), secrets);
      } catch {
        return; // not a stream event (a plain JSON report, or noise)
      }
    } else if (executor === "agent") {
      try {
        lines = agentEventLines(JSON.parse(line), secrets, agentState);
      } catch {
        return;
      }
    } else if (executor === "codex") {
      try {
        lines = codexEventLines(JSON.parse(line), secrets, codexState);
      } catch {
        return;
      }
    }
    lines = lines.map((l) => cut(redactText(l, secrets), LINE_MAX)).filter(Boolean);
    if (lines.length) onLines(lines);
  };
  return {
    push(chunk) {
      rest += chunk;
      const parts = rest.split("\n");
      rest = parts.pop() ?? "";
      for (const p of parts) handle(p);
    },
    end() {
      const last = rest;
      rest = "";
      handle(last);
    },
  };
}

/**
 * Collects lines and hands them to `send(lines)` at most every `flushMs`; when nothing
 * was output for `keepaliveMs` it adds "delegate alive, Ns since last output".
 * `tick()` is called by a timer; `close()` flushes the rest.
 */
export function createBatcher({ send, now = Date.now, flushMs = 3000, keepaliveMs = 30_000 }) {
  let buffer = [];
  let dropped = 0;
  let lastFlush = now();
  let lastOutput = now();
  let lastSignal = now();
  const flush = () => {
    lastFlush = now();
    if (dropped) {
      buffer.unshift(`… ${dropped} lines dropped`);
      dropped = 0;
    }
    const lines = buffer;
    buffer = [];
    for (let i = 0; i < lines.length; i += BATCH_MAX_LINES) {
      try {
        send(lines.slice(i, i + BATCH_MAX_LINES));
      } catch {
        /* the relay must not affect the run */
      }
    }
  };
  return {
    add(lines) {
      if (!lines.length) return;
      lastOutput = lastSignal = now();
      buffer.push(...lines);
      if (buffer.length > BUFFER_MAX_LINES) {
        dropped += buffer.length - BUFFER_MAX_LINES;
        buffer = buffer.slice(-BUFFER_MAX_LINES);
      }
    },
    tick() {
      const t = now();
      if (!buffer.length && t - lastSignal >= keepaliveMs) {
        lastSignal = t;
        buffer.push(`delegate alive, ${Math.round((t - lastOutput) / 1000)}s since last output`);
        flush();
      } else if (buffer.length && t - lastFlush >= flushMs) flush();
    },
    close() {
      if (buffer.length || dropped) flush();
    },
  };
}

/**
 * Append one record to `file` without following a link: the directory is writable by
 * the container, and the channel refuses anything but a regular file with one link.
 * Returns false (writes nothing) for a symlink, a non-regular file, a hard link, or a
 * record that would take the file past `maxBytes`. Throws on I/O errors.
 */
export function appendRelayRecord(file, rec, maxBytes = RELAY_MAX_BYTES) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const flags =
    fs.constants.O_WRONLY |
    fs.constants.O_APPEND |
    fs.constants.O_CREAT |
    fs.constants.O_NOFOLLOW |
    fs.constants.O_NONBLOCK;
  const fd = fs.openSync(file, flags, 0o660);
  try {
    const st = fs.fstatSync(fd);
    const bytes = Buffer.byteLength(rec);
    if (!st.isFile() || st.nlink !== 1 || st.size + bytes > maxBytes) return false;
    fs.writeSync(fd, rec);
    return true;
  } finally {
    fs.closeSync(fd);
  }
}

/**
 * The relay: appends `{v, task_id, attempt, at, lines}` JSON lines to `file`.
 * Bounded and silent on failure; after an error, a refused target or the byte cap it
 * stops writing.
 */
export function createFileRelay({ file, taskId, attempt, maxBytes = RELAY_MAX_BYTES }) {
  let broken = false;
  return (lines) => {
    if (broken || !lines.length) return;
    try {
      const rec = `${JSON.stringify({ v: 1, task_id: taskId, attempt, at: new Date().toISOString(), lines })}\n`;
      if (!appendRelayRecord(file, rec, maxBytes)) broken = true;
    } catch {
      broken = true;
    }
  };
}

/**
 * Follow `stdoutFile` while the executor runs. Returns `stop()`, which drains the rest
 * and flushes. Everything is wrapped: a failure here never reaches the caller.
 */
export function startLiveLog({
  stdoutFile,
  executor,
  send,
  secrets = [],
  intervalMs = 1000,
  flushMs,
  keepaliveMs,
  now = Date.now,
}) {
  const batcher = createBatcher({ send, now, flushMs, keepaliveMs });
  const parser = createStreamParser({
    executor,
    secrets,
    onLines: (l) => batcher.add(l),
  });
  let offset = 0;
  const decoder = new StringDecoder("utf8");
  const drain = () => {
    try {
      const size = fs.statSync(stdoutFile).size;
      if (size <= offset) return;
      const fd = fs.openSync(stdoutFile, "r");
      try {
        const buf = Buffer.alloc(size - offset);
        const n = fs.readSync(fd, buf, 0, buf.length, offset);
        offset += n;
        parser.push(decoder.write(buf.subarray(0, n)));
      } finally {
        fs.closeSync(fd);
      }
    } catch {
      /* not there yet, or unreadable: try again next tick */
    }
  };
  const timer = setInterval(() => {
    try {
      drain();
      batcher.tick();
    } catch {
      /* ignore */
    }
  }, intervalMs);
  timer.unref?.();
  return {
    stop() {
      clearInterval(timer);
      try {
        drain();
        parser.end();
        batcher.close();
      } catch {
        /* ignore */
      }
    },
  };
}
