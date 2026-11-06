/**
 * Host side of `argus-task attach` (docs/specs/task-helpers.spec.md 3.10).
 *
 * The worker holds no Argus credential. `argus-task attach` (in the session container)
 * copies the file into a spool directory in the agent group's directory, appends a request
 * line to a file there, and waits for a receipt. This module reads those requests for the
 * claimed task and attempt, hands the spooled bytes to `store` (the channel's upload with the
 * host's token), and appends a durable receipt. The host never opens a path the worker names,
 * only `<spool>/<nonce>`, and never trusts the worker's size or hash: it hashes what it read.
 *
 * Every file here is untrusted input in a directory the container can write: plain files
 * only, no links, and the receipt is rebuilt field by field from host-held values.
 */
import { createHash } from "node:crypto";
import { constants as fsc } from "node:fs";
import { chmod, lstat, mkdir, open, rename, rm, unlink } from "node:fs/promises";
import path from "node:path";

/** Bridge files in the group directory; keep in step with argus-task.mjs. */
export const ATTACH_REQUESTS_FILE = "argus-task-attach.jsonl";
export const ATTACH_RECEIPTS_FILE = "argus-task-attach-receipts.jsonl";
export const ATTACH_STATUS_FILE = "argus-task-attach-status.json";
export const ATTACH_SPOOL_DIR = "argus-task-attach-spool";
export const ATTACH_PROTOCOL = 1;
export const ATTACH_CAPABILITY = "artifact-attach";
/** Below the API's artifact_max_bytes (100 MiB): the host holds the bytes in memory to upload. */
export const ATTACH_MAX_BYTES = 25 * 1024 * 1024;
/** The roles the API accepts (ArtifactRole in artifacts/service.py). */
export const ATTACH_ROLES = ["report", "delegate_report", "policy_snapshot", "qa_receipt", "other"];
const MAX_REQUESTS = 50; // per attempt
const READ_MAX = 256 * 1024; // of the request file per poll
const LINE_MAX = 4096;
const HEX32 = /^[0-9a-f]{32}$/;
const HEX64 = /^[0-9a-f]{64}$/;

export interface AttachTask {
  id: string;
  attempt: number;
}

/** What the channel recorded for a request; `error` leaves nothing recorded. */
export type AttachStored =
  | {
      ok: true;
      artifact_id: string;
      name: string;
      role: string | null;
      bytes: number;
      sha256: string;
      duplicate: boolean;
      renamed: boolean;
    }
  | { ok: false; code: string; message: string; retryable: boolean };

export interface AttachRequest {
  request_id: string;
  nonce: string;
  name: string;
  role: string | null;
  bytes: number;
  sha256: string;
}

type Refusal = { code: string; message: string; retryable: boolean };
const refuse = (code: string, message: string, retryable = false): Refusal => ({
  code,
  message,
  retryable,
});

const isObject = (x: unknown): x is Record<string, unknown> =>
  x !== null && typeof x === "object" && !Array.isArray(x);

/**
 * The request a line carries, or the reason it cannot be served. `ids` is whatever of
 * `request_id` / `nonce` is well formed, so a refusal can still be correlated.
 */
export function parseAttachRequest(
  raw: string,
  task: AttachTask,
  maxBytes = ATTACH_MAX_BYTES,
): { request: AttachRequest } | { ids: Partial<AttachRequest>; refusal: Refusal } | null {
  let rec: unknown;
  try {
    rec = JSON.parse(raw);
  } catch {
    return null; // torn or foreign
  }
  if (!isObject(rec) || rec.type !== "attach") return null;
  const ids: Partial<AttachRequest> = {};
  if (typeof rec.request_id === "string" && HEX32.test(rec.request_id))
    ids.request_id = rec.request_id;
  if (typeof rec.nonce === "string" && HEX32.test(rec.nonce)) ids.nonce = rec.nonce;
  if (!ids.request_id || !ids.nonce) return null; // nothing to correlate a receipt with
  const bad = (code: string, message: string, retryable = false) => ({
    ids,
    refusal: refuse(code, message, retryable),
  });
  if (rec.v !== ATTACH_PROTOCOL) return bad("unsupported_protocol", "unknown attach protocol");
  if (rec.task_id !== task.id)
    return bad("wrong_task", "the request names a task this channel is not running");
  if (rec.attempt !== task.attempt)
    return bad("stale_attempt", "the request is for an attempt that is not the active one");
  if (typeof rec.name !== "string" || !rec.name || rec.name.length > 255)
    return bad("invalid_request", "name must be a string of 1 to 255 characters");
  const role = rec.role ?? null;
  if (role !== null && (typeof role !== "string" || !ATTACH_ROLES.includes(role)))
    return bad("invalid_request", `role must be one of ${ATTACH_ROLES.join(", ")}`);
  if (!Number.isSafeInteger(rec.bytes) || (rec.bytes as number) < 1)
    return bad(
      "invalid_request",
      "bytes must be a positive integer (an empty file is not attached)",
    );
  if ((rec.bytes as number) > maxBytes)
    return bad("too_large", `the file is larger than the ${maxBytes} byte limit`);
  if (typeof rec.sha256 !== "string" || !HEX64.test(rec.sha256))
    return bad("invalid_request", "sha256 must be 64 lowercase hex characters");
  return {
    request: {
      request_id: ids.request_id,
      nonce: ids.nonce,
      name: rec.name,
      role,
      bytes: rec.bytes as number,
      sha256: rec.sha256,
    },
  };
}

