#!/usr/bin/env node
// Deterministic task helpers for workers (argus-task). Spec: docs/specs/task-helpers.spec.md.
// Built on the forge clients (argus-gitea-api / argus-github-api): no token, no HTTP
// client of its own. Each command prints one JSON line and exits with a code from EXIT.
import { execFileSync, spawnSync } from "node:child_process";
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

const {
  usage,
  policy,
  positiveNumber,
  plainText,
  timestamp,
  commitSha,
  token,
  requireMergeAuthorization,
} = internals;

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
// A merged PR or a finished CI is noticed in seconds, not after one full interval: the first
// re-checks run at 3, 6, 12, 24 s and then settle on the interval, which stays the ceiling.
const FAST_POLL_MS = 3000;
const MAX_DOUBLINGS = 4; // 3, 6, 12, 24, then the interval
const DEFAULT_MAX_S = 90; // well under the shortest engine shell timeout we know of, 120 s (spec 3.8)
const PR_READY_MAX_S = 60; // pr-ready is one read pass; a slow forge ends it here, not at the tool timeout
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
const CONTROL = /[\x00-\x08\x0b-\x1f\x7f-\x9f]/g; // keeps tab and newline
const TIMESTAMP = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\s?/;
const LINK_CHARS = 300;
// Secret-looking values in forge output; each match keeps its label and loses the value.
const SECRETS = [
  [/\b(https?:\/\/)[^\s/@:]+:[^\s/@]+@/gi, "$1[REDACTED]@"],
  [/\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})/g, "[REDACTED]"],
  [/\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/g, "[REDACTED]"],
  [/(\bAuthorization\s*[:=]\s*)[^\n]*/gi, "$1[REDACTED]"],
  [/(\bBearer\s+)[A-Za-z0-9._~+/=-]+/gi, "$1[REDACTED]"],
  [
    /([A-Za-z0-9_]*(?:password|passwd|token|secret|api[_-]?key)\s*[=:]\s*)(["']?)[^\s"'&,;]+/gi,
    "$1$2[REDACTED]",
  ],
];

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
    if (lstatSync(file).isSymbolicLink()) return null;
    return JSON.parse(readFileSync(file, "utf8"));
  } catch {
    return null;
  }
}

