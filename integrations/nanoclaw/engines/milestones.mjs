// Progress milestones for a delegated run (docs/specs/progress-milestones.spec.md).
//
// Pure and injectable: the tracker takes probes (git, executor output, CI) and a
// sink, so tests need no git, network, or clock. Nothing here reads a credential.

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

/** The closed set; keep in step with argus_core.tasks.ProgressKind. */
export const KINDS = [
  "commit_created",
  "pr_opened",
  "ci_running",
  "ci_passed",
  "ci_failed",
  "gate_failed",
  "waiting_input",
];

const SHA = /^[0-9a-f]{7,40}$/;

/** An http(s) URL with no userinfo, query or fragment, or null. */
export function safeUrl(raw) {
  try {
    const u = new URL(raw);
    if (!["http:", "https:"].includes(u.protocol) || !u.hostname) return null;
    if (u.username || u.password || u.search || u.hash) return null;
    return u.href.length <= 500 ? u.href : null;
  } catch {
    return null;
  }
}

/** Pull requests named in text: any http(s) URL whose path ends in /pulls/N (or /pull/N). */
export function pullRequestsIn(text) {
  const found = [];
  for (const m of String(text).matchAll(/https?:\/\/[^\s"'<>)\]\\]+/g)) {
    // Drop any query/fragment before checking, so a tokenised link still yields the bare URL.
    const bare = m[0].replace(/[?#].*$/, "").replace(/[.,;:]+$/, "");
    const hit = bare.match(/\/pulls?\/(\d+)$/);
    const url = hit && safeUrl(bare);
    if (url) found.push({ number: Number(hit[1]), url });
  }
  return found;
}

/** Validate a milestone against the API's shapes; returns it or null. */
export function milestone(kind, data = {}) {
  if (!KINDS.includes(kind)) return null;
  const ok = {
    commit_created: () => typeof data.branch === "string" && data.branch && SHA.test(data.sha),
    pr_opened: () => Number.isSafeInteger(data.number) && data.number >= 1 && safeUrl(data.url),
    ci_running: () => SHA.test(data.sha),
    ci_passed: () => SHA.test(data.sha),
    ci_failed: () => SHA.test(data.sha),
    gate_failed: () => typeof data.gate === "string" && data.gate && data.gate.length <= 200,
    waiting_input: () => Object.keys(data).length === 0,
  }[kind]();
  return ok ? { kind, data } : null;
}

const short = (sha) => sha.slice(0, 7);

/** The readable `message` that goes with a milestone (older clients show only this). */
export function describe({ kind, data }) {
  switch (kind) {
    case "commit_created":
      return `Commit ${short(data.sha)} created on ${data.branch}`;
    case "pr_opened":
      return `Pull request #${data.number} opened`;
    case "ci_running":
      return `CI running for ${short(data.sha)}`;
    case "ci_passed":
      return `CI passed for ${short(data.sha)}`;
    case "ci_failed":
      return `CI failed for ${short(data.sha)}`;
    case "gate_failed":
      return `Gate failed: ${data.gate}`;
    default:
      return "Waiting for input";
  }
}

/** Reduce Gitea commit statuses (newest per context wins) to one CI state. */
export function ciState(statuses) {
  if (!Array.isArray(statuses) || statuses.length === 0) return null;
  const latest = new Map();
  for (const s of statuses) {
    const prev = latest.get(s.context);
    if (!prev || (s.id ?? 0) > (prev.id ?? 0)) latest.set(s.context, s);
  }
  const all = [...latest.values()];
  const failing = all
    .filter((s) => ["failure", "error"].includes(s.status ?? s.state))
    .map((s) => String(s.context ?? "ci").slice(0, 200));
  if (failing.length) return { state: "failure", failing };
  if (all.every((s) => (s.status ?? s.state) === "success"))
    return { state: "success", failing: [] };
  return { state: "pending", failing: [] };
}

/**
 * Detects milestones from probes and hands each new one to `emit(milestone)`.
 *   probes.head()    -> { branch, sha } | null
 *   probes.output()  -> executor output so far (string)
 *   probes.ci(sha)   -> statuses array | null        (optional)
 * `repository` (optional URL) limits pull requests to that repository.
 * A probe that throws is skipped for that poll. Each distinct milestone is emitted once.
 */
export function createTracker({ probes, emit, repository }) {
  const seen = new Set();
  let baseline = null;
  const once = (kind, data) => {
    const m = milestone(kind, data);
    if (!m) return;
    const key = JSON.stringify(m);
    if (seen.has(key)) return;
    seen.add(key);
    emit(m);
  };
  const safely = async (fn, fallback) => {
    try {
      return (await fn()) ?? fallback;
    } catch {
      return fallback;
    }
  };
  return {
    /** Record HEAD before the executor starts: only later commits are milestones. */
    async start() {
      baseline = (await safely(() => probes.head(), null))?.sha ?? null;
    },
    once,
    async poll() {
      const head = await safely(() => probes.head(), null);
      if (head && head.sha !== baseline)
        once("commit_created", { branch: head.branch, sha: head.sha });
      const prefix = repository ? `${repository.replace(/(\.git)?\/*$/, "")}/pull` : null;
      for (const pr of pullRequestsIn(await safely(() => probes.output(), "")))
        if (!prefix || pr.url.startsWith(prefix)) once("pr_opened", pr);
      if (head && head.sha !== baseline && probes.ci) {
        const ci = ciState(await safely(() => probes.ci(head.sha), null));
        if (ci?.state === "pending") once("ci_running", { sha: head.sha });
        if (ci?.state === "success") once("ci_passed", { sha: head.sha });
        if (ci?.state === "failure") {
          once("ci_failed", { sha: head.sha });
          for (const gate of ci.failing) once("gate_failed", { gate });
        }
      }
    },
  };
}

/** Did the executor's final JSON report it was refused something (Claude `permission_denials`)? */
export function waitingInput(stdoutText) {
  try {
    const last = String(stdoutText).trim().split("\n").filter(Boolean).at(-1);
    const doc = JSON.parse(last);
    return Array.isArray(doc?.permission_denials) && doc.permission_denials.length > 0;
  } catch {
    return false;
  }
}

/** `git` probe for a working tree; `run` is injectable for tests. */
export function gitHead(cwd, run = spawnSync) {
  const git = (...args) => {
    const r = run("git", ["-C", cwd, ...args], { encoding: "utf8", timeout: 10000 });
    return r.status === 0 ? String(r.stdout).trim() : null;
  };
  const sha = git("rev-parse", "HEAD");
  if (!sha || !SHA.test(sha)) return null;
  return { sha, branch: git("rev-parse", "--abbrev-ref", "HEAD") || "HEAD" };
}

/**
 * Where milestones go: stderr, `<out>/milestones.jsonl`, and the task's progress
 * endpoint when ARGUS_API_URL, ARGUS_TASK_ID, ARGUS_TASK_ATTEMPT and
 * ARGUS_AGENT_TOKEN are all set (the agent's own bearer token, as the NanoClaw
 * channel uses it). The token is only ever put in the Authorization header.
 * A sink that fails never fails the delegation.
 */
export function createSink({ e, out, stderr = process.stderr, fetchImpl = globalThis.fetch }) {
  const base = (e.ARGUS_API_URL ?? "").trim().replace(/\/+$/, "");
  const attempt = Number(e.ARGUS_TASK_ATTEMPT);
  const taskId = (e.ARGUS_TASK_ID ?? "").trim();
  const token = (e.ARGUS_AGENT_TOKEN ?? "").trim();
  const apiUrl = (() => {
    try {
      const u = new URL(`${base}/api/tasks/${encodeURIComponent(taskId)}/progress`);
      return base && taskId && token && ["http:", "https:"].includes(u.protocol) && !u.username
        ? u.href
        : null;
    } catch {
      return null;
    }
  })();
  const post = Number.isSafeInteger(attempt) && attempt >= 1 ? apiUrl : null;
  return async (m) => {
    const line = JSON.stringify({ ...m, message: describe(m), at: new Date().toISOString() });
    try {
      stderr.write(`argus-milestone ${line}\n`);
    } catch {
      /* ignore */
    }
    try {
      fs.mkdirSync(out, { recursive: true });
      fs.appendFileSync(path.join(out, "milestones.jsonl"), `${line}\n`);
    } catch {
      /* ignore */
    }
    if (!post) return;
    try {
      await fetchImpl(post, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ attempt, message: describe(m), kind: m.kind, data: m.data }),
        signal: AbortSignal.timeout(10000),
      });
    } catch {
      /* best effort: a 429 or an unreachable API must not stop the run */
    }
  };
}
