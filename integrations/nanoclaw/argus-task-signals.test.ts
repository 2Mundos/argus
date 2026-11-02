import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import {
  createWaitingRelay,
  markSteer,
  parseWaiting,
  readWaiting,
  waitingMessage,
} from "./argus-task-signals.js";

const NOW = Date.parse("2026-11-02T10:00:00Z");
const dirs: string[] = [];
afterEach(() => {
  for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
});

function workspace(): string {
  const d = mkdtempSync(path.join(os.tmpdir(), "argus-signals-"));
  dirs.push(d);
  mkdirSync(path.join(d, ".git"));
  return d;
}

const record = { on: "ci", ref: "#412", since: "2026-11-02T09:54:00.000Z" };

describe("markSteer", () => {
  it("writes the interrupt marker the helper reads", async () => {
    const ws = workspace();
    expect(await markSteer(ws, new Date(NOW))).toBe(true);
    const marker = JSON.parse(readFileSync(path.join(ws, ".git", "argus", "interrupt"), "utf8"));
    expect(marker).toEqual({ reason: "steer", at: "2026-11-02T10:00:00.000Z" });
  });

  it("never writes through a symlinked signal directory", async () => {
    const ws = workspace();
    const outside = mkdtempSync(path.join(os.tmpdir(), "argus-outside-"));
    dirs.push(outside);
    symlinkSync(outside, path.join(ws, ".git", "argus"));
    expect(await markSteer(ws, new Date(NOW))).toBe(false);
    expect(() => readFileSync(path.join(outside, "interrupt"))).toThrow();
  });

  it("does nothing without a .git directory", async () => {
    const ws = mkdtempSync(path.join(os.tmpdir(), "argus-nogit-"));
    dirs.push(ws);
    expect(await markSteer(ws, new Date(NOW))).toBe(false);
  });
});

describe("readWaiting", () => {
  it("reads a fresh record and ignores a stale or symlinked one", async () => {
    const ws = workspace();
    const dir = path.join(ws, ".git", "argus");
    mkdirSync(dir);
    const file = path.join(dir, "waiting.json");
    writeFileSync(file, JSON.stringify(record));
    utimesSync(file, new Date(NOW - 60_000), new Date(NOW - 60_000));
    expect(await readWaiting(ws, NOW)).toEqual(record);
    utimesSync(file, new Date(NOW - 16 * 60_000), new Date(NOW - 16 * 60_000));
    expect(await readWaiting(ws, NOW)).toBeNull();
    rmSync(file);
    const target = path.join(ws, "elsewhere.json");
    writeFileSync(target, JSON.stringify(record));
    symlinkSync(target, file);
    expect(await readWaiting(ws, NOW)).toBeNull();
  });

  it.each([
    [{ ...record, on: "deploy" }],
    [{ ...record, ref: "x".repeat(201) }],
    [{ ...record, ref: "line\nbreak" }],
    [{ ...record, since: "soon" }],
    [{ ...record, since: "2026-11-02T11:00:00Z" }],
    [[record]],
  ])("refuses %j", (raw) => {
    expect(parseWaiting(raw, NOW)).toBeNull();
  });

  it("describes the wait", () => {
    expect(waitingMessage({ on: "review", ref: "#7", since: record.since })).toBe(
      "Waiting on review for #7",
    );
  });
});

describe("createWaitingRelay", () => {
  it("posts a new wait, then again only after five minutes or a change", () => {
    const relay = createWaitingRelay();
    const w = parseWaiting(record, NOW)!;
    expect(relay.due(w, NOW)).toBe(true);
    relay.posted(w, NOW);
    expect(relay.due(w, NOW + 60_000)).toBe(false);
    expect(relay.due(w, NOW + 5 * 60_000)).toBe(true);
    expect(relay.due({ ...w, on: "review" }, NOW + 60_000)).toBe(true);
    expect(relay.due(null, NOW + 60_000)).toBe(false);
    expect(relay.due(w, NOW + 61_000)).toBe(true); // a wait that ended and started again
  });
});
