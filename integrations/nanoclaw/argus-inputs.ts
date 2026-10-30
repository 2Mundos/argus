/**
 * Task inputs (task inputs spec 3.4-3.7): the attachments an orchestrator shared with a
 * task's workers, downloaded by the channel (the host process, which holds the worker
 * token) into a task-local directory the session container mounts, verified by size and
 * SHA-256 before the model is told about them. The model reads files, never the API.
 *
 * Content never reaches a log line, an error, a receipt or the prompt: only artifact
 * ids, sizes, hashes and reason codes do.
 */
import { createHash } from "node:crypto";
import { constants as fsc } from "node:fs";
import {
  chmod,
  type FileHandle,
  lstat,
  mkdir,
  open,
  readFile,
  realpath,
  rename,
  unlink,
} from "node:fs/promises";
import path from "node:path";

/** Below the agent group directory; the session container sees it under /workspace/agent. */
export const TASK_INPUTS_DIR = "task-inputs";
export const INPUT_MANIFEST = "manifest.json";
export const INPUT_VERIFIER = "verify-inputs.mjs";
export const INPUT_CONSUMED = "consumed.json";
const PARTIAL_DIR = ".partial";
const CONTAINER_GROUP_DIR = "/workspace/agent";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SHA256 = /^[0-9a-f]{64}$/;
/** The server's artifact name rule (artifacts/service.py), re-checked on the host. */
const NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/;
const DEVICE = /^(con|prn|aux|nul|conin\$|conout\$|com[0-9]|lpt[0-9])(\..*)?$/i;
const RESERVED = new Set([INPUT_MANIFEST, INPUT_VERIFIER, INPUT_CONSUMED]);
const MAX_TRANSFERS = 3;
const BACKOFF_MS = 500;
const IDLE_TIMEOUT_MS = 60_000;
const HASH_CHUNK = 64 * 1024;
const CONSUMED_MAX_BYTES = 1024 * 1024;
const DEFAULT_MAX_BYTES = 100 * 1024 * 1024;
const DEFAULT_MAX_TOTAL_BYTES = 256 * 1024 * 1024;
const DEFAULT_MAX_FILES = 20;

export type InputFailureReason = "env_input_unavailable" | "env_input_invalid" | "env_input_limit";

/** One entry of `GET /tasks/{id}/inputs`, the only authority for id, size and hash. */
export interface TaskInputListing {
  id: string;
  task_id: string;
  name: string;
  media_type?: string;
  size: number;
  sha256: string;
  attached_by?: string | null;
  created_at?: string;
  worker_readable?: boolean;
  receipts?: unknown[];
}

export interface ManifestEntry {
  artifact_id: string;
  /** Null when the server's name was rejected: a rejected name is never shown. */
  name: string | null;
  size: number;
  sha256: string;
  state: "verified" | "failed";
  attempt: number;
  required: boolean;
  reason?: InputFailureReason;
}

export interface InputManifest {
  version: 1;
  task_id: string;
  attempt: number;
  inputs: ManifestEntry[];
}

export interface MaterializedInput extends ManifestEntry {
  /** Host and container paths; null for a failed input. */
  path: string | null;
  containerPath: string | null;
  /** True when a verified file of an earlier run was kept without a download. */
  reused: boolean;
}

export type InputsResult =
  | {
      ok: true;
      inputs: MaterializedInput[];
      manifest: InputManifest | null;
      /** The task inputs directory on the host, once one was written. */
      dir?: string;
      /** The listing could not be read and nothing was required (an older server, say). */
      skipped?: string;
    }
  | { ok: false; reason: InputFailureReason; error: string; artifactId?: string };

export interface InputReceipt {
  artifact_id: string;
  state: "materialized" | "verified" | "consumed" | "failed";
  sha256?: string;
  size?: number;
  reason?: InputFailureReason;
}

