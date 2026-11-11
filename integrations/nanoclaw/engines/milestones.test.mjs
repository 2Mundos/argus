import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { COPILOT_SENTINEL_TOKEN, EXECUTORS } from "./argus-delegate.mjs";
import {
  ciState,
  createSink,
  createTracker,
  describe,
  gitHead,
  KINDS,
  MILESTONE_RELAY_FILE,
  milestone,
  pullRequestsIn,
  safeUrl,
  waitingInput,
} from "./milestones.mjs";
import { policySnapshot } from "./model-policy.mjs";

const SHA = "0123456789abcdef0123456789abcdef01234567";
const PR = "https://git.example.com/org/repo/pulls/12";

function tracker(probes, repository) {
  const emitted = [];
  const t = createTracker({ probes, emit: (m) => emitted.push(m), repository });
  return { t, emitted };
}

test("the kinds are a closed set", () => {
  assert.equal(milestone("deployed"), null);
  const samples = {
    commit_created: { branch: "b", sha: SHA },
    pr_opened: { number: 1, url: PR },
    ci_running: { sha: SHA },
    ci_passed: { sha: SHA },
    ci_failed: { sha: SHA },
    gate_failed: { gate: "lint" },
    waiting_input: {},
  };
  assert.deepEqual(Object.keys(samples), KINDS);
  for (const k of KINDS)
    assert.ok(milestone(k, samples[k]) && describe({ kind: k, data: samples[k] }));
  assert.equal(milestone("commit_created", { branch: "b", sha: "xyz" }), null);
});

test("safeUrl accepts only http(s) without userinfo, query or fragment", () => {
  assert.equal(safeUrl(PR), PR);
  for (const bad of [
    "javascript:alert(1)",
    "ftp://x/pulls/1",
    "https://u:p@git.example.com/pulls/1",
    `${PR}?token=abc`,
    `${PR}#frag`,
    "not a url",
  ])
    assert.equal(safeUrl(bad), null, bad);
});

test("pullRequestsIn finds PR links, strips tokens, and skips credentialed URLs", () => {
  const text = `opened ${PR}?access_token=SECRET.\nalso https://u:p@h.example.com/o/r/pulls/3 and https://h.example.com/o/r/issues/4`;
  assert.deepEqual(pullRequestsIn(text), [{ number: 12, url: PR }]);
});

test("a new commit is a milestone, the baseline commit is not, and each is emitted once", async () => {
  let head = { branch: "main", sha: "a".repeat(40) };
  const { t, emitted } = tracker({ head: () => head, output: () => "" });
  await t.start();
  await t.poll();
  assert.deepEqual(emitted, []); // nothing happened yet
  head = { branch: "feat/x", sha: SHA };
  await t.poll();
  await t.poll();
  assert.deepEqual(emitted, [{ kind: "commit_created", data: { branch: "feat/x", sha: SHA } }]);
});

test("absence: no git, no output and no CI emit nothing and do not throw", async () => {
  const { t, emitted } = tracker({
    head: () => {
      throw new Error("not a repo");
    },
    output: () => {
      throw new Error("gone");
    },
    ci: () => {
      throw new Error("offline");
    },
  });
  await t.start();
  await t.poll();
  assert.deepEqual(emitted, []);
});

test("pr_opened comes from executor output, only for the configured repository", async () => {
  const output = `see https://git.example.com/other/repo/pulls/9 and ${PR}`;
  const { t, emitted } = tracker(
    { head: () => null, output: () => output },
    "https://git.example.com/org/repo.git",
  );
  await t.poll();
  assert.deepEqual(emitted, [{ kind: "pr_opened", data: { number: 12, url: PR } }]);
});

test("pr_opened matches the repository whatever the owner's casing (2Mundos/argus #255)", async () => {
  const url = "https://github.com/2Mundos/argus/pull/255";
  const { t, emitted } = tracker(
    { head: () => null, output: () => `opened ${url}` },
    "https://github.com/2mundos/argus.git",
  );
  await t.poll();
  assert.deepEqual(emitted, [{ kind: "pr_opened", data: { number: 255, url } }]);
});

