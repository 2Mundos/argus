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
import { normalizeRepositoryUrl, sameRepositoryRemote } from "./argus-accounts.js";

const run = promisify(execFile);

/** Below the agent group directory; the session container sees it under /workspace/agent. */
export const TASK_WORKSPACES_DIR = "task-workspaces";
/** Commit-msg hook directory baked into the agent image (worker-lifecycle 3.8). */
export const WORKER_GIT_HOOKS_PATH = "/usr/local/lib/argus/git-hooks";
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
  | "env_workspace_unpreserved_commits"
  | "env_workspace_limit"
  | "env_workspace_prior_unverified"
  | "env_workspace_prior_unavailable";

export type WorkspaceResult =
  | { ok: true; path: string; containerPath: string; branch: string; created: boolean }
  | { ok: false; reason: WorkspaceFailureReason; error: string };

export type WorkspaceCleanResult =
  | { ok: true; clean: true }
  | { ok: true; clean: false; reason: "unpublished_commits" | "uncommitted_changes" }
  | { ok: false; reason: "env_checkpoint_invalid"; error: string };

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
export const defaultGit: GitRunner = async (args, cwd, ceiling) => {
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

/** What git says about the task branch in a workspace (read-only). */
export type BranchProbe = {
  /** `refs/heads/<branch>` exists. */
  exists: boolean;
  /** Commits reachable from the branch and from no remote-tracking ref, newest first. */
  unpublished: string[];
};

/**
 * Commits of `ref` that no remote-tracking ref reaches, i.e. work that exists only in this
 * workspace. A repository with no remote-tracking refs holds every commit unpublished.
 */
async function unpublishedCommits(
  ws: string,
  ceiling: string,
  ref: string,
  git: GitRunner,
  forgeHeads: string[] = [],
): Promise<string[]> {
  const out = await git(["rev-list", ref, "--not", "--remotes", ...forgeHeads, "--"], ws, ceiling);
  return out.split("\n").filter(Boolean);
}

/** Probe the task branch of the workspace at `ws`. Read-only; rejects when git fails. */
export async function probeTaskBranch(opts: {
  path: string;
  branch: string;
  git?: GitRunner;
}): Promise<BranchProbe> {
  const git = opts.git ?? defaultGit;
  const ws = await realpath(opts.path);
  const ceiling = path.dirname(ws);
  const ref = `refs/heads/${opts.branch}`;
  const exists = await git(["rev-parse", "--verify", "-q", `${ref}^{commit}`], ws, ceiling).then(
    () => true,
    () => false,
  );
  return { exists, unpublished: exists ? await unpublishedCommits(ws, ceiling, ref, git) : [] };
}

const MAX_NAMED_SHAS = 10;

/**
 * The continuation's WIP claim as git states it: whether the task branch exists in the
 * workspace and which commits only the workspace holds. Replaces a previous agent's prose.
 */
export function branchWipClaim(
  branch: string,
  probe: BranchProbe,
): { summary: string; ref?: string } {
  const prefix = "Checked in the task workspace with git:";
  if (!probe.exists)
    return {
      summary: `${prefix} branch ${branch} does not exist; no local commits from an earlier attempt are present. Any earlier claim of unpublished work is not backed by this workspace.`,
    };
  if (probe.unpublished.length === 0)
    return {
      summary: `${prefix} branch ${branch} exists and has no unpublished commits: every commit is on a remote-tracking ref.`,
    };
  const named = probe.unpublished.slice(0, MAX_NAMED_SHAS).join(", ");
  const more =
    probe.unpublished.length > MAX_NAMED_SHAS
      ? ` and ${probe.unpublished.length - MAX_NAMED_SHAS} more`
      : "";
  return {
    summary: `${prefix} branch ${branch} has ${probe.unpublished.length} commit(s) on no remote-tracking ref (unpublished): ${named}${more}.`,
    ref: probe.unpublished[0],
  };
}

/**
 * Unpublished commits a refused retry would strand: those of the task branch, plus those
 * of a detached HEAD. Empty when the task branch is gone or fully published.
 */
async function strandedCommits(
  ws: string,
  ceiling: string,
  branch: string,
  head: string,
  git: GitRunner,
  forgeHeads: string[] = [],
): Promise<string[]> {
  const found = new Set<string>();
  const ref = `refs/heads/${branch}`;
  if (await git(["rev-parse", "--verify", "-q", `${ref}^{commit}`], ws, ceiling).catch(() => "")) {
    for (const sha of await unpublishedCommits(ws, ceiling, ref, git, forgeHeads)) found.add(sha);
  }
  if (
    !head &&
    (await git(["rev-parse", "--verify", "-q", "HEAD^{commit}"], ws, ceiling).catch(() => ""))
  )
    for (const sha of await unpublishedCommits(ws, ceiling, "HEAD", git, forgeHeads))
      found.add(sha);
  return [...found];
}

/** A reviewed PR head the task input names: a commit the forge is shown to hold. */
export type ReviewedHead = { pr: unknown; headSha: unknown };

/**
 * The reviewed head as forge evidence, or null. A commit counts as published only when the
 * forge advertises `refs/pull/<n>/head` (it survives a squash merge and branch deletion)
 * at the reviewed head, or at a descendant this workspace holds. Anything unverifiable,
 * including an unreachable forge, is no evidence.
 */
async function verifiedForgeHead(
  ws: string,
  ceiling: string,
  reviewed: ReviewedHead | undefined,
  git: GitRunner,
): Promise<string | null> {
  if (!reviewed || typeof reviewed.headSha !== "string") return null;
  const sha = reviewed.headSha.trim().toLowerCase();
  const num = /^(?:#|.*\/pull\/)?(\d+)\/?$/.exec(String(reviewed.pr ?? "").trim())?.[1];
  if (!/^[0-9a-f]{40}$/.test(sha) || !num) return null;
  try {
    const out = await git(["ls-remote", "origin", `refs/pull/${num}/head`], ws, ceiling);
    const [forge, name] = out.split("\n")[0]?.split(/\s+/) ?? [];
    if (name !== `refs/pull/${num}/head` || !/^[0-9a-f]{40}$/.test(forge ?? "")) return null;
    if (forge === sha) return sha;
    await git(["merge-base", "--is-ancestor", sha, forge!], ws, ceiling);
    return sha;
  } catch {
    return null;
  }
}

/**
 * Whether a review's earlier attempts can have left nothing behind: the forge shows the
 * task branch and `refs/pull/<n>/head` both at the reviewed head, so a review that ran
 * elsewhere published everything it could hold. Read-only, runs before any workspace
 * exists, and fails closed: a missing branch, a different head, a credentialed URL or an
 * unreachable forge is no proof.
 */
export async function reviewHeadPublished(opts: {
  groupDir: string | null;
  taskId: string;
  repositoryUrl: string | null | undefined;
  reviewed: ReviewedHead | undefined;
  git?: GitRunner;
}): Promise<boolean> {
  const { groupDir, repositoryUrl, reviewed } = opts;
  if (!groupDir || !repositoryUrl || hasCredentials(repositoryUrl) || !reviewed) return false;
  if (typeof reviewed.headSha !== "string") return false;
  const sha = reviewed.headSha.trim().toLowerCase();
  const num = /^(?:#|.*\/pull\/)?(\d+)\/?$/.exec(String(reviewed.pr ?? "").trim())?.[1];
  if (!/^[0-9a-f]{40}$/.test(sha) || !num) return false;
  try {
    const branch = taskBranch(opts.taskId);
    const pull = `refs/pull/${num}/head`;
    const out = await (opts.git ?? defaultGit)(
      ["ls-remote", repositoryUrl, `refs/heads/${branch}`, pull],
      groupDir,
      groupDir,
    );
    const heads = new Map<string, string>();
    for (const line of out.split("\n")) {
      const [id, name] = line.split(/\s+/);
      if (id && name) heads.set(name, id.toLowerCase());
    }
    return heads.get(`refs/heads/${branch}`) === sha && heads.get(pull) === sha;
  } catch {
    return false;
  }
}

const z = (s: string) => s.split("\0").filter(Boolean);

// The workspace's own git config is written by the worker, yet git runs on the host here:
// `diff` runs `filter.*.clean`, `log` runs `gpg.program` under `log.showSignature`,
// `core.worktree` points git at host files. Only inert keys or the fixed worker hook path
// (always overridden by host git) are accepted.
const INERT_GIT_CONFIG = [
  /^core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|precomposeunicode|symlinks|autocrlf|eol|safecrlf|quotepath|checkstat|trustctime|untrackedcache)$/,
  /^remote\..+\.(url|pushurl|fetch|push|tagopt|prune)$/,
  /^branch\..+\.(remote|merge|rebase|pushremote|description)$/,
  /^(user|author|committer|init|pull|push|fetch|commit|tag|rebase|color|advice|status|gc|index|pack|feature)\.[^.]+$/,
  /^extensions\.objectformat$/,
];

/**
 * Returns an error string when the workspace's `.git` could execute a program or read
 * outside the repository; `null` when every config key is inert. Read-only; fails closed.
 */
export async function findNonInertGitConfig(
  ws: string,
  ceiling: string,
  git: GitRunner = defaultGit,
): Promise<string | null> {
  const dotGit = path.join(ws, ".git");
  for (const redirect of ["commondir", "objects/info/alternates", "objects/info/http-alternates"])
    if (await lstat(path.join(dotGit, redirect)).catch(() => null))
      return `the workspace git directory has ${redirect}`;
  // --file reads only this file and follows no include; an unreadable config fails closed.
  let listed: string;
  try {
    listed = await git(
      ["config", "--file", path.join(dotGit, "config"), "--list", "-z"],
      ws,
      ceiling,
    );
  } catch {
    return "the workspace git config is unreadable";
  }
  for (const entry of z(listed)) {
    const [key, value] = entry.split("\n", 2);
    if (key === "core.hookspath" && value === WORKER_GIT_HOOKS_PATH) continue;
    if (!INERT_GIT_CONFIG.some((re) => re.test(key)))
      return `the workspace git config sets ${key.slice(0, 100)}, which git could act on`;
  }
  return null;
}

/**
 * Prove whether a prepared task workspace holds no unpublished work: no commits on the
 * task branch, detached HEAD, or any other local ref missing from remote-tracking refs,
 * and no staged, unstaged or untracked changes. Read-only; fails closed (`ok: false`)
 * whenever the workspace cannot be verified so `nothing_to_save` is never reported
 * without proof.
 */
export async function checkWorkspaceClean(opts: {
  groupDir: string | null;
  taskId: string;
  repositoryUrl: string | null | undefined;
  /** The PR head a code_review attempt reviewed (task input `pr`, `head_sha`). */
  reviewed?: ReviewedHead;
  git?: GitRunner;
}): Promise<WorkspaceCleanResult> {
  const { groupDir, taskId, repositoryUrl } = opts;
  const git = opts.git ?? defaultGit;
  const invalid = (error: string): WorkspaceCleanResult => ({
    ok: false,
    reason: "env_checkpoint_invalid",
    error,
  });
  if (!groupDir || !repositoryUrl)
    return invalid("the group directory or repository is not configured");
  if (hasCredentials(repositoryUrl)) return invalid("the repository URL carries credentials");
  let ws: string;
  let branch: string;
  try {
    ws = taskWorkspacePath(groupDir, taskId);
    branch = taskBranch(taskId);
  } catch (err) {
    return invalid(String(err));
  }
  try {
    const root = path.dirname(ws);
    const rootStat = await lstat(root).catch(() => null);
    if (!rootStat || rootStat.isSymbolicLink() || !rootStat.isDirectory())
      return invalid("the workspaces root is not a real directory");
    const realRoot = await realpath(root);
    if (realRoot !== path.join(await realpath(groupDir), TASK_WORKSPACES_DIR))
      return invalid("the workspaces root resolves outside the group");
    const st = await lstat(ws).catch(() => null);
    if (!st) return invalid("the task workspace is missing");
    if (st.isSymbolicLink() || !st.isDirectory())
      return invalid("the workspace is not a real directory");
    const real = await realpath(ws);
    if (path.dirname(real) !== realRoot)
      return invalid("the workspace resolves outside the workspaces root");
    const dotGit = await lstat(path.join(real, ".git")).catch(() => null);
    if (!dotGit?.isDirectory() || dotGit.isSymbolicLink())
      return invalid("the workspace is not a git repository");
    const gitDir = await git(["rev-parse", "--absolute-git-dir"], real, realRoot);
    if (gitDir !== path.join(real, ".git")) return invalid(`the workspace git dir is ${gitDir}`);
    const origin = (
      await git(
        ["config", "--file", path.join(real, ".git", "config"), "--get-all", "remote.origin.url"],
        real,
        realRoot,
      ).catch(() => "")
    )
      .split("\n")
      .filter(Boolean);
    if (origin.length !== 1 || !sameRepositoryRemote(origin[0]!, repositoryUrl))
      return invalid("the workspace origin does not match the task repository");
    const nonInert = await findNonInertGitConfig(real, realRoot, git);
    if (nonInert) return invalid(nonInert);
    const head = await git(["symbolic-ref", "-q", "HEAD"], real, realRoot).catch(() => "");
    const forge = await verifiedForgeHead(real, realRoot, opts.reviewed, git);
    const forgeHeads = forge ? [forge] : [];
    const stranded = await strandedCommits(real, realRoot, branch, head, git, forgeHeads);
    if (stranded.length > 0) return { ok: true, clean: false, reason: "unpublished_commits" };
    const otherUnpublished = await unpublishedCommits(real, realRoot, "--all", git, forgeHeads);
    if (otherUnpublished.length > 0)
      return { ok: true, clean: false, reason: "unpublished_commits" };
    const diff = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z"];
    const staged = z(await git([...diff, "--cached"], real, realRoot));
    const unstaged = z(await git(diff, real, realRoot));
    const untracked = z(
      await git(["ls-files", "--others", "--exclude-standard", "-z"], real, realRoot),
    );
    if (staged.length > 0 || unstaged.length > 0 || untracked.length > 0)
      return { ok: true, clean: false, reason: "uncommitted_changes" };
    return { ok: true, clean: true };
  } catch (err) {
    return invalid(String(err));
  }
}

/** An earlier attempt's workspace owner as the claim reports it (`ClaimedTask.prior_workspaces`). */
export type PriorWorkspace = {
  attempt: number;
  owner_agent_id: string;
  owner_node_id?: string | null;
  owner_status?: "live" | "stale" | "ended" | "unknown";
  checkpoint_state?: "running" | "saved" | "nothing_to_save" | "failed" | null;
};

const describePrior = (p: PriorWorkspace): string =>
  `attempt ${p.attempt} ran on agent ${p.owner_agent_id}` +
  ` (node ${p.owner_node_id ?? "not reported"}, ${p.owner_status ?? "unknown"}),` +
  ` checkpoint step ${p.checkpoint_state ?? "not reported"}`;

/**
 * A claim is about to proceed from this instance's workspace (empty, an old checkout or a
 * restored checkpoint). Prior claim rows on other instances after this instance's newest
 * claim row that durably finished its checkpoint may hold unpublished work this one cannot
 * see, so "no work" must be proven, never assumed: only a worker's own `nothing_to_save`
 * report proves it. `prior` is ordered oldest-first by claim row across all retry
 * generations (because operator retries reset `attempt` to 1), so evaluation and the
 * reconciliation cutoff follow list order rather than `attempt` numbers. `null` means
 * nothing is at risk (a genuinely first claim with an empty list, an older server's silence
 * at attempt 1, or every earlier claim ran here, where the workspace's own state is checked
 * by `prepareTaskWorkspace`). Otherwise a typed refusal that names each attempt, owner, node
 * and checkpoint step. `prior` undefined on attempt > 1 is an older server that cannot say
 * where work lives: unknown, not absent. Read-only. A refusal carries `pending` when every
 * unproven attempt has no step or a `running` one: its report may simply not have landed
 * yet, so the caller may read the rows again before treating it as a block.
 */
export function fencePriorWorkspaces(opts: {
  attempt: number;
  selfAgentId: string | null;
  prior: PriorWorkspace[] | null | undefined;
  /** A workspace already exists or was restored: an older server's silence then keeps the legacy reuse. */
  hasLocalWork?: boolean;
  /**
   * A review's branch and PR head are both published at the reviewed head
   * (`reviewHeadPublished`): earlier attempts without a checkpoint report hold nothing.
   * A saved checkpoint still blocks.
   */
  publishedHead?: boolean;
}): { reason: WorkspaceFailureReason; error: string; pending?: true } | null {
  const refuse = (reason: WorkspaceFailureReason, error: string, pending?: true) => ({
    reason,
    error,
    ...(pending ? { pending } : {}),
  });
  if (!opts.prior)
    return opts.attempt <= 1 || opts.hasLocalWork
      ? null
      : refuse(
          "env_workspace_prior_unverified",
          `attempt ${opts.attempt} would start from an empty workspace but the server did not say ` +
            "where earlier attempts worked, so unpublished work elsewhere cannot be ruled out. " +
            "Nothing was created; run the retry on the instance that holds the earlier work",
        );
  const mine = (p: PriorWorkspace) => !!opts.selfAgentId && p.owner_agent_id === opts.selfAgentId;
  // Work after this instance's newest *reconciled* claim row is what its checkout cannot contain
  // (A, B, A). Ordering follows the server's claim-row sequence (`opts.prior`), not `attempt`
  // numbers, because an operator retry resets `attempt` to 1. A claim alone proves nothing: an
  // attempt that died or was released before its checkpoint ended (null, running, failed) never
  // showed the earlier foreign work was absorbed, so only a recorded `saved`/`nothing_to_save`
  // step advances the cutoff.
  const reconciled = (p: PriorWorkspace) =>
    p.checkpoint_state === "saved" || p.checkpoint_state === "nothing_to_save";
  let cutoff = -1;
  for (const [i, p] of opts.prior.entries()) {
    if (mine(p) && reconciled(p)) cutoff = i;
  }
  const elsewhere = opts.prior.slice(cutoff + 1).filter((p) => !mine(p));
  const unavailable = elsewhere.filter((p) => p.checkpoint_state === "saved");
  if (unavailable.length)
    return refuse(
      "env_workspace_prior_unavailable",
      `a checkpoint of unpublished work exists but is not restorable here: ` +
        `${unavailable.map(describePrior).join("; ")}. The checkpoint store of that node ` +
        "must be reachable (ARGUS_CHECKPOINT_DIR) or the work published from there; " +
        "no empty workspace was created",
    );
  const unproven = elsewhere.filter(
    (p) => p.checkpoint_state !== "nothing_to_save" && !opts.publishedHead,
  );
  if (unproven.length)
    return refuse(
      "env_workspace_prior_unverified",
      `earlier attempts on another instance may hold unpublished work: ` +
        `${unproven.map(describePrior).join("; ")}. Absence is not proven; ` +
        "publish or checkpoint that work from there, or retry on that instance. " +
        "No empty workspace was created",
      // Only steps that may still be on their way (not reported, or running) can resolve
      // by themselves: an attempt that just ended has not had time to report yet.
      unproven.every((p) => !p.checkpoint_state || p.checkpoint_state === "running")
        ? true
        : undefined,
    );
  return null;
}

const fail = (reason: WorkspaceFailureReason, error: string): WorkspaceResult => ({
  ok: false,
  reason,
  error,
});

const code = (err: unknown) => (err as NodeJS.ErrnoException | null)?.code;
const isPermission = (err: unknown) => ["EACCES", "EPERM", "EROFS"].includes(code(err) ?? "");

/** True when the URL carries userinfo (a credential must never reach .git/config). */
export function hasCredentials(url: string): boolean {
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
  // GitHub owner and repository names are case-insensitive: an origin cloned under another
  // spelling of the same github.com repository is the task's work, kept as written.
  if (urls.length > 1 || !sameRepositoryRemote(urls[0]!, repositoryUrl))
    return fail(
      "env_workspace_remote_mismatch",
      urls.some(hasCredentials)
        ? "origin carries credentials"
        : `origin is ${urls.join(", ")}, expected ${repositoryUrl}${caseHint(repositoryUrl)}`,
    );
  const head = await git(["symbolic-ref", "-q", "HEAD"], real, realRoot).catch(() => "");
  if (head !== `refs/heads/${branch}`) {
    const stranded = await strandedCommits(real, realRoot, branch, head, git);
    if (stranded.length) {
      const named = stranded.slice(0, MAX_NAMED_SHAS).join(", ");
      const more =
        stranded.length > MAX_NAMED_SHAS ? ` and ${stranded.length - MAX_NAMED_SHAS} more` : "";
      return fail(
        "env_workspace_unpreserved_commits",
        `HEAD is ${head || "detached"}, not refs/heads/${branch}, and ${stranded.length} unpublished ` +
          `commit(s) (no remote-tracking ref has them) would be left behind: ${named}${more}. ` +
          "The workspace is untouched; publish or archive that work, then retry",
      );
    }
    return fail(
      "env_workspace_branch_mismatch",
      `HEAD is ${head || "detached"}, expected refs/heads/${branch}`,
    );
  }
  return null;
}

function caseHint(repositoryUrl: string): string {
  try {
    return normalizeRepositoryUrl(repositoryUrl).provider === "github"
      ? " (GitHub owner and repository may differ only in letter case)"
      : "";
  } catch {
    return "";
  }
}

/** After validation succeeds, ensure worker hooksPath (idempotent). */
async function adoptExistingWorkspace(ws: string, realRoot: string, git: GitRunner): Promise<void> {
  await ensureWorkerGitHooks(await realpath(ws), realRoot, git);
}

/** Point the workspace at the image-managed commit-msg hook (host git keeps hooks disabled). */
async function ensureWorkerGitHooks(ws: string, realRoot: string, git: GitRunner): Promise<void> {
  const cfg = await git(
    ["config", "--file", path.join(ws, ".git", "config"), "--get", "core.hooksPath"],
    ws,
    realRoot,
  ).catch(() => "");
  if (cfg.trim() === WORKER_GIT_HOOKS_PATH) return;
  await git(["config", "core.hooksPath", WORKER_GIT_HOOKS_PATH], ws, realRoot);
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
    if (exists) {
      const validationError = await validate(ws, realRoot, repositoryUrl, branch, git);
      if (validationError) return validationError;
      await adoptExistingWorkspace(ws, realRoot, git);
      return done(false);
    }
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
      await ensureWorkerGitHooks(tmp, realRoot, git);
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
    const validationError = await validate(ws, realRoot, repositoryUrl, branch, git);
    if (validationError) return validationError;
    await adoptExistingWorkspace(ws, realRoot, git);
    return done(false);
  } catch (err) {
    if (isPermission(err)) return fail("env_workspace_permission", String(err));
    return fail("env_workspace_invalid", String(err));
  }
}
