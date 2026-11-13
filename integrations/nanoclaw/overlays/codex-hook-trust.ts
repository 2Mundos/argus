import fs from "fs";
import path from "path";

/**
 * Normal Codex hook trust for the NanoClaw Codex provider (docs/specs/codex-hook-trust.spec.md).
 *
 * The upstream provider ran `codex --dangerously-bypass-hook-trust` and sent
 * `config.bypass_hook_trust` on every thread, so a hook was never checked. Argus removes
 * both and trusts exactly the hooks it registered itself, through Codex's own mechanism:
 * `hooks/list` reports each hook's `currentHash` and `trustStatus`, and a
 * `hooks.state."<key>".trusted_hash` entry in the user config makes it `trusted`. An
 * untrusted hook is skipped by Codex without an error, so every refusal here is thrown
 * before the first thread starts: no model turn runs with a protection hook that is not
 * really active. Hook keys contain the hooks.json path (dots included), so trust is
 * written as one `hooks.state` table rather than through a dotted key path.
 */

export const CODEX_GUARD_HOOK_SCRIPT = "/usr/local/lib/argus/command_guard.py";
export const CODEX_GUARD_HOOK_COMMAND = `python3 ${CODEX_GUARD_HOOK_SCRIPT}`;
const CODEX_GUARD_HOOK_TIMEOUT_S = 10;
// Codex names its shell tool "Bash" for hook matchers and payloads; command_guard.py
// acts only on tool_name "Bash". Not yet observed in a live 0.162.1 turn.
const CODEX_GUARD_MATCHER = "Bash";

export class CodexHookTrustError extends Error {
  constructor(message: string) {
    super(`Codex hook trust: ${message}`);
    this.name = "CodexHookTrustError";
  }
}

export interface ExpectedCodexHook {
  readonly eventName: "preToolUse" | "sessionStart";
  readonly command: string;
  readonly matcher?: string;
}

/** The hooks this process registered: the guard always, the memory hook once it was written. */
const registered = new Map<string, ExpectedCodexHook>();

export function registerExpectedCodexHook(hook: ExpectedCodexHook): void {
  registered.set(`${hook.eventName}\n${hook.command}`, hook);
}

export function expectedCodexHooks(): ExpectedCodexHook[] {
  return [
    { eventName: "preToolUse", command: CODEX_GUARD_HOOK_COMMAND, matcher: CODEX_GUARD_MATCHER },
    ...[...registered.values()].filter(
      (h) => !(h.eventName === "preToolUse" && h.command === CODEX_GUARD_HOOK_COMMAND),
    ),
  ];
}

export function resetExpectedCodexHooksForTest(): void {
  registered.clear();
}

type Json = Record<string, unknown>;

