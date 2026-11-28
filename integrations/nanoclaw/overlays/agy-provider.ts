/**
 * Google Antigravity (`agy`) harness provider for the NanoClaw agent runner
 * (google accounts spec 3). Installed by engines/install-engines.sh as
 * container/agent-runner/src/providers/agy.ts.
 *
 * Drives `agy acp`, the CLI's Agent Client Protocol server over stdio. The worker
 * container and the OneCLI allow-list are the boundary, as for the Claude, Codex and
 * Muse harnesses: permissions are auto-approved with `--dangerously-skip-permissions`
 * (with an allow_once/allow_always fallback if the server still requests permission).
 * `--gemini-dir` points at the per-group writable mount (`/home/node/.agy-data`,
 * overlays/agy-host.ts) so sessions persist across container respawns while `HOME`
 * stays `/home/node` for git, ssh and other tools.
 *
 * Auth is staged from the gateway-supplied sentinel before spawning `agy acp` and
 * fails closed when neither mode is present:
 * - API key mode (`GEMINI_API_KEY` sentinel): writes `settings.json` with
 *   `{"modelProvider":"gemini"}` and removes any stale OAuth stub from `dataDir`.
 * - OAuth subscription mode (`ARGUS_AGY_OAUTH_STUB` or the OneCLI stub path):
 *   copies the read-only sentinel stub into `<dataDir>/antigravity-cli/antigravity-oauth-token`,
 *   clears `modelProvider` in `settings.json`, and scrubs `GEMINI_API_KEY`.
 * No real credential ever enters the container.
 */
import { type ChildProcessWithoutNullStreams, spawn } from "child_process";
import fs from "fs";
import path from "path";
import readline from "readline";

import { memoryContextForSessionStart } from "../memory/session-hook.js";
import { shimCwd } from "./cwd-shim.js";
import { registerProvider } from "./provider-registry.js";
import type {
  AgentProvider,
  AgentQuery,
  McpServerConfig,
  ProviderEvent,
  ProviderOptions,
  QueryInput,
} from "./types.js";

/** The error `session/load` returns for a session this host does not hold (agy 1.5.12+). */
const SESSION_NOT_FOUND = -32002;
const METHOD_NOT_FOUND = -32601;
const STDERR_TAIL_CHARS = 1000;
/** Session state and staged auth, on the per-group mount (overlays/agy-host.ts). */
const AGY_DATA_DIR = "/home/node/.agy-data";
/** Read-only OAuth sentinel stub mounted by OneCLI (argus-agy-stub.ts). */
const AGY_OAUTH_STUB_PATH = "/home/node/.gemini/antigravity-cli/antigravity-oauth-token";
/** Not a credential: mirrors AGY_SENTINEL_API_KEY in argus-agy-stub.ts and argus-delegate.mjs. */
export const AGY_SENTINEL_API_KEY = "AIzaSyARGUS_ONECLI_SENTINEL_not_a_credential_00000";

type Json = Record<string, unknown>;

export interface AgyRuntime {
  command: string;
  commandArgs: string[];
  /** Holds the shared memory tree. */
  agentDir: string;
  /** `--gemini-dir` of the CLI: where its sessions, settings and staged auth live. */
  dataDir: string;
  /** Read-only OAuth sentinel stub path written by OneCLI (argus-agy-stub.ts). */
  oauthStubPath: string;
}

const DEFAULT_RUNTIME: AgyRuntime = {
  command: "agy",
  commandArgs: [],
  agentDir: "/workspace/agent",
  dataDir: AGY_DATA_DIR,
  oauthStubPath: AGY_OAUTH_STUB_PATH,
};

export class AgyAcpError extends Error {
  readonly code?: number;

  constructor(message: string, code?: number) {
    super(message);
    this.name = "AgyAcpError";
    this.code = code;
  }
}

/** The ACP form of the runner's MCP servers; ACP stdio servers have no cwd field. */
export function acpMcpServers(servers: Record<string, McpServerConfig>): Json[] {
  const pairs = (values: Record<string, string> = {}) =>
    Object.entries(values).map(([name, value]) => ({ name, value }));
  return Object.entries(servers).map(([name, config]) => {
    if (config.type === "http") {
      return { type: "http", name, url: config.url, headers: pairs(config.headers) };
    }
    const server = shimCwd(config) as Extract<McpServerConfig, { command: string }>;
    return { name, command: server.command, args: server.args ?? [], env: pairs(server.env) };
  });
}

