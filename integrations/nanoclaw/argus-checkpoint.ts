/**
 * Task WIP checkpoints (task checkpoint spec): when an attempt ends without success,
 * the unpublished work of its task workspace (local commits, staged, unstaged and
 * untracked files) is written to a node-local store, hash-verified; the next attempt
 * restores it into its own instance before any model starts, or the task is blocked
 * with a typed reason. Nothing here deletes or overwrites existing work, touches the
 * network or handles a credential. The store is on one node: a checkpoint carries its
 * node id and is refused anywhere else.
 */
import { execFile } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { constants, type Dirent } from "node:fs";
import {
  chmod,
  copyFile,
  link,
  lstat,
  mkdir,
  open,
  readdir,
  readFile,
  readlink,
  realpath,
  rename,
  rm,
  stat,
  symlink,
  unlink,
  writeFile,
} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import {
  defaultGit,
  type GitRunner,
  hasCredentials,
  TASK_WORKSPACES_DIR,
  taskBranch,
  taskWorkspacePath,
} from "./argus-workspace.js";

const run = promisify(execFile);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const CKPT_ID = /^[0-9]{14}-[0-9a-f]{12}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const FORMAT = 1;
const DEFAULT_MAX_BYTES = 256 * 1024 * 1024;
const MAX_UNTRACKED_FILES = 5000;
const MAX_NAMES = 5000;
const KEEP_PER_TASK = 3;
const LEASE_TTL_MS = 120_000;
const LEASE_FILE = ".lease";
const MANIFEST = "manifest.json";

export type CheckpointFailureReason =
  | "env_checkpoint_unavailable" // expected, but absent, incomplete, or from another node
  | "env_checkpoint_invalid" // fails integrity, identity or safety checks
  | "env_checkpoint_conflict" // the destination already holds different work
  | "env_checkpoint_busy" // another holder owns the task's checkpoint lease
  | "env_checkpoint_limit"; // the work exceeds the size or file limits

export type CheckpointFailure = { ok: false; reason: CheckpointFailureReason; error: string };

export interface CheckpointFile {
  name: string; // file inside the checkpoint directory
  bytes: number;
  sha256: string;
}
export interface UntrackedEntry {
  path: string;
  type: "file" | "symlink";
  mode: number;
  stored?: string; // name of the copy (files)
  bytes?: number;
  sha256?: string;
  target?: string; // symlinks
}
export interface CheckpointManifest {
  format: number;
  task_id: string;
  checkpoint_id: string;
  attempt: number;
  node_id: string;
  repository_url: string;
  branch: string;
  head: string | null;
  created_at: string;
  files: CheckpointFile[];
  staged: string[];
  unstaged: string[];
  untracked: UntrackedEntry[];
  skipped_sensitive: string[];
  skipped_nested?: string[]; // nested repositories, by directory
  sensitive_in_commits: string[];
}

export type CheckpointResult =
  | { ok: true; empty: true }
  | { ok: true; empty: false; id: string; manifestSha256: string; ref: string; bytes: number }
  | CheckpointFailure;

export type RestoreResult =
  | { ok: true; restored: false; reason: "none" | "already_present" }
  | { ok: true; restored: true; id: string; manifestSha256: string; head: string | null }
  | CheckpointFailure;

const fail = (reason: CheckpointFailureReason, error: string): CheckpointFailure => ({
  ok: false,
  reason,
  error,
});

class Failure extends Error {
  constructor(
    readonly reason: CheckpointFailureReason,
    message: string,
  ) {
    super(message);
  }
}

/** A name the node can be told apart by; tests pass distinct ids to simulate two nodes. */
export function defaultNodeId(): string {
  return process.env.ARGUS_NODE_ID || os.hostname();
}

export function maxCheckpointBytesFromEnv(): number {
  const n = Number(process.env.ARGUS_CHECKPOINT_MAX_BYTES);
  return Number.isInteger(n) && n > 0 ? n : DEFAULT_MAX_BYTES;
}

/** `checkpoint:<id>:<sha256 of manifest.json>`, the reference reported to Argus. */
export function checkpointRef(id: string, manifestSha256: string): string {
  return `checkpoint:${id}:${manifestSha256}`;
}

export function parseCheckpointRef(
  ref: string | null | undefined,
): { id: string; sha256: string } | null {
  const m = /^checkpoint:([0-9]{14}-[0-9a-f]{12}):([0-9a-f]{64})$/.exec(ref ?? "");
  return m ? { id: m[1], sha256: m[2] } : null;
}

const SENSITIVE_DIRS = [".ssh", ".aws", ".gnupg", ".config/gh", ".docker", "secrets"];
const SENSITIVE_FILES = [
  /^\.env/,
  /\.pem$/,
  /\.key$/,
  /^id_/,
  /^\.netrc$/,
  /^\.git-credentials$/,
  /^auth\.json$/,
  /^credentials/,
  /^\.credentials\.json$/,
];

