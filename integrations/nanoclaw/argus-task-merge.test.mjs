// `argus-task merge` through the real helper, the GitHub client and its authorized merge path,
// against a stateful mocked GitHub. Spec: docs/specs/task-helpers.spec.md 3.9.
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, it } from "node:test";
import { EXIT, main } from "./argus-task.mjs";

const REPO = "https://github.com/acme/widget";
const API = "/repos/acme/widget";
const HEAD = "a".repeat(40);
const MOVED = "b".repeat(40);
const MERGE_SHA = "c".repeat(40);
const T0 = Date.parse("2026-11-18T17:00:00Z");
const KEY = "k".repeat(64);
const grant = (task = "t1", attempt = "1") => ({
  ARGUS_MERGE_AUTH_KEY: KEY,
  ARGUS_MERGE_AUTH_TASK: task,
  ARGUS_MERGE_AUTH_ATTEMPT: attempt,
  ARGUS_MERGE_AUTHORIZED: createHmac("sha256", KEY)
    .update(`merge:${task}:${attempt}`)
    .digest("hex"),
});
const ENV = {
  ARGUS_REPOSITORY_URL: REPO,
  ARGUS_REPOSITORIES_JSON: JSON.stringify({ p: REPO }),
  ARGUS_FORGE_REVIEW: "off",
  ...grant(),
};

const scratch = mkdtempSync(join(tmpdir(), "argus-task-merge-"));
after(() => rmSync(scratch, { recursive: true, force: true }));
let dirs = 0;
const cwd = () => {
  const dir = join(scratch, `r${dirs++}`);
  mkdirSync(join(dir, ".git"), { recursive: true });
  return dir;
};

const checkRun = (over = {}) => ({
  id: 11,
  name: "test",
  status: "completed",
  conclusion: "success",
  details_url: `https://github.com/acme/widget/actions/runs/900/job/11`,
  ...over,
});

/** The forge: mutable `s`, every non-GET recorded in `writes`. */
function forge(over = {}) {
  const s = {
    pr: {
      number: 7,
      state: "open",
      merged: false,
      mergeable: true,
      mergeable_state: "clean",
      head: { sha: HEAD },
    },
    runs: [checkRun()],
    movedRuns: [],
    // Workflow runs, their jobs and job logs for the failure diagnostics.
    actionRuns: [],
    jobs: {},
    logs: {},
    statuses: [],
    comments: [],
    reviews: [],
    workflow: { id: 900, status: "completed", conclusion: "success", head_sha: HEAD },
    putStatus: 200,
    ...over,
  };
  const writes = [];
  const calls = [];
  const fetch = async (target, init = {}) => {
    const url = new URL(target);
    const method = init.method ?? "GET";
    const path = url.pathname.slice(API.length);
    calls.push(`${method} ${path}`);
    const json = (v, status = 200) => Response.json(v, { status });
    if (method !== "GET") {
      writes.push(`${method} ${path}`);
      if (method === "PUT" && path === "/pulls/7/merge") {
        if (s.putStatus !== 200)
          return json({ message: "Pull Request is not mergeable" }, s.putStatus);
        s.pr = {
          ...s.pr,
          state: "closed",
          merged: true,
          merge_commit_sha: MERGE_SHA,
          merged_at: "2026-11-18T17:30:00Z",
        };
        s.mergeBody = JSON.parse(init.body);
        return json({ merged: true, sha: MERGE_SHA });
      }
      return json({}, 404);
    }
    const route = {
      "/pulls/7": () => (typeof s.pr === "function" ? s.pr() : s.pr),
      [`/commits/${s.pr.head?.sha}/check-runs`]: () => ({
        total_count: s.runs.length,
        check_runs: s.runs,
      }),
      [`/commits/${HEAD}/check-runs`]: () => ({ total_count: s.runs.length, check_runs: s.runs }),
      [`/commits/${MOVED}/check-runs`]: () => ({
        total_count: s.movedRuns.length,
        check_runs: s.movedRuns,
      }),
      [`/commits/${HEAD}/status`]: () => ({ statuses: s.statuses }),
      [`/commits/${MOVED}/status`]: () => ({ statuses: [] }),
      "/issues/7/comments": () => s.comments,
      "/pulls/7/reviews": () => s.reviews,
      "/actions/runs/900": () => s.workflow,
      "/actions/runs": () => {
        const sha = url.searchParams.get("head_sha");
        const runs = s.actionRuns.filter((r) => r.head_sha === sha);
        return { total_count: runs.length, workflow_runs: runs };
      },
    }[path];
    const jobs = path.match(/^\/actions\/runs\/(\d+)\/jobs$/)?.[1];
    if (jobs && s.jobs[jobs]) return json({ total_count: s.jobs[jobs].length, jobs: s.jobs[jobs] });
    const log = path.match(/^\/actions\/jobs\/(\d+)\/logs$/)?.[1];
    if (log && s.logs[log] !== undefined) return new Response(s.logs[log]);
    return route ? json(route()) : json({ message: "Not Found" }, 404);
  };
  return { s, writes, calls, fetch };
}