/**
 * Stage the active gateway auth mode into `<dataDir>/antigravity-cli/` and return the
 * sanitized child environment. Refuses to run when neither gateway sentinel is present.
 */
export function stageAgyAuth(
  baseEnv: NodeJS.ProcessEnv,
  dataDir: string,
  defaultOauthStubPath: string = AGY_OAUTH_STUB_PATH,
): { mode: "api_key" | "oauth"; env: NodeJS.ProcessEnv } {
  const apiKeyMode = baseEnv.GEMINI_API_KEY === AGY_SENTINEL_API_KEY;
  const stubPath = baseEnv.ARGUS_AGY_OAUTH_STUB || defaultOauthStubPath;
  const oauthMode = !apiKeyMode && Boolean(stubPath) && fs.existsSync(stubPath);
  if (!apiKeyMode && !oauthMode) {
    throw new AgyAcpError(
      "no usable google account: neither the gateway GEMINI_API_KEY sentinel nor antigravity-oauth-token stub is present",
    );
  }

  const cliDir = path.join(dataDir, "antigravity-cli");
  fs.mkdirSync(cliDir, { recursive: true, mode: 0o700 });
  const settingsPath = path.join(cliDir, "settings.json");
  const tokenDest = path.join(cliDir, "antigravity-oauth-token");

  let settings: Record<string, unknown> = {};
  try {
    const parsed = JSON.parse(fs.readFileSync(settingsPath, "utf8")) as unknown;
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      settings = { ...(parsed as Record<string, unknown>) };
    }
  } catch {
    // Missing or unreadable settings.json: start clean.
  }

  const env: NodeJS.ProcessEnv = { ...baseEnv, NO_BROWSER: "1" };
  delete env.ARGUS_AGY_OAUTH_STUB;

  if (apiKeyMode) {
    settings.modelProvider = "gemini";
    fs.writeFileSync(settingsPath, `${JSON.stringify(settings, null, 2)}\n`, { mode: 0o600 });
    fs.rmSync(tokenDest, { force: true });
    return { mode: "api_key", env };
  }

  delete settings.modelProvider;
  fs.writeFileSync(settingsPath, `${JSON.stringify(settings, null, 2)}\n`, { mode: 0o600 });
  fs.copyFileSync(stubPath, tokenDest);
  fs.chmodSync(tokenDest, 0o600);
  delete env.GEMINI_API_KEY;
  return { mode: "oauth", env };
}

class AcpConnection {
  onUpdate: ((update: Json) => void) | null = null;
  failure: AgyAcpError | null = null;
  private readonly child: ChildProcessWithoutNullStreams;
  private readonly calls = new Map<
    number,
    { resolve: (result: Json) => void; reject: (err: Error) => void }
  >();
  private nextId = 0;
  private stderr = "";

  constructor(child: ChildProcessWithoutNullStreams, onClosed: () => void) {
    this.child = child;
    const fail = (message: string) => {
      this.failure ??= new AgyAcpError(message);
      for (const call of this.calls.values()) call.reject(this.failure);
      this.calls.clear();
      onClosed();
    };
    child.on("error", (err) => fail(`agy acp could not start: ${err.message}`));
    // `close`, not `exit`: everything the agent wrote has been read by then.
    child.on("close", (code, signal) => {
      const tail = this.stderr.trim();
      fail(`agy acp exited (${signal ?? code})${tail ? `: ${tail}` : ""}`);
    });
    child.stdin.on("error", () => {}); // a dead agent surfaces through `close`
    child.stderr.on("data", (data) => {
      this.stderr = `${this.stderr}${data}`.slice(-STDERR_TAIL_CHARS);
    });
    readline.createInterface({ input: child.stdout }).on("line", (line) => this.receive(line));
  }

