/**
 * Quota resume (docs/specs/quota-resume.spec.md): what the host can observe of a
 * Claude Code session, the decision to resume it or start fresh, and the seed that
 * makes a new session resume it. File-system and session mailbox only; the channel
 * does the Argus calls and the sending.
 */

/** The native session a quota pause left, as the claim offers it (3.3). */
export interface QuotaResumeOffer {
  session_id: string;
  engine: string;
  node: string;
  from_attempt: number;
}

/** Only Claude Code sessions are resumed; the Argus provider name is the engine. */
export const RESUME_ENGINE = "anthropic";

export type FallbackReason =
  | "other_node"
  | "other_engine"
  | "engine_unsupported"
  | "not_available"
  | "unreadable"
  | "resume_failed";

export type ResumeDecision = { action: "resume" } | { action: "fallback"; reason: FallbackReason };

const SESSION_ID = /^[A-Za-z0-9_-]{8,64}$/;

/**
 * Resume only the same node, the same engine, a supported engine, a host that can start
 * a session from a native id, and a transcript that exists; anything else is a fresh
 * session, and the reason is reported (3.4).
 */
export function decideQuotaResume(
  offer: QuotaResumeOffer,
  here: { node: string | null; engine: string | null; canResume: boolean; readable: boolean },
): ResumeDecision {
  if (offer.node !== here.node) return { action: "fallback", reason: "other_node" };
  if (offer.engine !== here.engine) return { action: "fallback", reason: "other_engine" };
  if (offer.engine !== RESUME_ENGINE) return { action: "fallback", reason: "engine_unsupported" };
  if (!here.canResume) return { action: "fallback", reason: "not_available" };
  if (!here.readable) return { action: "fallback", reason: "unreadable" };
  return { action: "resume" };
}

/**
 * Pinned runner `rotateClaudeContinuation` defaults (NanoClaw 8fbacc9): a seed whose
 * transcript is over this size or older than this age is cleared and the container
 * starts fresh. Matching them here is what makes `resumed` fail closed.
 */
export const RUNNER_MAX_TRANSCRIPT_BYTES = 12 * 1024 * 1024;
export const RUNNER_MAX_TRANSCRIPT_AGE_MS = 14 * 86_400_000;

type TranscriptHit = { id: string; path: string; size: number; mtimeMs: number };

/** The session transcripts under `<projectsDir>/<project>/<session id>.jsonl`. */
async function transcripts(projectsDir: string, sinceMs = 0): Promise<TranscriptHit[]> {
  const { readdir, stat } = await import("node:fs/promises");
  const path = await import("node:path");
  const found: TranscriptHit[] = [];
  let projects: string[];
  try {
    projects = await readdir(projectsDir);
  } catch {
    return found;
  }
  for (const project of projects) {
    let files: string[];
    try {
      files = (await readdir(path.join(projectsDir, project))).filter((f) => f.endsWith(".jsonl"));
    } catch {
      continue;
    }
    for (const file of files) {
      try {
        const filePath = path.join(projectsDir, project, file);
        const info = await stat(filePath);
        if (info.isFile() && info.size > 0 && info.mtimeMs >= sinceMs)
          found.push({
            id: file.slice(0, -".jsonl".length),
            path: filePath,
            size: info.size,
            mtimeMs: info.mtimeMs,
          });
      } catch {}
    }
  }
  return found;
}

/**
 * True when the pinned runner would keep this transcript for a cold resume: non-empty,
 * at most 12 MiB, and (when the first JSONL entry has a timestamp) at most 14 days old.
 * Anything else is discarded before `Resuming agent session` is logged.
 */
export function transcriptResumableByRunner(
  size: number,
  firstLine: string,
  nowMs = Date.now(),
): boolean {
  if (!(size > 0) || size > RUNNER_MAX_TRANSCRIPT_BYTES) return false;
  try {
    const timestamp = JSON.parse(firstLine)?.timestamp;
    const startMs = timestamp ? Date.parse(timestamp) : NaN;
    if (!Number.isNaN(startMs) && nowMs - startMs > RUNNER_MAX_TRANSCRIPT_AGE_MS) return false;
  } catch {
    // Runner ignores an unreadable first entry and still resumes on size alone.
  }
  return true;
}

