import { spawnSync } from "node:child_process";
import { appendFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { afterEach, describe, expect, it } from "vitest";
import {
  checkProvisionalRecord,
  createProvisionalUsageRelay,
  PROVISIONAL_RELAY_FILE,
  type ProvisionalBody,
} from "./argus-provisional-usage-relay.js";

const task = { id: "task-1", attempt: 2 };
const held = { engine: "acc-engine", executor: "acc-exec" };
const RUN = "r1:task-1:a:codex:claude:s";

const record = (over: Record<string, unknown> = {}) => ({
  v: 1,
  kind: "usage_provisional",
  task_id: "task-1",
  attempt: 2,
  account_id: "acc-exec",
  run_key: RUN,
  engine: "claude",
  revision: 1,
  model: "claude-opus-5-5",
  input_tokens: 10,
  output_tokens: 2,
  cached_input_tokens: 30,
  ...over,
});

const dirs: string[] = [];
afterEach(() => {
  for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
});

describe("checkProvisionalRecord", () => {
  it("rebuilds the body for the executor account the host leased", () => {
    expect(checkProvisionalRecord(record({ extra: "x" }), task, held)).toEqual({
      ok: true,
      accountId: "acc-exec",
      body: {
        task_id: "task-1",
        attempt: 2,
        run_key: RUN,
        revision: 1,
        model: "claude-opus-5-5",
        engine: "claude",
        input_tokens: 10,
        output_tokens: 2,
        cached_input_tokens: 30,
      },
    });
    // An executor without a lease of its own runs on the engine's account.
    const engineOnly = { engine: "acc-engine", executor: null };
    expect(checkProvisionalRecord(record({ account_id: "acc-engine" }), task, engineOnly).ok).toBe(
      true,
    );
  });

  it.each([
    ["final usage record", record({ kind: "usage" }), "not_provisional"],
    ["foreign task", record({ task_id: "other" }), "other_task"],
    ["stale attempt", record({ attempt: 1 }), "other_task"],
    ["foreign account", record({ account_id: "acc-engine" }), "account_mismatch"],
    ["bad run key", record({ run_key: "x:task-1" }), "bad_run_key"],
    ["run of another task", record({ run_key: "r1:task-9:a" }), "run_mismatch"],
    ["zero revision", record({ revision: 0 }), "bad_revision"],
    ["fractional revision", record({ revision: 1.5 }), "bad_revision"],
    ["negative tokens", record({ input_tokens: -1 }), "bad_figures"],
    ["unknown tokens", record({ output_tokens: null }), "bad_figures"],
  ])("rejects a %s", (_, raw, why) => {
    expect(checkProvisionalRecord(raw, task, held)).toEqual({ ok: false, why });
  });

  it("rejects every record when no account is held", () => {
    const none = { engine: null, executor: null };
    expect(checkProvisionalRecord(record(), task, none)).toEqual({
      ok: false,
      why: "no_account_held",
    });
  });
});

describe("createProvisionalUsageRelay", () => {
  function setup(post: (accountId: string, b: ProvisionalBody) => Promise<unknown>) {
    const dir = mkdtempSync(path.join(os.tmpdir(), "argus-provisional-relay-"));
    dirs.push(dir);
    const file = path.join(dir, PROVISIONAL_RELAY_FILE);
    const clock = { t: 1_000_000 };
    const relay = createProvisionalUsageRelay({
      file,
      task,
      held: () => held,
      post,
      now: () => clock.t,
    });
    const add = (...recs: unknown[]) =>
      appendFileSync(file, recs.map((r) => `${JSON.stringify(r)}\n`).join(""));
    return { relay, add, clock };
  }
  const err = (status: number, code?: string) =>
    Object.assign(new Error(`http ${status}`), { status, code });

  it("posts only the newest revision of each run, and never an older one again", async () => {
    const posted: [string, number][] = [];
    const s = setup(async (a, b) => void posted.push([b.run_key, b.revision]));
    const other = "r1:task-1:a:codex:claude:t";
    s.add(record(), record({ revision: 2 }), record({ run_key: other, revision: 1 }));
    await s.relay.poll();
    s.add(record({ revision: 2 }), record({ revision: 1 }), record({ revision: 3 }));
    await s.relay.poll();
    expect(posted).toEqual([
      [RUN, 2],
      [other, 1],
      [RUN, 3],
    ]);
  });

  it("keeps the newest revision through a back-off and drops it on another 4xx", async () => {
    let status = 503;
    const posted: number[] = [];
    const s = setup(async (_, b) => {
      if (status) throw err(status);
      posted.push(b.revision);
    });
    s.add(record());
    await s.relay.poll();
    s.add(record({ revision: 2 }));
    s.clock.t += 60_000;
    status = 422;
    await s.relay.poll();
    expect(s.relay.stats.reasons).toEqual({ http_422: 1 });
    status = 0;
    s.add(record({ revision: 3 }));
    await s.relay.poll();
    expect(posted).toEqual([3]);
  });

  it("stops on a lost claim and on 401, posting nothing after", async () => {
    for (const e of [err(409, "stale_attempt"), err(401)]) {
      let calls = 0;
      const s = setup(async () => {
        calls += 1;
        throw e;
      });
      s.add(record());
      await s.relay.poll();
      expect(s.relay.stopped).toBe(true);
      s.add(record({ revision: 2 }));
      await s.relay.poll();
      expect(calls).toBe(1);
    }
  });

  it("drops foreign accounts with a reason and ignores other tasks without noise", async () => {
    const posted: ProvisionalBody[] = [];
    const s = setup(async (_, b) => void posted.push(b));
    s.add(record({ account_id: "acc-other" }), record({ task_id: "task-2" }));
    await s.relay.poll();
    expect(posted).toHaveLength(0);
    expect(s.relay.stats.reasons).toEqual({ account_mismatch: 1 });
  });

  it("drain posts what is left; after stop nothing more is read or posted", async () => {
    let fail = 1;
    const posted: number[] = [];
    const s = setup(async (_, b) => {
      if (fail-- > 0) throw err(503);
      posted.push(b.revision);
    });
    s.add(record());
    expect(await s.relay.drain(60)).toEqual({ left: 1 });
    s.clock.t += 60_000;
    expect(await s.relay.drain(500)).toEqual({ left: 0 });
    s.relay.stop();
    s.add(record({ revision: 2 }));
    await s.relay.poll();
    expect(await s.relay.drain(60)).toEqual({ left: 0 });
    expect(posted).toEqual([1]);
  });
});

const usageModule = path.join(import.meta.dirname, "engines", "usage.mjs");
const fixture = path.join(
  import.meta.dirname,
  "engines",
  "fixtures",
  "claude-stream-inflight.jsonl",
);

describe("delegate to host, no token", () => {
  it.skipIf(!existsSync(usageModule) || !existsSync(fixture))(
    "a real Claude stream reaches the fake API as the newest cumulative revision",
    async () => {
      const root = mkdtempSync(path.join(os.tmpdir(), "argus-provisional-e2e-"));
      dirs.push(root);
      const group = path.join(root, "group");
      mkdirSync(group);
      const relayFile = path.join(group, PROVISIONAL_RELAY_FILE);
      const stdoutFile = path.join(root, "stdout.jsonl");
      // The delegate's meter, driven the way argus-delegate does: no token, no API url.
      const script = `
        import fs from "node:fs";
        import { startProvisionalUsage } from ${JSON.stringify(pathToFileURL(usageModule).href)};
        const lines = fs.readFileSync(${JSON.stringify(fixture)}, "utf8").trim().split("\\n");
        const out = ${JSON.stringify(stdoutFile)};
        fs.writeFileSync(out, "");
        const meter = startProvisionalUsage({
          file: ${JSON.stringify(relayFile)},
          stdoutFile: out,
          identity: { task_id: "task-1", attempt: 2, account_id: "acc-exec", run_key: "${RUN}", engine: "claude" },
        });
        for (const line of lines) { fs.appendFileSync(out, line + "\\n"); meter.poll(); }
      `;
      const run = spawnSync(process.execPath, ["--input-type=module", "-e", script], {
        env: { PATH: process.env.PATH ?? "" },
        encoding: "utf8",
      });
      expect(run.status, run.stderr).toBe(0);
      expect(readFileSync(relayFile, "utf8")).not.toMatch(/bearer|argus_[A-Za-z0-9]{8}/i);

      const posted: [string, ProvisionalBody][] = [];
      const relay = createProvisionalUsageRelay({
        file: relayFile,
        task,
        held: () => held,
        post: async (a, b) => void posted.push([a, b]),
      });
      await relay.poll();
      expect(posted).toHaveLength(1);
      const [accountId, body] = posted[0];
      expect(accountId).toBe("acc-exec");
      expect(body).toMatchObject({
        run_key: RUN,
        model: "claude-opus-5-5",
        input_tokens: 47026,
        output_tokens: 90,
        cached_input_tokens: 216541,
      });
      expect(body.revision).toBeGreaterThan(1);
    },
  );
});
