/**
 * Host-side relay of command guard denials (command guard spec 3).
 *
 * The guard hook (`engines/command_guard.py`) only prints a deny decision, and holds no
 * network or state. Claude Code records that decision in the session transcript as a
 * Bash `tool_result` with `is_error` whose text is the fixed block message; the matching
 * `tool_use` holds the command. This module tails the transcripts the attempt wrote and
 * posts each denial once to `POST /api/tasks/{id}/command-blocked` with the host's token.
 *
 * The transcript is untrusted input (a command can print the block message): the rule is
 * taken from the closed table below, never from free text, and the server takes the
 * reason from its own table, redacts and cuts the command again, and caps the events an
 * attempt may record. A denial is informational and never changes the attempt.
 */
import { log } from "../log.js";
import { type TranscriptHit, transcripts } from "./argus-quota-resume.js";

/**
 * Rule id -> reason, as the hook prints it. Keep equal to `REASONS` in
 * `engines/command_guard.py` (a test compares them).
 */
export const COMMAND_GUARD_REASONS: Readonly<Record<string, string>> = {
  commit_attribution:
    "a commit message must not contain a Co-Authored-By or other co-author attribution trailer (remove the trailer; git author and committer is enough)",
  rm_root: "recursive deletion of /, ~, $HOME, /home/* or /root",
  no_preserve_root: "--no-preserve-root disables the safety check of rm",
  mkfs: "creating a filesystem destroys the data on the device",
  dd_device: "writing to a block device overwrites a disk",
  device_redirect: "redirecting output to a block device overwrites a disk",
  fork_bomb: "a fork bomb exhausts the machine",
};

const PREFIX = "Blocked by Argus command guard: ";
const SUFFIX = ". Do not retry or work around this block.";
const COMMAND_MAX = 16_000; // the server accepts this much and cuts to 200 after redacting
const READ_MAX = 4 * 1024 * 1024; // per file per poll
const PENDING_MAX = 500; // tool_use commands remembered
const QUEUE_MAX = 50;
const TOOL_USE_ID = /^[A-Za-z0-9_.:-]{1,200}$/;
const CLAIM_LOST = ["stale_attempt", "not_assignee", "not_claimed"];

export interface CommandBlockedTask {
  id: string;
  attempt: number;
}

export interface CommandBlockedBody {
  attempt: number;
  tool_use_id: string;
  rule: string;
  command: string;
}

/** The rule id of a guard block message; null for any other text. */
export function ruleOfBlockText(text: string): string | null {
  if (!text.startsWith(PREFIX)) return null;
  const rest = text.slice(PREFIX.length);
  const end = rest.indexOf(SUFFIX);
  if (end < 0) return null;
  const reason = rest.slice(0, end);
  return (
    Object.keys(COMMAND_GUARD_REASONS).find((k) => COMMAND_GUARD_REASONS[k] === reason) ?? null
  );
}

type Block = Record<string, unknown>;
const isObject = (x: unknown): x is Block =>
  x !== null && typeof x === "object" && !Array.isArray(x);

const resultText = (content: unknown): string => {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((c) => (isObject(c) && c.type === "text" && typeof c.text === "string" ? c.text : ""))
    .join("");
};

/**
 * Reads one transcript line. A Bash `tool_use` is remembered in `commands` by its id; a
 * guard-denied `tool_result` yields a body, once the command is known. Records older than
 * `sinceMs` (a resumed session's earlier turns) are ignored.
 */