export function isSensitive(rel: string): boolean {
  const parts = rel.split("/");
  const joined = `/${parts.join("/")}/`;
  if (SENSITIVE_DIRS.some((d) => joined.includes(`/${d}/`))) return true;
  const last = parts[parts.length - 1];
  return SENSITIVE_FILES.some((re) => re.test(last));
}

function safeRel(p: string): boolean {
  return (
    p.length > 0 &&
    !p.startsWith("/") &&
    !p.includes("\0") &&
    !p.split("/").some((s) => s === ".." || s === "." || s === "") &&
    p.split("/")[0] !== ".git"
  );
}

const sha256Of = (buf: Buffer | string) => createHash("sha256").update(buf).digest("hex");

async function hashFile(file: string): Promise<{ sha256: string; bytes: number }> {
  const fh = await open(file, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const h = createHash("sha256");
    let bytes = 0;
    for await (const chunk of fh.createReadStream({ autoClose: false })) {
      h.update(chunk as Buffer);
      bytes += (chunk as Buffer).length;
    }
    return { sha256: h.digest("hex"), bytes };
  } finally {
    await fh.close();
  }
}

/** Raw git output (patches may be binary and need their final newline) straight to a file. */
async function gitToFile(
  args: string[],
  cwd: string,
  ceiling: string,
  file: string,
): Promise<void> {
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
    { cwd, env, timeout: 60_000, encoding: "buffer", maxBuffer: 1024 * 1024 * 1024 },
  );
  await writeFile(file, stdout, { mode: 0o600, flag: "wx" });
}

/** The store root must be a real directory, owned by us, closed to other users. */
async function checkStore(root: string): Promise<string> {
  await mkdir(root, { recursive: true, mode: 0o700 });
  const st = await lstat(root);
  if (st.isSymbolicLink() || !st.isDirectory())
    throw new Failure("env_checkpoint_invalid", "the checkpoint store is not a real directory");
  if (typeof process.getuid === "function" && st.uid !== process.getuid())
    throw new Failure("env_checkpoint_invalid", "the checkpoint store belongs to another user");
  if ((st.mode & 0o077) !== 0)
    throw new Failure("env_checkpoint_invalid", "the checkpoint store is open to other users");
  return realpath(root);
}

async function taskDir(root: string, taskId: string, create: boolean): Promise<string | null> {
  if (!UUID.test(taskId)) throw new Failure("env_checkpoint_invalid", "malformed task id");
  const real = await checkStore(root);
  const dir = path.join(real, taskId);
  let st = await lstat(dir).catch(() => null);
  if (!st) {
    if (!create) return null;
    try {
      await mkdir(dir, { mode: 0o700 });
      return dir;
    } catch (err) {
      // A concurrent holder created it first: validate theirs like any existing directory.
      if ((err as NodeJS.ErrnoException).code !== "EEXIST") throw err;
      st = await lstat(dir);
    }
  }
  if (st.isSymbolicLink() || !st.isDirectory() || (st.mode & 0o077) !== 0)
    throw new Failure("env_checkpoint_invalid", "the task checkpoint directory is not private");
  return dir;
}

interface LeaseBody {
  owner: string;
  nonce: string;
  expires_at: number;
}

/**
 * A lease on the task's checkpoint directory: created exclusively, expires after a TTL.
 * An expired one is taken over by whoever first creates the takeover token named after
 * it, and replaced by an atomic rename: the lease file never goes missing, so neither a
 * late taker nor a newcomer can slip in. `beforeTakeover` lets tests interleave racers.
 */