/** How the channel reaches Argus with its own worker token. */
export interface InputsSource {
  /** `GET /tasks/{id}/inputs`, parsed. Rejects with an error carrying `status` on an HTTP error. */
  list(taskId: string): Promise<unknown>;
  /** `GET /artifacts/{id}/content` with an optional `Range` header; any status resolves. */
  content(artifactId: string, range: string | null, signal: AbortSignal): Promise<Response>;
}

export interface InputLimits {
  maxBytes: number;
  maxTotalBytes: number;
  maxFiles: number;
}

const positive = (raw: string | undefined): number | null => {
  const n = Number(raw);
  return Number.isSafeInteger(n) && n > 0 ? n : null;
};

export function inputLimitsFromEnv(env: NodeJS.ProcessEnv = process.env): InputLimits {
  return {
    maxBytes:
      positive(env.ARGUS_TASK_INPUT_MAX_BYTES) ??
      positive(env.ARGUS_ARTIFACT_MAX_BYTES) ??
      DEFAULT_MAX_BYTES,
    maxTotalBytes: positive(env.ARGUS_TASK_INPUT_MAX_TOTAL_BYTES) ?? DEFAULT_MAX_TOTAL_BYTES,
    maxFiles: positive(env.ARGUS_TASK_INPUT_MAX_FILES) ?? DEFAULT_MAX_FILES,
  };
}

/** Argus task ids are canonical lowercase UUIDs; only those name an inputs directory. */
export function isCanonicalTaskId(taskId: string): boolean {
  return UUID.test(taskId);
}

function checkTaskId(taskId: string): void {
  if (!UUID.test(taskId)) throw new Error("task id must be a canonical lowercase UUID");
}

export function taskInputsPath(groupDir: string, taskId: string): string {
  checkTaskId(taskId);
  return path.join(groupDir, TASK_INPUTS_DIR, taskId);
}

export function taskInputsContainerPath(taskId: string): string {
  checkTaskId(taskId);
  return path.posix.join(CONTAINER_GROUP_DIR, TASK_INPUTS_DIR, taskId);
}

/** The continuation WIP reference of a required input: `attachment:<artifact id>:<sha256>`. */
export function parseInputRef(
  ref: string | null | undefined,
): { artifactId: string; sha256: string } | null {
  const m =
    /^attachment:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):([0-9a-f]{64})$/.exec(
      ref ?? "",
    );
  return m ? { artifactId: m[1], sha256: m[2] } : null;
}

/** Null when `name` is a safe file name in the task directory; otherwise why not. */
export function unsafeInputName(name: unknown): string | null {
  if (typeof name !== "string" || !NAME.test(name)) return "the name breaks the artifact name rule";
  if (/^\.+$/.test(name)) return "the name is dot-only";
  if (DEVICE.test(name)) return "the name is a device name";
  if (RESERVED.has(name.toLowerCase())) return "the name is reserved for the channel";
  return null;
}

class InputError extends Error {
  constructor(
    readonly reason: InputFailureReason,
    message: string,
  ) {
    super(message);
  }
}

/** A retriable transfer failure (interrupted, 5xx, unreachable, range not honoured). */
class TransferError extends Error {}

const code = (err: unknown) => (err as NodeJS.ErrnoException | null)?.code;
const statusOf = (err: unknown) => {
  const s = (err as { status?: unknown } | null)?.status;
  return typeof s === "number" ? s : null;
};
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const myUid = () => (typeof process.getuid === "function" ? process.getuid() : null);

async function lstatOrNull(p: string) {
  try {
    return await lstat(p);
  } catch (err) {
    if (code(err) === "ENOENT") return null;
    throw err;
  }
}

