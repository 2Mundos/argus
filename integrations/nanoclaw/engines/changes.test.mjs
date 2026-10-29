import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import {
  boundPatch,
  CUT_MARK,
  cutAtLine,
  MAX_FILE_PATCH_BYTES,
  MAX_PATCH_BYTES,
  parseNameStatus,
  parseNumstat,
  snapshotChanges,
  startChanges,
} from "./changes.mjs";

const git = (cwd, ...args) =>
  execFileSync("git", ["-C", cwd, "-c", "user.name=t", "-c", "user.email=t@t", ...args], {
    encoding: "utf8",
    env: { ...process.env, GIT_CONFIG_GLOBAL: "/dev/null", GIT_CONFIG_NOSYSTEM: "1" },
  });

/** A workspace with an origin whose main has one commit, on branch argus/task-x. */
function workspace() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "argus-changes-"));
  const origin = path.join(root, "origin.git");
  const seed = path.join(root, "seed");
  const ws = path.join(root, "ws");
  fs.mkdirSync(seed);
  git(seed, "init", "-q", "-b", "main");
  fs.writeFileSync(path.join(seed, "a.txt"), "one\ntwo\n");
  fs.writeFileSync(path.join(seed, "old.txt"), "moved\n".repeat(20));
  git(seed, "add", ".");
  git(seed, "commit", "-q", "-m", "seed");
  git(root, "clone", "-q", "--bare", seed, origin);
  git(root, "clone", "-q", origin, ws);
  git(ws, "switch", "-q", "-c", "argus/task-x");
  return { root, ws };
}

test("name-status and numstat -z parse renames and binaries", () => {
  assert.deepEqual(
    [...parseNameStatus("M\0a.txt\0R100\0old.txt\0new.txt\0A\0b.bin\0")],
    [
      ["a.txt", "M"],
      ["new.txt", "R"],
      ["b.bin", "A"],
    ],
  );
  const nums = parseNumstat("1\t2\ta.txt\0" + "0\t0\t\0old.txt\0new.txt\0" + "-\t-\tb.bin\0");
  assert.deepEqual(nums.get("a.txt"), { added: 1, deleted: 2, binary: false });
  assert.deepEqual(nums.get("new.txt"), { added: 0, deleted: 0, binary: false });
  assert.deepEqual(nums.get("b.bin"), { added: null, deleted: null, binary: true });
});

test("patches are cut after a whole line, per file and in total", () => {
  assert.deepEqual(cutAtLine("short\n", 100), ["short\n", false]);
  const [cut, did] = cutAtLine(
    "+aaaa\n+bbbb\n+cccc\n+dddd\n+eeee\n",
    12 + Buffer.byteLength(CUT_MARK),
  );
  assert.ok(did);
  assert.equal(cut, `+aaaa\n+bbbb\n${CUT_MARK}`);
  const big = `diff --git a/x b/x\n${"+".padEnd(99, "x")}\n`.repeat(1);
  const huge = `diff --git a/y b/y\n${`+${"y".repeat(98)}\n`.repeat(2000)}`;
  const [patch, wasCut] = boundPatch(big + huge);
  assert.ok(wasCut && patch.startsWith(big));
  assert.ok(Buffer.byteLength(patch) <= big.length + MAX_FILE_PATCH_BYTES);
  const many = `diff --git a/z b/z\n${"+z\n".repeat(1000)}`.repeat(200);
  assert.ok(Buffer.byteLength(boundPatch(many)[0]) <= MAX_PATCH_BYTES);
});