async function merge(f, argv = [], { env = {}, onSleep } = {}) {
  let t = T0;
  const out = [];
  const err = [];
  const dir = cwd();
  const code = await main(
    ["merge", "--pr", "7", ...argv],
    { ...ENV, ...env },
    {
      fetch: f.fetch,
      cwd: dir,
      git: (a) => (a[0] === "rev-parse" ? join(dir, ".git") : ""),
      now: () => t,
      sleep: async (ms) => {
        t += ms;
        onSleep?.((t - T0) / 1000, dir);
      },
      retrySleep: async () => {},
      stdout: { write: (x) => out.push(x) },
      stderr: { write: (x) => err.push(x) },
    },
  );
  return { code, out: out.length ? JSON.parse(out.join("")) : null, err: err.join(""), dir };
}

/** A failed workflow run `runId` of `sha` with one failed job `jobId` and its log. */
const failedRun = (sha, runId, jobId, log) => ({
  actionRuns: [{ id: runId, head_sha: sha, conclusion: "failure" }],
  jobs: { [runId]: [{ id: jobId, name: "test", conclusion: "failure" }] },
  logs: { [jobId]: log },
});

const agentic = (n, sha = HEAD) => [
  {
    id: 3,
    body: `Agentic review for head ${sha}: ${n} findings`,
    created_at: "2026-11-18T16:00:00Z",
  },
];

describe("merge: agentic-review CI with review policy off", () => {
  for (const [name, review, code, reason] of [
    ["pending", { status: "in_progress", conclusion: null }, EXIT.pending, "ci_running"],
    ["failure", { conclusion: "failure" }, EXIT.not_ready, "ci_failed"],
    ["unknown", { conclusion: "unrecognized" }, EXIT.pending, "ci_unknown"],
    ["missing conclusion", { conclusion: null }, EXIT.pending, "ci_unknown"],
  ]) {
    it(`${name} blocks a green neighboring check`, async () => {
      const f = forge({
        runs: [checkRun(), checkRun({ id: 12, name: "agentic-review", ...review })],
      });
      const r = await merge(f, ["--max", "20"]);
      assert.equal(r.code, code, r.err);
      assert.ok((r.out.waiting ?? r.out.blocked).includes(reason), JSON.stringify(r.out));
      assert.deepEqual(f.writes, []);
    });
  }
  for (const conclusion of ["success", "skipped"]) {
    it(`${conclusion} permits merge alongside an executed successful check`, async () => {
      const f = forge({
        runs: [checkRun(), checkRun({ id: 12, name: "agentic-review", conclusion })],
      });
      const r = await merge(f);
      assert.equal(r.code, EXIT.ok, r.err);
      assert.equal(f.s.mergeBody.sha, HEAD);
    });
  }
  it("all-skipped review is not execution", async () => {
    const f = forge({ runs: [checkRun({ name: "agentic-review", conclusion: "skipped" })] });
    const r = await merge(f);
    assert.equal(r.code, EXIT.not_ready, r.err);
    assert.ok(r.out.blocked.includes("ci_not_executed"));
    assert.deepEqual(f.writes, []);
  });
});