export async function acquireLease(
  dir: string,
  owner: string,
  now: () => number,
  ttlMs: number,
  beforeTakeover?: () => Promise<void>,
): Promise<() => Promise<void>> {
  const file = path.join(dir, LEASE_FILE);
  const body: LeaseBody = {
    owner,
    nonce: randomBytes(8).toString("hex"),
    expires_at: now() + ttlMs,
  };
  const text = JSON.stringify(body);
  const tmp = path.join(dir, `.lease-${body.nonce}`);
  await writeFile(tmp, text, { mode: 0o600, flag: "wx" });
  try {
    try {
      await link(tmp, file); // fails with EEXIST when another holder exists
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== "EEXIST") throw err;
      const expired = (s: string | null) => {
        try {
          return s !== null && (JSON.parse(s) as LeaseBody).expires_at <= now();
        } catch {
          return false; // unreadable: fail closed
        }
      };
      const seen = await readFile(file, "utf8").catch(() => null);
      if (!expired(seen))
        throw new Failure("env_checkpoint_busy", "another holder has the task's checkpoint lease");
      await beforeTakeover?.();
      // A token left by a taker that died is expired too, and taken over the same way.
      const dead: string[] = [];
      let token = path.join(dir, `.lease-takeover-${sha256Of(seen as string).slice(0, 16)}`);
      for (;;) {
        try {
          await writeFile(token, text, { mode: 0o600, flag: "wx" });
          break;
        } catch (e) {
          if ((e as NodeJS.ErrnoException).code !== "EEXIST") throw e;
          const held = await readFile(token, "utf8").catch(() => null);
          if (dead.length >= 3 || !expired(held))
            throw new Failure(
              "env_checkpoint_busy",
              "another holder is taking over the expired lease",
            );
          dead.push(token);
          token = `${token}-${sha256Of(held as string).slice(0, 8)}`;
        }
      }
      try {
        if ((await readFile(file, "utf8").catch(() => null)) !== seen)
          throw new Failure(
            "env_checkpoint_busy",
            "the checkpoint lease changed while taking it over",
          );
        await rename(tmp, file);
      } finally {
        // Every token here names the lease just replaced (or changed): none guards anything now.
        for (const t of [token, ...dead]) await unlink(t).catch(() => undefined);
      }
    }
  } finally {
    await unlink(tmp).catch(() => undefined);
  }
  return async () => {
    const cur = await readFile(file, "utf8").catch(() => null);
    if (cur === text) await unlink(file).catch(() => undefined);
  };
}

interface WorkspaceCtx {
  ws: string;
  ceiling: string;
}

/** Identity checks of a source workspace, read-only: real directory, our origin, our branch. */
async function openWorkspace(
  groupDir: string,
  taskId: string,
  repositoryUrl: string,
  git: GitRunner,
): Promise<WorkspaceCtx | null> {
  const ws = taskWorkspacePath(groupDir, taskId);
  const st = await lstat(ws).catch(() => null);
  if (!st) return null;
  if (st.isSymbolicLink() || !st.isDirectory())
    throw new Failure("env_checkpoint_invalid", "the workspace is not a real directory");
  const ceiling = path.dirname(await realpath(ws));
  const dotGit = await lstat(path.join(ws, ".git")).catch(() => null);
  if (!dotGit?.isDirectory() || dotGit.isSymbolicLink())
    throw new Failure("env_checkpoint_invalid", "the workspace is not a git repository");
  const origin = (
    await git(
      ["config", "--file", path.join(ws, ".git", "config"), "--get-all", "remote.origin.url"],
      ws,
      ceiling,
    ).catch(() => "")
  )
    .split("\n")
    .filter(Boolean);
  if (origin.length !== 1 || origin[0] !== repositoryUrl)
    throw new Failure(
      "env_checkpoint_invalid",
      "the workspace origin does not match the task repository",
    );
  const head = await git(["symbolic-ref", "-q", "HEAD"], ws, ceiling).catch(() => "");
  if (head !== `refs/heads/${taskBranch(taskId)}`)
    throw new Failure("env_checkpoint_invalid", "the workspace is not on the task branch");
  return { ws, ceiling };
}

// The workspace's own git config is written by the worker, yet git runs on the host here:
// `diff` runs `filter.*.clean`, `log` runs `gpg.program` under `log.showSignature`,
// `core.worktree` points git at host files. Only keys known to be inert are accepted.
const INERT_CONFIG = [
  /^core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|precomposeunicode|symlinks|autocrlf|eol|safecrlf|quotepath|checkstat|trustctime|untrackedcache)$/,
  /^remote\..+\.(url|pushurl|fetch|push|tagopt|prune)$/,
  /^branch\..+\.(remote|merge|rebase|pushremote|description)$/,
  /^(user|author|committer|init|pull|push|fetch|commit|tag|rebase|color|advice|status|gc|index|pack|feature)\.[^.]+$/,
  /^extensions\.objectformat$/,
];

/** Fails closed unless the workspace's git setup can neither run a program nor read elsewhere. */
async function assertInertGit(ctx: WorkspaceCtx, git: GitRunner): Promise<void> {
  const dotGit = path.join(ctx.ws, ".git");
  for (const redirect of ["commondir", "objects/info/alternates", "objects/info/http-alternates"])
    if (await lstat(path.join(dotGit, redirect)).catch(() => null))
      throw new Failure("env_checkpoint_invalid", `the workspace git directory has ${redirect}`);
  // --file reads only this file and follows no include; an unreadable config fails closed.
  const listed = await git(
    ["config", "--file", path.join(dotGit, "config"), "--list", "-z"],
    ctx.ws,
    ctx.ceiling,
  ).catch(() => {
    throw new Failure("env_checkpoint_invalid", "the workspace git config is unreadable");
  });
  for (const entry of z(listed)) {
    const key = entry.split("\n", 1)[0];
    if (!INERT_CONFIG.some((re) => re.test(key)))
      throw new Failure(
        "env_checkpoint_invalid",
        `the workspace git config sets ${key.slice(0, 100)}, which git could act on`,
      );
  }
}

