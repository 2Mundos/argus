#!/usr/bin/env node
// Deterministic task helpers for workers (argus-task). Spec: docs/specs/task-helpers.spec.md.
// Built on the forge clients (argus-gitea-api / argus-github-api): no token, no HTTP
// client of its own. Each command prints one JSON line and exits with a code from EXIT.
import { execFileSync } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import {
  closeSync,
  existsSync,
  constants as fsc,
  fstatSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  readSync,
  realpathSync,
  renameSync,
  rmSync,
  writeFileSync,
  writeSync,
} from "node:fs";
import { basename, dirname, isAbsolute, join, relative, resolve } from "node:path";
import {
  createGatewayFetch,
  errorPayload,
  GiteaError,
  internals,
  isAgenticReviewComment,
  parseArgs,
} from "./gitea-api.mjs";
import { repositoryClient } from "./github-api.mjs";

const { usage, policy, positiveNumber, plainText, timestamp } = internals;

export const EXIT = Object.freeze({
  ok: 0,
  error: 1,
  usage: 2,
  pending: 10,
  interrupted: 11,
  not_ready: 12,
});

const DEFAULT_INTERVAL_S = 30;
const MIN_INTERVAL_S = 10;
const DEFAULT_MAX_S = 110; // under the shortest engine shell timeout we know of (spec 3.8)
const CEILING_MAX_S = 540;
const MARKER_TICK_MS = 1000;
const PROBE_REQUEST_MS = 10_000; // per forge request inside a wait
const TRANSIENT = new Set(["network_error", "server_error", "rate_limited", "ambiguous"]);
const DIGEST_BYTES = 2048;
const MIN_EXCERPT_BYTES = 64;
const TAIL_LINES = 20;
const LOG_BYTES = 20 * 1024 * 1024;
const ERROR_LINE =
  /\b(?:errors?|failed|failure|fail|traceback|panic)\b|[a-z](?:Error|Exception)\b|\bassert|exit (?:code|status) [1-9]/i;
