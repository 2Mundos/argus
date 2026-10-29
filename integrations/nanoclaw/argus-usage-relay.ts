/**
 * Host-side relay of token usage (account usage spec 3.8, 3.9).
 *
 * The agent container holds no Argus token. `argus-delegate` (executor role) and the
 * agent runner's harness hook (engine role) append usage records to a file in the agent
 * group's directory, which the container mounts. This module reads that file, checks every
 * record against what the host itself knows (the claimed task and attempt, and the accounts
 * the agent really holds), and posts `POST /api/accounts/{id}/usage` with the host's token.
 *
 * The file is untrusted input: records name a task, attempt, role and account, but the
 * account that is billed is always the one this host leased for that role, and the body is
 * rebuilt field by field. A record that does not fit is dropped with a reason code.
 */
import { constants as fsc } from "node:fs";
import { lstat, open } from "node:fs/promises";
import path from "node:path";
import { log } from "../log.js";

/** Where the container appends usage records; keep in step with engines/usage.mjs. */
export const USAGE_RELAY_FILE = "argus-usage.jsonl";
const READ_MAX = 1024 * 1024; // per poll
const PENDING_MAX = 500;
const BACKOFF_MIN_MS = 2000;
const BACKOFF_MAX_MS = 30_000;
const DRAIN_MS = 10_000;
const RUN_KEY = /^[A-Za-z0-9._:-]{1,200}$/;
const REASON = /^[a-z0-9_]{1,60}$/;
const ROLES = ["engine", "executor"] as const;
type Role = (typeof ROLES)[number];

export interface UsageTask {
  id: string;
  attempt: number;
}

/** The account ids this agent holds now (null: not leased). */
export interface HeldAccountIds {
  engine: string | null;
  executor: string | null;
}

export interface UsageBody {
  task_id: string;
  attempt: number;
  model: string | null;
  engine: string | null;
  role: Role;
  measurement: "measured" | "not_reported";
  input_tokens: number | null;
  output_tokens: number | null;
  cached_input_tokens: number | null;
  cost_usd: number | null;
  reason?: string;
  run_key: string;
}

export type Checked = { ok: true; accountId: string; body: UsageBody } | { ok: false; why: string };

const isObject = (x: unknown): x is Record<string, unknown> =>
  x !== null && typeof x === "object" && !Array.isArray(x);
const count = (x: unknown): number | null | undefined =>
  x === null || x === undefined
    ? null
    : Number.isSafeInteger(x) && (x as number) >= 0
      ? (x as number)
      : undefined;
const money = (x: unknown): number | null | undefined =>
  x === null || x === undefined
    ? null
    : typeof x === "number" && Number.isFinite(x) && x >= 0
      ? x
      : undefined;
const label = (x: unknown, max: number): string | null =>
  typeof x === "string" && x.trim() ? x.slice(0, max) : null;

/**
 * One record from the container against the host's own view. The account a record names
 * must be the one held for its role; an executor without a lease of its own runs on the
 * engine's account (the same lease, never a second one).
 */
export function checkUsageRecord(raw: unknown, task: UsageTask, held: HeldAccountIds): Checked {
  if (!isObject(raw) || raw.v !== 1 || raw.kind !== "usage") return { ok: false, why: "not_usage" };
  if (raw.task_id !== task.id || raw.attempt !== task.attempt)
    return { ok: false, why: "other_task" };
  const role = ROLES.find((r) => r === raw.role);
  if (!role) return { ok: false, why: "bad_role" };
  const accountId = role === "executor" ? (held.executor ?? held.engine) : held.engine;
  if (!accountId) return { ok: false, why: "no_account_held" };
  if (raw.account_id !== accountId) return { ok: false, why: "account_mismatch" };
  const p = raw.payload;
  if (!isObject(p) || p.task_id !== task.id) return { ok: false, why: "bad_payload" };
  if (typeof p.run_key !== "string" || !RUN_KEY.test(p.run_key))
    return { ok: false, why: "bad_run_key" };
  const input = count(p.input_tokens);
  const output = count(p.output_tokens);
  const cached = count(p.cached_input_tokens);
  const cost = money(p.cost_usd);
  if ([input, output, cached, cost].includes(undefined)) return { ok: false, why: "bad_figures" };
  const figures = [input, output, cached, cost] as (number | null)[];
  const body: UsageBody = {
    task_id: task.id,
    attempt: task.attempt,
    model: label(p.model, 100),
    engine: label(p.engine, 50),
    role,
    measurement: p.measurement === "not_reported" ? "not_reported" : "measured",
    input_tokens: figures[0],
    output_tokens: figures[1],
    cached_input_tokens: figures[2],
    cost_usd: figures[3],
    run_key: p.run_key,
  };
  if (p.measurement !== "measured" && p.measurement !== "not_reported")
    return { ok: false, why: "bad_measurement" };
  if (body.measurement === "not_reported") {
    // The API rejects figures on an unmeasured run; a missing figure is never a zero.
    if (figures.some((f) => f !== null)) return { ok: false, why: "unmeasured_with_figures" };
    body.reason = typeof p.reason === "string" && REASON.test(p.reason) ? p.reason : "unknown";
  } else if (figures.every((f) => f === null)) {
    return { ok: false, why: "measured_without_figures" };
  }
  return { ok: true, accountId, body };
}

interface Post {
  (accountId: string, body: UsageBody): Promise<unknown>;
}

/**
 * Reads the container's usage file and posts each valid record once. Transient failures
 * (network, 5xx, 429, 409 conflicts) keep the record and back off; a permanent refusal
 * (403/404/422/...) drops it, as does 401 (the channel stops anyway). The API deduplicates
 * on `run_key`, so a record posted twice (a retry after a lost response) adds no row.
 */