test("CI states map to milestones and a failing context is a gate_failed", async () => {
  let statuses = [{ id: 1, context: "lint", status: "pending" }];
  let head = { branch: "b", sha: "a".repeat(40) };
  const { t, emitted } = tracker({ head: () => head, output: () => "", ci: () => statuses });
  await t.start();
  head = { branch: "b", sha: SHA };
  await t.poll();
  statuses = [
    { id: 1, context: "lint", status: "pending" },
    { id: 2, context: "lint", status: "failure" },
    { id: 3, context: "test", status: "success" },
  ];
  await t.poll();
  assert.deepEqual(
    emitted.map((m) => [m.kind, m.data.gate ?? m.data.sha]),
    [
      ["commit_created", SHA],
      ["ci_running", SHA],
      ["ci_failed", SHA],
      ["gate_failed", "lint"],
    ],
  );
  statuses = [{ id: 4, context: "lint", status: "success" }];
  await t.poll();
  assert.equal(emitted.at(-1).kind, "ci_passed");
});

test("ciState: no statuses is no signal; newest per context wins", () => {
  assert.equal(ciState([]), null);
  assert.equal(
    ciState([
      { id: 2, context: "a", status: "success" },
      { id: 1, context: "a", status: "failure" },
    ]).state,
    "success",
  );
});

test("waitingInput reads permission denials from the executor's final JSON", () => {
  assert.equal(waitingInput('x\n{"result":"r","permission_denials":[{"tool":"Bash"}]}'), true);
  assert.equal(waitingInput('{"result":"r","permission_denials":[]}'), false);
  assert.equal(waitingInput("not json"), false);
});

test("gitHead reads HEAD through an injected runner", () => {
  const run = (_bin, args) => ({
    status: 0,
    stdout: args.includes("--abbrev-ref") ? "feat/x\n" : `${SHA}\n`,
  });
  assert.deepEqual(gitHead("/w", run), { sha: SHA, branch: "feat/x" });
  assert.equal(
    gitHead("/w", () => ({ status: 128, stdout: "" })),
    null,
  );
});

test("the sink writes stderr and jsonl, posts only with the full environment, and never leaks the token", async (t) => {
  const out = fs.mkdtempSync(path.join(os.tmpdir(), "milestones-"));
  t.after(() => fs.rmSync(out, { recursive: true, force: true }));
  const err = [];
  const calls = [];
  const fetchImpl = async (url, init) => calls.push({ url, init });
  const m = { kind: "pr_opened", data: { number: 12, url: PR } };

  await createSink({ e: {}, out, stderr: { write: (s) => err.push(s) }, fetchImpl })(m);
  assert.equal(calls.length, 0); // no API environment: file and stderr only

  const e = {
    ARGUS_API_URL: "http://argus:8000/",
    ARGUS_TASK_ID: "t1",
    ARGUS_TASK_ATTEMPT: "2",
    ARGUS_AGENT_TOKEN: "argus_SECRET_TOKEN",
  };
  await createSink({ e, out, stderr: { write: (s) => err.push(s) }, fetchImpl })(m);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "http://argus:8000/api/tasks/t1/progress");
  assert.equal(calls[0].init.headers.Authorization, "Bearer argus_SECRET_TOKEN");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    attempt: 2,
    message: "Pull request #12 opened",
    kind: "pr_opened",
    data: m.data,
  });
  const files = fs.readFileSync(path.join(out, "milestones.jsonl"), "utf8") + err.join("");
  assert.equal(files.includes("SECRET"), false);
  assert.equal(files.trim().split("\n").length, 4);

  const failing = createSink({
    e,
    out,
    stderr: { write: () => {} },
    fetchImpl: async () => {
      throw new Error("down");
    },
  });
  await failing(m); // best effort: does not throw
});