/** A private real directory under `parentReal`: created 0700, never a link, never shared. */
async function privateDir(dir: string, parentReal: string, what: string): Promise<string> {
  try {
    await mkdir(dir, { mode: 0o700 });
  } catch (err) {
    if (code(err) !== "EEXIST") throw err;
  }
  const st = await lstat(dir);
  if (st.isSymbolicLink() || !st.isDirectory())
    throw new InputError("env_input_invalid", `the ${what} is not a real directory`);
  const real = await realpath(dir);
  if (real !== path.join(parentReal, path.basename(dir)))
    throw new InputError("env_input_invalid", `the ${what} resolves outside the group directory`);
  const uid = myUid();
  if (uid !== null && st.uid !== uid)
    throw new InputError("env_input_invalid", `the ${what} is not owned by the channel user`);
  if ((st.mode & 0o077) !== 0)
    throw new InputError("env_input_invalid", `the ${what} is not private`);
  return real;
}

/** Size and SHA-256 of a regular, single-link file, read without following a link. */
async function hashFile(file: string): Promise<{ size: number; sha256: string }> {
  const fh = await open(file, fsc.O_RDONLY | fsc.O_NOFOLLOW);
  try {
    const st = await fh.stat();
    if (!st.isFile() || st.nlink !== 1)
      throw new InputError("env_input_invalid", "an input path is not a plain file");
    const hash = createHash("sha256");
    const buf = Buffer.alloc(HASH_CHUNK);
    let size = 0;
    for (;;) {
      const { bytesRead } = await fh.read(buf, 0, buf.length, null);
      if (bytesRead === 0) break;
      hash.update(buf.subarray(0, bytesRead));
      size += bytesRead;
    }
    return { size, sha256: hash.digest("hex") };
  } finally {
    await fh.close();
  }
}

/** Whatever is at `file` must be absent or a plain single-link file we own. */
async function checkPlainFile(file: string): Promise<boolean> {
  const st = await lstatOrNull(file);
  if (!st) return false;
  const uid = myUid();
  if (st.isSymbolicLink() || !st.isFile() || st.nlink !== 1 || (uid !== null && st.uid !== uid))
    throw new InputError("env_input_invalid", "an input path is a link, a directory or shared");
  return true;
}

/** Write `data` to `file` through a fresh sibling and a rename (never a partial file). */
async function writeAtomic(file: string, data: string, mode: number): Promise<void> {
  const tmp = path.join(path.dirname(file), `.${path.basename(file)}.${process.pid}.tmp`);
  await unlink(tmp).catch(() => undefined);
  const fh = await open(tmp, fsc.O_WRONLY | fsc.O_CREAT | fsc.O_EXCL | fsc.O_NOFOLLOW, 0o600);
  try {
    await fh.writeFile(data);
    await fh.sync();
  } finally {
    await fh.close();
  }
  await chmod(tmp, mode);
  await rename(tmp, file);
}

async function writeAll(fh: FileHandle, chunk: Uint8Array, position: number): Promise<void> {
  let off = 0;
  while (off < chunk.length) {
    const { bytesWritten } = await fh.write(chunk, off, chunk.length - off, position + off);
    off += bytesWritten;
  }
}

/**
 * One transfer into `partial`, resuming from its current size. Resolves when the response
 * ended (complete or short: the caller compares sizes); throws TransferError on a
 * retriable failure and InputError on a final one. The file is opened before the request
 * so the body is read as soon as it arrives: a stream that errors before it is read drops
 * what it buffered, and a resume would start over.
 */
