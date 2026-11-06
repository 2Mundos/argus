import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, it } from "node:test";
import { ciSummary, digestLog, EXIT, main, waitCap } from "./argus-task.mjs";

const REPO = "https://git.example.com/team/project";
const ENV = {
  ARGUS_REPOSITORY_URL: REPO,
  ARGUS_REPOSITORIES_JSON: JSON.stringify({ p: REPO }),
};
const API = "/api/v1/repos/team/project";
const HEAD = "a".repeat(40);
const T0 = Date.parse("2026-11-02T10:00:00Z");
const iso = (s) => new Date(T0 + s * 1000).toISOString();

const scratch = mkdtempSync(join(tmpdir(), "argus-task-"));
after(() => rmSync(scratch, { recursive: true, force: true }));

const git = (args, cwd) =>
  execFileSync("git", args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();

let repoCount = 0;
function repo() {
  const dir = join(scratch, `r${repoCount++}`);
  mkdirSync(dir);
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
async function run(argv, { routes, cwd = repo(), env = {}, onSleep } = {}) {
  let t = T0;
  const out = [];
  const err = [];
  const f = forge(routes ?? {});
  const code = await main(
    argv,
    { ...ENV, ...env },
    {
      fetch: f.fetch,
      cwd,
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
  it("keeps the newest status per context and leaves agentic review out", () => {
    assert.deepEqual(
      ciSummary([
        { id: 1, context: "test", status: "failure" },
        { id: 2, context: "test", status: "success" },
        { id: 3, context: "lint", status: "pending" },
        { id: 4, context: "agentic-review", status: "pending" },
      ]),
      { state: "pending", total: 2, running: 1, failed: [] },
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
  it("defaults to 110 s and never exceeds ARGUS_TASK_WAIT_MAX_S", () => {
    assert.equal(waitCap(undefined, {}), 110);
    assert.equal(waitCap(undefined, { ARGUS_TASK_WAIT_MAX_S: "480" }), 480);
    assert.equal(waitCap("600", { ARGUS_TASK_WAIT_MAX_S: "480" }), 480);
    assert.equal(waitCap("60", { ARGUS_TASK_WAIT_MAX_S: "480" }), 60);
    assert.equal(waitCap(undefined, { ARGUS_TASK_WAIT_MAX_S: "9999" }), 540);
  });
});

describe("wait ci", () => {
  it("keeps waiting after a failure until every check has finished", async () => {
    let checks = 0;
    const r = await run(["wait", "ci", "--pr", "7"], {
      routes: {
        "GET /pulls/7": pr(),
        [`GET /statuses/${HEAD}`]: () => [
          { id: 1, context: "lint", status: "failure" },
          { id: 2, context: "test", status: ++checks < 2 ? "pending" : "success" },
        ],
      },
    });
    assert.equal(r.code, EXIT.ok);
    assert.equal(r.elapsed, 30);
    assert.deepEqual([r.out.state, r.out.failed, r.out.running], ["failure", ["lint"], 0]);
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
    assert.equal(r.elapsed, 30);
    assert.equal(r.calls.filter((c) => c.startsWith("GET /statuses")).length, 2);
  });

  it("checks once per interval and returns when CI finishes", async () => {
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
    assert.equal(r.elapsed, 60); // checks at 0, 30 and 60 s
    assert.deepEqual(during, { on: "ci", ref: "#7", since: iso(0) });
    assert.equal(existsSync(join(signals(r.cwd), "waiting.json")), false);
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
              { id: 11, conclusion: "failure" },
              { id: 12, conclusion: "success" },
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
        "GET /actions/jobs/21/logs": () => new Response("FAILED tests/test_x.py::test_y\ndone\n"),
      },
    });
    assert.equal(r.code, EXIT.ok, r.err);
    assert.deepEqual(r.out.excerpts, [
      {
        run_id: 11,
        job_id: 21,
        job: "test",
        step: "pytest",
        text: "FAILED tests/test_x.py::test_y\ndone",
      },
    ]);
    assert.equal(r.out.failed_runs, 1);
  });
});

describe("deliver", () => {
  /** origin (bare) with main, and a clone on branch `feature` one commit ahead. */
  function setup() {
    const origin = join(scratch, `o${repoCount++}.git`);
    git(["init", "-q", "--bare", "-b", "main", origin]);
    const seed = repo();
    git(["config", "user.email", "t@example.com"], seed);
    git(["config", "user.name", "t"], seed);
    writeFileSync(join(seed, "a.txt"), "one\n");
    git(["add", "."], seed);
    git(["commit", "-qm", "seed"], seed);
    git(["push", "-q", origin, "main"], seed);
    const work = join(scratch, `w${repoCount++}`);
    git(["clone", "-q", origin, work]);
    git(["config", "user.email", "t@example.com"], work);
    git(["config", "user.name", "t"], work);
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
    const r = await run(["deliver"], { routes: forgeRoutes(created), cwd: work });
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