describe("merge: happy path", () => {
  it("merges once at the exact head and confirms the PR through a forge read", async () => {
    const f = forge();
    const r = await merge(f);
    assert.equal(r.code, EXIT.ok, r.err);
    assert.equal(r.out.merged, true);
    assert.equal(r.out.merged_this_call, true);
    assert.equal(r.out.merge_commit_sha, MERGE_SHA);
    assert.equal(r.out.pr_head_sha, HEAD);
    assert.deepEqual(f.writes, ["PUT /pulls/7/merge"]);
    assert.equal(f.s.mergeBody.sha, HEAD);
    assert.equal(f.calls.at(-1), "GET /pulls/7");
  });

  it("waits while checks run, then merges once they pass", async () => {
    const f = forge({ runs: [checkRun({ status: "in_progress", conclusion: null })] });
    const r = await merge(f, [], {
      onSleep: (secs) => {
        if (secs >= 6) f.s.runs = [checkRun()];
      },
    });
    assert.equal(r.code, EXIT.ok, r.err);
    assert.equal(f.writes.length, 1);
  });

  it("passes a required review only with a zero-finding review of the exact head", async () => {
    const f = forge({ comments: agentic(0) });
    const r = await merge(f, [], { env: { ARGUS_FORGE_REVIEW: "required" } });
    assert.equal(r.code, EXIT.ok, r.err);
    assert.equal(r.out.review.finding_count, 0);
  });
});

describe("merge: never merges without the grant", () => {
  const bare = {
    ARGUS_MERGE_AUTH_KEY: undefined,
    ARGUS_MERGE_AUTH_TASK: undefined,
    ARGUS_MERGE_AUTH_ATTEMPT: undefined,
    ARGUS_MERGE_AUTHORIZED: undefined,
  };
  for (const [name, env] of [
    ["no grant at all", bare],
    ["a bare worker-set ARGUS_MERGE_AUTHORIZED", { ...bare, ARGUS_MERGE_AUTHORIZED: "yes" }],
    ["a token forged without the key", { ...bare, ARGUS_MERGE_AUTHORIZED: "0".repeat(64) }],
    ["a token for another attempt", { ARGUS_MERGE_AUTH_ATTEMPT: "2" }],
    ["a token for another task", { ARGUS_MERGE_AUTH_TASK: "t2" }],
  ]) {
    it(`refuses ${name} before any forge request`, async () => {
      const f = forge();
      const r = await merge(f, [], { env });
      assert.equal(r.code, EXIT.error);
      assert.match(r.err, /policy_denied|authoriz/);
      assert.deepEqual(f.calls, []);
    });
  }

  it("wait merge stays observation-only, even with a grant and green checks", async () => {
    const f = forge();
    let t = T0;
    const dir = cwd();
    const code = await main(["wait", "merge", "--pr", "7", "--max", "10"], ENV, {
      fetch: f.fetch,
      cwd: dir,
      git: () => join(dir, ".git"),
      now: () => t,
      sleep: async (ms) => {
        t += ms;
      },
      stdout: { write() {} },
      stderr: { write() {} },
    });
    assert.equal(code, EXIT.pending);
    assert.deepEqual(f.writes, []);
  });
});