async function transfer(
  source: InputsSource,
  item: TaskInputListing,
  partial: string,
  idleMs: number,
): Promise<void> {
  await checkPlainFile(partial);
  const fh = await open(partial, fsc.O_WRONLY | fsc.O_CREAT | fsc.O_NOFOLLOW, 0o600);
  let drop = false;
  const ctl = new AbortController();
  let idle: NodeJS.Timeout | undefined;
  const arm = () => {
    clearTimeout(idle);
    idle = setTimeout(() => ctl.abort(), idleMs);
  };
  try {
    const st = await fh.stat();
    const uid = myUid();
    if (!st.isFile() || st.nlink !== 1 || (uid !== null && st.uid !== uid))
      throw new InputError("env_input_invalid", "the partial file is a link or shared");
    let offset = st.size;
    if (offset > item.size) {
      await fh.truncate(0);
      offset = 0;
    }
    arm();
    let res: Response;
    try {
      res = await source.content(item.id, offset > 0 ? `bytes=${offset}-` : null, ctl.signal);
    } catch {
      throw new TransferError("Argus is unreachable");
    }
    const reader = res.body?.getReader();
    const discard = () => reader?.cancel().catch(() => undefined);
    if (res.status === 403 || res.status === 404 || res.status === 401) {
      await discard();
      throw new InputError("env_input_unavailable", `the content endpoint answered ${res.status}`);
    }
    if (res.status === 416) {
      await discard();
      drop = true;
      throw new TransferError("the range was not satisfiable");
    }
    if (res.status !== 200 && res.status !== 206) {
      await discard();
      throw new TransferError(`the content endpoint answered ${res.status}`);
    }
    if (res.status === 206) {
      const m = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(res.headers.get("content-range") ?? "");
      if (
        offset === 0 ||
        !m ||
        Number(m[1]) !== offset ||
        Number(m[2]) !== item.size - 1 ||
        Number(m[3]) !== item.size
      ) {
        await discard();
        drop = true;
        throw new TransferError("the range answer did not match the request");
      }
    } else if (offset > 0) {
      // The range was not honoured: the body is the whole file, so start the file again.
      await fh.truncate(0);
      offset = 0;
    }
    let written = offset;
    if (reader) {
      for (;;) {
        let next: Awaited<ReturnType<typeof reader.read>>;
        try {
          next = await reader.read();
        } catch {
          throw new TransferError(
            ctl.signal.aborted ? "the transfer stalled" : "the transfer was interrupted",
          );
        }
        if (next.done) break;
        arm();
        const chunk = next.value;
        if (written + chunk.length > item.size) {
          // Stop at size + 1: never more than one byte past what Argus says it holds.
          await writeAll(fh, chunk.subarray(0, item.size + 1 - written), written);
          await discard();
          drop = true;
          throw new InputError("env_input_invalid", "the content is longer than its size");
        }
        await writeAll(fh, chunk, written);
        written += chunk.length;
      }
    }
    await fh.datasync();
    if (written < item.size) throw new TransferError("the transfer ended early");
  } finally {
    clearTimeout(idle);
    await fh.close();
    if (drop) await unlink(partial).catch(() => undefined);
  }
}

/** Download, verify and publish one input; the final name appears only once verified. */
async function fetchInput(
  source: InputsSource,
  item: TaskInputListing,
  dir: string,
  partialDir: string,
  opts: { backoffMs: number; idleMs: number },
): Promise<void> {
  const partial = path.join(partialDir, `${item.id}.part`);
  const final = path.join(dir, item.name);
  let transfers = 0;
  let mismatches = 0;
  for (;;) {
    try {
      await transfer(source, item, partial, opts.idleMs);
    } catch (err) {
      if (!(err instanceof TransferError)) throw err;
      transfers += 1;
      if (transfers >= MAX_TRANSFERS)
        throw new InputError("env_input_unavailable", `${err.message} after ${transfers} tries`);
      await sleep(opts.backoffMs * 2 ** (transfers - 1));
      continue;
    }
    const got = await hashFile(partial);
    if (got.size === item.size && got.sha256 === item.sha256) break;
    await unlink(partial);
    mismatches += 1;
    if (mismatches >= 2)
      throw new InputError("env_input_invalid", "the downloaded bytes do not match the hash");
  }
  await chmod(partial, 0o400);
  // Same file system (a subdirectory): the rename replaces whatever is there, a link included,
  // without following it.
  await rename(partial, final);
}