/**
 * The Claude Code session id of the attempt that just ended: the one transcript written
 * since `sinceMs`. Zero or several (a delegated run, a second session) are ambiguous and
 * give null: a wrong id would resume somebody else's conversation.
 */
export async function observeClaudeSession(
  projectsDir: string,
  sinceMs: number,
): Promise<string | null> {
  const ids = [...new Set((await transcripts(projectsDir, sinceMs)).map((t) => t.id))];
  const [only] = ids;
  return ids.length === 1 && only && SESSION_ID.test(only) ? only : null;
}

/**
 * The transcript of `sessionId` still exists and the pinned runner would actually
 * resume it (not rotate it away). Missing, empty, oversized or too-old → false.
 */
export async function claudeSessionReadable(
  projectsDir: string,
  sessionId: string,
): Promise<boolean> {
  if (!SESSION_ID.test(sessionId)) return false;
  const hit = (await transcripts(projectsDir)).find((t) => t.id === sessionId);
  if (!hit) return false;
  try {
    const { open } = await import("node:fs/promises");
    const fh = await open(hit.path, "r");
    try {
      const buf = Buffer.alloc(4096);
      const { bytesRead } = await fh.read(buf, 0, buf.length, 0);
      const firstLine = buf.subarray(0, bytesRead).toString("utf8").split(/\r?\n/, 1)[0] ?? "";
      return transcriptResumableByRunner(hit.size, firstLine);
    } finally {
      await fh.close();
    }
  } catch {
    return false;
  }
}

/**
 * The agent runner's key for the Claude session it resumes at container start, and
 * the pre-per-provider key it would adopt instead (NanoClaw 8fbacc9,
 * container/agent-runner/src/db/session-state.ts).
 */
const CONTINUATION_KEY = "continuation:claude";
const LEGACY_CONTINUATION_KEY = "sdk_session_id";

/**
 * Make the session whose mailbox is `outboundDb` resume `sessionId` when its container
 * starts (quota resume spec 3.4). True only when the continuation now holds exactly
 * that id: written here once, or already there from an earlier call. False, writing
 * nothing, for any other layout, another continuation, or a session that already ran.
 */
export async function seedClaudeContinuation(
  outboundDb: string,
  sessionId: string,
): Promise<boolean> {
  if (!SESSION_ID.test(sessionId)) return false;
  let db: import("better-sqlite3").Database | undefined;
  try {
    const { default: Database } = await import("better-sqlite3");
    db = new Database(outboundDb, { fileMustExist: true });
    db.pragma("busy_timeout = 5000");
    const conn = db;
    const columns = (conn.pragma("table_info('session_state')") as { name: string }[])
      .map((c) => c.name)
      .sort()
      .join(",");
    if (columns !== "key,updated_at,value") return false;
    const value = (key: string) =>
      (
        conn.prepare("SELECT value FROM session_state WHERE key = ?").get(key) as
          | { value: string }
          | undefined
      )?.value;
    return conn
      .transaction(() => {
        const held = value(CONTINUATION_KEY);
        if (held !== undefined) return held === sessionId;
        if (value(LEGACY_CONTINUATION_KEY) !== undefined) return false;
        const ran =
          conn.prepare("SELECT 1 FROM messages_out LIMIT 1").get() ??
          conn.prepare("SELECT 1 FROM processing_ack LIMIT 1").get();
        if (ran) return false;
        conn
          .prepare("INSERT INTO session_state (key, value, updated_at) VALUES (?, ?, ?)")
          .run(CONTINUATION_KEY, sessionId, new Date().toISOString());
        return true;
      })
      .immediate();
  } catch {
    return false;
  } finally {
    db?.close();
  }
}
