/**
 * Host-side relay of structured progress milestones (progress milestones spec 3.3).
 *
 * The delegate container holds no Argus token. `argus-delegate` appends one JSON line per
 * milestone to a file in the agent group's directory; this module reads it with the fenced
 * reader, checks every record against what the host itself knows (the claimed task and
 * attempt, the run, the closed kinds and their data shapes, the task's repository), and
 * posts `POST /api/tasks/{id}/progress` with the host's token.
 *
 * The file is untrusted input: the body is rebuilt field by field, the message is the
 * host's own, and a record that does not fit is dropped with a reason code. The same
 * milestone (task, attempt, kind, data) is posted once, whichever emitter produced it.
 */
import { log } from "../log.js";
import { createFencedReader } from "./argus-fenced-relay.js";
import { pullRequestsOf } from "./argus-single-layer.js";

/** Where the delegate appends milestones; keep in step with engines/milestones.mjs. */
export const MILESTONE_RELAY_FILE = "argus-milestones.jsonl";
const PENDING_MAX = 200;
const SEEN_MAX = 2000;
const BACKOFF_MIN_MS = 2000;
const BACKOFF_MAX_MS = 30_000;
const DRAIN_MS = 10_000;
const CLAIM_LOST = ["stale_attempt", "not_assignee", "not_claimed"];
/** `r1:<task id>:...` or the hashed `r1:<sha256>` form of engines/usage.mjs runKey. */
const RUN_KEY = /^r1:[A-Za-z0-9._:-]{1,196}$/;
const SHA = /^[0-9a-f]{40}$/;
/** waiting and waiting_input are the worker's own words (argus-task, the Claude denials), never relayed. */
const KINDS = [
  "commit_created",
  "pr_opened",
  "ci_running",
  "ci_passed",
  "ci_failed",
  "gate_failed",
] as const;
export type MilestoneKind = (typeof KINDS)[number];

export interface MilestoneTask {
  id: string;
  attempt: number;
}

export interface MilestoneBody {
  attempt: number;
  message: string;
  kind: MilestoneKind;
  data: Record<string, string | number>;
}

export type Checked = { ok: true; body: MilestoneBody; key: string } | { ok: false; why: string };

const isObject = (x: unknown): x is Record<string, unknown> =>
  x !== null && typeof x === "object" && !Array.isArray(x);
const exactly = (o: Record<string, unknown>, keys: string[]) =>
  Object.keys(o).length === keys.length && keys.every((k) => k in o);
/** Anything shaped like a token or a URL with credentials is never posted. */
const CREDENTIAL =
  /(?:bearer\s+\S|argus_[A-Za-z0-9]{8}|gh[pousr]_[A-Za-z0-9]{8}|github_pat_|sk-[A-Za-z0-9]{8}|:\/\/[^/\s]*@)/i;

const short = (sha: string) => sha.slice(0, 7);

/** The readable line that goes with a milestone; keep in step with engines/milestones.mjs describe. */
function describe(kind: MilestoneKind, d: Record<string, string | number>): string {
  switch (kind) {
    case "commit_created":
      return `Commit ${short(String(d.sha))} created on ${d.branch}`;
    case "pr_opened":
      return `Pull request #${d.number} opened`;
    case "ci_running":
      return `CI running for ${short(String(d.sha))}`;
    case "ci_passed":
      return `CI passed for ${short(String(d.sha))}`;
    case "ci_failed":
      return `CI failed for ${short(String(d.sha))}`;
    case "gate_failed":
      return `Gate failed: ${d.gate}`;
  }
}

/**
 * One record from the container against the host's own view. `repository` is the task's
 * repository URL: a pull request must belong to it (host, owner and name, any casing).
 */