function isRecord(value: unknown): value is Json {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

/**
 * Adds the command guard as the one PreToolUse hook on Bash. Removes any earlier copy
 * first, so repeated writes converge on a single entry; other entries stay untouched.
 */
export function withCodexGuardHook(hooksConfig: Json): void {
  let hooks = hooksConfig.hooks;
  if (hooks === undefined) hooks = hooksConfig.hooks = {};
  if (!isRecord(hooks))
    throw new CodexHookTrustError("hooks config property 'hooks' must be an object");
  const current = hooks.PreToolUse;
  if (current !== undefined && !Array.isArray(current)) {
    throw new CodexHookTrustError("hooks config property 'PreToolUse' must be an array");
  }
  const kept = (current ?? [])
    .map((entry: unknown) => {
      if (!isRecord(entry) || !Array.isArray(entry.hooks)) return entry;
      const remaining = entry.hooks.filter(
        (h: unknown) => !(isRecord(h) && h.command === CODEX_GUARD_HOOK_COMMAND),
      );
      return remaining.length > 0 ? { ...entry, hooks: remaining } : undefined;
    })
    .filter((entry: unknown) => entry !== undefined);
  kept.push({
    matcher: CODEX_GUARD_MATCHER,
    hooks: [
      { type: "command", command: CODEX_GUARD_HOOK_COMMAND, timeout: CODEX_GUARD_HOOK_TIMEOUT_S },
    ],
  });
  hooks.PreToolUse = kept;
}

export interface CodexRpcResponse {
  result?: unknown;
  error?: { message: string };
}
export type CodexRpc = (
  method: string,
  params: Record<string, unknown>,
) => Promise<CodexRpcResponse>;

interface ListedHook {
  key: string;
  eventName: string;
  command?: string;
  matcher?: string | null;
  enabled: boolean;
  source: string;
  sourcePath: string;
  currentHash: string;
  trustStatus: string;
}

export interface EstablishOptions {
  cwd: string;
  /** The hooks.json this provider wrote; only hooks that come from it can be trusted. */
  hooksPath?: string;
  expected?: readonly ExpectedCodexHook[];
  guardScriptExists?: () => boolean;
}

export interface HookTrustReport {
  trusted: string[];
  /** Listed hooks that are not ours: Codex leaves them untrusted and does not run them. */
  skipped: string[];
}

/** Where writeCodexConfigToml writes hooks.json; a CODEX_HOME elsewhere leaves our hooks unlisted. */
export function codexHooksPath(env: NodeJS.ProcessEnv = process.env): string {
  return path.join(env.HOME || "/home/node", ".codex", "hooks.json");
}

async function listHooks(rpc: CodexRpc, cwd: string): Promise<ListedHook[]> {
  const resp = await rpc("hooks/list", { cwds: [cwd] });
  if (resp.error) throw new CodexHookTrustError(`hooks/list failed: ${resp.error.message}`);
  const data = (resp.result as { data?: unknown } | undefined)?.data;
  if (!Array.isArray(data)) throw new CodexHookTrustError("hooks/list returned no data");
  const entry = data.find((d) => isRecord(d) && d.cwd === cwd) ?? data[0];
  if (!isRecord(entry) || !Array.isArray(entry.hooks))
    throw new CodexHookTrustError("hooks/list returned no hooks array");
  if (Array.isArray(entry.errors) && entry.errors.length > 0) {
    throw new CodexHookTrustError(
      `hooks/list reported errors: ${JSON.stringify(entry.errors).slice(0, 300)}`,
    );
  }
  return entry.hooks as ListedHook[];
}

function matches(hook: ListedHook, want: ExpectedCodexHook, hooksPath: string): boolean {
  return (
    hook.eventName === want.eventName &&
    hook.command === want.command &&
    (want.matcher === undefined || hook.matcher === want.matcher) &&
    hook.source === "user" &&
    path.resolve(hook.sourcePath) === path.resolve(hooksPath)
  );
}

/**
 * Trusts each expected hook, and only those, by the hash Codex computed for it, then reads
 * the state back. Throws unless every expected hook is listed, enabled and `trusted`.
 */
export async function establishCodexHookTrust(
  rpc: CodexRpc,
  options: EstablishOptions,
): Promise<HookTrustReport> {
  const hooksPath = options.hooksPath ?? codexHooksPath();
  const expected = options.expected ?? expectedCodexHooks();
  const guardPresent = options.guardScriptExists ?? (() => fs.existsSync(CODEX_GUARD_HOOK_SCRIPT));
  if (!guardPresent()) {
    throw new CodexHookTrustError(
      `${CODEX_GUARD_HOOK_SCRIPT} is missing from this image; refusing to run without the command guard`,
    );
  }

  const listed = await listHooks(rpc, options.cwd);
  const state: Record<string, { trusted_hash: string }> = {};
  for (const want of expected) {
    const hook = listed.find((h) => matches(h, want, hooksPath));
    if (!hook) {
      throw new CodexHookTrustError(
        `${want.eventName} hook '${want.command}' is not registered in ${hooksPath}`,
      );
    }
    if (!hook.enabled)
      throw new CodexHookTrustError(`${want.eventName} hook '${want.command}' is disabled`);
    if (hook.trustStatus === "trusted") continue;
    if (hook.trustStatus !== "untrusted" && hook.trustStatus !== "modified") {
      throw new CodexHookTrustError(
        `${want.eventName} hook '${want.command}' has trust status '${hook.trustStatus}'`,
      );
    }
    if (!/^sha256:[0-9a-f]{64}$/.test(hook.currentHash)) {
      throw new CodexHookTrustError(`hook '${hook.key}' has an unusable hash`);
    }
    state[hook.key] = { trusted_hash: hook.currentHash };
  }
  if (Object.keys(state).length > 0) {
    const written = await rpc("config/batchWrite", {
      edits: [{ keyPath: "hooks.state", mergeStrategy: "upsert", value: state }],
      reloadUserConfig: true,
    });
    if (written.error)
      throw new CodexHookTrustError(`writing hook trust failed: ${written.error.message}`);
  }

  const after = await listHooks(rpc, options.cwd);
  const trusted: string[] = [];
  for (const want of expected) {
    const hook = after.find((h) => matches(h, want, hooksPath));
    if (!hook || !hook.enabled || hook.trustStatus !== "trusted") {
      throw new CodexHookTrustError(
        `${want.eventName} hook '${want.command}' is ${hook ? hook.trustStatus : "not listed"} after trusting; refusing to start a thread`,
      );
    }
    trusted.push(hook.key);
  }
  const skipped = after
    .filter((h) => !trusted.includes(h.key) && h.trustStatus !== "trusted")
    .map((h) => h.key);
  return { trusted, skipped };
}