const z = (s: string) => s.split("\0").filter(Boolean);

interface Observed {
  head: string | null;
  staged: string[];
  unstaged: string[];
  untracked: string[];
  localCommits: number;
  sensitiveInCommits: string[];
}

async function observe(ctx: WorkspaceCtx, git: GitRunner): Promise<Observed> {
  const { ws, ceiling } = ctx;
  const head =
    (await git(["rev-parse", "--verify", "-q", "HEAD"], ws, ceiling).catch(() => "")) || null;
  const diff = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z"];
  const staged = z(await git([...diff, "--cached"], ws, ceiling));
  const unstaged = z(await git(diff, ws, ceiling));
  const untracked = z(await git(["ls-files", "--others", "--exclude-standard", "-z"], ws, ceiling));
  let localCommits = 0;
  let sensitiveInCommits: string[] = [];
  if (head) {
    localCommits = Number(
      (await git(["rev-list", "--count", "--all", "--not", "--remotes"], ws, ceiling)) || 0,
    );
    if (localCommits > 0)
      sensitiveInCommits = [
        ...new Set(
          z(
            await git(
              ["log", "--all", "--not", "--remotes", "--name-only", "-z", "--format="],
              ws,
              ceiling,
            ),
          ).filter(isSensitive),
        ),
      ].sort();
  }
  return { head, staged, unstaged, untracked, localCommits, sensitiveInCommits };
}

function pathspecExcludes(): string[] {
  return [
    ...SENSITIVE_DIRS.map((d) => `:(exclude,glob)**/${d}/**`),
    ":(exclude,glob)**/.env*",
    ":(exclude,glob)**/*.pem",
    ":(exclude,glob)**/*.key",
    ":(exclude,glob)**/id_*",
    ":(exclude,glob)**/.netrc",
    ":(exclude,glob)**/.git-credentials",
    ":(exclude,glob)**/auth.json",
    ":(exclude,glob)**/credentials*",
    ":(exclude,glob)**/.credentials.json",
  ];
}

const stamp = (d: Date) => d.toISOString().replace(/[-:T]/g, "").slice(0, 14);

export interface CheckpointOpts {
  groupDir: string | null;
  storeRoot: string | null;
  taskId: string;
  attempt: number;
  repositoryUrl: string | null | undefined;
  nodeId?: string;
  maxBytes?: number;
  git?: GitRunner;
  now?: () => number;
  leaseTtlMs?: number;
}

/**
 * Write a verified checkpoint of the task workspace. `empty` when nothing is
 * unpublished (or no workspace exists). Never modifies the workspace.
 */
