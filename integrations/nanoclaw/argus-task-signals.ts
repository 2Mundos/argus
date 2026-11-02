/**
 * Host side of the argus-task file channel (task helpers spec 3.4).
 *
 * The helper runs in the agent container without an Argus token. It and the host share
 * `<task workspace>/.git/argus/`: the host writes `interrupt` when it injects a steer, so a
 * blocking `argus-task wait` returns at once; the helper writes `waiting.json` while it
 * waits, and the host turns it into a `waiting` progress milestone.
 *
 * The workspace belongs to the container, so everything in it is untrusted: the host never
 * follows a symlink there, reads at most a few KiB, and rebuilds the milestone field by field.
 */
import { constants as fsc } from "node:fs";
import { lstat, mkdir, open, rename, unlink } from "node:fs/promises";
import path from "node:path";

const READ_MAX = 4096;
const STALE_MS = 15 * 60_000; // a wait call is capped well below this (spec 3.3)
const REPOST_MS = 5 * 60_000; // keeps a long wait out of the quiet flag (20 min)

export type Waiting = { on: "ci" | "review"; ref: string; since: string };

/** `.git/argus` under the workspace, or null when `.git` or it is not a plain directory. */
async function signalDir(workspace: string, create: boolean): Promise<string | null> {
  const gitDir = path.join(workspace, ".git");
  const dir = path.join(gitDir, "argus");
  try {
    if (!(await lstat(gitDir)).isDirectory()) return null;
    if (create)
      await mkdir(dir).catch((err) => (err?.code === "EEXIST" ? undefined : Promise.reject(err)));
    return (await lstat(dir)).isDirectory() ? dir : null;
  } catch {
    return null;
  }
}

/** Tell any running wait that a steer arrived. Best effort: a failure only delays the steer. */
export async function markSteer(workspace: string, now: Date): Promise<boolean> {
  const dir = await signalDir(workspace, true);
  if (!dir) return false;
  const temp = path.join(dir, `.interrupt.${process.pid}.${now.getTime()}`);
  try {
    const fh = await open(temp, fsc.O_WRONLY | fsc.O_CREAT | fsc.O_EXCL | fsc.O_NOFOLLOW, 0o644);
    try {
      await fh.writeFile(`${JSON.stringify({ reason: "steer", at: now.toISOString() })}\n`);
    } finally {
      await fh.close();
    }
    await rename(temp, path.join(dir, "interrupt")); // replaces a symlink, never follows it
    return true;
  } catch {
    await unlink(temp).catch(() => undefined);
    return false;
  }
}

/** Validate what the helper wrote; null for anything that is not exactly a waiting record. */
export function parseWaiting(raw: unknown, nowMs: number): Waiting | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const { on, ref, since } = raw as Record<string, unknown>;
  if (on !== "ci" && on !== "review") return null;
  if (typeof ref !== "string" || !/^[\x20-\x7e]{1,200}$/.test(ref)) return null;
  if (typeof since !== "string" || since.length > 40) return null;
  const at = Date.parse(since);
  if (!Number.isFinite(at) || at > nowMs + 60_000) return null;
  return { on, ref, since: new Date(at).toISOString() };
}

/** The waiting record, or null when there is none, it is stale, or it is not valid. */
export async function readWaiting(workspace: string, nowMs: number): Promise<Waiting | null> {
  const dir = await signalDir(workspace, false);
  if (!dir) return null;
  let fh;
  try {
    fh = await open(path.join(dir, "waiting.json"), fsc.O_RDONLY | fsc.O_NOFOLLOW);
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
}

export function waitingMessage(w: Waiting): string {
  return `Waiting on ${w.on === "ci" ? "CI" : "review"} for ${w.ref}`;
}

/**
 * Decides when a waiting record becomes a milestone post: when it differs from the last one
 * posted, and again every few minutes while it lasts. Call `posted` after a successful post.
 */
export function createWaitingRelay() {
  let last: { key: string; at: number } | null = null;
  return {
    due(w: Waiting | null, nowMs: number): boolean {
      if (!w) {
        last = null;
        return false;
      }
      const key = `${w.on}|${w.ref}|${w.since}`;
      return !last || last.key !== key || nowMs - last.at >= REPOST_MS;
    },
    posted(w: Waiting, nowMs: number): void {
      last = { key: `${w.on}|${w.ref}|${w.since}`, at: nowMs };
    },
    reset(): void {
      last = null;
    },
  };
}
