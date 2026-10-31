/**
 * The session stderr tail (worker-lifecycle.spec.md 3.12.1): the redacted last bytes of
 * a session container's merged stdout+stderr, stored as the attempt-level `stderr-tail`
 * artifact. Every cap here counts UTF-8 bytes and cuts at a code-point boundary.
 */

export const STDERR_TAIL_ARTIFACT = "stderr-tail";
/** The in-memory tail, and the artifact: 16 KiB. */
export const STDERR_TAIL_MAX_BYTES = 16 * 1024;
/** A task error that carries a tail excerpt stays within this, pointer included. */
export const STDERR_TAIL_INLINE_BYTES = 2 * 1024;
/** Bump when redactSessionTail changes what it scrubs. */
export const STDERR_TAIL_REDACTION_VERSION = "1";
export const STDERR_TAIL_POINTER = ` (full tail: artifact ${STDERR_TAIL_ARTIFACT})`;
/** `docker logs --tail`: lines read per container, before the byte cap. */
export const SESSION_LOG_TAIL_LINES = 200;

/** Best-effort credential scrub for fail-error log excerpts (stall visibility). */
export function redactSessionTail(text: string): string {
  return (
    text
      // A URL's userinfo (OneCLI's session proxy URL carries its agent token) and proxy variables.
      .replace(/\b([a-z][a-z0-9+.-]*:\/\/)[^\s/?#@]*@/gi, "$1[redacted]@")
      .replace(/\b((?:https?|all|no)_proxy\s*[=:]\s*)\S+/gi, "$1[redacted]")
      .replace(/\b(?:bearer\s+|sk-|eyJ|ghp_|github_pat_)[A-Za-z0-9._~+/=-]{8,}/gi, "[redacted]")
      .replace(
        /((?:token|secret|password|passwd|pwd|api[_-]?key)["']?\s*[:=]\s*["']?)[^\s"',;]+/gi,
        "$1[redacted]",
      )
  );
}

const encoder = new TextEncoder();
const decoder = new TextDecoder();
const isContinuation = (b: number | undefined) => b !== undefined && (b & 0xc0) === 0x80;

/** UTF-8 length of `text` (a lone surrogate counts as U+FFFD, as it is encoded). */
export function utf8Bytes(text: string): number {
  return encoder.encode(text).length;
}

/**
 * The last at most `maxBytes` UTF-8 bytes of `text`, starting at a code point: leading
 * continuation bytes of a cut character are dropped, so the result is valid UTF-8.
 */
export function boundTail(
  text: string,
  maxBytes: number,
): { text: string; bytes: number; truncated: boolean } {
  const buf = encoder.encode(text);
  if (buf.length <= maxBytes)
    return { text: decoder.decode(buf), bytes: buf.length, truncated: false };
  let start = buf.length - Math.max(0, maxBytes);
  while (start < buf.length && isContinuation(buf[start])) start += 1;
  return { text: decoder.decode(buf.subarray(start)), bytes: buf.length - start, truncated: true };
}

/** The first at most `maxBytes` UTF-8 bytes of `text`, ending before a cut character. */
export function boundHead(text: string, maxBytes: number): string {
  const buf = encoder.encode(text);
  if (buf.length <= maxBytes) return decoder.decode(buf);
  let end = Math.max(0, maxBytes);
  while (end > 0 && isContinuation(buf[end])) end -= 1;
  return decoder.decode(buf.subarray(0, end));
}

/** `head` + the end of `tail` + `suffix`, within `maxBytes` in total (the tail gives way). */
export function inlineExcerpt(
  head: string,
  tail: string,
  suffix: string,
  maxBytes = STDERR_TAIL_INLINE_BYTES,
): string {
  const budget = maxBytes - utf8Bytes(head) - utf8Bytes(suffix);
  if (budget <= 0) return boundHead(head, maxBytes);
  return `${head}${boundTail(tail, budget).text}${suffix}`;
}

/** What the runtime read of a session container, which may already be gone. */
export interface SessionEvidence {
  exitCode: number | null;
  /** Redacted, then capped to STDERR_TAIL_MAX_BYTES. */
  logTail: string;
  /** Bytes were cut from the front of the redacted text. */
  truncated?: boolean;
  /** See SessionTail.complete; absent means not known to be complete. */
  complete?: boolean;
  /** `State.OOMKilled` from `docker inspect`; absent when not read. Never from the logs. */
  oomKilled?: boolean;
  /** The inspected container's name (without the leading "/"); absent when not read. */
  container?: string;
}

/** True only for the kernel OOM kill of `container` (failure classes 3.2.1). */
export function isOomKill(evidence: SessionEvidence, container: string): boolean {
  return (
    evidence.oomKilled === true && evidence.exitCode === 137 && evidence.container === container
  );
}

/** The tail as stored in the `stderr-tail` artifact. */
export interface SessionTail {
  text: string;
  bytes: number;
  truncated: boolean;
  /**
   * True only when the tail holds everything the container wrote: `docker logs` was
   * read without error, returned fewer lines than its `--tail` limit (so nothing older
   * was left out), and the byte cap cut nothing. False whenever that is not known.
   */
  complete: boolean;
}

/** Redact (again: a tail from a hook may not be) and cap; never trusts a hook's cap. */
export function sessionTail(evidence: Pick<SessionEvidence, "logTail" | "truncated" | "complete">) {
  const bounded = boundTail(redactSessionTail(evidence.logTail), STDERR_TAIL_MAX_BYTES);
  const truncated = bounded.truncated || evidence.truncated === true;
  return {
    text: bounded.text,
    bytes: bounded.bytes,
    truncated,
    complete: evidence.complete === true && !truncated,
  } satisfies SessionTail;
}

/** The artifact's media type: its metadata ride as media-type parameters (no other field). */
export function stderrTailMediaType(tail: SessionTail): string {
  return (
    `text/plain; charset=utf-8; truncated=${tail.truncated}; complete=${tail.complete}` +
    `; bytes=${tail.bytes}; redaction_version=${STDERR_TAIL_REDACTION_VERSION}`
  );
}

/** Runs the container CLI and returns both streams (`docker logs` replays stderr on stderr). */
export type DockerRun = (args: string[]) => Promise<{ stdout: string; stderr: string }>;

/** Exit code and redacted, capped merged stdout+stderr tail of a container. */
export async function readSessionEvidence(
  run: DockerRun,
  containerId: string,
  lines = SESSION_LOG_TAIL_LINES,
): Promise<SessionEvidence> {
  let exitCode: number | null = null;
  const state: Pick<SessionEvidence, "oomKilled" | "container"> = {};
  try {
    const raw = (
      await run([
        "inspect",
        "--format",
        "{{.State.ExitCode}}|{{.State.Running}}|{{.State.OOMKilled}}|{{.Name}}",
        containerId,
      ])
    ).stdout.trim();
    const [code, running, oom, name] = raw.split("|");
    if (running !== "true" && code !== undefined && code !== "") {
      exitCode = Number.parseInt(code, 10);
      if (Number.isNaN(exitCode)) exitCode = null;
    }
    if (oom === "true" || oom === "false") state.oomKilled = oom === "true";
    if (name) state.container = name.replace(/^\//, "");
  } catch {
    /* container may already be removed */
  }
  try {
    const { stdout, stderr } = await run(["logs", "--tail", String(lines), containerId]);
    // Docker's --tail counts lines across both streams; the stream order is lost.
    const merged = [stdout.trimEnd(), stderr.trimEnd()].filter(Boolean).join("\n");
    const read = merged ? merged.split("\n").length : 0;
    // Redaction first: a secret cut by the cap would no longer match its pattern.
    const tail = boundTail(redactSessionTail(merged), STDERR_TAIL_MAX_BYTES);
    return {
      exitCode,
      logTail: tail.text,
      truncated: tail.truncated,
      complete: !tail.truncated && read < lines,
      ...state,
    };
  } catch {
    return { exitCode, logTail: "", truncated: false, complete: false, ...state };
  }
}
