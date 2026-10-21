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
 * malformed events give no lines. Text keeps its line breaks (one output line each).
 */
export function claudeEventLines(ev) {
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
          for (const l of b.text.split("\n")) if (l.trim()) lines.push(l);
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

/** Feed it stdout chunks; calls `onLines` with the readable lines of each complete event. */
export function createStreamParser({ executor, onLines, secrets = [] }) {
  let rest = "";
  const handle = (raw) => {
    const line = raw.trim();
    if (!line) return;
    let lines = [];
    if (executor === "claude") {
      try {
        lines = claudeEventLines(JSON.parse(line));
      } catch {
        return; // not a stream event (a plain JSON report, or noise)
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
 * The relay: appends `{v, task_id, attempt, at, lines}` JSON lines to `file`.
 * Bounded and silent on failure; after an error or the byte cap it stops writing.
 */
export function createFileRelay({ file, taskId, attempt, maxBytes = RELAY_MAX_BYTES }) {
  let written = 0;
  let broken = false;
  return (lines) => {
    if (broken || !lines.length) return;
    try {
      const rec = `${JSON.stringify({ v: 1, task_id: taskId, attempt, at: new Date().toISOString(), lines })}\n`;
      if (written + rec.length > maxBytes) {
        broken = true;
        return;
      }
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.appendFileSync(file, rec, { mode: 0o660 });
      written += rec.length;
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
