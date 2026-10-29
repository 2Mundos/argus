import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import {
  chmodSync,
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  readlinkSync,
  renameSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import {
  checkpointRef,
  checkpointTaskWorkspace,
  parseCheckpointRef,
  restoreTaskWorkspace,
} from "./argus-checkpoint.js";
import { prepareTaskWorkspace, TASK_WORKSPACES_DIR, taskWorkspacePath } from "./argus-workspace.js";

const REPO = "https://git.example.test/org/repo.git";
const NODE = "node-a";

const saved = { HOME: process.env.HOME, XDG_CONFIG_HOME: process.env.XDG_CONFIG_HOME };
let home = "";
beforeAll(() => {
  home = mkdtempSync(path.join(os.tmpdir(), "argus-ckpt-home-"));
  process.env.HOME = home;
  process.env.XDG_CONFIG_HOME = home;
});
afterAll(() => {
  for (const [k, v] of Object.entries(saved)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
  rmSync(home, { recursive: true, force: true });
});

function gitEnv(): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {};
  for (const [k, v] of Object.entries(process.env)) if (!k.startsWith("GIT_")) env[k] = v;
  return {
    ...env,
    GIT_CONFIG_GLOBAL: "/dev/null",
    GIT_CONFIG_SYSTEM: "/dev/null",
    GIT_AUTHOR_NAME: "Argus Test",
    GIT_AUTHOR_EMAIL: "test@example.invalid",
    GIT_COMMITTER_NAME: "Argus Test",
    GIT_COMMITTER_EMAIL: "test@example.invalid",
    GIT_TERMINAL_PROMPT: "0",
  };
}
const git = (cwd: string, ...args: string[]) =>
  execFileSync("git", ["-c", "core.hooksPath=/dev/null", ...args], {
    cwd,
    env: gitEnv(),
    encoding: "utf8",
  }).trim();

/** Every entry under `dir` outside .git: content hashes, link targets, modes. */
function snapshot(dir: string, skipGit = true): Record<string, string> {
  const out: Record<string, string> = {};
  const walk = (p: string) => {
    const st = lstatSync(p);
    const rel = path.relative(dir, p) || ".";
    if (skipGit && rel === ".git") return;
    if (st.isSymbolicLink()) out[rel] = `link:${readlinkSync(p)}`;
    else if (st.isDirectory()) {
      out[rel] = "dir";
      for (const name of readdirSync(p)) walk(path.join(p, name));
    } else {
      const sha = createHash("sha256").update(readFileSync(p)).digest("hex");
      out[rel] = `file:${(st.mode & 0o777).toString(8)}:${sha}`;
    }
  };
  walk(dir);
  return out;
}

const dirs: string[] = [];
afterEach(() => {
  for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
});
const tmp = (prefix: string) => {
  const d = mkdtempSync(path.join(os.tmpdir(), prefix));
  dirs.push(d);
  return d;
};

interface World {
  taskId: string;
  store: string;
  groupA: string; // instance A (interrupted)
  groupB: string; // instance B (resumes), isolated from A
  wsA: string;
}

/** Instance A holds: published base, 2 unpublished commits, staged, unstaged, untracked work. */
async function world(): Promise<World> {
  const taskId = randomUUID();
  const root = tmp("argus-ckpt-");
  const store = path.join(root, "store");
  const groupA = path.join(root, "instance-a", "groups", "argus");
  const groupB = path.join(root, "instance-b", "groups", "argus");
  mkdirSync(groupA, { recursive: true });
  mkdirSync(groupB, { recursive: true });
  const prepared = await prepareTaskWorkspace({ groupDir: groupA, taskId, repositoryUrl: REPO });
  if (!prepared.ok) throw new Error(prepared.error);
  const ws = prepared.path;
  writeFileSync(path.join(ws, "base.txt"), "base\n");
  writeFileSync(path.join(ws, "tracked.txt"), "one\n");
  writeFileSync(path.join(ws, ".gitignore"), "ignored.log\n");
  git(ws, "add", ".");
  git(ws, "commit", "-q", "-m", "base");
  git(ws, "update-ref", "refs/remotes/origin/main", "HEAD"); // published
  writeFileSync(path.join(ws, "unpublished-1.txt"), "u1\n");
  git(ws, "add", ".");
  git(ws, "commit", "-q", "-m", "unpublished 1");
  writeFileSync(path.join(ws, "tracked.txt"), "one\ntwo\n");
  git(ws, "commit", "-q", "-am", "unpublished 2");
  // staged: new file + modification; unstaged: another modification; binary too
  writeFileSync(path.join(ws, "staged-new.txt"), "staged\n");
  writeFileSync(path.join(ws, "base.txt"), "base\nstaged edit\n");
  writeFileSync(path.join(ws, "blob.bin"), Buffer.from([0, 1, 2, 255, 254, 0]));
  git(ws, "add", "staged-new.txt", "base.txt", "blob.bin");
  writeFileSync(path.join(ws, "tracked.txt"), "one\ntwo\nunstaged edit\n");
  // untracked: nested file, executable, relative symlink, ignored file, a secret
  mkdirSync(path.join(ws, "src", "deep"), { recursive: true });
  writeFileSync(path.join(ws, "src", "deep", "new.ts"), "export const x = 1;\n");
  writeFileSync(path.join(ws, "run.sh"), "#!/bin/sh\n", { mode: 0o755 });
  chmodSync(path.join(ws, "run.sh"), 0o755);
  symlinkSync("src/deep/new.ts", path.join(ws, "link.ts"));
  writeFileSync(path.join(ws, "ignored.log"), "noise\n");
  writeFileSync(path.join(ws, ".env"), "TOKEN=super-secret-value\n");
  return { taskId, store, groupA, groupB, wsA: ws };
}

const ckpt = (w: World, over: Partial<Parameters<typeof checkpointTaskWorkspace>[0]> = {}) =>
  checkpointTaskWorkspace({
    groupDir: w.groupA,
    storeRoot: w.store,
    taskId: w.taskId,
    attempt: 1,
    repositoryUrl: REPO,
    nodeId: NODE,
    ...over,
  });
const restore = (w: World, over: Partial<Parameters<typeof restoreTaskWorkspace>[0]> = {}) =>
  restoreTaskWorkspace({
    groupDir: w.groupB,
    storeRoot: w.store,
    taskId: w.taskId,
    repositoryUrl: REPO,
    attempt: 2,
    nodeId: NODE,
    ...over,
  });
const taskStore = (w: World) => path.join(w.store, w.taskId);
const checkpointDir = (w: World) =>
  path.join(
    taskStore(w),
    readdirSync(taskStore(w))
      .filter((n) => /^\d{14}-/.test(n))
      .sort()
      .pop() as string,
  );
const wsB = (w: World) => taskWorkspacePath(w.groupB, w.taskId);

describe("checkpoint and restore across two isolated instances", () => {
  it("restores commits, staged, unstaged and untracked work with identical hashes", async () => {
    const w = await world();
    const before = snapshot(w.wsA);
    const headA = git(w.wsA, "rev-parse", "HEAD");
    const statusA = git(w.wsA, "status", "--porcelain=v1");
    const stagedDiffA = git(w.wsA, "diff", "--cached", "--binary");
    const unstagedDiffA = git(w.wsA, "diff", "--binary");
    const logA = git(w.wsA, "log", "--format=%H %s");

    const c = await ckpt(w);
    expect(c).toMatchObject({ ok: true, empty: false });
    // the source is never modified by a checkpoint
    expect(snapshot(w.wsA)).toEqual(before);
    expect(git(w.wsA, "rev-parse", "HEAD")).toBe(headA);

    // "interrupted": instance A is gone for B's purposes; B shares nothing but the store
    expect(existsSync(wsB(w))).toBe(false);
    const r = await restore(w);
    expect(r).toMatchObject({ ok: true, restored: true, head: headA });
    if (!r.ok || !r.restored || !c.ok || c.empty) throw new Error("unreachable");
    expect(r.manifestSha256).toBe(c.manifestSha256);

    const ws = wsB(w);
    expect(git(ws, "rev-parse", "HEAD")).toBe(headA);
    expect(git(ws, "log", "--format=%H %s")).toBe(logA);
    expect(git(ws, "symbolic-ref", "HEAD")).toBe(`refs/heads/argus/task-${w.taskId.slice(0, 8)}`);
    expect(git(ws, "config", "--get-all", "remote.origin.url")).toBe(REPO);
    expect(git(ws, "diff", "--cached", "--binary")).toBe(stagedDiffA);
    expect(git(ws, "diff", "--binary")).toBe(unstagedDiffA);

    // everything but the ignored file and the secret is byte-identical, modes included
    const after = snapshot(ws);
    const expected = { ...before };
    delete expected["ignored.log"];
    delete expected[".env"];
    expect(after).toEqual(expected);
    expect(git(ws, "status", "--porcelain=v1")).toBe(
      statusA
        .split("\n")
        .filter((l) => !l.endsWith(".env"))
        .join("\n"),
    );
    expect(readlinkSync(path.join(ws, "link.ts"))).toBe("src/deep/new.ts");

    // the restored workspace is what the next attempt's preflight expects
    const again = await prepareTaskWorkspace({
      groupDir: w.groupB,
      taskId: w.taskId,
      repositoryUrl: REPO,
    });
    expect(again).toMatchObject({ ok: true, created: false });
    // A kept its own work too: a restore never reads or moves it
    expect(snapshot(w.wsA)).toEqual(before);
  });

  it("leaves nothing for a clean or missing workspace", async () => {
    const w = await world();
    expect(await ckpt(w, { groupDir: w.groupB })).toEqual({ ok: true, empty: true }); // no workspace
    git(w.wsA, "add", "-A", "--", ".", ":!.env");
    git(w.wsA, "commit", "-q", "-m", "all in");
    git(w.wsA, "update-ref", "refs/remotes/origin/main", "HEAD"); // now published
    rmSync(path.join(w.wsA, ".env"));
    expect(await ckpt(w)).toEqual({ ok: true, empty: true });
    expect(existsSync(taskStore(w))).toBe(false);
    expect(await restore(w)).toEqual({ ok: true, restored: false, reason: "none" });
  });

  it("restores an unborn branch with only untracked and staged work", async () => {
    const taskId = randomUUID();
    const root = tmp("argus-ckpt-unborn-");
    const groupA = path.join(root, "a");
    const groupB = path.join(root, "b");
    mkdirSync(groupA);
    mkdirSync(groupB);
    const p = await prepareTaskWorkspace({ groupDir: groupA, taskId, repositoryUrl: REPO });
    if (!p.ok) throw new Error(p.error);
    writeFileSync(path.join(p.path, "a.txt"), "a\n");
    writeFileSync(path.join(p.path, "b.txt"), "b\n");
    git(p.path, "add", "a.txt");
    const w: World = { taskId, store: path.join(root, "store"), groupA, groupB, wsA: p.path };
    expect(await ckpt(w)).toMatchObject({ ok: true, empty: false });
    expect(await restore(w)).toMatchObject({ ok: true, restored: true, head: null });
    expect(git(wsB(w), "status", "--porcelain=v1")).toBe("A  a.txt\n?? b.txt");
  });
});

describe("a checkpoint that cannot be trusted blocks the resume and never discards work", () => {
  async function ready() {
    const w = await world();
    const c = await ckpt(w);
    if (!c.ok || c.empty) throw new Error("no checkpoint");
    return { w, c };
  }
  const nothingRestored = (w: World) => expect(existsSync(wsB(w))).toBe(false);

  it("rejects a file whose hash changed", async () => {
    const { w } = await ready();
    const dir = checkpointDir(w);
    chmodSync(path.join(dir, "unstaged.patch"), 0o600);
    writeFileSync(path.join(dir, "unstaged.patch"), "tampered\n");
    const r = await restore(w);
    expect(r).toMatchObject({ ok: false, reason: "env_checkpoint_invalid" });
    nothingRestored(w);
    expect(existsSync(path.join(dir, "manifest.json"))).toBe(true); // kept for forensics
  });

  it("rejects a truncated bundle and a missing file", async () => {
    const { w } = await ready();
    const dir = checkpointDir(w);
    const bundle = path.join(dir, "work.bundle");
    writeFileSync(bundle, readFileSync(bundle).subarray(0, 40));
    expect(await restore(w)).toMatchObject({ ok: false, reason: "env_checkpoint_invalid" });
    rmSync(bundle);
    expect(await restore(w)).toMatchObject({ ok: false, reason: "env_checkpoint_unavailable" });
    nothingRestored(w);
  });

  it("ignores an incomplete write (no manifest) but requires an expected checkpoint to exist", async () => {
    const { w, c } = await ready();
    // a half-written checkpoint of a crashed writer is hidden and never chosen
    mkdirSync(path.join(taskStore(w), ".tmp-20990101000000-aaaaaaaaaaaa"), { mode: 0o700 });
    mkdirSync(path.join(taskStore(w), "20990101000000-bbbbbbbbbbbb"), { mode: 0o700 });
    expect(await restore(w, { expected: { id: c.id, sha256: c.manifestSha256 } })).toMatchObject({
      ok: true,
      restored: true,
    });
    rmSync(wsB(w), { recursive: true });
    const r = await restore(w, {
      expected: { id: "20990101000000-bbbbbbbbbbbb", sha256: "0".repeat(64) },
    });
    expect(r).toMatchObject({ ok: false, reason: "env_checkpoint_unavailable" });
    nothingRestored(w);
  });

  it("requires the expected manifest digest", async () => {
    const { w, c } = await ready();
    expect(await restore(w, { expected: { id: c.id, sha256: "f".repeat(64) } })).toMatchObject({
      ok: false,
      reason: "env_checkpoint_invalid",
    });
    nothingRestored(w);
  });

  it("fails closed when the expected checkpoint is not in this node's store", async () => {
    const w = await world();
    const missing = { id: "20260101000000-aaaaaaaaaaaa", sha256: "a".repeat(64) };
    expect(await restore(w, { expected: missing })).toMatchObject({
      ok: false,
      reason: "env_checkpoint_unavailable",
    });
    nothingRestored(w);
  });

  it("refuses another node: no transport is proven", async () => {
    const { w } = await ready();
    const r = await restore(w, { nodeId: "node-b" });
    expect(r).toMatchObject({ ok: false, reason: "env_checkpoint_unavailable" });
    expect((r as { error: string }).error).toMatch(/no proven transport/);
    nothingRestored(w);
  });

  it("refuses another repository and another task", async () => {
    const { w } = await ready();
    expect(
      await restore(w, { repositoryUrl: "https://git.example.test/org/other.git" }),
    ).toMatchObject({
      ok: false,
      reason: "env_checkpoint_invalid",
    });
    // the same bytes filed under a different task id are not that task's checkpoint
    const other = randomUUID();
    mkdirSync(path.join(w.store, other), { mode: 0o700 });
    const id = path.basename(checkpointDir(w));
    execFileSync("cp", ["-a", checkpointDir(w), path.join(w.store, other, id)]);
    const r = await restore(w, { taskId: other });
    expect(r).toMatchObject({ ok: false, reason: "env_checkpoint_invalid" });
    expect(await restore(w, { taskId: "../escape" })).toMatchObject({
      ok: false,
      reason: "env_checkpoint_invalid",
    });
    expect(
      await restore(w, { repositoryUrl: "https://user:pw@git.example.test/org/repo.git" }),
    ).toMatchObject({
      ok: false,
    });
  });

  it("refuses a store or task directory open to other users, or replaced by a symlink", async () => {
    const { w } = await ready();
    chmodSync(w.store, 0o755);
    expect(await restore(w)).toMatchObject({ ok: false, reason: "env_checkpoint_invalid" });
    chmodSync(w.store, 0o700);
    chmodSync(taskStore(w), 0o755);
    expect(await restore(w)).toMatchObject({ ok: false, reason: "env_checkpoint_invalid" });
    chmodSync(taskStore(w), 0o700);
    const moved = `${taskStore(w)}-real`;
    renameSync(taskStore(w), moved);
    symlinkSync(moved, taskStore(w));
    expect(await restore(w)).toMatchObject({ ok: false, reason: "env_checkpoint_invalid" });
    nothingRestored(w);
  });

  it("rejects a manifest that points outside the checkpoint or at an unsafe path", async () => {
    const { w } = await ready();
    const mfile = path.join(checkpointDir(w), "manifest.json");
    const m = JSON.parse(readFileSync(mfile, "utf8"));
    m.untracked[0].path = "../../escape.txt";
    chmodSync(mfile, 0o600);
    writeFileSync(mfile, JSON.stringify(m));
    expect(await restore(w)).toMatchObject({ ok: false, reason: "env_checkpoint_invalid" });
    const m2 = JSON.parse(readFileSync(mfile, "utf8"));
    m2.untracked[0].path = "ok.txt";
    m2.files[0].name = "../../etc/passwd";
    writeFileSync(mfile, JSON.stringify(m2));
    expect(await restore(w)).toMatchObject({ ok: false, reason: "env_checkpoint_invalid" });
    nothingRestored(w);
    expect(existsSync(path.join(w.store, "..", "escape.txt"))).toBe(false);
  });

  it("never replaces different work in the destination", async () => {
    const { w } = await ready();
    const p = await prepareTaskWorkspace({
      groupDir: w.groupB,
      taskId: w.taskId,
      repositoryUrl: REPO,
    });
    if (!p.ok) throw new Error(p.error);
    writeFileSync(path.join(p.path, "other-work.txt"), "mine\n");
    const before = snapshot(p.path, false);
    const r = await restore(w);
    expect(r).toMatchObject({ ok: false, reason: "env_checkpoint_conflict" });
    expect(snapshot(p.path, false)).toEqual(before);
  });

  it("does not use the wrong branch: a destination on another branch is a conflict", async () => {
    const { w } = await ready();
    const p = await prepareTaskWorkspace({
      groupDir: w.groupB,
      taskId: w.taskId,
      repositoryUrl: REPO,
    });
    if (!p.ok) throw new Error(p.error);
    git(p.path, "symbolic-ref", "HEAD", "refs/heads/main");
    expect(await restore(w)).toMatchObject({ ok: false, reason: "env_checkpoint_conflict" });
    expect(git(p.path, "symbolic-ref", "HEAD")).toBe("refs/heads/main");
  });

  it("is idempotent, and swaps in over a pristine bootstrap workspace", async () => {
    const { w } = await ready();
    const p = await prepareTaskWorkspace({
      groupDir: w.groupB,
      taskId: w.taskId,
      repositoryUrl: REPO,
    });
    if (!p.ok) throw new Error(p.error);
    expect(await restore(w)).toMatchObject({ ok: true, restored: true });
    const snap = snapshot(p.path, false);
    expect(await restore(w)).toEqual({ ok: true, restored: false, reason: "already_present" });
    expect(snapshot(p.path, false)).toEqual(snap);
    expect(readdirSync(path.dirname(p.path)).filter((n) => n.startsWith("."))).toEqual([]);
  });
});

describe("leases, concurrency and limits", () => {
  it("blocks while another holder has the task's lease, and takes over an expired one", async () => {
    const w = await world();
    mkdirSync(taskStore(w), { recursive: true, mode: 0o700 });
    const lease = path.join(taskStore(w), ".lease");
    const body = (expires_at: number) => JSON.stringify({ owner: "x", nonce: "n", expires_at });
    writeFileSync(lease, body(Date.now() + 60_000), { mode: 0o600 });
    expect(await ckpt(w)).toMatchObject({ ok: false, reason: "env_checkpoint_busy" });
    expect(await restore(w)).toMatchObject({ ok: false, reason: "env_checkpoint_busy" });
    writeFileSync(lease, body(Date.now() - 1));
    expect(await ckpt(w)).toMatchObject({ ok: true, empty: false });
    expect(existsSync(lease)).toBe(false); // released
    writeFileSync(lease, "not json"); // unreadable: fail closed, never taken over
    expect(await restore(w)).toMatchObject({ ok: false, reason: "env_checkpoint_busy" });
  });

  it("serialises concurrent checkpoints and restores: no torn state, work intact", async () => {
    const w = await world();
    const results = await Promise.all([
      ckpt(w, { attempt: 1 }),
      ckpt(w, { attempt: 2 }),
      ckpt(w, { attempt: 3 }),
    ]);
    for (const r of results) {
      expect(r.ok ? true : r.reason).toSatisfy((v) => v === true || v === "env_checkpoint_busy");
    }
    expect(results.some((r) => r.ok)).toBe(true);
    const names = readdirSync(taskStore(w));
    expect(names.filter((n) => n.startsWith(".tmp-"))).toEqual([]);
    const both = await Promise.all([restore(w), restore(w)]);
    expect(both.some((r) => r.ok && r.restored)).toBe(true);
    for (const r of both)
      expect(r.ok ? true : r.reason).toSatisfy((v) => v === true || v === "env_checkpoint_busy");
    expect(git(wsB(w), "rev-parse", "HEAD")).toBe(git(w.wsA, "rev-parse", "HEAD"));
  });

  it("keeps the newest three checkpoints of a task and only that task's", async () => {
    const w = await world();
    const otherId = randomUUID();
    mkdirSync(path.join(w.store, otherId), { recursive: true, mode: 0o700 });
    writeFileSync(path.join(w.store, otherId, "sentinel"), "x");
    let t = Date.parse("2026-10-29T00:00:00Z");
    for (let i = 1; i <= 5; i++) {
      writeFileSync(path.join(w.wsA, "untracked-grow.txt"), `v${i}\n`);
      expect(await ckpt(w, { attempt: i, now: () => (t += 1000) })).toMatchObject({
        ok: true,
        empty: false,
      });
    }
    expect(readdirSync(taskStore(w)).filter((n) => /^\d{14}-/.test(n))).toHaveLength(3);
    expect(existsSync(path.join(w.store, otherId, "sentinel"))).toBe(true);
  });

  it("refuses work over the size limit and leaves no partial checkpoint", async () => {
    const w = await world();
    const r = await ckpt(w, { maxBytes: 100 });
    expect(r).toMatchObject({ ok: false, reason: "env_checkpoint_limit" });
    expect(readdirSync(taskStore(w)).filter((n) => n !== ".lease")).toEqual([]);
  });

  it("refuses to checkpoint a workspace on another branch, with another origin, or a symlink", async () => {
    const w = await world();
    git(w.wsA, "remote", "set-url", "origin", "https://git.example.test/org/other.git");
    expect(await ckpt(w)).toMatchObject({ ok: false, reason: "env_checkpoint_invalid" });
    git(w.wsA, "remote", "set-url", "origin", REPO);
    git(w.wsA, "symbolic-ref", "HEAD", "refs/heads/elsewhere");
    expect(await ckpt(w)).toMatchObject({ ok: false, reason: "env_checkpoint_invalid" });
    git(w.wsA, "symbolic-ref", "HEAD", `refs/heads/argus/task-${w.taskId.slice(0, 8)}`);
    writeFileSync(path.join(w.wsA, "evil"), "x");
    rmSync(path.join(w.wsA, "evil"));
    symlinkSync("/etc/passwd", path.join(w.wsA, "abs-link"));
    expect(await ckpt(w)).toMatchObject({ ok: false, reason: "env_checkpoint_invalid" });
    expect(
      existsSync(taskStore(w)) ? readdirSync(taskStore(w)).filter((n) => /^\d{14}-/.test(n)) : [],
    ).toEqual([]);
  });
});

describe("confidentiality", () => {
  it("keeps secrets out of the store, lists their names only, and uses private modes", async () => {
    const w = await world();
    writeFileSync(path.join(w.wsA, "tracked.txt"), "one\ntwo\nunstaged\n");
    const c = await ckpt(w);
    expect(c).toMatchObject({ ok: true, empty: false });
    const dir = checkpointDir(w);
    for (const name of readdirSync(dir)) {
      expect(readFileSync(path.join(dir, name)).includes("super-secret-value")).toBe(false);
      expect(statSync(path.join(dir, name)).mode & 0o077).toBe(0);
    }
    expect(statSync(dir).mode & 0o077).toBe(0);
    expect(statSync(taskStore(w)).mode & 0o077).toBe(0);
    const m = JSON.parse(readFileSync(path.join(dir, "manifest.json"), "utf8"));
    expect(m.skipped_sensitive).toEqual([".env"]);
    expect(JSON.stringify(m)).not.toContain("super-secret-value");
    await restore(w);
    expect(existsSync(path.join(wsB(w), ".env"))).toBe(false);
    // the store holds no credential and sits outside every instance directory
    expect(path.relative(w.groupA, w.store).startsWith("..")).toBe(true);
    expect(w.store.includes(TASK_WORKSPACES_DIR)).toBe(false);
  });
});

describe("checkpoint references", () => {
  it("round-trips and rejects anything else", () => {
    const id = "20261029125500-0123456789ab";
    const sha = "c".repeat(64);
    expect(parseCheckpointRef(checkpointRef(id, sha))).toEqual({ id, sha256: sha });
    for (const bad of [
      "",
      null,
      undefined,
      "/workspace/x",
      `checkpoint:../x:${sha}`,
      `checkpoint:${id}:zz`,
    ])
      expect(parseCheckpointRef(bad)).toBeNull();
  });
});
