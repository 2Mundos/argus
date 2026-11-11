import { spawnSync } from "node:child_process";
import {
  appendFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { afterEach, describe, expect, it } from "vitest";
import {
  checkMilestoneRecord,
  createMilestoneRelay,
  MILESTONE_RELAY_FILE,
  type MilestoneBody,
} from "./argus-milestone-relay.js";

const REPO = "https://forge.example/acme/app";
const SHA = "a".repeat(40);
const task = { id: "task-1", attempt: 2 };

const record = (
  kind: string,
  data: Record<string, unknown>,
  over: Record<string, unknown> = {},
) => ({
  v: 1,
  task_id: "task-1",
  attempt: 2,
  run_key: "r1:task-1:a:codex:claude:s",
  kind,
  data,
  message: "ignored",
  at: "2026-11-11T00:00:00.000Z",
  ...over,
});
const commit = (over: Record<string, unknown> = {}) =>
  record("commit_created", { branch: "feat/x", sha: SHA }, over);

const dirs: string[] = [];
afterEach(() => {
  for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
});

describe("checkMilestoneRecord", () => {
  it("rebuilds the body and the message itself", () => {
    const c = checkMilestoneRecord(commit({ message: "Bearer abc" }), task, REPO);
    expect(c).toMatchObject({
      ok: true,
      body: {
        attempt: 2,
        kind: "commit_created",
        message: `Commit ${SHA.slice(0, 7)} created on feat/x`,
        data: { branch: "feat/x", sha: SHA },
      },
    });
  });

  it("accepts every relayed kind, matching the repository in any casing", () => {
    const ok = [
      record("pr_opened", { number: 7, url: `${REPO}/pulls/7` }),
      record("pr_opened", { number: 8, url: "https://FORGE.example/Acme/App/pulls/8" }),
      record("ci_running", { sha: SHA }),
      record("ci_passed", { sha: SHA }),
      record("ci_failed", { sha: SHA }),
      record("gate_failed", { gate: "lint" }),
    ];
    for (const r of ok) expect(checkMilestoneRecord(r, task, REPO).ok).toBe(true);
  });

  it.each([
    ["foreign task", commit({ task_id: "other" }), "other_task"],
    ["stale attempt", commit({ attempt: 1 }), "other_task"],
    ["bad run key", commit({ run_key: "x:task-1" }), "bad_run_key"],
    ["run of another task", commit({ run_key: "r1:task-9:a" }), "run_mismatch"],
    ["waiting", record("waiting", { reason: "x" }), "bad_kind"],
    ["waiting_input", record("waiting_input", {}), "bad_kind"],
    ["unknown kind", record("deploy", {}), "bad_kind"],
    ["short sha", record("ci_passed", { sha: "abc1234" }), "bad_sha"],
    ["uppercase sha", record("ci_passed", { sha: "A".repeat(40) }), "bad_sha"],
    ["extra key", record("ci_passed", { sha: SHA, extra: 1 }), "bad_data"],
    ["missing key", record("commit_created", { sha: SHA }), "bad_data"],
    ["gate too long", record("gate_failed", { gate: "g".repeat(201) }), "bad_data"],
    ["gate with newline", record("gate_failed", { gate: "a\nb" }), "bad_data"],
    ["credential in gate", record("gate_failed", { gate: "Bearer abcdef" }), "credential_like"],
    [
      "token in url",
      record("pr_opened", { number: 7, url: `https://x:tok@forge.example/acme/app/pulls/7` }),
      "foreign_repository",
    ],
    [
      "foreign repository",
      record("pr_opened", { number: 7, url: "https://forge.example/evil/app/pulls/7" }),
      "foreign_repository",
    ],
    [
      "number mismatch",
      record("pr_opened", { number: 8, url: `${REPO}/pulls/7` }),
      "foreign_repository",
    ],
  ])("rejects %s", (_n, rec, why) => {
    expect(checkMilestoneRecord(rec, task, REPO)).toEqual({ ok: false, why });
  });

  it("rejects a pull request when the task has no repository", () => {
    expect(
      checkMilestoneRecord(
        record("pr_opened", { number: 7, url: `${REPO}/pulls/7` }),
        task,
        undefined,
      ),
    ).toEqual({ ok: false, why: "foreign_repository" });
  });

  it("keys on task, attempt, kind and data, not on run or time", () => {
    const a = checkMilestoneRecord(commit(), task, REPO);
    const b = checkMilestoneRecord(commit({ run_key: "r1:task-1:z", at: "later" }), task, REPO);
    expect(a.ok && b.ok && a.key === b.key).toBe(true);
  });
});

describe("createMilestoneRelay", () => {
  function setup(
    post: (b: MilestoneBody) => Promise<unknown>,
    extra: Record<string, unknown> = {},
  ) {
    const dir = mkdtempSync(path.join(os.tmpdir(), "argus-milestone-relay-"));
    dirs.push(dir);
    const file = path.join(dir, MILESTONE_RELAY_FILE);
    const clock = { t: 1_000_000 };
    const relay = createMilestoneRelay({
      file,
      task,
      repository: REPO,
      post,
      now: () => clock.t,
      ...extra,
    });
    const add = (...recs: unknown[]) =>
      appendFileSync(file, recs.map((r) => `${JSON.stringify(r)}\n`).join(""));
    return { relay, add, file, dir, clock };
  }
  const err = (status: number, code?: string) =>
    Object.assign(new Error(`http ${status}`), { status, code });

  it("posts each valid milestone once, in order, with attempt, kind and data", async () => {
    const posted: MilestoneBody[] = [];
    const s = setup(async (b) => void posted.push(b));
    s.add(commit(), record("ci_failed", { sha: SHA }), record("gate_failed", { gate: "lint" }));
    await s.relay.poll();
    await s.relay.poll();
    expect(posted.map((b) => b.kind)).toEqual(["commit_created", "ci_failed", "gate_failed"]);
    expect(posted[0]).toEqual({
      attempt: 2,
      kind: "commit_created",
      message: `Commit ${SHA.slice(0, 7)} created on feat/x`,
      data: { branch: "feat/x", sha: SHA },
    });
  });

  it("does not post a replayed record again, even after the file is rewritten", async () => {
    const posted: MilestoneBody[] = [];
    const s = setup(async (b) => void posted.push(b));
    s.add(commit());
    await s.relay.poll();
    s.add(commit({ run_key: "r1:task-1:again" })); // same milestone, another run key
    writeFileSync(s.file, `${JSON.stringify(commit())}\n${JSON.stringify(commit())}\n`); // rotated and replayed
    await s.relay.poll();
    expect(posted).toHaveLength(1);
  });

  it("does not post a pull request the channel already relayed", async () => {
    const posted: MilestoneBody[] = [];
    const relayed = new Set<string>();
    const s = setup(async (b) => void posted.push(b), {
      alreadyPosted: (b: MilestoneBody) =>
        b.kind === "pr_opened" && relayed.has(String(b.data.url)),
      onPosted: (b: MilestoneBody) => b.kind === "pr_opened" && relayed.add(String(b.data.url)),
    });
    relayed.add(`${REPO}/pulls/7`);
    s.add(
      record("pr_opened", { number: 7, url: `${REPO}/pulls/7` }),
      record("pr_opened", { number: 8, url: `${REPO}/pulls/8` }),
    );
    await s.relay.poll();
    expect(posted.map((b) => b.data.number)).toEqual([8]);
    expect(relayed.has(`${REPO}/pulls/8`)).toBe(true); // the channel will not repeat it
  });

  it("keeps a record and backs off on a network error, 429 and 5xx", async () => {
    const answers: unknown[] = [new Error("ECONNRESET"), err(429), err(503), null];
    let calls = 0;
    const s = setup(async () => {
      calls += 1;
      const a = answers.shift();
      if (a) throw a;
    });
    s.add(commit());
    await s.relay.poll();
    expect(s.relay.pending).toBe(1);
    await s.relay.poll(); // same instant: backing off
    expect(calls).toBe(1);
    for (const wait of [2_100, 4_100, 8_100]) {
      s.clock.t += wait;
      await s.relay.poll();
    }
    expect(calls).toBe(4);
    expect(s.relay.pending).toBe(0);
    expect(s.relay.stats).toMatchObject({ posted: 1, dropped: 0 });
  });

  it("stops on a lost claim and on 401, and drops a record for any other 4xx", async () => {
    for (const code of ["stale_attempt", "not_assignee", "not_claimed"]) {
      const s = setup(async () => {
        throw err(409, code);
      });
      s.add(commit());
      await s.relay.poll();
      expect(s.relay.stopped).toBe(true);
      expect(s.relay.pending).toBe(0);
    }
    const unauth = setup(async () => {
      throw err(401);
    });
    unauth.add(commit());
    await unauth.relay.poll();
    expect(unauth.relay.stopped).toBe(true);

    const refusals = [err(422), err(404)];
    const posted: string[] = [];
    const s = setup(async (b) => {
      const e = refusals.shift();
      if (e) throw e;
      posted.push(b.kind);
    });
    s.add(commit(), record("ci_passed", { sha: SHA }), record("gate_failed", { gate: "lint" }));
    await s.relay.poll();
    expect(s.relay.stopped).toBe(false);
    expect(posted).toEqual(["gate_failed"]);
    expect(s.relay.stats.reasons).toEqual({ http_422: 1, http_404: 1 });
  });

  it("retries a 409 that is not a lost claim", async () => {
    let n = 0;
    const s = setup(async () => {
      if (++n === 1) throw err(409, "rate_limited");
    });
    s.add(commit());
    await s.relay.poll();
    expect(s.relay.stopped).toBe(false);
    expect(s.relay.pending).toBe(1);
  });

  it("drops bad lines with a reason and ignores other tasks without noise", async () => {
    const posted: MilestoneBody[] = [];
    const s = setup(async (b) => void posted.push(b));
    appendFileSync(s.file, "not json\n");
    s.add(
      commit({ task_id: "other" }),
      commit({ attempt: 1 }),
      record("waiting_input", {}),
      record("ci_passed", { sha: "short" }),
      commit(),
    );
    appendFileSync(s.file, `${"x".repeat(9000)}\n`);
    await s.relay.poll();
    expect(posted).toHaveLength(1);
    expect(s.relay.stats.reasons).toEqual({
      torn_line: 1,
      bad_kind: 1,
      bad_sha: 1,
      line_too_long: 1,
    });
  });

  it("stops when the file is a symlink and never reads through it", async () => {
    const posted: MilestoneBody[] = [];
    const s = setup(async (b) => void posted.push(b));
    const target = path.join(s.dir, "elsewhere");
    writeFileSync(target, `${JSON.stringify(commit())}\n`);
    symlinkSync(target, s.file);
    await s.relay.poll();
    expect(s.relay.stopped).toBe(true);
    expect(posted).toEqual([]);
  });

  it("bounds the pending queue", async () => {
    const posted: MilestoneBody[] = [];
    const s = setup(async () => {
      throw err(503);
    });
    for (let i = 0; i < 250; i += 1) s.add(record("gate_failed", { gate: `g${i}` }));
    await s.relay.poll();
    expect(s.relay.pending).toBe(200);
    expect(posted).toHaveLength(0);
  });

  it("drain posts what is left, waiting out a back-off, and reports what it could not", async () => {
    let fail = 1;
    const s = setup(async () => {
      if (fail-- > 0) throw err(503);
    });
    s.add(commit());
    expect(await s.relay.drain(60)).toEqual({ left: 1 });
    s.clock.t += 60_000;
    expect(await s.relay.drain(500)).toEqual({ left: 0 });
  });
});

const delegate = path.join(import.meta.dirname, "engines", "argus-delegate.mjs");
const sinkModule = path.join(import.meta.dirname, "engines", "milestones.mjs");

describe("delegate to host, no token", () => {
  it.skipIf(!existsSync(delegate))(
    "the delegate's relay file reaches the fake API and carries no token",
    async () => {
      const root = mkdtempSync(path.join(os.tmpdir(), "argus-milestone-e2e-"));
      dirs.push(root);
      const group = path.join(root, "group");
      mkdirSync(group);
      const relayFile = path.join(group, MILESTONE_RELAY_FILE);
      // The delegate's writer, driven the way argus-delegate does: no token, no API url.
      const script = `
        import { createSink } from ${JSON.stringify(pathToFileURL(sinkModule).href)};
        const sink = createSink({
          e: { ARGUS_TASK_ID: "task-1", ARGUS_TASK_ATTEMPT: "2" },
          out: ${JSON.stringify(path.join(root, "out"))},
          relay: { file: ${JSON.stringify(relayFile)}, taskId: "task-1", attempt: 2, runKey: "r1:task-1:a:codex:claude:s" },
          stderr: { write() {} },
          fetchImpl: async () => { throw new Error("no network on this path"); },
        });
        await sink({ kind: "commit_created", data: { branch: "feat/x", sha: "${SHA}" } });
        await sink({ kind: "pr_opened", data: { number: 7, url: "${REPO}/pulls/7" } });
        await sink({ kind: "waiting_input", data: {} });
      `;
      const run = spawnSync(process.execPath, ["--input-type=module", "-e", script], {
        env: { PATH: process.env.PATH ?? "" },
        encoding: "utf8",
      });
      expect(run.status, run.stderr).toBe(0);
      expect(readFileSync(relayFile, "utf8")).not.toMatch(/token|bearer/i);

      const posted: MilestoneBody[] = [];
      const relay = createMilestoneRelay({
        file: relayFile,
        task,
        repository: REPO,
        post: async (b) => void posted.push(b),
      });
      await relay.poll();
      expect(posted.map((b) => b.kind)).toEqual(["commit_created", "pr_opened"]);
    },
  );
});
