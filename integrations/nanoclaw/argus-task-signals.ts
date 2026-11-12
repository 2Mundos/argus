/**
 * Host side of the argus-task file channel (task helpers spec 3.4).
 *
 * The helper runs in the agent container without an Argus token. It and the host share
 * `<task workspace>/.git/argus/`: the host writes `interrupt` when it injects a steer, so a
 * blocking `argus-task wait` returns at once; the helper writes `waiting.json` while it
 * waits, and the host turns it into a `waiting` progress milestone.
 *
 * The workspace belongs to the container, so everything in it is untrusted. The host walks
 * from its own group directory to `.git/argus` one component at a time, opening each as a
 * directory without following symlinks, and then works only through the last open handle
 * (`/proc/self/fd/N/...`), so a worker that swaps a directory for a symlink mid-way cannot
 * redirect a read or write. It never opens a FIFO or device, reads at most a few KiB, and
 * rebuilds the milestone field by field.
 */
import { constants as fsc } from "node:fs";
import { type FileHandle, mkdir, open, rename, unlink } from "node:fs/promises";
import { TASK_WORKSPACES_DIR, taskWorkspacePath } from "./argus-workspace.js";

const READ_MAX = 4096;
const STALE_MS = 15 * 60_000; // a wait call is capped well below this (spec 3.3)
const REPOST_MS = 5 * 60_000; // keeps a long wait out of the quiet flag (20 min)
const DIR_FLAGS = fsc.O_RDONLY | fsc.O_DIRECTORY | fsc.O_NOFOLLOW;

const OUTCOME_STATES = ["failure", "success", "skipped", "unknown"] as const;
const MAX_FAILED = 20;

/** How a finished ci wait ended: the overall state and the failed contexts of the watched commit. */
export type Outcome = { state: (typeof OUTCOME_STATES)[number]; failed: string[] };

/** A pending wait, or (with `outcome`) a finished one that the host turns into one last milestone. */
export type Waiting = {
  on: "ci" | "review";
  ref: string;
  since: string;
  sha?: string;
  outcome?: Outcome;
};

const via = (fh: FileHandle, name = "") => `/proc/self/fd/${fh.fd}${name ? `/${name}` : ""}`;

/**
 * Run `fn` with a path that resolves through an open handle on
 * `<groupDir>/task-workspaces/<taskId>/.git/argus`; null when any component is missing or
 * is not a plain directory. `create` makes the last component.
 */
async function inSignalDir<T>(
  groupDir: string,
  taskId: string,
  create: boolean,
  fn: (dir: string) => Promise<T>,
): Promise<T | null> {
  const handles: FileHandle[] = [];
  try {
    taskWorkspacePath(groupDir, taskId); // validates the id
    let dir = await open(groupDir, fsc.O_RDONLY | fsc.O_DIRECTORY);
    handles.push(dir);
    for (const name of [TASK_WORKSPACES_DIR, taskId, ".git", "argus"]) {
      if (name === "argus" && create)
        await mkdir(via(dir, name)).catch((err) =>
          err?.code === "EEXIST" ? undefined : Promise.reject(err),
        );
      dir = await open(via(dir, name), DIR_FLAGS);
      handles.push(dir);
    }
    return await fn(via(dir));
  } catch {
    return null;
  } finally {
    for (const fh of handles.reverse()) await fh.close().catch(() => undefined);
  }
}

/** Tell any running wait that a steer arrived. Best effort: a failure only delays the steer. */
export async function markSteer(groupDir: string, taskId: string, now: Date): Promise<boolean> {
  const written = await inSignalDir(groupDir, taskId, true, async (dir) => {
    const temp = `${dir}/.interrupt.${process.pid}.${now.getTime()}`;
    try {
      const fh = await open(temp, fsc.O_WRONLY | fsc.O_CREAT | fsc.O_EXCL | fsc.O_NOFOLLOW, 0o644);
      try {
        await fh.writeFile(`${JSON.stringify({ reason: "steer", at: now.toISOString() })}\n`);
      } finally {
        await fh.close();
      }
      await rename(temp, `${dir}/interrupt`); // replaces a symlink, never follows it
      return true;
    } catch {
      await unlink(temp).catch(() => undefined);
      return false;
    }
  });
  return written === true;
}