describe("merge: fails closed", () => {
  const blocked = async (f, argv, env, reason) => {
    const r = await merge(f, argv, { env });
    assert.equal(r.code, EXIT.not_ready, r.err);
    assert.ok(r.out.blocked.includes(reason), JSON.stringify(r.out));
    assert.equal(r.out.merged, false);
    assert.deepEqual(f.writes, []);
    return r;
  };

  it("fails fast on a failed check with an actionable result", async () => {
    const f = forge({
      runs: [
        checkRun({ conclusion: "failure", html_url: `${REPO}/runs/11` }),
        checkRun({ id: 12, name: "lint", status: "in_progress", conclusion: null }),
      ],
      ...failedRun(HEAD, 900, 11, "Error: boom Bearer abc"),
    });
    const r = await blocked(f, [], {}, "ci_failed");
    assert.match(r.out.action, new RegExp(`ci-digest --sha ${HEAD}`));
    assert.equal(r.out.waited_s, 0);
    assert.equal(r.out.pr_head_sha, HEAD);
    const d = r.out.diagnostics;
    assert.deepEqual(d.checks, [{ context: "test", state: "failure", link: `${REPO}/runs/11` }]);
    assert.deepEqual(
      d.excerpts.map((e) => [e.run_id, e.job_id, e.text]),
      [[900, 11, "Error: boom Bearer [REDACTED]"]],
    );
  });

  it("gates only the new head: an old head's failure does not block a green one", async () => {
    const f = forge({
      pr: { ...forge().s.pr, head: { sha: MOVED } },
      runs: [checkRun({ conclusion: "failure" })],
      movedRuns: [checkRun({ id: 13 })],
      ...failedRun(HEAD, 900, 11, "Error: old head"),
    });
    const r = await merge(f);
    assert.equal(r.code, EXIT.ok, r.err);
    assert.equal(r.out.diagnostics, undefined);
    assert.equal(f.s.mergeBody.sha, MOVED);
    assert.ok(!f.calls.some((c) => c.startsWith("GET /actions/")), f.calls.join());
  });

  for (const [name, run, code] of [
    ["failed", { conclusion: "failure" }, EXIT.not_ready],
    ["pending", { status: "in_progress", conclusion: null }, EXIT.pending],
  ])
    it(`refuses the merge when a green head is replaced by a ${name} one`, async () => {
      const f = forge({
        movedRuns: [checkRun({ id: 13, details_url: `${REPO}/actions/runs/901/job/13`, ...run })],
        ...failedRun(MOVED, 901, 13, "Error: new head"),
      });
      // An old head's failed run must never be attributed to the new head.
      f.s.actionRuns.push({ id: 900, head_sha: HEAD, conclusion: "failure" });
      let reads = 0;
      const base = f.s.pr;
      f.s.pr = () => (++reads <= 1 ? base : { ...base, head: { sha: MOVED } });
      const r = await merge(f, ["--max", "20"]);
      assert.equal(r.code, code, JSON.stringify(r.out));
      assert.equal(r.out.pr_head_sha, MOVED);
      assert.deepEqual(f.writes, []);
      if (code === EXIT.not_ready) {
        assert.deepEqual(r.out.blocked, ["ci_failed"]);
        assert.equal(r.out.diagnostics.head_sha, MOVED);
        assert.deepEqual(
          r.out.diagnostics.excerpts.map((e) => [e.run_id, e.text]),
          [[901, "Error: new head"]],
        );
      } else assert.equal(r.out.diagnostics, undefined);
    });

  it("does not treat an all-skipped run as execution", async () => {
    await blocked(
      forge({ runs: [checkRun({ conclusion: "skipped" })] }),
      [],
      {},
      "ci_not_executed",
    );
  });

  it("a standing changes-requested review blocks", async () => {
    const reviews = [
      {
        id: 1,
        state: "CHANGES_REQUESTED",
        user: { login: "rev" },
        submitted_at: "2026-11-18T16:00:00Z",
      },
    ];
    await blocked(forge({ reviews }), [], {}, "changes_requested");
  });

  it("agentic review findings block even when the review policy is off", async () => {
    await blocked(
      forge({ comments: agentic(2) }),
      [],
      { ARGUS_FORGE_REVIEW: "off" },
      "review_findings",
    );
  });

  for (const state of ["blocked", "behind", "dirty", "draft"]) {
    it(`branch rules: mergeable_state ${state} blocks`, async () => {
      const f = forge();
      f.s.pr = { ...f.s.pr, mergeable_state: state };
      await blocked(f, [], {}, `merge_${state}`);
    });
  }

  it("a closed PR is never merged and never recreated", async () => {
    const f = forge();
    f.s.pr = { ...f.s.pr, state: "closed" };
    await blocked(f, [], {}, "pr_closed");
  });

  it("an explicit --sha that is no longer the head fails as moved", async () => {
    await blocked(forge(), ["--sha", MOVED], {}, "head_moved");
  });

  it("the forge refusing the PUT is a blocked result, not a merge", async () => {
    const f = forge({ putStatus: 405 });
    const r = await merge(f);
    assert.equal(r.code, EXIT.not_ready, r.err);
    assert.deepEqual(r.out.blocked, ["merge_refused"]);
    assert.equal(r.out.merged, false);
  });
});

