/**
 * Host-side relay of provisional executor usage (task cost spec 3.8, account usage spec 3.9).
 *
 * The delegate container holds no Argus token. While a Claude executor runs, `argus-delegate`
 * appends the run's cumulative usage so far, one JSON line per new revision, to a file in the
 * agent group's directory. This module reads it with the fenced reader, checks every record
 * against what the host itself knows (the claimed task and attempt, the executor account it
 * leased, the run) and posts `POST /api/accounts/{id}/usage/provisional` with the host's token.
 *
 * The file is untrusted input: the body is rebuilt field by field and a record that does not
 * fit is dropped with a reason code. Only the newest revision of each run is posted; the API
 * ignores a revision it already has, so a replay costs nothing.
 */
import { log } from "../log.js";
import { createFencedReader } from "./argus-fenced-relay.js";
import type { HeldAccountIds } from "./argus-usage-relay.js";

/** Where the delegate appends provisional usage; keep in step with engines/usage.mjs. */
export const PROVISIONAL_RELAY_FILE = "argus-usage-provisional.jsonl";
const RUNS_MAX = 50;
const BACKOFF_MIN_MS = 2000;
const BACKOFF_MAX_MS = 30_000;
const DRAIN_MS = 10_000;
const CLAIM_LOST = ["stale_attempt", "not_assignee", "not_claimed"];
/** `r1:<task id>:...` or the hashed `r1:<sha256>` form of engines/usage.mjs runKey. */
const RUN_KEY = /^r1:[A-Za-z0-9._:-]{1,196}$/;

export interface ProvisionalTask {
  id: string;
  attempt: number;
}

export interface ProvisionalBody {
  task_id: string;
  attempt: number;
  run_key: string;
  revision: number;
  model: string | null;
  engine: string | null;
  input_tokens: number;
  output_tokens: number;
  cached_input_tokens: number;
}

export type Checked =
  | { ok: true; accountId: string; body: ProvisionalBody }
  | { ok: false; why: string };

const isObject = (x: unknown): x is Record<string, unknown> =>
  x !== null && typeof x === "object" && !Array.isArray(x);
const count = (x: unknown): x is number => Number.isSafeInteger(x) && (x as number) >= 0;
const label = (x: unknown, max: number): string | null =>
  typeof x === "string" && x.trim() ? x.slice(0, max) : null;

/**
 * One record from the container against the host's own view. The account must be the one
 * this host leased for the executor (or the engine's, when the executor has none of its own).
 */
export function checkProvisionalRecord(
  raw: unknown,
  task: ProvisionalTask,
  held: HeldAccountIds,
): Checked {
  if (!isObject(raw) || raw.v !== 1 || raw.kind !== "usage_provisional")
    return { ok: false, why: "not_provisional" };
  if (raw.task_id !== task.id || raw.attempt !== task.attempt)
    return { ok: false, why: "other_task" };
  const accountId = held.executor ?? held.engine;
  if (!accountId) return { ok: false, why: "no_account_held" };
  if (raw.account_id !== accountId) return { ok: false, why: "account_mismatch" };
  if (typeof raw.run_key !== "string" || !RUN_KEY.test(raw.run_key))
    return { ok: false, why: "bad_run_key" };
  // A run key names its task (the hashed form of a very long key cannot be checked).
  if (!/^r1:[0-9a-f]{64}$/.test(raw.run_key)) {
    const named = raw.run_key.split(":")[1];
    if (named !== task.id.replace(/[^A-Za-z0-9._-]/g, "_"))
      return { ok: false, why: "run_mismatch" };
  }
  if (!count(raw.revision) || raw.revision < 1) return { ok: false, why: "bad_revision" };
  const { input_tokens, output_tokens, cached_input_tokens } = raw;
  if (!count(input_tokens) || !count(output_tokens) || !count(cached_input_tokens))
    return { ok: false, why: "bad_figures" };
  return {
    ok: true,
    accountId,
    body: {
      task_id: task.id,
      attempt: task.attempt,
      run_key: raw.run_key,
      revision: raw.revision,
      model: label(raw.model, 100),
      engine: label(raw.engine, 50),
      input_tokens,
      output_tokens,
      cached_input_tokens,
    },
  };
}

