/**
 * Per-task git author (settings spec 3.8). Argus sends the effective project-over-global
 * identity in the claim; the worker puts it in its own process environment for that task
 * only, because the agent container copies ARGUS_GIT_AUTHOR_* from there when a session
 * spawns and the in-container git wrapper reads nothing else. It is never written to a git
 * config or any file. After the task the pool's pinned value (the fallback) comes back.
 */
export interface GitIdentity {
  name: string;
  email: string;
}

const NAME = "ARGUS_GIT_AUTHOR_NAME";
const EMAIL = "ARGUS_GIT_AUTHOR_EMAIL";

/** The identity usable from a claim: both parts non-empty, else null (fail closed). */
export function claimedGitIdentity(raw: unknown): GitIdentity | null {
  if (!raw || typeof raw !== "object") return null;
  const { name, email } = raw as Record<string, unknown>;
  if (typeof name !== "string" || typeof email !== "string") return null;
  const n = name.trim();
  const e = email.trim();
  return n && e ? { name: n, email: e } : null;
}

const GUARD = "ARGUS_COMMAND_GUARD";

/**
 * Command-guard opt-out (command-guard spec 3). A claim with `command_guard_enabled: false`
 * sets ARGUS_COMMAND_GUARD=off for the attempt; anything else restores the pool's value.
 */
export function scopeCommandGuard(
  env: NodeJS.ProcessEnv,
  pool: string | undefined,
  enabled: unknown,
): void {
  const next = enabled === false ? "off" : pool;
  if (next) env[GUARD] = next;
  else delete env[GUARD];
}

/** What the pool pinned at container start; the fallback when a task carries none. */
export function poolGitIdentity(env: NodeJS.ProcessEnv): Partial<GitIdentity> {
  return { name: env[NAME], email: env[EMAIL] };
}

/**
 * Sets `env` to the task's identity, or back to the pool's when the task has none. A
 * pool value that is unset stays unset, so the in-container git still refuses to commit.
 */
export function scopeGitIdentity(
  env: NodeJS.ProcessEnv,
  pool: Partial<GitIdentity>,
  task: GitIdentity | null,
): void {
  const next = task ?? pool;
  for (const [key, value] of [
    [NAME, next.name],
    [EMAIL, next.email],
  ] as const) {
    if (value) env[key] = value;
    else delete env[key];
  }
}