function validateItem(raw: unknown, taskId: string): TaskInputListing | string {
  const it = raw as Partial<TaskInputListing> | null;
  if (!it || typeof it !== "object") return "an entry is not an object";
  if (typeof it.id !== "string" || !UUID.test(it.id)) return "an entry has no valid id";
  if (it.task_id !== taskId) return "an entry belongs to another task";
  if (!Number.isSafeInteger(it.size) || (it.size as number) < 0) return "an entry has no size";
  if (typeof it.sha256 !== "string" || !SHA256.test(it.sha256)) return "an entry has no sha256";
  return it as TaskInputListing;
}

async function readManifest(dir: string): Promise<InputManifest | null> {
  try {
    if (!(await checkPlainFile(path.join(dir, INPUT_MANIFEST)))) return null;
    const m = JSON.parse(await readFile(path.join(dir, INPUT_MANIFEST), "utf8")) as InputManifest;
    return Array.isArray(m?.inputs) ? m : null;
  } catch {
    return null;
  }
}

/**
 * Materialize the shared inputs of `taskId` under `<groupDir>/task-inputs/<taskId>/`.
 * A failure of the required input (or of the directory itself when one is required) is
 * the result; an optional input that fails is a `failed` entry and does not block.
 */
export async function materializeTaskInputs(opts: {
  /** The agent group directory, or how to resolve it: asked only when there is an input. */
  groupDir: string | null | (() => Promise<string | null>);
  taskId: string;
  attempt: number;
  source: InputsSource;
  required?: { artifactId: string; sha256: string } | null;
  limits?: InputLimits;
  onReceipt?: (receipt: InputReceipt) => Promise<void>;
  backoffMs?: number;
  idleTimeoutMs?: number;
}): Promise<InputsResult> {
  const { taskId, attempt, source } = opts;
  const required = opts.required ?? null;
  const limits = opts.limits ?? inputLimitsFromEnv();
  const receipt = async (r: InputReceipt) => {
    try {
      await opts.onReceipt?.(r);
    } catch {
      // best effort: a lost receipt is evidence missing, never a reason to stop
    }
  };
  const fail = (reason: InputFailureReason, error: string, artifactId?: string): InputsResult => ({
    ok: false,
    reason,
    error: artifactId ? `input ${artifactId}: ${error}` : error,
    ...(artifactId ? { artifactId } : {}),
  });

  if (!isCanonicalTaskId(taskId)) {
    const why = "the task id is not a canonical UUID";
    if (required) return fail("env_input_invalid", why, required.artifactId);
    return { ok: true, inputs: [], manifest: null, skipped: why };
  }
  let listed: unknown;
  try {
    listed = await source.list(taskId);
  } catch (err) {
    const status = statusOf(err);
    const why = status ? `the inputs list answered ${status}` : "Argus is unreachable";
    if (required) return fail("env_input_unavailable", why, required.artifactId);
    return { ok: true, inputs: [], manifest: null, skipped: why };
  }
  // The API answers `{ task_id, inputs }`; `items` and a bare array are accepted too.
  const body = listed as { inputs?: unknown; items?: unknown } | null;
  const rawItems = Array.isArray(listed) ? listed : (body?.inputs ?? body?.items);
  if (!Array.isArray(rawItems)) {
    if (required)
      return fail("env_input_unavailable", "the inputs list is malformed", required.artifactId);
    return { ok: true, inputs: [], manifest: null, skipped: "the inputs list is malformed" };
  }
  const items: TaskInputListing[] = [];
  for (const raw of rawItems) {
    const it = validateItem(raw, taskId);
    if (typeof it === "string") {
      const id = (raw as { id?: unknown } | null)?.id;
      if (required && id === required.artifactId)
        return fail("env_input_invalid", it, required.artifactId);
      continue; // an entry Argus did not vouch for is not an input
    }
    if (it.worker_readable === false) continue;
    items.push(it);
  }
  if (required) {
    const hit = items.find((i) => i.id === required.artifactId);
    if (!hit)
      return fail(
        "env_input_unavailable",
        "the input is not listed as shared",
        required.artifactId,
      );
    if (hit.sha256 !== required.sha256)
      return fail(
        "env_input_invalid",
        "the reference's hash differs from the artifact's",
        required.artifactId,
      );
  }
  if (items.length === 0) return { ok: true, inputs: [], manifest: null };

  // Required first: when it cannot be had, nothing else is worth downloading.
  items.sort(
    (a, b) => Number(b.id === required?.artifactId) - Number(a.id === required?.artifactId),
  );
  let dir: string;
  let partialDir: string;
  try {
    const groupDir = typeof opts.groupDir === "function" ? await opts.groupDir() : opts.groupDir;
    if (!groupDir) throw new InputError("env_input_invalid", "the group directory is unknown");
    const groupReal = await realpath(groupDir);
    const rootReal = await privateDir(
      path.join(groupReal, TASK_INPUTS_DIR),
      groupReal,
      "task inputs root",
    );
    dir = await privateDir(taskInputsPath(groupReal, taskId), rootReal, "task inputs directory");
    partialDir = await privateDir(path.join(dir, PARTIAL_DIR), dir, "partial directory");
  } catch (err) {
    const reason = err instanceof InputError ? err.reason : "env_input_invalid";
    const msg = err instanceof InputError ? err.message : "the task inputs directory is unusable";
    for (const it of items) await receipt({ artifact_id: it.id, state: "failed", reason });
    if (required) return fail(reason, msg, required.artifactId);
    return {
      ok: true,
      inputs: items.map((it) => failedInput(it, attempt, false, reason)),
      manifest: null,
    };
  }
  const previous = await readManifest(dir);
  await unlink(path.join(dir, INPUT_CONSUMED)).catch(() => undefined);
  await writeAtomic(path.join(dir, INPUT_VERIFIER), VERIFY_INPUTS_SOURCE, 0o400);

  const total = items.reduce((n, i) => n + i.size, 0);
  const taskLimit =
    items.length > limits.maxFiles
      ? `${items.length} inputs exceed ARGUS_TASK_INPUT_MAX_FILES (${limits.maxFiles})`
      : total > limits.maxTotalBytes
        ? `${total} bytes exceed ARGUS_TASK_INPUT_MAX_TOTAL_BYTES (${limits.maxTotalBytes})`
        : null;
  const folded = new Map<string, number>();
  for (const it of items)
    if (typeof it.name === "string")
      folded.set(it.name.toLowerCase(), (folded.get(it.name.toLowerCase()) ?? 0) + 1);

  const inputs: MaterializedInput[] = [];
  const manifest: InputManifest = { version: 1, task_id: taskId, attempt, inputs: [] };
  const publish = async () => {
    manifest.inputs = inputs.map(({ path: _p, containerPath: _c, reused: _r, ...e }) => e);
    await writeAtomic(
      path.join(dir, INPUT_MANIFEST),
      `${JSON.stringify(manifest, null, 2)}\n`,
      0o400,
    );
  };
  const containerDir = taskInputsContainerPath(taskId);
  for (const it of items) {
    const isRequired = it.id === required?.artifactId;
    try {
      const unsafe = unsafeInputName(it.name);
      if (unsafe) throw new InputError("env_input_invalid", unsafe);
      if ((folded.get(it.name.toLowerCase()) ?? 0) > 1)
        throw new InputError("env_input_invalid", "the name is a duplicate after case folding");
      if (taskLimit) throw new InputError("env_input_limit", taskLimit);
      if (it.size > limits.maxBytes)
        throw new InputError(
          "env_input_limit",
          `${it.size} bytes exceed ARGUS_TASK_INPUT_MAX_BYTES (${limits.maxBytes})`,
        );
      const final = path.join(dir, it.name);
      let reused = false;
      if (await checkPlainFile(final)) {
        const have = await hashFile(final);
        if (have.size === it.size && have.sha256 === it.sha256) {
          await chmod(final, 0o400);
          reused = true;
        } else {
          await unlink(final);
        }
      }
      if (!reused) {
        await fetchInput(source, it, dir, partialDir, {
          backoffMs: opts.backoffMs ?? BACKOFF_MS,
          idleMs: opts.idleTimeoutMs ?? IDLE_TIMEOUT_MS,
        });
        await receipt({ artifact_id: it.id, state: "materialized" });
      }
      // Re-read what is under the final name now: that is what the model will see.
      const check = await hashFile(final);
      if (check.size !== it.size || check.sha256 !== it.sha256) {
        await unlink(final).catch(() => undefined);
        throw new InputError("env_input_invalid", "the published file does not match the hash");
      }
      inputs.push({
        artifact_id: it.id,
        name: it.name,
        size: it.size,
        sha256: it.sha256,
        state: "verified",
        attempt,
        required: isRequired,
        path: final,
        containerPath: path.posix.join(containerDir, it.name),
        reused,
      });
      await publish();
      await receipt({ artifact_id: it.id, state: "verified", sha256: it.sha256, size: it.size });
    } catch (err) {
      const reason: InputFailureReason =
        err instanceof InputError ? err.reason : "env_input_invalid";
      const msg = err instanceof InputError ? err.message : "the input could not be written";
      await receipt({ artifact_id: it.id, state: "failed", reason });
      inputs.push(failedInput(it, attempt, isRequired, reason));
      await publish().catch(() => undefined);
      if (isRequired) return fail(reason, msg, it.id);
    }
  }
  // A file an earlier run published and this list no longer vouches for is not left behind.
  const kept = new Set(inputs.filter((i) => i.state === "verified").map((i) => i.name));
  for (const old of previous?.inputs ?? []) {
    if (!old.name || kept.has(old.name) || unsafeInputName(old.name)) continue;
    if (await checkPlainFile(path.join(dir, old.name)).catch(() => false))
      await unlink(path.join(dir, old.name)).catch(() => undefined);
  }
  await publish();
  return { ok: true, inputs, manifest, dir };
}

