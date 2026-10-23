/**
 * Bounded removal confirmation after `docker rm` fails.
 *
 * A single synchronous `docker ps` is racy: `--rm` containers can still list
 * while Docker tears them down, and a failed `rm` does not prove the name is
 * still held. Poll until the name is gone or the deadline passes; only then
 * treat a lingering listing as a real teardown failure.
 */
import type { Cli } from "./cli.js";
import type { SessionFailure } from "./types.js";

export const DEFAULT_REMOVAL_POLL_MS = 100;
export const DEFAULT_REMOVAL_DEADLINE_MS = 30_000;

export function dockerPsNameFilterArgs(name: string): string[] {
  return ["ps", "-a", "--filter", `name=^/${name}$`, "--format", "{{.Names}}"];
}

/** True when `docker ps --format '{{.Names}}'` still lists the session container. */
export function dockerPsListsName(psOutput: string, name: string): boolean {
  if (!psOutput.trim()) return false;
  for (const line of psOutput.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    if (trimmed === name || trimmed.endsWith(`/${name}`)) return true;
  }
  return false;
}

export type NormalizeDockerError = (error: unknown) => Error & SessionFailure;

export async function waitForContainerRemoval(args: {
  cli: Cli;
  name: string;
  rmError: unknown;
  normalizeError: NormalizeDockerError;
  deadlineMs?: number;
  pollMs?: number;
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
}): Promise<void> {
  const deadlineMs = args.deadlineMs ?? DEFAULT_REMOVAL_DEADLINE_MS;
  const pollMs = args.pollMs ?? DEFAULT_REMOVAL_POLL_MS;
  const now = args.now ?? (() => Date.now());
  const sleep = args.sleep ?? ((ms: number) => new Promise((resolve) => setTimeout(resolve, ms)));
  const started = now();

  for (;;) {
    try {
      const remaining = args.cli.run(dockerPsNameFilterArgs(args.name));
      if (!dockerPsListsName(remaining, args.name)) return;
    } catch (probeError) {
      throw args.normalizeError(probeError);
    }
    if (now() - started >= deadlineMs) {
      throw args.normalizeError(args.rmError);
    }
    await sleep(pollMs);
  }
}