test("a snapshot shows commits, edits, renames and untracked files against the merge base", () => {
  const { root, ws } = workspace();
  try {
    fs.writeFileSync(path.join(ws, "a.txt"), "one\nTWO\n");
    git(ws, "commit", "-qam", "edit a");
    git(ws, "mv", "old.txt", "new.txt");
    fs.writeFileSync(path.join(ws, "fresh.txt"), "hi\n");
    fs.mkdirSync(path.join(ws, ".argus-delegate"));
    fs.writeFileSync(path.join(ws, ".argus-delegate", "stdout.log"), "noise");
    const snap = snapshotChanges(ws, { now: () => new Date("2026-10-29T12:00:00Z") });
    assert.ok(snap);
    assert.equal(snap.branch, "argus/task-x");
    assert.equal(snap.base.ref, "origin/main");
    assert.equal(snap.base.sha, git(ws, "rev-parse", "origin/main").trim());
    assert.equal(snap.head, git(ws, "rev-parse", "HEAD").trim());
    assert.deepEqual(
      snap.commits.map((c) => c.subject),
      ["edit a"],
    );
    const byPath = Object.fromEntries(snap.files.map((f) => [f.path, f]));
    assert.deepEqual(byPath["a.txt"], {
      path: "a.txt",
      status: "M",
      added: 1,
      deleted: 1,
      binary: false,
    });
    assert.equal(byPath["new.txt"].status, "R");
    assert.deepEqual(snap.untracked, [{ path: "fresh.txt", bytes: 3 }]); // not .argus-delegate
    assert.match(snap.patch, /^diff --git a\/a.txt/m);
    assert.match(snap.patch, /^\+TWO$/m);
    assert.deepEqual(snap.truncated, { files: false, patch: false });
    assert.equal(snap.at, "2026-10-29T12:00:00.000Z");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("an unborn branch with no fetch diffs against the empty tree", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "argus-changes-"));
  try {
    git(root, "init", "-q", "-b", "argus/task-y");
    fs.writeFileSync(path.join(root, "x.txt"), "x\n");
    git(root, "add", "x.txt");
    const snap = snapshotChanges(root);
    assert.equal(snap.base, null);
    assert.equal(snap.head, null);
    assert.deepEqual(snap.commits, []);
    assert.deepEqual(
      snap.files.map((f) => [f.path, f.status]),
      [["x.txt", "A"]],
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("worker-controlled diff drivers, textconv and pagers never run", () => {
  const { root, ws } = workspace();
  const marker = path.join(root, "pwned");
  try {
    const evil = `sh -c 'touch ${marker}'`;
    git(ws, "config", "diff.external", evil);
    git(ws, "config", "diff.evil.textconv", evil);
    git(ws, "config", "core.pager", evil);
    git(ws, "config", "core.fsmonitor", evil);
    fs.writeFileSync(path.join(ws, ".gitattributes"), "*.txt diff=evil\n");
    fs.writeFileSync(path.join(ws, "a.txt"), "changed\n");
    const snap = snapshotChanges(ws);
    assert.ok(snap && snap.files.some((f) => f.path === "a.txt"));
    assert.equal(fs.existsSync(marker), false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("secrets in the patch, paths and subjects are masked", () => {
  const { root, ws } = workspace();
  const key = `ghp_${"a".repeat(36)}`;
  try {
    fs.writeFileSync(path.join(ws, "a.txt"), `token=${key}\n`);
    git(ws, "commit", "-qam", `leak ${key}`);
    const snap = snapshotChanges(ws, { secrets: ["exact-secret-value"] });
    const text = JSON.stringify(snap);
    assert.equal(text.includes(key), false);
    assert.match(snap.patch, /\[redacted\]/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("not a git worktree gives no snapshot, and a failing git never throws", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "argus-changes-"));
  try {
    assert.equal(snapshotChanges(dir), null);
    const boom = () => {
      throw new Error("spawn failed");
    };
    assert.equal(snapshotChanges(dir, { run: boom }), null);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("startChanges writes the file whole, only on change, and once more at stop", () => {
  const { root, ws } = workspace();
  const file = path.join(root, "argus-task-changes.json");
  try {
    const c = startChanges({ cwd: ws, file, taskId: "t-1", attempt: 2, intervalMs: 60_000 });
    c.tick();
    const first = JSON.parse(fs.readFileSync(file, "utf8"));
    assert.equal(first.v, 1);
    assert.equal(first.task_id, "t-1");
    assert.equal(first.attempt, 2);
    fs.rmSync(file);
    c.tick(); // nothing changed
    assert.equal(fs.existsSync(file), false);
    fs.writeFileSync(path.join(ws, "a.txt"), "new\n");
    c.tick();
    assert.ok(fs.existsSync(file));
    fs.rmSync(file);
    c.stop(); // the final snapshot is always written
    assert.ok(fs.existsSync(file));
    assert.deepEqual(
      fs.readdirSync(root).filter((n) => n.endsWith(".tmp")),
      [],
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
