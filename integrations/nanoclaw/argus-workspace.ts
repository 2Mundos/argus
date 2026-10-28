/**
 * The stable per-task workspace (task workspace spec): a git repository at a fixed
 * path under the Argus agent group directory, keyed by task id, prepared and checked
 * on the host before any model starts. It never fetches, pushes, repairs, resets or
 * deletes: the worker fetches and pushes inside the container through the gateway,
 * and whatever is in an existing workspace is the task's work in progress.
 */
import { execFile } from "node:child_process";
import { constants } from "node:fs";
import { access, lstat, mkdir, mkdtemp, readdir, realpath, rename, rm } from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";

const run = promisify(execFile);

/** Below the agent group directory; the session container sees it under /workspace/agent. */
export const TASK_WORKSPACES_DIR = "task-workspaces";
const CONTAINER_GROUP_DIR = "/workspace/agent";
const DEFAULT_MAX_WORKSPACES = 200;
const GIT_TIMEOUT_MS = 30_000;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export type WorkspaceFailureReason =
  | "env_workspace_unconfigured"
  | "env_workspace_permission"
  | "env_workspace_invalid"
  | "env_workspace_remote_mismatch"
  | "env_workspace_branch_mismatch"
  | "env_workspace_limit";

export type WorkspaceResult =
  | { ok: true; path: string; containerPath: string; branch: string; created: boolean }
  | { ok: false; reason: WorkspaceFailureReason; error: string };

/** Runs git with `args` in `cwd` and resolves its stdout; rejects on a non-zero exit. */
export type GitRunner = (args: string[], cwd: string, ceiling: string) => Promise<string>;

function checkTaskId(taskId: string): void {
  if (!UUID.test(taskId)) throw new Error("task id must be a canonical lowercase UUID");
}

export function taskWorkspacePath(groupDir: string, taskId: string): string {
  checkTaskId(taskId);
  return path.join(groupDir, TASK_WORKSPACES_DIR, taskId);
}

export function taskWorkspaceContainerPath(taskId: string): string {
  checkTaskId(taskId);
  return path.posix.join(CONTAINER_GROUP_DIR, TASK_WORKSPACES_DIR, taskId);
}

export function taskBranch(taskId: string): string {
  checkTaskId(taskId);
  return `argus/task-${taskId.slice(0, 8)}`;
}

/**
 * Host git, hardened against a workspace the worker controls: no hooks, no fsmonitor,
 * no inherited GIT_* redirection, no discovery above the workspaces root.
 */
const defaultGit: GitRunner = async (args, cwd, ceiling) => {
  const env: NodeJS.ProcessEnv = {};
  for (const [k, v] of Object.entries(process.env)) if (!k.startsWith("GIT_")) env[k] = v;
  Object.assign(env, {
    GIT_CEILING_DIRECTORIES: ceiling,
    GIT_CONFIG_NOSYSTEM: "1",
    GIT_TERMINAL_PROMPT: "0",
    LC_ALL: "C",
  });
  const { stdout } = await run(
    "git",
    ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", ...args],
    { cwd, env, timeout: GIT_TIMEOUT_MS },
  );
  return stdout.trim();
};

const fail = (reason: WorkspaceFailureReason, error: string): WorkspaceResult => ({
  ok: false,
  reason,
  error,
});

const code = (err: unknown) => (err as NodeJS.ErrnoException | null)?.code;
const isPermission = (err: unknown) => ["EACCES", "EPERM", "EROFS"].includes(code(err) ?? "");

/** True when the URL carries userinfo (a credential must never reach .git/config). */
function hasCredentials(url: string): boolean {
  try {
    const u = new URL(url);
    return !!(u.username || u.password);
  } catch {
    return false;
  }
}

export function maxWorkspacesFromEnv(): number {
  const n = Number(process.env.ARGUS_TASK_WORKSPACE_MAX);
  return Number.isInteger(n) && n > 0 ? n : DEFAULT_MAX_WORKSPACES;
}

/**
 * Read-only checks of an existing workspace. Never repairs: a dirty tree is the
 * task's work in progress and is fine.
 */