export async function checkpointTaskWorkspace(opts: CheckpointOpts): Promise<CheckpointResult> {
  const { groupDir, storeRoot, taskId, repositoryUrl } = opts;
  const git = opts.git ?? defaultGit;
  const now = opts.now ?? Date.now;
  if (!groupDir || !storeRoot || !repositoryUrl)
    return fail(
      "env_checkpoint_unavailable",
      "the group directory, store or repository is not configured",
    );
  if (hasCredentials(repositoryUrl))
    return fail("env_checkpoint_invalid", "the repository URL carries credentials");
  const maxBytes = opts.maxBytes ?? maxCheckpointBytesFromEnv();
  let release: (() => Promise<void>) | null = null;
  let tmp: string | null = null;
  try {
    const ctx = await openWorkspace(groupDir, taskId, repositoryUrl, git);
    if (!ctx) return { ok: true, empty: true };
    await assertInertGit(ctx, git);
    const seen = await observe(ctx, git);
    if (
      !seen.localCommits &&
      !seen.staged.length &&
      !seen.unstaged.length &&
      !seen.untracked.length
    )
      return { ok: true, empty: true };
    if (
      seen.untracked.length > MAX_UNTRACKED_FILES ||
      seen.staged.length + seen.unstaged.length > MAX_NAMES
    )
      throw new Failure("env_checkpoint_limit", "the workspace has too many changed files");
    const dir = (await taskDir(storeRoot, taskId, true)) as string;
    release = await acquireLease(
      dir,
      `checkpoint:${opts.attempt}`,
      now,
      opts.leaseTtlMs ?? LEASE_TTL_MS,
    );
    const created = new Date(now());
    const id = `${stamp(created)}-${randomBytes(6).toString("hex")}`;
    tmp = path.join(dir, `.tmp-${id}`);
    await mkdir(tmp, { mode: 0o700 });
    const files: CheckpointFile[] = [];
    let total = 0;
    const record = async (name: string) => {
      const h = await hashFile(path.join(tmp as string, name));
      total += h.bytes;
      if (total > maxBytes)
        throw new Failure("env_checkpoint_limit", "the work exceeds the checkpoint size limit");
      files.push({ name, ...h });
      return h;
    };
    if (seen.head) {
      await git(["bundle", "create", path.join(tmp, "work.bundle"), "--all"], ctx.ws, ctx.ceiling);
      await git(["bundle", "verify", "-q", path.join(tmp, "work.bundle")], ctx.ws, ctx.ceiling);
      await chmod(path.join(tmp, "work.bundle"), 0o600);
      await record("work.bundle");
    }
    const patch = [
      "diff",
      "--binary",
      "--no-ext-diff",
      "--no-textconv",
      "--no-color",
      "--no-renames",
      "--src-prefix=a/",
      "--dst-prefix=b/",
    ];
    const staged = seen.staged.filter((p) => !isSensitive(p));
    const unstaged = seen.unstaged.filter((p) => !isSensitive(p));
    for (const [label, extra, names] of [
      ["staged", ["--cached"], staged],
      ["unstaged", [], unstaged],
    ] as const) {
      if (!names.length) continue;
      await gitToFile(
        [...patch, ...extra, "--", ".", ...pathspecExcludes()],
        ctx.ws,
        ctx.ceiling,
        path.join(tmp, `${label}.patch`),
      );
      await record(`${label}.patch`);
    }
    const skipped = new Set(
      [...seen.staged, ...seen.unstaged, ...seen.untracked].filter(isSensitive),
    );
    const untracked: UntrackedEntry[] = [];
    const nested: string[] = [];
    let n = 0;
    for (const rel of seen.untracked) {
      if (isSensitive(rel)) continue;
      if (rel.endsWith("/")) {
        nested.push(rel); // git lists a nested repository as its directory: named, not copied
        continue;
      }
      if (!safeRel(rel)) throw new Failure("env_checkpoint_invalid", "an untracked path is unsafe");
      const full = path.join(ctx.ws, rel);
      const st = await lstat(full);
      if (st.isSymbolicLink()) {
        const target = await readlink(full);
        if (path.isAbsolute(target) || !safeLinkTarget(rel, target))
          throw new Failure(
            "env_checkpoint_invalid",
            "an untracked symlink points outside the workspace",
          );
        untracked.push({ path: rel, type: "symlink", mode: st.mode & 0o777, target });
        continue;
      }
      if (!st.isFile()) continue; // special files are not copied
      const stored = `untracked-${n++}`;
      await copyFile(full, path.join(tmp, stored), constants.COPYFILE_EXCL);
      await chmod(path.join(tmp, stored), 0o600);
      const h = await record(stored);
      untracked.push({
        path: rel,
        type: "file",
        mode: st.mode & 0o777,
        stored,
        bytes: h.bytes,
        sha256: h.sha256,
      });
    }
    const manifest: CheckpointManifest = {
      format: FORMAT,
      task_id: taskId,
      checkpoint_id: id,
      attempt: opts.attempt,
      node_id: opts.nodeId ?? defaultNodeId(),
      repository_url: repositoryUrl,
      branch: taskBranch(taskId),
      head: seen.head,
      created_at: created.toISOString(),
      files,
      staged,
      unstaged,
      untracked,
      skipped_sensitive: [...skipped].sort(),
      skipped_nested: nested.sort(),
      sensitive_in_commits: seen.sensitiveInCommits,
    };
    const text = JSON.stringify(manifest, null, 2);
    await writeFile(path.join(tmp, MANIFEST), text, { mode: 0o600, flag: "wx" });
    const final = path.join(dir, id);
    await rename(tmp, final); // complete only once the manifest is in place
    tmp = null;
    const manifestSha256 = sha256Of(text);
    await prune(dir, id);
    return {
      ok: true,
      empty: false,
      id,
      manifestSha256,
      ref: checkpointRef(id, manifestSha256),
      bytes: total,
    };
  } catch (err) {
    return toFailure(err, "env_checkpoint_invalid");
  } finally {
    if (tmp) await rm(tmp, { recursive: true, force: true });
    if (release) await release();
  }
}

function toFailure(err: unknown, fallback: CheckpointFailureReason): CheckpointFailure {
  if (err instanceof Failure) return fail(err.reason, err.message);
  const e = err as (NodeJS.ErrnoException & { killed?: boolean }) | null;
  if (e?.code === "ENOSPC" || e?.code === "EDQUOT")
    return fail("env_checkpoint_limit", `no space: ${e.code}`);
  // A git step past its timeout (a large history to bundle) or its output bound.
  if (e?.killed || e?.code === "ERR_CHILD_PROCESS_STDIO_MAXBUFFER")
    return fail("env_checkpoint_limit", "a git step exceeded its time or output limit");
  return fail(fallback, String(err instanceof Error ? err.message : err).slice(0, 300));
}

