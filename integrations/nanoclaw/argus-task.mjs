#!/usr/bin/env node
// Deterministic task helpers for workers (argus-task). Spec: docs/specs/task-helpers.spec.md.
// Built on the forge clients (argus-gitea-api / argus-github-api): no token, no HTTP
// client of its own. Each command prints one JSON line and exits with a code from EXIT.
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
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
const DIGEST_BYTES = 2048;
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

const isPending = (s) => ["pending", "running", "queued", "in_progress", "waiting"].includes(s);
const isFailed = (s) => ["failure", "error", "cancelled", "timed_out"].includes(s);

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
    state: s.status ?? s.state ?? "pending",
  }));
  const failed = checks.filter((c) => isFailed(c.state)).map((c) => c.context);
  const running = checks.filter((c) => isPending(c.state)).length;
  const state = !checks.length
    ? "missing"
    : failed.length
      ? "failure"
      : running
        ? "pending"
        : "success";
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
  if (ci.state === "success" || ci.state === "failure") return { done: true, sha, ...ci };
  const status =
    ci.state === "missing"
      ? "no checks reported yet"
      : `${ci.running} of ${ci.total} checks running`;
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
  try {
    for (;;) {
      const steer = interruptAfter(ctx.dir, start);
      if (steer) {
        clear();
        return { code: EXIT.interrupted, result: { on, ref, interrupted: steer.reason } };
      }
      const check = on === "ci" ? await checkCi(api, o) : await checkReview(api, o, cursor);
      if (check.done) {
        clear();
        return {
          code: EXIT.ok,
          result: { on, ref, waited_s: (ctx.now() - start) / 1000, ...check },
        };
      }
      const nextCheck = ctx.now() + intervalMs;
      for (;;) {
        if (ctx.now() - start >= capMs) {
          // The waiting file stays: the agent is expected to call again.
          const { done: _, ...rest } = check;
          return {
            code: EXIT.pending,
            result: { on, ref, waited_s: (ctx.now() - start) / 1000, cursor, ...rest },
          };
        }
        if (ctx.now() >= nextCheck || interruptAfter(ctx.dir, start)) break;
        await ctx.sleep(MARKER_TICK_MS);
      }
    }
  } catch (error) {
    clear();
    throw error;
  }
}

// ---- pr-ready ----

const CHANGES_REQUESTED = new Set(["REQUEST_CHANGES", "CHANGES_REQUESTED"]);