describe("merge: required review policy (not conflated with off, acceptance or CI)", () => {
  const required = { ARGUS_FORGE_REVIEW: "required" };

  it("waits, never merges, when no agentic review exists", async () => {
    const f = forge();
    const r = await merge(f, ["--max", "20"], { env: required });
    assert.equal(r.code, EXIT.pending);
    assert.deepEqual(r.out.waiting, ["review_missing"]);
    assert.deepEqual(f.writes, []);
  });

  it("a review of an older head does not count", async () => {
    const f = forge({ comments: agentic(0, MOVED) });
    const r = await merge(f, ["--max", "20"], { env: required });
    assert.equal(r.code, EXIT.pending);
    assert.deepEqual(f.writes, []);
  });

  it("an unset or unknown policy counts as required", async () => {
    for (const value of [undefined, "", "maybe"]) {
      const f = forge();
      const r = await merge(f, ["--max", "20"], { env: { ARGUS_FORGE_REVIEW: value } });
      assert.equal(r.code, EXIT.pending, String(value));
      assert.deepEqual(f.writes, []);
    }
  });

  it("off drops only the wait for a review: branch rules and CI still apply", async () => {
    const f = forge();
    f.s.pr = { ...f.s.pr, mergeable_state: "blocked" };
    const r = await merge(f, [], { env: { ARGUS_FORGE_REVIEW: "off" } });
    assert.equal(r.code, EXIT.not_ready);
    assert.deepEqual(f.writes, []);
  });

  it("a review that arrives while waiting lets the merge go ahead", async () => {
    const f = forge();
    const r = await merge(f, [], {
      env: required,
      onSleep: (secs) => {
        if (secs >= 6) f.s.comments = agentic(0);
      },
    });
    assert.equal(r.code, EXIT.ok, r.err);
    assert.equal(f.writes.length, 1);
  });
});

describe("merge: PR 428 contradictory check shape", () => {
  const contradictory = () =>
    checkRun({
      status: "in_progress",
      conclusion: "success",
      completed_at: "2026-11-18T17:20:00Z",
    });

  it("merges when the workflow run is completed/success for this exact head", async () => {
    const f = forge({ runs: [contradictory()] });
    const r = await merge(f);
    assert.equal(r.code, EXIT.ok, r.err);
    assert.ok(f.calls.includes("GET /actions/runs/900"));
    assert.equal(f.writes.length, 1);
  });

  for (const [name, workflow] of [
    [
      "the workflow run is still in progress",
      { id: 900, status: "in_progress", conclusion: null, head_sha: HEAD },
    ],
    [
      "the workflow run failed",
      { id: 900, status: "completed", conclusion: "failure", head_sha: HEAD },
    ],
    [
      "the workflow run is for another commit",
      { id: 900, status: "completed", conclusion: "success", head_sha: MOVED },
    ],
  ]) {
    it(`never infers success when ${name}`, async () => {
      const f = forge({ runs: [contradictory()], workflow });
      const r = await merge(f, ["--max", "20"]);
      assert.equal(r.code, EXIT.pending);
      assert.deepEqual(r.out.waiting, ["ci_contradictory"]);
      assert.deepEqual(r.out.ci.contradictory, ["test"]);
      assert.deepEqual(f.writes, []);
    });
  }

  it("never infers success when the check has no workflow run to ask", async () => {
    const f = forge({ runs: [{ ...contradictory(), details_url: null }] });
    const r = await merge(f, ["--max", "20"]);
    assert.equal(r.code, EXIT.pending);
    assert.deepEqual(f.writes, []);
  });

  it("fails closed when the workflow run cannot be read", async () => {
    const f = forge({ runs: [contradictory()] });
    f.s.workflow = undefined;
    const r = await merge(f, ["--max", "20"]);
    assert.notEqual(r.code, EXIT.ok);
    assert.deepEqual(f.writes, []);
  });
});