async function assertRealDir(dir: string): Promise<void> {
  if (!(await lstat(dir)).isDirectory())
    throw new Error("attach directory is not a plain directory");
}

/** Create the spool directory empty (anything the last attempt left is removed). */
export async function resetAttachSpool(dir: string): Promise<string> {
  await assertRealDir(dir);
  const spool = path.join(dir, ATTACH_SPOOL_DIR);
  await rm(spool, { recursive: true, force: true }); // removes a link itself, never its target
  await mkdir(spool, { mode: 0o777 });
  await chmod(spool, 0o777); // the container user writes here
  return spool;
}

/** The spooled bytes for `nonce`, or why they cannot be used. Reads only a plain file. */
async function readSpooled(
  spool: string,
  req: AttachRequest,
): Promise<{ data: Buffer } | { refusal: Refusal }> {
  const file = path.join(spool, req.nonce);
  let handle: Awaited<ReturnType<typeof open>>;
  try {
    handle = await open(file, fsc.O_RDONLY | fsc.O_NOFOLLOW | fsc.O_NONBLOCK);
  } catch (err) {
    const code = (err as NodeJS.ErrnoException)?.code;
    if (code === "ENOENT")
      return { refusal: refuse("spool_missing", "the spooled copy of the file is missing") };
    return { refusal: refuse("unsafe_spool", "the spooled copy is not a plain file") };
  }
  try {
    const st = await handle.stat();
    if (!st.isFile() || st.nlink !== 1)
      return { refusal: refuse("unsafe_spool", "the spooled copy is not a plain file") };
    if (st.size !== req.bytes)
      return {
        refusal: refuse("integrity_mismatch", "the spooled size differs from the request"),
      };
    const data = Buffer.alloc(st.size);
    const { bytesRead } = await handle.read(data, 0, data.length, 0);
    if (bytesRead !== data.length)
      return { refusal: refuse("integrity_mismatch", "the spooled copy was cut short") };
    if (createHash("sha256").update(data).digest("hex") !== req.sha256)
      return { refusal: refuse("integrity_mismatch", "the spooled bytes do not match sha256") };
    return { data };
  } finally {
    await handle.close();
  }
}

/**
 * Serves attach requests for the claimed task and attempt. `store` records the artifact
 * (and maps the API's errors to a refusal); `active` says whether the claim is still the
 * one in flight, so a late request never reaches the next one.
 */
