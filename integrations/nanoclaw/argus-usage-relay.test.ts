import { appendFileSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import {
  checkUsageRecord,
  createUsageRelay,
  type HeldAccountIds,
  USAGE_RELAY_FILE,
  type UsageBody,
} from "./argus-usage-relay.js";

const task = { id: "t1", attempt: 2 };
const held: HeldAccountIds = { engine: "acc-engine", executor: "acc-exec" };

const record = (over: Record<string, unknown> = {}, payload: Record<string, unknown> = {}) => ({
  v: 1,
  kind: "usage",
  source: "delegate",
  role: "executor",
  task_id: "t1",
  attempt: 2,
  account_id: "acc-exec",
  payload: {
    task_id: "t1",
    model: "m",
    engine: "claude",
    measurement: "measured",
    input_tokens: 3,
    output_tokens: 4,
    cached_input_tokens: 0,
    cost_usd: null,
    run_key: "r1:t1:a:codex:claude:s",
    ...payload,
  },
  ...over,
});

describe("checkUsageRecord", () => {
  it("builds the body field by field and bills the account held for the role", () => {
    const c = checkUsageRecord(
      record({}, { evil: "x", rate_limits: [{ pool: "cursor" }], account_id: "other" }),
      task,
      held,
    );
    expect(c).toEqual({
      ok: true,
      accountId: "acc-exec",
      body: {
        task_id: "t1",
        model: "m",
        engine: "claude",
        role: "executor",
        measurement: "measured",
        input_tokens: 3,
        output_tokens: 4,
        cached_input_tokens: 0,
        cost_usd: null,
        run_key: "r1:t1:a:codex:claude:s",
      },
    });
  });

  it("an executor without its own lease runs on the engine account", () => {
    const c = checkUsageRecord(record({ account_id: "acc-engine" }), task, {
      engine: "acc-engine",
      executor: null,
    });
    expect(c).toMatchObject({ ok: true, accountId: "acc-engine" });
  });

  it.each([
    ["wrong account", record({ account_id: "acc-other" }), "account_mismatch"],
    ["engine role naming the executor account", record({ role: "engine" }), "account_mismatch"],
    ["other task", record({ task_id: "t9" }), "other_task"],
    ["other attempt", record({ attempt: 1 }), "other_task"],
    ["bad role", record({ role: "admin" }), "bad_role"],
    ["payload task differs", record({}, { task_id: "t9" }), "bad_payload"],
    ["run_key with a space", record({}, { run_key: "a b" }), "bad_run_key"],
    ["negative tokens", record({}, { input_tokens: -1 }), "bad_figures"],
    ["fractional tokens", record({}, { output_tokens: 1.5 }), "bad_figures"],
    ["tokens as strings", record({}, { output_tokens: "7" }), "bad_figures"],
    [
      "measured without figures",
      record(
        {},
        { input_tokens: null, output_tokens: null, cached_input_tokens: null, cost_usd: null },
      ),
      "measured_without_figures",
    ],
    [
      "unmeasured with figures",
      record({}, { measurement: "not_reported" }),
      "unmeasured_with_figures",
    ],
    ["unknown measurement", record({}, { measurement: "guess" }), "bad_measurement"],
  ])("drops %s", (_n, rec, why) => {
    expect(checkUsageRecord(rec, task, held)).toEqual({ ok: false, why });
  });

  it("drops a record when no account is held for the role", () => {
    expect(
      checkUsageRecord(record({ role: "engine" }), task, { engine: null, executor: null }),
    ).toEqual({ ok: false, why: "no_account_held" });
  });

  it("keeps an unmeasured run with its reason and never turns a missing figure into 0", () => {
    const c = checkUsageRecord(
      record(
        {},
        {
          measurement: "not_reported",
          reason: "no_result_object",
          input_tokens: null,
          output_tokens: null,
          cached_input_tokens: null,
          cost_usd: null,
        },
      ),
      task,
      held,
    );
    expect(c).toMatchObject({
      ok: true,
      body: { measurement: "not_reported", reason: "no_result_object", input_tokens: null },
    });
    const noReason = checkUsageRecord(
      record(
        {},
        {
          measurement: "not_reported",
          reason: "Bad Reason!",
          input_tokens: null,
          output_tokens: null,
          cached_input_tokens: null,
          cost_usd: null,
        },
      ),
      task,
      held,
    );
    expect(noReason).toMatchObject({ ok: true, body: { reason: "unknown" } });
  });
});

describe("createUsageRelay", () => {
  const dirs: string[] = [];
  afterEach(() => {
    for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
  });
  const setup = (post: (id: string, b: UsageBody) => Promise<unknown>, clock = { t: 0 }) => {
    const dir = mkdtempSync(path.join(os.tmpdir(), "argus-usage-relay-"));
    dirs.push(dir);
    const file = path.join(dir, USAGE_RELAY_FILE);
    writeFileSync(file, "");
    const relay = createUsageRelay({ file, task, held: () => held, post, now: () => clock.t });
    const add = (...recs: unknown[]) =>
      appendFileSync(file, recs.map((r) => `${JSON.stringify(r)}\n`).join(""));
    return { relay, add, file, dir, clock };
  };
  const status = (n: number) => Object.assign(new Error(`http ${n}`), { status: n });

  it("posts a record once however often the file is read", async () => {
    const posted: [string, string][] = [];
    const s = setup(async (id, b) => void posted.push([id, b.run_key]));
    s.add(record());
    await s.relay.poll();
    await s.relay.poll();
    expect(posted).toEqual([["acc-exec", "r1:t1:a:codex:claude:s"]]);
    // the same key appended again (a delegate re-run with the same start) is not reposted
    s.add(record());
    await s.relay.poll();
    expect(posted).toHaveLength(1);
  });

  it("waits for a record's newline before reading it", async () => {
    const posted: string[] = [];
    const s = setup(async (_i, b) => void posted.push(b.run_key));
    const line = JSON.stringify(record());
    appendFileSync(s.file, line.slice(0, 40));
    await s.relay.poll();
    expect(posted).toEqual([]);
    appendFileSync(s.file, `${line.slice(40)}\n`);
    await s.relay.poll();
    expect(posted).toHaveLength(1);
  });

  it("keeps the record through 503, 429 and network errors and retries after a back-off", async () => {
    const outcomes: unknown[] = [status(503), status(429), new TypeError("fetch failed"), null];
    const sent: string[] = [];
    const s = setup(async (_i, b) => {
      const o = outcomes.shift();
      if (o) throw o;
      sent.push(b.run_key);
    });
    s.add(record());
    for (let i = 0; i < 4; i++) {
      await s.relay.poll();
      s.clock.t += 60_000; // past any back-off
    }
    expect(sent).toEqual(["r1:t1:a:codex:claude:s"]);
    expect(s.relay.pending).toBe(0);
  });

  it("does not retry before the back-off has passed", async () => {
    let calls = 0;
    const s = setup(async () => {
      calls += 1;
      throw status(503);
    });
    s.add(record());
    await s.relay.poll();
    await s.relay.poll(); // same instant: backing off
    expect(calls).toBe(1);
    expect(s.relay.pending).toBe(1);
    s.clock.t += 60_000;
    await s.relay.poll();
    expect(calls).toBe(2);
  });

  it("drops a permanently refused record (403 not the holder, 422) and goes on with the next", async () => {
    const sent: string[] = [];
    const s = setup(async (_i, b) => {
      if (b.run_key === "a") throw status(403);
      if (b.run_key === "b") throw status(422);
      sent.push(b.run_key);
    });
    s.add(record({}, { run_key: "a" }), record({}, { run_key: "b" }), record({}, { run_key: "c" }));
    await s.relay.poll();
    expect(sent).toEqual(["c"]);
    expect(s.relay.stats).toMatchObject({ posted: 1, dropped: 2 });
    expect(s.relay.stats.reasons).toEqual({ http_403: 1, http_422: 1 });
  });

  it("an idempotent repeat from the API (204 again) is the same as a first post", async () => {
    // The API answers 204 for a repeated run_key; the relay cannot tell and must not care.
    const s = setup(async () => undefined);
    s.add(record());
    await s.relay.poll();
    expect(s.relay.pending).toBe(0);
  });

  it("drain posts what is written and returns what could not be posted", async () => {
    let up = false;
    const sent: string[] = [];
    const s = setup(async (_i, b) => {
      if (!up) throw status(503);
      sent.push(b.run_key);
    });
    s.add(record());
    expect(await s.relay.drain(120)).toEqual({ left: 1 });
    up = true;
    s.clock.t += 60_000;
    expect(await s.relay.drain(500)).toEqual({ left: 0 });
    expect(sent).toHaveLength(1);
  });

  it("stops at a symlink instead of following it", async () => {
    const posted: string[] = [];
    const s = setup(async (_i, b) => void posted.push(b.run_key));
    const secret = path.join(s.dir, "elsewhere");
    writeFileSync(secret, `${JSON.stringify(record())}\n`);
    rmSync(s.file);
    symlinkSync(secret, s.file);
    await s.relay.poll();
    expect(posted).toEqual([]);
    expect(s.relay.stopped).toBe(true);
  });

  it("a stale file from another task posts nothing", async () => {
    const posted: string[] = [];
    const s = setup(async (_i, b) => void posted.push(b.run_key));
    s.add(record({ task_id: "t0", attempt: 1 }));
    await s.relay.poll();
    expect(posted).toEqual([]);
    expect(s.relay.stats.dropped).toBe(0); // another task's record is ignored, not an error
  });
});