const ANSI = /\x1b\[[0-9;]*[A-Za-z]/g;
const TIMESTAMP = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\s?/;
const AGENTIC_REVIEW = /agentic[\s_-]*review/i;

// ---- git and the .git/argus/ file channel ----

function defaultGit(args, cwd) {
  return execFileSync("git", args, {
    cwd,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  }).trim();
}

/** `<git dir>/argus`, where the host and the helper exchange signals; null outside a repository. */
export function signalDir(git, cwd) {
  try {
    return join(git(["rev-parse", "--absolute-git-dir"], cwd), "argus");
  } catch {
    return null;
  }
}

function readJson(file) {
  try {
    return JSON.parse(readFileSync(file, "utf8"));
  } catch {
    return null;
  }
}

function writeJson(file, value) {
  const temp = `${file}.${process.pid}.tmp`;
  writeFileSync(temp, `${JSON.stringify(value)}\n`);
  renameSync(temp, file);
}

/** A steer the host wrote after `sinceMs`, or null. */
function interruptAfter(dir, sinceMs) {
  const marker = dir ? readJson(join(dir, "interrupt")) : null;
  const at = Date.parse(marker?.at ?? "");
  return Number.isFinite(at) && at > sinceMs ? { reason: "steer", at: marker.at } : null;
}

// ---- CI ----

const FAILED_STATES = new Set(["failure", "error", "cancelled", "timed_out"]);
const PENDING_STATES = new Set([
  "pending",
  "queued",
  "in_progress",
  "waiting",
  "requested",
  "running",
]);

/** One of failure, pending, success, skipped or unknown; anything unrecognized is unknown. */
function canonicalState(state) {
  if (typeof state !== "string") return "unknown";
  if (FAILED_STATES.has(state)) return "failure";
  if (PENDING_STATES.has(state)) return "pending";
  if (state === "success" || state === "neutral") return "success";
  return state === "skipped" ? "skipped" : "unknown";
}

/** Newest status per context; agentic-review contexts are review, not CI. */
export function ciSummary(statuses) {
  const latest = new Map();
  for (const s of Array.isArray(statuses) ? statuses : []) {
    if (AGENTIC_REVIEW.test(s.context ?? "")) continue;
    const prev = latest.get(s.context);
    if (!prev || (s.id ?? 0) > (prev.id ?? 0)) latest.set(s.context, s);
  }
  const checks = [...latest.values()].map((s) => ({
    context: String(s.context ?? "ci").slice(0, 200),
    state: canonicalState(s.status ?? s.state),
  }));
  const failed = checks.filter((c) => c.state === "failure").map((c) => c.context);
  const running = checks.filter((c) => c.state === "pending").length;
  const has = (state) => checks.some((c) => c.state === state);
  // failure > pending > unknown > success > skipped; skipped only when every check is.
  const state = !checks.length
    ? "missing"
    : failed.length
      ? "failure"
      : running
        ? "pending"
        : has("unknown")
          ? "unknown"
          : has("success")
            ? "success"
            : "skipped";
  return { state, total: checks.length, running, failed };
}

async function headSha(api, number, sha) {
  if (sha !== undefined) return internals.commitSha(sha);
  const pr = await api.getPullRequest(number);
  if (typeof pr?.head?.sha !== "string")
    throw new GiteaError("bad_response", "PR response has no head SHA");
  return pr.head.sha;
}

// ---- wait ----

function seconds(value, name, fallback, min, max) {
  if (value === undefined) return fallback;
  const n = Number(value);
  if (!Number.isInteger(n) || n < min) throw usage(`--${name} must be a whole number >= ${min}`);
  return Math.min(n, max);
}

/** The cap for this call: --max, never above ARGUS_TASK_WAIT_MAX_S (or the default). */
export function waitCap(option, env) {
  const configured = seconds(
    env.ARGUS_TASK_WAIT_MAX_S || undefined,
    "max",
    DEFAULT_MAX_S,
    1,
    CEILING_MAX_S,
  );
  return seconds(option, "max", configured, 1, configured);
}

async function checkCi(api, o) {
  const sha = await headSha(api, o.number, o.sha);
  const ci = ciSummary(await api.ciStatuses(sha));
  // Done only when every check has finished, even after one has failed (spec 3.3).
  if (ci.state !== "missing" && ci.running === 0) return { done: true, sha, ...ci };
  const failedSoFar = ci.failed.length ? `, ${ci.failed.length} failed` : "";
  const status =
    ci.state === "missing"
      ? "no checks reported yet"
      : `${ci.running} of ${ci.total} checks running${failedSoFar}`;
  return { done: false, sha, status, ...ci };
}

const reviewTime = (r) => r.submitted_at ?? r.updated_at ?? r.created_at ?? null;

async function checkReview(api, o, since) {
  const sinceMs = Date.parse(since);
  const comments = (await api.listComments(o.number, { since, all: true, limit: 50 })).items;
  const reviews = (await api.listReviews(o.number, { all: true, limit: 50 })).items;
  const items = [
    ...comments.map((c) => ({
      kind: isAgenticReviewComment(c.body) ? "agentic_review" : "comment",
      comment_id: c.id,
      author: c.user?.login ?? null,
      at: c.updated_at ?? c.created_at ?? null,
    })),
    ...reviews.map((r) => ({
      kind: "review",
      review_id: r.id,
      state: r.state ?? null,
      author: r.user?.login ?? null,
      at: reviewTime(r),
    })),
  ].filter((i) => Date.parse(i.at ?? "") > sinceMs);
  if (!items.length) return { done: false, status: "no new reviews or comments" };
  const cursor = items.reduce((max, i) => (i.at > max ? i.at : max), since);
  return { done: true, items, cursor };
}

async function wait(api, o, ctx) {
  const on = o.on;
  if (on !== "ci" && on !== "review") throw usage("wait takes ci or review");
  positiveNumber(o.number);
  const intervalMs =
    seconds(o.interval, "interval", DEFAULT_INTERVAL_S, MIN_INTERVAL_S, CEILING_MAX_S) * 1000;
  const capMs = waitCap(o.max, ctx.env) * 1000;
  const start = ctx.now();
  const ref = `#${o.number}`;
  const file = ctx.dir ? join(ctx.dir, "waiting.json") : null;
  // A re-call for the same condition keeps the original start, so the dashboard's
  // timer and the review cursor survive the cap.
  const previous = file ? readJson(file) : null;
  const same = previous?.on === on && previous?.ref === ref;
  const since = same ? previous.since : new Date(start).toISOString();
  const cursor =
    on === "review" ? (o.since === undefined ? since : timestamp(o.since, "since")) : null;
  if (file) {
    mkdirSync(ctx.dir, { recursive: true });
    writeJson(file, { on, ref, since });
  }
  const clear = () => file && rmSync(file, { force: true });
  // One deadline bounds the whole wait: every exchange (each page, the head lookup) checks it
  // before it starts and is cut to the time left. No retries inside a wait (the next check is
  // the retry), so there is no backoff to bound either.
  const deadline = start + capMs;
  api.maxRetries = 0;
  api.timeoutMs = PROBE_REQUEST_MS;
  api.deadline = { at: deadline, now: ctx.now };
  const pending = (check) => {
    // The waiting file stays: the agent is expected to call again.
    const { done: _, ...rest } = check;
    return {
      code: EXIT.pending,
      result: { on, ref, waited_s: (ctx.now() - start) / 1000, cursor, ...rest },
    };
  };
  try {
    for (;;) {
      const steer = interruptAfter(ctx.dir, start);
      if (steer) {
        clear();
        return { code: EXIT.interrupted, result: { on, ref, interrupted: steer.reason } };
      }
      let check;
      try {
        check = on === "ci" ? await checkCi(api, o) : await checkReview(api, o, cursor);
      } catch (error) {
        if (!(error instanceof GiteaError)) throw error;
        if (error.code === "deadline_exceeded") return pending({ status: "wait budget spent" });
        if (!TRANSIENT.has(error.code)) throw error;
        check = { done: false, status: `forge not answering (${error.code}); will check again` };
      }
      // A check that finished after the budget is late: report pending, never success.
      if (check.done && ctx.now() >= deadline) check = { done: false, status: "wait budget spent" };
      if (check.done) {
        clear();
        return {
          code: EXIT.ok,
          result: { on, ref, waited_s: (ctx.now() - start) / 1000, ...check },
        };
      }
      const nextCheck = ctx.now() + intervalMs;
      for (;;) {
        if (ctx.now() >= deadline) return pending(check);
        if (ctx.now() >= nextCheck || interruptAfter(ctx.dir, start)) break;
        await ctx.sleep(MARKER_TICK_MS);
      }
    }
  } catch (error) {
    clear();
    throw error;
  } finally {
    api.deadline = null;
  }
}

// ---- pr-ready ----

const CHANGES_REQUESTED = new Set(["REQUEST_CHANGES", "CHANGES_REQUESTED"]);

/**
 * The forge agentic-review policy the Argus channel put in ARGUS_FORGE_REVIEW from the claim
 * (settings 3.12): "required" | "off". Unset means a standalone helper outside an Argus
 * session (legacy: --skip-review still applies). Anything else is unknown and fails closed
 * as required, and --skip-review never relaxes a delivered policy.
 */
export function reviewPolicy(env) {
  const v = env.ARGUS_FORGE_REVIEW;
  if (v === undefined || v === "") return "unset";
  return v === "off" ? "off" : "required";
}

async function prReady(api, o, ctx) {
  const policy = reviewPolicy(ctx.env);
  // Missing review is a reason only when policy requires one; an existing review is always read.
  const needsReview = policy === "required" || (policy === "unset" && !o["skip-review"]);
  const readsReview = policy !== "unset" || !o["skip-review"];
  const pr = await api.getPullRequest(o.number);
  const head = pr?.head?.sha;
  if (typeof head !== "string") throw new GiteaError("bad_response", "PR response has no head SHA");
  const reasons = [];
  const ci = ciSummary(await api.ciStatuses(head));
  if (ci.state === "pending") reasons.push("ci_running");
  if (ci.state === "failure") reasons.push("ci_failed");
  if (ci.state === "unknown") reasons.push("ci_unknown");
  if (ci.state === "missing") reasons.push("ci_missing");
  let review = null;
  if (readsReview) {
    review = await api.reviewFindings(pr.number);
    // reviewFindings reads the PR again: a push in between would mix two heads' evidence.
    if (review.head_sha !== head) reasons.push("head_moved");
    else if (review.matches_head && review.finding_count > 0) reasons.push("review_findings");
    else if (needsReview && (!review.matches_head || review.finding_count === null))
      reasons.push("review_missing");
  }
  const latest = new Map();
  const reviews = (await api.listReviews(pr.number, { all: true, limit: 50 })).items;
  for (const r of reviews) {
    if (r.dismissed || !r.user?.login || r.state === "COMMENTED" || r.state === "COMMENT") continue;
    const prev = latest.get(r.user.login);
    if (!prev || (reviewTime(r) ?? "") >= (reviewTime(prev) ?? "")) latest.set(r.user.login, r);
  }
  const requesters = [...latest.values()]
    .filter((r) => CHANGES_REQUESTED.has(r.state))
    .map((r) => r.user.login);
  if (requesters.length) reasons.push("changes_requested");
  if (pr.mergeable === false) reasons.push("not_mergeable");
  // A push during any read above leaves evidence for an old head: check the head once more.
  // A failed or head-less read propagates, so the result never claims a head it did not confirm.
  const final = (await api.getPullRequest(o.number))?.head?.sha;
  if (typeof final !== "string")
    throw new GiteaError("bad_response", "PR response has no head SHA");
  if (final !== head && !reasons.includes("head_moved")) reasons.push("head_moved");
  return {
    code: reasons.length ? EXIT.not_ready : EXIT.ok,
    result: {
      number: pr.number,
      head_sha: head,
      ready: !reasons.length,
      reasons,
      review_policy: policy,
      ci,
      review: review && {
        matches_head: review.matches_head,
        finding_count: review.finding_count,
        reason: review.reason,
      },
      changes_requested_by: requesters,
    },
  };
}

// ---- ci-digest ----

/** Error-looking lines plus the tail, cleaned and deduplicated, newest kept within `budget`. */
export function digestLog(text, budget) {
  const lines = text
    .split(/\r?\n/)
    .map((l) => l.replace(ANSI, "").replace(TIMESTAMP, "").trimEnd())
    .filter(Boolean);
  const picked = new Set();
  lines.forEach((l, i) => {
    if (ERROR_LINE.test(l) || i >= lines.length - TAIL_LINES) picked.add(l);
  });
  const kept = [];
  let bytes = 0;
  for (const line of [...picked].reverse()) {
    const size = Buffer.byteLength(line) + 1;
    if (bytes + size > budget) return { text: kept.reverse().join("\n"), truncated: true };
    kept.push(line);
    bytes += size;
  }
  return { text: kept.reverse().join("\n"), truncated: false };
}

const failed = (x) => FAILED_STATES.has(x.conclusion ?? x.status);

async function ciDigest(api, o) {
  if ((o.number === undefined) === (o["run-id"] === undefined))
    throw usage("ci-digest takes --pr N or --run-id ID");
  const budget = seconds(o["max-bytes"], "max-bytes", DIGEST_BYTES, 256, 64 * 1024);
  let runs;
  let sha = null;
  if (o["run-id"] !== undefined) runs = [await api.getRun(o["run-id"])];
  else {
    sha = await headSha(api, o.number);
    runs = (await api.listRuns({ headSha: sha, all: true, limit: 50 })).items;
  }
  const jobs = [];
  for (const run of runs.filter(failed))
    for (const job of (await api.listJobs(run.id, { all: true, limit: 50 })).items.filter(failed))
      jobs.push({ run, job });
  const failedRuns = runs.filter(failed).length;
  const excerpts = [];
  const size = (v) => Buffer.byteLength(JSON.stringify(v));
  // The budget bounds the printed line: the envelope, every excerpt's fields and the
  // escaped text all count.
  let left =
    budget - size({ head_sha: sha, failed_runs: failedRuns, excerpts: [], truncated: true });
  let truncated = false;
  for (const { run, job } of jobs) {
    const excerpt = {
      run_id: run.id,
      job_id: job.id,
      job: typeof job.name === "string" ? job.name.slice(0, 200) : null,
      step: (job.steps ?? []).find(failed)?.name?.slice(0, 200) ?? null,
      text: "",
    };
    const room = left - size(excerpt) - (excerpts.length ? 1 : 0);
    if (room < MIN_EXCERPT_BYTES) {
      truncated = true;
      break;
    }
    const chunks = [];
    try {
      await api.jobLogs(job.id, { maxBytes: LOG_BYTES, sink: (c) => chunks.push(Buffer.from(c)) });
      const log = Buffer.concat(chunks).toString("utf8");
      // JSON escaping can grow the text; shrink the raw budget until the escaped text fits.
      for (let raw = room; raw > 0; raw -= Math.max(16, size(excerpt.text) - 2 - room)) {
        const d = digestLog(log, raw);
        excerpt.text = d.text;
        truncated ||= d.truncated;
        if (size(excerpt.text) - 2 <= room) break;
      }
    } catch (error) {
      if (!(error instanceof GiteaError)) throw error;
      excerpt.note = `log not read: ${error.code}`;
    }
    if (size(excerpt) + (excerpts.length ? 1 : 0) > left) {
      truncated = true;
      break;
    }
    excerpts.push(excerpt);
    left -= size(excerpt) + (excerpts.length > 1 ? 1 : 0);
  }
  return {
    code: EXIT.ok,
    result: { head_sha: sha, failed_runs: failedRuns, excerpts, truncated },
  };
}

// ---- deliver ----

const GIT_STDERR_BYTES = 160;
const GIT_OPERATIONS = [
  ["rebase-merge", "rebase"],
  ["rebase-apply", "rebase"],
  ["MERGE_HEAD", "merge"],
  ["CHERRY_PICK_HEAD", "cherry-pick"],
  ["REVERT_HEAD", "revert"],
];

/** A git failure as a thrown error: stable code, subcommand, exit status, bounded stderr. */
function gitError(args, error) {
  const status = typeof error?.status === "number" ? error.status : null;
  const stderr = String(error?.stderr ?? "")
    .trimEnd()
    .slice(0, GIT_STDERR_BYTES);
  return new GiteaError(
    "git_failed",
    `git ${args[0]} failed (exit ${status ?? "unknown"})${stderr ? `: ${stderr}` : ""}`,
  );
}

function gitStep(git, args, cwd) {
  try {
    return git(args, cwd);
  } catch (error) {
    throw gitError(args, error);
  }
}

/** The merge, rebase, cherry-pick or revert already in progress in this worktree, or null. */
function operationInProgress(git, cwd, only) {
  for (const [name, operation] of GIT_OPERATIONS) {
    if (only && operation !== only) continue;
    const path = gitStep(git, ["rev-parse", "--git-path", name], cwd);
    if (existsSync(resolve(cwd, path))) return operation;
  }
  return null;
}

async function deliver(api, o, ctx) {
  const { git, cwd } = ctx;
  // Before the branch check: a rebase in progress detaches HEAD, and we must not touch it.
  const pending = operationInProgress(git, cwd);
  if (pending)
    throw new GiteaError(
      "git_operation_in_progress",
      `a ${pending} is already in progress in this worktree; finish or abort it, then run deliver again`,
    );
  const branch = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd);
  if (!branch || branch === "HEAD") throw usage("deliver needs a checked-out branch");
  const base =
    o.base === undefined
      ? (await api.getRepository()).default_branch
      : plainText(o.base, "base", 250);
  if (typeof base !== "string" || !base)
    throw new GiteaError("bad_response", "repository has no default branch");
  if (branch === base) throw usage(`deliver refuses to push the base branch ${base}`);
  gitStep(git, ["fetch", "--quiet", "origin", base], cwd);
  let rebased = false;
  let ancestor = true;
  try {
    git(["merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD"], cwd);
  } catch (error) {
    if (error?.status !== 1) throw gitError(["merge-base"], error);
    ancestor = false;
  }
  if (!ancestor) {
    const changed = gitStep(git, ["status", "--porcelain", "--untracked-files=no"], cwd)
      .split("\n")
      .filter(Boolean)
      .map((line) => line.slice(3));
    if (changed.length)
      throw new GiteaError(
        "dirty_worktree",
        `${changed.length} changed path(s) block the rebase; commit or stash them: ${changed.slice(0, 5).join(", ")}`,
      );
    try {
      git(["rebase", "--quiet", "FETCH_HEAD"], cwd);
    } catch (error) {
      // Only a rebase this invocation started (none existed before) is ours to abort.
      if (!operationInProgress(git, cwd, "rebase")) throw gitError(["rebase"], error);
      let conflicts = [];
      let failure = null;
      try {
        conflicts = git(["diff", "--name-only", "--diff-filter=U"], cwd)
          .split("\n")
          .filter(Boolean);
      } catch (diffError) {
        failure = gitError(["diff"], diffError);
      }
      try {
        git(["rebase", "--abort"], cwd);
      } catch (abortError) {
        throw gitError(["rebase --abort"], abortError);
      }
      if (failure) throw failure;
      if (!conflicts.length) throw gitError(["rebase"], error);
      return {
        code: EXIT.not_ready,
        result: { branch, base, reasons: ["rebase_conflict"], conflicts },
      };
    }
    rebased = true;
  }
  const refspec = `HEAD:refs/heads/${branch}`;
  gitStep(
    git,
    ["push", "--quiet", ...(rebased ? ["--force-with-lease"] : []), "origin", refspec],
    cwd,
  );
  const head = gitStep(git, ["rev-parse", "HEAD"], cwd);
  const body = o["body-file"] === undefined ? undefined : readFileSync(o["body-file"], "utf8");
  const existing = await api.findOpenPullRequest(branch, base);
  let pr = existing;
  if (existing) {
    const update = {};
    if (o.title !== undefined) update.title = o.title;
    if (body !== undefined) update.body = body;
    if (Object.keys(update).length) pr = await api.updatePullRequest(existing.number, update);
  } else {
    const title = o.title ?? gitStep(git, ["log", "-1", "--format=%s"], cwd);
    pr = await api.createPullRequest({ title, head: branch, base, body: body ?? "" });
  }
  return {
    code: EXIT.ok,
    result: {
      url: pr.html_url ?? pr.url ?? null,
      number: pr.number,
      head_sha: head,
      branch,
      base,
      created: !existing,
      rebased,
    },
  };
}