test("argus-delegate emits milestones for a real commit and PR and keeps its report contract", async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "milestones-run-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const work = path.join(dir, "work");
  const bin = path.join(dir, "bin");
  const home = path.join(dir, "home");
  for (const d of [work, bin, home]) fs.mkdirSync(d);
  const gitEnv = {
    GIT_AUTHOR_NAME: "t",
    GIT_AUTHOR_EMAIL: "t@example.com",
    GIT_COMMITTER_NAME: "t",
    GIT_COMMITTER_EMAIL: "t@example.com",
  };
  const git = (...a) =>
    spawnSync("git", ["-C", work, ...a], { env: { ...process.env, ...gitEnv }, encoding: "utf8" });
  if (git("init", "-q").status !== 0) return t.skip("git is not available");
  git("commit", "-q", "--allow-empty", "-m", "base");
  fs.writeFileSync(
    path.join(bin, "claude"),
    `#!/bin/sh\nif [ "$1" = "--version" ]; then echo 1.0; exit 0; fi\ncat >/dev/null\n` +
      `git -C ${work} commit -q --allow-empty -m work\n` +
      `echo '{"result":"opened ${PR}?token=SECRET"}'\n`,
    { mode: 0o755 },
  );

  const posts = [];
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      posts.push({ url: req.url, auth: req.headers.authorization, body: JSON.parse(body) });
      res.end("{}");
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(() => server.close());

  const env = {
    ...process.env,
    ...gitEnv,
    NO_PROXY: "127.0.0.1",
    no_proxy: "127.0.0.1",
    PATH: `${bin}:${process.env.PATH}`,
    HOME: home,
    ARGUS_DELEGATE_ROOTS: work,
    ANTHROPIC_API_KEY: "placeholder",
    ARGUS_API_URL: `http://127.0.0.1:${server.address().port}`,
    ARGUS_TASK_ID: "task-1",
    ARGUS_TASK_ATTEMPT: "1",
    ARGUS_AGENT_TOKEN: "agent-token-value",
  };
  const delegate = path.join(path.dirname(fileURLToPath(import.meta.url)), "argus-delegate.mjs");
  const result = await new Promise((resolve) => {
    const child = spawn(process.execPath, [delegate, "claude", "--cwd", work, "--timeout", "30"], {
      env,
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (c) => (stdout += c));
    child.stderr.on("data", (c) => (stderr += c));
    child.stdin.end("go");
    child.on("close", (code) => resolve({ code, stdout, stderr }));
  });
  assert.equal(result.code, 0);
  const report = JSON.parse(result.stdout.trim().split("\n").pop());
  assert.equal(report.executor, "claude"); // the report contract is unchanged
  assert.equal(report.milestones, undefined);

  const lines = fs
    .readFileSync(path.join(work, ".argus-delegate", "milestones.jsonl"), "utf8")
    .trim()
    .split("\n")
    .map((l) => JSON.parse(l));
  assert.deepEqual(
    lines.map((l) => l.kind),
    ["commit_created", "pr_opened"],
  );
  assert.deepEqual(lines[1].data, { number: 12, url: PR });
  assert.match(result.stderr, /argus-milestone /);
  assert.deepEqual(
    posts.map((p) => [p.url, p.auth, p.body.kind]),
    [
      ["/api/tasks/task-1/progress", "Bearer agent-token-value", "commit_created"],
      ["/api/tasks/task-1/progress", "Bearer agent-token-value", "pr_opened"],
    ],
  );
  assert.equal((result.stderr + JSON.stringify(lines)).includes("agent-token-value"), false);
  assert.equal(JSON.stringify(lines).includes("SECRET"), false);
});