async function validate(
  ws: string,
  realRoot: string,
  repositoryUrl: string,
  branch: string,
  git: GitRunner,
): Promise<WorkspaceResult | null> {
  const st = await lstat(ws);
  if (st.isSymbolicLink()) return fail("env_workspace_invalid", "the workspace is a symlink");
  if (!st.isDirectory()) return fail("env_workspace_invalid", "the workspace is not a directory");
  const real = await realpath(ws);
  if (path.dirname(real) !== realRoot)
    return fail("env_workspace_invalid", "the workspace resolves outside the workspaces root");
  const dotGit = await lstat(path.join(real, ".git")).catch(() => null);
  if (!dotGit?.isDirectory() || dotGit.isSymbolicLink())
    return fail("env_workspace_invalid", "the workspace is not a git repository");
  try {
    const gitDir = await git(["rev-parse", "--absolute-git-dir"], real, realRoot);
    if (gitDir !== path.join(real, ".git")) throw new Error(`git dir is ${gitDir}`);
  } catch (err) {
    return fail("env_workspace_invalid", `the workspace is not a git repository: ${String(err)}`);
  }
  // --file reads only this config: no includes, nothing the worker could redirect.
  const urls = (
    await git(
      ["config", "--file", path.join(real, ".git", "config"), "--get-all", "remote.origin.url"],
      real,
      realRoot,
    ).catch(() => "")
  )
    .split("\n")
    .filter(Boolean);
  if (urls.length === 0) return fail("env_workspace_remote_mismatch", "origin is not set");
  if (urls.length > 1 || urls[0] !== repositoryUrl)
    return fail(
      "env_workspace_remote_mismatch",
      urls.some(hasCredentials)
        ? "origin carries credentials"
        : `origin is ${urls.join(", ")}, expected ${repositoryUrl}`,
    );
  const head = await git(["symbolic-ref", "-q", "HEAD"], real, realRoot).catch(() => "");
  if (head !== `refs/heads/${branch}`)
    return fail(
      "env_workspace_branch_mismatch",
      `HEAD is ${head || "detached"}, expected refs/heads/${branch}`,
    );
  return null;
}

/** Task directories only: hidden temporary directories of a prepare in flight do not count. */
async function countWorkspaces(root: string): Promise<number> {
  const entries = await readdir(root, { withFileTypes: true });
  return entries.filter((e) => UUID.test(e.name)).length;
}

/**
 * Create (first claim) or validate (later attempts) the workspace of `taskId`.
 * Fails closed with a typed reason; never deletes anything but its own temp dir.
 */
export async function prepareTaskWorkspace(opts: {
  groupDir: string | null;
  taskId: string;
  repositoryUrl: string | null | undefined;
  maxWorkspaces?: number;
  git?: GitRunner;
}): Promise<WorkspaceResult> {
  const { groupDir, taskId, repositoryUrl } = opts;
  const git = opts.git ?? defaultGit;
  if (!groupDir) return fail("env_workspace_unconfigured", "the agent group directory is unknown");
  if (!repositoryUrl) return fail("env_workspace_unconfigured", "the task has no repository URL");
  if (hasCredentials(repositoryUrl))
    return fail("env_workspace_remote_mismatch", "the repository URL carries credentials");
  let ws: string;
  let branch: string;
  try {
    ws = taskWorkspacePath(groupDir, taskId);
    branch = taskBranch(taskId);
  } catch (err) {
    return fail("env_workspace_invalid", String(err));
  }
  const root = path.dirname(ws);
  const done = (created: boolean): WorkspaceResult => ({
    ok: true,
    path: ws,
    containerPath: taskWorkspaceContainerPath(taskId),
    branch,
    created,
  });
  let realRoot: string;
  try {
    await mkdir(root, { recursive: true, mode: 0o700 });
    const rootStat = await lstat(root);
    if (rootStat.isSymbolicLink() || !rootStat.isDirectory())
      return fail("env_workspace_invalid", "the workspaces root is not a real directory");
    realRoot = await realpath(root);
    if (realRoot !== path.join(await realpath(groupDir), TASK_WORKSPACES_DIR))
      return fail("env_workspace_invalid", "the workspaces root resolves outside the group");
    await access(realRoot, constants.R_OK | constants.W_OK | constants.X_OK);
  } catch (err) {
    if (isPermission(err)) return fail("env_workspace_permission", String(err));
    return fail("env_workspace_invalid", String(err));
  }
  try {
    const exists = await lstat(ws).then(
      () => true,
      (err) => {
        if (code(err) === "ENOENT") return false;
        throw err;
      },
    );
    if (exists) return (await validate(ws, realRoot, repositoryUrl, branch, git)) ?? done(false);
    const max = opts.maxWorkspaces ?? maxWorkspacesFromEnv();
    const count = await countWorkspaces(realRoot);
    if (count >= max)
      return fail(
        "env_workspace_limit",
        `${count} task workspaces exist; ARGUS_TASK_WORKSPACE_MAX is ${max}`,
      );
    // Build aside and rename into place, so a workspace is either absent or complete.
    const tmp = await mkdtemp(path.join(realRoot, `.tmp-${taskId}-`));
    try {
      await git(["init", "-q", "--template=", tmp], realRoot, realRoot);
      await git(["symbolic-ref", "HEAD", `refs/heads/${branch}`], tmp, realRoot);
      await git(["remote", "add", "origin", repositoryUrl], tmp, realRoot);
      try {
        await rename(tmp, ws);
        return done(true);
      } catch (err) {
        // A concurrent prepare of the same task won: use its workspace.
        if (code(err) !== "ENOTEMPTY" && code(err) !== "EEXIST") throw err;
      }
    } finally {
      await rm(tmp, { recursive: true, force: true });
    }
    return (await validate(ws, realRoot, repositoryUrl, branch, git)) ?? done(false);
  } catch (err) {
    if (isPermission(err)) return fail("env_workspace_permission", String(err));
    return fail("env_workspace_invalid", String(err));
  }
}
