import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { copilotAuthRejected } from "./engines/argus-delegate.mjs";
import { parseUsage } from "./engines/usage.mjs";
import {
  claudeAuthExpiredError,
  claudeAuthExpiredFailure,
  claudeRateLimitError,
  claudeRateLimitFailure,
} from "./overlays/claude-rate-limit.js";
import { codexUsageLimitFailure } from "./overlays/codex-usage-limit.js";

const NOW = new Date("2026-10-20T22:06:00Z");

interface ClaudeEvent {
  type?: string;
  subtype?: string;
  is_error?: boolean;
  terminal_reason?: string;
  result?: string;
  error?: string;
}

/** What the channel derives from a Claude Code stream-json transcript, as the poll loop does per turn. */
export function deriveClaude(events: ClaudeEvent[]) {
  const sdkError = (e: string) => events.some((ev) => ev.type === "assistant" && ev.error === e);
  const result = events.findLast((ev) => ev.type === "result");
  const failed = result?.is_error === true;
  const authExpiredError = failed
    ? claudeAuthExpiredError(sdkError("authentication_failed"))
    : undefined;
  const rateLimitError = failed
    ? claudeRateLimitError(result?.result, sdkError("rate_limit"))
    : undefined;
  return {
    result: {
      is_error: result?.is_error,
      subtype: result?.subtype,
      terminal_reason: result?.terminal_reason,
    },
    rateLimitError: rateLimitError ?? null,
    rateLimitFailure: claudeRateLimitFailure(rateLimitError, NOW) ?? null,
    authExpiredError: authExpiredError ?? null,
    authExpiredFailure: claudeAuthExpiredFailure(authExpiredError) ?? null,
  };
}

interface CodexEvent {
  type?: string;
  usage?: unknown;
  error?: { message?: string };
}

/**
 * What the channel derives from a `codex exec --json` stream. Only `turn.failed` carries the
 * final failure; top-level `error` events are retry notices. Fields follow
 * fixtures/codex-0.162.1/PIN.md.
 */
export function deriveCodex(events: CodexEvent[]) {
  const failed = events.findLast((ev) => ev.type === "turn.failed");
  const completed = events.findLast((ev) => ev.type === "turn.completed" && ev.usage != null);
  return {
    turnFailed: failed !== undefined,
    codexUsageLimitFailure: codexUsageLimitFailure(failed?.error?.message) ?? null,
    usage: completed?.usage ?? null,
  };
}

interface CursorEvent {
  type?: string;
  subtype?: string;
  is_error?: boolean;
  usage?: unknown;
}

function cursorStdoutFromEvents(events: CursorEvent[]): string {
  return events.map((ev) => JSON.stringify(ev)).join("\n");
}

/** Cursor CLI (`agent`) stream-json: same usage path as executor `agent` in usage.mjs. */
export function deriveCursor(events: CursorEvent[]) {
  const result = events.findLast((ev) => ev.type === "result");
  const usageRecordPresent =
    result !== undefined &&
    result.usage !== null &&
    result.usage !== undefined &&
    typeof result.usage === "object";
  const usage = parseUsage("agent", cursorStdoutFromEvents(events));
  return {
    result: {
      is_error: result?.is_error,
      subtype: result?.subtype,
    },
    usageRecordPresent,
    usage,
  };
}

/**
 * Copilot CLI (`copilot -s`) prints plain text, so its recordings are text, not JSON lines.
 * Uses the production auth-rejection and usage functions (engines/argus-delegate.mjs, usage.mjs).
 */
export function deriveCopilot(run: string | { stdout?: string; stderr?: string }) {
  const { stdout = "", stderr = "" } = typeof run === "string" ? { stdout: run } : run;
  return {
    authRejected: copilotAuthRejected(`${stdout}\n${stderr}`),
    usage: parseUsage("copilot", stdout),
  };
}

/** The host-side facts of one claim, as the `tasks` row and its attempt carry them (stall visibility 3.7). */
export interface StallRow {
  state: "claimed" | "queued";
  quiet_at: string | null;
  activity_at?: string | null;
  progress_at?: string | null;
  output_at?: string | null;
  claimed_at: string | null;
  /** JSON of a LeaseObservation, or null when the host sent none. */
  observation: string | null;
  observation_at: string | null;
  paused_disk_at?: string | null;
}

export interface StallFacts {
  stall_cause: string | null;
  quiet: boolean;
  last_signal_source: string | null;
}

const REPO_ROOT = join(import.meta.dir, "..", "..");

// Runs the production classifier unchanged: `argus_api.tasks.expiry.quiet_facts_from_row`
// (the function behind GET /api/tasks/{id} and the inbox) over a dict row. Needs the Python
// environment from `uv sync --all-packages --group dev`; no Docker, no network, no database.
const STALL_SCRIPT = `
import json, sys
from datetime import datetime
from argus_api.tasks.expiry import quiet_facts_from_row
req = json.load(sys.stdin)
f = quiet_facts_from_row(req["row"], datetime.fromisoformat(req["now"]))
print(json.dumps({"stall_cause": f.stall_cause, "quiet": f.quiet_at is not None,
                  "last_signal_source": f.last_signal_source}))
`;

function pythonCommand(): [string, string[]] {
  if (process.env.ARGUS_PYTHON) return [process.env.ARGUS_PYTHON, []];
  const venv = join(REPO_ROOT, ".venv", "bin", "python");
  if (existsSync(venv)) return [venv, []];
  return ["uv", ["run", "--no-sync", "python"]];
}

/** What the production stall classifier derives for one claim row at `now` (ISO-8601 UTC). */
export function deriveStall(row: StallRow, now: string): StallFacts {
  const [cmd, args] = pythonCommand();
  const out = execFileSync(cmd, [...args, "-c", STALL_SCRIPT], {
    cwd: REPO_ROOT,
    input: JSON.stringify({
      row: { activity_at: null, progress_at: null, output_at: null, paused_disk_at: null, ...row },
      now,
    }),
    encoding: "utf8",
  });
  return JSON.parse(out) as StallFacts;
}

export const DERIVE = {
  claude: deriveClaude,
  codex: deriveCodex,
  cursor: deriveCursor,
  copilot: deriveCopilot,
} as const;

/** Engines whose recordings are plain text rather than JSON lines. */
const TEXT_ENGINES = new Set(["copilot"]);

export function readEvents(path: string): unknown[] {
  return readFileSync(path, "utf8")
    .split("\n")
    .filter((l) => l.trim() !== "")
    .map((l) => JSON.parse(l));
}

// `bun transcripts-derive.ts <engine> <events.jsonl>` prints the derived values as JSON;
// scripts/record_transcript.py uses it to draft expected.json.
if (import.meta.main) {
  const [engine, path] = process.argv.slice(2);
  const derive = DERIVE[engine as keyof typeof DERIVE];
  if (!derive || !path) {
    console.error(`usage: transcripts-derive.ts <${Object.keys(DERIVE).join("|")}> <events.jsonl>`);
    process.exit(2);
  }
  const input = TEXT_ENGINES.has(engine) ? readFileSync(path, "utf8") : readEvents(path);
  console.log(JSON.stringify(derive(input as never), null, 2));
}