export function checkMilestoneRecord(
  raw: unknown,
  task: MilestoneTask,
  repository: string | undefined,
): Checked {
  if (!isObject(raw) || raw.v !== 1) return { ok: false, why: "not_milestone" };
  if (raw.task_id !== task.id || raw.attempt !== task.attempt)
    return { ok: false, why: "other_task" };
  if (typeof raw.run_key !== "string" || !RUN_KEY.test(raw.run_key))
    return { ok: false, why: "bad_run_key" };
  // A run key names its task (the hashed form of a very long key cannot be checked).
  if (!/^r1:[0-9a-f]{64}$/.test(raw.run_key)) {
    const named = raw.run_key.split(":")[1];
    if (named !== task.id.replace(/[^A-Za-z0-9._-]/g, "_"))
      return { ok: false, why: "run_mismatch" };
  }
  const kind = KINDS.find((k) => k === raw.kind);
  if (!kind) return { ok: false, why: "bad_kind" };
  const d = raw.data;
  if (!isObject(d)) return { ok: false, why: "bad_data" };
  let data: Record<string, string | number>;
  switch (kind) {
    case "commit_created": {
      if (!exactly(d, ["branch", "sha"])) return { ok: false, why: "bad_data" };
      const { branch, sha } = d;
      if (
        typeof branch !== "string" ||
        !branch ||
        branch.length > 250 ||
        /[\0-\x1f\x7f]/.test(branch)
      )
        return { ok: false, why: "bad_data" };
      if (typeof sha !== "string" || !SHA.test(sha)) return { ok: false, why: "bad_sha" };
      data = { branch, sha };
      break;
    }
    case "ci_running":
    case "ci_passed":
    case "ci_failed": {
      if (!exactly(d, ["sha"])) return { ok: false, why: "bad_data" };
      if (typeof d.sha !== "string" || !SHA.test(d.sha)) return { ok: false, why: "bad_sha" };
      data = { sha: d.sha };
      break;
    }
    case "gate_failed": {
      const { gate } = d;
      if (!exactly(d, ["gate"]) || typeof gate !== "string") return { ok: false, why: "bad_data" };
      if (!gate || gate.length > 200 || /[\0-\x1f\x7f]/.test(gate))
        return { ok: false, why: "bad_data" };
      data = { gate };
      break;
    }
    case "pr_opened": {
      const { number, url } = d;
      if (!exactly(d, ["number", "url"]) || typeof url !== "string" || url.length > 500)
        return { ok: false, why: "bad_data" };
      if (typeof number !== "number" || !Number.isSafeInteger(number) || number < 1)
        return { ok: false, why: "bad_data" };
      const [pr] = pullRequestsOf(repository, url);
      if (!pr || pr.number !== number || pr.url !== url)
        return { ok: false, why: "foreign_repository" };
      data = { number, url };
      break;
    }
  }
  const message = describe(kind, data);
  if (CREDENTIAL.test(message) || CREDENTIAL.test(JSON.stringify(data)))
    return { ok: false, why: "credential_like" };
  return {
    ok: true,
    body: { attempt: task.attempt, message, kind, data },
    key: JSON.stringify([task.id, task.attempt, kind, Object.entries(data).sort()]),
  };
}

/**
 * Reads the container's milestone file and posts each valid record once. Network errors,
 * 408/425/429 (the API takes one note per kind per 10 s) and 5xx keep the record and back
 * off. A lost claim (409 stale_attempt, not_assignee, not_claimed) or 401 stops the relay;
 * any other refusal drops that record.
 */
export function createMilestoneRelay(opts: {
  file: string;
  task: MilestoneTask;
  /** The task's repository URL, for pull request ownership. */
  repository: string | undefined;
  post: (body: MilestoneBody) => Promise<unknown>;
  /** True when another emitter already posted this milestone (the channel's own pr_opened). */
  alreadyPosted?: (body: MilestoneBody) => boolean;
  /** Called after the API accepted a milestone, so the other emitter does not repeat it. */
  onPosted?: (body: MilestoneBody) => void;
  now?: () => number;
}) {
  const now = opts.now ?? Date.now;
  const reader = createFencedReader({ file: opts.file });
  const pending: { body: MilestoneBody; key: string }[] = [];
  const seen = new Set<string>(); // insertion-ordered; the oldest leaves at SEEN_MAX
  let stopped = false;
  let busy = false;
  let backoffMs = 0;
  let notBefore = 0;
  const stats = { posted: 0, dropped: 0 };
  const dropped = new Map<string, number>();

  const drop = (why: string) => {
    stats.dropped += 1;
    dropped.set(why, (dropped.get(why) ?? 0) + 1);
    log.warn("Argus milestone record dropped", { task: opts.task.id, why });
  };
  const remember = (key: string) => {
    seen.add(key);
    if (seen.size > SEEN_MAX) seen.delete(seen.values().next().value as string);
  };

  async function readNew(): Promise<void> {
    const got = await reader.read();
    if (reader.refused) {
      stopped = true;
      log.warn("Milestone relay file is not a plain file; relay stopped", {
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
      const checked = checkMilestoneRecord(rec, opts.task, opts.repository);
      if (!checked.ok) {
        if (checked.why !== "other_task") drop(checked.why);
        continue;
      }
      if (seen.has(checked.key)) continue;
      remember(checked.key);
      if (pending.length >= PENDING_MAX) pending.shift();
      pending.push({ body: checked.body, key: checked.key });
    }
  }

  async function postPending(): Promise<void> {
    while (pending.length && now() >= notBefore) {
      const item = pending[0];
      if (opts.alreadyPosted?.(item.body)) {
        pending.shift();
        continue;
      }
      try {
        await opts.post(item.body);
        pending.shift();
        opts.onPosted?.(item.body);
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
          pending.length = 0;
          log.warn("Argus refuses milestones (claim lost); relay stopped", {
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
      log.warn("Milestone relay failed (will retry)", { err: String(err).slice(0, 200) });
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
    /** Before the attempt ends: read and post everything, waiting out a back-off, for at most `timeoutMs`. */
    async drain(timeoutMs = DRAIN_MS): Promise<{ left: number }> {
      const deadline = Date.now() + timeoutMs;
      for (;;) {
        const ran = await poll();
        if (stopped || (ran && !pending.length && reader.unread === 0) || Date.now() >= deadline)
          break;
        const wait = Math.max(50, Math.min(notBefore - now(), deadline - Date.now()));
        await new Promise((r) => setTimeout(r, wait));
      }
      return { left: pending.length };
    },
  };
}
