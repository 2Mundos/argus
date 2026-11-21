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
  branchWipClaim,
  fencePriorWorkspaces,
  maxWorkspacesFromEnv,
  type PriorWorkspace,
  prepareTaskWorkspace,
  probeTaskBranch,
  TASK_WORKSPACES_DIR,
  taskBranch,
  taskWorkspaceContainerPath,
  taskWorkspacePath,
  WORKER_GIT_HOOKS_PATH,
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

/** Read a key from the workspace `.git/config` (host git pins hooksPath=/dev/null). */
const workspaceGitConfig = (ws: string, key: string) =>
  git(ws, "config", "--file", path.join(".git", "config"), "--get", key);

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
    expect(workspaceGitConfig(r.path, "core.hooksPath")).toBe(WORKER_GIT_HOOKS_PATH);
    expect(readdirSync(path.join(group, TASK_WORKSPACES_DIR))).toEqual([id]);
  });

  it("sets core.hooksPath on an existing valid workspace when it was missing", async () => {
    const group = tmp("argus-ws-group-");
    const id = randomUUID();
    const r = await prepare(group, id);
    if (!r.ok) throw new Error(r.error);
    git(r.path, "config", "--unset", "core.hooksPath");
    expect(() => workspaceGitConfig(r.path, "core.hooksPath")).toThrow();
    const again = await prepare(group, id);
    expect(again).toMatchObject({ ok: true, created: false });
    expect(workspaceGitConfig(r.path, "core.hooksPath")).toBe(WORKER_GIT_HOOKS_PATH);
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

  describe("a GitHub origin spelled in another letter case", () => {
    const LOWER = "https://github.com/2mundos/argus.git";
    const CANONICAL = "https://github.com/2Mundos/argus.git";

    /** A workspace created for `created`, its origin then set to `origin` (if given). */
    const workspaceWith = async (created: string, origin?: string) => {
      const group = tmp("argus-ws-group-");
      const id = randomUUID();
      const r = await prepare(group, id, { repositoryUrl: created });
      if (!r.ok) throw new Error(r.error);
      if (origin !== undefined) git(r.path, "remote", "set-url", "origin", origin);
      return { group, id, path: r.path };
    };

    it.each([
      ["lowercase origin, canonical task URL", LOWER, CANONICAL],
      ["canonical origin, lowercase task URL", CANONICAL, LOWER],
      ["origin without .git", "https://github.com/2Mundos/argus", LOWER],
      ["task URL without .git", LOWER, "https://github.com/2Mundos/Argus"],
    ])("accepts %s and never rewrites origin", async (_label, origin, expected) => {
      const ws = await workspaceWith(LOWER, origin);
      const before = snapshot(ws.group);
      const r = await prepare(ws.group, ws.id, { repositoryUrl: expected });
      expect(r).toMatchObject({ ok: true, created: false });
      expect(git(ws.path, "config", "--get-all", "remote.origin.url")).toBe(origin);
      expect(snapshot(ws.group)).toEqual(before);
    });

    it("leaves HEAD, branch, origin, status and every file of work in progress identical", async () => {
      const ws = await workspaceWith(LOWER);
      writeFileSync(path.join(ws.path, "tracked.txt"), "committed\n");
      git(ws.path, "add", "tracked.txt");
      git(ws.path, "commit", "-q", "-m", "wip");
      writeFileSync(path.join(ws.path, "tracked.txt"), "dirty\n");
      writeFileSync(path.join(ws.path, "staged.txt"), "staged\n");
      git(ws.path, "add", "staged.txt");
      writeFileSync(path.join(ws.path, "untracked.txt"), "untracked\n");
      const state = () => ({
        head: git(ws.path, "rev-parse", "HEAD"),
        branch: git(ws.path, "symbolic-ref", "HEAD"),
        origin: readFileSync(path.join(ws.path, ".git", "config"), "utf8").match(/url = (.*)/)?.[1],
        status: git(ws.path, "status", "--porcelain"),
        files: Object.fromEntries(
          ["tracked.txt", "staged.txt", "untracked.txt"].map((f) => [
            f,
            readFileSync(path.join(ws.path, f), "utf8"),
          ]),
        ),
        tree: snapshot(ws.path),
      });
      const before = state();
      expect(before.origin).toBe(LOWER);
      expect(before.status.split("\n").sort()).toEqual([
        " M tracked.txt",
        "?? untracked.txt",
        "A  staged.txt",
      ]);
      const r = await prepare(ws.group, ws.id, { repositoryUrl: CANONICAL });
      expect(r).toMatchObject({ ok: true, created: false });
      expect(state()).toEqual(before);
    });

    it.each([
      ["another scheme", "http://github.com/2Mundos/argus.git"],
      ["another port", "https://github.com:8443/2Mundos/argus.git"],
      ["an explicit default port", "https://github.com:443/2Mundos/argus.git"],
      ["another host", "https://gitlab.com/2Mundos/argus.git"],
      ["the host in another case", "https://GitHub.com/2Mundos/argus.git"],
      ["another owner", "https://github.com/2Mundos-fork/argus.git"],
      ["another repository", "https://github.com/2Mundos/argus-fork.git"],
      ["a query", "https://github.com/2Mundos/argus.git?ref=x"],
      ["an empty query", "https://github.com/2Mundos/argus.git?"],
      ["a fragment", "https://github.com/2Mundos/argus.git#x"],
      ["a trailing slash", "https://github.com/2Mundos/argus/"],
      ["an extra path segment", "https://github.com/2Mundos/argus/extra.git"],
      ["a dot segment", "https://github.com/x/../2Mundos/argus.git"],
      ["a doubled .git", "https://github.com/2Mundos/argus.git.git"],
      ["scp-like syntax", "git@github.com:2Mundos/argus.git"],
      ["no URL at all", "argus"],
    ])("refuses %s, naming both URLs", async (_label, origin) => {
      const ws = await workspaceWith(LOWER, origin);
      const before = snapshot(ws.group);
      const r = await prepare(ws.group, ws.id, { repositoryUrl: CANONICAL });
      expect(r).toEqual({
        ok: false,
        reason: "env_workspace_remote_mismatch",
        error: `origin is ${origin}, expected ${CANONICAL} (GitHub owner and repository may differ only in letter case)`,
      });
      expect(snapshot(ws.group)).toEqual(before);
    });

    it("refuses userinfo without echoing it", async () => {
      const secret = "tok3n-not-a-real-secret";
      const ws = await workspaceWith(LOWER, `https://x:${secret}@github.com/2Mundos/argus.git`);
      const r = await prepare(ws.group, ws.id, { repositoryUrl: CANONICAL });
      expect(r).toEqual({
        ok: false,
        reason: "env_workspace_remote_mismatch",
        error: "origin carries credentials",
      });
    });

    it("refuses two origin URLs even when both name the repository", async () => {
      const ws = await workspaceWith(LOWER);
      git(ws.path, "config", "--add", "remote.origin.url", CANONICAL);
      const r = await prepare(ws.group, ws.id, { repositoryUrl: CANONICAL });
      expect(r).toMatchObject({ ok: false, reason: "env_workspace_remote_mismatch" });
    });

    it("keeps other forges byte for byte", async () => {
      for (const origin of [
        "https://git.example.test/Org/repo.git",
        "https://git.example.test/org/repo",
      ]) {
        const ws = await workspaceWith(REPO, origin);
        const r = await prepare(ws.group, ws.id, { repositoryUrl: REPO });
        expect(r).toEqual({
          ok: false,
          reason: "env_workspace_remote_mismatch",
          error: `origin is ${origin}, expected ${REPO}`,
        });
      }
    });
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
      git(r.path, "checkout", "-q", "-b", "other");
      await failsWith(group, id, "env_workspace_branch_mismatch", [group]);
    });

    it("env_workspace_unpreserved_commits: another branch is out and the task branch holds unpublished commits", async () => {
      const group = tmp("argus-ws-group-");
      const id = randomUUID();
      const r = await prepare(group, id);
      if (!r.ok) throw new Error(r.error);
      writeFileSync(path.join(r.path, "a.txt"), "a\n");
      git(r.path, "add", "a.txt");
      git(r.path, "commit", "-q", "-m", "a");
      const sha = git(r.path, "rev-parse", "HEAD");
      git(r.path, "checkout", "-q", "-b", "other");
      const before = snapshot(r.path);
      const again = await prepare(group, id);
      expect(again).toMatchObject({ ok: false, reason: "env_workspace_unpreserved_commits" });
      if (again.ok) throw new Error("expected a refusal");
      expect(again.error).toContain(`refs/heads/${r.branch}`);
      expect(again.error).toContain(sha);
      expect(snapshot(r.path)).toEqual(before);
    });

    it("env_workspace_unpreserved_commits: detached HEAD with unpublished commits", async () => {
      const group = tmp("argus-ws-group-");
      const id = randomUUID();
      const r = await prepare(group, id);
      if (!r.ok) throw new Error(r.error);
      writeFileSync(path.join(r.path, "a.txt"), "a\n");
      git(r.path, "add", "a.txt");
      git(r.path, "commit", "-q", "-m", "a");
      const sha = git(r.path, "rev-parse", "HEAD");
      git(r.path, "checkout", "-q", "--detach");
      const again = await prepare(group, id);
      expect(again).toMatchObject({ ok: false, reason: "env_workspace_unpreserved_commits" });
      if (again.ok) throw new Error("expected a refusal");
      expect(again.error).toContain(sha);
    });

    it("env_workspace_branch_mismatch stays when the task branch is published or gone", async () => {
      const group = tmp("argus-ws-group-");
      const id = randomUUID();
      const r = await prepare(group, id);
      if (!r.ok) throw new Error(r.error);
      writeFileSync(path.join(r.path, "a.txt"), "a\n");
      git(r.path, "add", "a.txt");
      git(r.path, "commit", "-q", "-m", "a");
      // A remote-tracking ref that reaches the commit means it is published.
      git(r.path, "update-ref", `refs/remotes/origin/${r.branch}`, "HEAD");
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

describe("task branch probe and the WIP claim derived from it", () => {
  const commit = (cwd: string, file: string) => {
    writeFileSync(path.join(cwd, file), `${file}\n`);
    git(cwd, "add", file);
    git(cwd, "commit", "-q", "-m", file);
    return git(cwd, "rev-parse", "HEAD");
  };

  it("same directory: the branch stays checked out and its unpublished commits stay reachable", async () => {
    const group = tmp("argus-ws-group-");
    const id = randomUUID();
    const r = await prepare(group, id);
    if (!r.ok) throw new Error(r.error);
    const sha = commit(r.path, "a.txt");
    const again = await prepare(group, id);
    expect(again).toMatchObject({ ok: true, created: false });
    expect(git(r.path, "symbolic-ref", "HEAD")).toBe(`refs/heads/${r.branch}`);
    expect(git(r.path, "rev-parse", r.branch)).toBe(sha);
    expect(git(r.path, "merge-base", "--is-ancestor", sha, "HEAD")).toBe("");
    const probe = await probeTaskBranch({ path: r.path, branch: r.branch });
    expect(probe).toEqual({ exists: true, unpublished: [sha] });
    expect(branchWipClaim(r.branch, probe)).toMatchObject({ ref: sha });
  });

  it("a branch that is gone says so plainly, not what an earlier agent wrote", async () => {
    const group = tmp("argus-ws-group-");
    const r = await prepare(group, randomUUID());
    if (!r.ok) throw new Error(r.error);
    const probe = await probeTaskBranch({ path: r.path, branch: r.branch });
    expect(probe).toEqual({ exists: false, unpublished: [] });
    const claim = branchWipClaim(r.branch, probe);
    expect(claim.summary).toContain(`branch ${r.branch} does not exist`);
    expect(claim.ref).toBeUndefined();
  });

  it("published commits are not unpublished", async () => {
    const group = tmp("argus-ws-group-");
    const r = await prepare(group, randomUUID());
    if (!r.ok) throw new Error(r.error);
    const first = commit(r.path, "a.txt");
    git(r.path, "update-ref", "refs/remotes/origin/main", first);
    const second = commit(r.path, "b.txt");
    const probe = await probeTaskBranch({ path: r.path, branch: r.branch });
    expect(probe.unpublished).toEqual([second]);
    git(r.path, "update-ref", `refs/remotes/origin/${r.branch}`, second);
    const none = await probeTaskBranch({ path: r.path, branch: r.branch });
    expect(none).toEqual({ exists: true, unpublished: [] });
    expect(branchWipClaim(r.branch, none).summary).toContain("no unpublished commits");
  });
});

describe("fencePriorWorkspaces: an empty workspace on another instance needs proof", () => {
  const prior = (over: Partial<PriorWorkspace> = {}): PriorWorkspace => ({
    attempt: 1,
    owner_agent_id: "agent-a",
    owner_node_id: "node-a",
    owner_status: "ended",
    checkpoint_state: "nothing_to_save",
    ...over,
  });
  const fence = (
    list: PriorWorkspace[] | undefined,
    self: string | null = "agent-b",
    attempt = 2,
  ) => fencePriorWorkspaces({ attempt, selfAgentId: self, prior: list });

  it("lets a first attempt and a same-instance retry through", () => {
    expect(fence([], "agent-b", 1)).toBeNull();
    expect(fence(undefined, "agent-b", 1)).toBeNull();
    expect(fence([prior({ owner_agent_id: "agent-b", checkpoint_state: null })])).toBeNull();
  });

  it("accepts only the other worker's own nothing_to_save report as absence", () => {
    expect(fence([prior()])).toBeNull();
  });

  it("refuses unpublished work whose checkpoint is not restorable here, naming the provenance", () => {
    const r = fence([prior({ checkpoint_state: "saved", owner_status: "live" })]);
    expect(r?.reason).toBe("env_workspace_prior_unavailable");
    expect(r?.error).toContain("agent agent-a");
    expect(r?.error).toContain("node node-a");
    expect(r?.error).toContain("checkpoint step saved");
  });

  it.each([
    ["failed", "failed"],
    ["stale (running, never finished)", "running"],
    ["never reported", null],
  ] as const)("does not report unknown as absent: checkpoint step %s", (_l, state) => {
    expect(fence([prior({ checkpoint_state: state })])?.reason).toBe(
      "env_workspace_prior_unverified",
    );
  });

  it("refuses a missing owner (no node, unknown status) and an older server's silence", () => {
    const r = fence([
      prior({ owner_node_id: null, owner_status: "unknown", checkpoint_state: null }),
    ]);
    expect(r?.reason).toBe("env_workspace_prior_unverified");
    expect(r?.error).toContain("node not reported");
    expect(fence(undefined)?.reason).toBe("env_workspace_prior_unverified");
  });

  it("is not satisfied by a newer proof: every attempt on another instance must be proven", () => {
    const r = fence(
      [prior({ checkpoint_state: "failed" }), prior({ attempt: 2, owner_agent_id: "agent-c" })],
      "agent-b",
      3,
    );
    expect(r?.reason).toBe("env_workspace_prior_unverified");
    expect(r?.error).toContain("attempt 1");
    expect(r?.error).not.toContain("attempt 2");
  });

  it("fences work after this instance's newest attempt even with local work (A, B, A)", () => {
    const abA = [
      prior({ owner_agent_id: "agent-b", checkpoint_state: "nothing_to_save" }),
      prior({ attempt: 2, owner_agent_id: "agent-a", checkpoint_state: "saved" }),
    ];
    // attempt 3 on agent-b: attempt 2 on agent-a is newer than anything b ran
    const r = fencePriorWorkspaces({
      attempt: 3,
      selfAgentId: "agent-b",
      prior: abA,
      hasLocalWork: true,
    });
    expect(r?.reason).toBe("env_workspace_prior_unavailable");
    expect(r?.error).toContain("attempt 2");
  });

  it("does not re-fence attempts older than this instance's own newest one", () => {
    const list = [
      prior({ attempt: 1, owner_agent_id: "agent-a", checkpoint_state: null }),
      prior({ attempt: 2, owner_agent_id: "agent-b", checkpoint_state: null }),
    ];
    expect(
      fencePriorWorkspaces({ attempt: 3, selfAgentId: "agent-b", prior: list, hasLocalWork: true }),
    ).toBeNull();
  });

  it("an older server's silence only passes with local work", () => {
    const o = { attempt: 2, selfAgentId: "agent-b", prior: undefined };
    expect(fencePriorWorkspaces({ ...o, hasLocalWork: true })).toBeNull();
    expect(fencePriorWorkspaces(o)?.reason).toBe("env_workspace_prior_unverified");
  });

  it("treats an unknown own identity as another instance", () => {
    expect(
      fence([prior({ owner_agent_id: "agent-b", checkpoint_state: null })], null)?.reason,
    ).toBe("env_workspace_prior_unverified");
  });
});
