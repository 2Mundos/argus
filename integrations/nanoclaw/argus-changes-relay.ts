/**
 * Host-side relay of task changes (task changes spec 3.2).
 *
 * `argus-delegate` writes a diff snapshot of the task workspace, computed in the session
 * container, to a file in the agent group's directory. This module reads that file and puts
 * it to `PUT /api/tasks/{id}/changes` with the host's token. The host never runs git over
 * the workspace: the worker controls its `.git`, and git reads run commands from it.
 *
 * The file is untrusted input: it must be a plain file, it must name the claimed task and
 * attempt, and the body is rebuilt field by field. The server validates and redacts again.
 */
import { constants as fsc } from "node:fs";
import { lstat, open } from "node:fs/promises";
import path from "node:path";
import { log } from "../log.js";

/** Where `argus-delegate` writes the snapshot; keep in step with engines/changes.mjs. */
export const CHANGES_RELAY_FILE = "argus-task-changes.json";
const READ_MAX = 1024 * 1024;
const MIN_GAP_MS = 10_000; // the host puts at most one snapshot per 10 s
const DRAIN_GAP_MS = 5_000; // the server takes one per 5 s
const DRAIN_MS = 6_000;
const BACKOFF_MIN_MS = 2_000;
const BACKOFF_MAX_MS = 30_000;
const CLAIM_LOST = ["stale_attempt", "not_assignee", "not_claimed"];

export interface ChangesTask {
  id: string;
  attempt: number;
}

export interface ChangesBody {
  attempt: number;
  at: string;
  branch: string;
  base: { ref: string; sha: string } | null;
  head: string | null;
  commits: Array<{ sha: string; subject: string }>;
  files: Array<{
    path: string;
    status: string;
    added: number | null;
    deleted: number | null;
    binary: boolean;
  }>;
  untracked: Array<{ path: string; bytes: number }>;
  patch: string;
  truncated: { files: boolean; patch: boolean };
}

const isObject = (x: unknown): x is Record<string, unknown> =>
  x !== null && typeof x === "object" && !Array.isArray(x);
const str = (x: unknown): x is string => typeof x === "string";
const count = (x: unknown): number | null =>
  Number.isSafeInteger(x) && (x as number) >= 0 ? (x as number) : null;
const list = (x: unknown, max: number): unknown[] => (Array.isArray(x) ? x.slice(0, max) : []);

/** The body for the claimed task, rebuilt from the file's record; null when it does not fit. */
export function changesBody(text: string, task: ChangesTask): ChangesBody | null {
  let rec: unknown;
  try {
    rec = JSON.parse(text);
  } catch {
    return null; // empty (just reset) or torn
  }
  if (!isObject(rec) || rec.v !== 1 || rec.task_id !== task.id || rec.attempt !== task.attempt)
    return null;
  if (!str(rec.at) || !str(rec.branch) || !str(rec.patch)) return null;
  const base = isObject(rec.base) && str(rec.base.ref) && str(rec.base.sha) ? rec.base : null;
  const truncated = isObject(rec.truncated) ? rec.truncated : {};
  return {
    attempt: task.attempt,
    at: rec.at,
    branch: rec.branch,
    base: base ? { ref: base.ref as string, sha: base.sha as string } : null,
    head: str(rec.head) ? rec.head : null,
    commits: list(rec.commits, 50)
      .filter((c): c is Record<string, unknown> => isObject(c) && str(c.sha) && str(c.subject))
      .map((c) => ({ sha: c.sha as string, subject: c.subject as string })),
    files: list(rec.files, 500)
      .filter((f): f is Record<string, unknown> => isObject(f) && str(f.path) && str(f.status))
      .map((f) => ({
        path: f.path as string,
        status: f.status as string,
        added: count(f.added),
        deleted: count(f.deleted),
        binary: f.binary === true,
      })),
    untracked: list(rec.untracked, 200)
      .filter((u): u is Record<string, unknown> => isObject(u) && str(u.path))
      .map((u) => ({ path: u.path as string, bytes: count(u.bytes) ?? 0 })),
    patch: rec.patch,
    truncated: { files: truncated.files === true, patch: truncated.patch === true },
  };
}

/**
 * Puts the newest snapshot for the claimed task, at most once per 10 s. 409 `stale_attempt`
 * / `not_assignee` / `not_claimed` and 401 stop it for good; 429, a network error or a 5xx
 * back off and retry the same snapshot; any other refusal drops that snapshot. Only status
 * and code are logged, never a header or the diff.
 */