function failedInput(
  it: TaskInputListing,
  attempt: number,
  required: boolean,
  reason: InputFailureReason,
): MaterializedInput {
  return {
    artifact_id: it.id,
    name: unsafeInputName(it.name) ? null : it.name,
    size: it.size,
    sha256: it.sha256,
    state: "failed",
    attempt,
    required,
    reason,
    path: null,
    containerPath: null,
    reused: false,
  };
}

/**
 * The prompt block (spec 3.5): trusted lines from verified data only. No content, no
 * rejected name, no Argus URL, no token. Empty when the task has no shared inputs.
 */
export function taskInputLines(taskId: string, inputs: readonly MaterializedInput[]): string[] {
  if (inputs.length === 0) return [];
  const dir = taskInputsContainerPath(taskId);
  const lines = [
    "Task inputs (downloaded from Argus by the channel and verified by size and SHA-256; trusted metadata):",
  ];
  for (const i of inputs) {
    if (i.state === "verified")
      lines.push(
        `- ${i.containerPath} · ${i.size} bytes · sha256 ${i.sha256}${i.required ? " · required" : ""}`,
      );
    else
      lines.push(
        `- input ${i.artifact_id} could not be materialized (${i.reason}); it is not available to this attempt.`,
      );
  }
  if (inputs.some((i) => i.state === "verified")) {
    lines.push(
      `Verifier: \`node ${path.posix.join(dir, INPUT_VERIFIER)}\` re-hashes every file against ${path.posix.join(dir, INPUT_MANIFEST)}.`,
      "Read these files in place. Do not re-download them or fetch them from anywhere else.",
      "Run the verifier before applying anything from them (for example before `git apply --check` against the exact base commit).",
      "If the verifier prints FAIL or a file does not match, stop and report the mismatch with ASSIST.",
    );
  }
  return lines;
}