// ---- attach ----
// The worker holds no Argus credential. `attach` copies the file into the bridge's spool
// directory, appends a request line, and waits for the receipt the Argus channel (host)
// writes after it stored and verified the bytes (spec 3.10). Keep the file names and the
// record shapes in step with argus-attach-relay.ts.

const ATTACH_PROTOCOL = 1;
const ATTACH_CAPABILITY = "artifact-attach";
const ATTACH_REQUESTS_FILE = "argus-task-attach.jsonl";
const ATTACH_RECEIPTS_FILE = "argus-task-attach-receipts.jsonl";
const ATTACH_STATUS_FILE = "argus-task-attach-status.json";
const ATTACH_SPOOL_DIR = "argus-task-attach-spool";
const ATTACH_ROLES = ["report", "delegate_report", "policy_snapshot", "qa_receipt", "other"];
const ATTACH_DEFAULT_WAIT_S = 20;
const ATTACH_POLL_MS = 500;
const ATTACH_CHUNK = 1024 * 1024;
const ARTIFACT_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/;
const ARTIFACT_ID = /^[A-Za-z0-9_-]{1,64}$/;
const HEX32 = /^[0-9a-f]{32}$/;
const HEX64 = /^[0-9a-f]{64}$/;
/** Error codes that mean "refused by policy or state": exit 12, not worth retrying unchanged. */
const ATTACH_REFUSED = new Set([
  "symlink",
  "not_regular",
  "outside_roots",
  "bridge_file",
  "empty_file",
  "too_large",
  "stale_attempt",
  "wrong_task",
  "no_active_attempt",
  "attempt_ended",
  "attempt_completed",
  "invalid_request",
  "unsupported_protocol",
  "too_many",
]);