function safeLinkTarget(rel: string, target: string): boolean {
  const resolved = path.posix.normalize(path.posix.join(path.posix.dirname(rel), target));
  return (
    !resolved.startsWith("..") &&
    resolved !== "." &&
    !path.posix.isAbsolute(resolved) &&
    resolved.split("/")[0] !== ".git"
  );
}

/** Keep the newest KEEP_PER_TASK complete checkpoints; only this task's, never the one just written. */
async function prune(dir: string, keep: string): Promise<void> {
  const ids = (await completeIds(dir)).sort();
  for (const id of ids.slice(0, Math.max(0, ids.length - KEEP_PER_TASK))) {
    if (id !== keep) await rm(path.join(dir, id), { recursive: true, force: true });
  }
}

async function completeIds(dir: string): Promise<string[]> {
  const entries: Dirent[] = await readdir(dir, { withFileTypes: true });
  const out: string[] = [];
  for (const e of entries) {
    if (!CKPT_ID.test(e.name) || !e.isDirectory()) continue;
    if (
      await stat(path.join(dir, e.name, MANIFEST)).then(
        () => true,
        () => false,
      )
    )
      out.push(e.name);
  }
  return out;
}

/** Reads and verifies one checkpoint; throws Failure on any mismatch. */
async function loadVerified(
  dir: string,
  id: string,
  expect: { taskId: string; repositoryUrl: string; nodeId: string; sha256?: string },
): Promise<{ manifest: CheckpointManifest; manifestSha256: string; dir: string }> {
  const cdir = path.join(dir, id);
  const st = await lstat(cdir).catch(() => null);
  if (!st || st.isSymbolicLink() || !st.isDirectory() || (st.mode & 0o077) !== 0)
    throw new Failure("env_checkpoint_invalid", "the checkpoint directory is not private");
  const mfile = path.join(cdir, MANIFEST);
  const mst = await lstat(mfile).catch(() => null);
  if (!mst)
    throw new Failure("env_checkpoint_unavailable", "the checkpoint is incomplete (no manifest)");
  if (!mst.isFile())
    throw new Failure("env_checkpoint_invalid", "the manifest is not a regular file");
  const raw = await readFile(mfile, "utf8");
  const manifestSha256 = sha256Of(raw);
  if (expect.sha256 && manifestSha256 !== expect.sha256)
    throw new Failure(
      "env_checkpoint_invalid",
      "the manifest digest differs from the expected reference",
    );
  let m: CheckpointManifest;
  try {
    m = JSON.parse(raw) as CheckpointManifest;
  } catch {
    throw new Failure("env_checkpoint_invalid", "the manifest is not valid JSON");
  }
  if (m.format !== FORMAT)
    throw new Failure("env_checkpoint_invalid", "unsupported checkpoint format");
  if (
    m.task_id !== expect.taskId ||
    m.checkpoint_id !== id ||
    m.branch !== taskBranch(expect.taskId)
  )
    throw new Failure("env_checkpoint_invalid", "the checkpoint belongs to another task or branch");
  if (m.repository_url !== expect.repositoryUrl)
    throw new Failure("env_checkpoint_invalid", "the checkpoint belongs to another repository");
  if (m.node_id !== expect.nodeId)
    throw new Failure(
      "env_checkpoint_unavailable",
      `the checkpoint was written on node ${m.node_id}; there is no proven transport to ${expect.nodeId}`,
    );
  if (
    !Array.isArray(m.files) ||
    !Array.isArray(m.untracked) ||
    !Array.isArray(m.staged) ||
    !Array.isArray(m.unstaged)
  )
    throw new Failure("env_checkpoint_invalid", "the manifest is malformed");
  if (m.head !== null && !/^[0-9a-f]{40}([0-9a-f]{24})?$/.test(m.head))
    throw new Failure("env_checkpoint_invalid", "the manifest head is malformed");
  const names = new Set<string>();
  for (const f of m.files) {
    if (
      !/^[A-Za-z0-9._-]+$/.test(f.name ?? "") ||
      f.name === MANIFEST ||
      !SHA256.test(f.sha256 ?? "")
    )
      throw new Failure("env_checkpoint_invalid", "the manifest names an unsafe file");
    const fst = await lstat(path.join(cdir, f.name)).catch(() => null);
    if (!fst)
      throw new Failure(
        "env_checkpoint_unavailable",
        `the checkpoint is incomplete (${f.name} is missing)`,
      );
    if (!fst.isFile() || fst.size !== f.bytes)
      throw new Failure("env_checkpoint_invalid", `${f.name} has the wrong size`);
    const h = await hashFile(path.join(cdir, f.name));
    if (h.sha256 !== f.sha256)
      throw new Failure("env_checkpoint_invalid", `${f.name} fails its hash`);
    names.add(f.name);
  }
  for (const u of m.untracked) {
    if (!safeRel(u.path))
      throw new Failure("env_checkpoint_invalid", "an untracked path is unsafe");
    if (u.type === "file") {
      const f = m.files.find((x) => x.name === u.stored);
      if (!f || f.sha256 !== u.sha256 || !names.has(u.stored as string))
        throw new Failure(
          "env_checkpoint_invalid",
          "an untracked file is not covered by the manifest",
        );
    } else if (
      u.type !== "symlink" ||
      typeof u.target !== "string" ||
      path.isAbsolute(u.target) ||
      !safeLinkTarget(u.path, u.target)
    ) {
      throw new Failure("env_checkpoint_invalid", "an untracked entry is unsafe");
    }
  }
  if (
    (m.head !== null) !== names.has("work.bundle") &&
    (m.head !== null || names.has("work.bundle"))
  )
    throw new Failure("env_checkpoint_unavailable", "the checkpoint is incomplete (bundle)");
  if (
    m.staged.length > 0 !== names.has("staged.patch") ||
    m.unstaged.length > 0 !== names.has("unstaged.patch")
  )
    throw new Failure("env_checkpoint_unavailable", "the checkpoint is incomplete (patch)");
  return { manifest: m, manifestSha256, dir: cdir };
}

