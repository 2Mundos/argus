/** Argus delivery ceiling for a single Codex engine turn (wrapper + gates + CI). */
export const CODEX_TURN_TIMEOUT_DEFAULT_MS = 10 * 60 * 1000;
export const CODEX_TURN_TIMEOUT_MIN_MS = 60_000;
export const CODEX_TURN_TIMEOUT_MAX_MS = 2 * 60 * 60 * 1000;

export class CodexTurnTimeoutConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'CodexTurnTimeoutConfigError';
  }
}

/** `ARGUS_CODEX_TURN_TIMEOUT_MS` — optional; defaults to upstream 10 minutes. */
export function resolveCodexTurnTimeoutMs(env: NodeJS.ProcessEnv = process.env): number {
  const raw = env.ARGUS_CODEX_TURN_TIMEOUT_MS?.trim();
  if (!raw) return CODEX_TURN_TIMEOUT_DEFAULT_MS;
  if (!/^\d+$/.test(raw)) {
    throw new CodexTurnTimeoutConfigError(
      'ARGUS_CODEX_TURN_TIMEOUT_MS must be a positive integer number of milliseconds',
    );
  }
  const ms = Number(raw);
  if (!Number.isSafeInteger(ms) || ms < CODEX_TURN_TIMEOUT_MIN_MS || ms > CODEX_TURN_TIMEOUT_MAX_MS) {
    throw new CodexTurnTimeoutConfigError(
      `ARGUS_CODEX_TURN_TIMEOUT_MS must be between ${CODEX_TURN_TIMEOUT_MIN_MS} and ${CODEX_TURN_TIMEOUT_MAX_MS}`,
    );
  }
  return ms;
}