class AttachError extends Error {
  constructor(code, message, extra = {}) {
    super(message);
    this.code = code;
    this.extra = extra;
  }
}

/** An Argus artifact name from a file name; keep in step with artifactName() in argus.ts. */
export function artifactName(filename) {
  const base = filename.split(/[\\/]/).pop() ?? "";
  const clean = base
    .normalize("NFKD")
    .replace(/\p{M}+/gu, "")
    .replace(/[^A-Za-z0-9._-]+/g, "-")
    .replace(/^[._-]+/, "");
  if (clean.length <= 200) return clean || "attachment";
  const dot = clean.lastIndexOf(".");
  const ext = dot > 0 && clean.length - dot <= 20 ? clean.slice(dot) : "";
  const stem = ext ? clean.slice(0, dot) : clean;
  return stem.slice(0, 200 - ext.length) + ext;
}

/** The directory the host and this helper share (where argus-delegate writes its relay files). */
export function bridgeDir(env) {
  if (env.ARGUS_TASK_LOG_FILE) return dirname(env.ARGUS_TASK_LOG_FILE);
  return dirname(env.ARGUS_MODEL_POLICY_FILE || "/workspace/agent/argus-model-policy.json");
}

function allowedRoots(env) {
  const roots = (env.ARGUS_TASK_ATTACH_ROOTS || env.ARGUS_DELEGATE_ROOTS || "/workspace")
    .split(":")
    .filter(Boolean);
  const real = [];
  for (const root of roots) {
    try {
      real.push(realpathSync(root));
    } catch {
      // a root that does not exist allows nothing
    }
  }
  return real;
}