test("the sink appends token-free relay records for the host and never relays waiting_input", async () => {
  const out = fs.mkdtempSync(path.join(os.tmpdir(), "milestones-relay-"));
  try {
    const file = path.join(out, "group", MILESTONE_RELAY_FILE);
    const relay = { file, taskId: "t1", attempt: 2, runKey: "r1:t1:a:codex:claude:s" };
    const e = { ARGUS_API_URL: "http://argus:8000", ARGUS_TASK_ID: "t1" }; // no token
    const calls = [];
    const sink = createSink({
      e,
      out,
      relay,
      stderr: { write: () => {} },
      fetchImpl: async (...a) => calls.push(a),
    });
    await sink({ kind: "commit_created", data: { branch: "b", sha: SHA } });
    await sink({ kind: "waiting_input", data: {} });
    assert.equal(calls.length, 0); // no network without the token
    const lines = fs.readFileSync(file, "utf8").trim().split("\n").map(JSON.parse);
    assert.equal(lines.length, 1);
    assert.deepEqual(
      { ...lines[0], at: typeof lines[0].at },
      {
        v: 1,
        task_id: "t1",
        attempt: 2,
        run_key: relay.runKey,
        kind: "commit_created",
        data: { branch: "b", sha: SHA },
        message: `Commit ${SHA.slice(0, 7)} created on b`,
        at: "string",
      },
    );
  } finally {
    fs.rmSync(out, { recursive: true, force: true });
  }
});

test("a failing CI head is followed by gate_failed for each failing context of that exact SHA", async () => {
  const other = "f".repeat(40);
  const asked = [];
  let head = { branch: "main", sha: other };
  const { t, emitted } = tracker({
    head: () => head,
    output: () => "",
    ci: (sha) => {
      asked.push(sha);
      return sha === SHA
        ? [
            { id: 1, context: "lint", status: "failure" },
            { id: 2, context: "unit", status: "error" },
            { id: 3, context: "build", status: "success" },
          ]
        : [{ id: 9, context: "old-head", status: "failure" }];
    },
  });
  await t.start();
  head = { branch: "feat/x", sha: SHA };
  await t.poll();
  await t.poll();
  assert.deepEqual(asked, [SHA, SHA]);
  assert.deepEqual(
    emitted.map((m) => [m.kind, m.data.gate ?? m.data.sha]),
    [
      ["commit_created", SHA],
      ["ci_failed", SHA],
      ["gate_failed", "lint"],
      ["gate_failed", "unit"],
    ],
  );
  assert.equal(
    emitted.some((m) => m.data.sha === other || m.data.gate === "old-head"),
    false,
  );
});

// Every executor identifier gets the same milestones through the real delegate, with no Argus
// token in its environment; where the provider offers no CI signal nothing is invented.
const AUTH = {
  claude: (s) => ({ ANTHROPIC_API_KEY: "placeholder" }),
  codex: () => ({ OPENAI_API_KEY: "placeholder" }),
  opencode: (s) => {
    const dir = path.join(s.home, ".local/share/opencode");
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, "auth.json"), '{"token":"sentinel"}');
    return {};
  },
  agent: (s) => {
    const dir = path.join(s.home, ".config/cursor");
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, "auth.json"), '{"accessToken":"sentinel"}');
    return {};
  },
  copilot: () => ({ COPILOT_GITHUB_TOKEN: COPILOT_SENTINEL_TOKEN }),
};

