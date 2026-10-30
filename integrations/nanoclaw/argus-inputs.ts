/**
 * Task inputs (task inputs spec 3.4-3.7): the attachments an orchestrator shared with a
 * task's workers, downloaded by the channel (the host process, which holds the worker
 * token) into a task-local directory the session container mounts, verified by size and
 * SHA-256 before the model is told about them. The model reads files, never the API.
 *
 * Content never reaches a log line, an error, a receipt or the prompt: only artifact
 * ids, sizes, hashes and reason codes do.
 */
import { createHash, randomUUID } from "node:crypto";
import { constants as fsc } from "node:fs";
import {
  type FileHandle,
  lstat,
  mkdir,
  open,
  realpath,
  rename,
  stat,
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
/** The manifest and consumed.json are a few hundred bytes per input; more is not ours. */
const JSON_MAX_BYTES = 1024 * 1024;
const DEFAULT_MAX_BYTES = 100 * 1024 * 1024;
const DEFAULT_MAX_TOTAL_BYTES = 256 * 1024 * 1024;
const DEFAULT_MAX_FILES = 20;

export type InputFailureReason = "env_input_unavailable" | "env_input_invalid" | "env_input_limit";
/** The receipt reason codes the API accepts (artifacts/inputs.py `InputFailure`). */
export type ReceiptFailure = "unavailable" | "invalid" | "limit" | "corrupt";

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
  reason?: ReceiptFailure;
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
    /** Bytes arrived but are not the artifact's (longer, or another hash). */
    readonly corrupt = false,
  ) {
    super(message);
  }
}

