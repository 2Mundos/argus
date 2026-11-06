import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
import { promisify } from "node:util";

/** Engines whose image registers the command guard hook (command guard spec 3). */
const GUARDED_ENGINES = new Set(["claude"]);

type GuardBlock = { reason: string; error: string };

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

/** False when the guard is off (`ARGUS_COMMAND_GUARD=0|off`) or the engine has no hook. */
function guarded(engine: string, env: NodeJS.ProcessEnv): boolean {
  const off = env.ARGUS_COMMAND_GUARD?.trim().toLowerCase();
  return off !== "0" && off !== "off" && GUARDED_ENGINES.has(engine);
}

/** Null when `read()` yields settings that register the hook; else `env_guard_missing`. */
function blockUnlessRegistered(
  engine: string,
  where: string,
  read: () => string,
): GuardBlock | null {
  let registered = false;
  try {
    registered = registersCommandGuard(JSON.parse(read()));
  } catch {
    // unreadable or malformed settings: the hook is not in effect
  }
  if (registered) return null;
  return {
    reason: "env_guard_missing",
    error: `the ${engine} engine would start without the command guard hook (${where} does not register command_guard.py as a PreToolUse Bash hook)`,
  };
}

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
): GuardBlock | null {
  if (!guarded(engine, env)) return null;
  const path = settingsPath?.trim() || DEFAULT_MANAGED_SETTINGS_PATH;
  return blockUnlessRegistered(engine, path, () => read(path));
}

/**
 * The check on this process's own filesystem: the channel's path, else
 * `ARGUS_CLAUDE_MANAGED_SETTINGS`, else the default. Only for an adapter without a
 * session image (tests); a NanoClaw host uses `sessionImageCommandGuardBlock`.
 */
export function workerCommandGuardBlock(
  engine: string,
  configuredPath: string | undefined,
  env: NodeJS.ProcessEnv = process.env,
  read?: (path: string) => string,
): GuardBlock | null {
  return commandGuardBlock(
    engine,
    configuredPath ?? env.ARGUS_CLAUDE_MANAGED_SETTINGS?.trim(),
    env,
    read,
  );
}

const execFileP = promisify(execFile);

/**
 * `path` inside `image`, printed by a throwaway container with no network that never
 * pulls (`CONTAINER_RUNTIME_BIN`, default `docker`). Rejects when the image is not on
 * this daemon or the file is not in it.
 */
export async function readImageFile(
  image: string,
  path: string,
  bin: string = process.env.CONTAINER_RUNTIME_BIN?.trim() || "docker",
): Promise<string> {
  if (!image || image.startsWith("-")) throw new Error(`not an image reference: ${image}`);
  const args = ["run", "--rm", "--pull=never", "--network=none", "--entrypoint", "cat"];
  const { stdout } = await execFileP(bin, [...args, image, path], {
    timeout: 60_000,
    maxBuffer: 1024 * 1024,
  });
  return stdout;
}

/**
 * The worker's check against the image the agent session will run (command guard spec 3).
 * The channel runs on the NanoClaw host, whose own filesystem has no managed settings:
 * only the agent image carries them. `image` names the session's image and `read` prints
 * a file from it; either failing blocks, as a file without the hook does.
 */
export async function sessionImageCommandGuardBlock(
  engine: string,
  image: () => Promise<string>,
  read: (image: string, path: string) => Promise<string> = readImageFile,
  env: NodeJS.ProcessEnv = process.env,
): Promise<GuardBlock | null> {
  if (!guarded(engine, env)) return null;
  let where = `${DEFAULT_MANAGED_SETTINGS_PATH} in the agent image`;
  let text = "";
  try {
    const ref = await image();
    where = `${DEFAULT_MANAGED_SETTINGS_PATH} in image ${ref}`;
    text = await read(ref, DEFAULT_MANAGED_SETTINGS_PATH);
  } catch (err) {
    where += ` (unreadable: ${String(err).split("\n")[0]?.slice(0, 300)})`;
  }
  return blockUnlessRegistered(engine, where, () => text);
}
