import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  ciState,
  createSink,
  createTracker,
  describe,
  gitHead,
  KINDS,
  milestone,
  pullRequestsIn,
  safeUrl,
  waitingInput,
} from "./milestones.mjs";

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