export function createChangesRelay(opts: {
  file: string;
  task: ChangesTask;
  put: (body: ChangesBody) => Promise<unknown>;
  now?: () => number;
}) {
  const now = opts.now ?? Date.now;
  let stopped = false;
  let busy = false;
  let lastAt: string | null = null; // of the last snapshot put or dropped
  let lastPutAt = Number.NEGATIVE_INFINITY;
  let backoffMs = 0;
  let notBefore = 0;

  const unsafe = (why: string) => {
    stopped = true;
    log.warn("Task changes file is not a plain file; relay stopped", { task: opts.task.id, why });
  };

  async function readBody(): Promise<ChangesBody | null> {
    const dir = await lstat(path.dirname(opts.file));
    if (!dir.isDirectory()) {
      unsafe("directory");
      return null;
    }
    let handle: Awaited<ReturnType<typeof open>>;
    try {
      handle = await open(opts.file, fsc.O_RDONLY | fsc.O_NOFOLLOW | fsc.O_NONBLOCK);
    } catch (err) {
      const code = (err as NodeJS.ErrnoException)?.code;
      if (code === "ENOENT") return null;
      if (code === "ELOOP") {
        unsafe("symlink");
        return null;
      }
      throw err;
    }
    try {
      const st = await handle.stat();
      if (!st.isFile() || st.nlink !== 1) {
        unsafe(st.isFile() ? "hard link" : "not a file");
        return null;
      }
      if (st.size === 0 || st.size > READ_MAX) return null;
      const buf = Buffer.alloc(st.size);
      const { bytesRead } = await handle.read(buf, 0, buf.length, 0);
      return changesBody(buf.subarray(0, bytesRead).toString("utf8"), opts.task);
    } finally {
      await handle.close();
    }
  }

  /** "put", "none" (nothing new) or "wait" (new, but not yet allowed). */
  async function step(gapMs: number): Promise<"put" | "none" | "wait"> {
    const body = await readBody();
    if (stopped || !body || body.at === lastAt) return "none";
    if (now() < Math.max(notBefore, lastPutAt + gapMs)) return "wait";
    try {
      await opts.put(body);
      lastAt = body.at;
      lastPutAt = now();
      backoffMs = 0;
      return "put";
    } catch (err) {
      const status = (err as { status?: number })?.status;
      const code = (err as { code?: string })?.code ?? "";
      if (status === 401 || (status === 409 && CLAIM_LOST.includes(code))) {
        stopped = true;
        log.warn("Argus refuses the task changes; relay stopped", { task: opts.task.id, code });
      } else if (
        typeof status === "number" &&
        status >= 400 &&
        status < 500 &&
        ![408, 409, 425, 429].includes(status)
      ) {
        lastAt = body.at; // 413 / 422: this snapshot can never be accepted
        log.warn("Argus rejected a task changes snapshot; dropped", {
          task: opts.task.id,
          status,
          code,
        });
      } else {
        backoffMs = Math.min(Math.max(backoffMs * 2, BACKOFF_MIN_MS), BACKOFF_MAX_MS);
        notBefore = now() + backoffMs;
      }
      return "wait";
    }
  }

  async function guarded(gapMs: number): Promise<"put" | "none" | "wait"> {
    if (stopped) return "none";
    if (busy) return "wait";
    busy = true;
    try {
      return await step(gapMs);
    } catch (err) {
      log.warn("Task changes relay failed (will retry)", { err: String(err).slice(0, 200) });
      return "wait";
    } finally {
      busy = false;
    }
  }

  return {
    get stopped(): boolean {
      return stopped;
    },
    stop(): void {
      stopped = true;
    },
    /** Put the newest snapshot if it is new and 10 s have passed. Never throws. */
    poll: async (): Promise<void> => void (await guarded(MIN_GAP_MS)),
    /**
     * The attempt's last snapshot, before it ends: put it as soon as the server's
     * 5 s pace allows, within `timeoutMs`. Never throws.
     */
    async drain(timeoutMs = DRAIN_MS): Promise<void> {
      const deadline = Date.now() + timeoutMs;
      while (Date.now() < deadline) {
        const r = await guarded(DRAIN_GAP_MS);
        if (r !== "wait") return;
        await new Promise((res) =>
          setTimeout(res, Math.min(250, Math.max(0, deadline - Date.now()))),
        );
      }
    },
  };
}