export function readTranscriptLine(
  line: string,
  commands: Map<string, string>,
  task: CommandBlockedTask,
  sinceMs: number,
): CommandBlockedBody | null {
  if (!line.includes("tool_use") && !line.includes("tool_result")) return null;
  let rec: unknown;
  try {
    rec = JSON.parse(line);
  } catch {
    return null;
  }
  if (!isObject(rec) || !isObject(rec.message) || !Array.isArray(rec.message.content)) return null;
  const at = typeof rec.timestamp === "string" ? Date.parse(rec.timestamp) : Number.NaN;
  if (!(at >= sinceMs)) return null;
  for (const block of rec.message.content) {
    if (!isObject(block) || typeof (block.id ?? block.tool_use_id) !== "string") continue;
    if (
      rec.type === "assistant" &&
      block.type === "tool_use" &&
      block.name === "Bash" &&
      isObject(block.input) &&
      typeof block.input.command === "string"
    ) {
      if (commands.size >= PENDING_MAX) commands.delete(commands.keys().next().value as string);
      commands.set(block.id as string, block.input.command.slice(0, COMMAND_MAX));
    } else if (
      rec.type === "user" &&
      block.type === "tool_result" &&
      block.is_error === true &&
      TOOL_USE_ID.test(block.tool_use_id as string)
    ) {
      const rule = ruleOfBlockText(resultText(block.content));
      const command = commands.get(block.tool_use_id as string);
      if (rule && command !== undefined)
        return { attempt: task.attempt, tool_use_id: block.tool_use_id as string, rule, command };
    }
  }
  return null;
}

/**
 * Tails the transcripts written since `since()` and posts each denial once. 401 and a
 * lost claim (409 `stale_attempt` / `not_assignee` / `not_claimed`) stop it; a 4xx that
 * cannot succeed drops that denial; a network error, 429 or 5xx keeps it for the next
 * poll. Only status and code are logged, never the command.
 */
export function createCommandBlockedRelay(opts: {
  projectsDir: () => Promise<string | null>;
  since: () => number;
  task: CommandBlockedTask;
  post: (body: CommandBlockedBody) => Promise<unknown>;
}) {
  let stopped = false;
  let busy = false;
  const offsets = new Map<string, number>();
  const commands = new Map<string, string>();
  const seen = new Set<string>(); // tool_use ids queued or posted
  const queue: CommandBlockedBody[] = [];

  async function scan(file: TranscriptHit): Promise<void> {
    const { open } = await import("node:fs/promises");
    const from = offsets.get(file.path) ?? 0;
    if (file.size <= from) return;
    const handle = await open(file.path, "r");
    try {
      const buf = Buffer.alloc(Math.min(file.size - from, READ_MAX));
      const { bytesRead } = await handle.read(buf, 0, buf.length, from);
      const chunk = buf.subarray(0, bytesRead);
      const end = chunk.lastIndexOf(0x0a) + 1; // only whole lines; the tail is read next poll
      if (end === 0) return;
      offsets.set(file.path, from + end);
      for (const line of chunk.subarray(0, end).toString("utf8").split("\n")) {
        const body = readTranscriptLine(line, commands, opts.task, opts.since());
        if (body && !seen.has(body.tool_use_id) && queue.length < QUEUE_MAX) {
          seen.add(body.tool_use_id);
          queue.push(body);
        }
      }
    } finally {
      await handle.close();
    }
  }

  async function flush(): Promise<void> {
    while (queue.length > 0 && !stopped) {
      const body = queue[0] as CommandBlockedBody;
      try {
        await opts.post(body);
        queue.shift();
      } catch (err) {
        const status = (err as { status?: number })?.status;
        const code = (err as { code?: string })?.code ?? "";
        if (status === 401 || (status === 409 && CLAIM_LOST.includes(code))) {
          stopped = true;
          log.warn("Argus refuses command guard reports; relay stopped", {
            task: opts.task.id,
            code,
          });
        } else if (
          typeof status === "number" &&
          status >= 400 &&
          status < 500 &&
          ![408, 409, 425, 429].includes(status)
        ) {
          queue.shift(); // 413 / 422: this denial can never be accepted
          log.warn("Argus rejected a command guard report; dropped", {
            task: opts.task.id,
            status,
            code,
          });
        } else return; // retry on the next poll
      }
    }
  }

  return {
    get stopped(): boolean {
      return stopped;
    },
    stop(): void {
      stopped = true;
    },
    /** Read what the transcripts gained and post the new denials. Never throws. */
    async poll(): Promise<void> {
      if (stopped || busy) return;
      busy = true;
      try {
        const dir = await opts.projectsDir();
        if (dir) for (const file of await transcripts(dir, opts.since())) await scan(file);
        await flush();
      } catch (err) {
        log.warn("Command guard relay failed (will retry)", { err: String(err).slice(0, 200) });
      } finally {
        busy = false;
      }
    },
  };
}