  request(method: string, params: Json): Promise<Json> {
    if (this.failure) return Promise.reject(this.failure);
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      this.calls.set(id, { resolve, reject });
      this.write({ id, method, params });
    });
  }

  notify(method: string, params: Json): void {
    this.write({ method, params });
  }

  close(): void {
    this.child.kill(); // SIGTERM: the CLI flushes its session state first
  }

  private write(message: Json): void {
    if (!this.failure) {
      this.child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", ...message })}\n`);
    }
  }

  private receive(line: string): void {
    let message: Json;
    try {
      message = JSON.parse(line) as Json;
    } catch {
      return; // stdout carries ACP only; ignore anything else rather than end the turn
    }
    const { id, method, params } = message;
    if (typeof method === "string") {
      if (id !== undefined) this.answer(id, method, (params ?? {}) as Json);
      else if (method === "session/update") {
        this.onUpdate?.(((params ?? {}) as Json).update as Json);
      }
      return;
    }
    const call = typeof id === "number" ? this.calls.get(id) : undefined;
    if (!call) return;
    this.calls.delete(id as number);
    const error = message.error as { code?: number; message?: string } | undefined;
    if (error) call.reject(new AgyAcpError(error.message ?? "agy acp error", error.code));
    else call.resolve((message.result ?? {}) as Json);
  }

  private answer(id: unknown, method: string, params: Json): void {
    if (method !== "session/request_permission") {
      this.write({
        id,
        error: { code: METHOD_NOT_FOUND, message: `unsupported client method: ${method}` },
      });
      return;
    }
    const options = (params.options ?? []) as Array<{ optionId: string; kind?: string }>;
    const allow =
      options.find((o) => o.kind === "allow_once") ??
      options.find((o) => o.kind === "allow_always");
    this.write({
      id,
      result: {
        outcome: allow
          ? { outcome: "selected", optionId: allow.optionId }
          : { outcome: "cancelled" },
      },
    });
  }
}

interface QueryState {
  pending: string[];
  ended: boolean;
  aborted: boolean;
  wake: (() => void) | null;
  connection: AcpConnection | null;
  sessionId: string | null;
  inTurn: boolean;
}

const kick = (state: QueryState): void => {
  state.wake?.();
  state.wake = null;
};

export class AgyProvider implements AgentProvider {
  readonly supportsNativeSlashCommands = false;
  private readonly options: ProviderOptions;
  private readonly runtime: AgyRuntime;

  constructor(options: ProviderOptions = {}, runtime: AgyRuntime = DEFAULT_RUNTIME) {
    this.options = options;
    this.runtime = runtime;
  }

  // Agy has no session-start hook over ACP: a new session gets memory in its system prompt.
  registerMemorySessionHook(): void {}

  isSessionInvalid(err: unknown): boolean {
    return err instanceof AgyAcpError && err.code === SESSION_NOT_FOUND;
  }

  query(input: QueryInput): AgentQuery {
    const state: QueryState = {
      pending: [input.prompt],
      ended: false,
      aborted: false,
      wake: null,
      connection: null,
      sessionId: null,
      inTurn: false,
    };
    return {
      // ACP has no mid-turn steering: a follow-up runs as the next turn.
      push: (message) => {
        state.pending.push(message);
        kick(state);
      },
      end: () => {
        state.ended = true;
        kick(state);
      },
      abort: () => {
        state.aborted = true;
        if (state.connection && state.sessionId && state.inTurn) {
          state.connection.notify("session/cancel", { sessionId: state.sessionId });
        }
        kick(state);
      },
      events: this.run(input, state),
    };
  }

  private async *run(input: QueryInput, state: QueryState): AsyncGenerator<ProviderEvent> {
    const { model, effort } = this.options;
    if (!model) {
      // Never the CLI's own default: a worker switching engines must not run on a guess.
      const err = new AgyAcpError("agy needs an explicit model");
      yield { type: "error", message: err.message, retryable: false };
      throw err;
    }

    let stagedEnv: NodeJS.ProcessEnv;
    try {
      const staged = stageAgyAuth(
        { ...process.env, ...this.options.env },
        this.runtime.dataDir,
        this.runtime.oauthStubPath,
      );
      stagedEnv = staged.env;
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      yield { type: "error", message, retryable: false };
      throw err;
    }

    const child = spawn(
      this.runtime.command,
      [
        ...this.runtime.commandArgs,
        "acp",
        "--dangerously-skip-permissions",
        "--disable-telemetry",
        "--gemini-dir",
        this.runtime.dataDir,
      ],
      {
        cwd: input.cwd,
        env: stagedEnv,
        stdio: "pipe",
      },
    );
    const connection = new AcpConnection(child, () => kick(state));
    state.connection = connection;
    try {
      await connection.request("initialize", {
        protocolVersion: 1,
        clientCapabilities: {
          fs: { readTextFile: false, writeTextFile: false },
          terminal: false,
        },
      });
      const session = await this.openSession(connection, input);
      await connection.request("session/set_config_option", {
        sessionId: session.id,
        configId: "model",
        value: model,
      });
      if (effort) {
        await connection.request("session/set_config_option", {
          sessionId: session.id,
          configId: "thought_level",
          value: effort,
        });
      }
      state.sessionId = session.id;
      yield { type: "init", continuation: session.id };

      while (true) {
        while (!state.pending.length && !state.ended && !state.aborted && !connection.failure) {
          await new Promise<void>((resolve) => {
            state.wake = resolve;
          });
        }
        if (state.aborted) return;
        if (connection.failure && !state.ended) throw connection.failure;
        const text = state.pending.shift();
        if (text === undefined) return;
        yield* this.turn(connection, state, session.id, [text]);
      }
    } catch (err) {
      if (state.aborted) return;
      const message = err instanceof Error ? err.message : String(err);
      yield { type: "error", message, retryable: false };
      throw err;
    } finally {
      state.connection = null;
      state.sessionId = null;
      connection.close();
    }
  }