/**
 * The `consumed` receipts of a run of the verifier in `dir` (spec 3.4): only entries the
 * manifest of the same attempt lists as verified, with its size and hash.
 */
export async function consumedReceipts(dir: string): Promise<InputReceipt[]> {
  const manifest = await readManifest(dir);
  if (!manifest) return [];
  const file = path.join(dir, INPUT_CONSUMED);
  let consumed: { task_id?: unknown; attempt?: unknown; inputs?: unknown };
  try {
    if (!(await checkPlainFile(file))) return [];
    if ((await lstat(file)).size > CONSUMED_MAX_BYTES) return [];
    consumed = JSON.parse(await readFile(file, "utf8"));
  } catch {
    return [];
  }
  if (consumed?.task_id !== manifest.task_id || consumed.attempt !== manifest.attempt) return [];
  if (!Array.isArray(consumed.inputs)) return [];
  const out: InputReceipt[] = [];
  for (const c of consumed.inputs as Array<Record<string, unknown>>) {
    const m = manifest.inputs.find(
      (e) =>
        e.state === "verified" &&
        e.artifact_id === c?.artifact_id &&
        e.sha256 === c?.sha256 &&
        e.size === c?.size,
    );
    if (m && !out.some((r) => r.artifact_id === m.artifact_id))
      out.push({ artifact_id: m.artifact_id, state: "consumed", sha256: m.sha256, size: m.size });
  }
  return out;
}