export function createAttachRelay(opts: {
  dir: string;
  task: AttachTask;
  active: () => boolean;
  store: (req: AttachRequest & { name: string; data: Buffer }) => Promise<AttachStored>;
  maxBytes?: number;
  now?: () => number;
  /** Where to say what was refused (the channel passes its logger); never gets file bytes. */
  warn?: (message: string, fields: Record<string, unknown>) => void;
}) {
  const maxBytes = opts.maxBytes ?? ATTACH_MAX_BYTES;
  const now = opts.now ?? Date.now;
  const warn = opts.warn ?? (() => {});
  const requestsFile = path.join(opts.dir, ATTACH_REQUESTS_FILE);
  const receiptsFile = path.join(opts.dir, ATTACH_RECEIPTS_FILE);
  const statusFile = path.join(opts.dir, ATTACH_STATUS_FILE);
  const spool = path.join(opts.dir, ATTACH_SPOOL_DIR);
  let offset = 0;
  let served = 0;
  let stopped = false;
  let busy = false;

  async function writeReceipt(
    ids: Partial<AttachRequest>,
    body:
      | { ok: true; stored: Extract<AttachStored, { ok: true }> }
      | { ok: false; refusal: Refusal },
  ): Promise<void> {
    const base = {
      v: ATTACH_PROTOCOL,
      type: "receipt",
      request_id: ids.request_id,
      nonce: ids.nonce,
      task_id: opts.task.id,
      attempt: opts.task.attempt,
      at: new Date(now()).toISOString(),
    };
    const rec = body.ok
      ? {
          ...base,
          ok: true,
          artifact_id: body.stored.artifact_id,
          name: body.stored.name,
          role: body.stored.role,
          bytes: body.stored.bytes,
          sha256: body.stored.sha256,
          duplicate: body.stored.duplicate,
          renamed: body.stored.renamed,
        }
      : {
          ...base,
          ok: false,
          error: {
            code: body.refusal.code,
            message: body.refusal.message.slice(0, 300),
            retryable: body.refusal.retryable,
          },
        };
    await assertRealDir(opts.dir);
    const handle = await open(
      receiptsFile,
      fsc.O_WRONLY | fsc.O_APPEND | fsc.O_CREAT | fsc.O_NOFOLLOW,
      0o666,
    );
    try {
      const st = await handle.stat();
      if (!st.isFile() || st.nlink !== 1) throw new Error("receipts file is not a plain file");
      await handle.write(`${JSON.stringify(rec)}\n`);
    } finally {
      await handle.close();
    }
  }

  async function serve(raw: string): Promise<void> {
    const parsed = parseAttachRequest(raw, opts.task, maxBytes);
    if (!parsed) return;
    if ("refusal" in parsed) {
      await writeReceipt(parsed.ids, { ok: false, refusal: parsed.refusal });
      return;
    }
    const { request } = parsed;
    const fail = async (refusal: Refusal) => {
      warn("Attach request refused", { task: opts.task.id, code: refusal.code });
      await writeReceipt(request, { ok: false, refusal });
    };
    if (!opts.active()) return fail(refuse("no_active_attempt", "the attempt is no longer active"));
    if (served >= MAX_REQUESTS)
      return fail(refuse("too_many", `at most ${MAX_REQUESTS} attach requests per attempt`));
    served += 1;
    const spooled = await readSpooled(spool, request);
    await rm(path.join(spool, request.nonce), { force: true }).catch(() => {});
    if ("refusal" in spooled) return fail(spooled.refusal);
    let stored: AttachStored;
    try {
      stored = await opts.store({ ...request, data: spooled.data });
    } catch (err) {
      warn("Attach upload failed", { task: opts.task.id, err: String(err).slice(0, 200) });
      stored = {
        ok: false,
        code: "upload_failed",
        message: "the upload failed; retry",
        retryable: true,
      };
    }
    if (!stored.ok) {
      if (stored.retryable) served -= 1; // a retryable failure does not use up the allowance
      return fail(refuse(stored.code, stored.message, stored.retryable));
    }
    await writeReceipt(request, { ok: true, stored });
  }

  /** Complete new lines of the request file, from the offset. */
  async function newLines(): Promise<string[]> {
    let handle: Awaited<ReturnType<typeof open>>;
    try {
      handle = await open(requestsFile, fsc.O_RDONLY | fsc.O_NOFOLLOW | fsc.O_NONBLOCK);
    } catch (err) {
      if ((err as NodeJS.ErrnoException)?.code === "ENOENT") return [];
      stopped = true;
      warn("Attach request file is not a plain file; relay stopped", { task: opts.task.id });
      return [];
    }
    try {
      const st = await handle.stat();
      if (!st.isFile() || st.nlink !== 1) {
        stopped = true;
        warn("Attach request file is not a plain file; relay stopped", { task: opts.task.id });
        return [];
      }
      if (st.size < offset) offset = 0; // truncated by the worker: read what is there
      const want = Math.min(st.size - offset, READ_MAX);
      if (want <= 0) return [];
      const buf = Buffer.alloc(want);
      const { bytesRead } = await handle.read(buf, 0, want, offset);
      const text = buf.subarray(0, bytesRead).toString("utf8");
      const end = text.lastIndexOf("\n");
      if (end === -1) {
        if (bytesRead >= LINE_MAX * 4) offset += bytesRead; // a runaway line: skip it
        return [];
      }
      offset += Buffer.byteLength(text.slice(0, end + 1));
      return text
        .slice(0, end)
        .split("\n")
        .filter((l) => l.trim() && l.length <= LINE_MAX);
    } finally {
      await handle.close();
    }
  }

  async function step(): Promise<number> {
    if (stopped || busy) return 0;
    busy = true;
    try {
      const lines = await newLines();
      for (const line of lines) {
        if (stopped) break;
        await serve(line);
      }
      return lines.length;
    } catch (err) {
      warn("Attach relay failed (will retry)", { err: String(err).slice(0, 200) });
      return 0;
    } finally {
      busy = false;
    }
  }

  return {
    get stopped(): boolean {
      return stopped;
    },
    /** Tell the worker that attaching is on offer for this attempt (atomic replace). */
    async open(): Promise<void> {
      const temp = `${statusFile}.${process.pid}.tmp`;
      await unlink(temp).catch(() => {});
      const handle = await open(
        temp,
        fsc.O_WRONLY | fsc.O_CREAT | fsc.O_EXCL | fsc.O_NOFOLLOW,
        0o644,
      );
      try {
        await handle.write(
          `${JSON.stringify({
            v: ATTACH_PROTOCOL,
            capability: ATTACH_CAPABILITY,
            task_id: opts.task.id,
            attempt: opts.task.attempt,
            max_bytes: maxBytes,
            roles: ATTACH_ROLES,
          })}\n`,
        );
      } finally {
        await handle.close();
      }
      await rename(temp, statusFile);
    },
    /** Serve the requests written so far. Never throws. */
    poll: async (): Promise<void> => void (await step()),
    /** Before the attempt's result is built: serve what the worker already wrote. */
    async drain(): Promise<void> {
      for (let i = 0; i < 5 && (await step()) > 0; i++);
    },
    /** Stop serving and withdraw the offer, so a late `attach` fails as unsupported. */
    stop(): void {
      stopped = true;
      void unlink(statusFile).catch(() => {});
    },
  };
}
