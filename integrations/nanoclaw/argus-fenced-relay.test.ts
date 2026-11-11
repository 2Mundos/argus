import {
  appendFileSync,
  linkSync,
  mkdirSync,
  mkdtempSync,
  renameSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { createFencedReader } from "./argus-fenced-relay.js";

const dirs: string[] = [];
afterEach(() => {
  for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
});
const tmp = () => {
  const d = mkdtempSync(path.join(os.tmpdir(), "argus-fenced-"));
  dirs.push(d);
  return d;
};

describe("createFencedReader", () => {
  it("returns complete lines once and holds a partial line until its newline", async () => {
    const file = path.join(tmp(), "r.jsonl");
    const r = createFencedReader({ file });
    expect((await r.read()).lines).toEqual([]); // no file yet
    writeFileSync(file, 'a\nb\n{"half":');
    expect((await r.read()).lines).toEqual(["a", "b"]);
    expect((await r.read()).lines).toEqual([]);
    appendFileSync(file, "1}\n");
    expect((await r.read()).lines).toEqual(['{"half":1}']);
  });

  it("drops an over-long line, including one that arrives in pieces, and keeps the next", async () => {
    const file = path.join(tmp(), "r.jsonl");
    const r = createFencedReader({ file, maxLine: 10 });
    writeFileSync(file, `${"x".repeat(30)}\nok\n`);
    expect(await r.read()).toMatchObject({ lines: ["ok"], oversized: 1 });
    appendFileSync(file, "y".repeat(25)); // runaway line, no newline yet
    expect(await r.read()).toMatchObject({ lines: [], oversized: 1 });
    appendFileSync(file, "y".repeat(25)); // still the same line: not counted twice
    expect(await r.read()).toMatchObject({ lines: [], oversized: 0 });
    appendFileSync(file, "\nnext\n");
    expect((await r.read()).lines).toEqual(["next"]);
  });

  it("reads at most readMax bytes per call and reports what is left", async () => {
    const file = path.join(tmp(), "r.jsonl");
    const r = createFencedReader({ file, readMax: 8 });
    writeFileSync(file, "aaa\nbbb\nccc\nddd\n");
    expect((await r.read()).lines).toEqual(["aaa", "bbb"]);
    expect(r.unread).toBe(8);
    expect((await r.read()).lines).toEqual(["ccc", "ddd"]);
    expect(r.unread).toBe(0);
  });

  it("starts over when the file shrank or was replaced", async () => {
    const dir = tmp();
    const file = path.join(dir, "r.jsonl");
    const r = createFencedReader({ file });
    writeFileSync(file, "one\ntwo\n");
    expect((await r.read()).lines).toEqual(["one", "two"]);
    writeFileSync(file, "x\n"); // truncated and rewritten
    expect(await r.read()).toMatchObject({ lines: ["x"], restarted: true });
    writeFileSync(path.join(dir, "new"), "y\nz\nw\n"); // rotated: a different file at the path
    renameSync(path.join(dir, "new"), file);
    expect(await r.read()).toMatchObject({ lines: ["y", "z", "w"], restarted: true });
  });

  it("refuses a symlink and stays refused", async () => {
    const dir = tmp();
    writeFileSync(path.join(dir, "secret"), "leak\n");
    const file = path.join(dir, "r.jsonl");
    symlinkSync(path.join(dir, "secret"), file);
    const r = createFencedReader({ file });
    expect((await r.read()).lines).toEqual([]);
    expect(r.refused).toBe("symlink");
    rmSync(file);
    writeFileSync(file, "late\n");
    expect((await r.read()).lines).toEqual([]); // the caller stops; the reader does not recover
  });

  it("refuses a directory and a hard-linked file", async () => {
    const dir = tmp();
    const asDir = path.join(dir, "d.jsonl");
    mkdirSync(asDir);
    const a = createFencedReader({ file: asDir });
    await a.read();
    expect(a.refused).toBe("not_a_plain_file");

    writeFileSync(path.join(dir, "orig"), "x\n");
    const linked = path.join(dir, "l.jsonl");
    linkSync(path.join(dir, "orig"), linked);
    const b = createFencedReader({ file: linked });
    expect((await b.read()).lines).toEqual([]);
    expect(b.refused).toBe("not_a_plain_file");
  });
});