export interface RestoreOpts {
  groupDir: string | null;
  storeRoot: string | null;
  taskId: string;
  repositoryUrl: string | null | undefined;
  /** From the task's continuation WIP reference: that exact checkpoint is required. */
  expected?: { id: string; sha256: string } | null;
  attempt: number;
  nodeId?: string;
  git?: GitRunner;
  now?: () => number;
  leaseTtlMs?: number;
}

/**
 * Restore the latest verified checkpoint of the task into its workspace, before any
 * model starts. `none` when no checkpoint exists and none was expected. Existing work
 * in the destination is never replaced: it must be pristine, or already equal.
 */
export async function restoreTaskWorkspace(opts: RestoreOpts): Promise<RestoreResult> {
  const { groupDir, storeRoot, taskId, repositoryUrl } = opts;
  const git = opts.git ?? defaultGit;
  const now = opts.now ?? Date.now;
  const nodeId = opts.nodeId ?? defaultNodeId();
  if (!groupDir || !storeRoot || !repositoryUrl)
    return fail(
      "env_checkpoint_unavailable",
      "the group directory, store or repository is not configured",
    );
  if (hasCredentials(repositoryUrl))
    return fail("env_checkpoint_invalid", "the repository URL carries credentials");
  let release: (() => Promise<void>) | null = null;
  let tmp: string | null = null;
  try {
    const dir = await taskDir(storeRoot, taskId, false);
    if (!dir) {
      if (opts.expected)
        throw new Failure(
          "env_checkpoint_unavailable",
          "the expected checkpoint is not in this node's store",
        );
      return { ok: true, restored: false, reason: "none" };
    }
    release = await acquireLease(
      dir,
      `restore:${opts.attempt}`,
      now,
      opts.leaseTtlMs ?? LEASE_TTL_MS,
    );
    const ids = (await completeIds(dir)).sort();
    let id: string;
    if (opts.expected) {
      if (!ids.includes(opts.expected.id))
        throw new Failure(
          "env_checkpoint_unavailable",
          "the expected checkpoint is not in this node's store",
        );
      id = opts.expected.id;
    } else if (ids.length) {
      id = ids[ids.length - 1];
    } else {
      return { ok: true, restored: false, reason: "none" };
    }
    // Only the chosen checkpoint is validated: a damaged older one does not block a good newer one.
    const { manifest: m, manifestSha256 } = await loadVerified(dir, id, {
      taskId,
      repositoryUrl,
      nodeId,
      sha256: opts.expected?.sha256,
    });
    const cdir = path.join(dir, id);
    const ws = taskWorkspacePath(groupDir, taskId);
    const root = path.dirname(ws);
    await mkdir(root, { recursive: true, mode: 0o700 });
    const rst = await lstat(root);
    if (rst.isSymbolicLink() || !rst.isDirectory())
      throw new Failure("env_checkpoint_invalid", "the workspaces root is not a real directory");
    const realRoot = await realpath(root);
    if (realRoot !== path.join(await realpath(groupDir), TASK_WORKSPACES_DIR))
      throw new Failure("env_checkpoint_invalid", "the workspaces root resolves outside the group");

    const existing = await lstat(ws).catch(() => null);
    if (existing) {
      const ctx = await openWorkspace(groupDir, taskId, repositoryUrl, git).catch((err) => {
        throw err instanceof Failure ? new Failure("env_checkpoint_conflict", err.message) : err;
      });
      await assertInertGit(ctx as WorkspaceCtx, git);
      const here = await observe(ctx as WorkspaceCtx, git);
      if (matchesManifest(here, m, await localHeadsMatch(ctx as WorkspaceCtx, git, m)))
        return { ok: true, restored: false, reason: "already_present" };
      if (
        here.head ||
        here.localCommits ||
        here.staged.length ||
        here.unstaged.length ||
        here.untracked.length
      )
        throw new Failure(
          "env_checkpoint_conflict",
          "the workspace already holds different work; nothing was replaced",
        );
    }

    tmp = path.join(realRoot, `.tmp-restore-${taskId}-${randomBytes(4).toString("hex")}`);
    await mkdir(tmp, { mode: 0o700 });
    const g = (args: string[]) => git(args, tmp as string, realRoot);
    await git(["init", "-q", "--template=", tmp], realRoot, realRoot);
    await g(["symbolic-ref", "HEAD", `refs/heads/${m.branch}`]);
    await g(["remote", "add", "origin", repositoryUrl]);
    if (m.head) {
      await g(["fetch", "-q", "--update-head-ok", path.join(cdir, "work.bundle"), "refs/*:refs/*"]);
      await g(["reset", "-q", "--hard", m.head]);
      const got = await g(["rev-parse", "--verify", "HEAD"]);
      if (got !== m.head)
        throw new Failure("env_checkpoint_invalid", "the restored head differs from the manifest");
    }
    if (m.staged.length) await g(["apply", "--index", "--binary", path.join(cdir, "staged.patch")]);
    if (m.unstaged.length) await g(["apply", "--binary", path.join(cdir, "unstaged.patch")]);
    for (const u of m.untracked) await restoreUntracked(tmp, cdir, u);
    const after = await observe({ ws: tmp, ceiling: realRoot }, git);
    if (!matchesManifest(after, m, true))
      throw new Failure(
        "env_checkpoint_invalid",
        "the restored workspace differs from the manifest",
      );
    if (existing) {
      // Verified pristine above (only our own bootstrap): move it aside, swap, then drop it.
      const aside = path.join(
        realRoot,
        `.tmp-pristine-${taskId}-${randomBytes(4).toString("hex")}`,
      );
      await rename(ws, aside);
      try {
        await rename(tmp, ws);
      } catch (err) {
        await rename(aside, ws).catch(() => undefined);
        throw err;
      }
      await rm(aside, { recursive: true, force: true });
    } else {
      await rename(tmp, ws);
    }
    tmp = null;
    return { ok: true, restored: true, id, manifestSha256, head: m.head };
  } catch (err) {
    return toFailure(err, "env_checkpoint_invalid");
  } finally {
    if (tmp) await rm(tmp, { recursive: true, force: true });
    if (release) await release();
  }
}