/** Validate what the helper wrote; null for anything that is not exactly a waiting record. */
export function parseWaiting(raw: unknown, nowMs: number): Waiting | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const { on, ref, since, sha, outcome } = raw as Record<string, unknown>;
  if (on !== "ci" && on !== "review") return null;
  if (typeof ref !== "string" || !/^[\x20-\x7e]{1,200}$/.test(ref)) return null;
  if (typeof since !== "string" || since.length > 40) return null;
  const at = Date.parse(since);
  if (!Number.isFinite(at) || at > nowMs + 60_000) return null;
  const base: Waiting = { on, ref, since: new Date(at).toISOString() };
  if (sha === undefined) return outcome === undefined ? base : null;
  if (typeof sha !== "string" || !/^[0-9a-f]{7,64}$/i.test(sha)) return null;
  const withSha = { ...base, sha: sha.toLowerCase() };
  if (outcome === undefined) return withSha;
  const parsed = on === "ci" ? parseOutcome(outcome) : null;
  return parsed ? { ...withSha, outcome: parsed } : null;
}

function parseOutcome(raw: unknown): Outcome | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const { state, failed } = raw as Record<string, unknown>;
  if (!OUTCOME_STATES.includes(state as Outcome["state"])) return null;
  if (!Array.isArray(failed) || failed.length > MAX_FAILED) return null;
  if (!failed.every((c) => typeof c === "string" && /^[\x20-\x7e]{1,100}$/.test(c))) return null;
  return { state: state as Outcome["state"], failed: [...failed] };
}

/** The waiting record, or null when there is none, it is stale, or it is not valid. */
export async function readWaiting(
  groupDir: string,
  taskId: string,
  nowMs: number,
): Promise<Waiting | null> {
  return inSignalDir(groupDir, taskId, false, async (dir) => {
    let fh: FileHandle;
    try {
      // O_NONBLOCK: a FIFO opens at once and is refused below instead of hanging the host.
      fh = await open(`${dir}/waiting.json`, fsc.O_RDONLY | fsc.O_NOFOLLOW | fsc.O_NONBLOCK);
    } catch {
      return null;
    }
    try {
      const st = await fh.stat();
      if (!st.isFile() || nowMs - st.mtimeMs > STALE_MS) return null;
      const buf = Buffer.alloc(READ_MAX);
      const { bytesRead } = await fh.read(buf, 0, READ_MAX, 0);
      return parseWaiting(JSON.parse(buf.subarray(0, bytesRead).toString("utf8")), nowMs);
    } catch {
      return null;
    } finally {
      await fh.close();
    }
  });
}

/**
 * The progress post for a record. A pending wait is `waiting`; a finished one is the single
 * `ci_failed` / `ci_passed` milestone that replaces it (the API has no kind for "inconclusive",
 * so `unknown` is not a pass). The failed contexts travel in the message: milestone data is a
 * closed set (progress milestones spec 3.1) and carries only the sha.
 */
export function waitingMilestone(w: Waiting): {
  kind: string;
  message: string;
  data: Record<string, string>;
} {
  const { outcome, sha } = w;
  if (!outcome || !sha) {
    return {
      kind: "waiting",
      message: waitingMessage(w),
      data: { on: w.on, ref: w.ref, since: w.since },
    };
  }
  const at = sha.slice(0, 7);
  const message = {
    failure: `CI failed at ${at}${outcome.failed.length ? `: ${outcome.failed.join(", ")}` : ""}`,
    success: `CI passed at ${at}`,
    skipped: `CI skipped at ${at}`,
    unknown: `CI inconclusive at ${at}: unknown`,
  }[outcome.state];
  const passed = outcome.state === "success" || outcome.state === "skipped";
  return { kind: passed ? "ci_passed" : "ci_failed", message, data: { sha: sha.slice(0, 40) } };
}

export function waitingMessage(w: Waiting): string {
  const at = w.on === "ci" && w.sha ? ` at ${w.sha.slice(0, 7)}` : "";
  return `Waiting on ${w.on === "ci" ? "CI" : "review"} for ${w.ref}${at}`;
}

/**
 * Decides when a waiting record becomes a milestone post: when it differs from the last one
 * posted, and again every few minutes while it lasts. Call `posted` after a successful post.
 */
export function createWaitingRelay() {
  let last: { key: string; at: number } | null = null;
  // An outcome is posted once; only a pending wait is repeated to stay out of the quiet flag.
  const keyOf = (w: Waiting) =>
    w.outcome
      ? `${w.on}|${w.ref}|${w.sha}|${w.outcome.state}|${w.outcome.failed.join("\n")}`
      : `${w.on}|${w.ref}|${w.since}|${w.sha ?? ""}`;
  return {
    due(w: Waiting | null, nowMs: number): boolean {
      if (!w) {
        last = null;
        return false;
      }
      return !last || last.key !== keyOf(w) || (!w.outcome && nowMs - last.at >= REPOST_MS);
    },
    posted(w: Waiting, nowMs: number): void {
      last = { key: keyOf(w), at: nowMs };
    },
    reset(): void {
      last = null;
    },
  };
}