  /** Resume the stored session; one this host no longer holds becomes a new session. */
  private async openSession(
    connection: AcpConnection,
    input: QueryInput,
  ): Promise<{ id: string; fresh: boolean }> {
    const base = {
      cwd: input.cwd,
      mcpServers: acpMcpServers(this.options.mcpServers ?? {}),
    };
    if (input.continuation) {
      try {
        // Replayed history arrives while onUpdate is unset, so it is not output.
        await connection.request("session/load", { ...base, sessionId: input.continuation });
        return { id: input.continuation, fresh: false };
      } catch (err) {
        if (!this.isSessionInvalid(err)) throw err;
      }
    }
    const systemAppend = this.sessionContext(input);
    const created = await connection.request("session/new", {
      ...base,
      ...(systemAppend
        ? { _meta: { antigravity: { systemPrompt: { append: systemAppend } } } }
        : {}),
    });
    if (typeof created.sessionId !== "string") {
      throw new AgyAcpError("agy acp returned no session id");
    }
    return { id: created.sessionId, fresh: true };
  }

  /** Runner instructions and startup memory appended to a new session's system prompt. */
  private sessionContext(input: QueryInput): string | null {
    const parts = [
      input.systemContext?.instructions,
      memoryContextForSessionStart("startup", this.runtime.agentDir),
    ].filter((part): part is string => !!part?.trim());
    return parts.length ? parts.join("\n\n") : null;
  }

  private async *turn(
    connection: AcpConnection,
    state: QueryState,
    sessionId: string,
    texts: string[],
  ): AsyncGenerator<ProviderEvent> {
    const buffer: ProviderEvent[] = [];
    const outcome: { done: boolean; stopReason?: unknown; failure?: Error } = { done: false };
    // The result is the last assistant message, as for the other harnesses.
    let message = "";
    let messageId: unknown;
    let afterTool = false;

    connection.onUpdate = (update) => {
      buffer.push({ type: "activity" });
      if (update?.sessionUpdate === "tool_call") afterTool = true;
      if (update?.sessionUpdate === "agent_message_chunk") {
        const content = update.content as { type?: string; text?: string } | undefined;
        if (content?.type === "text" && content.text) {
          if (afterTool || update.messageId !== messageId) message = "";
          afterTool = false;
          messageId = update.messageId;
          message += content.text;
        }
      }
      kick(state);
    };
    state.inTurn = true;
    void connection
      .request("session/prompt", {
        sessionId,
        prompt: texts.map((text) => ({ type: "text", text })),
      })
      .then(
        (result) => {
          outcome.stopReason = result.stopReason;
        },
        (err: Error) => {
          outcome.failure = err;
        },
      )
      .finally(() => {
        outcome.done = true;
        kick(state);
      });

    try {
      while (true) {
        while (buffer.length) yield buffer.shift() as ProviderEvent;
        if (outcome.done || state.aborted) break;
        await new Promise<void>((resolve) => {
          state.wake = resolve;
        });
      }
    } finally {
      connection.onUpdate = null;
      state.inTurn = false;
    }
    if (state.aborted) return;
    if (outcome.failure) throw outcome.failure;
    if (outcome.stopReason !== "end_turn") {
      throw new AgyAcpError(`Agy stopped the turn: ${String(outcome.stopReason)}`);
    }
    yield { type: "result", text: message || null };
  }
}

registerProvider("agy", (options) => new AgyProvider(options));
