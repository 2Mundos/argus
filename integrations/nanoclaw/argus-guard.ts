import { readFileSync } from "node:fs";

/** Engines whose image registers the command guard hook (command guard spec 3). */
const GUARDED_ENGINES = new Set(["claude"]);

/** True when the managed settings register `command_guard.py` as a PreToolUse Bash hook. */
export function registersCommandGuard(settings: unknown): boolean {
  const pre = (settings as { hooks?: { PreToolUse?: unknown } } | null)?.hooks?.PreToolUse;
  if (!Array.isArray(pre)) return false;
  return pre.some((entry) => {
    const e = entry as { matcher?: unknown; hooks?: unknown } | null;
    if (typeof e?.matcher !== "string" || !e.matcher.split("|").includes("Bash")) return false;
    return (
      Array.isArray(e.hooks) &&
      e.hooks.some((h) =>
        String((h as { command?: unknown })?.command).includes("command_guard.py"),
      )
    );
  });
}

/** Where Claude Code reads managed settings on Linux; the tools layer installs them here. */
export const DEFAULT_MANAGED_SETTINGS_PATH = "/etc/claude-code/managed-settings.json";

/**
 * `env_guard_missing` when a guarded engine would start without the command guard hook
 * (command guard spec 3, failure classes 3.3). Null when the guard is off
 * (`ARGUS_COMMAND_GUARD=0|off`), the engine is not guarded, or the hook is registered.
 * An unset or empty `settingsPath` checks `DEFAULT_MANAGED_SETTINGS_PATH`: a missing,
 * unreadable or malformed file, or one without the hook, blocks.
 */
export function commandGuardBlock(
  engine: string,
  settingsPath: string | undefined,
  env: NodeJS.ProcessEnv = process.env,
  read: (path: string) => string = (p) => readFileSync(p, "utf8"),
): { reason: string; error: string } | null {
  const off = env.ARGUS_COMMAND_GUARD?.trim().toLowerCase();
  if (off === "0" || off === "off") return null;
  if (!GUARDED_ENGINES.has(engine)) return null;
  const path = settingsPath?.trim() || DEFAULT_MANAGED_SETTINGS_PATH;
  let registered = false;
  try {
    registered = registersCommandGuard(JSON.parse(read(path)));
  } catch {
    // unreadable or malformed settings: the hook is not in effect
  }
  if (registered) return null;
  return {
    reason: "env_guard_missing",
    error: `the ${engine} engine would start without the command guard hook (${path} does not register command_guard.py as a PreToolUse Bash hook)`,
  };
}

/** The worker's check: the channel's path, else `ARGUS_CLAUDE_MANAGED_SETTINGS`, else the default. */
export function workerCommandGuardBlock(
  engine: string,
  configuredPath: string | undefined,
  env: NodeJS.ProcessEnv = process.env,
  read?: (path: string) => string,
): { reason: string; error: string } | null {
  return commandGuardBlock(
    engine,
    configuredPath ?? env.ARGUS_CLAUDE_MANAGED_SETTINGS?.trim(),
    env,
    read,
  );
}