/** Plain Node, `node:fs` and `node:crypto` only: what the session runs as `node verify-inputs.mjs`. */
export const VERIFY_INPUTS_SOURCE = `// Written by the Argus channel (task inputs spec 3.4). Re-hashes every input against
// manifest.json in this directory; prints "name size sha256 ok|FAIL" per input and exits
// non-zero on any failure. On success it writes consumed.json for the channel to report.
import { createHash } from "node:crypto";
import fs from "node:fs";

const here = new URL("./", import.meta.url);
const NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/;

function hash(name) {
  const fd = fs.openSync(new URL(name, here), fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  try {
    const st = fs.fstatSync(fd);
    if (!st.isFile()) throw new Error("not a file");
    const h = createHash("sha256");
    const buf = Buffer.alloc(65536);
    let size = 0;
    for (;;) {
      const n = fs.readSync(fd, buf, 0, buf.length, null);
      if (n === 0) break;
      h.update(buf.subarray(0, n));
      size += n;
    }
    return { size, sha256: h.digest("hex") };
  } finally {
    fs.closeSync(fd);
  }
}

let manifest;
try {
  manifest = JSON.parse(fs.readFileSync(new URL("manifest.json", here), "utf8"));
} catch {
  console.log("manifest.json - - FAIL");
  process.exit(1);
}
const verified = (manifest.inputs ?? []).filter((e) => e.state === "verified");
let failed = verified.length === 0;
const consumed = [];
for (const e of verified) {
  let got = null;
  try {
    if (NAME.test(e.name)) got = hash(e.name);
  } catch {}
  const ok = got !== null && got.size === e.size && got.sha256 === e.sha256;
  console.log(\`\${e.name} \${e.size} \${e.sha256} \${ok ? "ok" : "FAIL"}\`);
  if (ok) consumed.push({ artifact_id: e.artifact_id, name: e.name, size: e.size, sha256: e.sha256 });
  else failed = true;
}
if (failed) process.exit(1);
const out = new URL("consumed.json", here);
const tmp = new URL(\`.consumed.json.\${process.pid}.tmp\`, here);
fs.writeFileSync(
  tmp,
  JSON.stringify({ task_id: manifest.task_id, attempt: manifest.attempt, inputs: consumed }) + "\\n",
  { flag: "wx", mode: 0o600 },
);
fs.renameSync(tmp, out);
`;