export function createUsageRelay(opts: {
  file: string;
  task: UsageTask;
  held: () => HeldAccountIds;
  post: Post;
  now?: () => number;
}) {
  const now = opts.now ?? Date.now;
  let offset = 0;
  let tail = "";
  let pending: { accountId: string; body: UsageBody }[] = [];
  const seen = new Set<string>(); // run_keys read in this relay: a rewritten file does not repost
  let stopped = false;
  let busy = false;
  let backoffMs = 0;
  let notBefore = 0;
  let fileSize = 0;
  const stats = { posted: 0, dropped: 0 };
  const dropped = new Map<string, number>();

  const drop = (why: string) => {
    stats.dropped += 1;
    dropped.set(why, (dropped.get(why) ?? 0) + 1);
    log.warn("Argus usage record dropped", { task: opts.task.id, why });
  };

  async function readNew(): Promise<void> {
    const st0 = await lstat(path.dirname(opts.file));
    if (!st0.isDirectory()) throw new Error("usage relay directory is not a plain directory");
    let handle: Awaited<ReturnType<typeof open>>;
    try {
      handle = await open(opts.file, fsc.O_RDONLY | fsc.O_NOFOLLOW | fsc.O_NONBLOCK);
    } catch (err) {
      const code = (err as NodeJS.ErrnoException)?.code;
      if (code === "ENOENT") return;
      if (code === "ELOOP") {
        stopped = true;
        log.warn("Usage relay file is a symlink; relay stopped", { task: opts.task.id });
        return;
      }
      throw err;
    }
    try {
      const st = await handle.stat();
      if (!st.isFile() || st.nlink !== 1) {
        stopped = true;
        log.warn("Usage relay file is not a plain file; relay stopped", { task: opts.task.id });
        return;
      }
      fileSize = st.size;
      if (st.size < offset) {
        offset = 0; // replaced or truncated: read again; seen run_keys are not reposted
        tail = "";
      }
      if (st.size === offset) return;
      const buf = Buffer.alloc(Math.min(st.size - offset, READ_MAX));
      const { bytesRead } = await handle.read(buf, 0, buf.length, offset);
      offset += bytesRead;
      const text = tail + buf.subarray(0, bytesRead).toString("utf8");
      const lines = text.split("\n");
      tail = lines.pop() ?? ""; // an unfinished record waits for its newline
      if (tail.length > READ_MAX) tail = ""; // one absurd line: skip it
      for (const line of lines) {
        if (!line.trim()) continue;
        let rec: unknown;
        try {
          rec = JSON.parse(line);
        } catch {
          continue; // a torn line
        }
        const checked = checkUsageRecord(rec, opts.task, opts.held());
        if (!checked.ok) {
          if (checked.why !== "other_task") drop(checked.why);
          continue;
        }
        if (seen.has(checked.body.run_key)) continue;
        seen.add(checked.body.run_key);
        if (pending.length >= PENDING_MAX) pending.shift();
        pending.push({ accountId: checked.accountId, body: checked.body });
      }
    } finally {
      await handle.close();
    }
  }

  async function postPending(): Promise<void> {
    while (pending.length && now() >= notBefore) {
      const item = pending[0];
      try {
        await opts.post(item.accountId, item.body);
        pending.shift();
        stats.posted += 1;
        backoffMs = 0;
      } catch (err) {
        const status = (err as { status?: number })?.status;
        if (status === 401) {
          stopped = true;
          return;
        }
        // 4xx other than 408/409/425/429 cannot succeed on retry (403 not the holder, 404, 422).
        if (
          typeof status === "number" &&
          status >= 400 &&
          status < 500 &&
          ![408, 409, 425, 429].includes(status)
        ) {
          pending.shift();
          drop(`http_${status}`);
          continue;
        }
        backoffMs = Math.min(Math.max(backoffMs * 2, BACKOFF_MIN_MS), BACKOFF_MAX_MS);
        notBefore = now() + backoffMs;
        return;
      }
    }
  }

  /** Read what is new and post what is due. Never throws; false when a poll is in flight. */
  async function poll(): Promise<boolean> {
    if (stopped) return true;
    if (busy) return false;
    busy = true;
    try {
      await readNew();
      await postPending();
    } catch (err) {
      log.warn("Usage relay failed (will retry)", { err: String(err).slice(0, 200) });
    } finally {
      busy = false;
    }
    return true;
  }

  return {
    get stopped(): boolean {
      return stopped;
    },
    get pending(): number {
      return pending.length;
    },
    get stats(): { posted: number; dropped: number; reasons: Record<string, number> } {
      return { ...stats, reasons: Object.fromEntries(dropped) };
    },
    stop(): void {
      stopped = true;
    },
    poll: async (): Promise<void> => void (await poll()),
    /**
     * Before the attempt ends (its leases are released with it, and the API takes usage
     * only from a holder): read and post everything, waiting out a back-off, for at most
     * `timeoutMs`. Whatever is left stays in the container's file and the delegate's spool.
     */
    async drain(timeoutMs = DRAIN_MS): Promise<{ left: number }> {
      const deadline = Date.now() + timeoutMs;
      for (;;) {
        const ran = await poll();
        const unread = fileSize > offset;
        if (stopped || (ran && !pending.length && !unread) || Date.now() >= deadline) break;
        const wait = Math.max(50, Math.min(notBefore - now(), deadline - Date.now()));
        await new Promise((r) => setTimeout(r, wait));
      }
      return { left: pending.length };
    },
  };
}