/** Atomic write that refuses a symlink at the target or the temp file; throws on any failure. */
function writeJson(file, value) {
  try {
    if (lstatSync(file).isSymbolicLink())
      throw new Error(`refusing to write through a symlink: ${file}`);
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
  const temp = `${file}.${process.pid}.tmp`;
  rmSync(temp, { force: true });
  const fd = openSync(temp, fsc.O_WRONLY | fsc.O_CREAT | fsc.O_EXCL | fsc.O_NOFOLLOW, 0o644);
  try {
    writeSync(fd, `${JSON.stringify(value)}\n`);
  } finally {
    closeSync(fd);
  }
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

function latestPerContext(statuses) {
  const latest = new Map();
  for (const s of Array.isArray(statuses) ? statuses : []) {
    const prev = latest.get(s.context);
    if (!prev || (s.id ?? 0) > (prev.id ?? 0)) latest.set(s.context, s);
  }
  return [...latest.values()];
}

/** Newest status per context, including review jobs that are also CI checks. */
export function ciSummary(statuses) {
  const checks = latestPerContext(statuses).map((s) => ({
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

/** A compact PR head read shared by the waits: both fields must be present or the forge is lying. */
function prHead(pr) {
  if (typeof pr?.head?.sha !== "string")
    throw new GiteaError("bad_response", "PR response has no head SHA");
  return pr.head.sha.toLowerCase();
}

/** Whether a PR can never change again: merged, or closed without a merge. */
function prTerminal(pr) {
  const state = String(pr?.state ?? "").toLowerCase();
  return pr?.merged === true ? "merged" : state === "closed" ? "closed without merge" : null;
}

async function headSha(api, number) {
  return prHead(await api.getPullRequest(number));
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

/**
 * How long to wait before the next check: 3, 6, 12, 24 s and then the configured interval, which
 * is the ceiling. A merge or a finished CI arrives in seconds instead of after a full interval,
 * and a long wait still settles at the cheap rate.
 */
export function pollDelay(attempt, capMs) {
  return Math.min(capMs, FAST_POLL_MS * 2 ** Math.min(attempt, MAX_DOUBLINGS));
}

const SHA = /^[0-9a-f]{7,64}$/i;
const MAX_FAILED = 20; // the host accepts at most this many contexts of 100 printable ASCII
const contextName = (name) =>
  String(name)
    .replace(/[^\x20-\x7e]/g, "?")
    .slice(0, 100) || "?";
const short = (sha) => sha.slice(0, 7);

/**
 * One CI observation of the watched commit. `watch` pins the commit for the whole call: an
 * explicit --sha as given, else the stored one while the PR head still is it, else the PR head
 * at the first observation. The PR head is read afresh on every check; the wait is done only
 * when the checks on the watched commit have finished, or one has failed unless `--all`, and the
 * PR head still is that commit.
 */
async function checkCi(api, o, watch) {
  const pr = await api.getPullRequest(o.number);
  const head = prHead(pr);
  // A merged or closed PR can never produce another check: waiting on it would spin to the cap and
  // leave the worker stale exactly when the owner merged. Report it as a final state instead.
  const terminal = prTerminal(pr);
  if (terminal) {
    if (!watch.bound) {
      watch.bound = true;
      watch.sha ??= head;
    }
    return {
      done: true,
      status: terminal,
      sha: watch.sha,
      watched_sha: watch.sha,
      pr_head_sha: head,
      head_matches: head.startsWith(watch.sha),
      merged: pr.merged === true,
      pr_state: String(pr.state ?? "").toLowerCase(),
      ...(pr.merged_at ? { merged_at: pr.merged_at } : {}),
      ...(pr.merge_commit_sha ? { merge_commit_sha: pr.merge_commit_sha } : {}),
    };
  }
  if (!watch.bound) {
    watch.bound = true;
    if (watch.sha && !watch.explicit && !head.startsWith(watch.sha)) {
      watch.reboundFrom = watch.sha;
      watch.sha = null;
    }
    watch.sha ??= head;
  }
  const sha = watch.sha;
  watch.statuses = await api.ciStatuses(sha);
  const ci = ciSummary(watch.statuses);
  const headMatches = head.startsWith(sha); // an abbreviated --sha matches by prefix
  const evidence = {
    sha,
    watched_sha: sha,
    pr_head_sha: head,
    head_matches: headMatches,
    ...(watch.reboundFrom ? { rebound_from: watch.reboundFrom } : {}),
    ...ci,
  };
  if (!headMatches)
    return {
      done: false,
      status: `PR head ${short(head)} is not the watched ${short(sha)}`,
      ...evidence,
    };
  // Fail fast: the first failed check ends the wait unless --all asks for the whole run (spec 3.3).
  if (ci.failed.length && !o.all) return { done: true, ...evidence };
  if (ci.state !== "missing" && ci.running === 0) return { done: true, ...evidence };
  const failedSoFar = ci.failed.length ? `, ${ci.failed.length} failed` : "";
  const status =
    ci.state === "missing"
      ? "no checks reported yet"
      : `${ci.running} of ${ci.total} checks running${failedSoFar}`;
  return { done: false, status, ...evidence };
}

/**
 * `failureDiagnostics` stopped early enough that one more PR head read fits before `deadline`:
 * one request timeout, or half the time left when less remains.
 */
async function diagnose(api, sha, statuses, deadline, now) {
  const reserve = Math.min(PROBE_REQUEST_MS, (deadline - now()) / 2);
  api.deadline = { at: deadline - reserve, now };
  try {
    return await failureDiagnostics(api, sha, { statuses });
  } finally {
    api.deadline = { at: deadline, now };
  }
}

/**
 * A failed ci result made actionable in the same call: the failed checks and the bounded,
 * sanitized job-log excerpts of the watched commit, then one more PR head read. A push during
 * the fetch makes the failure an old head's: it is no outcome and the wait goes on (spec 3.3).
 * Out of time, the diagnostics are unavailable and the observed failure stands.
 */
async function ciFailure(api, o, check, watch, deadline, now) {
  // The PR head matched the watched (maybe abbreviated) sha: it is the full commit to filter by.
  const diagnostics = await diagnose(api, check.pr_head_sha, watch.statuses, deadline, now);
  const head = await headSha(api, o.number);
  if (!head.startsWith(check.sha)) {
    const { done: _, ...evidence } = check;
    return {
      done: false,
      status: `PR head ${short(head)} is not the watched ${short(check.sha)}`,
      ...evidence,
      pr_head_sha: head,
      head_matches: false,
    };
  }
  const running = check.running
    ? `; ${check.running} checks are still running on this commit and will be superseded by the new push`
    : "";
  return {
    ...check,
    diagnostics,
    action: `diagnostics holds the failed jobs' error lines for ${short(check.sha)}; for more, run argus-task ci-digest --sha ${check.sha} or the forge helper's job-logs; fix, push, then run wait ci again${running}`,
  };
}

const reviewTime = (r) => r.submitted_at ?? r.updated_at ?? r.created_at ?? null;

/** `wait merge`: done the moment the PR is merged; a closed PR is terminal too, without a merge. */
async function checkMerge(api, o) {
  const pr = await api.getPullRequest(o.number);
  const head = prHead(pr);
  const terminal = prTerminal(pr);
  return {
    done: terminal !== null,
    status: terminal ?? "not merged yet",
    sha: head,
    pr_head_sha: head,
    merged: pr.merged === true,
    pr_state: String(pr.state ?? "").toLowerCase(),
    ...(pr.merged_at ? { merged_at: pr.merged_at } : {}),
    ...(pr.merge_commit_sha ? { merge_commit_sha: pr.merge_commit_sha } : {}),
  };
}

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
  if (on !== "ci" && on !== "review" && on !== "merge")
    throw usage("wait takes ci, review or merge");
  if (on === "merge" && o.sha !== undefined) throw usage("wait merge takes no --sha");
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
  // A record with an outcome is a finished wait: only a pending one is continued.
  const same = previous?.on === on && previous?.ref === ref && !previous?.outcome;
  const since = same ? previous.since : new Date(start).toISOString();
  const cursor =
    on === "review" ? (o.since === undefined ? since : timestamp(o.since, "since")) : null;
  // Only a ci wait has a watched commit; a merge wait follows the PR state alone.
  const stored =
    on === "ci" && same && typeof previous.sha === "string" && SHA.test(previous.sha)
      ? previous.sha.toLowerCase()
      : null;
  const watch =
    on === "ci"
      ? {
          sha: o.sha === undefined ? stored : internals.commitSha(o.sha),
          explicit: o.sha !== undefined,
          bound: false,
          reboundFrom: null,
        }
      : null;
  // The waiting file is best effort: a failed write (or a symlink in the way) is skipped and
  // the wait goes on, the result being the same; the milestone is only lost.
  let published = null;
  const publish = () => {
    if (!file) return;
    const sha = watch?.sha ?? null;
    if (published && published.sha === sha) return;
    try {
      mkdirSync(ctx.dir, { recursive: true });
      writeJson(file, { on, ref, since, ...(sha ? { sha } : {}) });
      published = { sha };
    } catch {
      // retried at the next check
    }
  };
  // A finished ci wait leaves a terminal record: the same fields plus the outcome, so the host
  // can supersede the waiting milestone (spec 3.4). Best effort like the pending record.
  const finish = (check) => {
    if (!file || on !== "ci" || !watch?.sha) return clear();
    // A merged or closed PR ends the wait without a CI state: there is no outcome to record, and
    // the terminal record's shape (3.4) must not gain a half-filled one.
    if (typeof check.state !== "string") return clear();
    try {
      mkdirSync(ctx.dir, { recursive: true });
      const failed = check.failed.slice(0, MAX_FAILED).map(contextName);
      writeJson(file, { on, ref, since, sha: watch.sha, outcome: { state: check.state, failed } });
    } catch {
      clear();
    }
  };
  const clear = () => {
    try {
      if (file) rmSync(file, { force: true });
    } catch {
      // best effort
    }
  };
  publish();
  // One deadline bounds the whole wait: every exchange (each page, the head lookup) checks it
  // before it starts and is cut to the time left. No retries inside a wait (the next check is
  // the retry), so there is no backoff to bound either.
  const deadline = start + capMs;
  // The first re-checks are fast (a merge or a finished CI is worth noticing in seconds); the
  // configured interval is the ceiling they settle on, never a floor.
  let attempt = 0;
  api.maxRetries = 0;
  api.timeoutMs = PROBE_REQUEST_MS;
  api.deadline = { at: deadline, now: ctx.now };
  const pending = (check) => {
    // The waiting file stays: the agent is expected to call again.
    const { done: _, ...rest } = check;
    return {
      code: EXIT.pending,
      result: {
        on,
        ref,
        waited_s: (ctx.now() - start) / 1000,
        cursor,
        ...rest,
        next: `still pending, nothing is lost: run the same command again (argus-task wait ${on} --pr ${o.number})`,
      },
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
        check =
          on === "ci"
            ? await checkCi(api, o, watch)
            : on === "merge"
              ? await checkMerge(api, o)
              : await checkReview(api, o, cursor);
        // A check that finished after the budget is late: report pending, never success.
        if (check.done && ctx.now() >= deadline)
          check = { done: false, status: "wait budget spent" };
        else if (check.done && check.state === "failure")
          check = await ciFailure(api, o, check, watch, deadline, ctx.now);
      } catch (error) {
        if (!(error instanceof GiteaError)) throw error;
        if (error.code === "deadline_exceeded") return pending({ status: "wait budget spent" });
        if (!TRANSIENT.has(error.code)) throw error;
        check = { done: false, status: `forge not answering (${error.code}); will check again` };
      }
      publish();
      if (check.done) {
        finish(check);
        return {
          code: EXIT.ok,
          result: { on, ref, waited_s: (ctx.now() - start) / 1000, ...check },
        };
      }
      const nextCheck = ctx.now() + pollDelay(attempt++, intervalMs);
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

/** Logins whose latest non-comment, non-dismissed review requests changes. */
async function changeRequesters(api, number) {
  const latest = new Map();
  const reviews = (await api.listReviews(number, { all: true, limit: 50 })).items;
  for (const r of reviews) {
    if (r.dismissed || !r.user?.login || r.state === "COMMENTED" || r.state === "COMMENT") continue;
    const prev = latest.get(r.user.login);
    if (!prev || (reviewTime(r) ?? "") >= (reviewTime(prev) ?? "")) latest.set(r.user.login, r);
  }
  return [...latest.values()]
    .filter((r) => CHANGES_REQUESTED.has(r.state))
    .map((r) => r.user.login);
}

async function prReady(api, o, ctx) {
  const policy = reviewPolicy(ctx.env);
  // Missing review is a reason only when policy requires one; an existing review is always read.
  const needsReview = policy === "required" || (policy === "unset" && !o["skip-review"]);
  const readsReview = policy !== "unset" || !o["skip-review"];
  // One bounded pass: a slow forge ends the call as pending instead of hitting the tool timeout.
  const start = ctx.now();
  api.timeoutMs = PROBE_REQUEST_MS;
  api.deadline = { at: start + PR_READY_MAX_S * 1000, now: ctx.now };
  try {
    return await prReadyPass(api, o, ctx, { policy, needsReview, readsReview });
  } catch (error) {
    if (error instanceof GiteaError && error.code === "deadline_exceeded")
      return {
        code: EXIT.pending,
        result: {
          number: o.number,
          ready: null,
          next: `the forge did not answer within ${PR_READY_MAX_S} s; run the same command again (argus-task pr-ready --pr ${o.number})`,
        },
      };
    throw error;
  } finally {
    api.deadline = null;
  }
}

async function prReadyPass(api, o, ctx, { policy, needsReview, readsReview }) {
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
  const requesters = await changeRequesters(api, pr.number);
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

// ---- merge ----

const MERGEABLE_STATES = new Set(["clean", "has_hooks"]);
// GitHub's mergeable_state values that no wait can fix: the author or a reviewer must act.
const MERGE_BLOCKED = {
  blocked:
    "branch protection or required reviews block the merge; satisfy them, then run merge again",
  behind:
    "the branch is behind its base; run argus-task deliver to rebase and push, then merge again",
  dirty: "the branch conflicts with its base; run argus-task deliver to rebase, then merge again",
  draft: "the pull request is a draft; mark it ready, then run merge again",
};

/**
 * CI states of the head with contradictory check runs reconciled. A check run that is not
 * `completed` but already carries a conclusion (PR 428: in_progress, success, completed_at) is
 * believed only when its own workflow run, read from the forge, is completed and successful for
 * this exact head. Anything else stays `unknown`, never success.
 */
async function authoritativeCi(api, sha, statuses) {
  const runs = new Map();
  const resolved = [];
  for (const s of Array.isArray(statuses) ? statuses : []) {
    if (!s?.contradictory) {
      resolved.push(s);
      continue;
    }
    let ok = false;
    if (s.run_id) {
      if (!runs.has(s.run_id)) runs.set(s.run_id, await api.getRun(s.run_id));
      const run = runs.get(s.run_id);
      ok =
        run?.status === "completed" &&
        run.conclusion === "success" &&
        typeof run.head_sha === "string" &&
        run.head_sha.toLowerCase() === sha.toLowerCase();
    }
    resolved.push({ ...s, state: ok ? "success" : "unknown", contradictory: !ok });
  }
  return {
    ...ciSummary(resolved),
    contradictory: resolved.filter((s) => s.contradictory).map((s) => contextName(s.context)),
  };
}

/** One look at the PR head: `blockers` end the call, `waiting` keeps it going, none means merge. */
async function mergeGates(api, pr, head, { needsReview }) {
  const blockers = [];
  const waiting = [];
  const statuses = await api.ciStatuses(head);
  const ci = await authoritativeCi(api, head, statuses);
  if (ci.state === "failure") blockers.push("ci_failed");
  else if (ci.state === "skipped") blockers.push("ci_not_executed");
  else if (ci.state === "pending") waiting.push("ci_running");
  else if (ci.state === "missing") waiting.push("ci_missing");
  else if (ci.state === "unknown")
    waiting.push(ci.contradictory.length ? "ci_contradictory" : "ci_unknown");
  const evidence = { ci };
  if (blockers.length) return { blockers, waiting, evidence, statuses };
  const review = await api.reviewFindings(pr.number);
  evidence.review = {
    matches_head: review.matches_head,
    finding_count: review.finding_count,
    reason: review.reason,
  };
  if (review.head_sha !== head) waiting.push("head_moved");
  else if (review.matches_head && review.finding_count > 0) blockers.push("review_findings");
  else if (needsReview && (!review.matches_head || review.finding_count === null))
    waiting.push("review_missing");
  const requesters = await changeRequesters(api, pr.number);
  if (requesters.length) {
    blockers.push("changes_requested");
    evidence.changes_requested_by = requesters;
  }
  if (blockers.length || waiting.length) return { blockers, waiting, evidence };
  // Branch rules last: GitHub reports `blocked` while checks or reviews are still outstanding.
  const state = typeof pr.mergeable_state === "string" ? pr.mergeable_state : null;
  evidence.mergeable_state = state;
  if (state !== null && Object.hasOwn(MERGE_BLOCKED, state)) blockers.push(`merge_${state}`);
  else if (pr.mergeable === false) blockers.push("not_mergeable");
  else if (state !== null ? !MERGEABLE_STATES.has(state) : pr.mergeable !== true)
    waiting.push("mergeability_unknown");
  return { blockers, waiting, evidence };
}

const BLOCKER_ACTION = {
  ci_not_executed: "every check was skipped, which is not execution; make the required checks run",
  review_findings: "resolve the agentic review findings for this head, push, then run merge again",
  changes_requested: "address the standing changes-requested review, push, then run merge again",
  not_mergeable: "the forge reports the PR as not mergeable; fix it, then run merge again",
};
const ciFailedAction = (head) =>
  `diagnostics holds the failed jobs' error lines for ${short(head)}; for more, run argus-task ci-digest --sha ${head}; fix, push with argus-task deliver, then run merge again`;

/**
 * `merge`: wait for the exact head's checks, apply the review policy and the forge's branch rules,
 * merge once at that head and read the forge back. It only acts on the channel's per-attempt
 * merge authorization (argus-merge-auth.ts), checked before any request; `wait merge` stays an
 * observation. A merged PR is reported as it is, so a re-run after a cap, a steer or an ambiguous
 * PUT never merges twice.
 */
async function mergePr(api, o, ctx) {
  const number = positiveNumber(o.number);
  const method = token(o["merge-method"] ?? "merge", "merge-method", /^(merge|squash|rebase)$/);
  const pinned = o.sha === undefined ? null : commitSha(o.sha).toLowerCase();
  requireMergeAuthorization(ctx.env);
  // An unset or unknown forge review policy counts as required: it never drops a gate.
  const needsReview = reviewPolicy(ctx.env) !== "off";
  const intervalMs =
    seconds(o.interval, "interval", DEFAULT_INTERVAL_S, MIN_INTERVAL_S, CEILING_MAX_S) * 1000;
  const start = ctx.now();
  const deadline = start + waitCap(o.max, ctx.env) * 1000;
  const ref = `#${number}`;
  const file = ctx.dir ? join(ctx.dir, "waiting.json") : null;
  const previous = file ? readJson(file) : null;
  const since =
    previous?.on === "merge" && previous?.ref === ref && !previous?.outcome
      ? previous.since
      : new Date(start).toISOString();
  const note = (write) => {
    if (!file) return;
    try {
      if (write) {
        mkdirSync(ctx.dir, { recursive: true });
        writeJson(file, { on: "merge", ref, since });
      } else rmSync(file, { force: true });
    } catch {
      // best effort, like the wait record
    }
  };
  const base = () => ({ on: "merge", ref, waited_s: (ctx.now() - start) / 1000 });
  const done = (pr, extra) => {
    note(false);
    return {
      code: EXIT.ok,
      result: {
        ...base(),
        merged: true,
        pr_head_sha: pr.head?.sha ?? null,
        merge_commit_sha: pr.merge_commit_sha ?? null,
        ...(pr.merged_at ? { merged_at: pr.merged_at } : {}),
        ...extra,
      },
    };
  };
  const stop = (code, result) => {
    note(code === EXIT.pending);
    return { code, result: { ...base(), merged: false, ...result } };
  };
  api.maxRetries = 0;
  api.timeoutMs = PROBE_REQUEST_MS;
  api.deadline = { at: deadline, now: ctx.now };
  note(true);
  let attempt = 0;
  let status = "starting";
  try {
    for (;;) {
      if (interruptAfter(ctx.dir, start)) {
        note(false);
        return {
          code: EXIT.interrupted,
          result: { on: "merge", ref, merged: false, interrupted: "steer" },
        };
      }
      let observation = null;
      let gatedHead = null;
      let merging = false;
      try {
        const pr = await api.getPullRequest(number);
        const head = prHead(pr);
        const terminal = prTerminal(pr);
        if (terminal === "merged") {
          if (!pr.merge_commit_sha)
            throw new GiteaError("bad_response", "merged PR has no merge commit SHA");
          return done(pr, { already_merged: true });
        }
        if (terminal)
          return stop(EXIT.not_ready, {
            blocked: ["pr_closed"],
            pr_head_sha: head,
            action:
              "the PR was closed without a merge; do not recreate it, report it to the orchestrator",
          });
        if (pinned && !head.startsWith(pinned))
          return stop(EXIT.not_ready, {
            blocked: ["head_moved"],
            pr_head_sha: head,
            action: `the PR head is no longer ${short(pinned)}; re-run the gates on the new head and call merge without --sha`,
          });
        gatedHead = head;
        const gates = await mergeGates(api, pr, head, { needsReview });
        let diagnostics;
        if (gates.blockers[0] === "ci_failed") {
          // Same diagnostics and head re-read as a failed wait ci (spec 3.3): a push during the
          // fetch makes it an old head's failure, and the new head is gated instead.
          diagnostics = await diagnose(api, head, gates.statuses, deadline, ctx.now);
          const now = await headSha(api, number);
          if (now !== head) observation = { waiting: ["head_moved"], pr_head_sha: now };
        }
        if (observation) {
          // the head moved under a failed check: re-gate it at the next check
        } else if (gates.blockers.length)
          return stop(EXIT.not_ready, {
            blocked: gates.blockers,
            pr_head_sha: head,
            ...gates.evidence,
            ...(diagnostics ? { diagnostics } : {}),
            action:
              gates.blockers[0] === "ci_failed"
                ? ciFailedAction(head)
                : (BLOCKER_ACTION[gates.blockers[0]] ??
                  MERGE_BLOCKED[gates.evidence.mergeable_state]),
          });
        else if (gates.waiting.length) {
          observation = { waiting: gates.waiting, pr_head_sha: head, ...gates.evidence };
        } else {
          // Every gate holds for `head`; merge only if it still is the head and time is left.
          const confirmed = await api.getPullRequest(number);
          if (confirmed.head?.sha !== head || prTerminal(confirmed)) {
            observation = { waiting: ["head_moved"], pr_head_sha: prHead(confirmed) };
          } else if (interruptAfter(ctx.dir, start) || ctx.now() >= deadline) {
            observation = { waiting: ["budget_spent"], pr_head_sha: head };
          } else {
            api.deadline = { at: ctx.now() + 30_000, now: ctx.now };
            merging = true;
            const readback = await api.mergePullRequest(number, {
              sha: head,
              merge_method: method,
            });
            return done(readback, {
              merged_this_call: true,
              merge_method: method,
              ...gates.evidence,
            });
          }
        }
      } catch (error) {
        if (!(error instanceof GiteaError)) throw error;
        if (error.code === "deadline_exceeded") observation = { waiting: ["budget_spent"] };
        else if (TRANSIENT.has(error.code)) observation = { waiting: [`forge_${error.code}`] };
        else if (merging) {
          // The forge refused the PUT or the PR changed under it: read it before deciding.
          api.deadline = { at: ctx.now() + PROBE_REQUEST_MS, now: ctx.now };
          const now = await api.getPullRequest(number);
          if (now.merged === true && now.merge_commit_sha)
            return done(now, { already_merged: true });
          const moved = prTerminal(now) === null && gatedHead !== null && prHead(now) !== gatedHead;
          if (!moved)
            return stop(EXIT.not_ready, {
              blocked: ["merge_refused"],
              error: errorPayload(error),
              action:
                "the forge refused the merge at this head; read the PR, re-run the gates, then run merge again",
            });
          observation = { waiting: ["head_moved"], pr_head_sha: prHead(now) };
        } else throw error;
      }
      status = observation.waiting.join(", ");
      if (ctx.now() >= deadline)
        return stop(EXIT.pending, {
          ...observation,
          status,
          next: `still pending, nothing is lost: run the same command again (argus-task merge --pr ${number})`,
        });
      api.deadline = { at: deadline, now: ctx.now };
      const nextCheck = ctx.now() + pollDelay(attempt++, intervalMs);
      for (;;) {
        if (ctx.now() >= deadline)
          return stop(EXIT.pending, {
            ...observation,
            status,
            next: `still pending, nothing is lost: run the same command again (argus-task merge --pr ${number})`,
          });
        if (ctx.now() >= nextCheck || interruptAfter(ctx.dir, start)) break;
        await ctx.sleep(MARKER_TICK_MS);
      }
    }
  } catch (error) {
    note(false);
    throw error;
  } finally {
    api.deadline = null;
  }
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

/** Forge text made safe to print: no escape or control sequences, secret-looking values redacted. */
export function sanitize(text) {
  let s = String(text).replace(ANSI, "").replace(CONTROL, "");
  for (const [pattern, replacement] of SECRETS) s = s.replace(pattern, replacement);
  return s;
}

const oneLine = (v) =>
  typeof v === "string" ? sanitize(v.replace(/\s+/g, " ")).slice(0, 200) : null;

/** `url` when it is a short https link on the forge's own host, else undefined. */
function safeLink(api, url) {
  if (typeof url !== "string" || url.length > LINK_CHARS || sanitize(url) !== url) return undefined;
  let u;
  try {
    u = new URL(url);
  } catch {
    return undefined;
  }
  const hosts = new Set();
  for (const base of [api.repository?.origin, api.base])
    try {
      hosts.add(new URL(base).host);
    } catch {}
  return u.protocol === "https:" && !u.username && !u.password && hosts.has(u.host)
    ? url
    : undefined;
}

/**
 * Bounded, sanitized evidence of why CI failed on exactly `sha`: an excerpt of every failed job
 * of the failed runs for that head, and, given `statuses`, the failed checks with a forge link.
 * Runs for any other head are ignored. A forge error never throws: an unreadable log is a note
 * on its excerpt, an unreadable run or job list is `diagnostics_unavailable`.
 */
export async function failureDiagnostics(
  api,
  sha,
  { budget = DIGEST_BYTES, statuses, runId } = {},
) {
  const head = sha ? sha.toLowerCase() : null;
  const out = { head_sha: head, failed_runs: 0, excerpts: [], truncated: false };
  if (statuses)
    out.checks = latestPerContext(statuses)
      .filter((s) => canonicalState(s.status ?? s.state) === "failure")
      .slice(0, MAX_FAILED)
      .map((s) => {
        const link = safeLink(api, s.target_url);
        return {
          context: contextName(sanitize(s.context ?? "ci")),
          state: "failure",
          ...(link ? { link } : {}),
        };
      });
  const unavailable = (error) => {
    if (!(error instanceof GiteaError)) throw error;
    out.diagnostics_unavailable ??= error.code;
  };
  let runs = [];
  try {
    runs =
      runId !== undefined
        ? [await api.getRun(runId)]
        : (await api.listRuns({ headSha: head, all: true, limit: 50 })).items;
  } catch (error) {
    unavailable(error);
  }
  if (head)
    runs = runs.filter((r) => typeof r?.head_sha === "string" && r.head_sha.toLowerCase() === head);
  runs = runs.filter(failed);
  out.failed_runs = runs.length;
  const jobs = [];
  for (const run of runs)
    try {
      for (const job of (await api.listJobs(run.id, { all: true, limit: 50 })).items.filter(failed))
        jobs.push({ run, job });
    } catch (error) {
      unavailable(error);
    }
  const excerpts = out.excerpts;
  const size = (v) => Buffer.byteLength(JSON.stringify(v));
  // The budget bounds the printed value: the envelope, every excerpt's fields and the
  // escaped text all count.
  let left = budget - size({ ...out, truncated: true });
  let truncated = false;
  for (const { run, job } of jobs) {
    const link = safeLink(api, job.html_url) ?? safeLink(api, run.html_url);
    const excerpt = {
      run_id: run.id,
      job_id: job.id,
      job: oneLine(job.name),
      step: oneLine((job.steps ?? []).find(failed)?.name),
      ...(link ? { link } : {}),
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
      const log = sanitize(Buffer.concat(chunks).toString("utf8"));
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
  out.truncated = truncated;
  return out;
}

const FULL_SHA = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/i;

async function ciDigest(api, o) {
  const targets = [o.number, o["run-id"], o.sha].filter((v) => v !== undefined);
  if (targets.length !== 1) throw usage("ci-digest takes one of --pr N, --sha SHA or --run-id ID");
  if (o.sha !== undefined && !FULL_SHA.test(o.sha)) throw usage("--sha must be a full commit SHA");
  const budget = seconds(o["max-bytes"], "max-bytes", DIGEST_BYTES, 256, 64 * 1024);
  const sha = o["run-id"] !== undefined ? null : (o.sha ?? (await headSha(api, o.number)));
  const result = await failureDiagnostics(api, sha, { budget, runId: o["run-id"] });
  return { code: EXIT.ok, result };
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

/** A GiteaError that carries the one command to run next, emitted as `action`. */
function failure(code, message, action) {
  const error = new GiteaError(code, message);
  error.action = action;
  return error;
}

const OPERATION_ABORT = {
  rebase: "git rebase --abort",
  merge: "git merge --abort",
  "cherry-pick": "git cherry-pick --abort",
  revert: "git revert --abort",
};
const COMMIT_ACTION =
  'run `git add -A && git commit -m "<message>"`, then run `argus-task deliver` again';

/** The next step for an error with no specific one, by code; emitted as `action`. */
function defaultAction(error) {
  if (typeof error?.action === "string") return error.action;
  switch (error?.code) {
    case "usage":
      return "fix the arguments (see `argus-task --help`) and run the command again";
    case "policy_denied":
      return "pass --repository URL for the task's repository, or set ARGUS_REPOSITORY_URL, then run the command again";
    case "git_failed":
      return "run `git status` and `git fetch origin`, fix what they report, then run `argus-task deliver` again";
    case "dirty_worktree":
      return COMMIT_ACTION;
    case "network_error":
    case "server_error":
    case "rate_limited":
    case "ambiguous":
      return "run the same command again in a few seconds";
    default:
      return "run the same command again; if it fails again, report the error message in your reply";
  }
}

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

// ---- pre-push checks ----
// Before deliver publishes a branch it runs the checks .githooks/pre-push runs on a developer
// push (`make check-changed`, then the secret scan) on the exact commit it is about to push.
// Worker checkouts do not enable that hook (their core.hooksPath is the Argus commit hook), so
// the gate lives here. Unlike the hook it never skips the scan: a scanner that is not installed
// means a required check cannot run, and deliver fails closed.
const PRE_PUSH_CHECKS = [
  // First and cheap: a stray or misplaced file is named with its fix, not buried in check-changed output.
  {
    name: "path check",
    argv: ["python3", "scripts/check_paths.py", "--json"],
    violations: true,
    // Argus-specific: a repository without the script has no path policy, so the check is
    // reported as not applicable. A script that is present always runs and fails closed.
    onlyIfPresent: "scripts/check_paths.py",
  },
  { name: "make check-changed", argv: ["make", "check-changed"] },
  { name: "gitleaks", argv: ["gitleaks", "version"], probe: true },
  { name: "make secret-scan", argv: ["make", "secret-scan"] },
];
const PRE_PUSH_TIMEOUT_MS = 40 * 60_000;
const PRE_PUSH_OUTPUT_BYTES = 2048;
// A fresh task checkout has no installed dependencies, so a check fails on a missing module or tool.
// The first matching signature names the sync to run; nothing runs it, and the online install is
// the fix (never --offline).
const UV_SYNC = "uv sync --frozen --all-packages --group dev";
const PNPM_INSTALL = "pnpm install --frozen-lockfile";
const SYNC_SIGNATURES = [
  { pattern: /No module named 'prometheus_client'/, commands: [UV_SYNC] },
  { pattern: /ModuleNotFoundError: No module named 'argus_\w+'/, commands: [UV_SYNC] },
  { pattern: /\bbiome: not found/, commands: [PNPM_INSTALL] },
  { pattern: /\btsc: not found/, commands: [PNPM_INSTALL] },
  { pattern: /\bopenapi-typescript: not found/, commands: [PNPM_INSTALL] },
  { pattern: /ERR_PNPM_NO_OFFLINE_TARBALL/, commands: [PNPM_INSTALL] },
  { pattern: /Network connectivity is disabled/, commands: [UV_SYNC, PNPM_INSTALL] },
];

/** The action that names the sync for a failed check's output, or undefined when no signature matches. */
function syncAction(output) {
  const signature = SYNC_SIGNATURES.find((s) => s.pattern.test(output));
  if (!signature) return undefined;
  const commands = signature.commands.map((c) => `\`${c}\``).join(" and ");
  return `run ${commands} from the workspace root, then run \`argus-task deliver\` again; do not use --offline and do not skip the gate`;
}

/** Run one check in the worktree. A missing program is `missing`; a timeout or signal is a failure. */
function defaultRunCheck(argv, cwd, env) {
  // The hook unsets the git variables a hook inherits; a check's own git must act on this worktree.
  const childEnv = Object.fromEntries(
    Object.entries(env).filter(([key]) => !key.startsWith("GIT_")),
  );
  const r = spawnSync(argv[0], argv.slice(1), {
    cwd,
    env: childEnv,
    encoding: "utf8",
    timeout: PRE_PUSH_TIMEOUT_MS,
    maxBuffer: 64 * 1024 * 1024,
  });
  if (r.error?.code === "ENOENT") return { missing: true, status: null, output: "" };
  return { missing: false, status: r.status, output: `${r.stdout ?? ""}${r.stderr ?? ""}` };
}

/** True when anything, even a dangling symlink, is at the path: only a clear absence is "not applicable". */
function pathExists(path) {
  try {
    lstatSync(path);
    return true;
  } catch (e) {
    return e?.code !== "ENOENT" && e?.code !== "ENOTDIR";
  }
}

/** The `[{path, rule, action}]` list `check_paths.py --json` prints, or null when the output is not that. */
function parseViolations(output) {
  try {
    const list = JSON.parse(output.slice(output.indexOf("[")));
    return Array.isArray(list) ? list : null;
  } catch {
    return null;
  }
}

/**
 * Run the pre-push checks on HEAD, the commit deliver is about to push. Returns null when every
 * check passed, or the failed check. A check that does not apply to the repository is pushed onto
 * `notApplicable` and skipped; every other check still runs. Throws when the tree is not clean at HEAD, a check cannot run,
 * or HEAD or the tree changed while the checks ran (the checks would not describe the pushed commit).
 */
function prePushChecks(git, cwd, ctx, notApplicable = []) {
  const clean = () => gitStep(git, ["status", "--porcelain"], cwd) === "";
  const head = gitStep(git, ["rev-parse", "HEAD"], cwd);
  if (!clean())
    throw failure(
      "dirty_worktree",
      `the worktree has changes or untracked files; the pre-push checks need the exact commit; ${COMMIT_ACTION}`,
      COMMIT_ACTION,
    );
  for (const check of PRE_PUSH_CHECKS) {
    if (check.onlyIfPresent && !pathExists(resolve(cwd, check.onlyIfPresent))) {
      notApplicable.push({ check: check.name, reason: `${check.onlyIfPresent} not found` });
      continue;
    }
    const r = ctx.runCheck(check.argv, cwd, ctx.env);
    if (r.missing)
      throw failure(
        "pre_push_unavailable",
        `${check.argv[0]} is not installed, so ${check.name} cannot run; nothing was pushed`,
        `${check.argv[0]} is missing from this image: do not push by other means; report it in your reply as a blocker`,
      );
    if (check.probe) continue;
    if (r.status !== 0) {
      const failed = {
        check: check.name,
        exit: r.status,
        output_tail: r.output.slice(-PRE_PUSH_OUTPUT_BYTES),
      };
      const violations = check.violations ? parseViolations(r.output) : null;
      if (violations?.length)
        return { ...failed, violations, action: violations.map((v) => v.action).join("; ") };
      const sync = syncAction(r.output);
      return sync ? { ...failed, action: sync } : failed;
    }
  }
  if (gitStep(git, ["rev-parse", "HEAD"], cwd) !== head || !clean())
    throw failure(
      "dirty_worktree",
      "HEAD or the worktree changed during the pre-push checks; nothing was pushed, run `argus-task deliver` again",
      "run `argus-task deliver` again without editing files while it runs",
    );
  return null;
}

async function deliver(api, o, ctx) {
  const { git, cwd } = ctx;
  // Before the branch check: a rebase in progress detaches HEAD, and we must not touch it.
  const pending = operationInProgress(git, cwd);
  if (pending)
    throw failure(
      "git_operation_in_progress",
      `a ${pending} is already in progress in this worktree; run \`${OPERATION_ABORT[pending]}\`, then run \`argus-task deliver\` again`,
      `run \`${OPERATION_ABORT[pending]}\`, then run \`argus-task deliver\` again`,
    );
  let branch = "";
  try {
    branch = git(["symbolic-ref", "-q", "--short", "HEAD"], cwd);
  } catch {
    // detached HEAD: symbolic-ref exits 1
  }
  if (!branch) {
    const action =
      "run `git switch -c <branch-name>` to keep the current commit on a branch, then run `argus-task deliver` again";
    throw failure(
      "detached_head",
      `deliver needs a checked-out branch but HEAD is detached; ${action}`,
      action,
    );
  }
  try {
    git(["rev-parse", "--verify", "-q", "HEAD"], cwd);
  } catch {
    throw failure(
      "unborn_branch",
      `branch ${branch} has no commits yet; ${COMMIT_ACTION}`,
      COMMIT_ACTION,
    );
  }
  if (gitStep(git, ["status", "--porcelain", "--untracked-files=no"], cwd) !== "")
    throw failure(
      "dirty_worktree",
      `the worktree has uncommitted or untracked changes; ${COMMIT_ACTION}`,
      COMMIT_ACTION,
    );
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
      throw failure(
        "dirty_worktree",
        `${changed.length} changed path(s) block the rebase: ${changed.slice(0, 5).join(", ")}; ${COMMIT_ACTION}`,
        COMMIT_ACTION,
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
        result: {
          branch,
          base,
          reasons: ["rebase_conflict"],
          conflicts,
          action: `run \`git fetch origin && git rebase origin/${base}\`, resolve the conflicts in the listed files, \`git add\` them, run \`git rebase --continue\`, then run \`argus-task deliver\` again`,
        },
      };
    }
    rebased = true;
  }
  // After any rebase, so the checks run on the commit that is pushed.
  const notApplicable = [];
  const failed = prePushChecks(git, cwd, ctx, notApplicable);
  if (failed)
    return {
      code: EXIT.not_ready,
      result: {
        branch,
        base,
        reasons: ["pre_push_failed"],
        action: "fix the failing check, commit the fix, then run deliver again",
        ...(notApplicable.length ? { checks_not_applicable: notApplicable } : {}),
        ...failed,
      },
    };
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
      ...(notApplicable.length ? { checks_not_applicable: notApplicable } : {}),
      sync: rebased
        ? `rebased the branch onto origin/${base}`
        : `branch already contains origin/${base}; no rebase needed`,
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
      "wait ci|review|merge --pr N: block until CI on the head SHA finishes or its first check fails (--all: wait for every check), a new review/comment lands, or the PR is merged or closed; exits 10 at the cap, 11 on a steer. First checks are 3 s apart, settling on --interval (default 30 s)",
    options: { sha: "string", since: "string", interval: "int", max: "int", all: "bool" },
    positional: "on",
    run: wait,
  },
  merge: {
    summary:
      "merge --pr N [--sha S] [--merge-method M] [--max S]: only with the attempt's merge authorization; waits for the head's checks, review policy and branch rules, merges once at that head and reads the forge back; 0 merged, 12 blocked (the result names why), 10 pending (run it again), 11 steer",
    options: { sha: "string", "merge-method": "string", interval: "int", max: "int" },
    run: mergePr,
  },
  "pr-ready": {
    summary:
      "--pr N: ready (0) or not (12) with reasons; never merges; returns within 60 s (10 = run it again). Review per ARGUS_FORGE_REVIEW (required|off, from the Argus claim); --skip-review only when it is unset",
    options: { "skip-review": "bool" },
    run: prReady,
  },
  "ci-digest": {
    summary:
      "--pr N | --sha SHA | --run-id ID: failed jobs and steps with error lines, at most --max-bytes (2048)",
    options: { sha: "string", "run-id": "string", "max-bytes": "int" },
    optionalPr: true,
    run: ciDigest,
  },
  deliver: {
    summary:
      "the one delivery step after you commit: fetch the base, rebase onto it when behind, run the pre-push checks, push, create or update the PR; exits 12 on a rebase conflict or failed check. Every error carries an `action`",
    options: { title: "string", "body-file": "string", base: "string" },
    noPr: true,
    run: deliver,
  },
};

function usageText() {
  const lines = Object.entries(COMMANDS).map(([name, c]) => `  ${name.padEnd(10)} ${c.summary}`);
  return `usage: argus-task COMMAND [options]

${lines.join("\n")}

Global options: --repository URL (default ARGUS_REPOSITORY_URL, else the task's only allowed repository).
Delivery path, no raw git push: commit, then \`argus-task deliver\`, then, when the task carries a merge grant (pr_merged
with merge=yes), \`argus-task merge --pr N\` (exit 10: run it again); otherwise \`argus-task wait merge --pr N\`, which only observes
and never merges.
\`wait\` returns after at most the cap with exit 10: that is not a failure, run the same command again.
There is no completion subcommand: finish by sending COMPLETE: with the send_message tool to
the argus destination (plain text, then the JSON object). On a task run the runner delivers only
explicit tool sends, so a COMPLETE written as reply text stays in the run log. Do not run
argus-task to complete, and do not wrap the reply in <message> blocks: a task run drops them.
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
      // The task's own repository: the flag, ARGUS_REPOSITORY_URL, else the only one the task
      // allowlist names. Never a guess from a git remote.
      const allowed = allowlistFrom(env);
      const repository =
        options.repository ??
        (env.ARGUS_REPOSITORY_URL || undefined) ??
        (allowed.length === 1 ? allowed[0] : undefined);
      if (!repository)
        throw policy("no repository: pass --repository URL or set ARGUS_REPOSITORY_URL");
      const fetchImpl =
        io.fetch ?? createGatewayFetch({ proxy: env.HTTPS_PROXY ?? env.https_proxy ?? "" });
      api = repositoryClient(repository, allowed, fetchImpl, {
        sleep: io.retrySleep,
      });
    }
    const git = io.git ?? defaultGit;
    const cwd = io.cwd ?? process.cwd();
    const ctx = {
      env,
      git,
      cwd,
      runCheck: defaultRunCheck,
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
    stderr.write(
      `${JSON.stringify({ error: { ...errorPayload(error), action: defaultAction(error) } })}\n`,
    );
    return error instanceof GiteaError && error.code === "usage" ? EXIT.usage : EXIT.error;
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main(process.argv.slice(2)).then((code) => {
    process.exitCode = code;
  });
}