/**
 * Reads the delegate's provisional usage file and posts the newest revision of each run.
 * Network errors, 408/409/425/429 and 5xx keep it and back off. A lost claim (409
 * stale_attempt, not_assignee, not_claimed) or 401 stops the relay; any other refusal drops
 * that revision (a newer one is still posted).
 */
export function createProvisionalUsageRelay(opts: {
  file: string;
  task: ProvisionalTask;
  held: () => HeldAccountIds;
  post: (accountId: string, body: ProvisionalBody) => Promise<unknown>;
  now?: () => number;
}) {
  const now = opts.now ?? Date.now;
  const reader = createFencedReader({ file: opts.file });
  // run_key -> newest unposted revision; posted holds the newest revision the API took.
  const pending = new Map<string, { accountId: string; body: ProvisionalBody }>();
  const posted = new Map<string, number>();
  let stopped = false;
  let busy = false;
  let backoffMs = 0;
  let notBefore = 0;
  const stats = { posted: 0, dropped: 0 };
  const dropped = new Map<string, number>();

  const drop = (why: string) => {
    stats.dropped += 1;
    dropped.set(why, (dropped.get(why) ?? 0) + 1);
    log.warn("Argus provisional usage record dropped", { task: opts.task.id, why });
  };

  async function readNew(): Promise<void> {
    const got = await reader.read();
    if (reader.refused) {
      stopped = true;
      log.warn("Provisional usage file is not a plain file; relay stopped", {
        task: opts.task.id,
        why: reader.refused,
      });
      return;
    }
    for (let i = 0; i < got.oversized; i += 1) drop("line_too_long");
    for (const line of got.lines) {
      let rec: unknown;
      try {
        rec = JSON.parse(line);
      } catch {
        drop("torn_line");
        continue;
      }
      const checked = checkProvisionalRecord(rec, opts.task, opts.held());
      if (!checked.ok) {
        if (checked.why !== "other_task") drop(checked.why);
        continue;
      }
      const key = checked.body.run_key;
      const newest = Math.max(posted.get(key) ?? 0, pending.get(key)?.body.revision ?? 0);
      if (checked.body.revision <= newest) continue;
      if (!pending.has(key) && !posted.has(key) && posted.size + pending.size >= RUNS_MAX) {
        drop("too_many_runs");
        continue;
      }
      pending.set(key, { accountId: checked.accountId, body: checked.body });
    }
  }

  async function postPending(): Promise<void> {
    while (pending.size && now() >= notBefore) {
      const [key, item] = pending.entries().next().value as [
        string,
        { accountId: string; body: ProvisionalBody },
      ];
      try {
        await opts.post(item.accountId, item.body);
        // A newer revision read while this one was in flight stays pending.
        if (pending.get(key) === item) pending.delete(key);
        posted.set(key, item.body.revision);
        stats.posted += 1;
        backoffMs = 0;
      } catch (err) {
        const { status, code } = err as { status?: number; code?: string };
        if (status === 401) {
          stopped = true;
          return;
        }
        if (status === 409 && CLAIM_LOST.includes(String(code))) {
          stopped = true;
          pending.clear();
          log.warn("Argus refuses provisional usage (claim lost); relay stopped", {
            task: opts.task.id,
            code,
          });
          return;
        }
        if (
          typeof status === "number" &&
          status >= 400 &&
          status < 500 &&
          ![408, 409, 425, 429].includes(status)
        ) {
          if (pending.get(key) === item) pending.delete(key);
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
      log.warn("Provisional usage relay failed (will retry)", { err: String(err).slice(0, 200) });
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
      return pending.size;
    },
    get stats(): { posted: number; dropped: number; reasons: Record<string, number> } {
      return { ...stats, reasons: Object.fromEntries(dropped) };
    },
    stop(): void {
      stopped = true;
    },
    poll: async (): Promise<void> => void (await poll()),
    /** Before the attempt ends: read and post everything, waiting out a back-off, for at most `timeoutMs`. */
    async drain(timeoutMs = DRAIN_MS): Promise<{ left: number }> {
      const deadline = Date.now() + timeoutMs;
      for (;;) {
        const ran = await poll();
        if (stopped || (ran && !pending.size && reader.unread === 0) || Date.now() >= deadline)
          break;
        const wait = Math.max(50, Math.min(notBefore - now(), deadline - Date.now()));
        await new Promise((r) => setTimeout(r, wait));
      }
      return { left: pending.size };
    },
  };
}