const inside = (root, file) => {
  const rel = relative(root, file);
  return rel !== "" && !rel.startsWith("..") && !isAbsolute(rel);
};

function readBridgeStatus(dir) {
  const missing = (why) =>
    new AttachError(
      "prerequisite_missing",
      `capability ${ATTACH_CAPABILITY} is not available: ${why}. The Argus channel must offer it for the active attempt (${join(dir, ATTACH_STATUS_FILE)}); this image or channel may be too old, or no attempt is active. Nothing was stored; do not ask the orchestrator to read worker-local files.`,
      { capability: ATTACH_CAPABILITY },
    );
  const status = readJson(join(dir, ATTACH_STATUS_FILE));
  if (!status) throw missing("the channel's status file is absent or unreadable");
  if (status.capability !== ATTACH_CAPABILITY)
    throw missing("the channel offers another capability");
  if (status.v !== ATTACH_PROTOCOL)
    throw missing(
      `attach protocol ${status.v} is not supported (this helper speaks ${ATTACH_PROTOCOL})`,
    );
  if (
    typeof status.task_id !== "string" ||
    !status.task_id ||
    !Number.isSafeInteger(status.attempt) ||
    !Number.isSafeInteger(status.max_bytes)
  )
    throw missing("the channel's status file is malformed");
  return status;
}

function jsonLines(file) {
  let text;
  try {
    text = readFileSync(file, "utf8");
  } catch {
    return [];
  }
  const out = [];
  for (const line of text.split("\n")) {
    try {
      const rec = JSON.parse(line);
      if (rec && typeof rec === "object" && !Array.isArray(rec)) out.push(rec);
    } catch {
      // a torn or foreign line
    }
  }
  return out;
}

