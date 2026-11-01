/**
 * Quota resume (docs/specs/quota-resume.spec.md): what the host can observe of a
 * Claude Code session, and the decision to resume it or start fresh. Pure and
 * file-system only; the channel does the Argus calls and the sending.
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

/** The session transcripts under `<projectsDir>/<project>/<session id>.jsonl`. */
async function transcripts(projectsDir: string, sinceMs = 0): Promise<string[]> {
  const { readdir, stat } = await import("node:fs/promises");
  const path = await import("node:path");
  const found: string[] = [];
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
        const info = await stat(path.join(projectsDir, project, file));
        if (info.isFile() && info.size > 0 && info.mtimeMs >= sinceMs)
          found.push(file.slice(0, -".jsonl".length));
      } catch {}
    }
  }
  return found;
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
  const ids = [...new Set(await transcripts(projectsDir, sinceMs))];
  const [only] = ids;
  return ids.length === 1 && only && SESSION_ID.test(only) ? only : null;
}

/** The transcript of `sessionId` still exists and is not empty. */
export async function claudeSessionReadable(
  projectsDir: string,
  sessionId: string,
): Promise<boolean> {
  return SESSION_ID.test(sessionId) && (await transcripts(projectsDir)).includes(sessionId);
}
