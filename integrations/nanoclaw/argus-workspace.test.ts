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
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import {
  maxWorkspacesFromEnv,
  prepareTaskWorkspace,
  TASK_WORKSPACES_DIR,
  taskBranch,
  taskWorkspaceContainerPath,
  taskWorkspacePath,
} from "./argus-workspace.js";

const REPO = "https://git.example.test/org/repo.git";

// Isolate git from the developer's config and hooks: the module strips GIT_* and
// reads $HOME/.gitconfig, so point HOME at an empty directory; fixture commands
// below also pin GIT_CONFIG_GLOBAL/SYSTEM to /dev/null and a test identity.
const saved = { HOME: process.env.HOME, XDG_CONFIG_HOME: process.env.XDG_CONFIG_HOME };
let home = "";
beforeAll(() => {
  home = mkdtempSync(path.join(os.tmpdir(), "argus-ws-home-"));
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

/** Every entry under `dir` (not following symlinks): content hashes, link targets, modes. */
function snapshot(dir: string): Record<string, string> {
  const out: Record<string, string> = {};
  const walk = (p: string) => {
    const st = lstatSync(p);
    const rel = path.relative(dir, p) || ".";
    if (st.isSymbolicLink()) out[rel] = `link:${readlinkSync(p)}`;
    else if (st.isDirectory()) {
      out[rel] = `dir:${(st.mode & 0o7777).toString(8)}`;
      for (const name of readdirSync(p)) walk(path.join(p, name));
    } else {
      const sha = createHash("sha256").update(readFileSync(p)).digest("hex");
      out[rel] = `file:${(st.mode & 0o7777).toString(8)}:${sha}`;
    }
  };
  walk(dir);
  return out;
}

const dirs: string[] = [];
afterEach(() => {
  for (const d of dirs.splice(0)) {
    // A read-only fixture must be writable again to be removed.
    if (existsSync(d)) chmodSync(d, 0o700);
    rmSync(d, { recursive: true, force: true });
  }
});
const tmp = (prefix: string) => {
  const d = mkdtempSync(path.join(os.tmpdir(), prefix));
  dirs.push(d);
  return d;
};

const prepare = (groupDir: string | null, taskId: string, extra: { repositoryUrl?: string } = {}) =>
  prepareTaskWorkspace({ groupDir, taskId, repositoryUrl: extra.repositoryUrl ?? REPO });

describe("task workspace paths", () => {
  it("keys the path and branch by a canonical task id and refuses anything else", () => {
    const id = "0f1e2d3c-4b5a-4968-8778-695a4b3c2d1e";
    expect(taskWorkspacePath("/g", id)).toBe(`/g/${TASK_WORKSPACES_DIR}/${id}`);
    expect(taskWorkspaceContainerPath(id)).toBe(`/workspace/agent/task-workspaces/${id}`);
    expect(taskBranch(id)).toBe("argus/task-0f1e2d3c");
    for (const bad of ["../escape", "t1", id.toUpperCase(), `${id}/..`, ""])
      expect(() => taskWorkspacePath("/g", bad)).toThrow(/canonical lowercase UUID/);
  });

  it("reads the workspace cap from ARGUS_TASK_WORKSPACE_MAX, default 200", () => {
    vi.stubEnv("ARGUS_TASK_WORKSPACE_MAX", "");
    expect(maxWorkspacesFromEnv()).toBe(200);
    vi.stubEnv("ARGUS_TASK_WORKSPACE_MAX", "7");
    expect(maxWorkspacesFromEnv()).toBe(7);
    vi.stubEnv("ARGUS_TASK_WORKSPACE_MAX", "-1");
    expect(maxWorkspacesFromEnv()).toBe(200);
    vi.unstubAllEnvs();
  });
});

describe("prepareTaskWorkspace", () => {
  it("creates an unborn repository on the task branch with origin pinned, mode 0700", async () => {
    const group = tmp("argus-ws-group-");
    const id = randomUUID();
    const r = await prepare(group, id);
    expect(r).toEqual({
      ok: true,
      path: path.join(group, TASK_WORKSPACES_DIR, id),
      containerPath: `/workspace/agent/task-workspaces/${id}`,
      branch: `argus/task-${id.slice(0, 8)}`,
      created: true,
    });
    if (!r.ok) return;
    expect(statSync(r.path).mode & 0o777).toBe(0o700);
    expect(git(r.path, "symbolic-ref", "HEAD")).toBe(`refs/heads/argus/task-${id.slice(0, 8)}`);
    expect(() => git(r.path, "rev-parse", "--verify", "-q", "HEAD")).toThrow(); // unborn
    expect(git(r.path, "config", "--get", "remote.origin.url")).toBe(REPO);
    expect(readdirSync(path.join(group, TASK_WORKSPACES_DIR))).toEqual([id]);
  });

  it("gives a later attempt the same workspace and leaves its work in progress intact", async () => {
    const group = tmp("argus-ws-group-");
    const id = randomUUID();
    const first = await prepare(group, id);
    if (!first.ok) throw new Error(first.error);
    const untouched = snapshot(first.path);
    const second = await prepare(group, id);
    expect(second).toEqual({ ...first, created: false });
    expect(snapshot(first.path)).toEqual(untouched);

    // Work in progress: a commit on the task branch, a staged and an untracked file.
    writeFileSync(path.join(first.path, "done.txt"), "committed\n");
    git(first.path, "add", "done.txt");
    git(first.path, "commit", "-q", "-m", "wip");
    writeFileSync(path.join(first.path, "staged.txt"), "staged\n");
    git(first.path, "add", "staged.txt");
    writeFileSync(path.join(first.path, "notes.txt"), "untracked\n");
    const head = git(first.path, "rev-parse", "HEAD");
    const wip = snapshot(first.path);
    expect(await prepare(group, id)).toMatchObject({ ok: true, created: false });
    expect(snapshot(first.path)).toEqual(wip);
    expect(git(first.path, "rev-parse", "HEAD")).toBe(head);
  });

  it("lets the worker fetch the pinned origin from a real remote", async () => {
    const base = tmp("argus-ws-remote-");
    const seed = path.join(base, "seed");
    const bare = path.join(base, "remote.git");
    mkdirSync(seed);
    git(seed, "init", "-q", "-b", "main");
    writeFileSync(path.join(seed, "README.md"), "seed\n");
    git(seed, "add", "README.md");
    git(seed, "commit", "-q", "-m", "seed");
    git(base, "clone", "-q", "--bare", seed, bare);
    const url = `file://${bare}`;
    const group = tmp("argus-ws-group-");
    const r = await prepare(group, randomUUID(), { repositoryUrl: url });
    if (!r.ok) throw new Error(r.error);
    git(r.path, "fetch", "-q", "origin");
    expect(git(r.path, "rev-parse", "origin/main")).toBe(git(seed, "rev-parse", "HEAD"));
    expect(git(r.path, "symbolic-ref", "HEAD")).toBe(`refs/heads/${r.branch}`);
  });

  it("refuses a credential-bearing repository URL and never writes it to disk", async () => {
    const group = tmp("argus-ws-group-");
    const secret = "tok3n-not-a-real-secret";
    const r = await prepare(group, randomUUID(), {
      repositoryUrl: `https://worker:${secret}@git.example.test/org/repo.git`,
    });
    expect(r).toMatchObject({ ok: false, reason: "env_workspace_remote_mismatch" });
    expect(JSON.stringify(r)).not.toContain(secret);
    expect(readdirSync(group)).toEqual([]);
  });

  it("does not echo credentials an existing origin carries", async () => {
    const group = tmp("argus-ws-group-");
    const id = randomUUID();
    const r = await prepare(group, id);
    if (!r.ok) throw new Error(r.error);
    const secret = "tok3n-not-a-real-secret";
    git(
      r.path,
      "remote",
      "set-url",
      "origin",
      `https://worker:${secret}@git.example.test/org/repo.git`,
    );
    const before = snapshot(group);
    const again = await prepare(group, id);
    expect(again).toEqual({
      ok: false,
      reason: "env_workspace_remote_mismatch",
      error: "origin carries credentials",
    });
    expect(snapshot(group)).toEqual(before);
  });

  describe("fails closed with a typed reason and touches nothing", () => {
    const failsWith = async (
      group: string | null,
      id: string,
      reason: string,
      watch: string[],
      opts: { repositoryUrl?: string; maxWorkspaces?: number } = {},
    ) => {
      const before = watch.map(snapshot);
      const r = await prepareTaskWorkspace({
        groupDir: group,
        taskId: id,
        repositoryUrl: "repositoryUrl" in opts ? opts.repositoryUrl : REPO,
        maxWorkspaces: opts.maxWorkspaces,
      });
      expect(r).toMatchObject({ ok: false, reason });
      expect(watch.map(snapshot)).toEqual(before);
      return r;
    };

    it("env_workspace_remote_mismatch: origin re-pointed", async () => {
      const group = tmp("argus-ws-group-");
      const id = randomUUID();
      const r = await prepare(group, id);
      if (!r.ok) throw new Error(r.error);
      git(r.path, "remote", "set-url", "origin", "https://git.example.test/org/other.git");
      await failsWith(group, id, "env_workspace_remote_mismatch", [group]);
    });

    it("env_workspace_remote_mismatch: origin removed", async () => {
      const group = tmp("argus-ws-group-");
      const id = randomUUID();
      const r = await prepare(group, id);
      if (!r.ok) throw new Error(r.error);
      git(r.path, "remote", "remove", "origin");
      await failsWith(group, id, "env_workspace_remote_mismatch", [group]);
    });

    it("env_workspace_branch_mismatch: another branch checked out", async () => {
      const group = tmp("argus-ws-group-");
      const id = randomUUID();
      const r = await prepare(group, id);
      if (!r.ok) throw new Error(r.error);
      writeFileSync(path.join(r.path, "a.txt"), "a\n");
      git(r.path, "add", "a.txt");
      git(r.path, "commit", "-q", "-m", "a");
      git(r.path, "checkout", "-q", "-b", "other");
      await failsWith(group, id, "env_workspace_branch_mismatch", [group]);
    });

    it("env_workspace_invalid: a plain directory, not a git repository", async () => {
      const group = tmp("argus-ws-group-");
      const id = randomUUID();
      const ws = taskWorkspacePath(group, id);
      mkdirSync(ws, { recursive: true });
      writeFileSync(path.join(ws, "keep.txt"), "mine\n");
      await failsWith(group, id, "env_workspace_invalid", [group]);
    });

    it("env_workspace_invalid: the workspace is a symlink", async () => {
      const group = tmp("argus-ws-group-");
      const outside = tmp("argus-ws-outside-");
      git(outside, "init", "-q");
      git(outside, "remote", "add", "origin", REPO);
      const id = randomUUID();
      mkdirSync(path.join(group, TASK_WORKSPACES_DIR));
      symlinkSync(outside, taskWorkspacePath(group, id));
      await failsWith(group, id, "env_workspace_invalid", [group, outside]);
    });

    it("env_workspace_invalid: the workspaces root is a symlink to elsewhere", async () => {
      const group = tmp("argus-ws-group-");
      const outside = tmp("argus-ws-outside-");
      writeFileSync(path.join(outside, "keep.txt"), "mine\n");
      symlinkSync(outside, path.join(group, TASK_WORKSPACES_DIR));
      await failsWith(group, randomUUID(), "env_workspace_invalid", [group, outside]);
    });

    it.skipIf(process.getuid?.() === 0)(
      "env_workspace_permission: the group directory is read-only",
      async () => {
        const group = tmp("argus-ws-group-");
        writeFileSync(path.join(group, "keep.txt"), "mine\n");
        chmodSync(group, 0o500);
        try {
          await failsWith(group, randomUUID(), "env_workspace_permission", [group]);
        } finally {
          chmodSync(group, 0o700);
        }
      },
    );

    it("env_workspace_unconfigured: no group directory or no repository URL", async () => {
      await failsWith(null, randomUUID(), "env_workspace_unconfigured", []);
      const group = tmp("argus-ws-group-");
      await failsWith(group, randomUUID(), "env_workspace_unconfigured", [group], {
        repositoryUrl: undefined,
      });
      expect(readdirSync(group)).toEqual([]);
    });

    it("env_workspace_limit: a new workspace over the cap; an existing one still passes", async () => {
      const group = tmp("argus-ws-group-");
      const [a, b] = [randomUUID(), randomUUID()];
      for (const id of [a, b])
        expect(
          await prepareTaskWorkspace({
            groupDir: group,
            taskId: id,
            repositoryUrl: REPO,
            maxWorkspaces: 2,
          }),
        ).toMatchObject({ ok: true, created: true });
      await failsWith(group, randomUUID(), "env_workspace_limit", [group], { maxWorkspaces: 2 });
      expect(
        await prepareTaskWorkspace({
          groupDir: group,
          taskId: a,
          repositoryUrl: REPO,
          maxWorkspaces: 2,
        }),
      ).toMatchObject({ ok: true, created: false });
    });
  });

  it("keeps concurrent prepares of different tasks apart", async () => {
    const group = tmp("argus-ws-group-");
    const ids = Array.from({ length: 8 }, () => randomUUID());
    const results = await Promise.all(ids.map((id) => prepare(group, id)));
    expect(results.every((r) => r.ok && r.created)).toBe(true);
    expect(readdirSync(path.join(group, TASK_WORKSPACES_DIR)).sort()).toEqual([...ids].sort());
    for (const id of ids) {
      const ws = taskWorkspacePath(group, id);
      expect(git(ws, "symbolic-ref", "HEAD")).toBe(`refs/heads/${taskBranch(id)}`);
      expect(git(ws, "config", "--get-all", "remote.origin.url")).toBe(REPO);
      expect(readdirSync(ws)).toEqual([".git"]);
    }
  });

  it("creates one workspace when the same task is prepared concurrently", async () => {
    const group = tmp("argus-ws-group-");
    const id = randomUUID();
    const results = await Promise.all(Array.from({ length: 6 }, () => prepare(group, id)));
    expect(results.every((r) => r.ok)).toBe(true);
    expect(results.filter((r) => r.ok && r.created)).toHaveLength(1);
    expect(new Set(results.map((r) => r.ok && r.path)).size).toBe(1);
    expect(readdirSync(path.join(group, TASK_WORKSPACES_DIR))).toEqual([id]); // no temp dirs
  });
});