/** Open the file without following a final link, and check it is a plain file under a root. */
function openSource(file, roots, dir, maxBytes) {
  let linkStat;
  try {
    linkStat = lstatSync(file);
  } catch {
    throw new AttachError("not_found", `no such file: ${file}`);
  }
  if (linkStat.isSymbolicLink())
    throw new AttachError("symlink", "the path is a symbolic link; attach the real file");
  let real;
  try {
    real = realpathSync(file);
  } catch {
    throw new AttachError("not_found", `no such file: ${file}`);
  }
  if (!roots.some((root) => inside(root, real)))
    throw new AttachError(
      "outside_roots",
      `the file is outside the allowed roots (${roots.join(", ") || "none"}); copy it under /workspace first`,
    );
  let bridge;
  try {
    bridge = realpathSync(dir);
  } catch {
    bridge = dir;
  }
  if (
    [ATTACH_REQUESTS_FILE, ATTACH_RECEIPTS_FILE, ATTACH_STATUS_FILE].some(
      (n) => real === join(bridge, n),
    ) ||
    inside(join(bridge, ATTACH_SPOOL_DIR), real)
  )
    throw new AttachError("bridge_file", "the attach bridge's own files cannot be attached");
  let fd;
  try {
    fd = openSync(real, fsc.O_RDONLY | fsc.O_NOFOLLOW | fsc.O_NONBLOCK);
  } catch {
    throw new AttachError("not_regular", "the file cannot be opened as a plain file");
  }
  const st = fstatSync(fd);
  if (!st.isFile()) {
    closeSync(fd);
    throw new AttachError("not_regular", "the path is not a regular file");
  }
  if (st.size === 0) {
    closeSync(fd);
    throw new AttachError("empty_file", "the file is empty; nothing to attach");
  }
  if (st.size > maxBytes) {
    closeSync(fd);
    throw new AttachError("too_large", `the file is ${st.size} bytes; the limit is ${maxBytes}`);
  }
  return { fd, st };
}

/** Copy the open file into a new spool file, hashing what was copied; the file must not change. */
function spoolCopy(fd, st, dest) {
  const out = openSync(dest, "wx", 0o644);
  const hash = createHash("sha256");
  const buf = Buffer.alloc(ATTACH_CHUNK);
  let total = 0;
  try {
    for (;;) {
      const n = readSync(fd, buf, 0, buf.length, total);
      if (n === 0) break;
      hash.update(buf.subarray(0, n));
      writeSync(out, buf, 0, n);
      total += n;
      if (total > st.size) break;
    }
  } finally {
    closeSync(out);
  }
  const after = fstatSync(fd);
  if (total !== st.size || after.size !== st.size || after.mtimeMs !== st.mtimeMs) {
    rmSync(dest, { force: true });
    throw new AttachError(
      "file_changed",
      "the file changed while it was being read; retry once it is final",
    );
  }
  return hash.digest("hex");
}

/** The worker-side check of a host receipt for this very request: nothing else is success. */
function checkReceipt(rec, want) {
  const bad = (why) =>
    new AttachError("invalid_receipt", `the channel's receipt is not valid: ${why}`);
  if (rec.v !== ATTACH_PROTOCOL || rec.type !== "receipt") throw bad("wrong record type");
  if (rec.task_id !== want.task_id || rec.attempt !== want.attempt)
    throw bad("it is for another task or attempt");
  if (rec.request_id !== want.request_id) throw bad("it is for another request");
  if (rec.ok === false) {
    const e = rec.error ?? {};
    const code = typeof e.code === "string" && /^[a-z_]{1,40}$/.test(e.code) ? e.code : "refused";
    throw new AttachError(code, String(e.message ?? "the channel refused the file").slice(0, 300), {
      retryable: e.retryable === true,
      request_id: want.request_id,
    });
  }
  if (rec.ok !== true) throw bad("ok is missing");
  if (typeof rec.artifact_id !== "string" || !ARTIFACT_ID.test(rec.artifact_id))
    throw bad("artifact_id is missing");
  if (typeof rec.name !== "string" || !ARTIFACT_NAME.test(rec.name)) throw bad("name is invalid");
  if (rec.bytes !== want.bytes) throw bad("bytes differ from the file");
  if (rec.sha256 !== want.sha256) throw bad("sha256 differs from the file");
  return {
    stored: true,
    task_id: rec.task_id,
    attempt: rec.attempt,
    artifact_id: rec.artifact_id,
    name: rec.name,
    role: ATTACH_ROLES.includes(rec.role) ? rec.role : null,
    bytes: rec.bytes,
    sha256: rec.sha256,
    duplicate: rec.duplicate === true,
    renamed: rec.renamed === true,
    request_id: want.request_id,
  };
}