async function prReady(api, o) {
  const pr = await api.getPullRequest(o.number);
  const head = pr?.head?.sha;
  if (typeof head !== "string") throw new GiteaError("bad_response", "PR response has no head SHA");
  const reasons = [];
  const ci = ciSummary(await api.ciStatuses(head));
  if (ci.state === "pending") reasons.push("ci_running");
  if (ci.state === "failure") reasons.push("ci_failed");
  if (ci.state === "missing") reasons.push("ci_missing");
  let review = null;
  if (!o["skip-review"]) {
    review = await api.reviewFindings(pr.number);
    if (!review.matches_head || review.finding_count === null) reasons.push("review_missing");
    else if (review.finding_count > 0) reasons.push("review_findings");
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
  return {
    code: reasons.length ? EXIT.not_ready : EXIT.ok,
    result: {
      number: pr.number,
      head_sha: head,
      ready: !reasons.length,
      reasons,
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

const failed = (x) => isFailed(x.conclusion ?? x.status);

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
  const excerpts = [];
  let left = budget;
  let truncated = false;
  for (const { run, job } of jobs) {
    const step = (job.steps ?? []).find(failed)?.name ?? null;
    let text = "";
    let note = null;
    if (left > 0) {
      const chunks = [];
      try {
        await api.jobLogs(job.id, {
          maxBytes: LOG_BYTES,
          sink: (c) => chunks.push(Buffer.from(c)),
        });
        const d = digestLog(Buffer.concat(chunks).toString("utf8"), left);
        text = d.text;
        truncated ||= d.truncated;
        left -= Buffer.byteLength(text);
      } catch (error) {
        if (!(error instanceof GiteaError)) throw error;
        note = `log not read: ${error.code}`;
      }
    } else truncated = true;
    excerpts.push({
      run_id: run.id,
      job_id: job.id,
      job: job.name ?? null,
      step,
      text,
      ...(note ? { note } : {}),
    });
  }
  return {
    code: EXIT.ok,
    result: { head_sha: sha, failed_runs: runs.filter(failed).length, excerpts, truncated },
  };
}

// ---- deliver ----

function gitOk(git, args, cwd) {
  try {
    git(args, cwd);
    return true;
  } catch {
    return false;
  }
}

async function deliver(api, o, ctx) {
  const { git, cwd } = ctx;
  const branch = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd);
  if (!branch || branch === "HEAD") throw usage("deliver needs a checked-out branch");
  const base =
    o.base === undefined
      ? (await api.getRepository()).default_branch
      : plainText(o.base, "base", 250);
  if (typeof base !== "string" || !base)
    throw new GiteaError("bad_response", "repository has no default branch");
  if (branch === base) throw usage(`deliver refuses to push the base branch ${base}`);
  git(["fetch", "--quiet", "origin", base], cwd);
  let rebased = false;
  if (!gitOk(git, ["merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD"], cwd)) {
    if (!gitOk(git, ["rebase", "--quiet", "FETCH_HEAD"], cwd)) {
      let conflicts = [];
      try {
        conflicts = git(["diff", "--name-only", "--diff-filter=U"], cwd)
          .split("\n")
          .filter(Boolean);
      } finally {
        gitOk(git, ["rebase", "--abort"], cwd);
      }
      return {
        code: EXIT.not_ready,
        result: { branch, base, reasons: ["rebase_conflict"], conflicts },
      };
    }
    rebased = true;
  }
  const refspec = `HEAD:refs/heads/${branch}`;
  git(["push", "--quiet", ...(rebased ? ["--force-with-lease"] : []), "origin", refspec], cwd);
  const head = git(["rev-parse", "HEAD"], cwd);
  const body = o["body-file"] === undefined ? undefined : readFileSync(o["body-file"], "utf8");
  const existing = await api.findOpenPullRequest(branch, base);
  let pr = existing;
  if (existing) {
    const update = {};
    if (o.title !== undefined) update.title = o.title;
    if (body !== undefined) update.body = body;
    if (Object.keys(update).length) pr = await api.updatePullRequest(existing.number, update);
  } else {
    const title = o.title ?? git(["log", "-1", "--format=%s"], cwd);
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

// ---- CLI ----

const COMMANDS = {
  wait: {
    summary:
      "wait ci|review --pr N: block until CI on the head SHA finishes or a new review/comment lands; exits 10 at the cap, 11 on a steer",
    options: { sha: "string", since: "string", interval: "int", max: "int" },
    positional: "on",
    run: wait,
  },
  "pr-ready": {
    summary: "--pr N: ready (0) or not (12) with reasons; never merges",
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
Output: one JSON line on stdout. Exit codes: 0 done/ready, 10 still pending,
11 interrupted by a steer, 12 not ready or rebase conflict, 1 error, 2 usage.
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
    let positional;
    if (definition.positional) [positional, ...rest] = rest;
    const options = parseArgs(rest, {
      repository: "string",
      ...(definition.noPr ? {} : { number: "string" }),
      ...definition.options,
    });
    if (definition.positional) options[definition.positional] = positional;
    if (!definition.noPr && !definition.optionalPr && options.number === undefined)
      throw usage(`${command} requires --pr`);
    const repository = options.repository ?? env.ARGUS_REPOSITORY_URL;
    if (!repository)
      throw policy("no repository: pass --repository URL or set ARGUS_REPOSITORY_URL");
    const fetchImpl =
      io.fetch ?? createGatewayFetch({ proxy: env.HTTPS_PROXY ?? env.https_proxy ?? "" });
    const api = repositoryClient(repository, allowlistFrom(env), fetchImpl, {
      sleep: io.retrySleep,
    });
    const git = io.git ?? defaultGit;
    const cwd = io.cwd ?? process.cwd();
    const ctx = {
      env,
      git,
      cwd,
      dir: signalDir(git, cwd),
      now: io.now ?? Date.now,
      sleep: io.sleep ?? ((ms) => new Promise((done) => setTimeout(done, ms))),
    };
    const { code, result } = await definition.run(api, options, ctx);
    stdout.write(`${JSON.stringify(result)}\n`);
    return code;
  } catch (error) {
    stderr.write(`${JSON.stringify({ error: errorPayload(error) })}\n`);
    return error instanceof GiteaError && error.code === "usage" ? EXIT.usage : EXIT.error;
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main(process.argv.slice(2)).then((code) => {
    process.exitCode = code;
  });
}
