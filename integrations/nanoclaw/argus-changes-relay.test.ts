import { linkSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import {
  CHANGES_RELAY_FILE,
  type ChangesBody,
  changesBody,
  createChangesRelay,
} from "./argus-changes-relay.js";

const task = { id: "t1", attempt: 2 };
const SHA = "0123456789abcdef0123456789abcdef01234567";

const record = (over: Record<string, unknown> = {}) => ({
  v: 1,
  task_id: "t1",
  attempt: 2,
  at: "2026-10-29T12:00:00.000Z",
  branch: "argus/task-x",
  base: { ref: "origin/main", sha: SHA },
  head: SHA,
  commits: [{ sha: SHA, subject: "feat: x" }],
  files: [{ path: "a.txt", status: "M", added: 1, deleted: 1, binary: false }],
  untracked: [{ path: "n.txt", bytes: 3 }],
  patch: "diff --git a/a.txt b/a.txt\n",
  truncated: { files: false, patch: false },
  ...over,
});

const dirs: string[] = [];
afterEach(() => {
  for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
});

function setup(put: (b: ChangesBody) => Promise<unknown>, clock = { t: 0 }) {
  const dir = mkdtempSync(path.join(os.tmpdir(), "argus-changes-relay-"));
  dirs.push(dir);
  const file = path.join(dir, CHANGES_RELAY_FILE);
  const write = (over: Record<string, unknown> = {}) =>
    writeFileSync(file, `${JSON.stringify(record(over))}\n`);
  const relay = createChangesRelay({ file, task, put, now: () => clock.t });
  return { dir, file, write, relay, clock };
}

describe("changesBody", () => {
  it("rebuilds the body field by field for the claimed task and attempt only", () => {
    const body = changesBody(JSON.stringify(record({ extra: "dropped", head: 7 })), task);
    expect(body).toMatchObject({ attempt: 2, branch: "argus/task-x", head: null });
    expect(body).not.toHaveProperty("extra");
    expect(body).not.toHaveProperty("task_id");
    expect(changesBody(JSON.stringify(record({ attempt: 1 })), task)).toBeNull();
    expect(changesBody(JSON.stringify(record({ task_id: "t2" })), task)).toBeNull();
    expect(changesBody(JSON.stringify(record({ v: 2 })), task)).toBeNull();
    expect(changesBody("", task)).toBeNull();
    expect(changesBody("{torn", task)).toBeNull();
  });

  it("drops malformed entries and bounds the lists", () => {
    const files = Array.from({ length: 600 }, (_, i) => ({ path: `f${i}`, status: "A" }));
    const body = changesBody(
      JSON.stringify(record({ files: [...files, 5], commits: [{ sha: 1 }], untracked: "x" })),
      task,
    );
    expect(body?.files).toHaveLength(500);
    expect(body?.files[0]).toEqual({
      path: "f0",
      status: "A",
      added: null,
      deleted: null,
      binary: false,
    });
    expect(body?.commits).toEqual([]);
    expect(body?.untracked).toEqual([]);
  });
});

describe("createChangesRelay", () => {
  it("puts a new snapshot at most once per 10 s, and never the same one twice", async () => {
    const puts: ChangesBody[] = [];
    const { write, relay, clock } = setup(async (b) => void puts.push(b));
    await relay.poll(); // no file yet
    write();
    await relay.poll();
    await relay.poll(); // same snapshot
    expect(puts).toHaveLength(1);
    write({ at: "2026-10-29T12:00:05.000Z" });
    clock.t = 5_000;
    await relay.poll(); // too soon
    expect(puts).toHaveLength(1);
    clock.t = 10_000;
    await relay.poll();
    expect(puts.map((p) => p.at)).toEqual(["2026-10-29T12:00:00.000Z", "2026-10-29T12:00:05.000Z"]);
  });

  it("drain puts the last snapshot within the server's 5 s pace", async () => {
    const puts: ChangesBody[] = [];
    const { write, relay, clock } = setup(async (b) => void puts.push(b));
    write();
    await relay.poll();
    write({ at: "later" });
    clock.t = 5_000; // 10 s have not passed, 5 s have
    await relay.drain(1_000);
    expect(puts.map((p) => p.at)).toEqual(["2026-10-29T12:00:00.000Z", "later"]);
  });

  it("stops when the claim is lost, drops a permanent refusal, retries the rest", async () => {
    const fail = (status: number, code = "") => Object.assign(new Error(code), { status, code });
    let next: Error | null = fail(422, "validation_failed");
    let calls = 0;
    const { write, relay, clock } = setup(async () => {
      calls += 1;
      if (next) throw next;
    });
    write();
    await relay.poll(); // 422: dropped
    await relay.poll();
    expect(calls).toBe(1);
    next = fail(503);
    write({ at: "b" });
    clock.t = 20_000;
    await relay.poll(); // 503: back off, keep it
    clock.t = 23_000;
    next = null;
    await relay.poll();
    expect(calls).toBe(3);
    next = fail(409, "stale_attempt");
    write({ at: "c" });
    clock.t = 40_000;
    await relay.poll();
    expect(relay.stopped).toBe(true);
    write({ at: "d" });
    clock.t = 60_000;
    await relay.poll();
    expect(calls).toBe(4);
  });

  it("never follows a link and stops on one", async () => {
    let calls = 0;
    const { dir, file, relay } = setup(async () => void (calls += 1));
    const target = path.join(dir, "elsewhere.json");
    writeFileSync(target, JSON.stringify(record()));
    symlinkSync(target, file);
    await relay.poll();
    expect(calls).toBe(0);
    expect(relay.stopped).toBe(true);
  });

  it("refuses a hard-linked file", async () => {
    let calls = 0;
    const { dir, file, write, relay } = setup(async () => void (calls += 1));
    write();
    linkSync(file, path.join(dir, "second-name"));
    await relay.poll();
    expect(calls).toBe(0);
    expect(relay.stopped).toBe(true);
  });
});