async function attach(_api, o, ctx) {
  if (o.file === undefined) throw usage("attach requires --file ABSOLUTE_PATH");
  if (!isAbsolute(o.file)) throw usage("--file must be an absolute path");
  if (o.role !== undefined && !ATTACH_ROLES.includes(o.role))
    throw usage(`--role must be one of ${ATTACH_ROLES.join(", ")}`);
  const capS =
    o.max === undefined
      ? Math.min(ATTACH_DEFAULT_WAIT_S, waitCap(undefined, ctx.env))
      : waitCap(o.max, ctx.env);
  const dir = bridgeDir(ctx.env);
  const status = readBridgeStatus(dir);
  const name = artifactName(o.name ?? basename(o.file));
  const role = o.role ?? null;
  const start = ctx.now();
  const { fd, st } = openSource(o.file, allowedRoots(ctx.env), dir, status.max_bytes);
  const spool = join(dir, ATTACH_SPOOL_DIR);
  const temp = join(spool, `.tmp-${randomBytes(8).toString("hex")}`);
  let sha;
  try {
    sha = spoolCopy(fd, st, temp);
  } catch (error) {
    if (error instanceof AttachError) throw error;
    rmSync(temp, { force: true });
    throw new AttachError(
      "prerequisite_missing",
      `capability ${ATTACH_CAPABILITY} is not available: the spool directory ${spool} is not writable`,
      { capability: ATTACH_CAPABILITY },
    );
  } finally {
    closeSync(fd);
  }
  const want = {
    task_id: status.task_id,
    attempt: status.attempt,
    request_id: createHash("sha256")
      .update([status.task_id, status.attempt, name, role ?? "", sha].join("\n"))
      .digest("hex")
      .slice(0, 32),
    bytes: st.size,
    sha256: sha,
  };
  const receiptsFile = join(dir, ATTACH_RECEIPTS_FILE);
  const requestsFile = join(dir, ATTACH_REQUESTS_FILE);
  const receipts = () => jsonLines(receiptsFile).filter((r) => HEX32.test(r.request_id ?? ""));
  const done = receipts().find((r) => r.request_id === want.request_id && r.ok === true);
  if (done) {
    rmSync(temp, { force: true });
    return { code: EXIT.ok, result: checkReceipt(done, want) };
  }
  // The same request already waiting for the host: wait for it, do not queue a second.
  const waiting = jsonLines(requestsFile).find(
    (r) =>
      r.type === "attach" &&
      r.request_id === want.request_id &&
      HEX32.test(r.nonce ?? "") &&
      !receipts().some((x) => x.nonce === r.nonce),
  );
  let nonce;
  if (waiting) {
    rmSync(temp, { force: true });
    nonce = waiting.nonce;
  } else {
    nonce = randomBytes(16).toString("hex");
    renameSync(temp, join(spool, nonce));
    let handle;
    try {
      handle = openSync(requestsFile, fsc.O_WRONLY | fsc.O_APPEND | fsc.O_NOFOLLOW);
    } catch {
      rmSync(join(spool, nonce), { force: true });
      throw new AttachError(
        "prerequisite_missing",
        `capability ${ATTACH_CAPABILITY} is not available: the request file ${requestsFile} cannot be appended to`,
        { capability: ATTACH_CAPABILITY },
      );
    }
    try {
      writeSync(
        handle,
        `${JSON.stringify({
          v: ATTACH_PROTOCOL,
          type: "attach",
          request_id: want.request_id,
          nonce,
          task_id: want.task_id,
          attempt: want.attempt,
          name,
          role,
          bytes: want.bytes,
          sha256: want.sha256,
          at: new Date(ctx.now()).toISOString(),
        })}\n`,
      );
    } finally {
      closeSync(handle);
    }
  }
  for (;;) {
    const mine = receipts().find(
      (r) => r.nonce === nonce || (r.request_id === want.request_id && r.ok === true),
    );
    if (mine) return { code: EXIT.ok, result: checkReceipt(mine, want) };
    const current = readJson(join(dir, ATTACH_STATUS_FILE));
    if (current?.task_id !== want.task_id || current?.attempt !== want.attempt) {
      throw new AttachError(
        "attempt_ended",
        "the attempt ended before the channel stored the file",
        {
          request_id: want.request_id,
        },
      );
    }
    if (ctx.now() - start >= capS * 1000) {
      return {
        code: EXIT.pending,
        result: {
          stored: false,
          pending: true,
          task_id: want.task_id,
          attempt: want.attempt,
          name,
          bytes: want.bytes,
          sha256: want.sha256,
          request_id: want.request_id,
          waited_s: (ctx.now() - start) / 1000,
          note: "no receipt yet: not stored until a receipt is printed; run the same command again",
        },
      };
    }
    await ctx.sleep(ATTACH_POLL_MS);
  }
}

// ---- CLI ----

const ATTACH_HELP = `usage: argus-task attach --file ABSOLUTE_PATH [--name NAME] [--role ROLE] [--max SECONDS]

Store a file as a task artifact of the attempt you are running. Do this BEFORE you reply
COMPLETE: the artifacts are fixed when the attempt ends, and an unattached file is lost.

  --file   absolute path of a regular file under /workspace (no symbolic link; at most the
           size the channel offers, 25 MiB by default; not empty)
  --name   artifact name (default: the file name); sanitized to [A-Za-z0-9._-]
  --role   ${ATTACH_ROLES.join(" | ")} (default: none)
  --max    seconds to wait for the receipt (default ${ATTACH_DEFAULT_WAIT_S}, at most ARGUS_TASK_WAIT_MAX_S)

The channel stores the file, reads it back and writes a receipt. Success is only the JSON line
{"stored":true,"task_id":...,"attempt":...,"artifact_id":...,"name":...,"bytes":...,"sha256":...};
that is your proof. COMPLETE then lists every artifact you attached; do not paste IDs yourself.
The same file, name and role again returns the same receipt (no duplicate). The same name with
other bytes is stored under a hashed name ("renamed":true). send_file is a chat attachment, not a receipt.

Examples:
  argus-task attach --file /workspace/agent/report.md --name report.md --role report
  argus-task attach --file /workspace/project/.argus-delegate/report.md --role delegate_report

Exit codes: 0 stored (receipt printed), 10 no receipt yet (run it again; nothing duplicates),
12 refused (symlink, not a regular file, outside /workspace, empty, too large, stale or ended
attempt), 1 error (no channel support, file changed while read, storage failed, bad receipt;
the error JSON on stderr says retryable), 2 usage.
`;

