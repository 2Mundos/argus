import fs from "fs";

export const CODEX_ENGINE_DIR_DEFAULT = "/pnpm";

/**
 * PATH for the Codex app-server child (worker-lifecycle spec 3.8): the engine dir
 * (PNPM_HOME) goes first when it exists and the base PATH does not list it yet.
 * Never fabricates a PATH: an absent base stays absent unless the dir is usable.
 */
export function resolveCodexCliPath(
  basePath: string | undefined,
  engineDir: string,
  isDirectory: (dir: string) => boolean,
): string | undefined {
  if (basePath?.split(":").includes(engineDir)) return basePath;
  if (!isDirectory(engineDir)) return basePath;
  return basePath === undefined ? engineDir : `${engineDir}:${basePath}`;
}

function isDirectorySync(dir: string): boolean {
  try {
    return fs.statSync(dir).isDirectory();
  } catch {
    return false;
  }
}

export function codexCliPath(
  basePath: string | undefined,
  pnpmHome: string | undefined,
): string | undefined {
  return resolveCodexCliPath(basePath, pnpmHome || CODEX_ENGINE_DIR_DEFAULT, isDirectorySync);
}