async function localHeadsMatch(
  ctx: WorkspaceCtx,
  git: GitRunner,
  m: CheckpointManifest,
): Promise<boolean> {
  if (!m.head) return true;
  const head = await git(["rev-parse", "--verify", "-q", "HEAD"], ctx.ws, ctx.ceiling).catch(
    () => "",
  );
  return head === m.head;
}

function sameSet(a: string[], b: string[]): boolean {
  return a.length === b.length && [...a].sort().every((v, i) => v === [...b].sort()[i]);
}

/** True when the observed workspace is exactly the checkpointed work. */
function matchesManifest(o: Observed, m: CheckpointManifest, headOk: boolean): boolean {
  if (!headOk || o.head !== m.head) return false;
  const tracked = (names: string[]) => names.filter((p) => !isSensitive(p));
  return (
    sameSet(tracked(o.staged), m.staged) &&
    sameSet(tracked(o.unstaged), m.unstaged) &&
    sameSet(
      o.untracked.filter((p) => !isSensitive(p) && !p.endsWith("/")),
      m.untracked.map((u) => u.path),
    )
  );
}

async function restoreUntracked(tmp: string, cdir: string, u: UntrackedEntry): Promise<void> {
  const parts = u.path.split("/");
  let parent = tmp;
  for (const part of parts.slice(0, -1)) {
    parent = path.join(parent, part);
    const st = await lstat(parent).catch(() => null);
    if (st?.isSymbolicLink())
      throw new Failure("env_checkpoint_invalid", "a restored path crosses a symlink");
    if (!st) await mkdir(parent, { mode: 0o755 });
  }
  const target = path.join(parent, parts[parts.length - 1]);
  if (await lstat(target).catch(() => null))
    throw new Failure("env_checkpoint_invalid", "a restored untracked path already exists");
  if (u.type === "symlink") {
    await symlink(u.target as string, target);
    return;
  }
  await copyFile(path.join(cdir, u.stored as string), target, constants.COPYFILE_EXCL);
  await chmod(target, u.mode & 0o777);
  const h = await hashFile(target);
  if (h.sha256 !== u.sha256)
    throw new Failure("env_checkpoint_invalid", "a restored file fails its hash");
}