for (const executor of EXECUTORS) {
  test(`argus-delegate ${executor}: milestones reach the relay file without a token, CI stays unknown`, async (t) => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "milestones-relay-run-"));
    t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
    const s = { home: path.join(dir, "home") };
    const work = path.join(dir, "work");
    const bin = path.join(dir, "bin");
    for (const d of [work, bin, s.home]) fs.mkdirSync(d);
    // Commits come from `git fast-import` (the identity is in the stream), so no git identity
    // variables or config are needed; the fake executor switches to the second branch.
    const git = (args, input) =>
      spawnSync("git", ["-C", work, ...args], { encoding: "utf8", input });
    const commit = (branch, mark, subject) =>
      `commit refs/heads/${branch}\nmark :${mark}\ncommitter t <t@example.com> 1700000000 +0000\n` +
      `data ${subject.length}\n${subject}\n\n`;
    if (git(["init", "-q", "-b", "base-branch"]).status !== 0)
      return t.skip("git is not available");
    if (
      git(
        ["fast-import", "--quiet"],
        commit("base-branch", 1, "base") + commit("feat/x", 2, "work"),
      ).status !== 0
    )
      return t.skip("git fast-import is not available");
    git(["checkout", "-q", "base-branch"]);
    fs.writeFileSync(
      path.join(bin, executor),
      `#!/bin/sh\nif [ "$1" = "--version" ]; then echo 1.0; exit 0; fi\ncat >/dev/null\n` +
        `git -C ${work} checkout -q feat/x\n` +
        `echo '{"result":"opened ${PR}"}'\n`,
      { mode: 0o755 },
    );
    const policy = JSON.parse(
      fs.readFileSync(new URL("./model-policy.defaults.json", import.meta.url), "utf8"),
    );
    const policyFile = path.join(dir, "group", "argus-model-policy.json");
    fs.mkdirSync(path.dirname(policyFile));
    fs.writeFileSync(
      policyFile,
      JSON.stringify(policySnapshot(policy, { task_id: "task-1", attempt: 3 })),
    );
    const env = {
      ...process.env,
      PATH: `${bin}:${process.env.PATH}`,
      HOME: s.home,
      ARGUS_DELEGATE_ROOTS: work,
      ARGUS_ENGINE: "codex",
      ARGUS_MODEL_POLICY_FILE: policyFile,
      ARGUS_API_URL: "http://127.0.0.1:9", // unreachable: the delegate must not need it
      ARGUS_TASK_ID: "task-1",
      ARGUS_TASK_ATTEMPT: "3",
      ...AUTH[executor](s),
    };
    for (const k of ["ARGUS_AGENT_TOKEN", "XDG_CONFIG_HOME", "XDG_DATA_HOME"]) delete env[k];
    const delegate = path.join(path.dirname(fileURLToPath(import.meta.url)), "argus-delegate.mjs");
    const result = await new Promise((resolve) => {
      const child = spawn(
        process.execPath,
        [
          delegate,
          executor,
          "--cwd",
          work,
          "--timeout",
          "30",
          "--out",
          path.join(dir, "out"),
          ...(executor === "copilot" ? ["--model", "gpt-5"] : []),
        ],
        { env },
      );
      let stdout = "";
      let stderr = "";
      child.stdout.on("data", (c) => (stdout += c));
      child.stderr.on("data", (c) => (stderr += c));
      child.stdin.end("go");
      child.on("close", (code) => resolve({ code, stdout, stderr }));
    });
    assert.equal(result.code, 0, result.stdout.trim().split("\n").pop() + result.stderr);
    const report = JSON.parse(result.stdout.trim().split("\n").pop());
    assert.equal(report.executor, executor);
    const lines = fs
      .readFileSync(path.join(dir, "group", MILESTONE_RELAY_FILE), "utf8")
      .trim()
      .split("\n")
      .map(JSON.parse);
    // No ARGUS_REPOSITORIES_JSON: the CI probe is absent, so no ci_* is invented; and a pull
    // request needs no repository filter here (the host filters by the task's repository).
    assert.deepEqual(
      lines.map((l) => l.kind),
      ["commit_created", "pr_opened"],
    );
    for (const l of lines) {
      assert.equal(l.task_id, "task-1");
      assert.equal(l.attempt, 3);
      assert.match(l.run_key, /^r1:task-1:/);
      assert.equal(l.message.toLowerCase().includes(executor), false); // no executor text invented
    }
    assert.match(lines[0].data.sha, /^[0-9a-f]{40}$/);
    assert.equal(lines[0].data.branch, "feat/x");
    assert.equal(JSON.stringify(lines).includes("TOKEN"), false);
  });
}
