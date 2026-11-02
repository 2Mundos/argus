import { execFileSync } from "node:child_process";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  renameSync,
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
const TASK = "0b9e2d1c-5a4f-4e3b-9c8d-7f6e5d4c3b2a";
const dirs: string[] = [];
afterEach(() => {
  for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
});

function temp(prefix: string): string {
  const d = mkdtempSync(path.join(os.tmpdir(), prefix));
  dirs.push(d);
  return d;
}

/** A group directory holding the task's workspace with a `.git` directory. */
function group(): { root: string; signals: string } {
  const root = temp("argus-signals-");
  const ws = path.join(root, "task-workspaces", TASK);
  mkdirSync(path.join(ws, ".git"), { recursive: true });
  return { root, signals: path.join(ws, ".git", "argus") };
}

const record = { on: "ci", ref: "#412", since: "2026-11-02T09:54:00.000Z" };

function writeWaiting(signals: string, mtimeMs = NOW - 60_000): string {
  mkdirSync(signals, { recursive: true });
  const file = path.join(signals, "waiting.json");
  writeFileSync(file, JSON.stringify(record));
  utimesSync(file, new Date(mtimeMs), new Date(mtimeMs));
  return file;
}

describe("markSteer", () => {
  it("writes the interrupt marker the helper reads", async () => {
    const { root, signals } = group();
    expect(await markSteer(root, TASK, new Date(NOW))).toBe(true);
    const marker = JSON.parse(readFileSync(path.join(signals, "interrupt"), "utf8"));
    expect(marker).toEqual({ reason: "steer", at: "2026-11-02T10:00:00.000Z" });
  });

  it.each([
    ["the signal directory", (ws: string) => path.join(ws, ".git", "argus")],
    ["the .git directory", (ws: string) => path.join(ws, ".git")],
    ["the task workspace", (ws: string) => ws],
  ])("never writes through a symlinked %s", async (_, pick) => {
    const { root } = group();
    const outside = temp("argus-outside-");
    const link = pick(path.join(root, "task-workspaces", TASK));
    rmSync(link, { recursive: true, force: true });
    symlinkSync(outside, link);
    expect(await markSteer(root, TASK, new Date(NOW))).toBe(false);
    expect(execFileSync("find", [outside, "-type", "f"], { encoding: "utf8" })).toBe("");
  });

  it("does nothing without a .git directory or with an invalid task id", async () => {
    const root = temp("argus-nogit-");
    mkdirSync(path.join(root, "task-workspaces", TASK), { recursive: true });
    expect(await markSteer(root, TASK, new Date(NOW))).toBe(false);
    expect(await markSteer(group().root, "../escape", new Date(NOW))).toBe(false);
  });
});

describe("readWaiting", () => {
  it("reads a fresh record and ignores a stale one", async () => {
    const { root, signals } = group();
    const file = writeWaiting(signals);
    expect(await readWaiting(root, TASK, NOW)).toEqual(record);
    utimesSync(file, new Date(NOW - 16 * 60_000), new Date(NOW - 16 * 60_000));
    expect(await readWaiting(root, TASK, NOW)).toBeNull();
  });

  it("refuses a symlinked record or directory", async () => {
    const { root, signals } = group();
    const elsewhere = temp("argus-elsewhere-");
    writeWaiting(elsewhere);
    mkdirSync(signals);
    symlinkSync(path.join(elsewhere, "waiting.json"), path.join(signals, "waiting.json"));
    expect(await readWaiting(root, TASK, NOW)).toBeNull();
    rmSync(signals, { recursive: true });
    symlinkSync(elsewhere, signals);
    expect(await readWaiting(root, TASK, NOW)).toBeNull();
  });

  it("refuses a FIFO without blocking", async () => {
    const { root, signals } = group();
    mkdirSync(signals);
    execFileSync("mkfifo", [path.join(signals, "waiting.json")]);
    expect(await readWaiting(root, TASK, NOW)).toBeNull();
  });

  it("reads through the handle it opened, not a path swapped afterwards", async () => {
    // Swapping the workspace after the walk cannot be timed in a test; renaming it away
    // and leaving a symlink shows that each component is opened without following one.
    const { root, signals } = group();
    writeWaiting(signals);
    const ws = path.join(root, "task-workspaces", TASK);
    renameSync(ws, `${ws}.real`);
    symlinkSync(`${ws}.real`, ws);
    expect(await readWaiting(root, TASK, NOW)).toBeNull();
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