function receiptReason(err: InputError): ReceiptFailure {
  if (err.corrupt) return "corrupt";
  if (err.reason === "env_input_unavailable") return "unavailable";
  return err.reason === "env_input_limit" ? "limit" : "invalid";
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

/**
 * A directory held open. Every operation below names `/proc/self/fd/<fd>/<leaf>`, so the
 * kernel resolves only the leaf, inside the directory that was checked: an ancestor the
 * session renames or swaps for a link afterwards cannot redirect a write, an unlink or a
 * rename. Leaves are always validated names (no separator) and never followed.
 */
interface Dir {
  fh: FileHandle;
  dev: number;
  ino: number;
}

const at = (d: Dir, leaf: string) => `/proc/self/fd/${d.fh.fd}/${leaf}`;

/** Open `p` as a directory without following a final link; refuse if it cannot be pinned. */
async function holdDir(p: string, what: string): Promise<Dir & { uid: number; mode: number }> {
  let fh: FileHandle;
  try {
    fh = await open(p, fsc.O_RDONLY | fsc.O_DIRECTORY | fsc.O_NOFOLLOW);
  } catch (err) {
    if (code(err) === "ELOOP" || code(err) === "ENOTDIR")
      throw new InputError("env_input_invalid", `the ${what} is not a real directory`);
    throw err;
  }
  try {
    const st = await fh.stat();
    // The handle path must name this very directory (Linux procfs); elsewhere it cannot be held.
    const pinned = await stat(`/proc/self/fd/${fh.fd}`).catch(() => null);
    if (!pinned || pinned.dev !== st.dev || pinned.ino !== st.ino)
      throw new InputError("env_input_invalid", `the ${what} cannot be held open on this host`);
    return { fh, dev: st.dev, ino: st.ino, uid: st.uid, mode: st.mode };
  } catch (err) {
    await fh.close();
    throw err;
  }
}

/** A private real directory `name` in `parent`: created 0700, never a link, never shared. */
async function privateDir(parent: Dir, name: string, what: string, create = true): Promise<Dir> {
  if (create)
    try {
      await mkdir(at(parent, name), { mode: 0o700 });
    } catch (err) {
      if (code(err) !== "EEXIST") throw err;
    }
  const d = await holdDir(at(parent, name), what);
  const uid = myUid();
  const why =
    uid !== null && d.uid !== uid
      ? `the ${what} is not owned by the channel user`
      : (d.mode & 0o077) !== 0
        ? `the ${what} is not private`
        : null;
  if (why) {
    await d.fh.close();
    throw new InputError("env_input_invalid", why);
  }
  return d;
}

/** The held group, root and task directories of `taskId`; null when one is absent. */
async function holdTaskDirs(
  groupReal: string,
  taskId: string,
  create: boolean,
): Promise<Dir[] | null> {
  const held: Dir[] = [];
  try {
    held.push(await holdDir(groupReal, "group directory"));
    held.push(await privateDir(held[0], TASK_INPUTS_DIR, "task inputs root", create));
    held.push(await privateDir(held[1], taskId, "task inputs directory", create));
    if (create) held.push(await privateDir(held[2], PARTIAL_DIR, "partial directory"));
    return held;
  } catch (err) {
    for (const d of held) await d.fh.close();
    if (!create && code(err) === "ENOENT") return null;
    throw err;
  }
}

/** Whether the path of the task directory still leads, link-free, to the held one. */
async function stillNamed(groupReal: string, taskId: string, root: Dir, dir: Dir) {
  const r = await lstatOrNull(path.join(groupReal, TASK_INPUTS_DIR));
  const d = await lstatOrNull(taskInputsPath(groupReal, taskId));
  return (
    !!r?.isDirectory() &&
    r.dev === root.dev &&
    r.ino === root.ino &&
    !!d?.isDirectory() &&
    d.dev === dir.dev &&
    d.ino === dir.ino
  );
}

const plain = (st: { isFile(): boolean; nlink: number; uid: number }) => {
  const uid = myUid();
  return st.isFile() && st.nlink === 1 && (uid === null || st.uid === uid);
};

/**
 * Whether `leaf` is a regular, single-link file with this size and SHA-256, read without
 * following a link (nor blocking on a FIFO); when it is and `mode` is given, set it.
 */
async function matches(
  d: Dir,
  leaf: string,
  want: { size: number; sha256: string },
  mode?: number,
): Promise<boolean> {
  const fh = await open(at(d, leaf), fsc.O_RDONLY | fsc.O_NOFOLLOW | fsc.O_NONBLOCK);
  try {
    if (!plain(await fh.stat()))
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
    const ok = size === want.size && hash.digest("hex") === want.sha256;
    if (ok && mode !== undefined) await fh.chmod(mode);
    return ok;
  } finally {
    await fh.close();
  }
}

/** Whatever is at `leaf` must be absent or a plain single-link file we own. */
async function checkPlainFile(d: Dir, leaf: string): Promise<boolean> {
  const st = await lstatOrNull(at(d, leaf));
  if (!st) return false;
  if (!plain(st))
    throw new InputError("env_input_invalid", "an input path is a link, a directory or shared");
  return true;
}

/** A small JSON file the channel or the verifier wrote, read with a bound; null otherwise. */
async function readJson(d: Dir, leaf: string): Promise<unknown> {
  let fh: FileHandle;
  try {
    fh = await open(at(d, leaf), fsc.O_RDONLY | fsc.O_NOFOLLOW | fsc.O_NONBLOCK);
  } catch {
    return null;
  }
  try {
    const st = await fh.stat();
    if (!plain(st) || st.size > JSON_MAX_BYTES) return null;
    const buf = Buffer.alloc(JSON_MAX_BYTES + 1);
    let n = 0;
    while (n < buf.length) {
      const { bytesRead } = await fh.read(buf, n, buf.length - n, n);
      if (bytesRead === 0) break;
      n += bytesRead;
    }
    return n > JSON_MAX_BYTES ? null : JSON.parse(buf.subarray(0, n).toString("utf8"));
  } catch {
    return null;
  } finally {
    await fh.close();
  }
}

/** Write `data` to `leaf` through a fresh, unguessable sibling and a rename. */
async function writeAtomic(d: Dir, leaf: string, data: string, mode: number): Promise<void> {
  const tmp = `.${leaf}.${randomUUID()}.tmp`;
  const fh = await open(
    at(d, tmp),
    fsc.O_WRONLY | fsc.O_CREAT | fsc.O_EXCL | fsc.O_NOFOLLOW,
    0o600,
  );
  try {
    await fh.writeFile(data);
    await fh.chmod(mode);
    await fh.sync();
  } finally {
    await fh.close();
  }
  await rename(at(d, tmp), at(d, leaf));
}

async function writeAll(fh: FileHandle, chunk: Uint8Array, position: number): Promise<void> {
  let off = 0;
  while (off < chunk.length) {
    const { bytesWritten } = await fh.write(chunk, off, chunk.length - off, position + off);
    off += bytesWritten;
  }
}

/**
 * One transfer into `<partialDir>/<leaf>`, resuming from its current size. Resolves once
 * the file holds `item.size` bytes (a partial already that long is left to the caller's
 * hash, without a request: a range from its end would only be refused); throws
 * TransferError on a retriable failure (a short body included) and InputError on a final
 * one. The file is opened before the request so the body is read as soon as it arrives: a
 * stream that errors before it is read drops what it buffered, and a resume would start over.
 */
async function transfer(
  source: InputsSource,
  item: TaskInputListing,
  partialDir: Dir,
  leaf: string,
  idleMs: number,
): Promise<void> {
  const partial = at(partialDir, leaf);
  const fh = await open(
    partial,
    fsc.O_WRONLY | fsc.O_CREAT | fsc.O_NOFOLLOW | fsc.O_NONBLOCK,
    0o600,
  );
  let drop = false;
  const ctl = new AbortController();
  let idle: NodeJS.Timeout | undefined;
  const arm = () => {
    clearTimeout(idle);
    idle = setTimeout(() => ctl.abort(), idleMs);
  };
  try {
    const st = await fh.stat();
    if (!plain(st))
      throw new InputError("env_input_invalid", "the partial file is a link or shared");
    let offset = st.size;
    if (offset > item.size) {
      await fh.truncate(0);
      offset = 0;
    }
    if (offset > 0 && offset === item.size) return;
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
          throw new InputError("env_input_invalid", "the content is longer than its size", true);
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

/**
 * Clear `leaf` in `d` of anything but a plain file of ours, without following it: a link
 * or a hard link loses only its name here (its target is never opened), a directory is
 * renamed aside into `aside`. True when a plain file remains.
 */
async function displace(d: Dir, leaf: string, aside: Dir): Promise<boolean> {
  const st = await lstatOrNull(at(d, leaf));
  if (!st) return false;
  if (plain(st)) return true;
  if (st.isDirectory()) await rename(at(d, leaf), at(aside, `${leaf}.displaced.${randomUUID()}`));
  else await unlink(at(d, leaf));
  return false;
}

/**
 * Download, verify and publish one input; the final name appears only once verified.
 * At most MAX_TRANSFERS failed transfers, BACKOFF_MS then twice that apart; bytes that
 * do not match the hash restart once from byte 0, a second mismatch is final.
 */
async function fetchInput(
  source: InputsSource,
  item: TaskInputListing,
  dir: Dir,
  partialDir: Dir,
  opts: { backoffMs: number; idleMs: number },
): Promise<void> {
  const leaf = `${item.id}.part`;
  let transfers = 0;
  let mismatches = 0;
  for (;;) {
    await displace(partialDir, leaf, partialDir);
    try {
      await transfer(source, item, partialDir, leaf, opts.idleMs);
    } catch (err) {
      if (!(err instanceof TransferError)) throw err;
      transfers += 1;
      if (transfers >= MAX_TRANSFERS)
        throw new InputError("env_input_unavailable", `${err.message} after ${transfers} tries`);
      await sleep(opts.backoffMs * 2 ** (transfers - 1));
      continue;
    }
    if (await matches(partialDir, leaf, item, 0o400)) break;
    await unlink(at(partialDir, leaf));
    mismatches += 1;
    if (mismatches >= 2)
      throw new InputError("env_input_invalid", "the downloaded bytes do not match the hash", true);
  }
  // Both ends are held directories on one file system; a planted entry was cleared first.
  await displace(dir, item.name, partialDir);
  await rename(at(partialDir, leaf), at(dir, item.name));
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

async function readManifest(dir: Dir): Promise<InputManifest | null> {
  const m = (await readJson(dir, INPUT_MANIFEST)) as InputManifest | null;
  return Array.isArray(m?.inputs) ? m : null;
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
  /** Test seam: runs once the directories are held, before the first write. */
  afterDirsChecked?: () => Promise<void>;
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
  const failed = (artifactId: string, err: InputError) =>
    receipt({ artifact_id: artifactId, state: "failed", reason: receiptReason(err) });
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
  const unusable = async (err: unknown): Promise<InputsResult> => {
    const e =
      err instanceof InputError
        ? err
        : new InputError("env_input_invalid", "the task inputs directory is unusable");
    for (const it of items) await failed(it.id, e);
    if (required) return fail(e.reason, e.message, required.artifactId);
    return {
      ok: true,
      inputs: items.map((it) => failedInput(it, attempt, false, e.reason)),
      manifest: null,
    };
  };
  let groupReal: string;
  let held: Dir[];
  try {
    const groupDir = typeof opts.groupDir === "function" ? await opts.groupDir() : opts.groupDir;
    if (!groupDir) throw new InputError("env_input_invalid", "the group directory is unknown");
    groupReal = await realpath(groupDir);
    held = (await holdTaskDirs(groupReal, taskId, true)) as Dir[];
  } catch (err) {
    return unusable(err);
  }
  try {
    const [, root, dir, partialDir] = held;
    await opts.afterDirsChecked?.();
    let result: InputsResult;
    try {
      result = await materializeIn(dir, partialDir);
    } catch (err) {
      return unusable(err);
    }
    // What the session mounts is the path: it must still name the directory written to.
    if (!(await stillNamed(groupReal, taskId, root, dir)))
      return unusable(
        new InputError("env_input_invalid", "the task inputs directory was replaced"),
      );
    return result;
  } finally {
    for (const d of held) await d.fh.close();
  }

  async function materializeIn(dir: Dir, partialDir: Dir): Promise<InputsResult> {
    const dirPath = taskInputsPath(groupReal, taskId);
    const previous = await readManifest(dir);
    await unlink(at(dir, INPUT_CONSUMED)).catch(() => undefined);
    await writeAtomic(dir, INPUT_VERIFIER, VERIFY_INPUTS_SOURCE, 0o400);

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
      await writeAtomic(dir, INPUT_MANIFEST, `${JSON.stringify(manifest, null, 2)}\n`, 0o400);
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
        let reused = false;
        if (await displace(dir, it.name, partialDir)) {
          if (await matches(dir, it.name, it, 0o400)) reused = true;
          else await unlink(at(dir, it.name));
        }
        if (!reused) {
          await fetchInput(source, it, dir, partialDir, {
            backoffMs: opts.backoffMs ?? BACKOFF_MS,
            idleMs: opts.idleTimeoutMs ?? IDLE_TIMEOUT_MS,
          });
          await receipt({
            artifact_id: it.id,
            state: "materialized",
            sha256: it.sha256,
            size: it.size,
          });
        }
        // Re-read what is under the final name now: that is what the model will see.
        if (!(await matches(dir, it.name, it))) {
          await unlink(at(dir, it.name)).catch(() => undefined);
          throw new InputError(
            "env_input_invalid",
            "the published file does not match the hash",
            true,
          );
        }
        inputs.push({
          artifact_id: it.id,
          name: it.name,
          size: it.size,
          sha256: it.sha256,
          state: "verified",
          attempt,
          required: isRequired,
          path: path.join(dirPath, it.name),
          containerPath: path.posix.join(containerDir, it.name),
          reused,
        });
        await publish();
        await receipt({ artifact_id: it.id, state: "verified", sha256: it.sha256, size: it.size });
      } catch (err) {
        const e =
          err instanceof InputError
            ? err
            : new InputError("env_input_invalid", "the input could not be written");
        await failed(it.id, e);
        inputs.push(failedInput(it, attempt, isRequired, e.reason));
        await publish().catch(() => undefined);
        if (isRequired) return fail(e.reason, e.message, it.id);
      }
    }
    // A file an earlier run published and this list no longer vouches for is not left behind.
    const kept = new Set(inputs.filter((i) => i.state === "verified").map((i) => i.name));
    for (const old of previous?.inputs ?? []) {
      if (!old.name || kept.has(old.name) || unsafeInputName(old.name)) continue;
      if (await checkPlainFile(dir, old.name).catch(() => false))
        await unlink(at(dir, old.name)).catch(() => undefined);
    }
    await publish();
    return { ok: true, inputs, manifest, dir: dirPath };
  }
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
 * manifest of the same attempt lists as verified, with its size and hash. Both files are
 * read through the held directory chain, bounded, and never through a link.
 */
export async function consumedReceipts(dir: string): Promise<InputReceipt[]> {
  const taskId = path.basename(dir);
  const rootPath = path.dirname(dir);
  if (!isCanonicalTaskId(taskId) || path.basename(rootPath) !== TASK_INPUTS_DIR) return [];
  let held: Dir[] | null;
  try {
    held = await holdTaskDirs(await realpath(path.dirname(rootPath)), taskId, false);
  } catch {
    return [];
  }
  if (!held) return [];
  let manifest: InputManifest | null;
  let consumed: { task_id?: unknown; attempt?: unknown; inputs?: unknown } | null;
  try {
    manifest = await readManifest(held[2]);
    consumed = (await readJson(held[2], INPUT_CONSUMED)) as typeof consumed;
  } finally {
    for (const d of held) await d.fh.close();
  }
  if (!manifest || !consumed) return [];
  if (consumed.task_id !== manifest.task_id || consumed.attempt !== manifest.attempt) return [];
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
