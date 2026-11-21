import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import {
  appendFileSync,
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, it } from "node:test";
import { ciSummary, digestLog, EXIT, main, pollDelay, waitCap } from "./argus-task.mjs";

const REPO = "https://git.example.com/team/project";
const ENV = {
  ARGUS_REPOSITORY_URL: REPO,
  ARGUS_REPOSITORIES_JSON: JSON.stringify({ p: REPO }),
};
const API = "/api/v1/repos/team/project";
const HEAD = "a".repeat(40);
const NEW = "b".repeat(40);
const T0 = Date.parse("2026-11-02T10:00:00Z");
const iso = (s) => new Date(T0 + s * 1000).toISOString();

const scratch = mkdtempSync(join(tmpdir(), "argus-task-"));
after(() => rmSync(scratch, { recursive: true, force: true }));

const git = (args, cwd) =>
  execFileSync("git", args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
// The fake checks run with a PATH of their own; the worker git front needs python3 on PATH, so use the real git.
const gitBin = ["/usr/bin/git", "/usr/local/bin/git"].find((p) => existsSync(p)) ?? "git";

// Worker git fronts inject the project identity and reject identity writes; CI has none.
function identity(dir) {
  if (process.env.ARGUS_GIT_AUTHOR_NAME && process.env.ARGUS_GIT_AUTHOR_EMAIL) return;
  git(["config", "user.email", "t@example.com"], dir);
  git(["config", "user.name", "t"], dir);
}

let repoCount = 0;
function repo() {
  const dir = join(scratch, `r${repoCount++}`);
  mkdirSync(dir);
  // No identity here: only the commit fixtures need one, and the sandbox refuses git config writes.
  git(["init", "-q", "-b", "main"], dir);
  return dir;
}

/** A fake forge: `routes["GET /pulls/7"]` is a value or `(url, body) => value | Response`. */
function forge(routes) {
  const calls = [];
  const fetch = async (target, init = {}) => {
    const url = new URL(target);
    const key = `${init.method ?? "GET"} ${url.pathname.slice(API.length) || "/"}`;
    calls.push(key);
    const route = routes[key];
    if (route === undefined) return new Response("{}", { status: 404 });
    const body = init.body ? JSON.parse(init.body) : undefined;
    const value = typeof route === "function" ? route(url, body) : route;
    return value instanceof Response ? value : Response.json(value);
  };
  return { fetch, calls };
}

/** Run the CLI with a fake clock that `sleep` advances; `onSleep(seconds)` runs after each tick. */
async function run(argv, { routes, cwd = repo(), env = {}, onSleep, onFetch, git: gitIo } = {}) {
  let t = T0;
  const out = [];
  const err = [];
  const f = forge(routes ?? {});
  // `onFetch(key, seconds, cwd)` runs when a request starts, with the 1-based call number
  // and the seconds elapsed on the fake clock; it returns the seconds the response takes.
  const inner = f.fetch;
  f.fetch = async (target, init) => {
    const response = await inner(target, init);
    t += (onFetch?.(f.calls.at(-1), f.calls.length, (t - T0) / 1000, cwd) ?? 0) * 1000;
    return response;
  };
  const code = await main(
    argv,
    { ...ENV, ...env },
    {
      fetch: f.fetch,
      cwd,
      git: gitIo,
      now: () => t,
      sleep: async (ms) => {
        t += ms;
        onSleep?.((t - T0) / 1000, cwd);
      },
      retrySleep: async () => {},
      stdout: { write: (s) => out.push(s) },
      stderr: { write: (s) => err.push(s) },
    },
  );
  return {
    code,
    out: out.length ? JSON.parse(out.join("")) : null,
    raw: out.join(""),
    err: err.join(""),
    calls: f.calls,
    elapsed: (t - T0) / 1000,
    cwd,
  };
}

const signals = (cwd) => join(cwd, ".git", "argus");
const pr = (extra = {}) => ({
  number: 7,
  head: { sha: HEAD, ref: "feature" },
  html_url: `${REPO}/pulls/7`,
  ...extra,
});

describe("ciSummary", () => {
  it("keeps the newest status per context including agentic review", () => {
    assert.deepEqual(
      ciSummary([
        { id: 1, context: "test", status: "failure" },
        { id: 2, context: "test", status: "success" },
        { id: 3, context: "lint", status: "pending" },
        { id: 4, context: "agentic-review", status: "pending" },
      ]),
      { state: "pending", total: 3, running: 2, failed: [] },
    );
    assert.equal(ciSummary([]).state, "missing");
    assert.equal(ciSummary([{ context: "a", state: "error" }]).state, "failure");
  });

  for (const [name, states, want] of [
    ["an unknown string", ["success", "bogus"], "unknown"],
    ["a missing state", ["success", undefined], "unknown"],
    ["a Gitea warning", ["warning"], "unknown"],
    ["neutral", ["neutral"], "success"],
    ["skipped only", ["skipped", "skipped"], "skipped"],
    ["skipped and success", ["skipped", "success"], "success"],
    ["skipped and unknown", ["skipped", "bogus"], "unknown"],
    ["cancelled", ["success", "cancelled"], "failure"],
    ["timed_out", ["timed_out"], "failure"],
    ["error", ["error"], "failure"],
    ["failure beats pending and unknown", ["failure", "pending", "bogus"], "failure"],
    ["pending beats unknown", ["bogus", "queued"], "pending"],
    ["queued", ["queued"], "pending"],
    ["in_progress", ["in_progress"], "pending"],
    ["waiting", ["waiting"], "pending"],
    ["requested", ["requested"], "pending"],
    ["running", ["running"], "pending"],
  ])
    it(`summarizes ${name} as ${want}`, () => {
      const statuses = states.map((state, i) => ({ id: i, context: `c${i}`, state }));
      const ci = ciSummary(statuses);
      assert.equal(ci.state, want);
      assert.equal(ci.total, states.length);
    });
});

describe("waitCap", () => {
  it("defaults to 90 s and never exceeds ARGUS_TASK_WAIT_MAX_S", () => {
    assert.equal(waitCap(undefined, {}), 90);
    assert.equal(waitCap(undefined, { ARGUS_TASK_WAIT_MAX_S: "480" }), 480);
    assert.equal(waitCap("600", { ARGUS_TASK_WAIT_MAX_S: "480" }), 480);
    assert.equal(waitCap("60", { ARGUS_TASK_WAIT_MAX_S: "480" }), 60);
    assert.equal(waitCap(undefined, { ARGUS_TASK_WAIT_MAX_S: "9999" }), 540);
  });
});

describe("wait ci", () => {
  const mixed = [
    { id: 1, context: "lint", status: "failure" },
    { id: 2, context: "node", status: "pending" },
    { id: 3, context: "python", status: "in_progress" },
  ];

  it("fails fast on the first failed check while others still run", async () => {
    const r = await run(["wait", "ci", "--pr", "7"], {
      routes: { "GET /pulls/7": pr(), [`GET /statuses/${HEAD}`]: mixed },
    });
    assert.equal(r.code, EXIT.ok);
    assert.equal(r.elapsed, 0);
    assert.deepEqual([r.out.state, r.out.failed, r.out.running], ["failure", ["lint"], 2]);
    assert.match(r.out.action, /job-logs.*2 checks are still running.*superseded/);
    // The terminal record carries the fail-fast outcome and is not continued by a later wait.
    const file = join(signals(r.cwd), "waiting.json");
    assert.deepEqual(JSON.parse(readFileSync(file, "utf8")).outcome, {
      state: "failure",
      failed: ["lint"],
    });
    const later = await run(["wait", "ci", "--pr", "7", "--max", "10"], {
      cwd: r.cwd,
      routes: { "GET /pulls/7": pr(), [`GET /statuses/${HEAD}`]: [mixed[1]] },
    });
    assert.equal(later.code, EXIT.pending);
    assert.equal(JSON.parse(readFileSync(file, "utf8")).outcome, undefined);
  });

  it("says nothing about running checks when none are left", async () => {
    const r = await run(["wait", "ci", "--pr", "7"], {
      routes: { "GET /pulls/7": pr(), [`GET /statuses/${HEAD}`]: [mixed[0]] },
    });
    assert.equal(r.out.running, 0);
    assert.doesNotMatch(r.out.action, /still running/);
  });

  it("with --all keeps waiting after a failure until every check has finished", async () => {
    let checks = 0;
    const routes = {
      "GET /pulls/7": pr(),
      [`GET /statuses/${HEAD}`]: () => [
        { id: 1, context: "lint", status: "failure" },
        { id: 2, context: "test", status: ++checks < 3 ? "pending" : "success" },
      ],
    };
    const capped = await run(["wait", "ci", "--pr", "7", "--all", "--max", "2"], { routes });
    assert.equal(capped.code, EXIT.pending);
    assert.equal(capped.out.running, 1);
    checks = 0;
    const r = await run(["wait", "ci", "--pr", "7", "--all"], { routes });
    assert.equal(r.code, EXIT.ok);
    assert.equal(r.elapsed, 9);
    assert.deepEqual([r.out.state, r.out.failed, r.out.running], ["failure", ["lint"], 0]);
    assert.deepEqual(r.out.diagnostics.checks, [{ context: "lint", state: "failure" }]);
    assert.doesNotMatch(r.out.action, /still running/);
  });

  it("does not fail fast on a failure of a commit the PR no longer points at", async () => {
    const r = await run(["wait", "ci", "--pr", "7", "--sha", NEW, "--max", "5"], {
      routes: { "GET /pulls/7": pr(), [`GET /statuses/${NEW}`]: mixed },
    });
    assert.equal(r.code, EXIT.pending);
    assert.equal(r.out.head_matches, false);
    assert.match(r.out.status, /is not the watched/);
  });

  const TOKEN = `ghp_${"x".repeat(36)}`;
  /** A failed run 11 of HEAD with failed jobs `ids`, plus a failed run 12 of another head. */
  const failedRuns = (ids, log = () => new Response("ok\nFAILED test_x\n")) => {
    const routes = {
      "GET /actions/runs": (url) => {
        assert.equal(url.searchParams.get("head_sha"), HEAD);
        return {
          workflow_runs: [
            { id: 11, head_sha: HEAD.toUpperCase(), conclusion: "failure" },
            { id: 12, head_sha: NEW, conclusion: "failure" },
          ],
        };
      },
      "GET /actions/runs/11/jobs": {
        jobs: ids.map((id) => ({
          id,
          name: `test ${id}`,
          conclusion: "failure",
          html_url: `${REPO}/actions/runs/11/jobs/${id}`,
        })),
      },
      "GET /actions/runs/12/jobs": () => assert.fail("a run of another head was read"),
    };
    for (const id of ids) routes[`GET /actions/jobs/${id}/logs`] = log;
    return routes;
  };

  it("attaches sanitized diagnostics of the watched head when a pending check fails", async () => {
    let checks = 0;
    const r = await run(["wait", "ci", "--pr", "7"], {
      routes: {
        "GET /pulls/7": pr(),
        [`GET /statuses/${HEAD}`]: () => [
          {
            id: 1,
            context: "lint",
            status: ++checks < 2 ? "pending" : "failure",
            target_url: `${REPO}/actions/runs/11`,
          },
          {
            id: 2,
            context: "ext",
            status: checks < 2 ? "pending" : "failure",
            target_url: "https://evil.example/x",
          },
          { id: 3, context: "ok", status: "success" },
        ],
        ...failedRuns(
          [21],
          () =>
            new Response(
              `\x1b[31mError: push to https://u:${TOKEN}@git.example.com failed\x07\nAuthorization: Bearer abc\ncurl -H "Bearer abc"\ntoken=${TOKEN}\n`,
            ),
        ),
      },
    });
    assert.equal(r.code, EXIT.ok, r.err);
    assert.equal(r.out.state, "failure");
    const d = r.out.diagnostics;
    assert.deepEqual(d.checks, [
      { context: "lint", state: "failure", link: `${REPO}/actions/runs/11` },
      { context: "ext", state: "failure" },
    ]);
    assert.equal(d.head_sha, HEAD);
    assert.equal(d.failed_runs, 1);
    assert.deepEqual(
      d.excerpts.map((e) => [e.run_id, e.job_id, e.job, e.link]),
      [[11, 21, "test 21", `${REPO}/actions/runs/11/jobs/21`]],
    );
    const text = d.excerpts[0].text;
    assert.doesNotMatch(r.raw, /ghp_|Bearer abc|\\u001b|\\u0007/);
    assert.match(text, /^Error: push to https:\/\/\[REDACTED\]@git\.example\.com failed$/m);
    assert.match(text, /^Authorization: \[REDACTED\]$/m);
    assert.match(text, /Bearer \[REDACTED\]"/);
    assert.match(text, /^token=\[REDACTED\]$/m);
    assert.match(r.out.action, new RegExp(`ci-digest --sha ${HEAD}`));
    // The terminal record keeps its shape: the diagnostics stay in the command output.
    const record = JSON.parse(readFileSync(join(signals(r.cwd), "waiting.json"), "utf8"));
    assert.deepEqual(record.outcome, { state: "failure", failed: ["lint", "ext"] });
    assert.equal(record.diagnostics, undefined);
  });

  it("bounds the diagnostics of many failed jobs and marks them truncated", async () => {
    const ids = Array.from({ length: 12 }, (_, i) => 30 + i);
    const log = () => new Response(Array.from({ length: 60 }, (_, i) => `error ${i}`).join("\n"));
    const r = await run(["wait", "ci", "--pr", "7"], {
      routes: {
        "GET /pulls/7": pr(),
        [`GET /statuses/${HEAD}`]: [{ id: 1, context: "test", status: "failure" }],
        ...failedRuns(ids, log),
      },
    });
    const d = r.out.diagnostics;
    assert.equal(d.truncated, true);
    assert.ok(d.excerpts.length >= 1 && d.excerpts.length < ids.length);
    assert.ok(Buffer.byteLength(JSON.stringify(d)) <= 2048, `${JSON.stringify(d).length} bytes`);
  });

  it("does not report a failure whose head moved during the diagnostics fetch", async () => {
    let moved = false;
    const r = await run(["wait", "ci", "--pr", "7", "--max", "5"], {
      routes: {
        "GET /pulls/7": () => pr(moved ? { head: { sha: NEW, ref: "feature" } } : {}),
        [`GET /statuses/${HEAD}`]: [{ id: 1, context: "test", status: "failure" }],
        ...failedRuns([21], () => {
          moved = true;
          return new Response("FAILED\n");
        }),
      },
    });
    assert.equal(r.code, EXIT.pending, JSON.stringify(r.out));
    assert.equal(r.out.head_matches, false);
    assert.match(
      r.out.status,
      new RegExp(`PR head ${NEW.slice(0, 7)} is not the watched ${HEAD.slice(0, 7)}`),
    );
    assert.equal(r.out.diagnostics, undefined);
    assert.equal(
      JSON.parse(readFileSync(join(signals(r.cwd), "waiting.json"), "utf8")).outcome,
      undefined,
    );
  });

  for (const [name, routes, want] of [
    [
      "a job log cannot be read",
      { ...failedRuns([21], () => new Response("{}", { status: 404 })) },
      (d) => {
        assert.equal(d.excerpts[0].note, "log not read: not_found");
        assert.equal(d.diagnostics_unavailable, undefined);
      },
    ],
    [
      "the run list cannot be read",
      { "GET /actions/runs": () => new Response("{}", { status: 403 }) },
      (d) => {
        assert.deepEqual([d.failed_runs, d.excerpts], [0, []]);
        assert.ok(d.diagnostics_unavailable);
      },
    ],
    [
      "the job list cannot be read",
      {
        ...failedRuns([21]),
        "GET /actions/runs/11/jobs": () => new Response("{}", { status: 404 }),
      },
      (d) => {
        assert.deepEqual([d.failed_runs, d.excerpts], [1, []]);
        assert.equal(d.diagnostics_unavailable, "not_found");
      },
    ],
  ])
    it(`still reports the failure when ${name}`, async () => {
      const r = await run(["wait", "ci", "--pr", "7"], {
        routes: {
          "GET /pulls/7": pr(),
          [`GET /statuses/${HEAD}`]: [{ id: 1, context: "test", status: "failure" }],
          ...routes,
        },
      });
      assert.deepEqual([r.code, r.out.state, r.out.failed], [EXIT.ok, "failure", ["test"]]);
      assert.deepEqual(r.out.diagnostics.checks, [{ context: "test", state: "failure" }]);
      want(r.out.diagnostics);
    });

  it("keeps the failure when the diagnostics run out of time", async () => {
    const r = await run(["wait", "ci", "--pr", "7", "--max", "15"], {
      routes: {
        "GET /pulls/7": pr(),
        [`GET /statuses/${HEAD}`]: [{ id: 1, context: "test", status: "failure" }],
        ...failedRuns([21]),
      },
      onFetch: (key) => (key === "GET /actions/runs" ? 8 : 0),
    });
    assert.deepEqual([r.code, r.out.state], [EXIT.ok, "failure"]);
    assert.equal(r.out.diagnostics.diagnostics_unavailable, "deadline_exceeded");
    assert.deepEqual(r.out.diagnostics.excerpts, []);
  });

  it("reports success only when every check finished and none failed", async () => {
    const r = await run(["wait", "ci", "--pr", "7"], {
      routes: {
        "GET /pulls/7": pr(),
        [`GET /statuses/${HEAD}`]: [
          { id: 1, context: "a", status: "success" },
          { id: 2, context: "b", status: "success" },
        ],
      },
    });
    assert.deepEqual([r.code, r.out.state, r.out.action], [EXIT.ok, "success", undefined]);
  });

  for (const [name, status, state] of [
    ["an unrecognized state", { status: "bogus" }, "unknown"],
    ["a missing state", {}, "unknown"],
    ["a Gitea warning", { status: "warning" }, "unknown"],
    ["success", { status: "success" }, "success"],
    ["skipped", { status: "skipped" }, "skipped"],
  ])
    it(`finishes with state ${state} for ${name}`, async () => {
      const r = await run(["wait", "ci", "--pr", "7"], {
        routes: {
          "GET /pulls/7": pr(),
          [`GET /statuses/${HEAD}`]: [{ id: 1, context: "test", ...status }],
        },
      });
      assert.equal(r.code, EXIT.ok);
      assert.equal(r.out.state, state);
      assert.equal(r.out.diagnostics, undefined);
      assert.equal(r.calls.filter((c) => c.includes("/actions/")).length, 0);
    });

  it("does not retry a failing request inside a wait; the next check is the retry", async () => {
    let checks = 0;
    const r = await run(["wait", "ci", "--pr", "7"], {
      routes: {
        "GET /pulls/7": pr(),
        [`GET /statuses/${HEAD}`]: () =>
          ++checks === 1
            ? new Response("{}", { status: 503 })
            : [{ id: 1, context: "test", status: "success" }],
      },
    });
    assert.equal(r.code, EXIT.ok, r.err);
    assert.equal(r.elapsed, 3);
    assert.equal(r.calls.filter((c) => c.startsWith("GET /statuses")).length, 2);
  });

  it("checks fast at first, then the interval, and returns when CI finishes", async () => {
    let checks = 0;
    let during = null;
    const r = await run(["wait", "ci", "--pr", "7"], {
      routes: {
        "GET /pulls/7": pr(),
        [`GET /statuses/${HEAD}`]: () =>
          ++checks < 3
            ? [{ id: 1, context: "test", status: "pending" }]
            : [{ id: 2, context: "test", status: "failure" }],
      },
      onSleep: (_, cwd) => {
        during ??= JSON.parse(readFileSync(join(signals(cwd), "waiting.json"), "utf8"));
      },
    });
    assert.equal(r.code, EXIT.ok);
    assert.equal(r.out.state, "failure");
    assert.deepEqual(r.out.failed, ["test"]);
    assert.equal(r.elapsed, 9); // checks at 0, 3 and 9 s: 3 then 6, doubling to the interval
    assert.deepEqual(during, { on: "ci", ref: "#7", since: iso(0), sha: HEAD });
    // The terminal record replaces the pending one, so the host can supersede the milestone.
    assert.deepEqual(JSON.parse(readFileSync(join(signals(r.cwd), "waiting.json"), "utf8")), {
      on: "ci",
      ref: "#7",
      since: iso(0),
      sha: HEAD,
      outcome: { state: "failure", failed: ["test"] },
    });
  });

  it("records the outcome of a success and bounds odd context names", async () => {
    const odd = `bad\nname\u00e9${"x".repeat(200)}`;
    const failing = Array.from({ length: 30 }, (_, i) => ({
      id: i + 1,
      context: i === 0 ? odd : `c${i}`,
      status: "failure",
    }));
    const routes = (list) => ({ "GET /pulls/7": pr(), [`GET /statuses/${HEAD}`]: list });
    const ok = await run(["wait", "ci", "--pr", "7"], {
      routes: routes([{ id: 1, context: "test", status: "success" }]),
    });
    assert.deepEqual(
      JSON.parse(readFileSync(join(signals(ok.cwd), "waiting.json"), "utf8")).outcome,
      {
        state: "success",
        failed: [],
      },
    );
    const bad = await run(["wait", "ci", "--pr", "7"], { routes: routes(failing) });
    const { failed } = JSON.parse(
      readFileSync(join(signals(bad.cwd), "waiting.json"), "utf8"),
    ).outcome;
    assert.equal(failed.length, 20);
    assert.match(failed[0], /^[\x20-\x7e]{1,100}$/);
  });

  it("does not continue a finished wait: a terminal record is never reused as previous", async () => {
    const cwd = repo();
    mkdirSync(signals(cwd), { recursive: true });
    const file = join(signals(cwd), "waiting.json");
    writeFileSync(
      file,
      JSON.stringify({
        on: "ci",
        ref: "#7",
        since: iso(-300),
        sha: NEW,
        outcome: { state: "failure", failed: ["lint"] },
      }),
    );
    const r = await run(["wait", "ci", "--pr", "7", "--max", "10"], {
      cwd,
      routes: {
        "GET /pulls/7": pr(),
        [`GET /statuses/${HEAD}`]: [{ id: 1, context: "test", status: "pending" }],
      },
    });
    assert.equal(r.out.rebound_from, undefined);
    assert.equal(r.out.watched_sha, HEAD);
    const saved = JSON.parse(readFileSync(file, "utf8"));
    assert.deepEqual(saved, { on: "ci", ref: "#7", since: iso(0), sha: HEAD });
  });

  it("returns still pending at the cap, keeps the waiting file, and a re-call keeps its start", async () => {
    const routes = {
      "GET /pulls/7": pr(),
      [`GET /statuses/${HEAD}`]: [
        { id: 1, context: "test", status: "pending" },
        { id: 2, context: "lint", status: "success" },
      ],
    };
    const env = { ARGUS_TASK_WAIT_MAX_S: "40" };
    const first = await run(["wait", "ci", "--pr", "7", "--max", "60"], { routes, env });
    assert.equal(first.code, EXIT.pending);
    assert.equal(first.elapsed, 40);
    assert.equal(first.out.status, "1 of 2 checks running");
    const file = join(signals(first.cwd), "waiting.json");
    writeFileSync(file, JSON.stringify({ on: "ci", ref: "#7", since: iso(-300) }));
    const again = await run(["wait", "ci", "--pr", "7"], { routes, env, cwd: first.cwd });
    assert.equal(again.code, EXIT.pending);
    assert.equal(JSON.parse(readFileSync(file, "utf8")).since, iso(-300));
  });

  it("keeps a stored sha while the PR head is still it, and says so when it rebinds", async () => {
    const routes = {
      "GET /pulls/7": pr(),
      [`GET /statuses/${HEAD}`]: [{ id: 1, context: "test", status: "pending" }],
      [`GET /statuses/${NEW}`]: [{ id: 1, context: "test", status: "pending" }],
    };
    const cwd = repo();
    mkdirSync(signals(cwd), { recursive: true });
    const file = join(signals(cwd), "waiting.json");
    const stored = (sha) =>
      writeFileSync(file, JSON.stringify({ on: "ci", ref: "#7", since: iso(-60), sha }));
    stored(HEAD);
    const same = await run(["wait", "ci", "--pr", "7", "--max", "10"], { routes, cwd });
    assert.equal(same.out.watched_sha, HEAD);
    assert.equal(same.out.rebound_from, undefined);
    stored(NEW);
    const moved = await run(["wait", "ci", "--pr", "7", "--max", "10"], { routes, cwd });
    assert.deepEqual([moved.out.watched_sha, moved.out.rebound_from], [HEAD, NEW]);
    const saved = JSON.parse(readFileSync(file, "utf8"));
    assert.deepEqual([saved.sha, saved.since], [HEAD, iso(-60)]);
  });

  it("ends at once on a steer written after it started, not on an older one", async () => {
    const routes = {
      "GET /pulls/7": pr(),
      [`GET /statuses/${HEAD}`]: [{ id: 1, context: "test", status: "pending" }],
    };
    const cwd = repo();
    mkdirSync(signals(cwd), { recursive: true });
    const marker = join(signals(cwd), "interrupt");
    writeFileSync(marker, JSON.stringify({ reason: "steer", at: iso(-10) }));
    const r = await run(["wait", "ci", "--pr", "7", "--max", "100"], {
      routes,
      cwd,
      onSleep: (s) => {
        if (s === 5) writeFileSync(marker, JSON.stringify({ reason: "steer", at: iso(5) }));
      },
    });
    assert.equal(r.code, EXIT.interrupted);
    assert.equal(r.out.interrupted, "steer");
    assert.equal(r.elapsed, 5);
    assert.equal(existsSync(join(signals(cwd), "waiting.json")), false);
    assert.equal(existsSync(marker), true); // the host owns the marker
  });
});

describe("wait ci pins one commit", () => {
  const X = HEAD;
  const failedX = [{ id: 1, context: "lint", status: "failure" }];
  const pendingY = [{ id: 2, context: "test", status: "pending" }];
  const prAt = (sha) => pr({ head: { sha, ref: "feature" } });

  // The first check sees head X with a failure and a running check; then Y is pushed and pending.
  it("does not finish on the checks of a previous head", async () => {
    let heads = 0;
    const r = await run(["wait", "ci", "--pr", "7", "--all", "--max", "100"], {
      routes: {
        "GET /pulls/7": () => prAt(++heads === 1 ? X : NEW),
        [`GET /statuses/${X}`]: () => [...failedX, ...(heads < 3 ? pendingY : [])],
        [`GET /statuses/${NEW}`]: pendingY,
      },
    });
    // X finishes at the third check, but by then the PR head is Y: never done.
    assert.equal(r.code, EXIT.pending);
    assert.deepEqual([r.out.watched_sha, r.out.pr_head_sha, r.out.head_matches], [X, NEW, false]);
    assert.deepEqual(r.out.failed, ["lint"]);
    // The failed old head is not an outcome: the record stays pending, on the watched commit.
    assert.deepEqual(JSON.parse(readFileSync(join(signals(r.cwd), "waiting.json"), "utf8")), {
      on: "ci",
      ref: "#7",
      since: iso(0),
      sha: X,
    });
  });

  it("never returns done while the PR head is not the watched commit", async () => {
    const routes = {
      "GET /pulls/7": prAt(X),
      [`GET /statuses/${NEW}`]: [{ id: 1, context: "test", status: "success" }],
      [`GET /statuses/${X}`]: failedX,
    };
    const explicit = await run(["wait", "ci", "--pr", "7", "--sha", NEW, "--max", "80"], {
      routes,
    });
    assert.equal(explicit.code, EXIT.pending);
    assert.equal(explicit.elapsed, 80);
    assert.equal(explicit.out.head_matches, false);
    assert.equal(
      explicit.out.status,
      `PR head ${X.slice(0, 7)} is not the watched ${NEW.slice(0, 7)}`,
    );
    assert.equal(explicit.out.watched_sha, NEW);
    assert.equal(explicit.out.pr_head_sha, X);
  });

  it("binds an omitted sha once, at the first observation", async () => {
    let heads = 0;
    const r = await run(["wait", "ci", "--pr", "7", "--max", "100"], {
      routes: {
        "GET /pulls/7": () => prAt(++heads === 1 ? X : NEW),
        [`GET /statuses/${X}`]: pendingY,
        [`GET /statuses/${NEW}`]: [{ id: 3, context: "test", status: "success" }],
      },
    });
    assert.equal(r.code, EXIT.pending); // X stays pending and Y is never adopted
    assert.deepEqual(
      [r.out.watched_sha, r.out.pr_head_sha, r.out.head_matches, r.out.state],
      [X, NEW, false, "pending"],
    );
    assert.equal(r.calls.filter((c) => c === `GET /statuses/${NEW}`).length, 0);
  });

  it("compares an explicit sha with the current head and keeps it", async () => {
    const r = await run(["wait", "ci", "--pr", "7", "--sha", X.toUpperCase()], {
      routes: { "GET /pulls/7": prAt(X), [`GET /statuses/${X}`]: failedX },
    });
    assert.equal(r.code, EXIT.ok);
    assert.deepEqual([r.out.watched_sha, r.out.pr_head_sha, r.out.head_matches], [X, X, true]);
    assert.equal((await run(["wait", "ci", "--pr", "7", "--sha", "zz"])).code, EXIT.usage);
  });

  for (const [state, statuses, extra] of [
    ["pending", pendingY, { code: EXIT.pending }],
    ["failure", [...failedX, { id: 4, context: "agentic-review", status: "failure" }], {}],
    ["success", [{ id: 5, context: "test", status: "success" }], {}],
  ])
    it(`reports state ${state} for the watched commit only`, async () => {
      const r = await run(["wait", "ci", "--pr", "7", "--max", "10"], {
        routes: {
          "GET /pulls/7": prAt(X),
          [`GET /statuses/${X}`]: statuses,
          [`GET /statuses/${NEW}`]: [{ id: 9, context: "other", status: "failure" }],
        },
      });
      assert.equal(r.code, extra.code ?? EXIT.ok);
      assert.equal(r.out.state, state);
      assert.deepEqual(r.out.failed, state === "failure" ? ["lint", "agentic-review"] : []);
    });

  it("survives a waiting file it cannot write, and never writes through a symlink", async () => {
    const routes = { "GET /pulls/7": prAt(X), [`GET /statuses/${X}`]: failedX };
    const blocked = repo();
    mkdirSync(join(signals(blocked), "waiting.json"), { recursive: true }); // rename onto a directory fails
    const a = await run(["wait", "ci", "--pr", "7"], { routes, cwd: blocked });
    assert.equal(a.code, EXIT.ok, a.err);
    const linked = repo();
    mkdirSync(signals(linked), { recursive: true });
    const outside = join(scratch, "outside.json");
    writeFileSync(outside, "untouched");
    symlinkSync(outside, join(signals(linked), "waiting.json"));
    const b = await run(["wait", "ci", "--pr", "7", "--max", "10"], {
      routes: { ...routes, [`GET /statuses/${X}`]: pendingY },
      cwd: linked,
    });
    assert.equal(b.code, EXIT.pending, b.err);
    assert.equal(readFileSync(outside, "utf8"), "untouched");
  });
});

describe("wait merge", () => {
  const merged = (extra = {}) =>
    pr({ state: "closed", merged: true, merged_at: iso(4), merge_commit_sha: NEW, ...extra });

  it("returns as soon as the PR is merged, with the merge evidence", async () => {
    const r = await run(["wait", "merge", "--pr", "7"], {
      routes: { "GET /pulls/7": merged() },
    });
    assert.equal(r.code, EXIT.ok, r.err);
    assert.equal(r.out.status, "merged");
    assert.equal(r.out.merged, true);
    assert.equal(r.out.merge_commit_sha, NEW);
    assert.equal(r.out.merged_at, iso(4));
    assert.equal(r.elapsed, 0);
  });

  it("keeps waiting while the PR is open and reacts on the first fast re-check", async () => {
    let reads = 0;
    const r = await run(["wait", "merge", "--pr", "7"], {
      routes: {
        // open on the first read, merged on the second
        "GET /pulls/7": () => (++reads === 1 ? pr({ state: "open", merged: false }) : merged()),
      },
    });
    assert.equal(r.code, EXIT.ok, r.err);
    assert.equal(r.elapsed, 3);
    assert.equal(r.out.status, "merged");
  });

  it("treats a PR closed without a merge as terminal, not as success", async () => {
    const r = await run(["wait", "merge", "--pr", "7"], {
      routes: { "GET /pulls/7": pr({ state: "closed", merged: false }) },
    });
    assert.equal(r.code, EXIT.ok, r.err);
    assert.equal(r.out.status, "closed without merge");
    assert.equal(r.out.merged, false);
  });

  it("refuses --sha: a merge wait follows the PR state, not a commit", async () => {
    const r = await run(["wait", "merge", "--pr", "7", "--sha", NEW], {
      routes: { "GET /pulls/7": merged() },
    });
    assert.equal(r.code, 2);
    assert.match(r.err, /wait merge takes no --sha/);
  });

  it("a ci wait ends when the PR is merged, instead of spinning to the cap", async () => {
    // The stale worker: CI still reports running checks, but the owner already merged.
    const r = await run(["wait", "ci", "--pr", "7", "--max", "100"], {
      routes: {
        "GET /pulls/7": merged(),
        [`GET /statuses/${HEAD}`]: [{ id: 1, context: "test", status: "pending" }],
      },
    });
    assert.equal(r.code, EXIT.ok, r.err);
    assert.equal(r.out.status, "merged");
    assert.equal(r.out.merged, true);
    assert.equal(r.out.merge_commit_sha, NEW);
    assert.equal(r.elapsed, 0);
    // No CI state to record: the pending record is cleared, not rewritten with a half outcome.
    assert.equal(existsSync(join(signals(r.cwd), "waiting.json")), false);
  });

  it("a ci wait ends on a closed PR too, and never calls the statuses API", async () => {
    const r = await run(["wait", "ci", "--pr", "7", "--max", "100"], {
      routes: { "GET /pulls/7": pr({ state: "closed", merged: false }) },
    });
    assert.equal(r.code, EXIT.ok, r.err);
    assert.equal(r.out.status, "closed without merge");
    assert.equal(r.calls.filter((c) => c.startsWith("GET /statuses")).length, 0);
  });
});

describe("pollDelay", () => {
  it("starts fast, doubles, and never exceeds the configured interval", () => {
    const delays = [0, 1, 2, 3, 4, 5, 9].map((n) => pollDelay(n, 30_000));
    assert.deepEqual(delays, [3000, 6000, 12000, 24000, 30000, 30000, 30000]);
  });

  it("respects a smaller interval as the ceiling", () => {
    const delays = [0, 1, 2, 3].map((n) => pollDelay(n, 10_000));
    assert.deepEqual(delays, [3000, 6000, 10000, 10000]);
  });
});

describe("wait deadline", () => {
  const link = (body) =>
    new Response(JSON.stringify(body), { headers: { link: '<x>; rel="next"' } });
  const statuses = `GET /statuses/${HEAD}`;
  const pending = (id) => ({ id, context: `c${id}`, status: "pending" });

  it("never returns success for a slow page that lands after the budget", async () => {
    const r = await run(["wait", "ci", "--pr", "7", "--max", "20"], {
      routes: { "GET /pulls/7": pr(), [statuses]: [{ id: 1, context: "t", status: "success" }] },
      onFetch: (key) => (key === statuses ? 25 : 0),
    });
    assert.equal(r.code, EXIT.pending);
    assert.equal(r.out.done, undefined);
    assert.equal(existsSync(join(signals(r.cwd), "waiting.json")), true);
  });

  it("starts no page once the budget is spent, across comments and reviews", async () => {
    let comments = 0;
    const r = await run(["wait", "review", "--pr", "7", "--since", iso(0), "--max", "30"], {
      routes: {
        "GET /issues/7/comments": () => link([{ id: ++comments, body: "x", created_at: iso(-5) }]),
        "GET /pulls/7/reviews": () => link([]),
      },
      onFetch: () => 12, // pages 1-3 start at 0, 12, 24; the 4th would start at 36
    });
    assert.equal(r.code, EXIT.pending);
    assert.equal(r.calls.length, 3);
    assert.equal(comments, 3);
  });

  it("shares the budget between the head lookup and the status request", async () => {
    const r = await run(["wait", "ci", "--pr", "7", "--max", "30"], {
      routes: { "GET /pulls/7": pr(), [statuses]: [pending(1)] },
      onFetch: (key) => (key === "GET /pulls/7" ? 31 : 0),
    });
    assert.equal(r.code, EXIT.pending);
    assert.deepEqual(r.calls, ["GET /pulls/7"]);
    assert.equal(r.out.cursor, null);
  });

  it("treats a transient error followed by budget exhaustion as pending", async () => {
    for (const bad of [new Response("{}", { status: 503 }), null]) {
      const r = await run(["wait", "ci", "--pr", "7", "--max", "40"], {
        routes: {
          "GET /pulls/7": pr(),
          [statuses]: () => {
            if (bad === null) throw new TypeError("fetch failed");
            return bad.clone();
          },
        },
      });
      assert.equal(r.code, EXIT.pending, r.err);
      assert.equal(r.elapsed, 40);
      // checks at 0, 3, 9 and 21 s; the next would be 45 s, past the cap
      assert.equal(r.calls.filter((c) => c === statuses).length, 4);
    }
  });

  it("does not start a request at the deadline, so the second check never runs", async () => {
    const r = await run(["wait", "ci", "--pr", "7", "--max", "30"], {
      routes: { "GET /pulls/7": pr(), [statuses]: [pending(1)] },
      onFetch: (_, n) => (n === 2 ? 30 : 0),
    });
    assert.equal(r.code, EXIT.pending);
    assert.equal(r.calls.length, 2);
  });

  it("returns pending for a result that finishes exactly at the deadline", async () => {
    const r = await run(["wait", "ci", "--pr", "7", "--max", "30"], {
      routes: { "GET /pulls/7": pr(), [statuses]: [{ id: 1, context: "t", status: "success" }] },
      onFetch: (key) => (key === statuses ? 30 : 0),
    });
    assert.equal(r.code, EXIT.pending);
    assert.equal(r.out.waited_s, 30);
  });

  it("ends a multi-page probe's wait on a steer: exit 11, marker cleared", async () => {
    const cwd = repo();
    mkdirSync(signals(cwd), { recursive: true });
    const marker = join(signals(cwd), "interrupt");
    const r = await run(["wait", "review", "--pr", "7", "--since", iso(0), "--max", "100"], {
      cwd,
      routes: {
        "GET /issues/7/comments": () => link([]),
        "GET /pulls/7/reviews": () => link([]),
      },
      onFetch: (_, n, s) => {
        if (n === 2) writeFileSync(marker, JSON.stringify({ reason: "steer", at: iso(s + 1) }));
        return 2;
      },
    });
    assert.equal(r.code, EXIT.interrupted);
    assert.equal(existsSync(join(signals(cwd), "waiting.json")), false);
  });

  it("prints the pending result inside a caller's 115 s timeout by default", async () => {
    const r = await run(["wait", "ci", "--pr", "7"], {
      routes: { "GET /pulls/7": pr(), [statuses]: [pending(1)] },
    });
    assert.equal(r.code, EXIT.pending);
    assert.equal(r.elapsed, 90);
    assert.ok(r.out.status);
  });

  it("keeps the --max and ARGUS_TASK_WAIT_MAX_S clamp as the deadline", async () => {
    const routes = { "GET /pulls/7": pr(), [statuses]: [pending(1)] };
    const env = { ARGUS_TASK_WAIT_MAX_S: "45" };
    assert.equal(
      (await run(["wait", "ci", "--pr", "7", "--max", "600"], { routes, env })).elapsed,
      45,
    );
    assert.equal(
      (await run(["wait", "ci", "--pr", "7", "--max", "35"], { routes, env })).elapsed,
      35,
    );
  });
});

describe("wait review", () => {
  it("returns new comments and reviews after the cursor, with the next cursor", async () => {
    const r = await run(["wait", "review", "--pr", "7", "--since", iso(0)], {
      routes: {
        "GET /issues/7/comments": (url) => {
          assert.equal(url.searchParams.get("since"), iso(0));
          return [
            { id: 1, body: "old", user: { login: "a" }, created_at: iso(-60) },
            {
              id: 2,
              body: "Agentic review: 2 findings",
              user: { login: "bot" },
              created_at: iso(5),
            },
          ];
        },
        "GET /pulls/7/reviews": [
          { id: 9, state: "REQUEST_CHANGES", user: { login: "b" }, submitted_at: iso(8) },
        ],
      },
    });
    assert.equal(r.code, EXIT.ok);
    assert.deepEqual(
      r.out.items.map((i) => i.kind),
      ["agentic_review", "review"],
    );
    assert.equal(r.out.cursor, iso(8));
  });
});

describe("pr-ready", () => {
  const routes = (overrides = {}) => ({
    "GET /pulls/7": pr({ mergeable: true }),
    [`GET /statuses/${HEAD}`]: [{ id: 1, context: "test", status: "success" }],
    "GET /issues/7/comments": [
      { id: 3, body: `Agentic review for head ${HEAD}: 0 findings`, created_at: iso(0) },
    ],
    [`GET /commits/${HEAD}/status`]: { state: "success" },
    [`GET /commits/${HEAD}/statuses`]: [],
    "GET /pulls/7/reviews": [
      { id: 1, state: "REQUEST_CHANGES", user: { login: "b" }, submitted_at: iso(0) },
      { id: 2, state: "APPROVED", user: { login: "b" }, submitted_at: iso(10) },
    ],
    ...overrides,
  });

  for (const [state, reason] of [
    ["pending", "ci_running"],
    ["failure", "ci_failed"],
    ["bogus", "ci_unknown"],
    [undefined, "ci_unknown"],
    ["success", null],
    ["skipped", null],
  ]) {
    it(`includes agentic-review ${state ?? "missing state"} with review policy off`, async () => {
      const r = await run(["pr-ready", "--pr", "7", "--skip-review"], {
        env: { ARGUS_FORGE_REVIEW: "off" },
        routes: routes({
          [`GET /statuses/${HEAD}`]: [
            { id: 1, context: "test", status: "success" },
            { id: 2, context: "agentic-review", status: state },
          ],
        }),
      });
      assert.equal(r.code, reason ? EXIT.not_ready : EXIT.ok, r.err);
      assert.deepEqual(r.out.reasons, reason ? [reason] : []);
    });
  }

  it("is ready when CI is green, the review covers the head, and no change request stands", async () => {
    const r = await run(["pr-ready", "--pr", "7"], { routes: routes() });
    assert.equal(r.code, EXIT.ok, r.err);
    assert.deepEqual(r.out.reasons, []);
  });

  for (const [name, status, reasons] of [
    ["an unrecognized state", { status: "bogus" }, ["ci_unknown"]],
    ["a missing state", {}, ["ci_unknown"]],
    ["skipped", { status: "skipped" }, []],
    ["neutral", { status: "neutral" }, []],
  ])
    it(`pr-ready with ${name} gives ${JSON.stringify(reasons)}`, async () => {
      const r = await run(["pr-ready", "--pr", "7"], {
        routes: routes({ [`GET /statuses/${HEAD}`]: [{ id: 1, context: "test", ...status }] }),
      });
      assert.equal(r.code, reasons.length ? EXIT.not_ready : EXIT.ok, r.err);
      assert.deepEqual(r.out.reasons, reasons);
    });

  it("is not ready when the head moved between its reads", async () => {
    let reads = 0;
    const moved = "b".repeat(40);
    const r = await run(["pr-ready", "--pr", "7"], {
      routes: routes({
        "GET /pulls/7": () => pr({ mergeable: true, head: { sha: ++reads === 1 ? HEAD : moved } }),
        "GET /issues/7/comments": [
          { id: 3, body: `Agentic review for head ${moved}: 0 findings`, created_at: iso(0) },
        ],
        [`GET /commits/${moved}/status`]: { state: "success" },
        [`GET /commits/${moved}/statuses`]: [],
      }),
    });
    assert.equal(r.code, EXIT.not_ready);
    assert.deepEqual(r.out.reasons, ["head_moved"]);
  });

  it("is not ready when the head moves while reviews load", async () => {
    const moved = "b".repeat(40);
    for (const extra of [[], ["--skip-review"]]) {
      let flipped = false;
      const r = await run(["pr-ready", "--pr", "7", ...extra], {
        routes: routes({
          "GET /pulls/7": () => pr({ mergeable: true, head: { sha: flipped ? moved : HEAD } }),
          "GET /pulls/7/reviews": () => {
            flipped = true;
            return [];
          },
          [`GET /statuses/${moved}`]: [{ id: 1, context: "test", status: "success" }],
          [`GET /commits/${moved}/status`]: { state: "success" },
          [`GET /commits/${moved}/statuses`]: [],
        }),
      });
      assert.equal(r.code, EXIT.not_ready, extra.join(" "));
      assert.deepEqual(r.out.reasons, ["head_moved"]);
    }
  });

  it("names the stable head when ready, also with --skip-review", async () => {
    for (const extra of [[], ["--skip-review"]]) {
      const r = await run(["pr-ready", "--pr", "7", ...extra], { routes: routes() });
      assert.equal(r.code, EXIT.ok, r.err);
      assert.equal(r.out.ready, true);
      assert.equal(r.out.head_sha, HEAD);
    }
  });

  it("fails closed when the final head read fails", async () => {
    for (const extra of [[], ["--skip-review"]]) {
      let loaded = false;
      const r = await run(["pr-ready", "--pr", "7", ...extra], {
        routes: routes({
          "GET /pulls/7": () =>
            loaded ? new Response("{}", { status: 500 }) : pr({ mergeable: true }),
          "GET /pulls/7/reviews": () => {
            loaded = true;
            return [];
          },
        }),
      });
      assert.notEqual(r.code, EXIT.ok, extra.join(" "));
      assert.notEqual(r.out?.ready, true);
      assert.notEqual(r.err, "");
    }
  });

  it("lists every reason it is not ready", async () => {
    const r = await run(["pr-ready", "--pr", "7"], {
      routes: routes({
        "GET /pulls/7": pr({ mergeable: false }),
        [`GET /statuses/${HEAD}`]: [{ id: 1, context: "test", status: "failure" }],
        "GET /issues/7/comments": [
          { id: 3, body: `Agentic review for head ${HEAD}: 2 findings`, created_at: iso(0) },
        ],
        "GET /pulls/7/reviews": [
          { id: 1, state: "CHANGES_REQUESTED", user: { login: "c" }, submitted_at: iso(0) },
        ],
      }),
    });
    assert.equal(r.code, EXIT.not_ready);
    assert.deepEqual(r.out.reasons, [
      "ci_failed",
      "review_findings",
      "changes_requested",
      "not_mergeable",
    ]);
    assert.deepEqual(r.out.changes_requested_by, ["c"]);
  });

  it("reports a missing review and missing CI, unless review is skipped", async () => {
    const bare = routes({ [`GET /statuses/${HEAD}`]: [], "GET /issues/7/comments": [] });
    const r = await run(["pr-ready", "--pr", "7"], { routes: bare });
    assert.deepEqual(r.out.reasons, ["ci_missing", "review_missing"]);
    const skipped = await run(["pr-ready", "--pr", "7", "--skip-review"], { routes: bare });
    assert.deepEqual(skipped.out.reasons, ["ci_missing"]);
  });
});

describe("pr-ready review policy (ARGUS_FORGE_REVIEW)", () => {
  const base = (overrides = {}) => ({
    "GET /pulls/7": pr({ mergeable: true }),
    [`GET /statuses/${HEAD}`]: [{ id: 1, context: "test", status: "success" }],
    "GET /issues/7/comments": [],
    [`GET /commits/${HEAD}/status`]: { state: "success" },
    [`GET /commits/${HEAD}/statuses`]: [],
    "GET /pulls/7/reviews": [],
    ...overrides,
  });
  const agentic = (n) => [
    { id: 3, body: `Agentic review for head ${HEAD}: ${n} findings`, created_at: iso(0) },
  ];
  const ready = (policy, extra = [], routes = base()) =>
    run(["pr-ready", "--pr", "7", ...extra], {
      routes,
      env: policy === undefined ? {} : { ARGUS_FORGE_REVIEW: policy },
    });

  it("off: a missing review is not a reason, with or without --skip-review", async () => {
    for (const extra of [[], ["--skip-review"]]) {
      const r = await ready("off", extra);
      assert.equal(r.code, EXIT.ok, r.err);
      assert.deepEqual(r.out.reasons, []);
      assert.equal(r.out.review_policy, "off");
    }
  });

  it("off: an agentic review that exists still blocks, also against --skip-review", async () => {
    for (const extra of [[], ["--skip-review"]]) {
      const r = await ready("off", extra, base({ "GET /issues/7/comments": agentic(2) }));
      assert.equal(r.code, EXIT.not_ready);
      assert.deepEqual(r.out.reasons, ["review_findings"]);
    }
    const clean = await ready("off", [], base({ "GET /issues/7/comments": agentic(0) }));
    assert.equal(clean.code, EXIT.ok);
  });

  it("off: changes requested, CI failure, unknown CI and a moved head still block", async () => {
    const changes = await ready(
      "off",
      [],
      base({
        "GET /pulls/7/reviews": [
          { id: 1, state: "CHANGES_REQUESTED", user: { login: "c" }, submitted_at: iso(0) },
        ],
      }),
    );
    assert.deepEqual(changes.out.reasons, ["changes_requested"]);
    for (const [status, reason] of [
      ["failure", "ci_failed"],
      ["bogus", "ci_unknown"],
    ]) {
      const r = await ready(
        "off",
        [],
        base({ [`GET /statuses/${HEAD}`]: [{ id: 1, context: "test", status }] }),
      );
      assert.deepEqual(r.out.reasons, [reason]);
    }
    const moved = "b".repeat(40);
    let reads = 0;
    const stale = await ready(
      "off",
      [],
      base({
        "GET /pulls/7": () => pr({ mergeable: true, head: { sha: ++reads < 3 ? HEAD : moved } }),
      }),
    );
    assert.deepEqual(stale.out.reasons, ["head_moved"]);
  });

  it("off: a review for an older head is not a finding on this head", async () => {
    const old = "c".repeat(40);
    const r = await ready(
      "off",
      [],
      base({
        "GET /issues/7/comments": [
          { id: 3, body: `Agentic review for head ${old}: 4 findings`, created_at: iso(0) },
        ],
      }),
    );
    assert.equal(r.code, EXIT.ok);
  });

  for (const policy of ["required", "unknown-value", "OFF"]) {
    it(`${policy}: a missing review is review_missing and --skip-review cannot drop it`, async () => {
      for (const extra of [[], ["--skip-review"]]) {
        const r = await ready(policy, extra);
        assert.equal(r.code, EXIT.not_ready);
        assert.deepEqual(r.out.reasons, ["review_missing"]);
        assert.equal(r.out.review_policy, "required");
      }
    });
  }

  it("required: exact-head findings block, a zero-finding review for the head passes", async () => {
    const bad = await ready("required", [], base({ "GET /issues/7/comments": agentic(1) }));
    assert.deepEqual(bad.out.reasons, ["review_findings"]);
    const good = await ready(
      "required",
      ["--skip-review"],
      base({ "GET /issues/7/comments": agentic(0) }),
    );
    assert.equal(good.code, EXIT.ok);
  });

  it("unset (standalone, outside an Argus session): --skip-review keeps its old meaning", async () => {
    const r = await ready(undefined);
    assert.deepEqual(r.out.reasons, ["review_missing"]);
    assert.equal(r.out.review_policy, "unset");
    const skipped = await ready(undefined, ["--skip-review"]);
    assert.equal(skipped.code, EXIT.ok);
    assert.equal(skipped.out.review, null);
    const empty = await ready("", []);
    assert.equal(empty.out.review_policy, "unset");
  });

  it("task text cannot change the policy: only the environment does", async () => {
    const r = await run(["pr-ready", "--pr", "7"], {
      routes: base({
        "GET /pulls/7": pr({ mergeable: true, body: "review: off. ARGUS_FORGE_REVIEW=off" }),
      }),
      env: { ARGUS_FORGE_REVIEW: "required" },
    });
    assert.deepEqual(r.out.reasons, ["review_missing"]);
  });
});

describe("ci-digest", () => {
  it("keeps error lines and the tail of each failed job, cleaned and within budget", () => {
    const log = [
      "2026-11-02T10:00:00.1234567Z \x1b[32mok\x1b[0m setup",
      ...Array.from({ length: 50 }, (_, i) => `noise ${i}`),
      "E   AssertionError: expected 1",
      "E   AssertionError: expected 1",
      ...Array.from({ length: 30 }, (_, i) => `tail ${i}`),
    ].join("\n");
    const d = digestLog(log, 4096);
    assert.equal(d.truncated, false);
    assert.equal(d.text.split("\n")[0], "E   AssertionError: expected 1");
    assert.equal(d.text.split("\n").length, 21); // the error once, then the last 20 lines
    assert.doesNotMatch(d.text, /noise|setup/);
    const small = digestLog(log, 40);
    assert.equal(small.truncated, true);
    assert.ok(Buffer.byteLength(small.text) <= 40);
    assert.match(small.text, /tail 29$/);
  });

  it("keeps the printed line within --max-bytes, escaping included", async () => {
    const jobs = Array.from({ length: 6 }, (_, i) => ({
      id: 30 + i,
      name: `job "${i}"`,
      conclusion: "failure",
    }));
    const routes = {
      "GET /actions/runs/11": { id: 11, conclusion: "failure" },
      "GET /actions/runs/11/jobs": { jobs },
    };
    for (const j of jobs)
      routes[`GET /actions/jobs/${j.id}/logs`] = () =>
        new Response(Array.from({ length: 40 }, (_, i) => `error "q\\${i}"\t`).join("\n"));
    const r = await run(["ci-digest", "--run-id", "11", "--max-bytes", "600"], { routes });
    assert.equal(r.code, EXIT.ok, r.err);
    assert.ok(Buffer.byteLength(r.raw) - 1 <= 600, `${Buffer.byteLength(r.raw)} bytes`);
    assert.equal(r.out.truncated, true);
    assert.ok(r.out.excerpts.length >= 1 && r.out.excerpts.length < jobs.length);
  });

  it("names the failed run, job and step for the PR head", async () => {
    const r = await run(["ci-digest", "--pr", "7"], {
      routes: {
        "GET /pulls/7": pr(),
        "GET /actions/runs": (url) => {
          assert.equal(url.searchParams.get("head_sha"), HEAD);
          return {
            workflow_runs: [
              { id: 11, head_sha: HEAD, conclusion: "failure" },
              { id: 12, head_sha: HEAD, conclusion: "success" },
              { id: 13, head_sha: NEW, conclusion: "failure" },
            ],
          };
        },
        "GET /actions/runs/11/jobs": {
          jobs: [
            {
              id: 21,
              name: "test",
              conclusion: "failure",
              steps: [
                { name: "setup", conclusion: "success" },
                { name: "pytest", conclusion: "failure" },
              ],
            },
            { id: 22, name: "lint", conclusion: "success" },
          ],
        },
        "GET /actions/runs/13/jobs": () => assert.fail("a run of another head was read"),
        "GET /actions/jobs/21/logs": () =>
          new Response("FAILED tests/test_x.py::test_y password=hunter2\x1b[0m\ndone\n"),
      },
    });
    assert.equal(r.code, EXIT.ok, r.err);
    assert.deepEqual(r.out.excerpts, [
      {
        run_id: 11,
        job_id: 21,
        job: "test",
        step: "pytest",
        text: "FAILED tests/test_x.py::test_y password=[REDACTED]\ndone",
      },
    ]);
    assert.equal(r.out.failed_runs, 1);
    // --sha reads that exact commit without the PR; a short sha is refused.
    const bySha = await run(["ci-digest", "--sha", NEW], {
      routes: {
        "GET /actions/runs": (url) => {
          assert.equal(url.searchParams.get("head_sha"), NEW);
          return { workflow_runs: [{ id: 13, head_sha: NEW, conclusion: "failure" }] };
        },
        "GET /actions/runs/13/jobs": { jobs: [] },
      },
    });
    assert.deepEqual([bySha.code, bySha.out.head_sha, bySha.out.failed_runs], [EXIT.ok, NEW, 1]);
    assert.equal((await run(["ci-digest", "--sha", "abc1234"])).code, EXIT.usage);
    assert.equal((await run(["ci-digest", "--sha", NEW, "--pr", "7"])).code, EXIT.usage);
  });
});

describe("deliver", () => {
  /** origin (bare) with main, and a clone on branch `feature` one commit ahead. */
  // pathScript: whether the repository carries scripts/check_paths.py (Argus does, Radar does not).
  function setup({ pathScript = true } = {}) {
    const origin = join(scratch, `o${repoCount++}.git`);
    git(["init", "-q", "--bare", "-b", "main", origin]);
    const seed = repo();
    identity(seed);
    writeFileSync(join(seed, "a.txt"), "one\n");
    if (pathScript) {
      mkdirSync(join(seed, "scripts"));
      writeFileSync(
        join(seed, "scripts", "check_paths.py"),
        "# fake; the fixture's python3 stands in\n",
      );
    }
    git(["add", "."], seed);
    git(["commit", "-qm", "seed"], seed);
    git(["push", "-q", origin, "main"], seed);
    const work = join(scratch, `w${repoCount++}`);
    git(["clone", "-q", origin, work]);
    identity(work);
    git(["checkout", "-qb", "feature"], work);
    return { origin, seed, work };
  }
  const advance = (seed, origin, file, text) => {
    writeFileSync(join(seed, file), text);
    git(["commit", "-qam", `edit ${file}`], seed);
    git(["push", "-q", origin, "main"], seed);
  };
  const forgeRoutes = (created) => ({
    "GET /": { default_branch: "main" },
    "GET /pulls": [],
    "POST /pulls": (_, body) => {
      created.push(body);
      return { number: 8, html_url: `${REPO}/pulls/8` };
    },
  });

  /**
   * Fake `make` and `gitleaks` on a PATH of their own. Each call and each push is appended to
   * one log in order; `make` logs the HEAD it ran on and fails for the target `fail` names.
   */
  function gateFixture({ fail = "", output = "", scanner = true } = {}) {
    const bin = join(scratch, `bin${repoCount++}`);
    const log = join(scratch, `gate${repoCount++}.log`);
    mkdirSync(bin);
    const script = (name, body) => {
      writeFileSync(join(bin, name), `#!/bin/sh\n${body}`);
      chmodSync(join(bin, name), 0o755);
    };
    script(
      "make",
      `echo "make $*" >> "$ARGUS_TEST_LOG"\n` +
        `echo "head $("$ARGUS_TEST_GIT" rev-parse HEAD)" >> "$ARGUS_TEST_LOG"\n` +
        `if [ "$*" = "$ARGUS_TEST_FAIL" ]; then echo "$* finding" >&2; printf '%s\\n' "$ARGUS_TEST_FAIL_OUTPUT" >&2; exit 2; fi\nexit 0\n`,
    );
    // The path check prints its JSON list on stdout, as scripts/check_paths.py --json does.
    script(
      "python3",
      `echo "python3 $*" >> "$ARGUS_TEST_LOG"\n` +
        `if [ "$ARGUS_TEST_FAIL" = "check-paths" ]; then\n` +
        `echo '[{"path":"pr-body.txt","rule":"root-file","action":"pr-body.txt is a stray file: keep it under /tmp"}]'\n` +
        `exit 1\nfi\n` +
        `if [ "$ARGUS_TEST_FAIL" = "check-paths-crash" ]; then echo "Traceback: boom" >&2; exit 1; fi\nexit 0\n`,
    );
    if (scanner) script("gitleaks", `echo "gitleaks $*" >> "$ARGUS_TEST_LOG"\nexit 0\n`);
    const options = {
      env: {
        PATH: bin,
        ARGUS_TEST_LOG: log,
        ARGUS_TEST_GIT: gitBin,
        ARGUS_TEST_FAIL: fail,
        ARGUS_TEST_FAIL_OUTPUT: output,
      },
      // A push is logged with its arguments, so ordering against the checks is visible.
      git: (args, cwd) => {
        if (args[0] === "push") appendFileSync(log, `push ${args.slice(1).join(" ")}\n`);
        return git(args, cwd);
      },
    };
    return {
      options,
      lines: () => (existsSync(log) ? readFileSync(log, "utf8").trimEnd().split("\n") : []),
    };
  }

  it("rebases cleanly, pushes and opens the PR", async () => {
    const { origin, seed, work } = setup();
    writeFileSync(join(work, "b.txt"), "new\n");
    git(["add", "."], work);
    git(["commit", "-qm", "Add b"], work);
    writeFileSync(join(seed, "c.txt"), "c\n");
    git(["add", "."], seed);
    git(["commit", "-qm", "Add c"], seed);
    git(["push", "-q", origin, "main"], seed);
    const created = [];
    const g = gateFixture();
    const r = await run(["deliver"], { routes: forgeRoutes(created), cwd: work, ...g.options });
    assert.equal(r.code, EXIT.ok, r.err);
    assert.equal(r.out.rebased, true);
    assert.equal(r.out.created, true);
    assert.equal(r.out.number, 8);
    assert.deepEqual(created, [{ title: "Add b", head: "feature", base: "main", body: "" }]);
    assert.equal(git(["rev-parse", "refs/heads/feature"], origin), r.out.head_sha);
    git(["merge-base", "--is-ancestor", "main", "feature"], origin);
  });

  it("stops on a rebase conflict and leaves the branch as it was", async () => {
    const { origin, seed, work } = setup();
    writeFileSync(join(work, "a.txt"), "mine\n");
    git(["commit", "-qam", "mine"], work);
    const before = git(["rev-parse", "HEAD"], work);
    advance(seed, origin, "a.txt", "theirs\n");
    const created = [];
    const r = await run(["deliver"], { routes: forgeRoutes(created), cwd: work });
    assert.equal(r.code, EXIT.not_ready);
    assert.deepEqual(r.out.reasons, ["rebase_conflict"]);
    assert.deepEqual(r.out.conflicts, ["a.txt"]);
    assert.equal(git(["rev-parse", "HEAD"], work), before);
    assert.equal(git(["status", "--porcelain"], work), "");
    assert.deepEqual(created, []);
  });

  /** A clone one commit ahead whose base has moved, so deliver needs a rebase. */
  function behind(file = "b.txt") {
    const s = setup();
    writeFileSync(join(s.work, file), "mine\n");
    git(["add", "."], s.work);
    git(["commit", "-qm", "mine"], s.work);
    advance(s.seed, s.origin, "a.txt", "theirs\n");
    return s;
  }
  const refuses = async ({ origin, work }, code) => {
    const remote = git(["for-each-ref"], origin);
    const created = [];
    const r = await run(["deliver"], { routes: forgeRoutes(created), cwd: work });
    assert.equal(r.code, EXIT.error);
    assert.equal(JSON.parse(r.err).error.code, code);
    assert.equal(git(["for-each-ref"], origin), remote);
    assert.deepEqual(created, []);
    assert.deepEqual(
      r.calls.filter((c) => c.startsWith("POST") || c.startsWith("PATCH")),
      [],
    );
    return r;
  };

  it("refuses a dirty worktree that needs a rebase and keeps the WIP", async () => {
    const s = behind();
    writeFileSync(join(s.work, "a.txt"), "wip\n");
    const status = git(["status", "--porcelain"], s.work);
    await refuses(s, "dirty_worktree");
    assert.equal(readFileSync(join(s.work, "a.txt"), "utf8"), "wip\n");
    assert.equal(git(["status", "--porcelain"], s.work), status);
    assert.equal(existsSync(join(s.work, ".git", "rebase-merge")), false);
  });

  it("says which sync it did: rebased onto the fetched base, or already up to date", async () => {
    const s = behind();
    const r = await run(["deliver"], {
      routes: forgeRoutes([]),
      cwd: s.work,
      ...gateFixture().options,
    });
    assert.equal(r.code, EXIT.ok, r.err);
    assert.equal(r.out.sync, "rebased the branch onto origin/main");
    const t = setup();
    writeFileSync(join(t.work, "b.txt"), "new\n");
    git(["add", "."], t.work);
    git(["commit", "-qm", "Add b"], t.work);
    const again = await run(["deliver"], {
      routes: forgeRoutes([]),
      cwd: t.work,
      ...gateFixture().options,
    });
    assert.equal(again.out.sync, "branch already contains origin/main; no rebase needed");
  });

  it("gives one next command for a detached HEAD, an unborn branch and a dirty tree", async () => {
    const detached = setup();
    git(["checkout", "-q", "--detach"], detached.work);
    const d = await refuses(detached, "detached_head");
    assert.match(JSON.parse(d.err).error.action, /^run `git switch -c <branch-name>`/);

    const unborn = setup();
    git(["checkout", "-q", "--orphan", "fresh"], unborn.work);
    git(["rm", "-rqf", "."], unborn.work);
    const u = await refuses(unborn, "unborn_branch");
    assert.match(JSON.parse(u.err).error.message, /no commits yet/);
    assert.match(JSON.parse(u.err).error.action, /git commit/);

    const dirty = setup();
    writeFileSync(join(dirty.work, "a.txt"), "wip\n");
    const w = await refuses(dirty, "dirty_worktree");
    assert.match(JSON.parse(w.err).error.action, /git add -A && git commit/);
  });

  it("names the abort command for an operation in progress and carries it as action", async () => {
    const s = behind("a.txt");
    git(["fetch", "-q", "origin", "main"], s.work);
    assert.throws(() => git(["rebase", "FETCH_HEAD"], s.work));
    const r = await refuses(s, "git_operation_in_progress");
    const { error } = JSON.parse(r.err);
    assert.match(error.message, /`git rebase --abort`/);
    assert.match(error.action, /git rebase --abort/);
  });

  describe("a reviewer's fix (input.fix)", () => {
    /** origin holds the PR branch `feature` at `prHead`; the clone adds one fix commit on top. */
    function fixSetup() {
      const s = setup();
      writeFileSync(join(s.work, "impl.txt"), "impl\n");
      git(["add", "."], s.work);
      git(["commit", "-qm", "impl"], s.work);
      git(["push", "-q", "origin", "feature"], s.work);
      const prHead = git(["rev-parse", "HEAD"], s.work);
      writeFileSync(join(s.work, "fix.txt"), "fix\n");
      git(["add", "."], s.work);
      git(["commit", "-qm", "fix"], s.work);
      return { ...s, prHead };
    }
    const scope = (prHead, over = {}) => ({
      ARGUS_FIX_PR: "7",
      ARGUS_FIX_BASE_SHA: prHead,
      ...over,
    });
    const forge7 = (live, ref = "feature", created = []) => ({
      ...forgeRoutes(created),
      "GET /pulls/7": { number: 7, state: "open", head: { ref, sha: live }, html_url: `${REPO}/pulls/7` },
    });
    const refused = async (s, code, routes, env) => {
      const before = git(["for-each-ref"], s.origin);
      const g = gateFixture();
      const r = await run(["deliver"], {
        routes,
        cwd: s.work,
        ...g.options,
        env: { ...g.options.env, ...env },
      });
      assert.equal(r.code, EXIT.error);
      assert.equal(JSON.parse(r.err).error.code, code);
      assert.equal(git(["for-each-ref"], s.origin), before);
      assert.equal(g.lines().filter((l) => l.startsWith("push")).length, 0);
    };

    it("fast-forwards the PR branch without rebase, force or PR edits", async () => {
      const s = fixSetup();
      advance(s.seed, s.origin, "a.txt", "base moved\n"); // a rebase would be needed in normal deliver
      const head = git(["rev-parse", "HEAD"], s.work);
      const created = [];
      const g = gateFixture();
      const r = await run(["deliver"], {
        routes: forge7(s.prHead, "feature", created),
        cwd: s.work,
        ...g.options,
        env: { ...g.options.env, ...scope(s.prHead) },
      });
      assert.equal(r.code, EXIT.ok, r.err);
      assert.equal(r.out.fix, true);
      assert.equal(r.out.rebased, false);
      assert.equal(r.out.head_sha, head);
      assert.equal(r.out.base_head_sha, s.prHead);
      assert.equal(git(["rev-parse", "refs/heads/feature"], s.origin), head);
      assert.equal(git(["rev-parse", "HEAD"], s.work), head);
      assert.deepEqual(created, []);
      assert.deepEqual(
        g.lines().filter((l) => l.startsWith("push")),
        ["push --quiet origin HEAD:refs/heads/feature"],
      );
    });

    it("refuses another branch than the PR's own", async () => {
      const s = fixSetup();
      await refused(s, "fix_branch_mismatch", forge7(s.prHead, "other"), scope(s.prHead));
    });

    it("refuses when HEAD does not contain the live PR head (never rebases or forces)", async () => {
      const s = fixSetup();
      const other = join(scratch, `x${repoCount++}`);
      git(["clone", "-q", s.origin, other]);
      identity(other);
      git(["checkout", "-q", "feature"], other);
      writeFileSync(join(other, "late.txt"), "late\n");
      git(["add", "."], other);
      git(["commit", "-qm", "late"], other);
      git(["push", "-q", "origin", "feature"], other);
      const live = git(["rev-parse", "HEAD"], other);
      await refused(s, "fix_not_fast_forward", forge7(live), scope(s.prHead));
    });

    it("refuses when the PR moved off the reviewed head", async () => {
      const s = fixSetup();
      const other = join(scratch, `x${repoCount++}`);
      git(["clone", "-q", s.origin, other]);
      identity(other);
      git(["checkout", "-q", "-B", "feature", "origin/main"], other);
      writeFileSync(join(other, "z.txt"), "z\n");
      git(["add", "."], other);
      git(["commit", "-qm", "rewrite"], other);
      git(["push", "-q", "--force", "origin", "feature"], other);
      const live = git(["rev-parse", "HEAD"], other);
      await refused(s, "fix_base_moved", forge7(live), scope(s.prHead));
    });

    it("refuses with no new commit, and a malformed or partial scope", async () => {
      const s = fixSetup();
      git(["reset", "-q", "--hard", s.prHead], s.work);
      await refused(s, "fix_no_commit", forge7(s.prHead), scope(s.prHead));
      const t = fixSetup();
      await refused(t, "fix_scope_invalid", forge7(t.prHead), {
        ARGUS_FIX_PR: "invalid",
        ARGUS_FIX_BASE_SHA: "invalid",
      });
      await refused(t, "fix_scope_invalid", forge7(t.prHead), { ARGUS_FIX_PR: "7" });
    });
  });

  it("a rebase conflict result carries an action", async () => {
    const { origin, seed, work } = setup();
    writeFileSync(join(work, "a.txt"), "mine\n");
    git(["commit", "-qam", "mine"], work);
    advance(seed, origin, "a.txt", "theirs\n");
    const r = await run(["deliver"], { routes: forgeRoutes([]), cwd: work });
    assert.match(r.out.action, /git rebase --continue/);
  });

  it("refuses a rebase already in progress without touching it", async () => {
    const s = behind("a.txt");
    git(["fetch", "-q", "origin", "main"], s.work);
    assert.throws(() => git(["rebase", "FETCH_HEAD"], s.work));
    const status = git(["status", "--porcelain"], s.work);
    const index = readFileSync(join(s.work, ".git", "index"));
    const r = await refuses(s, "git_operation_in_progress");
    assert.match(JSON.parse(r.err).error.message, /rebase/);
    assert.equal(existsSync(join(s.work, ".git", "rebase-merge")), true);
    assert.equal(git(["status", "--porcelain"], s.work), status);
    assert.deepEqual(readFileSync(join(s.work, ".git", "index")), index);
  });

  it("refuses a merge already in progress without touching it", async () => {
    const s = setup();
    git(["checkout", "-q", "-b", "other", "origin/main"], s.work);
    writeFileSync(join(s.work, "o.txt"), "o\n");
    git(["add", "."], s.work);
    git(["commit", "-qm", "other"], s.work);
    git(["checkout", "-q", "feature"], s.work);
    git(["merge", "--no-commit", "--no-ff", "other"], s.work);
    const status = git(["status", "--porcelain"], s.work);
    const r = await refuses(s, "git_operation_in_progress");
    assert.match(JSON.parse(r.err).error.message, /merge/);
    assert.equal(git(["status", "--porcelain"], s.work), status);
    assert.equal(existsSync(join(s.work, ".git", "MERGE_HEAD")), true);
  });

  it("reports a rebase that fails without state as a bounded git error", async () => {
    const s = behind();
    // The base adds a file that exists untracked here: the checkout refuses before any state.
    writeFileSync(join(s.seed, "u.txt"), "theirs\n");
    git(["add", "."], s.seed);
    git(["commit", "-qm", "Add u"], s.seed);
    git(["push", "-q", s.origin, "main"], s.seed);
    writeFileSync(join(s.work, "u.txt"), "untracked\n");
    const head = git(["rev-parse", "HEAD"], s.work);
    const r = await refuses(s, "git_failed");
    assert.match(JSON.parse(r.err).error.message, /^git rebase failed \(exit \d+\)/);
    assert.equal(git(["rev-parse", "HEAD"], s.work), head);
    assert.equal(readFileSync(join(s.work, "u.txt"), "utf8"), "untracked\n");
    assert.equal(existsSync(join(s.work, ".git", "rebase-merge")), false);
  });

  it("treats a failing is-ancestor check as an operational error", async () => {
    const { origin, work } = setup();
    const calls = [];
    const created = [];
    const f = forge(forgeRoutes(created));
    const code = await main(["deliver"], ENV, {
      fetch: f.fetch,
      cwd: work,
      stdout: { write: () => {} },
      stderr: { write: () => {} },
      git: (args, cwd) => {
        calls.push(args[0]);
        if (args[0] === "merge-base") throw Object.assign(new Error("boom"), { status: 128 });
        return git(args, cwd);
      },
    });
    assert.equal(code, EXIT.error);
    assert.equal(calls.includes("rebase"), false);
    assert.equal(calls.includes("push"), false);
    assert.equal(git(["for-each-ref", "refs/heads/feature"], origin), "");
  });

  it("bounds the stderr it relays", async () => {
    const { work } = setup();
    const f = forge(forgeRoutes([]));
    const err = [];
    const code = await main(["deliver"], ENV, {
      fetch: f.fetch,
      cwd: work,
      stdout: { write: () => {} },
      stderr: { write: (s) => err.push(s) },
      git: (args, cwd) => {
        if (args[0] === "fetch")
          throw Object.assign(new Error("x"), {
            status: 128,
            stderr: `${"fatal line of stderr text. ".repeat(300)}\n\n`,
          });
        return git(args, cwd);
      },
    });
    assert.equal(code, EXIT.error);
    const { code: id, message } = JSON.parse(err.join("")).error;
    assert.equal(id, "git_failed");
    assert.match(message, /^git fetch failed \(exit 128\): fatal line of stderr text\. /);
    assert.ok(message.length <= 201);
  });

  it("runs the checks on the commit it pushes, before the push", async () => {
    const { origin, work } = setup();
    writeFileSync(join(work, "b.txt"), "new\n");
    git(["add", "."], work);
    git(["commit", "-qm", "Add b"], work);
    const g = gateFixture();
    const r = await run(["deliver"], { routes: forgeRoutes([]), cwd: work, ...g.options });
    assert.equal(r.code, EXIT.ok, r.err);
    const head = git(["rev-parse", "HEAD"], work);
    assert.equal(r.out.head_sha, head);
    assert.deepEqual(g.lines(), [
      "python3 scripts/check_paths.py --json",
      "make check-changed",
      `head ${head}`,
      "gitleaks version",
      "make secret-scan",
      `head ${head}`,
      "push --quiet origin HEAD:refs/heads/feature",
    ]);
    assert.equal(git(["rev-parse", "refs/heads/feature"], origin), head);
  });

  it("checks the rebased commit and pushes it with --force-with-lease", async () => {
    const s = behind();
    const g = gateFixture();
    const r = await run(["deliver"], { routes: forgeRoutes([]), cwd: s.work, ...g.options });
    assert.equal(r.code, EXIT.ok, r.err);
    assert.equal(r.out.rebased, true);
    const lines = g.lines();
    assert.equal(lines[2], `head ${r.out.head_sha}`);
    assert.equal(lines.at(-1), "push --quiet --force-with-lease origin HEAD:refs/heads/feature");
    assert.equal(git(["rev-parse", "refs/heads/feature"], s.origin), r.out.head_sha);
  });

  it("a failing check prevents every push and reports it", async () => {
    const { origin, work } = setup();
    writeFileSync(join(work, "b.txt"), "new\n");
    git(["add", "."], work);
    git(["commit", "-qm", "Add b"], work);
    const g = gateFixture({ fail: "check-changed" });
    const created = [];
    const r = await run(["deliver"], { routes: forgeRoutes(created), cwd: work, ...g.options });
    assert.equal(r.code, EXIT.not_ready);
    assert.deepEqual(r.out.reasons, ["pre_push_failed"]);
    assert.equal(r.out.check, "make check-changed");
    assert.equal(r.out.exit, 2);
    assert.match(r.out.output_tail, /check-changed finding/);
    assert.match(r.out.action, /run deliver again/);
    assert.deepEqual(
      g
        .lines()
        .filter((l) => l.startsWith("make") || l.startsWith("gitleaks") || l.startsWith("push")),
      ["make check-changed"],
    );
    assert.equal(git(["for-each-ref", "refs/heads/feature"], origin), "");
    assert.deepEqual(created, []);
  });

  const UV = "`uv sync --frozen --all-packages --group dev`";
  const PNPM = "`pnpm install --frozen-lockfile`";
  const OFFLINE =
    /then run `argus-task deliver` again; do not use --offline and do not skip the gate/;
  const SIGNATURE_ROWS = [
    ["a missing prometheus_client", "ModuleNotFoundError: No module named 'prometheus_client'", UV],
    ["a missing workspace package", "ModuleNotFoundError: No module named 'argus_core'", UV],
    ["a missing biome", "sh: 1: biome: not found", PNPM],
    ["a missing tsc", "sh: 1: tsc: not found", PNPM],
    ["a missing openapi-typescript", "sh: 1: openapi-typescript: not found", PNPM],
    ["a pnpm store miss", "ERR_PNPM_NO_OFFLINE_TARBALL No offline tarball", PNPM],
    ["a network-disabled install", "Network connectivity is disabled", `${UV} and ${PNPM}`],
    [
      "two signatures (the table order decides)",
      "sh: 1: tsc: not found\nNo module named 'prometheus_client'",
      UV,
    ],
  ];
  for (const [name, output, expected] of SIGNATURE_ROWS) {
    it(`the failed check names the sync for ${name}`, async () => {
      const { origin, work } = setup();
      writeFileSync(join(work, "b.txt"), "new\n");
      git(["add", "."], work);
      git(["commit", "-qm", "Add b"], work);
      const g = gateFixture({ fail: "check-changed", output });
      const created = [];
      const r = await run(["deliver"], { routes: forgeRoutes(created), cwd: work, ...g.options });
      assert.equal(r.code, EXIT.not_ready);
      assert.equal(r.out.check, "make check-changed");
      assert.equal(r.out.action.split(" from the workspace root, ")[0], `run ${expected}`);
      assert.match(r.out.action, OFFLINE);
      assert.equal(
        g.lines().some((l) => l.startsWith("push")),
        false,
      );
      assert.equal(git(["for-each-ref", "refs/heads/feature"], origin), "");
      assert.deepEqual(created, []);
    });
  }

  it("an unrelated check failure keeps the generic action", async () => {
    const { work } = setup();
    writeFileSync(join(work, "b.txt"), "new\n");
    git(["add", "."], work);
    git(["commit", "-qm", "Add b"], work);
    const g = gateFixture({ fail: "check-changed", output: "ruff: 3 errors in argus_api" });
    const r = await run(["deliver"], { routes: forgeRoutes([]), cwd: work, ...g.options });
    assert.equal(r.code, EXIT.not_ready);
    assert.equal(r.out.action, "fix the failing check, commit the fix, then run deliver again");
  });

  it("reports the path check not applicable without scripts/check_paths.py and runs every other gate", async () => {
    const { origin, work } = setup({ pathScript: false });
    writeFileSync(join(work, "b.txt"), "new\n");
    git(["add", "."], work);
    git(["commit", "-qm", "Add b"], work);
    const g = gateFixture();
    const r = await run(["deliver"], { routes: forgeRoutes([]), cwd: work, ...g.options });
    assert.equal(r.code, EXIT.ok, r.err);
    assert.deepEqual(r.out.checks_not_applicable, [
      { check: "path check", reason: "scripts/check_paths.py not found" },
    ]);
    const head = git(["rev-parse", "HEAD"], work);
    assert.deepEqual(g.lines(), [
      "make check-changed",
      `head ${head}`,
      "gitleaks version",
      "make secret-scan",
      `head ${head}`,
      "push --quiet origin HEAD:refs/heads/feature",
    ]);
    assert.notEqual(git(["for-each-ref", "refs/heads/feature"], origin), "");
  });

  for (const target of ["make check-changed", "make secret-scan"]) {
    it(`without the path script, a failing ${target} still blocks the push`, async () => {
      const { origin, work } = setup({ pathScript: false });
      writeFileSync(join(work, "b.txt"), "new\n");
      git(["add", "."], work);
      git(["commit", "-qm", "Add b"], work);
      const g = gateFixture({ fail: target.replace("make ", "") });
      const r = await run(["deliver"], { routes: forgeRoutes([]), cwd: work, ...g.options });
      assert.equal(r.code, EXIT.not_ready);
      assert.equal(r.out.check, target);
      assert.ok(!g.lines().some((l) => l.startsWith("push")));
      assert.equal(git(["for-each-ref", "refs/heads/feature"], origin), "");
    });
  }

  it("without the path script, a missing gitleaks still fails closed", async () => {
    const { origin, work } = setup({ pathScript: false });
    writeFileSync(join(work, "b.txt"), "new\n");
    git(["add", "."], work);
    git(["commit", "-qm", "Add b"], work);
    const g = gateFixture({ scanner: false });
    const r = await run(["deliver"], { routes: forgeRoutes([]), cwd: work, ...g.options });
    assert.notEqual(r.code, EXIT.ok);
    assert.equal(git(["for-each-ref", "refs/heads/feature"], origin), "");
  });

  it("with the path script, an execution failure without violations blocks the push", async () => {
    const { origin, work } = setup();
    writeFileSync(join(work, "b.txt"), "new\n");
    git(["add", "."], work);
    git(["commit", "-qm", "Add b"], work);
    const g = gateFixture({ fail: "check-paths-crash" });
    const r = await run(["deliver"], { routes: forgeRoutes([]), cwd: work, ...g.options });
    assert.equal(r.code, EXIT.not_ready);
    assert.equal(r.out.check, "path check");
    assert.equal(r.out.violations, undefined);
    assert.equal(git(["for-each-ref", "refs/heads/feature"], origin), "");
  });

  it("a path violation prevents every push and passes its action through", async () => {
    const { origin, work } = setup();
    writeFileSync(join(work, "pr-body.txt"), "body\n");
    git(["add", "."], work);
    git(["commit", "-qm", "Add pr-body"], work);
    const g = gateFixture({ fail: "check-paths" });
    const created = [];
    const r = await run(["deliver"], { routes: forgeRoutes(created), cwd: work, ...g.options });
    assert.equal(r.code, EXIT.not_ready);
    assert.deepEqual(r.out.reasons, ["pre_push_failed"]);
    assert.equal(r.out.check, "path check");
    assert.deepEqual(r.out.violations, [
      {
        path: "pr-body.txt",
        rule: "root-file",
        action: "pr-body.txt is a stray file: keep it under /tmp",
      },
    ]);
    assert.equal(r.out.action, "pr-body.txt is a stray file: keep it under /tmp");
    assert.deepEqual(g.lines(), ["python3 scripts/check_paths.py --json"]);
    assert.equal(git(["for-each-ref", "refs/heads/feature"], origin), "");
    assert.deepEqual(created, []);
  });

  it("a failing secret scan prevents the push after check-changed passed", async () => {
    const { origin, work } = setup();
    writeFileSync(join(work, "b.txt"), "new\n");
    git(["add", "."], work);
    git(["commit", "-qm", "Add b"], work);
    const g = gateFixture({ fail: "secret-scan" });
    const r = await run(["deliver"], { routes: forgeRoutes([]), cwd: work, ...g.options });
    assert.equal(r.code, EXIT.not_ready);
    assert.equal(r.out.check, "make secret-scan");
    assert.equal(
      g.lines().some((l) => l.startsWith("push")),
      false,
    );
    assert.equal(git(["for-each-ref", "refs/heads/feature"], origin), "");
  });

  it("refuses to push when gitleaks is not installed", async () => {
    const { origin, work } = setup();
    writeFileSync(join(work, "b.txt"), "new\n");
    git(["add", "."], work);
    git(["commit", "-qm", "Add b"], work);
    const g = gateFixture({ scanner: false });
    const created = [];
    const r = await run(["deliver"], { routes: forgeRoutes(created), cwd: work, ...g.options });
    assert.equal(r.code, EXIT.error);
    assert.equal(JSON.parse(r.err).error.code, "pre_push_unavailable");
    assert.match(JSON.parse(r.err).error.message, /gitleaks is not installed/);
    assert.equal(
      g.lines().some((l) => l.startsWith("push")),
      false,
    );
    assert.equal(git(["for-each-ref", "refs/heads/feature"], origin), "");
    assert.deepEqual(created, []);
  });

  it("refuses an untracked file before any check runs", async () => {
    const { origin, work } = setup();
    writeFileSync(join(work, "b.txt"), "new\n");
    git(["add", "."], work);
    git(["commit", "-qm", "Add b"], work);
    writeFileSync(join(work, "scratch.txt"), "wip\n");
    const g = gateFixture();
    const r = await run(["deliver"], { routes: forgeRoutes([]), cwd: work, ...g.options });
    assert.equal(r.code, EXIT.error);
    assert.equal(JSON.parse(r.err).error.code, "dirty_worktree");
    assert.deepEqual(g.lines(), []);
    assert.equal(git(["for-each-ref", "refs/heads/feature"], origin), "");
  });
});

describe("usage", () => {
  it("exits 2 on a bad command or a missing --pr", async () => {
    assert.equal((await run(["nope"])).code, EXIT.usage);
    assert.equal((await run(["pr-ready"])).code, EXIT.usage);
    assert.equal((await run(["wait", "deploy", "--pr", "7"])).code, EXIT.usage);
  });

  it("lists no completion subcommand and says to reply COMPLETE:", async () => {
    const out = [];
    assert.equal(await main(["--help"], ENV, { stdout: { write: (s) => out.push(s) } }), EXIT.ok);
    const help = out.join("");
    assert.match(help, /no completion subcommand/);
    assert.match(help, /COMPLETE:/);
    assert.doesNotMatch(help, /^\s+(complete|done)\s/m);
  });
});

describe("task plumbing", () => {
  const unmerged = { "GET /pulls/7": pr({ state: "open", merged: false }) };

  it("wait merge returns pending inside the tool timeout and says to run it again", async () => {
    const r = await run(["wait", "merge", "--pr", "7"], { routes: unmerged });
    assert.equal(r.code, EXIT.pending);
    assert.ok(r.elapsed < 100, `waited ${r.elapsed} s`);
    assert.match(r.out.next, /run the same command again \(argus-task wait merge --pr 7\)/);
  });

  it("pr-ready gives up on a slow forge as pending, not at the tool timeout", async () => {
    const r = await run(["pr-ready", "--pr", "7"], {
      routes: unmerged,
      onFetch: () => 70,
    });
    assert.equal(r.code, EXIT.pending);
    assert.equal(r.out.ready, null);
    assert.match(r.out.next, /argus-task pr-ready --pr 7/);
    assert.ok(r.elapsed < 100, `waited ${r.elapsed} s`);
  });

  it("infers the repository from the task allowlist when no flag or URL is set", async () => {
    const r = await run(["wait", "merge", "--pr", "7", "--max", "1"], {
      routes: unmerged,
      env: { ARGUS_REPOSITORY_URL: "", ARGUS_REPOSITORIES_JSON: JSON.stringify({ p: REPO }) },
    });
    assert.equal(r.code, EXIT.pending);
    assert.equal(r.calls[0], "GET /pulls/7");
  });

  it("does not guess a repository when the allowlist names several", async () => {
    const r = await run(["wait", "merge", "--pr", "7"], {
      routes: unmerged,
      env: {
        ARGUS_REPOSITORY_URL: "",
        ARGUS_REPOSITORIES_JSON: JSON.stringify({ a: REPO, b: `${REPO}2` }),
      },
    });
    assert.equal(r.code, EXIT.error);
    const { error } = JSON.parse(r.err);
    assert.equal(error.code, "policy_denied");
    assert.match(error.action, /--repository/);
    assert.deepEqual(r.calls, []);
  });

  it("every error object carries an action", async () => {
    const usageError = await run(["wait", "merge"], { routes: unmerged });
    assert.match(JSON.parse(usageError.err).error.action, /argus-task --help/);
    const internal = await main(["deliver"], ENV, {
      fetch: forge({}).fetch,
      cwd: repo(),
      git: () => {
        throw new TypeError("boom");
      },
      stdout: { write: () => {} },
      stderr: {
        write: (s) => {
          assert.equal(typeof JSON.parse(s).error.action, "string");
        },
      },
    });
    assert.equal(internal, EXIT.error);
  });
});