const COMMANDS = {
  attach: {
    summary:
      "attach --file PATH [--name N] [--role R]: store a report or delegate output as an artifact before COMPLETE; exits 0 only with the channel's receipt",
    options: { file: "string", name: "string", role: "string", max: "int" },
    local: true,
    help: ATTACH_HELP,
    run: attach,
  },
  wait: {
    summary:
      "wait ci|review --pr N: block until CI on the head SHA finishes or a new review/comment lands; exits 10 at the cap, 11 on a steer",
    options: { sha: "string", since: "string", interval: "int", max: "int" },
    positional: "on",
    run: wait,
  },
  "pr-ready": {
    summary:
      "--pr N: ready (0) or not (12) with reasons; never merges. Review per ARGUS_FORGE_REVIEW (required|off, from the Argus claim); --skip-review only when it is unset",
    options: { "skip-review": "bool" },
    run: prReady,
  },
  "ci-digest": {
    summary:
      "--pr N | --run-id ID: failed jobs and steps with error lines, at most --max-bytes (2048)",
    options: { "run-id": "string", "max-bytes": "int" },
    optionalPr: true,
    run: ciDigest,
  },
  deliver: {
    summary:
      "push the branch (clean rebase onto the base first), create or update its PR; exits 12 on a rebase conflict",
    options: { title: "string", "body-file": "string", base: "string" },
    noPr: true,
    run: deliver,
  },
};

function usageText() {
  const lines = Object.entries(COMMANDS).map(([name, c]) => `  ${name.padEnd(10)} ${c.summary}`);
  return `usage: argus-task COMMAND [options]

${lines.join("\n")}

Global options: --repository URL (default ARGUS_REPOSITORY_URL).
There is no completion subcommand: finish by replying COMPLETE: (plain text, then the
JSON object); the Argus channel submits it. Do not run argus-task to complete.
Output: one JSON line on stdout. Exit codes: 0 done/ready/stored, 10 still pending,
11 interrupted by a steer, 12 not ready, rebase conflict or attach refused, 1 error, 2 usage.
Run argus-task attach --help for storing a report or delegate output as an artifact.
Wait cap: --max seconds, clamped to ARGUS_TASK_WAIT_MAX_S (default ${DEFAULT_MAX_S}).
`;
}

function allowlistFrom(env) {
  try {
    const parsed = JSON.parse(env.ARGUS_REPOSITORIES_JSON ?? "{}");
    return Object.values(parsed ?? {}).filter((v) => typeof v === "string");
  } catch {
    throw policy("ARGUS_REPOSITORIES_JSON is not valid JSON");
  }
}

/** CLI entry point; injectable for tests. Returns the exit code. */
export async function main(argv, env = process.env, io = {}) {
  const stdout = io.stdout ?? process.stdout;
  const stderr = io.stderr ?? process.stderr;
  try {
    let [command, ...rest] = argv;
    if (!command || ["help", "--help", "-h"].includes(command)) {
      stdout.write(usageText());
      return EXIT.ok;
    }
    const definition = Object.hasOwn(COMMANDS, command) ? COMMANDS[command] : null;
    if (!definition) throw usage("unknown command; run argus-task --help");
    if (definition.help && ["--help", "-h", "help"].includes(rest[0])) {
      stdout.write(definition.help);
      return EXIT.ok;
    }
    let positional;
    if (definition.positional) [positional, ...rest] = rest;
    const options = parseArgs(
      rest,
      definition.local
        ? definition.options
        : {
            repository: "string",
            ...(definition.noPr ? {} : { number: "string" }),
            ...definition.options,
          },
    );
    if (definition.positional) options[definition.positional] = positional;
    if (
      !definition.local &&
      !definition.noPr &&
      !definition.optionalPr &&
      options.number === undefined
    )
      throw usage(`${command} requires --pr`);
    // A local command talks to the Argus channel's files, not to the forge.
    let api = null;
    if (!definition.local) {
      const repository = options.repository ?? env.ARGUS_REPOSITORY_URL;
      if (!repository)
        throw policy("no repository: pass --repository URL or set ARGUS_REPOSITORY_URL");
      const fetchImpl =
        io.fetch ?? createGatewayFetch({ proxy: env.HTTPS_PROXY ?? env.https_proxy ?? "" });
      api = repositoryClient(repository, allowlistFrom(env), fetchImpl, {
        sleep: io.retrySleep,
      });
    }
    const git = io.git ?? defaultGit;
    const cwd = io.cwd ?? process.cwd();
    const ctx = {
      env,
      git,
      cwd,
      dir: definition.local ? null : signalDir(git, cwd),
      now: io.now ?? Date.now,
      sleep: io.sleep ?? ((ms) => new Promise((done) => setTimeout(done, ms))),
    };
    const { code, result } = await definition.run(api, options, ctx);
    stdout.write(`${JSON.stringify(result)}\n`);
    return code;
  } catch (error) {
    if (error instanceof AttachError) {
      stderr.write(
        `${JSON.stringify({ error: { code: error.code, message: error.message, ...error.extra } })}\n`,
      );
      return ATTACH_REFUSED.has(error.code) ? EXIT.not_ready : EXIT.error;
    }
    stderr.write(`${JSON.stringify({ error: errorPayload(error) })}\n`);
    return error instanceof GiteaError && error.code === "usage" ? EXIT.usage : EXIT.error;
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main(process.argv.slice(2)).then((code) => {
    process.exitCode = code;
  });
}