describe("merge: states, retries and bounds", () => {
  it("an already merged PR is reported, with no second merge", async () => {
    const f = forge();
    f.s.pr = { ...f.s.pr, state: "closed", merged: true, merge_commit_sha: MERGE_SHA };
    const r = await merge(f);
    assert.equal(r.code, EXIT.ok);
    assert.equal(r.out.already_merged, true);
    assert.deepEqual(f.writes, []);
  });

  it("a re-run after the merge landed reports it and does not merge twice", async () => {
    const f = forge();
    assert.equal((await merge(f)).code, EXIT.ok);
    const again = await merge(f);
    assert.equal(again.code, EXIT.ok);
    assert.equal(again.out.already_merged, true);
    assert.equal(f.writes.length, 1);
  });

  it("a merged PR without a merge commit is an error, not success", async () => {
    const f = forge();
    f.s.pr = { ...f.s.pr, state: "closed", merged: true };
    const r = await merge(f);
    assert.equal(r.code, EXIT.error);
  });

  it("a head that moves between the gates and the merge is re-gated, not merged", async () => {
    const f = forge();
    let reads = 0;
    const base = f.s.pr;
    f.s.pr = () => (++reads <= 3 ? base : { ...base, head: { sha: MOVED } });
    f.s.runs = [checkRun()];
    const r = await merge(f, ["--max", "10"]);
    assert.notEqual(r.code, EXIT.ok);
    assert.deepEqual(f.writes, []);
  });

  it("an unreachable forge ends pending at the cap and never merges", async () => {
    const f = forge();
    const dead = async () => new Response("down", { status: 503 });
    const r = await merge({ ...f, fetch: dead }, ["--max", "20"]);
    assert.equal(r.code, EXIT.pending);
    assert.match(r.out.waiting[0], /forge_/);
    assert.deepEqual(f.writes, []);
  });

  it("missing mergeability data is not readiness", async () => {
    const f = forge();
    f.s.pr = { ...f.s.pr, mergeable: null, mergeable_state: "unknown" };
    const r = await merge(f, ["--max", "20"]);
    assert.equal(r.code, EXIT.pending);
    assert.deepEqual(r.out.waiting, ["mergeability_unknown"]);
    assert.deepEqual(f.writes, []);
  });

  it("a steer ends the call with exit 11 and no merge", async () => {
    const f = forge({ runs: [checkRun({ status: "in_progress", conclusion: null })] });
    const r = await merge(f, [], {
      onSleep: (_secs, dir) => {
        mkdirSync(join(dir, ".git", "argus"), { recursive: true });
        writeFileSync(
          join(dir, ".git", "argus", "interrupt"),
          JSON.stringify({ at: new Date(T0 + 1000).toISOString() }),
        );
      },
    });
    assert.equal(r.code, EXIT.interrupted);
    assert.equal(r.out.merged, false);
    assert.deepEqual(f.writes, []);
  });

  it("rejects an invalid merge method before any request", async () => {
    const f = forge();
    const r = await merge(f, ["--merge-method", "octopus"]);
    assert.equal(r.code, EXIT.usage);
    assert.deepEqual(f.calls, []);
  });
});
